"""End-to-end API tests. Tests run in file order and share one database (like a real day of use)."""
import hashlib
import hmac
import json
import time
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app import automations, collab, dashboard, intake, maintenance, services, timing
from app.db import tx
from app.main import app
from conftest import CLAUDE, CLAUDE_CALLS, SENT, login

T = date.today()
S = {}  # ids shared between tests


def ids(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- sign-in & roles ----------

def test_signin_rules():
    with TestClient(app) as anon:
        assert anon.get("/", follow_redirects=False).status_code == 307
        assert anon.get("/api/projects").status_code == 401
        assert anon.get("/healthz").status_code == 200
        login(anon, "someone@gmail.com", "Outsider")
        assert anon.get("/api/projects").status_code == 401   # personal Gmail refused


def test_first_user_is_admin_second_is_staff(mgr, staff):
    assert mgr.me["role"] == "admin"
    assert staff.me["role"] == "staff"


def test_manager_cannot_see_admin_areas(mgr):
    with TestClient(app) as m2:
        me = login(m2, "pat@foresitecre.com", "Pat Manager")
        assert me["role"] == "staff"
        ids(mgr.patch(f"/api/users/{me['id']}", json={"role": "manager"}))
        assert m2.get("/api/me").json()["role"] == "manager"
        # managers run projects…
        pid = ids(m2.post("/api/projects", json={"name": "Mgr project"}))["id"]
        ids(m2.post("/api/tasks", json={"project_id": pid, "title": "x"}))
        assert m2.get("/api/dashboard?scope=team").status_code == 200
        assert m2.get("/api/users").status_code == 200 and m2.get("/api/roles").status_code == 200   # lists are readable
        # …but People and Reports & backups are admin-only
        for method, url, body in [("post", "/api/users", {"name": "x"}), ("patch", f"/api/users/{me['id']}", {"role": "admin"}),
                                  ("delete", "/api/users/1", None), ("post", "/api/roles", {"name": "x"}),
                                  ("post", "/api/users/match-blink", None), ("get", "/api/backups", None),
                                  ("post", "/api/backups/run", None), ("get", "/api/insights/time", None),
                                  ("get", "/api/automations", None), ("put", "/api/automations", {"blocked": 0}),
                                  ("get", "/api/summary/preview", None), ("post", "/api/summary/send", None),
                                  ("get", "/api/blink/messages?blink_user_id=x", None)]:
            r = getattr(m2, method)(url, **({"json": body} if body is not None else {}))
            assert r.status_code == 403, (method, url, r.status_code)
        ids(mgr.delete(f"/api/users/{me['id']}"))


def test_people_setup(mgr, staff):
    with tx() as c:
        c.execute("UPDATE users SET blink_user_id='u-boss' WHERE id=?", (mgr.me["id"],))
        c.execute("UPDATE users SET blink_user_id='u-sam' WHERE id=?", (staff.me["id"],))
    S["jo"] = ids(mgr.post("/api/users", json={"name": "Jordan Lee", "email": "jordan@foresitecre.com",
                                               "blink_user_id": "u-jo"}))["id"]
    S["dup"] = ids(mgr.post("/api/users", json={"name": "Jordan (dup)", "email": "jlee@foresitecre.com"}))["id"]
    people = ids(mgr.get("/api/users"))
    assert {p["name"] for p in people} >= {"Bethany Boss", "Sam Patel", "Jordan Lee"}


def test_staff_permissions(staff):
    assert staff.get("/api/users").status_code == 200      # names for assignee lists
    forbidden = [("post", "/api/projects", {"name": "x"}), ("post", "/api/templates", {"name": "x"}),
                 ("post", "/api/roles", {"name": "x"}), ("post", "/api/users", {"name": "x"}),
                 ("put", "/api/automations", {"blocked": 0}), ("get", "/api/backups", None),
                 ("get", "/api/summary/preview", None), ("get", "/api/insights/time", None),
                 ("get", "/api/dashboard?scope=team", None), ("post", "/api/intake/extract", {"data_b64": ""}),
                 ("delete", "/api/users/1", None), ("post", "/api/recurrences", {"template_id": 1, "name_pattern": "x"})]
    for method, url, body in forbidden:
        r = getattr(staff, method)(url, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (method, url, r.status_code)
    assert staff.get("/api/projects").status_code == 200
    assert staff.get("/api/dashboard").status_code == 200


def test_last_manager_and_self_protection(mgr):
    assert mgr.patch(f"/api/users/{mgr.me['id']}", json={"role": "staff"}).status_code == 400
    assert mgr.delete(f"/api/users/{mgr.me['id']}").status_code == 400


def test_merge_duplicate_person(mgr):
    ids(mgr.delete(f"/api/users/{S['dup']}?merge_into={S['jo']}"))
    assert S["dup"] not in {u["id"] for u in ids(mgr.get("/api/users"))}


# ---------- roles, templates, projects ----------

def test_roles_and_template(mgr, staff):
    roles = {r["name"]: r["id"] for r in ids(mgr.get("/api/roles"))}
    S["pm"], S["acct"] = roles["Property Manager"], roles["Head of Accounting"]
    ids(mgr.patch(f"/api/roles/{S['acct']}", json={"default_user_id": S["jo"]}))
    S["tpl"] = ids(mgr.post("/api/templates", json={"name": "Takeover", "items": []}))["id"]
    add = lambda **b: ids(mgr.post(f"/api/templates/{S['tpl']}/items", json=b))["id"]
    S["i_walk"] = add(title="Walk property", default_role_id=S["pm"], due_offset_days=1)
    S["i_gl"] = add(title="Set up GL", default_role_id=S["acct"], due_offset_days=3)
    S["i_keys"] = add(title="Collect keys", parent_item_id=S["i_walk"], default_assignee_id=mgr.me["id"])
    S["i_sign"] = add(title="Order signage", default_assignee_id=staff.me["id"], due_offset_days=5)
    ids(mgr.patch(f"/api/template-items/{S['i_walk']}", json={"estimate_minutes": 120}))
    ids(mgr.post(f"/api/template-items/{S['i_walk']}/checklist", json={"text": "Photos"}))
    ids(mgr.post(f"/api/template-items/{S['i_sign']}/dependencies", json={"depends_on_item_id": S["i_walk"]}))
    assert mgr.post(f"/api/template-items/{S['i_walk']}/dependencies",
                    json={"depends_on_item_id": S["i_sign"]}).status_code == 400   # loop refused
    ids(mgr.post(f"/api/template-items/{S['i_sign']}/move?direction=-1"))
    ids(mgr.patch(f"/api/templates/{S['tpl']}", json={"name": "Management Takeover", "description": "d"}))
    t = [t for t in ids(mgr.get("/api/templates")) if t["id"] == S["tpl"]][0]
    assert t["name"] == "Management Takeover"
    assert [i["title"] for i in t["items"]][:2] == ["Walk property", "Collect keys"]


def test_create_project_from_template_with_team(mgr, staff):
    r = ids(mgr.post("/api/projects", json={
        "name": "100 Elm", "template_id": S["tpl"], "start_date": T.isoformat(), "property": "100 Elm St",
        "client": "Elm LLC", "team": {str(S["pm"]): staff.me["id"]}}))
    S["p1"] = r["id"]
    p = [p for p in ids(mgr.get("/api/projects")) if p["id"] == S["p1"]][0]
    by = {t["title"]: t for t in p["tasks"]}
    assert by["Walk property"]["assignee_name"] == "Sam Patel" and by["Walk property"]["estimate_minutes"] == 120
    assert by["Set up GL"]["assignee_name"] == "Jordan Lee"                 # role default
    assert by["Collect keys"]["parent_id"] == by["Walk property"]["id"]     # subtask kept
    assert by["Walk property"]["check_total"] == 1
    assert by["Order signage"]["waiting_on"] == str(by["Walk property"]["id"])
    S.update(walk=by["Walk property"]["id"], gl=by["Set up GL"]["id"], keys=by["Collect keys"]["id"],
             sign=by["Order signage"]["id"])


def test_change_role_holder_moves_open_tasks(mgr):
    SENT.clear()
    assert ids(mgr.put(f"/api/projects/{S['p1']}/team/{S['pm']}", json={"user_id": S["jo"]}))["moved"] == 1
    assert any(b == "u-jo" and "Property Manager" in t for b, t in SENT)
    ids(mgr.put(f"/api/projects/{S['p1']}/team/{S['pm']}", json={"user_id": None}))
    with tx() as c:
        c.execute("UPDATE tasks SET assignee_id=(SELECT id FROM users WHERE name='Sam Patel') WHERE id=?", (S["walk"],))


def test_project_details_patch(mgr):
    ids(mgr.patch(f"/api/projects/{S['p1']}", json={"description": "3% fee", "client": "Elm Holdings"}))
    p = [p for p in ids(mgr.get("/api/projects")) if p["id"] == S["p1"]][0]
    assert p["client"] == "Elm Holdings" and p["description"] == "3% fee"


# ---------- tasks: permissions, subtasks, checklist, comments ----------

def test_task_permissions(staff, mgr):
    assert staff.patch(f"/api/tasks/{S['gl']}", json={"status": "done"}).status_code == 403   # not theirs
    assert staff.patch(f"/api/tasks/{S['walk']}", json={"assignee_id": 1}).status_code == 403  # can't reassign
    assert staff.post("/api/tasks", json={"project_id": S["p1"], "title": "x"}).status_code == 403
    sub = ids(staff.post("/api/tasks", json={"parent_id": S["walk"], "title": "Book locksmith"}))   # own parent: ok
    assert ids(mgr.get(f"/api/tasks/{sub['id']}"))["parent_id"] == S["walk"]
    ck = ids(staff.post(f"/api/tasks/{S['walk']}/checklist", json={"text": "Roof"}))["id"]
    ids(staff.patch(f"/api/checklist/{ck}", json={"done": True}))
    assert staff.post(f"/api/tasks/{S['gl']}/checklist", json={"text": "x"}).status_code == 403
    assert staff.post(f"/api/tasks/{S['walk']}/dependencies", json={"depends_on_id": S["gl"]}).status_code == 403
    assert staff.delete(f"/api/tasks/{S['walk']}").status_code == 403


def test_comments_and_mentions(staff):
    SENT.clear()
    r = ids(staff.post(f"/api/tasks/{S['gl']}/comments", json={"body": "@jordan GL codes attached, @boss fyi"}))
    assert set(r["mentioned"]) == {"Jordan Lee", "Bethany Boss"}
    assert {b for b, _ in SENT} == {"u-jo", "u-boss"}
    assert staff.post(f"/api/tasks/{S['gl']}/comments", json={"body": "  "}).status_code == 400
    assert any(h["handle"] == "jordan" for h in ids(staff.get("/api/handles")))


# ---------- automations ----------

def test_blocked_and_your_turn_and_project_done(mgr, staff):
    SENT.clear()
    ids(staff.patch(f"/api/tasks/{S['walk']}", json={"status": "blocked", "note": "no access"}))
    assert any(b == "u-boss" and "Blocked" in t for b, t in SENT)
    SENT.clear()
    for tid in (S["keys"], S["walk"]):
        ids(mgr.patch(f"/api/tasks/{tid}", json={"status": "done"}))
    for t in ids(mgr.get(f"/api/tasks/{S['walk']}"))["subtasks"]:
        ids(mgr.patch(f"/api/tasks/{t['id']}", json={"status": "done"}))
    assert any(b == "u-sam" and "Your turn" in t for b, t in SENT)          # signage was waiting on walk
    ids(staff.patch(f"/api/tasks/{S['sign']}", json={"status": "done"}))
    SENT.clear()
    services.handle_inbound("u-jo", f"done {S['gl']}")
    assert any(b == "u-boss" and "Project complete" in t for b, t in SENT)
    p = [p for p in ids(mgr.get("/api/projects")) if p["id"] == S["p1"]][0]
    assert p["status"] == "done"
    ids(mgr.patch(f"/api/tasks/{S['gl']}", json={"status": "in_progress"}))           # reopen
    assert [p for p in ids(mgr.get("/api/projects")) if p["id"] == S["p1"]][0]["status"] == "active"


def test_overdue_escalation_once(mgr):
    late = ids(mgr.post("/api/tasks", json={"project_id": S["p1"], "title": "Late thing", "assignee_id": S["jo"],
                                           "due_date": (T - timedelta(days=5)).isoformat()}))["id"]
    S["late"] = late
    assert automations.overdue_escalation() >= 1
    assert automations.overdue_escalation() == 0
    ids(mgr.put("/api/automations", json={"overdue_days": 0}))
    assert ids(mgr.get("/api/automations"))["overdue_days"] == "0"
    ids(mgr.put("/api/automations", json={"overdue_days": 2}))


# ---------- time tracking ----------

def test_time_tracking(mgr, staff):
    assert [timing.parse_duration(x) for x in ["1h30", "90m", "1.5h", "1:15", "2 hours", "abc"]] == [90, 90, 90, 75, 120, None]
    assert staff.patch(f"/api/tasks/{S['walk']}", json={"actual": "a while"}).status_code == 400
    ids(staff.patch(f"/api/tasks/{S['walk']}", json={"actual": "2h30", "time_notes": "owner late"}))
    t = ids(mgr.get(f"/api/tasks/{S['walk']}"))
    assert t["actual_minutes"] == 150 and t["completed_at"] and t["created_at"]
    reply = services.handle_inbound("u-sam", f"time {S['sign']} 45m quick one")
    assert "Logged 45m" in reply
    ins = ids(mgr.get("/api/insights/time"))
    assert ins["overall"]["count"] >= 2 and any(x["title"] == "Walk property" for x in ins["tasks"])


# ---------- Blink chat ----------

def test_blink_commands():
    assert "don't recognize" in services.handle_inbound("u-stranger", "hi")
    assert "open tasks" in services.handle_inbound("u-jo", "list").lower()
    assert "Skipped" in services.handle_inbound("u-sam", f"done {S['late']}")          # not Sam's task
    assert "Comment added" in services.handle_inbound("u-sam", f"comment {S['gl']} thanks @jordan")
    assert "remind you" in services.handle_inbound("u-sam", "remind me 2030-01-02 09:30 renew insurance")
    CLAUDE.append({"updates": [{"task_id": S["late"], "status": "done", "note": "", "actual_minutes": 30,
                                "time_note": "easy"}], "reply": "Nice."})
    r = services.handle_inbound("u-jo", "finished the late thing, took half an hour")
    assert "Done" in r
    assert ids_task(S["late"])["actual_minutes"] == 30


def ids_task(tid):
    with tx() as c:
        return dict(c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())


def test_webhook_signature(mgr):
    with TestClient(app) as anon:
        body = json.dumps({"@type": "blink.chat.message-sent",
                           "detail": {"message_id": {"chat_id": "c-1", "local_message_id": "m-1"}}}).encode()
        t = str(int(time.time()))
        hdrs = {"content-type": "application/json", "x-event-id": "e1", "x-event-type": "blink.chat.message-sent"}
        h = "content-type x-event-id x-event-type"
        v1 = hmac.new(b"whsec", (t + "." + h + "." + ".".join(hdrs[k] for k in h.split()) + ".").encode() + body,
                      hashlib.sha256).hexdigest()
        import app.blink as b
        b.get_message = lambda c, m: {"sender": {"@type": "app"}}
        ok = anon.post("/blink/webhook", content=body, headers={**hdrs, "X-Signature": f"t={t},h={h},v1={v1}"})
        bad = anon.post("/blink/webhook", content=body, headers={**hdrs, "X-Signature": f"t={t},h={h},v1={'0' * 64}"})
        assert ok.status_code == 200 and bad.status_code == 401


# ---------- start a project from a document ----------

def _draft(**k):
    d = {"project_name": "", "property": "", "client": "", "start_date": "", "template_id": 0, "team": [], "notes": ""}
    d.update(k)
    return d


def test_intake_conversation(mgr, staff):
    full = _draft(project_name="200 Oak – Takeover", property="200 Oak", client="Oak LLC",
                  start_date=(T + timedelta(days=40)).isoformat(), template_id=S["tpl"],
                  team=[{"role_id": S["pm"], "user_id": staff.me["id"]}, {"role_id": 999, "user_id": 1}])
    intake.download_blink_file = lambda aid: (b"%PDF-1.4 fake", "application/pdf")
    CLAUDE.append({"draft": _draft(project_name="200 Oak – Takeover", template_id=S["tpl"]),
                   "reply": "Who will be the **Property Manager**?", "asking_to_confirm": False,
                   "user_confirmed": False, "cancelled": False})
    assert "Property Manager" in services.handle_inbound("u-boss", "Here's the management agreement",
                                                          [{"attachment_id": "a-1"}])
    assert CLAUDE_CALLS[-1]["messages"][-1]["content"][0]["type"] == "document"
    CLAUDE.append({"draft": full, "reply": "Create it?", "asking_to_confirm": True, "user_confirmed": False,
                   "cancelled": False})
    services.handle_inbound("u-boss", "Sam")
    CLAUDE.append({"draft": full, "reply": "", "asking_to_confirm": False, "user_confirmed": True, "cancelled": False})
    assert "Created" in services.handle_inbound("u-boss", "yes")
    p = [p for p in ids(mgr.get("/api/projects")) if p["name"] == "200 Oak – Takeover"][0]
    assert p["client"] == "Oak LLC" and any(m["user_name"] == "Sam Patel" for m in p["team"])
    assert intake.get_session(mgr.me["id"]) is None
    # staff can't start projects by chat; cancel works
    assert intake.get_session(staff.me["id"]) is None
    services.handle_inbound("u-sam", "new project for 9 Pine")
    assert intake.get_session(staff.me["id"]) is None
    CLAUDE.append({"draft": _draft(), "reply": "What's the property?", "asking_to_confirm": False,
                   "user_confirmed": False, "cancelled": False})
    services.handle_inbound("u-boss", "start a project for a new listing")
    assert "cancelled" in services.handle_inbound("u-boss", "cancel").lower()


def test_intake_web_extract(mgr):
    import base64
    CLAUDE.append({"draft": _draft(project_name="Web Oak", team=[{"role_id": S["pm"], "user_id": S["jo"]}]),
                   "reply": "Found it.", "asking_to_confirm": False, "user_confirmed": False, "cancelled": False})
    r = ids(mgr.post("/api/intake/extract", json={"filename": "a.pdf", "content_type": "application/pdf",
                                                  "data_b64": base64.b64encode(b"%PDF-1 x").decode()}))
    assert r["draft"]["project_name"] == "Web Oak"


# ---------- dashboards, reminders, recap ----------

def test_dashboards_reminders_recap(mgr, staff):
    me = ids(staff.get("/api/dashboard"))
    for k in ("counts", "agenda", "blocked", "waiting_on_me", "projects", "reminders", "activity", "done_week"):
        assert k in me
    team = ids(mgr.get("/api/dashboard?scope=team"))
    assert team["counts"]["projects"] >= 1 and team["workload"]
    rid = ids(staff.post("/api/reminders", json={"text": "Call owner",
                                                 "remind_at": (datetime.now() - timedelta(minutes=1)).isoformat(timespec="minutes")}))["id"]
    assert staff.post("/api/reminders", json={"text": "x", "remind_at": "not a date"}).status_code == 400
    SENT.clear()
    assert dashboard.send_due_reminders() >= 1 and any("Call owner" in t for _, t in SENT)
    ids(staff.patch(f"/api/reminders/{rid}"))
    ids(staff.patch("/api/me/prefs", json={"daily_digest": True}))
    r = ids(staff.post("/api/me/digest-preview"))
    assert r["text"] and "Your day" in r["text"]
    SENT.clear()
    assert dashboard.send_daily_digests(force=True) >= 1


# ---------- scheduled jobs & admin ----------

def test_scheduled_jobs_run_cleanly(mgr):
    tid = ids(mgr.post("/api/templates", json={"name": "Close", "items": [{"title": "Rent roll"}]}))["id"]
    ids(mgr.post("/api/recurrences", json={"template_id": tid, "name_pattern": "{month} {year} close", "day_of_month": 1}))
    n = len(ids(mgr.get("/api/projects")))
    services.run_recurrences()
    assert len(ids(mgr.get("/api/projects"))) == n                  # no duplicate for the same month
    maintenance.backup_now()
    assert ids(mgr.get("/api/backups"))
    services.send_reminders(force=True)
    maintenance.send_weekly_summary(force=True)
    assert "Weekly task summary" in ids(mgr.get("/api/summary/preview"))["text"]
    h = ids(mgr.get("/healthz"))
    assert h["ok"] and h["sign_in_enabled"]


def test_save_as_template_round_trip(mgr):
    tid = ids(mgr.post(f"/api/projects/{S['p1']}/save-as-template", json={"name": "Elm copy"}))["id"]
    t = [t for t in ids(mgr.get("/api/templates")) if t["id"] == tid][0]
    walk = [i for i in t["items"] if i["title"] == "Walk property"][0]
    assert walk["default_role_id"] is None or walk["default_role_id"] == S["pm"]
    assert any(i["waiting_on"] for i in t["items"])                     # dependency kept
    assert any(i["depth"] == 1 for i in t["items"])                      # subtask kept


def test_delete_task_with_subtasks(mgr):
    parent = ids(mgr.post("/api/tasks", json={"project_id": S["p1"], "title": "Temp parent"}))["id"]
    ids(mgr.post("/api/tasks", json={"parent_id": parent, "title": "Temp child"}))
    ids(mgr.delete(f"/api/tasks/{parent}"))
    titles = {t["title"] for p in ids(mgr.get("/api/projects")) for t in p["tasks"]}
    assert "Temp parent" not in titles and "Temp child" not in titles


def test_every_page_and_static(mgr):
    assert mgr.get("/").status_code == 200
    for url in ["/api/me", "/api/config", "/api/users", "/api/templates", "/api/projects", "/api/recurrences",
                "/api/roles", "/api/handles", "/api/automations", "/api/insights/time", "/api/summary/preview",
                f"/api/tasks/{S['walk']}", f"/api/tasks/{S['walk']}/updates", "/api/dashboard",
                "/api/dashboard?scope=team", "/api/blink/messages?blink_user_id=u-sam"]:
        assert mgr.get(url).status_code == 200, url


def test_tasks_without_project(mgr, staff):
    assert staff.post("/api/tasks", json={"title": "x", "assignee_id": S["jo"]}).status_code == 403   # not for others
    mine = ids(staff.post("/api/tasks", json={"title": "Renew notary", "estimate": "30m"}))["id"]
    t = ids(mgr.get(f"/api/tasks/{mine}"))
    assert t["assignee_id"] == staff.me["id"] and t["project_name"] == "No project" and t["estimate_minutes"] == 30
    assert staff.post("/api/tasks", json={"title": "x", "project_id": S["p1"]}).status_code == 403
    gen = [p for p in ids(mgr.get("/api/projects")) if p["is_general"]]
    assert len(gen) == 1
    other = ids(mgr.post("/api/tasks", json={"title": "Order toner", "assignee_id": S["jo"]}))["id"]
    ids(mgr.put(f"/api/tasks/{other}/project", json={"project_id": S["p1"]}))
    assert ids(mgr.get(f"/api/tasks/{other}"))["project_id"] == S["p1"]
    ids(mgr.put(f"/api/tasks/{other}/project", json={"project_id": None}))
    assert ids(mgr.get(f"/api/tasks/{other}"))["project_id"] == gen[0]["id"]
    assert staff.put(f"/api/tasks/{mine}/project", json={"project_id": S["p1"]}).status_code == 403
    SENT.clear()
    ids(staff.patch(f"/api/tasks/{mine}", json={"status": "done"}))
    assert not any("Project complete" in t for _, t in SENT)          # the hidden bucket never "completes"
    assert all(p["name"] != "No project" for p in ids(mgr.get("/api/dashboard?scope=team"))["projects"])


def test_week_ahead_message(mgr, staff):
    from app import extras
    ids(mgr.post("/api/tasks", json={"title": "Due this week", "assignee_id": staff.me["id"],
                                    "due_date": (T + timedelta(days=2)).isoformat(), "estimate": "2h"}))
    text = extras.build_week_ahead(staff.me["id"])
    assert "week ahead" in text and "Due this week" in text
    r = ids(staff.post("/api/me/week-ahead-preview"))
    assert r["sent"] == 1 and r["text"]
    ids(staff.patch("/api/me/prefs", json={"week_ahead": False}))
    assert extras.send_week_ahead(force=True, only_user=staff.me["id"]) == 0
    ids(staff.patch("/api/me/prefs", json={"week_ahead": True}))
    assert extras.send_week_ahead() in (0, 1, 2, 3, 4)            # runs cleanly whatever the weekday


def test_unread_mentions(mgr, staff):
    ids(staff.post("/api/me/mentions-seen"))
    assert ids(staff.get("/api/me/unread"))["mentions"] == 0
    time.sleep(1.1)  # comments are timestamped to the second
    ids(mgr.post(f"/api/tasks/{S['walk']}/comments", json={"body": "@sam can you check the roof?"}))
    ids(mgr.post(f"/api/tasks/{S['walk']}/comments", json={"body": "no mention here"}))
    assert ids(staff.get("/api/me/unread"))["mentions"] == 1
    ids(staff.post("/api/me/mentions-seen"))
    assert ids(staff.get("/api/me/unread"))["mentions"] == 0


def test_calendar_feed(mgr, staff):
    link = ids(staff.get("/api/me/calendar"))
    assert link["webcal"].startswith("webcal://") and "calendar.google.com" in link["google"]
    path = link["url"].split("testserver")[-1]
    with TestClient(app) as anon:                                  # Google fetches it without signing in
        r = anon.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
        body = r.text
        assert body.startswith("BEGIN:VCALENDAR") and body.rstrip().endswith("END:VCALENDAR")
        assert "SUMMARY:#" in body and "DTSTART;VALUE=DATE:" in body and "renew insurance" in body.lower()
        assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))   # RFC 5545 line folding
        assert anon.get("/calendar/not-a-real-token.ics").status_code == 404
        new = ids(staff.get("/api/me/calendar?regenerate=true"))["url"].split("testserver")[-1]
        assert anon.get(path).status_code == 404 and anon.get(new).status_code == 200   # old link revoked


def test_starter_templates_and_samples(mgr, staff):
    cat = ids(mgr.get("/api/templates/starters"))
    keys = [c["key"] for c in cat]
    assert {"onboarding", "sale_handoff", "sale_retain"} <= set(keys)
    assert staff.post("/api/templates/starters", json={"keys": keys}).status_code == 403
    r = ids(mgr.post("/api/templates/starters", json={"keys": keys, "sample_projects": True}))
    assert len(r["templates"]) == len(keys) and len(r["samples"]) == len(keys)
    again = ids(mgr.post("/api/templates/starters", json={"keys": keys, "sample_projects": True}))
    assert again == {"templates": [], "samples": []}                                  # no duplicates
    t = [t for t in ids(mgr.get("/api/templates")) if t["name"].startswith("Property Onboarding")][0]
    assert all(i["default_role_id"] for i in t["items"])                              # every item routed by role
    assert any(i["waiting_on"] for i in t["items"]) and any(i["depth"] == 1 for i in t["items"])
    assert sum(len(i["checklist"]) for i in t["items"]) >= 20
    projects = ids(mgr.get("/api/projects"))
    samples = [p for p in projects if p["is_sample"]]
    assert len(samples) == len(keys)
    onb = [p for p in samples if "Onboarding" in p["name"]][0]
    assert onb["property"] and any(t["status"] == "done" for t in onb["tasks"])        # looks lived-in
    assert staff.delete(f"/api/projects/{onb['id']}").status_code == 403
    ids(mgr.delete(f"/api/projects/{onb['id']}"))
    gen = [p for p in projects if p["is_general"]][0]
    assert mgr.delete(f"/api/projects/{gen['id']}").status_code == 400
    assert ids(mgr.delete("/api/projects/samples"))["removed"] == len(keys) - 1
    assert not [p for p in ids(mgr.get("/api/projects")) if p["is_sample"]]
    assert ids(mgr.get(f"/api/tasks/{S['walk']}"))["id"] == S["walk"]                 # real projects untouched


def test_project_photos_and_streetview(mgr, staff):
    import base64
    import types

    from app import photos
    jpg = b"\xff\xd8\xff\xe0" + b"0" * 200
    b64 = base64.b64encode(jpg).decode()
    assert staff.post(f"/api/projects/{S['p1']}/photo", json={"data_b64": b64}).status_code == 403
    assert mgr.post(f"/api/projects/{S['p1']}/photo", json={"data_b64": base64.b64encode(b"not an image").decode()}).status_code == 400
    name = ids(mgr.post(f"/api/projects/{S['p1']}/photo", json={"data_b64": "data:image/jpeg;base64," + b64}))["photo_path"]
    r = staff.get(f"/api/projects/{S['p1']}/photo")
    assert r.status_code == 200 and r.content == jpg
    assert [p for p in ids(mgr.get("/api/projects")) if p["id"] == S["p1"]][0]["photo_path"] == name
    ids(mgr.delete(f"/api/projects/{S['p1']}/photo"))
    assert staff.get(f"/api/projects/{S['p1']}/photo").status_code == 404
    # Street View: no key -> 404 (page falls back to a map)
    photos.MAPS_KEY = ""
    assert staff.get(f"/api/projects/{S['p1']}/streetview").status_code == 404
    # with a key: metadata OK -> image fetched once and cached
    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(url)
        if url.endswith("/metadata"):
            return types.SimpleNamespace(json=lambda: {"status": "OK"})
        return types.SimpleNamespace(status_code=200, content=jpg)
    real_get, photos.httpx.get, photos.MAPS_KEY = photos.httpx.get, fake_get, "test-maps-key"
    try:
        assert staff.get(f"/api/projects/{S['p1']}/streetview").content == jpg
        assert staff.get(f"/api/projects/{S['p1']}/streetview").status_code == 200
        assert len(calls) == 2                                   # second request served from cache
        assert ids(mgr.get("/api/config"))["streetview"] is True
    finally:
        photos.httpx.get, photos.MAPS_KEY = real_get, ""


def test_demo_data_load_and_remove(mgr, staff):
    before_projects = {p["id"] for p in ids(mgr.get("/api/projects"))}
    before_users = {u["id"] for u in ids(mgr.get("/api/users"))}
    assert staff.post("/api/demo/load").status_code == 403
    SENT.clear()
    r = ids(mgr.post("/api/demo/load"))
    assert r["projects"] >= 8 and r["people"] == 4
    assert SENT == []                                                     # nothing sent to Blink while loading
    from app import blink as real_blink
    with real_blink.quiet():
        assert real_blink._real_send_dm("u-x", "hi") is False                # the real sender stays quiet too
    assert mgr.post("/api/demo/load").status_code == 400                   # can't double-load
    projects = ids(mgr.get("/api/projects"))
    sonterra = [p for p in projects if "777 E Sonterra Blvd (Onboarding)" in p["name"]][0]
    assert sonterra["property"] == "777 E Sonterra Blvd, San Antonio, TX 78258"
    assert any(m["role"] == "Property Manager" and m["user_id"] == mgr.me["id"] for m in sonterra["team"])
    assert any(t["status"] == "blocked" for t in sonterra["tasks"])
    home = ids(mgr.get("/api/dashboard"))
    assert home["counts"]["open"] > 0 and home["projects"] and home["reminders"]
    assert ids(mgr.get("/api/me/unread"))["mentions"] >= 1
    assert ids(mgr.get("/api/insights/time"))["overall"]["count"] > 0
    assert ids(mgr.get("/api/demo"))["loaded"]
    ids(mgr.delete("/api/demo"))
    assert {p["id"] for p in ids(mgr.get("/api/projects"))} == before_projects
    assert {u["id"] for u in ids(mgr.get("/api/users"))} == before_users
    assert not ids(mgr.get("/api/demo"))["loaded"]


def test_branded_signin_page_and_assets():
    with TestClient(app) as anon:
        r = anon.get("/login")
        assert r.status_code == 200 and "Sign in with Google" in r.text and "/brand/logo-white.png" in r.text
        assert anon.get("/login?go=1", follow_redirects=False).headers["location"].startswith("https://accounts.google.com/")
        for f in ("logo.png", "logo-white.png", "favicon.png", "hero.jpg"):
            assert anon.get(f"/brand/{f}").status_code == 200, f
        assert anon.get("/").status_code in (200, 307)


def test_tutorial_links(mgr, staff):
    tid = ids(mgr.post("/api/templates", json={"name": "Tutorial test", "items": []}))["id"]
    a = ids(mgr.post(f"/api/templates/{tid}/items", json={"title": "Set up GL codes", "default_assignee_id": staff.me["id"]}))["id"]
    b = ids(mgr.post(f"/api/templates/{tid}/items", json={"title": "Send welcome letter", "default_assignee_id": staff.me["id"]}))["id"]
    assert mgr.patch(f"/api/template-items/{a}", json={"tutorial_url": "not a link"}).status_code == 400
    ids(mgr.patch(f"/api/template-items/{a}", json={"tutorial_url": "loom.com/share/abc123"}))     # https:// added
    ids(mgr.post(f"/api/template-items/{b}/dependencies", json={"depends_on_item_id": a}))
    ids(mgr.patch(f"/api/template-items/{b}", json={"tutorial_url": "https://youtu.be/dQw4w9WgXcQ"}))
    SENT.clear()
    pid = ids(mgr.post("/api/projects", json={"name": "Tutorial project", "template_id": tid}))["id"]
    tasks = {t["title"]: t for t in [p for p in ids(mgr.get("/api/projects")) if p["id"] == pid][0]["tasks"]}
    assert tasks["Set up GL codes"]["tutorial_url"] == "https://loom.com/share/abc123"
    assert any("How-to: https://loom.com/share/abc123" in t for _, t in SENT)          # in the assignment DM
    SENT.clear()
    ids(staff.patch(f"/api/tasks/{tasks['Set up GL codes']['id']}", json={"status": "done"}))
    assert any("Your turn" in t and "youtu.be" in t for _, t in SENT)                  # "your turn" carries the how-to
    assert staff.patch(f"/api/tasks/{tasks['Send welcome letter']['id']}", json={"tutorial_url": "https://x.com"}).status_code == 403
    ids(mgr.patch(f"/api/tasks/{tasks['Send welcome letter']['id']}", json={"tutorial_url": ""}))
    assert ids(mgr.get(f"/api/tasks/{tasks['Send welcome letter']['id']}"))["tutorial_url"] is None
    t2 = ids(mgr.post(f"/api/projects/{pid}/save-as-template", json={"name": "Tutorial copy"}))["id"]
    items = {i["title"]: i for i in [t for t in ids(mgr.get("/api/templates")) if t["id"] == t2][0]["items"]}
    assert items["Set up GL codes"]["tutorial_url"] == "https://loom.com/share/abc123"


def test_repeating_tasks_and_projects(mgr, staff):
    from app import recur
    assert recur.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)          # month-end safe
    assert recur.next_due("biweekly", date(2026, 1, 1)) == date(2026, 1, 15)
    # repeating task: done -> next one created once, checklist reset, repeat moves forward
    due = T + timedelta(days=2)
    tid = ids(staff.post("/api/tasks", json={"title": "Weekly site walk", "due_date": due.isoformat(), "repeat": "weekly"}))["id"]
    ids(staff.post(f"/api/tasks/{tid}/checklist", json={"text": "Parking lot lights"}))
    ck = ids(mgr.get(f"/api/tasks/{tid}"))["checklist"][0]["id"]
    ids(staff.patch(f"/api/checklist/{ck}", json={"done": True}))
    assert staff.post("/api/tasks", json={"title": "x", "repeat": "hourly"}).status_code == 400
    reply = services.handle_inbound("u-sam", f"done {tid}")
    assert "Next one scheduled" in reply
    first = ids(mgr.get(f"/api/tasks/{tid}"))
    nxt = ids(mgr.get(f"/api/tasks/{first['repeat_next_id']}"))
    assert nxt["due_date"] == (due + timedelta(days=7)).isoformat() and nxt["repeat"] == "weekly"
    assert nxt["checklist"][0]["done"] == 0 and nxt["assignee_id"] == staff.me["id"]
    ids(staff.patch(f"/api/tasks/{tid}", json={"status": "todo"}))
    ids(staff.patch(f"/api/tasks/{tid}", json={"status": "done"}))
    assert ids(mgr.get(f"/api/tasks/{tid}"))["repeat_next_id"] == nxt["id"]       # no duplicate on re-complete
    # finished very late -> next due jumps to the future
    late = ids(staff.post("/api/tasks", json={"title": "Monthly filter change", "repeat": "monthly",
                                              "due_date": (T - timedelta(days=95)).isoformat()}))["id"]
    ids(staff.patch(f"/api/tasks/{late}", json={"status": "done"}))
    assert ids(mgr.get(f"/api/tasks/{ids(mgr.get(f'/api/tasks/{late}'))['repeat_next_id']}"))["due_date"] >= T.isoformat()
    ov = ids(mgr.get("/api/recurring"))
    assert any(t["id"] == nxt["id"] for t in ov["tasks"])
    ids(staff.patch(f"/api/tasks/{nxt['id']}", json={"repeat": ""}))                 # stop repeating
    assert not any(t["id"] == nxt["id"] for t in ids(mgr.get("/api/recurring"))["tasks"])
    # repeating project: needs a template; first copy now; quarterly only runs in the right months
    assert mgr.post("/api/projects", json={"name": "No tpl", "repeat": "monthly"}).status_code == 400
    tpl_id = ids(mgr.post("/api/templates", json={"name": "Quarterly review", "items": [{"title": "Owner review"}]}))["id"]
    start = date(T.year, T.month, 1)
    pid = ids(mgr.post("/api/projects", json={"name": "Sonterra Quarterly Review", "template_id": tpl_id, "repeat": "quarterly",
                                             "start_date": start.isoformat(), "property": "777 E Sonterra Blvd"}))["id"]
    p = [x for x in ids(mgr.get("/api/projects")) if x["id"] == pid][0]
    assert p["name"] == f"Sonterra Quarterly Review – {start:%B} {start.year}" and p["recurrence_id"]
    r = [x for x in ids(mgr.get("/api/recurring"))["projects"] if x["id"] == p["recurrence_id"]][0]
    assert r["every_label"] == "Every quarter" and r["next_run"] >= T.isoformat()
    rid = p["recurrence_id"]
    ours = lambda: [x for x in ids(mgr.get("/api/projects")) if x["recurrence_id"] == rid]
    with tx() as c:   # keep other tests' monthly recurrences out of this check
        c.execute("UPDATE recurrences SET active=0 WHERE id!=?", (rid,))
    n = len(ours())
    services.run_recurrences(today=recur.add_months(start, 1))      # next month: not a quarter month -> nothing
    assert len(ours()) == n
    made = services.run_recurrences(today=recur.add_months(start, 3))
    assert len(made) == 1 and len(ours()) == n + 1
    newp = [x for x in ids(mgr.get("/api/projects")) if x["id"] == made[0]][0]
    assert newp["property"] == "777 E Sonterra Blvd" and newp["tasks"][0]["title"] == "Owner review"
    ids(mgr.delete(f"/api/recurrences/{p['recurrence_id']}"))
    # template items can repeat; tasks made from them inherit it
    item = [t for t in ids(mgr.get("/api/templates")) if t["id"] == tpl_id][0]["items"][0]["id"]
    ids(mgr.patch(f"/api/template-items/{item}", json={"repeat": "monthly"}))
    p2 = ids(mgr.post("/api/projects", json={"name": "Inherit", "template_id": tpl_id}))["id"]
    assert [x for x in ids(mgr.get("/api/projects")) if x["id"] == p2][0]["tasks"][0]["repeat"] == "monthly"


def test_page_script_has_no_syntax_errors(tmp_path):
    """Catch typos (e.g. a stray apostrophe or bracket) in the web page's JavaScript before they reach users."""
    import os
    import shutil
    import subprocess
    if not shutil.which("node"):
        import pytest
        pytest.skip("node not installed")
    html = open(os.path.join(os.path.dirname(__file__), "..", "static", "index.html"), encoding="utf8").read()
    js = html[html.rindex("<script>") + 8: html.rindex("</script>")]
    f = tmp_path / "page.js"
    f.write_text(js, encoding="utf8")
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:500]
