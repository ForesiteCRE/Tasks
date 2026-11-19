"""Departments and department-head escalations.

Each person belongs to a department; each department has a head. A daily job tells each head (one Blink digest)
about things in their department that have gone on too long — each item once:
  dept_overdue_days  – a task is this many days overdue                → head of the assignee's department
  dept_blocked_days  – a task has been Blocked this many days          → head of the assignee's department
  dept_date_days     – a critical date passed this many days ago and isn't handled
                                                                        → head of the property manager's department
If the department has no head (or the person IS the head), managers get it instead, as before.
"""
from datetime import date, timedelta

from . import automations
from .blockers import text as blocked_text
from .db import get_setting, rows, set_setting, tx

DEFAULTS = {"dept_overdue_days": "3", "dept_blocked_days": "3", "dept_date_days": "1"}
STARTER = ["Property Management", "Accounting", "Leasing", "Investment Sales", "Brokerage Operations", "Marketing"]


def init():
    with tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS departments(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, head_user_id INTEGER, position INTEGER NOT NULL DEFAULT 0)""")
        if "department_id" not in {r[1] for r in c.execute("PRAGMA table_info(users)")}:
            c.execute("ALTER TABLE users ADD COLUMN department_id INTEGER")
        if not c.execute("SELECT 1 FROM departments").fetchone() and not get_setting("departments.seeded"):
            c.executemany("INSERT INTO departments(name, position) VALUES(?,?)", [(n, i) for i, n in enumerate(STARTER)])
    set_setting("departments.seeded", "1")


def settings() -> dict:
    return {k: get_setting(f"automation.{k}") or v for k, v in DEFAULTS.items()}


def list_all() -> list[dict]:
    with tx() as c:
        ds = rows(c.execute("""SELECT d.*, u.name AS head_name,
                                      (SELECT COUNT(*) FROM users m WHERE m.department_id=d.id) AS members
                               FROM departments d LEFT JOIN users u ON u.id=d.head_user_id ORDER BY d.position, d.name"""))
    return ds


def create(name: str, head_user_id=None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Name the department.")
    with tx() as c:
        if c.execute("SELECT 1 FROM departments WHERE lower(name)=lower(?)", (name,)).fetchone():
            raise ValueError("That department already exists.")
        pos = c.execute("SELECT COALESCE(MAX(position),0)+1 FROM departments").fetchone()[0]
        did = c.execute("INSERT INTO departments(name, head_user_id, position) VALUES(?,?,?)",
                        (name, head_user_id or None, pos)).lastrowid
        if head_user_id:   # a head belongs to the department they run
            c.execute("UPDATE users SET department_id=? WHERE id=?", (did, head_user_id))
        return did


def update(dept_id: int, fields: dict):
    with tx() as c:
        if fields.get("name"):
            c.execute("UPDATE departments SET name=? WHERE id=?", (fields["name"].strip(), dept_id))
        if "head_user_id" in fields:
            c.execute("UPDATE departments SET head_user_id=? WHERE id=?", (fields["head_user_id"] or None, dept_id))
            if fields["head_user_id"]:
                c.execute("UPDATE users SET department_id=? WHERE id=? AND department_id IS NULL", (dept_id, fields["head_user_id"]))


def delete(dept_id: int):
    with tx() as c:
        c.execute("UPDATE users SET department_id=NULL WHERE department_id=?", (dept_id,))
        c.execute("DELETE FROM departments WHERE id=?", (dept_id,))


def set_user_department(user_id: int, dept_id):
    with tx() as c:
        c.execute("UPDATE users SET department_id=? WHERE id=?", (dept_id or None, user_id))


def head_of(c, user_id) -> dict | None:
    """The department head for this person (None if no department/head, or they are the head themselves)."""
    if not user_id:
        return None
    r = c.execute("""SELECT h.id, h.name, h.blink_user_id, d.name AS dept FROM users u JOIN departments d ON d.id=u.department_id
                     JOIN users h ON h.id=d.head_user_id WHERE u.id=?""", (user_id,)).fetchone()
    return dict(r) if r and r["id"] != user_id else None


def _pm_of_property(c, property_id):
    r = c.execute("""SELECT pt.user_id FROM property_team pt JOIN project_roles r ON r.id=pt.role_id
                     WHERE pt.property_id=? AND r.name='Property Manager'""", (property_id,)).fetchone()
    return r["user_id"] if r else None


def run_escalations(today: date | None = None) -> int:
    """Daily. Returns how many items were escalated."""
    today = today or date.today()
    s = settings()
    od, bd, dd = (int(s[k] or 0) for k in ("dept_overdue_days", "dept_blocked_days", "dept_date_days"))
    items = []   # (rule, id, person_id, line)
    with tx() as c:
        if od > 0:
            for t in rows(c.execute(
                    """SELECT t.id, t.title, t.due_date, t.assignee_id, COALESCE(u.name,'Unassigned') AS who, p.name AS project
                       FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN users u ON u.id=t.assignee_id
                       WHERE t.status!='done' AND p.status='active' AND t.due_date IS NOT NULL AND t.due_date<=?
                         AND NOT EXISTS (SELECT 1 FROM automation_log a WHERE a.rule='dept_overdue' AND a.task_id=t.id)""",
                    ((today - timedelta(days=od)).isoformat(),))):
                late = (today - date.fromisoformat(t["due_date"])).days
                items.append(("dept_overdue", t["id"], t["assignee_id"],
                              f"⚠️ #{t['id']} {t['title']} · {t['who']} · {t['project']} — **{late} days overdue**"))
        if bd > 0:
            for t in rows(c.execute(
                    """SELECT t.id, t.title, t.assignee_id, COALESCE(u.name,'Unassigned') AS who, p.name AS project,
                              t.blocked_kind, t.blocked_label, t.blocked_reason,
                              (SELECT MAX(created_at) FROM task_updates x WHERE x.task_id=t.id AND x.status='blocked') AS since,
                              (SELECT note FROM task_updates x WHERE x.task_id=t.id AND x.status='blocked' ORDER BY id DESC LIMIT 1) AS note
                       FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN users u ON u.id=t.assignee_id
                       WHERE t.status='blocked' AND p.status='active'
                         AND NOT EXISTS (SELECT 1 FROM automation_log a WHERE a.rule='dept_blocked' AND a.task_id=t.id)""")):
                if t["since"] and t["since"][:10] <= (today - timedelta(days=bd)).isoformat():
                    days = (today - date.fromisoformat(t["since"][:10])).days
                    items.append(("dept_blocked", t["id"], t["assignee_id"],
                                  f"⛔ #{t['id']} {t['title']} · {t['who']} · {t['project']} — **blocked {days} days**"
                                  + (f" — {blocked_text(t) or t['note']}" if (blocked_text(t) or t["note"]) else "")))
        if dd > 0:
            for d in rows(c.execute(
                    """SELECT cd.id, cd.title, cd.date, cd.property_id, cd.assignee_id, p.name AS property FROM critical_dates cd
                       JOIN properties p ON p.id=cd.property_id WHERE cd.status='open' AND cd.date<=?
                         AND NOT EXISTS (SELECT 1 FROM automation_log a WHERE a.rule='dept_date' AND a.task_id=cd.id)""",
                    ((today - timedelta(days=dd)).isoformat(),))):
                late = (today - date.fromisoformat(d["date"])).days
                owner = _pm_of_property(c, d["property_id"]) or d["assignee_id"]
                items.append(("dept_date", d["id"], owner,
                              f"⚑ {d['title']} · {d['property']} — **passed {late} day{'s' if late != 1 else ''} ago, not handled**"))
        # group by who should hear about it
        by_head, to_managers = {}, []
        for rule, iid, person, line in items:
            h = head_of(c, person)
            if h and h["blink_user_id"]:
                by_head.setdefault(h["blink_user_id"], {"name": h["name"], "dept": h["dept"], "lines": []})["lines"].append(line)
            else:
                to_managers.append(line)
        c.executemany("INSERT OR IGNORE INTO automation_log(rule, task_id) VALUES(?,?)", [(r, i) for r, i, _, _ in items])
    for blink_id, g in by_head.items():
        automations._send([blink_id], f"📣 **{g['dept']}: {len(g['lines'])} item{'s' if len(g['lines']) != 1 else ''} "
                                      f"going on too long**\n" + "\n".join(g["lines"][:25])
                          + (f"\n…and {len(g['lines']) - 25} more" if len(g["lines"]) > 25 else ""))
    if to_managers:
        automations._send(automations._managers(), f"📣 **{len(to_managers)} item{'s' if len(to_managers) != 1 else ''} going on "
                                                   f"too long** (no department head set)\n" + "\n".join(to_managers[:25]))
    return len(items)


def reset(rule: str, item_id: int):
    """Due date moved / task unblocked → allow a fresh escalation later."""
    with tx() as c:
        c.execute("DELETE FROM automation_log WHERE rule=? AND task_id=?", (rule, item_id))
