"""Out-of-office coverage, quiet hours, and phone inspections."""
import base64
from datetime import date, datetime, timedelta

from app import inspections, notify, ooo
from app.db import tx
from conftest import SENT

T = date.today()
S = {}


def _user(name, blink):
    with tx() as c:
        r = c.execute("SELECT id FROM users WHERE name=?", (name,)).fetchone()
        if r:
            return r[0]
        return c.execute("INSERT INTO users(name, email, role, blink_user_id) VALUES(?,?,?,?)",
                         (name, name.split()[0].lower() + "@foresitecre.com", "staff", blink)).lastrowid


def _task(title, uid, due, prop=None):
    from app import team
    with tx() as c:
        return c.execute("INSERT INTO tasks(project_id, title, assignee_id, due_date, property_id) VALUES(?,?,?,?,?)",
                         (team.general_project_id(), title, uid, due.isoformat() if due else None, prop)).lastrowid


def _assignee(tid):
    with tx() as c:
        return c.execute("SELECT assignee_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def test_out_of_office_hands_over_and_back(mgr):
    lisa, jackie, donna = _user("Lisa Away", "b-lisa"), _user("Jackie Cover", "b-jackie"), _user("Donna Backup", "b-donna")
    for u in (lisa, jackie, donna):   # instant messages, no quiet hours
        notify.save(u, {"frequency": "instant", "quiet": {"on": False}})
    pid = mgr.post("/api/properties", json={"address": "9 Vacation Way, San Antonio, TX", "name": "Vacation Plaza"}).json()
    pid = pid if isinstance(pid, int) else pid["id"]
    with tx() as c:
        roles = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM project_roles")}
        c.execute("INSERT INTO property_team(property_id, role_id, user_id) VALUES(?,?,?)", (pid, roles["Property Manager"], lisa))
        c.execute("INSERT INTO property_team(property_id, role_id, user_id) VALUES(?,?,?)", (pid, roles["Backup Property Manager"], donna))
    start, end = T + timedelta(days=3), T + timedelta(days=10)
    t_in = _task("Due while out", lisa, T + timedelta(days=5))
    t_prop = _task("Property thing", lisa, T + timedelta(days=6), pid)
    t_late = _task("Due long after", lisa, T + timedelta(days=40))
    t_none = _task("No date", lisa, None)
    S.update(lisa=lisa, jackie=jackie, donna=donna)

    r = mgr.post("/api/me/ooo/preview", json={"user_id": lisa, "end": end.isoformat(), "backup_id": jackie}).json()
    assert r["tasks"] == 2 and {p["name"]: p["tasks"] for p in r["by_person"]} == {"Jackie Cover": 1, "Donna Backup": 1}
    assert mgr.post("/api/me/ooo", json={"user_id": lisa, "start": end.isoformat(), "end": start.isoformat(), "backup_id": jackie}).status_code == 400
    o = mgr.post("/api/me/ooo", json={"user_id": lisa, "start": start.isoformat(), "end": end.isoformat(), "backup_id": jackie,
                                      "note": "Urgent → call Nancy"}).json()
    assert o["status"] == "scheduled" and _assignee(t_in) == lisa
    assert mgr.post("/api/me/ooo", json={"user_id": lisa, "start": start.isoformat(), "end": end.isoformat(), "backup_id": jackie}).status_code == 400

    SENT.clear()
    ooo.run(start)
    assert ooo.get(o["id"])["status"] == "active" and ooo.is_out(lisa)
    assert (_assignee(t_in), _assignee(t_prop), _assignee(t_late), _assignee(t_none)) == (jackie, donna, lisa, lisa)
    jmsg = [t for b, t in SENT if b == "b-jackie"]
    assert jmsg and "covering for Lisa Away" in jmsg[0] and "Due while out" in jmsg[0] and "Urgent → call Nancy" in jmsg[0]
    assert any(b == "b-donna" and "Property thing" in t for b, t in SENT)

    # while she's out: nothing reaches her; new work moves over
    SENT.clear()
    assert notify.deliver(lisa, "mentions", "@Lisa can you look?") == "held"
    assert not [1 for b, _ in SENT if b == "b-lisa"]
    t_new = _task("New while out", lisa, T + timedelta(days=8))
    ooo.run(start + timedelta(days=1))
    assert _assignee(t_new) == jackie

    with tx() as c:
        c.execute("UPDATE tasks SET status='done' WHERE id=?", (t_in,))
    SENT.clear()
    ooo.run(end + timedelta(days=1))
    assert ooo.get(o["id"])["status"] == "ended" and not ooo.is_out(lisa)
    assert (_assignee(t_prop), _assignee(t_new), _assignee(t_in)) == (lisa, lisa, jackie)   # finished work stays done
    lmsg = [t for b, t in SENT if b == "b-lisa"]
    assert any("Welcome back" in t and "1 task got finished" in t and "Property thing" in t for t in lmsg)
    assert any("held while you were out" in t and "can you look" in t for t in lmsg)
    assert any(b == "b-jackie" and "Thanks for covering" in t for b, t in SENT)


def test_out_of_office_permissions_and_cancel(mgr, staff):
    lisa, jackie = S["lisa"], S["jackie"]
    assert staff.post("/api/me/ooo", json={"user_id": lisa, "start": T.isoformat(), "end": T.isoformat(), "backup_id": jackie}).status_code == 403
    o = staff.post("/api/me/ooo", json={"start": (T + timedelta(days=30)).isoformat(), "end": (T + timedelta(days=31)).isoformat(),
                                        "backup_id": jackie}).json()
    assert o["user_id"] == staff.me["id"]
    assert any(x["id"] == o["id"] for x in staff.get("/api/ooo").json()["items"])
    assert staff.delete(f"/api/me/ooo/{o['id']}").json()["status"] == "cancelled"


def test_quiet_hours(mgr):
    u = _user("Quiet Person", "b-quiet")
    notify.save(u, {"frequency": "instant", "quiet": {"on": True, "start": "19:00", "end": "07:30", "mentions": False}})
    p = notify.prefs(u)
    assert notify.in_quiet(p, datetime(2026, 11, 18, 22, 0)) and notify.in_quiet(p, datetime(2026, 11, 18, 6, 0))
    assert not notify.in_quiet(p, datetime(2026, 11, 18, 12, 0))
    notify.save(u, {"quiet": {"on": False, "weekends": True}})
    assert notify.in_quiet(notify.prefs(u), datetime(2026, 11, 21, 12, 0)) and not notify.in_quiet(notify.prefs(u), datetime(2026, 11, 20, 12, 0))
    # held now (the test clock is "now"): force a window that covers the current time
    now = datetime.now()
    a = (now - timedelta(hours=1)).strftime("%H:%M")
    b = (now + timedelta(hours=1)).strftime("%H:%M")
    notify.save(u, {"quiet": {"on": True, "weekends": False, "start": a, "end": b, "mentions": True}})
    SENT.clear()
    assert notify.deliver(u, "assigned", "New task for you") == "held"
    assert notify.deliver(u, "mentions", "@Quiet ping") == "instant"          # mentions let through
    assert [t for x, t in SENT if x == "b-quiet"] == ["@Quiet ping"]
    notify.run_summaries(now)                                                   # still quiet: nothing
    assert len([1 for x, _ in SENT if x == "b-quiet"]) == 1
    notify.run_summaries(now + timedelta(hours=2))                              # quiet over → one catch-up message
    msgs = [t for x, t in SENT if x == "b-quiet"]
    assert len(msgs) == 2 and "While you were offline" in msgs[1] and "New task for you" in msgs[1]
    r = mgr.put("/api/me/notifications", json={"quiet": {"on": True, "start": "20:00", "end": "07:00"}}).json()
    assert r["quiet"]["on"] and r["quiet"]["start"] == "20:00"
    mgr.put("/api/me/notifications", json={"quiet": {"on": False}})


def test_inspection_walkthrough(mgr, staff):
    pid = mgr.post("/api/properties", json={"address": "5 Inspect Ln, San Antonio, TX", "name": "Inspect Center"}).json()
    pid = pid if isinstance(pid, int) else pid["id"]
    assert staff.post(f"/api/properties/{pid}/inspections").status_code == 403    # not on the team
    i = mgr.post(f"/api/properties/{pid}/inspections").json()
    assert i["status"] == "draft" and len(i["items"]) > 25 and i["counts"]["unchecked"] == len(i["items"])
    assert mgr.post(f"/api/properties/{pid}/inspections").json()["id"] == i["id"]   # resumes the draft
    lot, roof = i["items"][0], next(x for x in i["items"] if x["label"].startswith("Roof"))
    assert mgr.patch(f"/api/inspection-items/{lot['id']}", json={"rating": "issue", "note": "Pothole by Suite 104", "urgent": True}).status_code == 200
    mgr.patch(f"/api/inspection-items/{roof['id']}", json={"rating": "good"})
    i = mgr.post(f"/api/inspections/{i['id']}/items", json={"section": "Site & parking", "label": "Bollard by drive-thru"}).json()
    extra = next(x for x in i["items"] if x["label"] == "Bollard by drive-thru")
    mgr.patch(f"/api/inspection-items/{extra['id']}", json={"rating": "issue", "make_task": False, "note": "Monitor"})
    jpg = base64.b64encode(b"\xff\xd8\xff\xe0fakejpeg").decode()
    ph = mgr.post(f"/api/inspections/{i['id']}/photos", json={"item_id": lot["id"], "data_b64": "data:image/jpeg;base64," + jpg}).json()
    mgr.post(f"/api/inspections/{i['id']}/photos", json={"data_b64": jpg, "caption": "Front elevation"})
    assert mgr.get(f"/api/inspection-photos/{ph['id']}").content == b"\xff\xd8\xff\xe0fakejpeg"
    r = mgr.post(f"/api/inspections/{i['id']}/finish", json={"notes": "Overall good shape."}).json()
    assert len(r["tasks"]) == 1
    with tx() as c:
        t = c.execute("SELECT title, due_date, property_id, description FROM tasks WHERE id=?", (r["tasks"][0],)).fetchone()
    assert t["title"] == "🚨 Fix: Parking lot surface (potholes, cracks)" and t["property_id"] == pid
    assert t["due_date"] == (T + timedelta(days=2)).isoformat() and "Pothole by Suite 104" in t["description"]
    html = mgr.get(r["report"]).text
    assert "Inspect Center" in html and "Pothole by Suite 104" in html and "Front elevation" not in html  # captions not printed
    assert f"/api/inspection-photos/{ph['id']}" in html and "Overall good shape." in html and "Needs attention" in html
    again = mgr.post(f"/api/inspections/{i['id']}/finish", json={}).json()
    assert again["tasks"] == []                                                 # never duplicates tasks
    lst = mgr.get(f"/api/properties/{pid}/inspections").json()["items"]
    assert lst[0]["issues"] == 2 and lst[0]["photos"] == 2 and lst[0]["status"] == "done"


def test_inspection_checks_off_quarterly_steps(mgr):
    from app import services
    pid = mgr.post("/api/properties", json={"address": "77 Quarter Rd, San Antonio, TX", "name": "Quarter Plaza"}).json()
    pid = pid if isinstance(pid, int) else pid["id"]
    with tx() as c:
        tid = c.execute("SELECT id FROM templates WHERE name='Quarterly Property Inspection'").fetchone()[0]
    proj = services.create_project_from_template(tid, "Q4 Inspection – Quarter Plaza", T - timedelta(days=3))
    with tx() as c:
        c.execute("UPDATE projects SET property_id=? WHERE id=?", (pid, proj))
        steps = {r["title"]: r["id"] for r in c.execute("SELECT id, title FROM tasks WHERE project_id=?", (proj,))}
    info = mgr.get(f"/api/tasks/{steps['Exterior walk']}").json()["inspection"]
    assert info == {"property_id": pid, "draft_id": None, "report_id": None, "report_date": None}
    i = mgr.post(f"/api/properties/{pid}/inspections").json()
    assert mgr.get(f"/api/tasks/{steps['Exterior walk']}").json()["inspection"]["draft_id"] == i["id"]
    for it in i["items"]:
        if it["section"] in ("Site & parking", "Building exterior", "Common areas"):
            mgr.patch(f"/api/inspection-items/{it['id']}", json={"rating": "good"})
    r = mgr.post(f"/api/inspections/{i['id']}/finish", json={}).json()
    with tx() as c:
        st = {t: c.execute("SELECT status FROM tasks WHERE id=?", (x,)).fetchone()[0] for t, x in steps.items()}
        note = c.execute("SELECT note FROM task_updates WHERE task_id=? ORDER BY id DESC", (steps["Work orders for issues found"],)).fetchone()[0]
    assert st["Exterior walk"] == st["Common areas & life safety"] == st["Photos & inspection report"] == "done"
    assert st["Work orders for issues found"] == "done" and "No issues" in note
    assert st["Check vacant suites"] != "done" and st["Send summary to owner / asset manager"] != "done"   # not looked at / still the PM's
    assert len(r["steps"]) == 4
    assert mgr.get(f"/api/tasks/{steps['Exterior walk']}").json()["inspection"]["report_id"] == i["id"]
    assert mgr.get(f"/api/tasks/{steps['Send summary to owner / asset manager']}").json()["inspection"] is None
