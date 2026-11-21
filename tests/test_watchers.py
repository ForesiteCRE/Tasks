"""Watchers: the creator of a task for someone else hears when it's done / blocked / commented on."""
from app import notify, watchers
from app.db import tx
from conftest import SENT


def _user(name, blink):
    with tx() as c:
        r = c.execute("SELECT id FROM users WHERE name=?", (name,)).fetchone()
        uid = r[0] if r else c.execute("INSERT INTO users(name, email, role, blink_user_id) VALUES(?,?,?,?)",
                                       (name, name.split()[0].lower() + ".w@foresitecre.com", "staff", blink)).lastrowid
    notify.save(uid, {"frequency": "instant", "quiet": {"on": False, "weekends": False}})
    return uid


def test_creator_watches_and_hears_done(mgr, staff):
    dez = _user("Dez Watcher", "b-dez")
    with tx() as c:
        c.execute("UPDATE users SET blink_user_id='b-boss' WHERE id=?", (mgr.me["id"],))
    notify.save(mgr.me["id"], {"frequency": "instant", "quiet": {"on": False, "weekends": False}})
    tid = mgr.post("/api/tasks", json={"title": "Send the CAM reconciliation", "assignee_id": dez}).json()["id"]
    d = mgr.get(f"/api/tasks/{tid}").json()
    assert [w["user_id"] for w in d["watchers"]] == [mgr.me["id"]]
    own = mgr.post("/api/tasks", json={"title": "My own thing", "assignee_id": mgr.me["id"]}).json()["id"]
    assert mgr.get(f"/api/tasks/{own}").json()["watchers"] == []       # your own task: no watcher needed

    SENT.clear()
    from app import services
    services.apply_update(tid, dez, "web", "blocked", "waiting on the owner")
    assert any(b == "b-boss" and "Blocked" in t and "waiting on the owner" in t for b, t in SENT)
    SENT.clear()
    from app import collab
    collab.add_comment(tid, dez, "Sent the draft to the owner")
    assert any(b == "b-boss" and "Dez Watcher commented" in t and "watching" in t for b, t in SENT)
    SENT.clear()
    services.apply_update(tid, dez, "web", "done", None)
    msg = [t for b, t in SENT if b == "b-boss"]
    assert msg and "Done:" in msg[0] and "Send the CAM reconciliation" in msg[0] and "Dez Watcher" in msg[0]
    assert not [1 for b, t in SENT if b == "b-dez" and "Done:" in t]      # the person who did it isn't told
    w = mgr.get("/api/me/watching").json()
    assert any(x["id"] == tid for x in w["done"])


def test_watch_unwatch_permissions(mgr, staff):
    dez = _user("Dez Watcher", "b-dez")
    tid = mgr.post("/api/tasks", json={"title": "Order new signage", "assignee_id": dez}).json()["id"]
    assert staff.post(f"/api/tasks/{tid}/watchers", json={}).status_code == 200          # anyone can watch
    assert staff.post(f"/api/tasks/{tid}/watchers", json={"user_id": dez}).status_code == 200   # a watcher can add someone
    other = mgr.post("/api/tasks", json={"title": "Private-ish", "assignee_id": dez}).json()["id"]
    assert staff.post(f"/api/tasks/{other}/watchers", json={"user_id": dez}).status_code == 403
    assert staff.delete(f"/api/tasks/{tid}/watchers/{mgr.me['id']}").status_code == 403   # can't remove others
    r = staff.delete(f"/api/tasks/{tid}/watchers/{staff.me['id']}").json()
    assert staff.me["id"] not in [w["user_id"] for w in r["watchers"]]
    assert staff.get(f"/api/tasks/{tid}").json()["watchers"]


def test_blink_and_email_tasks_auto_watch(mgr):
    from app import newtask
    dez = _user("Dez Watcher", "b-dez")
    from conftest import CLAUDE
    CLAUDE.append({"title": "Call the roofer", "property_id": 0, "project_id": 0, "assignee_id": dez, "due_date": "", "estimate": ""})
    with tx() as c:
        me = dict(c.execute("SELECT id, name, role FROM users WHERE id=?", (mgr.me["id"],)).fetchone())
    reply = newtask.handle(me, "call the roofer for Dez")
    tid = int(reply.split("#")[1].split()[0])
    assert mgr.me["id"] in [w["user_id"] for w in watchers.for_task(tid)]
