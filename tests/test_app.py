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


def test_blink_follow_up_comment_uses_conversation(mgr):
    """“Add it to #141” → bot asks what → next message becomes a comment (bot remembers the exchange)."""
    with tx() as c:
        sam = c.execute("SELECT id FROM users WHERE blink_user_id='u-sam'").fetchone()["id"]
    tid = ids(mgr.post("/api/tasks", json={"title": "Property tax & insurance projections", "assignee_id": sam}))["id"]
    CLAUDE.append({"updates": [], "comments": [], "reply": f"Sure. What should I add to #{tid}?"})
    assert f"#{tid}" in services.handle_inbound("u-sam", f"Add it to #{tid}")
    CLAUDE.append({"updates": [], "comments": [{"task_id": tid, "body": "This is a test comment from Blink"}], "reply": ""})
    r = services.handle_inbound("u-sam", "This is a test comment from Blink")
    assert "Comment added" in r and "test comment from Blink" in r
    sent = CLAUDE_CALLS[-1]["messages"]
    assert sent[-3]["content"] == f"Add it to #{tid}" and "What should I add" in sent[-2]["content"]   # earlier turn included
    with tx() as c:
        assert c.execute("SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE 'This is a test comment%'", (tid,)).fetchone()


def test_reply_to_mention_comments_on_someone_elses_task(mgr):
    """Nicole @mentions Bethany on Nicole's task; Bethany replies "141 I see it" → comment on that task."""
    with tx() as c:
        sam = c.execute("SELECT id FROM users WHERE blink_user_id='u-sam'").fetchone()["id"]
        jo = c.execute("SELECT id FROM users WHERE blink_user_id='u-jo'").fetchone()["id"]
    tid = ids(mgr.post("/api/tasks", json={"title": "Leasing assumptions", "assignee_id": jo, "due_date": "2027-03-01"}))["id"]
    from app import collab
    collab.add_comment(tid, jo, "@sam this is the task that disappeared", source="web")
    assert any(b == "u-sam" and f"#{tid}" in t for b, t in SENT[-3:])
    CLAUDE.append({"updates": [], "comments": [{"task_id": tid, "body": "I see it"}], "reply": ""})
    r = services.handle_inbound("u-sam", f"{tid} I see it")
    assert "Comment added" in r
    assert "mentioned you" in CLAUDE_CALLS[-1]["messages"][-1]["content"]       # the notification was in context
    with tx() as c:
        assert c.execute("SELECT 1 FROM task_comments WHERE task_id=? AND body='I see it'", (tid,)).fetchone()


def test_far_off_tasks_still_on_my_home(mgr):
    tid = ids(mgr.post("/api/tasks", json={"title": "Far-off budget item", "assignee_id": mgr.me["id"], "due_date": "2027-06-30"}))["id"]
    assert tid in [a.get("id") for a in ids(mgr.get("/api/dashboard"))["agenda"] if a["kind"] == "task"]


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
    assert mgr.delete(f"/api/projects/{onb['id']}").status_code == 400                # must type the name
    assert mgr.delete(f"/api/projects/{onb['id']}", params={"confirm": "wrong"}).status_code == 400
    ids(mgr.delete(f"/api/projects/{onb['id']}", params={"confirm": onb["name"].upper()}))
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
    assert r["projects"] >= 8 and r["people"] == 8
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
    # the showcase: department heads, roof bids waiting on the owner, blocked reasons, high-fives
    depts = {d["name"]: d for d in ids(mgr.get("/api/departments"))}
    assert depts["Accounting"]["head_name"] and depts["Property Management"]["members"] >= 4
    roof = [p for p in ids(mgr.get("/api/projects")) if p["name"].endswith("Roof replacement")][0]
    rb = ids(mgr.get(f"/api/projects/{roof['id']}/bids"))
    assert len(rb["bids"]) == 3 and rb["approvals"][0]["status"] == "pending" and rb["recommendation"]
    with tx() as c:
        assert c.execute("SELECT COUNT(*) FROM tasks WHERE status='blocked' AND blocked_kind IS NOT NULL").fetchone()[0] >= 3
        assert c.execute("SELECT COUNT(*) FROM win_kudos").fetchone()[0] > 0
    assert ids(mgr.get("/api/fun/team"))["wins"]
    from app import properties as _props
    hq = _props.property_for_address("777 E Sonterra Blvd, San Antonio, TX 78258")
    roles = {c["role"] for c in ids(mgr.get(f"/api/properties/{hq}"))["contacts"]}
    assert {"Owner contact", "CPA", "Bank", "Lawyer", "Tax protest company"} <= roles
    with tx() as c:
        assert all(r[0].endswith("@example.com") for r in c.execute("SELECT email FROM property_contacts WHERE email IS NOT NULL"))
    feed = ids(mgr.get("/api/fun/team"))["wins"]
    assert any(w["kind"] == "shoutout" and w["icon"] == "🏆" for w in feed)
    from app import demo as _demo
    _demo.refresh_wins()
    # a demo loaded before sample wins existed (or where the first try found nobody) gets them on startup
    import json as _json
    from app.db import get_setting, set_setting
    m = _json.loads(get_setting("demo.manifest"))
    with tx() as c:
        for wid in m["sample_wins"]:
            c.execute("DELETE FROM win_kudos WHERE win_id=?", (wid,))
            c.execute("DELETE FROM wins WHERE id=?", (wid,))
    m["sample_wins"] = []
    m.pop("loaded_by", None)
    set_setting("demo.manifest", _json.dumps(m))
    _demo.backfill_contacts()
    assert len(_demo.manifest()["sample_wins"]) == len(_demo.SAMPLE_WINS)
    with tx() as c:   # someone deleted them by hand → the admin button puts them back
        c.execute("DELETE FROM wins WHERE kind='shoutout'")
    assert staff.post("/api/demo/fill").status_code == 403
    r = ids(mgr.post("/api/demo/fill"))
    assert r["wins"] == len(_demo.SAMPLE_WINS) and r["contacts"] > 0
    assert sum(w["kind"] == "shoutout" for w in ids(mgr.get("/api/fun/team"))["wins"]) >= 1
    ids(mgr.delete("/api/demo"))
    with tx() as c:
        assert not c.execute("SELECT 1 FROM property_contacts WHERE name='Margaret Ellison'").fetchone()
        assert not c.execute("SELECT 1 FROM wins WHERE kind='shoutout'").fetchone()
    left = [p for p in ids(mgr.get("/api/projects")) if p["id"] not in before_projects and not p["is_general"]]
    assert not left, [(p["name"], p["is_general"]) for p in left]
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


def test_creator_can_delete_own_project_only(mgr, staff):
    from app.db import tx
    mine = ids(mgr.post("/api/projects", json={"name": "Sam scratch project"}))["id"]
    theirs = ids(mgr.post("/api/projects", json={"name": "Boss project"}))["id"]
    with tx() as c:   # staff can't create projects on the web today; simulate one they started
        c.execute("UPDATE projects SET created_by=? WHERE id=?", (staff.me["id"], mine))
    assert staff.delete(f"/api/projects/{theirs}", params={"confirm": "Boss project"}).status_code == 403
    ids(staff.delete(f"/api/projects/{mine}", params={"confirm": "Sam scratch project"}))
    ids(mgr.delete(f"/api/projects/{theirs}", params={"confirm": "Boss project"}))


# ---------- properties & critical dates ----------

def test_property_page_and_critical_dates(mgr, staff):
    from datetime import date, timedelta
    from app import properties
    p1 = ids(mgr.post("/api/projects", json={"name": "Oak takeover", "property": "451 Bandera Rd, San Antonio, TX"}))["id"]
    p2 = ids(mgr.post("/api/projects", json={"name": "Oak CAM recon", "property": "451 Bandera Road, San Antonio, Texas"}))["id"]
    props = ids(mgr.get("/api/properties"))
    oak = [p for p in props if p["name"].startswith("451 Bandera")]
    assert len(oak) == 1 and oak[0]["projects"] == 2                       # both spellings → one property
    pid = oak[0]["id"]
    ids(mgr.post("/api/tasks", json={"title": "Walk the roof", "project_id": p1, "due_date": "2030-01-05"}))
    soon = (date.today() + timedelta(days=20)).isoformat()
    later = (date.today() + timedelta(days=300)).isoformat()
    with tx() as c:
        sam = c.execute("SELECT id FROM users WHERE blink_user_id='u-sam'").fetchone()["id"]
    before = len(SENT)
    r = ids(mgr.post(f"/api/properties/{pid}/dates", json={"dates": [
        {"kind": "option_notice", "title": "Renewal option notice due", "date": soon, "tenant": "Suite 110 – Coastal Coffee",
         "assignee_id": sam},
        {"kind": "lease_expiration", "date": later, "tenant": "Suite 110"}]}))
    assert len(r["ids"]) == 2
    # the near one is inside its 30-day window → task created for Sam + one Blink reminder (not one per offset)
    msgs = [t for b, t in SENT[before:] if b == "u-sam"]
    assert len(msgs) == 1 and "Critical date in 20 days" in msgs[0]
    page = ids(mgr.get(f"/api/properties/{pid}"))
    near = [d for d in page["dates"] if d["kind"] == "option_notice"][0]
    assert near["task_id"] and near["days_left"] == 20
    assert {pr["id"] for pr in page["projects"]} == {p1, p2} and any(t["title"] == "Walk the roof" for t in page["tasks"])
    assert properties.run_alerts() == 0                                    # idempotent
    # staff can see it, mark their own date handled, but not edit someone else's/add dates
    assert staff.get(f"/api/properties/{pid}").status_code == 200
    assert staff.post(f"/api/properties/{pid}/dates", json={"dates": []}).status_code == 403
    far = [d for d in page["dates"] if d["kind"] == "lease_expiration"][0]
    assert staff.patch(f"/api/critical-dates/{far['id']}", json={"status": "handled"}).status_code == 403
    ids(staff.patch(f"/api/critical-dates/{near['id']}", json={"status": "handled"}))
    assert ids_task(near["task_id"])["status"] == "done"                 # handling the date finishes its task
    # finishing the task also closes the date
    ids(mgr.patch(f"/api/critical-dates/{near['id']}", json={"status": "open"}))
    with tx() as c:
        c.execute("UPDATE tasks SET status='done' WHERE id=?", (near["task_id"],))
    properties.run_alerts()
    assert [d for d in ids(mgr.get(f"/api/properties/{pid}"))["dates"] if d["id"] == near["id"]][0]["status"] == "handled"
    # moving the date resets reminders and moves the task's due date
    ids(mgr.patch(f"/api/critical-dates/{far['id']}", json={"date": soon}))
    properties.run_alerts()
    far2 = [d for d in ids(mgr.get(f"/api/properties/{pid}"))["dates"] if d["id"] == far["id"]][0]
    assert far2["task_id"] and ids_task(far2["task_id"])["due_date"] == soon
    # renaming a project's address moves it to another property
    ids(mgr.patch(f"/api/projects/{p2}", json={"property": "99 Elm St, Austin, TX"}))
    assert ids(mgr.get(f"/api/properties/{pid}"))["projects"][0]["id"] == p1
    assert mgr.delete(f"/api/properties/{pid}").status_code == 400          # must type the name


def test_read_property_document_and_insurance_check(mgr, staff):
    import base64
    pid = ids(mgr.post("/api/properties", json={"address": "1 Doc Test Way, Austin, TX"}))["id"]
    assert ids(mgr.get(f"/api/properties/{pid}"))["insurance"]["state"] == "missing"
    CLAUDE.append({"doc_type": "insurance", "summary": "Commercial property policy, Travelers.",
                   "dates": [{"kind": "tax_protest", "title": "x", "date": "not a date", "repeat": "", "notes": "",
                              "quote": "", "confidence": "low"}],
                   "spend_limit": 0, "report_notes": "", "bank_name": "", "contacts": [
                       {"role": "Insurance agent", "name": "Pat Lee", "company": "Alamo Insurance", "email": "pat@x.com", "phone": ""}],
                   "insurance": {"carrier": "Travelers", "policy_number": "P-123", "expires": "2031-03-01",
                                 "foresite_additional_insured": "yes", "evidence": "Additional Insured endorsement CG 20 26"}})
    out = ids(mgr.post(f"/api/properties/{pid}/read-document", json={
        "filename": "policy.pdf", "content_type": "application/pdf", "data_b64": base64.b64encode(b"%PDF-1.4 ...").decode()}))
    assert [d["kind"] for d in out["dates"]] == ["insurance_renewal"]          # bad date dropped, renewal offered
    assert out["dates"][0]["date"] == "2031-03-01" and out["dates"][0]["repeat"] == "yearly"
    page = ids(mgr.get(f"/api/properties/{pid}"))
    assert page["insurance"]["state"] == "ok" and page["insurance"]["expires"] == "2031-03-01"   # check recorded
    assert len(page["docs"]) == 1 and not page["dates"] and not page["contacts"]               # rest awaits review
    assert mgr.get(f"/api/property-docs/{page['docs'][0]['id']}").content.startswith(b"%PDF")
    # a manager can override the check after looking themselves
    ids(mgr.patch(f"/api/property-docs/{page['docs'][0]['id']}", json={"insured_ok": False}))
    assert ids(mgr.get(f"/api/properties/{pid}"))["insurance"]["state"] == "not_named"
    # owner requirements + contacts
    ids(mgr.patch(f"/api/properties/{pid}", json={"spend_limit": 5000, "report_notes": "PDF by the 15th to owner + CPA"}))
    cid = ids(mgr.post(f"/api/properties/{pid}/contacts", json={"role": "CPA", "name": "Dana Cho", "company": "Cho CPA"}))["id"]
    assert mgr.post(f"/api/properties/{pid}/contacts", json={"role": "CPA"}).status_code == 400
    assert staff.post(f"/api/properties/{pid}/contacts", json={"role": "CPA", "name": "x"}).status_code == 403
    page = ids(mgr.get(f"/api/properties/{pid}"))
    assert page["spend_limit"] == 5000 and page["contacts"][0]["id"] == cid
    ids(mgr.patch(f"/api/properties/{pid}", json={"bank_name": "Broadway Bank", "bank_accounts": "Operating 1234567890"}))
    page = ids(mgr.get(f"/api/properties/{pid}"))
    assert page["bank_name"] == "Broadway Bank" and page["bank_accounts"] == "Operating ••7890"   # full number never kept
    sp = ids(staff.get(f"/api/properties/{pid}"))
    assert sp["bank_name"] == "Broadway Bank" and sp["bank_accounts"] is None and sp["bank_hidden"]   # staff: no accounts
    assert all(x["bank_accounts"] is None for x in ids(staff.get("/api/properties")))
    ids(mgr.post(f"/api/properties/{pid}/contacts", json={"role": "Bank", "name": "Sam Ortiz", "company": "Broadway Bank"}))


def test_repeating_property_date_rolls_forward(mgr):
    from datetime import date, timedelta
    from app import properties
    pid = ids(mgr.post("/api/properties", json={"address": "2 Report Rd, Austin, TX"}))["id"]
    due = (date.today() + timedelta(days=5)).isoformat()
    did = ids(mgr.post(f"/api/properties/{pid}/dates", json={"dates": [
        {"kind": "owner_report", "title": "Monthly owner report due", "date": due, "repeat": "monthly"}]}))["ids"][0]
    ids(mgr.patch(f"/api/critical-dates/{did}", json={"status": "handled"}))
    ds = ids(mgr.get(f"/api/properties/{pid}"))["dates"]
    nxt = [d for d in ds if d["status"] == "open"]
    assert len(nxt) == 1
    from app.recur import add_months
    assert nxt[0]["date"] == add_months(date.fromisoformat(due), 1).isoformat() and nxt[0]["repeat"] == "monthly"
    # finishing the next one's task also rolls it
    properties.run_alerts()
    with tx() as c:
        tid = c.execute("SELECT task_id FROM critical_dates WHERE id=?", (nxt[0]["id"],)).fetchone()["task_id"]
        c.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    properties.run_alerts()
    assert len([d for d in ids(mgr.get(f"/api/properties/{pid}"))["dates"] if d["status"] == "open"]) == 1


def test_roles_live_on_the_property(mgr):
    with tx() as c:
        sam = c.execute("SELECT id FROM users WHERE blink_user_id='u-sam'").fetchone()["id"]
        jo = c.execute("SELECT id FROM users WHERE blink_user_id='u-jo'").fetchone()["id"]
    pm = S["pm"]
    a = ids(mgr.post("/api/projects", json={"name": "Elm takeover", "property": "500 Elm Ave, Austin, TX", "team": {str(pm): jo}}))["id"]
    pid = ids(mgr.get("/api/properties/team-for", params={"address": "500 Elm Avenue, Austin, Texas"}))["property_id"]
    assert pid and ids(mgr.get("/api/properties/team-for", params={"address": "500 Elm Ave, Austin, TX"}))["team"][str(pm)] == jo
    # a second project at the same address picks up the property's team without choosing anyone
    b = ids(mgr.post("/api/projects", json={"name": "Elm CAM", "property": "500 Elm Avenue, Austin, TX"}))["id"]
    team_of = lambda p: {m["role_id"]: m["user_id"] for m in [x for x in ids(mgr.get("/api/projects")) if x["id"] == p][0]["team"]}
    assert team_of(b)[pm] == jo
    with tx() as c:   # role-assigned open tasks in both projects
        ta = c.execute("INSERT INTO tasks(project_id,title,assignee_id,role_id) VALUES(?,?,?,?)", (a, "PM thing A", jo, pm)).lastrowid
        tb = c.execute("INSERT INTO tasks(project_id,title,assignee_id,role_id) VALUES(?,?,?,?)", (b, "PM thing B", jo, pm)).lastrowid
    # change the role on the property → both projects' teams and tasks follow
    assert ids(mgr.put(f"/api/properties/{pid}/team/{pm}", json={"user_id": sam}))["moved"] == 2
    assert ids_task(ta)["assignee_id"] == sam and ids_task(tb)["assignee_id"] == sam
    assert team_of(a)[pm] == sam and team_of(b)[pm] == sam
    roles = {r["role_id"]: r["user_id"] for r in ids(mgr.get(f"/api/properties/{pid}"))["roles"]}
    assert roles[pm] == sam
    # changing it from a project's panel changes the property (and so the other project) too
    ids(mgr.put(f"/api/projects/{b}/team/{pm}", json={"user_id": jo}))
    assert ids_task(ta)["assignee_id"] == jo and team_of(a)[pm] == jo


def test_recently_deleted_and_activity_log(mgr, staff):
    pid = ids(mgr.post("/api/projects", json={"name": "Bin test project"}))["id"]
    t1 = ids(mgr.post("/api/tasks", json={"title": "Parent to delete", "project_id": pid}))["id"]
    sub = ids(mgr.post("/api/tasks", json={"title": "Child", "parent_id": t1}))["id"]
    other = ids(mgr.post("/api/tasks", json={"title": "Blocked by parent", "project_id": pid}))["id"]
    ids(mgr.post(f"/api/tasks/{other}/dependencies", json={"depends_on_id": t1}))
    ids(mgr.post(f"/api/tasks/{t1}/checklist", json={"text": "step one"}))
    ids(mgr.post(f"/api/tasks/{t1}/comments", json={"body": "a comment"}))
    ids(mgr.patch(f"/api/tasks/{t1}", json={"status": "in_progress", "due_date": "2030-02-02"}))
    # delete → in the bin; a new task created meanwhile may reuse the old id and must not be disturbed
    tr = ids(mgr.delete(f"/api/tasks/{t1}"))["trash_id"]
    newer = ids(mgr.post("/api/tasks", json={"title": "Created after delete", "project_id": pid}))["id"]
    assert staff.get("/api/trash").status_code == 403
    item = [i for i in ids(mgr.get("/api/trash")) if i["id"] == tr][0]
    assert item["label"] == "Parent to delete" and item["days_left"] == 30
    back = ids(mgr.post(f"/api/trash/{tr}/restore"))
    assert back["kind"] == "task" and ids_task(newer)["title"] == "Created after delete"
    rt = ids(mgr.get(f"/api/tasks/{back['id']}"))
    assert rt["title"] == "Parent to delete" and rt["status"] == "in_progress" and rt["due_date"] == "2030-02-02"
    assert [x["text"] for x in rt["checklist"]] == ["step one"] and len(rt["comments"]) == 1
    with tx() as c:
        assert c.execute("SELECT COUNT(*) FROM tasks WHERE parent_id=? AND title='Child'", (back["id"],)).fetchone()[0] == 1
        assert c.execute("SELECT 1 FROM task_dependencies WHERE task_id=? AND depends_on_id=?", (other, back["id"])).fetchone()
    assert mgr.post(f"/api/trash/{tr}/restore").status_code == 400                       # only once
    # whole project round-trip
    name = "Bin test project"
    trp = ids(mgr.delete(f"/api/projects/{pid}", params={"confirm": name}))["trash_id"]
    assert not [p for p in ids(mgr.get("/api/projects")) if p["name"] == name]
    np = ids(mgr.post(f"/api/trash/{trp}/restore"))["id"]
    proj = [p for p in ids(mgr.get("/api/projects")) if p["id"] == np][0]
    assert proj["name"] == name and {t["title"] for t in proj["tasks"]} >= {"Parent to delete", "Child", "Blocked by parent", "Created after delete"}
    # property page round-trip keeps dates/contacts and relinks projects
    prop = ids(mgr.post("/api/properties", json={"address": "3 Bin Blvd, Austin, TX"}))["id"]
    ids(mgr.post(f"/api/properties/{prop}/contacts", json={"role": "CPA", "name": "Bin CPA"}))
    ids(mgr.post(f"/api/properties/{prop}/dates", json={"dates": [{"kind": "tax_protest", "date": "2031-05-15"}]}))
    trq = ids(mgr.delete(f"/api/properties/{prop}", params={"confirm": "3 Bin Blvd"}))["trash_id"]
    newp = ids(mgr.post(f"/api/trash/{trq}/restore"))["id"]
    page = ids(mgr.get(f"/api/properties/{newp}"))
    assert page["contacts"][0]["name"] == "Bin CPA" and page["dates"][0]["kind"] == "tax_protest"
    # who did what
    acts = ids(mgr.get("/api/activity"))
    kinds = {(a["action"], a["label"]) for a in acts}
    assert ("deleted", "Parent to delete") in kinds and ("restored", "Parent to delete") in kinds
    assert ("due date changed", "Parent to delete") in kinds and ("status → In progress", "Parent to delete") in kinds
    assert ("created", name) in kinds and all(a["who"] for a in acts)
    assert staff.get("/api/activity").status_code == 403


def test_month_end_close_board(mgr):
    from datetime import date
    tpl = ids(mgr.post("/api/templates", json={"name": "Test Month-End Close", "items": [
        {"title": "Post receipts", "due_offset_days": 2}, {"title": "Bank rec", "due_offset_days": 8},
        {"title": "Owner report", "due_offset_days": 12}]}))["id"]
    ids(mgr.patch(f"/api/templates/{tpl}", json={"board_period": "month"}))
    month = "2030-03"
    a = ids(mgr.post("/api/projects", json={"name": "Close A", "template_id": tpl, "start_date": "2030-03-01", "property": "10 Close St, Austin, TX"}))["id"]
    b = ids(mgr.post("/api/projects", json={"name": "Close B", "template_id": tpl, "start_date": "2030-03-01", "property": "20 Close St, Austin, TX"}))["id"]
    ids(mgr.post("/api/projects", json={"name": "Close C Feb", "template_id": tpl, "start_date": "2030-02-01", "property": "30 Close St, Austin, TX"}))
    a_tasks = [t for t in [p for p in ids(mgr.get("/api/projects")) if p["id"] == a][0]["tasks"]]
    ids(mgr.patch(f"/api/tasks/{a_tasks[0]['id']}", json={"status": "done"}))
    board = ids(mgr.get("/api/close-board", params={"month": month, "template_id": tpl}))
    assert board["columns"] == ["Post receipts", "Bank rec", "Owner report"]
    assert [r["label"] for r in board["rows"]] == ["10 Close St", "20 Close St"]
    ra = board["rows"][0]
    assert ra["cells"]["Post receipts"]["status"] == "done" and ra["done"] == 1 and ra["next"]["title"] == "Bank rec"
    assert board["totals"]["Post receipts"] == 1 and board["prev"] == "2030-02" and board["next"] == "2030-04"
    assert [m["label"] for m in board["missing"]] == ["30 Close St"]                 # closed last month, not this month
    assert ids(mgr.get("/api/close-board", params={"period": "March", "template_id": tpl}))["period"] == date.today().strftime("%Y-%m")


def test_tracking_boards_periods_and_switch(mgr):
    tpl = ids(mgr.post("/api/templates", json={"name": "Test Inspection", "items": [{"title": "Walk"}, {"title": "Report"}]}))["id"]
    ids(mgr.post("/api/projects", json={"name": "Insp A", "template_id": tpl, "start_date": "2031-05-10", "property": "1 Q St, Austin, TX"}))
    ids(mgr.post("/api/projects", json={"name": "Insp B old", "template_id": tpl, "start_date": "2031-02-01", "property": "2 Q St, Austin, TX"}))
    # off by default: not offered as a board
    b = ids(mgr.get("/api/close-board", params={"template_id": tpl}))
    assert tpl not in [t["id"] for t in b["templates"]]
    ids(mgr.patch(f"/api/templates/{tpl}", json={"board_period": "quarter"}))
    assert [t for t in ids(mgr.get("/api/templates")) if t["id"] == tpl][0]["board_period"] == "quarter"
    b = ids(mgr.get("/api/close-board", params={"template_id": tpl, "period": "2031-Q2"}))
    assert b["period_kind"] == "quarter" and b["label"] == "Q2 2031" and b["prev"] == "2031-Q1" and b["next"] == "2031-Q3"
    assert [r["label"] for r in b["rows"]] == ["1 Q St"] and [m["label"] for m in b["missing"]] == ["2 Q St"]
    assert b["start_date"] == "2031-04-01" and "2031-Q1" in b["periods"]
    # ongoing: every open project, no periods
    ids(mgr.patch(f"/api/templates/{tpl}", json={"board_period": "none"}))
    b = ids(mgr.get("/api/close-board", params={"template_id": tpl}))
    assert b["period_kind"] == "none" and len(b["rows"]) == 2 and b["missing"] == []
    ids(mgr.patch(f"/api/templates/{tpl}", json={"board_period": None}))
    assert tpl not in [t["id"] for t in ids(mgr.get("/api/close-board"))["templates"]]


def test_starter_templates_come_with_boards(mgr):
    from app import starter_templates
    starter_templates.install(["tax_protest", "inspection", "month_end"])
    by = {t["name"]: t["board_period"] for t in ids(mgr.get("/api/templates"))}
    assert by["Property Tax Protest"] == "year" and by["Quarterly Property Inspection"] == "quarter"
    assert by["Month-End Close"] == "month"


def test_vendors_ratings_properties_and_projects(mgr, staff):
    prop = ids(mgr.post("/api/properties", json={"address": "40 Vendor Ln, San Antonio, TX"}))["id"]
    green = ids(mgr.post("/api/vendors", json={"name": "Green Thumb Landscaping", "trades": ["Landscaping"], "phone": "210-555-0101"}))["id"]
    brown = ids(staff.post("/api/vendors", json={"name": "Brown Lawn Co", "trades": ["Landscaping"]}))["id"]   # staff can add
    assert mgr.post("/api/vendors", json={"name": "green thumb landscaping"}).status_code == 400              # no duplicates
    # reviews → badges
    ids(mgr.post(f"/api/vendors/{green}/reviews", json={"stars": 5, "price": 4, "note": "Always on time", "property_id": prop}))
    ids(staff.post(f"/api/vendors/{green}/reviews", json={"stars": 4, "price": 5, "note": "Good edging"}))
    assert mgr.post(f"/api/vendors/{green}/reviews", json={"stars": 4, "price": 9}).status_code == 400
    ids(staff.post(f"/api/vendors/{brown}/reviews", json={"stars": 2, "note": "Missed two visits in June"}))
    assert mgr.post(f"/api/vendors/{brown}/reviews", json={"stars": 7}).status_code == 400
    vs = {v["name"]: v for v in ids(mgr.get("/api/vendors", params={"trade": "Landscaping"}))["vendors"]}
    assert vs["Green Thumb Landscaping"]["avg"] == 4.5 and vs["Green Thumb Landscaping"]["badge"] == "good"
    assert vs["Green Thumb Landscaping"]["price"] == 4 and vs["Green Thumb Landscaping"]["price_label"] == "Above average"
    assert vs["Brown Lawn Co"]["price"] is None
    assert vs["Brown Lawn Co"]["badge"] == "bad" and "Missed two visits" in vs["Brown Lawn Co"]["warning"]
    # property slot: Brown first, then swapped to Green → Brown becomes 'previously used'
    assert staff.put(f"/api/properties/{prop}/vendors", json={"trade": "Landscaping", "vendor_id": brown}).status_code == 403
    ids(mgr.put(f"/api/properties/{prop}/vendors", json={"trade": "Landscaping", "vendor_id": brown}))
    ids(mgr.put(f"/api/properties/{prop}/vendors", json={"trade": "Landscaping", "vendor_id": green}))
    slot = [s for s in ids(mgr.get(f"/api/properties/{prop}"))["vendors"]["slots"] if s["trade"] == "Landscaping"][0]
    assert [c["vendor_name"] for c in slot["current"]] == ["Green Thumb Landscaping"]
    assert [p["vendor_name"] for p in slot["past"]] == ["Brown Lawn Co"] and slot["past"][0]["ended"]
    ids(mgr.put(f"/api/properties/{prop}/vendors", json={"trade": "Utilities – Electric", "vendor_id": green}))   # adds the trade
    prof = ids(mgr.get(f"/api/vendors/{green}"))
    assert "Utilities – Electric" in prof["trades"] and {p["status"] for p in prof["properties"]} == {"current"}
    assert [p for p in prof["properties"] if p["trade"] == "Landscaping"][0]["avg_here"] == 5
    assert ids(mgr.get(f"/api/vendors/{brown}"))["properties"][0]["status"] == "past"
    # someone on the property's team may change its vendors
    with tx() as c:
        sam = c.execute("SELECT id FROM users WHERE blink_user_id='u-sam'").fetchone()["id"]
    ids(mgr.put(f"/api/properties/{prop}/team/{S['pm']}", json={"user_id": sam}))
    ids(staff.post(f"/api/properties/{prop}/vendor-slots", json={"trade": "Gutter cleaning"}))
    assert any(s["trade"] == "Gutter cleaning" for s in ids(mgr.get(f"/api/properties/{prop}/vendors"))["slots"])
    # projects
    pj = ids(mgr.post("/api/projects", json={"name": "Vendor Ln roof", "property": "40 Vendor Ln, San Antonio, TX"}))["id"]
    roofer = ids(mgr.post("/api/vendors", json={"name": "Top Roofing", "trades": ["Roofing"]}))["id"]
    pv = ids(staff.post(f"/api/projects/{pj}/vendors", json={"vendor_id": roofer, "trade": "Roofing"}))["id"]   # sam is PM there
    proj = [p for p in ids(mgr.get("/api/projects")) if p["id"] == pj][0]
    assert proj["vendors"][0]["vendor_name"] == "Top Roofing"
    assert ids(mgr.get(f"/api/vendors/{roofer}"))["projects"][0]["project_id"] == pj
    ids(staff.delete(f"/api/project-vendors/{pv}"))
    # reviews: only author or a manager can delete
    rid = ids(mgr.get(f"/api/vendors/{brown}"))["review_list"][0]["id"]
    assert ids(staff.delete(f"/api/vendor-reviews/{rid}"))["ok"]


def test_vendor_coi_per_property(mgr, staff):
    import base64
    from datetime import date, timedelta
    a = ids(mgr.post("/api/properties", json={"address": "50 Coi Ave, San Antonio, TX", "client": "Coi Owner LLC"}))["id"]
    b = ids(mgr.post("/api/properties", json={"address": "60 Coi Ave, San Antonio, TX"}))["id"]
    v = ids(mgr.post("/api/vendors", json={"name": "Coi Roofing", "trades": ["Roofing"]}))["id"]
    for p in (a, b):
        ids(mgr.put(f"/api/properties/{p}/vendors", json={"trade": "Roofing", "vendor_id": v}))
    slot = lambda p: [x for x in ids(mgr.get(f"/api/properties/{p}/vendors"))["slots"] if x["trade"] == "Roofing"][0]["current"][0]
    assert slot(a)["coi"]["state"] == "missing"
    exp = (date.today() + timedelta(days=200)).isoformat()
    CLAUDE.append({"is_coi": True, "named_insured": "Coi Roofing LLC", "carrier": "Hartford", "expires": exp,
                   "property_additional_insured": "yes", "foresite_additional_insured": "yes", "evidence": "AI: Coi Owner LLC"})
    doc = {"filename": "coi.pdf", "content_type": "application/pdf", "data_b64": base64.b64encode(b"%PDF coi").decode()}
    out = ids(mgr.post(f"/api/properties/{a}/vendors/{v}/coi", json=doc))
    assert out["status"]["state"] == "ok" and out["status"]["expires"] == exp
    assert slot(a)["coi"]["state"] == "ok" and slot(b)["coi"]["state"] == "missing"          # one property's COI ≠ another's
    dates = [d for d in ids(mgr.get(f"/api/properties/{a}"))["dates"] if d["kind"] == "vendor_coi"]
    assert len(dates) == 1 and dates[0]["date"] == exp and dates[0]["remind_days"] == "30,7"
    # a certificate that doesn't name the property
    CLAUDE.append({"is_coi": True, "named_insured": "Coi Roofing LLC", "carrier": "Hartford", "expires": exp,
                   "property_additional_insured": "no", "foresite_additional_insured": "unclear", "evidence": "Holder only"})
    out_b = ids(mgr.post(f"/api/properties/{b}/vendors/{v}/coi", json=doc))
    assert out_b["status"]["state"] == "not_named"
    ids(mgr.patch(f"/api/vendor-cois/{out_b['id']}", json={"insured_ok": True}))           # a person checked the endorsement
    assert slot(b)["coi"]["state"] == "ok"
    # replacing the certificate retires the old expiry reminder; staff can't upload unless on the property team
    CLAUDE.append({"is_coi": True, "named_insured": "x", "carrier": "x", "expires": (date.today() + timedelta(days=400)).isoformat(),
                   "property_additional_insured": "yes", "foresite_additional_insured": "yes", "evidence": ""})
    ids(mgr.post(f"/api/properties/{a}/vendors/{v}/coi", json=doc))
    open_coi = [d for d in ids(mgr.get(f"/api/properties/{a}"))["dates"] if d["kind"] == "vendor_coi" and d["status"] == "open"]
    assert len(open_coi) == 1 and open_coi[0]["date"] != exp
    assert staff.post(f"/api/properties/{a}/vendors/{v}/coi", json=doc).status_code == 403
    assert mgr.get(f"/api/vendor-cois/{out['id']}").content == b"%PDF coi"
    prof = ids(mgr.get(f"/api/vendors/{v}"))
    assert {p["coi"]["state"] for p in prof["properties"]} == {"ok"}
    # stop using the vendor → their COI reminders stop too
    pv = slot(a)["id"]
    ids(mgr.post(f"/api/property-vendors/{pv}/end"))
    assert not [d for d in ids(mgr.get(f"/api/properties/{a}"))["dates"] if d["kind"] == "vendor_coi" and d["status"] == "open"]


def test_expiring_reminders_go_to_property_manager(mgr):
    from datetime import date, timedelta
    from app import properties
    with tx() as c:
        sam = c.execute("SELECT id FROM users WHERE blink_user_id='u-sam'").fetchone()["id"]
        jo = c.execute("SELECT id FROM users WHERE blink_user_id='u-jo'").fetchone()["id"]
    pid = ids(mgr.post("/api/properties", json={"address": "70 Pm Way, Austin, TX"}))["id"]
    ids(mgr.put(f"/api/properties/{pid}/team/{S['pm']}", json={"user_id": sam}))
    soon = lambda n: (date.today() + timedelta(days=n)).isoformat()
    before = len(SENT)
    ids(mgr.post(f"/api/properties/{pid}/dates", json={"dates": [
        {"kind": "insurance_renewal", "title": "Policy renewal", "date": soon(20)},                     # nobody assigned
        {"kind": "tax_protest", "title": "Protest deadline", "date": soon(5), "assignee_id": jo}]}))     # someone else too
    got = [(b, t.split("\n")[0]) for b, t in SENT[before:]]
    assert ("u-sam", "☂️ **Critical date in 20 days:** Policy renewal") in got
    assert {b for b, t in got if "Protest deadline" in t} == {"u-sam", "u-jo"}
    d = [x for x in ids(mgr.get(f"/api/properties/{pid}"))["dates"] if x["title"] == "Policy renewal"][0]
    assert ids_task(d["task_id"])["assignee_id"] == sam                                             # task goes to the PM
    # PM changes → the next reminder goes to the new PM
    ids(mgr.put(f"/api/properties/{pid}/team/{S['pm']}", json={"user_id": jo}))
    before = len(SENT)
    properties.run_alerts(date.today() + timedelta(days=14))                                         # crosses the 7-day mark
    assert [b for b, t in SENT[before:] if "Policy renewal" in t] == ["u-jo"]


def test_department_heads_get_escalations(mgr, staff):
    from datetime import date, timedelta
    from app import departments
    with tx() as c:
        sam = c.execute("SELECT id FROM users WHERE blink_user_id='u-sam'").fetchone()["id"]
        jo = c.execute("SELECT id FROM users WHERE blink_user_id='u-jo'").fetchone()["id"]
        c.execute("DELETE FROM automation_log WHERE rule LIKE 'dept_%'")
    assert staff.post("/api/departments", json={"name": "X"}).status_code == 403          # admin only
    acct = ids(mgr.post("/api/departments", json={"name": "Test Accounting", "head_user_id": jo}))["id"]
    ids(mgr.patch(f"/api/users/{sam}", json={"department_id": acct}))
    d = [x for x in ids(mgr.get("/api/departments")) if x["id"] == acct][0]
    assert d["head_name"] and d["members"] == 2
    today = date.today()
    late = ids(mgr.post("/api/tasks", json={"title": "Late bank rec", "assignee_id": sam, "due_date": (today - timedelta(days=4)).isoformat()}))["id"]
    fresh = ids(mgr.post("/api/tasks", json={"title": "Barely late", "assignee_id": sam, "due_date": (today - timedelta(days=1)).isoformat()}))["id"]
    blk = ids(mgr.post("/api/tasks", json={"title": "Stuck on owner", "assignee_id": sam}))["id"]
    ids(mgr.patch(f"/api/tasks/{blk}", json={"status": "blocked", "note": "waiting on owner"}))
    with tx() as c:
        c.execute("UPDATE task_updates SET created_at=datetime('now','-5 days') WHERE task_id=? AND status='blocked'", (blk,))
    before = len(SENT)
    departments.run_escalations()
    msgs = [t for b, t in SENT[before:] if b == "u-jo"]
    digest = next(m for m in msgs if "Test Accounting" in m)
    assert "Late bank rec" in digest and "Stuck on owner" in digest and "Barely late" not in digest
    before = len(SENT)
    departments.run_escalations()                                                           # each item only once
    assert not [t for b, t in SENT[before:] if "Late bank rec" in t]
    # moving the due date allows a fresh escalation later; the head's own tasks don't go to themselves
    ids(mgr.patch(f"/api/tasks/{late}", json={"due_date": (today - timedelta(days=10)).isoformat()}))
    own = ids(mgr.post("/api/tasks", json={"title": "Head's own late task", "assignee_id": jo, "due_date": (today - timedelta(days=9)).isoformat()}))["id"]
    before = len(SENT)
    departments.run_escalations()
    new = SENT[before:]
    assert any(b == "u-jo" and "Late bank rec" in t for b, t in new)
    assert not any(b == "u-jo" and "Head's own late task" in t for b, t in new)            # goes to managers instead
    s = ids(mgr.get("/api/automations"))
    assert s["dept_overdue_days"] == "3" and ids(mgr.put("/api/automations", json={"dept_overdue_days": 5}))["dept_overdue_days"] == "5"
    ids(mgr.put("/api/automations", json={"dept_overdue_days": 3}))


def test_sample_vendors_load_and_remove(mgr, staff):
    assert staff.post("/api/vendors/samples").status_code == 403
    r = ids(mgr.post("/api/vendors/samples"))
    assert r["added"] >= 15
    assert ids(mgr.post("/api/vendors/samples"))["added"] == 0                       # no duplicates
    vs = ids(mgr.get("/api/vendors"))
    names = {v["name"]: v for v in vs["vendors"]}
    assert vs["samples"] >= 15 and names["River City Plumbing"]["avg"] and names["Spark & Wire Electric"]["badge"] == "bad"
    assert {t for v in vs["vendors"] for t in v["trades"]} >= {"Plumbing", "Electrical", "Janitorial", "Elevator", "Security"}
    assert ids(mgr.delete("/api/vendors/samples"))["removed"] == vs["samples"]
    assert ids(mgr.get("/api/vendors"))["samples"] == 0


def test_insurance_emails_drafted_for_property_manager(mgr, staff, monkeypatch):
    import base64 as b64
    from datetime import date, timedelta
    from app import emails
    with tx() as c:
        sam = c.execute("SELECT id FROM users WHERE blink_user_id='u-sam'").fetchone()["id"]
        c.execute("UPDATE email_drafts SET status='dismissed'")
    pid = ids(mgr.post("/api/properties", json={"address": "80 Mail St, San Antonio, TX", "client": "Mail Street Owner LLC"}))["id"]
    ids(mgr.put(f"/api/properties/{pid}/team/{S['pm']}", json={"user_id": sam}))
    v = ids(mgr.post("/api/vendors", json={"name": "Mail Test Roofing", "trades": ["Roofing"], "email": "office@mailroof.test",
                                          "contact_name": "Pat Jones"}))["id"]
    ids(mgr.put(f"/api/properties/{pid}/vendors", json={"trade": "Roofing", "vendor_id": v}))
    ids(mgr.post(f"/api/properties/{pid}/contacts", json={"role": "Insurance agent", "name": "Lee Agent", "email": "lee@agency.test"}))
    before = len(SENT)
    emails.run()
    mine = ids(staff.get("/api/emails"))["drafts"]
    coi = [d for d in mine if d["vendor_id"] == v][0]
    assert coi["to_email"] == "office@mailroof.test" and "Mail Street Owner LLC" in coi["body"] and "Foresite Real Estate" in coi["body"]
    assert coi["body"].startswith("Hi Pat,") and "80 Mail St" in coi["body"] and coi["compose_url"].startswith("https://mail.google.com/")
    ins = [d for d in mine if d["kind"] == "property_insurance" and d["property_id"] == pid][0]
    assert ins["to_email"] == "lee@agency.test" and "additional insured" in ins["body"]
    assert sum(1 for b, t in SENT[before:] if b == "u-sam" and "Email drafted" in t and "80 Mail St" in t) == 2   # PM told
    n = len(ids(staff.get("/api/emails"))["drafts"])
    emails.run()
    assert len(ids(staff.get("/api/emails"))["drafts"]) == n                                    # once per issue
    # a valid COI resolves the vendor draft
    CLAUDE.append({"is_coi": True, "named_insured": "x", "carrier": "x", "expires": (date.today() + timedelta(days=300)).isoformat(),
                   "property_additional_insured": "yes", "foresite_additional_insured": "yes", "evidence": ""})
    doc = {"filename": "coi.pdf", "content_type": "application/pdf", "data_b64": b64.b64encode(b"%PDF").decode()}
    ids(mgr.post(f"/api/properties/{pid}/vendors/{v}/coi", json=doc))
    emails.run()
    assert not [d for d in ids(staff.get("/api/emails"))["drafts"] if d["vendor_id"] == v]
    # a COI that doesn't name the owner → drafted right away
    CLAUDE.append({"is_coi": True, "named_insured": "x", "carrier": "x", "expires": (date.today() + timedelta(days=400)).isoformat(),
                   "property_additional_insured": "no", "foresite_additional_insured": "no", "evidence": "holder only"})
    out = ids(mgr.post(f"/api/properties/{pid}/vendors/{v}/coi", json=doc))
    d = [x for x in ids(staff.get("/api/emails"))["drafts"] if x["id"] == out["email_draft_id"]][0]
    assert "doesn't name Mail Street Owner LLC as additional insured" in d["body"]
    # PM edits and marks sent; other staff can't touch it
    ids(staff.patch(f"/api/emails/{d['id']}", json={"body": d["body"] + "\nP.S. thanks!", "status": "sent"}))
    assert not [x for x in ids(staff.get("/api/emails"))["drafts"] if x["id"] == d["id"]]
    # manual draft button
    m = ids(mgr.post("/api/emails", json={"property_id": pid, "vendor_id": v}))
    assert m["subject"] and m["status"] == "open"
    # with Gmail connected the draft also goes into the PM's Gmail Drafts
    calls = []
    class R:
        def __init__(self, code, data): self.status_code, self._d, self.text = code, data, ""
        def json(self): return self._d
    def fake_post(url, **kw):
        calls.append(url)
        return R(200, {"access_token": "at"}) if "oauth2" in url else R(200, {"id": "gmail-draft-1"})
    monkeypatch.setattr(emails.httpx, "post", fake_post)
    monkeypatch.setattr(emails.httpx, "put", lambda url, **kw: (calls.append(url), R(200, {"id": "gmail-draft-1"}))[1])
    emails.save_gmail_token(sam, "refresh-xyz")
    assert ids(staff.get("/api/emails"))["gmail_connected"]
    before = len(SENT)
    did = emails.draft_for_property(pid, sam, manual=True)
    assert emails.get(did)["gmail_draft_id"] == "gmail-draft-1" and any("gmail.googleapis.com" in u for u in calls)
    assert ids(staff.patch(f"/api/emails/{did}", json={"subject": "Updated"}))["in_gmail"]
    ids(staff.delete("/api/me/gmail"))
    assert not ids(staff.get("/api/emails"))["gmail_connected"]


def test_bids_recommendation_and_owner_approval(mgr, staff):
    import base64
    from fastapi.testclient import TestClient
    with tx() as c:
        sam = c.execute("SELECT id FROM users WHERE blink_user_id='u-sam'").fetchone()["id"]
    prop = ids(mgr.post("/api/properties", json={"address": "90 Bid Blvd, San Antonio, TX", "client": "Bid Blvd Owner LP"}))["id"]
    ids(mgr.patch(f"/api/properties/{prop}", json={"spend_limit": 5000}))
    ids(mgr.put(f"/api/properties/{prop}/team/{S['pm']}", json={"user_id": sam}))
    ids(mgr.post(f"/api/properties/{prop}/contacts", json={"role": "Owner contact", "name": "Olivia Owner", "email": "olivia@owner.test"}))
    pj = ids(mgr.post("/api/projects", json={"name": "Bid Blvd roof replacement", "property": "90 Bid Blvd, San Antonio, TX"}))["id"]
    good = ids(mgr.post("/api/vendors", json={"name": "Bid Good Roofing LLC", "trades": ["Roofing"]}))["id"]
    ids(mgr.post(f"/api/vendors/{good}/reviews", json={"stars": 5, "price": 4}))
    # a bid read from a PDF: amount/scope filled in and vendor matched by name
    CLAUDE.append({"vendor_name": "Bid Good Roofing", "amount": 48500, "scope": "Tear off and replace TPO roof",
                   "exclusions": "Deck repairs", "timeline": "3 weeks", "warranty": "20-yr NDL", "submitted_on": "2026-11-10"})
    pdf = base64.b64encode(b"%PDF bid one").decode()
    b1 = ids(staff.post(f"/api/projects/{pj}/bids", json={"filename": "good.pdf", "content_type": "application/pdf", "data_b64": pdf}))
    assert b1["read"]["amount"] == 48500
    b2 = ids(mgr.post(f"/api/projects/{pj}/bids", json={"vendor_name": "Cheap Roof Co", "amount": "$39,900", "scope": "Overlay only"}))
    info = ids(mgr.get(f"/api/projects/{pj}/bids"))
    by = {b["vendor_label"]: b for b in info["bids"]}
    assert by["Bid Good Roofing LLC"]["vendor_id"] == good and by["Bid Good Roofing LLC"]["exclusions"] == "Deck repairs"
    assert by["Bid Good Roofing LLC"]["avg"] == 5 and info["low"] == 39900
    assert mgr.post(f"/api/projects/{pj}/approval", json={}).status_code == 400              # recommend one first
    ids(staff.put(f"/api/projects/{pj}/recommendation", json={"bid_id": b1["id"], "text": "Full replacement with 20-yr warranty."}))
    assert ids(mgr.get(f"/api/projects/{pj}/bids"))["over_limit"]                             # 48.5k > 5k limit
    assert ids(mgr.post(f"/api/projects/{pj}/recommendation-draft"))["text"]
    r = ids(staff.post(f"/api/projects/{pj}/approval", json={}))
    token = r["link"].rsplit("/", 1)[1]
    d = [x for x in ids(staff.get("/api/emails"))["drafts"] if x["id"] == r["email_draft_id"]][0]
    assert d["to_email"] == "olivia@owner.test" and "Bid Good Roofing LLC" in d["body"] and token in d["body"]
    # the owner (not signed in) answers on the public page
    with TestClient(app) as owner:
        page = owner.get(f"/approve/{token}")
        assert page.status_code == 200 and "Bid Blvd roof replacement" in page.text and "Recommended" in page.text
        assert owner.get(f"/approve/{token}/bid/{b1['id']}").content == b"%PDF bid one"
        assert owner.get(f"/approve/{token}/bid/999999").status_code == 404
        bad = owner.post(f"/approve/{token}", data={"decision": "approved", "bid_id": str(b1["id"]), "name": "", "consent": "1"})
        assert bad.status_code == 400 and "your name" in bad.text
        nocon = owner.post(f"/approve/{token}", data={"decision": "approved", "bid_id": str(b1["id"]), "name": "Olivia Owner"})
        assert nocon.status_code == 400 and "electronic signature" in nocon.text
        # while the owner is deciding, the bids they're looking at can't be removed
        assert mgr.delete(f"/api/bids/{b2['id']}").status_code == 400
        before = len(SENT)
        ok = owner.post(f"/approve/{token}", data={"decision": "approved", "bid_id": str(b1["id"]), "name": "Olivia Owner",
                                                    "title": "Managing Member", "note": "Go ahead", "consent": "1"},
                        headers={"x-forwarded-for": "203.0.113.7", "user-agent": "OwnerPhone/1.0"})
        assert ok.status_code == 200 and "Approved" in ok.text and "receipt" in ok.text
        receipt = owner.get(f"/approve/{token}/receipt")
        assert receipt.status_code == 200 and "203.0.113.7" in receipt.text and "Managing Member" in receipt.text
        assert "Verified" in receipt.text and "OwnerPhone/1.0" in receipt.text
        assert any(b == "u-sam" and "Owner ✅" not in t and "approved" in t for b, t in SENT[before:])
        assert owner.post(f"/approve/{token}", data={"decision": "declined", "name": "X"}).status_code == 400   # once only
        assert owner.get("/approve/not-a-token").status_code == 404
    info = ids(mgr.get(f"/api/projects/{pj}/bids"))
    assert {b["vendor_label"]: b["status"] for b in info["bids"]} == {"Bid Good Roofing LLC": "selected", "Cheap Roof Co": "declined"}
    assert info["approvals"][0]["status"] == "approved" and info["approvals"][0]["decided_by"] == "Olivia Owner"
    aid = info["approvals"][0]["id"]
    from app import bids as B
    v = B.verify(aid)
    assert v["ok"] and v["files"][0]["match"] in (True, None)
    cert = mgr.get(f"/api/approvals/{aid}/certificate")
    assert cert.status_code == 200 and v["hash"] in cert.text and "viewed" in cert.text      # link opened, then signed
    with tx() as c:   # tampering with the stored record is detectable
        c.execute("UPDATE project_approvals SET evidence=replace(evidence, '48500', '4850') WHERE id=?", (aid,))
    assert not B.verify(aid)["ok"]
    assert mgr.delete(f"/api/bids/{b1['id']}").status_code == 400                               # approved bids are locked
    proj = [p for p in ids(mgr.get("/api/projects")) if p["id"] == pj][0]
    assert any(v["vendor_id"] == good for v in proj["vendors"])                              # winner added to the project
    # staff not on the team can't add bids elsewhere
    other = ids(mgr.post("/api/projects", json={"name": "Not Sam's project"}))["id"]
    assert staff.post(f"/api/projects/{other}/bids", json={"vendor_name": "X", "amount": 1}).status_code == 403


def test_blocked_requires_what_its_waiting_on(mgr, staff):
    from datetime import date, timedelta
    with tx() as c:
        sam = c.execute("SELECT id FROM users WHERE blink_user_id='u-sam'").fetchone()["id"]
    pid = ids(mgr.post("/api/projects", json={"name": "Blocker test", "property": "11 Block Rd, Austin, TX"}))["id"]
    prop = [p for p in ids(mgr.get("/api/projects")) if p["id"] == pid][0]["property_id"]
    owner = ids(mgr.post(f"/api/properties/{prop}/contacts", json={"role": "Owner contact", "name": "Olga Owner"}))["id"]
    first = ids(mgr.post("/api/tasks", json={"title": "Get owner sign-off", "project_id": pid, "assignee_id": sam}))["id"]
    t = ids(mgr.post("/api/tasks", json={"title": "Order materials", "project_id": pid, "assignee_id": sam}))["id"]
    # no reason → refused
    assert staff.patch(f"/api/tasks/{t}", json={"status": "blocked"}).status_code == 400
    opts = ids(staff.get(f"/api/tasks/{t}/block-options"))
    assert any(x["id"] == first for x in opts["tasks"]) and any(x["ref"] == f"contact:{owner}" for x in opts["people"])
    # waiting on a person, with a follow-up reminder
    follow = (date.today() + timedelta(days=3)).isoformat()
    ids(staff.patch(f"/api/tasks/{t}", json={"status": "blocked", "block": {"kind": "person", "ref": f"contact:{owner}",
                                                                            "reason": "roof invoice", "follow_up": follow}}))
    tt = ids_task(t)
    assert tt["status"] == "blocked" and tt["blocked_label"] == "Olga Owner (Owner contact)" and tt["blocked_follow_up"] == follow
    with tx() as c:
        rem = c.execute("SELECT * FROM reminders WHERE id=?", (tt["blocked_reminder_id"],)).fetchone()
    assert rem and rem["user_id"] == sam and rem["remind_at"].startswith(follow) and "Olga Owner" in rem["text"]
    # change it to "waiting on another task" → dependency; finishing that task unblocks automatically
    ids(staff.patch(f"/api/tasks/{t}", json={"block": {"kind": "task", "task_id": first}}))
    with tx() as c:
        assert c.execute("SELECT 1 FROM task_dependencies WHERE task_id=? AND depends_on_id=?", (t, first)).fetchone()
        assert not c.execute("SELECT 1 FROM reminders WHERE id=?", (rem["id"],)).fetchone()          # old follow-up dropped
    before = len(SENT)
    ids(staff.patch(f"/api/tasks/{first}", json={"status": "done"}))
    tt = ids_task(t)
    assert tt["status"] == "in_progress" and tt["blocked_kind"] is None
    assert any(b == "u-sam" and "back to In progress" in m for b, m in SENT[before:])
    # a loop is refused; "other" needs a reason
    assert staff.patch(f"/api/tasks/{first}", json={"status": "blocked", "block": {"kind": "task", "task_id": first}}).status_code == 400
    assert staff.patch(f"/api/tasks/{t}", json={"status": "blocked", "block": {"kind": "other"}}).status_code == 400
    ids(staff.patch(f"/api/tasks/{t}", json={"status": "blocked", "block": {"kind": "other", "reason": "City permit"}}))
    assert ids_task(t)["blocked_reason"] == "City permit"
    ids(staff.patch(f"/api/tasks/{t}", json={"status": "in_progress"}))
    assert ids_task(t)["blocked_kind"] is None
    # Blink: "blocked" with no reason → the bot asks; the answer becomes the reason
    CLAUDE.append({"updates": [{"task_id": t, "status": "blocked", "note": "", "actual_minutes": 0, "time_note": ""}], "comments": [], "reply": ""})
    r = services.handle_inbound("u-sam", f"{t} is blocked")
    assert "waiting on" in r
    CLAUDE.append({"updates": [{"task_id": t, "status": "unchanged", "note": "Waiting on the tenant to pick colors", "actual_minutes": 0,
                                "time_note": ""}], "comments": [], "reply": ""})
    services.handle_inbound("u-sam", "the tenant needs to pick colors")
    assert ids_task(t)["blocked_reason"] == "Waiting on the tenant to pick colors"


def test_wins_kudos_streaks_and_badges(mgr, staff):
    from datetime import date, timedelta
    from app import fun
    with tx() as c:
        sam = c.execute("SELECT id FROM users WHERE blink_user_id='u-sam'").fetchone()["id"]
    pj = ids(mgr.post("/api/projects", json={"name": "Fun project"}))["id"]
    t1 = ids(mgr.post("/api/tasks", json={"title": "Fun one", "project_id": pj, "assignee_id": sam, "due_date": date.today().isoformat()}))["id"]
    t2 = ids(mgr.post("/api/tasks", json={"title": "Fun two", "project_id": pj, "assignee_id": sam,
                                          "due_date": (date.today() + timedelta(days=3)).isoformat()}))["id"]
    me = ids(staff.get("/api/fun/me"))
    assert me["today"]["total"] >= 1 and len(me["badges"]) == 6
    ids(staff.patch(f"/api/tasks/{t1}", json={"status": "done"}))
    ids(staff.patch(f"/api/tasks/{t2}", json={"status": "done"}))       # finishes the project
    team = ids(mgr.get("/api/fun/team"))
    mine = [w for w in team["wins"] if w["user_id"] == sam]
    assert any(w["kind"] == "task" and w["title"] == "Fun one" for w in mine)
    assert any(w["kind"] == "project" and w["title"] == "Fun project" for w in mine)
    # re-ticking doesn't duplicate
    ids(staff.patch(f"/api/tasks/{t1}", json={"status": "todo"}))
    ids(staff.patch(f"/api/tasks/{t1}", json={"status": "done"}))
    assert sum(1 for w in ids(mgr.get("/api/fun/team"))["wins"] if w["kind"] == "task" and w["ref_id"] == t1) == 1
    # high-five → Blink to Sam; can't high-five yourself
    wid = next(w["id"] for w in mine if w["kind"] == "task")
    before = len(SENT)
    assert ids(mgr.post(f"/api/wins/{wid}/kudos"))["kudos"] == 1
    assert any(b == "u-sam" and "high-five" in t for b, t in SENT[before:])
    assert ids(mgr.post(f"/api/wins/{wid}/kudos"))["kudos"] == 1                   # once per person
    assert staff.post(f"/api/wins/{wid}/kudos").status_code == 400
    # early bird badge after 10 tasks finished before their due date
    with tx() as c:
        for i in range(10):
            c.execute("""INSERT INTO tasks(project_id, title, assignee_id, status, due_date, completed_at)
                         VALUES(?,?,?,'done',?,?)""", (pj, f"early {i}", sam, (date.today() + timedelta(days=5)).isoformat(),
                                                       datetime.now().isoformat(timespec="seconds")))
    before = len(SENT)
    assert "early_bird" in fun.check_badges(sam)
    assert any(b == "u-sam" and "Early Bird" in t for b, t in SENT[before:])
    assert "early_bird" not in fun.check_badges(sam)                                # once
    # streak: workdays with nothing overdue
    with tx() as c:
        c.execute("UPDATE tasks SET status='done' WHERE assignee_id=? AND status!='done'", (sam,))
        d = date.today()
        for i in range(1, 15):
            day = d - timedelta(days=i)
            c.execute("INSERT OR REPLACE INTO user_days(user_id, day, overdue) VALUES(?,?,0)", (sam, day.isoformat()))
    assert fun.streak(sam) >= 5
    with tx() as c:   # an overdue day breaks it
        c.execute("UPDATE user_days SET overdue=2 WHERE user_id=? AND day=?", (sam, (d - timedelta(days=3)).isoformat()))
    assert fun.streak(sam) < 5
    assert any(x["id"] == sam for x in ids(mgr.get("/api/fun/team"))["today"])


def test_our_own_coi_per_property(mgr, staff):
    import base64
    from datetime import date, timedelta
    pid = ids(mgr.post("/api/properties", json={"address": "12 Our Coi Ct, Austin, TX", "client": "Our Coi Owner LLC"}))["id"]
    ids(mgr.patch(f"/api/properties/{pid}", json={"our_coi_requirements": "CGL $1M/$2M; Crime $250k; Owner as additional insured"}))
    page = ids(mgr.get(f"/api/properties/{pid}"))
    assert page["our_coi"]["status"]["state"] == "missing" and "Crime" in page["our_coi_requirements"]
    assert [p for p in ids(mgr.get("/api/properties")) if p["id"] == pid][0]["our_coi"]["state"] == "missing"
    exp = (date.today() + timedelta(days=200)).isoformat()
    CLAUDE.append({"is_coi": True, "named_insured": "Foresite Real Estate, Inc.", "carrier": "Chubb", "expires": exp,
                   "owner_additional_insured": "yes", "meets_requirements": "no", "gaps": "Crime coverage not shown",
                   "evidence": "CGL 1M/2M; AI: Our Coi Owner LLC"})
    doc = {"filename": "foresite-coi.pdf", "content_type": "application/pdf", "data_b64": base64.b64encode(b"%PDF ours").decode()}
    out = ids(mgr.post(f"/api/properties/{pid}/our-coi", json=doc))
    assert out["status"]["state"] == "gaps" and "Crime" in out["status"]["gaps"]
    assert staff.post(f"/api/properties/{pid}/our-coi", json=doc).status_code == 403
    ids(mgr.patch(f"/api/our-cois/{out['id']}", json={"meets_reqs": True}))       # someone checked: crime is on a separate cert
    st = ids(mgr.get(f"/api/properties/{pid}"))["our_coi"]["status"]
    assert st["state"] == "ok" and st["expires"] == exp
    dates = [d for d in ids(mgr.get(f"/api/properties/{pid}"))["dates"] if d["kind"] == "our_coi"]
    assert len(dates) == 1 and dates[0]["date"] == exp and dates[0]["remind_days"] == "30,7"
    assert mgr.get(f"/api/our-cois/{out['id']}").content == b"%PDF ours"
    # request a certificate from our agent
    ids(mgr.put("/api/our-coi/agent", json={"name": "Kelly Agent", "email": "kelly@ouragency.test", "company": "Our Agency"}))
    d = ids(mgr.post(f"/api/properties/{pid}/our-coi/request"))
    assert d["to_email"] == "kelly@ouragency.test" and "Our Coi Owner LLC" in d["body"] and "Crime $250k" in d["body"]
    # reading a management agreement picks up what we have to carry
    CLAUDE.append({"doc_type": "mgmt_agreement", "summary": "PMA", "dates": [], "spend_limit": 0, "report_notes": "", "bank_name": "",
                   "manager_insurance": "CGL $2M per occurrence; E&O $1M; Owner and lender as additional insured",
                   "contacts": [], "insurance": {"carrier": "", "policy_number": "", "expires": "", "foresite_additional_insured": "not_applicable", "evidence": ""}})
    r = ids(mgr.post(f"/api/properties/{pid}/read-document", json={"filename": "pma.pdf", "content_type": "application/pdf",
                                                                    "data_b64": base64.b64encode(b"%PDF pma").decode()}))
    assert "E&O $1M" in r["manager_insurance"]


def test_help_assistant(mgr, staff):
    from app import helpbot
    CLAUDE.append("Go to Projects ▾ → Projects and click + New project.")
    r = ids(staff.post("/api/help", json={"messages": [{"role": "user", "content": "How do I start a project?"}], "page": "home"}))
    assert "New project" in r["reply"]
    sent = CLAUDE_CALLS[-1]
    assert "tools" in sent and "Foresite" in sent["system"] and "role: staff" in sent["system"]
    # the read-only lookups respect permissions
    with tx() as c:
        sam = dict(c.execute("SELECT id, name, role FROM users WHERE blink_user_id='u-sam'").fetchone())
        boss = dict(c.execute("SELECT id, name, role FROM users WHERE email='boss@foresitecre.com'").fetchone())
    pid = ids(mgr.post("/api/properties", json={"address": "13 Help Ln, Austin, TX"}))["id"]
    ids(mgr.patch(f"/api/properties/{pid}", json={"bank_name": "Help Bank", "bank_accounts": "Operating 123456789"}))
    as_staff = helpbot.run_tool("property_info", {"name": "13 Help"}, sam)
    as_mgr = helpbot.run_tool("property_info", {"name": "13 Help"}, boss)
    assert as_staff["bank"] == "Help Bank" and "bank_accounts" not in as_staff and as_mgr["bank_accounts"] == "Operating ••6789"
    assert as_staff["link"] == f"#property-{pid}"
    t = ids(mgr.post("/api/tasks", json={"title": "Help lookup task", "assignee_id": sam["id"], "due_date": "2020-01-01"}))["id"]
    found = helpbot.run_tool("find_tasks", {"person": "me", "overdue_only": True}, sam)
    assert any(x["id"] == t and x["link"] == f"#task-{t}" for x in found["tasks"])
    assert helpbot.run_tool("my_day", {}, sam)["overdue"]
    assert "vendors" in helpbot.run_tool("find_vendors", {"trade": "landscaper"}, sam)
    assert helpbot.run_tool("project_info", {"name": "zzz no such"}, sam)["error"]


def test_lj_answers_questions_in_blink():
    from app import services as sv
    assert sv.helpbot_wanted("How do I start a project?") and sv.helpbot_wanted("LJ, where are vendors?")
    assert sv.helpbot_wanted("@LJ vendors for roofing") and sv.helpbot_wanted("hey lj show my day")
    assert sv.helpbot_wanted("what's overdue for me") and sv.helpbot_wanted("who is the PM at 777?")
    assert not sv.helpbot_wanted("done 12") and not sv.helpbot_wanted("finished the rent roll")
    assert not sv.helpbot_wanted("12 is blocked waiting on the owner") and not sv.helpbot_wanted("list")
    assert "http://testserver/#task-12" in sv.blink_links("See [Open task #12](#task-12).")
    CLAUDE.append("Go to Projects ▾ → [Projects](#view-projects) and click + New project.")
    r = services.handle_inbound("u-sam", "How do I start a project?")
    assert r.startswith("🙋 **LJ:**") and "http://testserver/#view-projects" in r
    assert any(b == "u-sam" and "LJ" in t for b, t in SENT[-2:])
    assert "role: staff" in CLAUDE_CALLS[-1]["system"] and CLAUDE_CALLS[-1]["tools"]
    # updates still go to the task parser
    assert "Comment added" in services.handle_inbound("u-sam", "comment 5 thanks")


def test_task_on_a_property_without_a_project(mgr):
    prop = ids(mgr.post("/api/properties", json={"address": "55 Loose Task Ln, San Antonio, TX"}))["id"]
    tid = ids(mgr.post("/api/tasks", json={"title": "Call the city about the sign permit", "property_id": prop}))["id"]
    page = ids(mgr.get(f"/api/properties/{prop}"))
    t = [x for x in page["tasks"] if x["id"] == tid][0]
    assert t["project_name"] == "No project"
    allp = [p for p in ids(mgr.get("/api/projects")) if p["is_general"]][0]
    assert [x for x in allp["tasks"] if x["id"] == tid][0]["property_id"] == prop
    # a project's property wins over the one sent
    proj = ids(mgr.post("/api/projects", json={"name": "Loose proj", "property": "55 Loose Task Ln, San Antonio, TX"}))["id"]
    t2 = ids(mgr.post("/api/tasks", json={"title": "In project", "project_id": proj}))["id"]
    pp = [p for p in ids(mgr.get("/api/projects")) if p["id"] == proj][0]
    assert [x for x in pp["tasks"] if x["id"] == t2][0]["property_id"] == pp["property_id"] == prop
    ids(mgr.patch(f"/api/tasks/{tid}", json={"property_id": 0}))
    assert tid not in [x["id"] for x in ids(mgr.get(f"/api/properties/{prop}"))["tasks"]]
    assert mgr.post("/api/tasks", json={"title": "x", "property_id": 999999}).status_code == 400


def test_add_task_from_blink(mgr, staff):
    import re
    from app.db import tx
    with tx() as c:
        c.execute("UPDATE users SET blink_user_id='u-boss' WHERE id=?", (mgr.me["id"],))
    prop = ids(mgr.post("/api/properties", json={"address": "66 Blink Add Way, Austin, TX"}))
    proj = ids(mgr.post("/api/projects", json={"name": "Blink add proj", "property": "66 Blink Add Way, Austin, TX"}))["id"]
    SENT.clear()
    CLAUDE.append({"title": "Call the city about the sign permit", "property_id": prop["id"], "project_id": 0,
                   "assignee_id": staff.me["id"], "due_date": "2031-01-10", "estimate": "30m"})
    r = services.handle_inbound("u-boss", "add task call the city about the sign permit at 66 Blink Add for Sam by Jan 10")
    tid = int(re.search(r"#(\d+)", r)[1])
    assert "🏢 66 Blink Add Way" in r and "Sam Patel" in r and "1 open project" in r
    t = ids_task(tid)
    assert t["assignee_id"] == staff.me["id"] and t["property_id"] == prop["id"] and t["due_date"] == "2031-01-10"
    assert any(b == "u-sam" and f"#{tid}" in m for b, m in SENT)                       # assignee told in Blink
    # no property or project → still fine, and the reply says so
    CLAUDE.append({"title": "Order toner", "property_id": 0, "project_id": 0, "assignee_id": 0, "due_date": "", "estimate": ""})
    r = services.handle_inbound("u-sam", "add task order toner")
    assert "Added #" in r and "optional" in r and "👤 you" in r
    # staff can't put tasks in projects or assign others
    CLAUDE.append({"title": "Fix gate", "property_id": prop["id"], "project_id": proj, "assignee_id": mgr.me["id"],
                   "due_date": "", "estimate": ""})
    r = services.handle_inbound("u-sam", "task: fix gate on Blink add proj for Bethany")
    tid = int(re.search(r"#(\d+)", r)[1])
    t = ids_task(tid)
    assert t["assignee_id"] == staff.me["id"] and t["property_id"] == prop["id"] and "Only managers" in r
    assert "property & project are optional" in services.handle_inbound("u-sam", "help")


def test_property_photo(mgr, staff):
    import base64
    prop = ids(mgr.post("/api/properties", json={"address": "77 Photo Ave, San Antonio, TX"}))["id"]
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()
    assert staff.post(f"/api/properties/{prop}/photo", json={"data_b64": png}).status_code == 403
    assert mgr.post(f"/api/properties/{prop}/photo", json={"data_b64": base64.b64encode(b"nope").decode()}).status_code == 400
    name = ids(mgr.post(f"/api/properties/{prop}/photo", json={"data_b64": "data:image/png;base64," + png}))["photo_file"]
    assert ids(mgr.get(f"/api/properties/{prop}"))["photo_file"] == name
    assert mgr.get(f"/api/properties/{prop}/photo").status_code == 200
    ids(mgr.delete(f"/api/properties/{prop}/photo"))
    assert ids(mgr.get(f"/api/properties/{prop}"))["photo_file"] is None
    assert mgr.get(f"/api/properties/{prop}/photo").status_code == 404


def test_home_layout_and_notes(mgr, staff):
    h = ids(staff.get("/api/me/home"))
    assert h["layout"] is None and h["notes"] == [] and isinstance(h["my_properties"], list)
    ids(staff.put("/api/me/home/layout", json={"layout": {"top": ["kpis"], "left": ["notes", "agenda", "agenda", "bad id!"],
                                                         "right": ["wins"], "hidden": ["today"]}}))
    L = ids(staff.get("/api/me/home"))["layout"]
    assert L == {"top": ["kpis"], "left": ["notes", "agenda"], "right": ["wins"], "hidden": ["today"]}
    assert ids(mgr.get("/api/me/home"))["layout"] != L                       # per person
    nid = ids(staff.post("/api/me/notes", json={"text": "call the roofer", "color": "pink"}))["id"]
    ids(staff.patch(f"/api/me/notes/{nid}", json={"text": "call the roofer back", "color": "nope"}))
    n = ids(staff.get("/api/me/home"))["notes"][0]
    assert n["text"] == "call the roofer back" and n["color"] == "pink"
    assert mgr.patch(f"/api/me/notes/{nid}", json={"text": "x"}).status_code == 404   # can't touch someone else's
    assert mgr.delete(f"/api/me/notes/{nid}").status_code == 404
    assert ids(mgr.get("/api/me/home"))["notes"] == [] or all(x["id"] != nid for x in ids(mgr.get("/api/me/home"))["notes"])
    ids(staff.delete(f"/api/me/notes/{nid}"))
    ids(staff.put("/api/me/home/layout", json={"layout": None}))
    assert ids(staff.get("/api/me/home"))["layout"] is None


def test_blink_notification_settings(mgr, staff):
    import json as _json
    from datetime import datetime
    from app import notify
    from app.db import tx
    real = {"frequency": "daily", "time": "18:00", "weekdays_only": False, "cats": {k: v[2] for k, v in notify.CATEGORIES.items()}}
    saved = _json.loads(_json.dumps(notify.DEFAULT))
    notify.DEFAULT.clear(); notify.DEFAULT.update(_json.loads(_json.dumps(real)))
    try:
        with tx() as c:
            c.execute("DELETE FROM user_prefs WHERE user_id=?", (staff.me["id"],))
            c.execute("DELETE FROM notify_queue WHERE user_id=?", (staff.me["id"],))
            c.execute("UPDATE users SET blink_user_id='u-sam', daily_digest=1, week_ahead=1 WHERE id=?", (staff.me["id"],))
        s = ids(staff.get("/api/me/notifications"))
        modes = {c["id"]: c["mode"] for c in s["categories"]}
        assert s["frequency"] == "daily" and s["time"] == "18:00"                      # default: daily summary at 6pm…
        assert modes["mentions"] == "instant" and modes["assigned"] == "summary"        # …plus @mentions right away
        assert "manager" not in modes                                                   # staff don't see team alerts
        assert "manager" in {c["id"] for c in ids(mgr.get("/api/me/notifications"))["categories"]}
        SENT.clear()
        assert notify.deliver(staff.me["id"], "mentions", "💬 tagged") == "instant" and SENT[-1] == ("u-sam", "💬 tagged")
        assert notify.deliver(staff.me["id"], "assigned", "📌 new task") == "summary" and len(SENT) == 1
        assert notify.deliver(staff.me["id"], "checkin", "⏰ check-in") == "off"
        assert ids(staff.get("/api/me/notifications"))["queued"] == 1
        # nothing before 6pm; at 6pm one summary with the saved update, then the queue is empty
        assert notify.run_summaries(datetime(2031, 3, 4, 17, 59), force_user=None) == 0 or all(b != "u-sam" for b, _ in SENT[1:])
        SENT.clear()
        notify.run_summaries(datetime(2031, 3, 4, 18, 1))
        mine = [t for b, t in SENT if b == "u-sam"]
        assert len(mine) == 1 and "📌 new task" in mine[0] and "Since your last summary" in mine[0]
        assert ids(staff.get("/api/me/notifications"))["queued"] == 0
        SENT.clear(); notify.run_summaries(datetime(2031, 3, 4, 19, 0))
        assert not [t for b, t in SENT if b == "u-sam"]                                 # once a day
        # change: twice a day at 5pm, weekdays only, assignments instant, wins off
        s = ids(staff.put("/api/me/notifications", json={"frequency": "twice", "time": "17:00", "weekdays_only": True,
                                                         "cats": {"assigned": "instant", "wins": "off", "recap": "instant", "bogus": "on"}}))
        modes = {c["id"]: c["mode"] for c in s["categories"]}
        assert s["frequency"] == "twice" and s["time"] == "17:00" and s["weekdays_only"]
        assert modes["assigned"] == "instant" and modes["wins"] == "off" and modes["recap"] == "summary"
        SENT.clear(); notify.deliver(staff.me["id"], "assigned", "📌 now"); assert SENT[-1] == ("u-sam", "📌 now")
        notify.deliver(staff.me["id"], "comments", "💬 later")
        SENT.clear(); notify.run_summaries(datetime(2031, 3, 8, 9, 0))                  # Saturday: weekdays only
        assert not [t for b, t in SENT if b == "u-sam"]
        notify.run_summaries(datetime(2031, 3, 10, 8, 5))                               # Monday 8am slot
        assert any("💬 later" in t for b, t in SENT if b == "u-sam")
        assert staff.put("/api/me/notifications", json={"time": "25:99"}).status_code == 200
        assert ids(staff.get("/api/me/notifications"))["time"] == "17:00"
        r = ids(staff.post("/api/me/notifications/reset"))
        assert r["frequency"] == "daily" and r["time"] == "18:00"
        assert "text" in ids(staff.post("/api/me/notifications/preview"))
    finally:
        notify.DEFAULT.clear(); notify.DEFAULT.update(saved)
        with tx() as c:
            c.execute("DELETE FROM user_prefs WHERE user_id=? AND key LIKE 'notify%'", (staff.me["id"],))


def test_onboarding_document_requests(mgr):
    from app import starter_templates as st, tpl
    from app.db import tx, set_setting
    st.install(["onboarding"])
    name = next(x for x in st.STARTERS if x["key"] == "onboarding")["name"]
    t = next(x for x in ids(mgr.get("/api/templates")) if x["name"] == name)
    doc = next(i for i in t["items"] if i["title"] == st.DOC_TASK)
    kids = [i for i in t["items"] if i.get("parent_item_id") == doc["id"]]
    assert len(kids) == len(st._doc_subtasks()) == 60
    assert any(k["title"] == "Tenant · Current rent roll" and k["due_offset_days"] == 5 for k in kids)
    assert any(k["title"].startswith("Accounting · Balance sheet") and k["due_offset_days"] == 55 for k in kids)
    # simulate a template installed before this change, then run the one-time update
    tpl.delete_item(doc["id"])
    agreement = next(i for i in t["items"] if i["title"] == "Fully executed management agreement on file")
    old = tpl.add_item(t["id"], "Collect property documents from owner / prior manager", due_offset_days=3)
    acct = next(i for i in t["items"] if i["title"] == "Set up property in AppFolio")
    tpl.add_dependency(acct["id"], old)
    with tx() as c:
        c.execute("UPDATE template_items SET position=? WHERE id=?", (agreement["position"] + 0, old))
    set_setting("tpl.onboarding_docs_v1", "")
    assert st.migrate_onboarding_docs() is True
    t = next(x for x in ids(mgr.get("/api/templates")) if x["name"] == name)
    titles = [i["title"] for i in t["items"] if not i.get("parent_item_id")]
    assert "Collect property documents from owner / prior manager" not in titles and st.DOC_TASK in titles
    doc = next(i for i in t["items"] if i["title"] == st.DOC_TASK)
    assert len([i for i in t["items"] if i.get("parent_item_id") == doc["id"]]) == 60
    acct = next(i for i in t["items"] if i["title"] == "Set up property in AppFolio")
    assert agreement["id"] in acct["waiting_on"]
    assert st.migrate_onboarding_docs() is False                                   # only once


def test_templates_v2_from_foresite_lists(mgr):
    from app import starter_templates as st, tpl
    from app.db import tx, set_setting
    keys = [s["key"] for s in st.STARTERS]
    assert "renewal" not in keys and {"cash_forecast", "acct_training"} <= set(keys)
    st.install(["month_end", "onboarding", "acct_training", "cash_forecast"])
    by = {t["name"]: t for t in ids(mgr.get("/api/templates"))}
    me = [i["title"] for i in by["Month-End Close"]["items"]]
    assert "Monthly financial review" in me and "Approve recurring bills (around the 22nd)" in me and "Compile owner report packet" in me
    ob = by["Property Onboarding (Management Takeover)"]["items"]
    setup = next(i for i in ob if i["title"] == "Set up property in AppFolio")
    assert any(i["title"] == "Add the owner" and i["parent_item_id"] == setup["id"] for i in ob)
    assert any(i["title"].startswith("Legal · ") for i in ob)
    tr = by["Accounting New Hire Training"]["items"]
    assert any(i["title"] == "A/P · Processing 1099s / 1096s" for i in tr)
    # an older install gets rebuilt; a Lease Renewal template is removed (its projects keep their tasks)
    with tx() as c:
        rid = c.execute("INSERT INTO templates(name, description) VALUES('Lease Renewal','old')").lastrowid
        mid = by["Month-End Close"]["id"]
        c.execute("DELETE FROM template_items WHERE template_id=?", (mid,))
    tpl.add_item(rid, "Renewal outreach")
    tpl.add_item(mid, "Old month-end step")
    pid = ids(mgr.post("/api/projects", json={"name": "Old renewal project", "template_id": rid}))["id"]
    set_setting("tpl.v2", "")
    done = st.upgrade_templates_v2()
    assert "Month-End Close" in done and "removed Lease Renewal" in done
    by = {t["name"]: t for t in ids(mgr.get("/api/templates"))}
    assert "Lease Renewal" not in by
    assert [i["title"] for i in by["Month-End Close"]["items"]][0] == "Post tenant receipts & bank deposits"
    assert any(p["id"] == pid for p in ids(mgr.get("/api/projects")))
    assert st.upgrade_templates_v2() == []
    assert "New Assignment Check List" in by["Property Onboarding (Management Takeover)"]["description"]


def test_report_package_import(mgr, staff):
    import base64, io, openpyxl
    p1 = ids(mgr.post("/api/properties", json={"address": "1546 Babcock, San Antonio, TX 78229", "name": "1546 Babcock"}))["id"]
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Report Packages"
    ws.append(["Property", "Monthly", "Quarterly", "Submitted Via", "Cover Sheet", "Executive Summary", "Inspection Report", "Site Plan",
               "Insurance Report", "AF Report", "Delinquency Report", "Bank Rec/ Stmt", "Distribution Schedule", "Loan Stmt", "Misc Schedules"])
    ws.append(["1546 Babcock - 1546 Babcock San Antonio, TX 78229", "X", None, "Email", None, "X", None, None, None, "X", "X", "X"])
    ws.append(["Lott's Landing (8336) - AT&T - 8336 FM 78 Converse, TX 78109", "X", None, "Email", None, "X", None, "X", None, "X",
               None, "X", None, None, None, "See report notes"])
    ws.append(["Blank Plaza - 100 Nowhere Rd San Antonio, TX 78201"])
    buf = io.BytesIO(); wb.save(buf); b64 = base64.b64encode(buf.getvalue()).decode()
    assert staff.post("/api/properties/report-packages/import", json={"data_b64": b64}).status_code == 403
    pre = ids(mgr.post("/api/properties/report-packages/import", json={"data_b64": b64}))
    assert pre["matched"] == 1 and pre["new"] == 2 and pre["with_package"] == 2 and not pre["applied"]
    lott = next(r for r in pre["rows"] if r["name"].startswith("Lott"))
    assert lott["name"] == "Lott's Landing (8336) - AT&T" and lott["address"].startswith("8336 FM 78")
    assert ids(mgr.get(f"/api/properties/{p1}")).get("report_items") in (None, "", "[]")       # preview changes nothing
    r = ids(mgr.post("/api/properties/report-packages/import", json={"data_b64": b64, "apply": True, "create_missing": True}))
    assert sum(x["status"] == "created" for x in r["rows"]) == 2
    p = ids(mgr.get(f"/api/properties/{p1}"))
    import json as _j
    assert p["report_freq"] == "monthly" and p["report_via"] == "Email"
    assert _j.loads(p["report_items"]) == ["Executive summary", "AppFolio report", "Delinquency report", "Bank rec & statement"]
    lp = next(x for x in ids(mgr.get("/api/properties")) if x["name"].startswith("Lott"))
    assert "See report notes" in (ids(mgr.get(f"/api/properties/{lp['id']}"))["report_notes"] or "")
    ids(mgr.put(f"/api/properties/{p1}/report-package", json={"freq": "quarterly", "items": ["Cover sheet", "Bogus"]}))
    p = ids(mgr.get(f"/api/properties/{p1}"))
    assert p["report_freq"] == "quarterly" and _j.loads(p["report_items"]) == ["Cover sheet"]
    proj = ids(mgr.post("/api/projects", json={"name": "Close", "property": "1546 Babcock, San Antonio, TX 78229"}))["id"]
    assert ids(mgr.get(f"/api/projects/{proj}/report-package"))["items"] == ["Cover sheet"]
    assert mgr.post("/api/properties/report-packages/import", json={"data_b64": "bm90IGV4Y2Vs"}).status_code == 400


def test_reporting_status_for_property_managers(mgr, staff):
    from datetime import date
    from app import starter_templates as st, properties as P
    from app.db import tx
    st.install(["month_end"])
    tid = next(t for t in ids(mgr.get("/api/templates")) if t["name"] == "Month-End Close")["id"]
    ids(mgr.patch(f"/api/templates/{tid}", json={"board_period": "month"}))
    addr = "88 Report Way, San Antonio, TX"
    proj = ids(mgr.post("/api/projects", json={"name": "Close – Report Way", "template_id": tid, "property": addr,
                                               "start_date": date.today().replace(day=1).isoformat()}))["id"]
    pid = P.property_for_address(addr)
    with tx() as c:
        role = c.execute("SELECT id FROM project_roles WHERE name='Property Manager'").fetchone()[0]
        c.execute("UPDATE users SET blink_user_id='u-sam' WHERE id=?", (staff.me["id"],))
    P.set_member(pid, role, staff.me["id"])
    r = ids(staff.get("/api/me/reporting"))
    row = next(x for x in r["rows"] if x["project_id"] == proj)
    assert row["total"] > 5 and not row["sent"] and r["scope"] == "mine"
    # the PM doesn't own the accounting steps…
    tasks = [t for t in next(p for p in ids(mgr.get("/api/projects")) if p["id"] == proj)["tasks"]]
    send = next(t for t in tasks if t["title"] == "Review & send owner report")
    assert send["assignee_id"] != staff.me["id"]
    # …but hears when the report goes out
    SENT.clear()
    ids(mgr.patch(f"/api/tasks/{send['id']}", json={"status": "done"}))
    assert any(b == "u-sam" and "Owner report sent" in t for b, t in SENT)
    assert next(x for x in ids(staff.get("/api/me/reporting"))["rows"] if x["project_id"] == proj)["sent"]
    assert "reporting" in {c["id"] for c in ids(staff.get("/api/me/notifications"))["categories"]}


def test_deleted_property_stays_deleted(mgr):
    addr = "321 Gone Away Ln, San Antonio, TX"
    proj = ids(mgr.post("/api/projects", json={"name": "Gone project", "property": addr}))["id"]
    pid = next(p["id"] for p in ids(mgr.get("/api/properties")) if p["address"] == addr)
    name = ids(mgr.get(f"/api/properties/{pid}"))["name"]
    r = ids(mgr.delete(f"/api/properties/{pid}?confirm={name}"))
    for _ in range(2):   # listing used to re-create it from the project's address
        assert addr not in [p["address"] for p in ids(mgr.get("/api/properties"))]
    assert any(p["id"] == proj for p in ids(mgr.get("/api/projects")))        # the project is kept
    ids(mgr.post(f"/api/trash/{r['trash_id']}/restore"))                        # undo brings it back, linked
    back = next(p for p in ids(mgr.get("/api/properties")) if p["address"] == addr)
    assert any(x["id"] == proj for x in ids(mgr.get(f"/api/properties/{back['id']}"))["projects"])
    ids(mgr.delete(f"/api/properties/{back['id']}?confirm={back['name']}"))
    again = ids(mgr.post("/api/properties", json={"address": addr}))["id"]     # adding it by hand reconnects the project
    assert any(x["id"] == proj for x in ids(mgr.get(f"/api/properties/{again}"))["projects"])


def test_archive_projects_and_properties(mgr, staff):
    from app import properties as P
    addr = "77 Archive Row, San Antonio, TX"
    p1 = ids(mgr.post("/api/projects", json={"name": "Arch A", "property": addr}))["id"]
    p2 = ids(mgr.post("/api/projects", json={"name": "Arch B", "property": addr}))["id"]
    t = ids(mgr.post("/api/tasks", json={"title": "Arch task", "project_id": p1, "assignee_id": mgr.me["id"], "due_date": "2000-01-01"}))["id"]
    prop = P.property_for_address(addr)
    assert staff.post(f"/api/projects/{p1}/archive").status_code == 403
    ids(mgr.post(f"/api/projects/{p1}/archive"))
    st = {p["id"]: p["status"] for p in ids(mgr.get("/api/projects"))}
    assert st[p1] == "archived"
    assert t not in [x["id"] for x in ids(mgr.get("/api/dashboard"))["agenda"]]          # off Home
    ids(mgr.post(f"/api/projects/{p1}/archive?undo=true"))
    assert {p["id"]: p["status"] for p in ids(mgr.get("/api/projects"))}[p1] == "active"
    # archiving the property archives its projects; unarchiving brings back exactly those
    r = ids(mgr.post(f"/api/properties/{prop}/archive"))
    assert r["projects"] == 2
    assert prop not in [x["id"] for x in ids(mgr.get("/api/properties"))]
    assert prop in [x["id"] for x in ids(mgr.get("/api/properties?archived=1"))]
    assert all(p["status"] == "archived" for p in ids(mgr.get("/api/projects")) if p["id"] in (p1, p2))
    r = ids(mgr.post(f"/api/properties/{prop}/archive?undo=true"))
    assert r["projects"] == 2 and prop in [x["id"] for x in ids(mgr.get("/api/properties"))]
    assert {p["id"]: p["status"] for p in ids(mgr.get("/api/projects"))}[p1] == "active"
