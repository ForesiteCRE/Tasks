"""Repeating tasks and repeating projects.

Tasks: tasks.repeat is one of REPEATS. When a repeating task is marked done, the next occurrence is created
with the next due date (same person, project, estimate, tutorial; checklist unticked) and the repeat moves to it.
Projects: a recurrence (template + name pattern + every N months + day of month) creates a fresh project each
period. It's set from the New project dialog ("Repeats") instead of a separate form.
"""
import calendar
from datetime import date, timedelta

from .db import rows, tx

REPEATS = {"weekly": ("days", 7, "Every week"), "biweekly": ("days", 14, "Every 2 weeks"),
           "monthly": ("months", 1, "Every month"), "quarterly": ("months", 3, "Every quarter"),
           "yearly": ("months", 12, "Every year")}


def init():
    with tx() as c:
        for table, col, typ in [("tasks", "repeat", "TEXT"), ("tasks", "repeat_next_id", "INTEGER"),
                                ("template_items", "repeat", "TEXT"), ("recurrences", "interval_months", "INTEGER NOT NULL DEFAULT 1"),
                                ("recurrences", "anchor_month", "INTEGER"), ("recurrences", "team_json", "TEXT"),
                                ("recurrences", "property", "TEXT"), ("recurrences", "client", "TEXT"),
                                ("projects", "created_by", "INTEGER")]:
            if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def clean(rule):
    rule = (rule or "").strip().lower()
    if not rule or rule == "none":
        return None
    if rule not in REPEATS:
        raise ValueError(f"Repeat must be one of: {', '.join(REPEATS)}")
    return rule


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def next_due(rule: str, from_date: date) -> date:
    unit, n, _ = REPEATS[rule]
    return from_date + timedelta(days=n) if unit == "days" else add_months(from_date, n)


def on_done(task_id: int) -> dict | None:
    """Create the next occurrence of a repeating task (once). Returns the new task row or None."""
    with tx() as c:
        t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t or not t["repeat"] or t["repeat_next_id"]:
            return None
        base = date.fromisoformat(t["due_date"]) if t["due_date"] else date.today()
        due = next_due(t["repeat"], base)
        while due < date.today():          # finished very late: jump to the next upcoming date
            due = next_due(t["repeat"], due)
        new_id = c.execute(
            """INSERT INTO tasks(project_id, parent_id, title, description, assignee_id, role_id, due_date, position,
                                 estimate_minutes, tutorial_url, repeat) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (t["project_id"], t["parent_id"], t["title"], t["description"], t["assignee_id"], t["role_id"],
             due.isoformat(), t["position"], t["estimate_minutes"], t["tutorial_url"], t["repeat"])).lastrowid
        c.execute("""INSERT INTO task_checklist(task_id, text, position)
                     SELECT ?, text, position FROM task_checklist WHERE task_id=?""", (new_id, task_id))
        c.execute("UPDATE tasks SET repeat_next_id=? WHERE id=?", (new_id, task_id))
        c.execute("UPDATE projects SET status='active' WHERE id=?", (t["project_id"],))
        return {"id": new_id, "title": t["title"], "due_date": due.isoformat(), "assignee_id": t["assignee_id"]}


def stop_task(task_id: int):
    with tx() as c:
        c.execute("UPDATE tasks SET repeat=NULL WHERE id=?", (task_id,))


# ---------- repeating projects ----------

def create_project_recurrence(template_id: int, name: str, start: date, interval_months: int, team: dict | None,
                              details: dict | None) -> int:
    """Repeat a project made from a template: the first one already exists (this period); later ones are created
    on the same day of the month every N months, named '<name> – {month} {year}'."""
    import json
    details = details or {}
    pattern = name if "{month}" in name else f"{name} – {{month}} {{year}}"
    with tx() as c:
        return c.execute(
            """INSERT INTO recurrences(template_id, name_pattern, day_of_month, active, last_period, interval_months,
                                       anchor_month, team_json, property, client) VALUES(?,?,?,1,?,?,?,?,?,?)""",
            (template_id, pattern, start.day, start.strftime("%Y-%m"), interval_months, start.month,
             json.dumps(team or {}), details.get("property"), details.get("client"))).lastrowid


def project_name_for(name: str, d: date) -> str:
    pattern = name if "{month}" in name else f"{name} – {{month}} {{year}}"
    return pattern.format(month=d.strftime("%B"), year=d.year, mm=d.strftime("%m"))


def due_this_month(r: dict, today: date) -> bool:
    every = r.get("interval_months") or 1
    anchor = r.get("anchor_month") or today.month
    return (today.month - anchor) % every == 0


def overview() -> dict:
    """Everything that repeats, for the Recurring page."""
    with tx() as c:
        projects = rows(c.execute(
            """SELECT r.*, t.name AS template_name FROM recurrences r JOIN templates t ON t.id=r.template_id
               ORDER BY r.active DESC, r.id"""))
        tasks = rows(c.execute(
            """SELECT t.id, t.title, t.repeat, t.due_date, t.status, p.name AS project_name, p.is_general,
                      COALESCE(u.name,'Unassigned') AS assignee_name FROM tasks t JOIN projects p ON p.id=t.project_id
               LEFT JOIN users u ON u.id=t.assignee_id
               WHERE t.repeat IS NOT NULL AND t.repeat_next_id IS NULL ORDER BY t.due_date"""))
    today = date.today()
    for r in projects:
        every = r.get("interval_months") or 1
        r["every_label"] = {1: "Every month", 3: "Every quarter", 12: "Every year"}.get(every, f"Every {every} months")
        nxt = date(today.year, today.month, 1)
        for _ in range(25):
            run_day = nxt.replace(day=min(r["day_of_month"], calendar.monthrange(nxt.year, nxt.month)[1]))
            if due_this_month(r, nxt) and r.get("last_period") != nxt.strftime("%Y-%m") and run_day >= today:
                break
            nxt = add_months(nxt, 1)
        r["next_run"] = run_day.isoformat()
    for t in tasks:
        t["repeat_label"] = REPEATS.get(t["repeat"], ("", 0, t["repeat"]))[2]
    return {"projects": projects, "tasks": tasks, "options": {k: v[2] for k, v in REPEATS.items()}}
