"""'Blocked by what?' — every Blocked task says what it's waiting on.

kind = 'task'   → waiting on another task (also stored as a 'waiting on' dependency; when that task is done this
                  one goes back to In progress automatically and the assignee hears "▶️ Your turn")
       'person' → waiting on someone: property contact (owner, lender…), vendor, or teammate
       'other'  → a short reason ("city permit")
Optional follow-up date → a reminder for the assignee that day.
"""
from datetime import date

from .db import rows, tx

COLS = [("blocked_kind", "TEXT"), ("blocked_ref", "TEXT"), ("blocked_label", "TEXT"), ("blocked_reason", "TEXT"),
        ("blocked_follow_up", "TEXT"), ("blocked_since", "TEXT"), ("blocked_reminder_id", "INTEGER")]


def init():
    with tx() as c:
        have = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
        for col, typ in COLS:
            if col not in have:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typ}")


def text(t: dict) -> str:
    """'Waiting on Rebecca Torres (Owner contact) — roof invoice' / 'Waiting on #12 Bank rec' / 'City permit'."""
    k = t.get("blocked_kind")
    if k in ("task", "person") and t.get("blocked_label"):
        return f"Waiting on {t['blocked_label']}" + (f" — {t['blocked_reason']}" if t.get("blocked_reason") else "")
    return t.get("blocked_reason") or ""


def options(task_id: int) -> dict:
    """What the 'Blocked by what?' picker offers for this task."""
    with tx() as c:
        t = c.execute("SELECT t.id, t.project_id, p.property_id, p.is_general FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=?",
                      (task_id,)).fetchone()
        if not t:
            return {}
        tasks = rows(c.execute("""SELECT t.id, t.title, t.status, COALESCE(u.name,'Unassigned') AS who FROM tasks t
                                  LEFT JOIN users u ON u.id=t.assignee_id WHERE t.project_id=? AND t.id!=? AND t.status!='done'
                                  ORDER BY t.position, t.id""", (t["project_id"], task_id)))
        people = []
        if t["property_id"]:
            people += [{"ref": f"contact:{r['id']}", "label": f"{r['name'] or r['company']} ({r['role']})", "group": "Property contacts"}
                       for r in c.execute("SELECT id, role, name, company FROM property_contacts WHERE property_id=? ORDER BY role, name",
                                          (t["property_id"],))]
            people += [{"ref": f"vendor:{r['id']}", "label": f"{r['name']} ({r['trade']})", "group": "Vendors at this property"}
                       for r in c.execute("""SELECT DISTINCT v.id, v.name, pv.trade FROM property_vendors pv JOIN vendors v ON v.id=pv.vendor_id
                                             WHERE pv.property_id=? AND pv.status='current' ORDER BY v.name""", (t["property_id"],))]
        people += [{"ref": f"vendor:{r['id']}", "label": f"{r['name']} (project vendor)", "group": "Vendors on this project"}
                   for r in c.execute("""SELECT DISTINCT v.id, v.name FROM project_vendors pv JOIN vendors v ON v.id=pv.vendor_id
                                         WHERE pv.project_id=?""", (t["project_id"],))]
        people += [{"ref": f"user:{r['id']}", "label": r["name"], "group": "Teammates"} for r in c.execute(
            "SELECT id, name FROM users ORDER BY lower(name)")]
    seen, uniq = set(), []
    for p in people:
        if p["ref"] not in seen:
            seen.add(p["ref"])
            uniq.append(p)
    return {"tasks": tasks, "people": uniq,
            "quick": ["Owner approval", "Vendor quote / bid", "Tenant", "City / permit", "Insurance", "Lender", "Invoice / payment"]}


def set_blocked(task_id: int, block: dict, actor_id=None) -> str:
    """Record what the task is blocked by (called when status becomes Blocked, or to edit it). Returns the text."""
    from . import collab, dashboard
    kind = block.get("kind")
    reason = (block.get("reason") or "").strip() or None
    ref = label = None
    if kind == "task":
        dep = int(block.get("task_id") or 0)
        with tx() as c:
            d = c.execute("SELECT id, title FROM tasks WHERE id=?", (dep,)).fetchone()
        if not d:
            raise ValueError("Pick the task it's waiting on.")
        collab.add_dependency(task_id, dep)          # raises on loops / self
        ref, label = f"task:{dep}", f"#{d['id']} {d['title']}"
    elif kind == "person":
        ref = (block.get("ref") or "").strip() or None
        label = (block.get("label") or "").strip() or None
        if ref and not label:
            label = _label_for(ref)
        if not (ref or label):
            raise ValueError("Pick who it's waiting on (or type a name).")
    elif kind == "other":
        if not reason:
            raise ValueError("Add a short reason it's blocked.")
    else:
        raise ValueError("Say what it's blocked by.")
    follow = block.get("follow_up") or None
    if follow:
        follow = date.fromisoformat(str(follow)[:10]).isoformat()
    with tx() as c:
        t = dict(c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        if t.get("blocked_reminder_id"):
            c.execute("DELETE FROM reminders WHERE id=? AND sent=0", (t["blocked_reminder_id"],))
        c.execute("""UPDATE tasks SET blocked_kind=?, blocked_ref=?, blocked_label=?, blocked_reason=?, blocked_follow_up=?,
                     blocked_since=COALESCE(blocked_since, datetime('now')), blocked_reminder_id=NULL WHERE id=?""",
                  (kind, ref, label, reason, follow, task_id))
    desc = text({"blocked_kind": kind, "blocked_label": label, "blocked_reason": reason})
    if follow and t.get("assignee_id"):
        rid = dashboard.add_reminder(t["assignee_id"], f"Follow up: {t['title']} — {desc}", f"{follow}T09:00", task_id=task_id)
        with tx() as c:
            c.execute("UPDATE tasks SET blocked_reminder_id=? WHERE id=?", (rid, task_id))
    return desc


def _label_for(ref: str) -> str | None:
    kind, _, rid = ref.partition(":")
    with tx() as c:
        if kind == "contact":
            r = c.execute("SELECT role, name, company FROM property_contacts WHERE id=?", (rid,)).fetchone()
            return f"{r['name'] or r['company']} ({r['role']})" if r else None
        if kind == "vendor":
            r = c.execute("SELECT name FROM vendors WHERE id=?", (rid,)).fetchone()
            return r["name"] if r else None
        if kind == "user":
            r = c.execute("SELECT name FROM users WHERE id=?", (rid,)).fetchone()
            return r["name"] if r else None
    return None


def clear(task_id: int):
    """Status left Blocked: forget the blocker (history keeps it) and cancel its follow-up reminder."""
    with tx() as c:
        t = c.execute("SELECT blocked_reminder_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if t and t["blocked_reminder_id"]:
            c.execute("DELETE FROM reminders WHERE id=? AND sent=0", (t["blocked_reminder_id"],))
        c.execute("""UPDATE tasks SET blocked_kind=NULL, blocked_ref=NULL, blocked_label=NULL, blocked_reason=NULL,
                     blocked_follow_up=NULL, blocked_since=NULL, blocked_reminder_id=NULL WHERE id=?""", (task_id,))


def note_as_reason(task_id: int, note: str):
    """A note arriving for a Blocked task with no reason yet (e.g. from Blink) becomes its reason."""
    with tx() as c:
        t = c.execute("SELECT status, blocked_kind FROM tasks WHERE id=?", (task_id,)).fetchone()
        if t and t["status"] == "blocked" and not t["blocked_kind"] and (note or "").strip():
            c.execute("UPDATE tasks SET blocked_kind='other', blocked_reason=?, blocked_since=COALESCE(blocked_since, datetime('now')) WHERE id=?",
                      (note.strip()[:300], task_id))
