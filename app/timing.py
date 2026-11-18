"""Time estimates, actual time spent, and turnaround — so the team learns how long work really takes.

tasks.estimate_minutes   – planned effort (copied from the template item when a project starts)
tasks.actual_minutes     – effort it really took, entered when it's done (web or Blink: `time 12 1h30 note`)
tasks.time_notes         – why it took that long ("waited 2 days on owner", "first time doing this")
tasks.created_at / started_at / completed_at – for turnaround (calendar time, not effort)
"""
import re
from datetime import datetime

from .db import rows, tx

COLUMNS = [("tasks", "estimate_minutes", "INTEGER"), ("tasks", "actual_minutes", "INTEGER"),
           ("tasks", "time_notes", "TEXT"), ("tasks", "created_at", "TEXT"), ("tasks", "started_at", "TEXT"),
           ("tasks", "completed_at", "TEXT"), ("template_items", "estimate_minutes", "INTEGER")]


def init():
    with tx() as c:
        for table, col, typ in COLUMNS:
            if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        # stamp creation time on every new task, whichever code path inserts it
        c.execute("""CREATE TRIGGER IF NOT EXISTS tasks_created_at AFTER INSERT ON tasks WHEN NEW.created_at IS NULL
                     BEGIN UPDATE tasks SET created_at=datetime('now') WHERE id=NEW.id; END""")
        # older tasks: best guess for created_at from their first update
        c.execute("""UPDATE tasks SET created_at=COALESCE(
                       (SELECT MIN(created_at) FROM task_updates u WHERE u.task_id=tasks.id), datetime('now'))
                     WHERE created_at IS NULL""")


_UNIT_H = r"(?:h|hr|hrs|hours?)"
_UNIT_M = r"(?:m|min|mins|minutes?)"
LEAD_RE = re.compile(
    rf"""^\s*(?:
        (?P<h>\d+(?:\.\d+)?)\s*{_UNIT_H}\.?\s*(?:(?P<hm>\d+)\s*(?:{_UNIT_M})?)?   # 1h, 1.5 hours, 1h30, 1h 30m
      | (?P<m>\d+)\s*{_UNIT_M}                                                   # 45m, 90 minutes
      | (?P<ch>\d+):(?P<cm>\d{{2}})                                                # 1:15
      | (?P<n>\d+(?:\.\d+)?)(?=\s|$|[,;])                                          # 90 (minutes)
    )(?![\w:])\s*[,;:\-–]?\s*""", re.I | re.X)


def _lead(text: str) -> tuple[int | None, str]:
    m = LEAD_RE.match(text or "")
    if not m:
        return None, (text or "").strip()
    if m["h"]:
        mins = round(float(m["h"]) * 60) + int(m["hm"] or 0)
    elif m["m"]:
        mins = int(m["m"])
    elif m["ch"]:
        mins = int(m["ch"]) * 60 + int(m["cm"])
    else:
        mins = round(float(m["n"]))
    return (mins or None), text[m.end():].strip()


def parse_duration(text: str | int | float | None) -> int | None:
    """'1h 30m' / '1.5h' / '1h30' / '90m' / '90' / '1:15' / '2 hours' -> minutes. None if blank or unreadable."""
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)):
        return int(text) if text > 0 else None
    mins, rest = _lead(str(text))
    return mins if not rest else None


def split_duration_and_note(text: str) -> tuple[int | None, str]:
    """'1h30 had to chase the owner' -> (90, 'had to chase the owner')"""
    return _lead(text)


def fmt(mins) -> str:
    if not mins:
        return "—"
    h, m = divmod(int(mins), 60)
    return f"{h}h {m}m" if h and m else (f"{h}h" if h else f"{m}m")


def set_times(task_id: int, estimate=None, actual=None, notes=None):
    sets, vals = [], []
    if estimate is not None:
        sets.append("estimate_minutes=?"); vals.append(estimate or None)
    if actual is not None:
        sets.append("actual_minutes=?"); vals.append(actual or None)
    if notes is not None:
        sets.append("time_notes=?"); vals.append(notes.strip() or None)
    if sets:
        with tx() as c:
            c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*vals, task_id))


def on_status(c, task_id: int, old: str | None, new: str | None):
    """Called inside apply_update's transaction."""
    now = datetime.now().isoformat(timespec="seconds")
    if new == "in_progress":
        c.execute("UPDATE tasks SET started_at=COALESCE(started_at, ?) WHERE id=?", (now, task_id))
    if new == "done" and old != "done":
        c.execute("UPDATE tasks SET completed_at=?, started_at=COALESCE(started_at, ?) WHERE id=?", (now, now, task_id))
    if new and new != "done" and old == "done":
        c.execute("UPDATE tasks SET completed_at=NULL WHERE id=?", (task_id,))


# ---------- insights ----------

def insights(days: int = 180) -> dict:
    """Completed tasks in the last `days`: by task (title), by person, overall."""
    with tx() as c:
        done = rows(c.execute(
            """SELECT t.id, t.title, t.estimate_minutes AS est, t.actual_minutes AS act, t.time_notes,
                      t.created_at, t.started_at, t.completed_at, COALESCE(u.name,'Unassigned') AS who,
                      p.name AS project_name, COALESCE(tpl.name,'') AS template_name
               FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN users u ON u.id=t.assignee_id
               LEFT JOIN templates tpl ON tpl.id=p.template_id
               WHERE t.status='done' AND t.completed_at >= datetime('now', ?)""", (f"-{days} days",)))

    def turnaround_days(t):
        try:
            return (datetime.fromisoformat(t["completed_at"]) - datetime.fromisoformat(t["created_at"])).total_seconds() / 86400
        except (TypeError, ValueError):
            return None

    def summarize(group):
        acts = [t["act"] for t in group if t["act"]]
        ests = [t["est"] for t in group if t["est"]]
        pairs = [(t["est"], t["act"]) for t in group if t["est"] and t["act"]]
        tas = [d for d in (turnaround_days(t) for t in group) if d is not None]
        return {
            "count": len(group), "with_actual": len(acts),
            "avg_estimate": round(sum(ests) / len(ests)) if ests else None,
            "avg_actual": round(sum(acts) / len(acts)) if acts else None,
            "min_actual": min(acts) if acts else None, "max_actual": max(acts) if acts else None,
            # >0 = takes longer than estimated
            "variance_pct": round(100 * (sum(a for _, a in pairs) - sum(e for e, _ in pairs)) / sum(e for e, _ in pairs))
            if pairs else None,
            "avg_turnaround_days": round(sum(tas) / len(tas), 1) if tas else None,
        }

    by_title: dict = {}
    by_person: dict = {}
    for t in done:
        by_title.setdefault(t["title"].strip().lower(), []).append(t)
        by_person.setdefault(t["who"], []).append(t)
    tasks = [{"title": g[0]["title"], **summarize(g),
              "notes": [{"who": t["who"], "project": t["project_name"], "actual": t["act"], "note": t["time_notes"]}
                        for t in g if t["time_notes"]][-5:]}
             for g in by_title.values()]
    tasks.sort(key=lambda x: (-(x["with_actual"]), -x["count"]))
    people = sorted(({"name": k, **summarize(g)} for k, g in by_person.items()), key=lambda x: -x["count"])
    return {"days": days, "overall": summarize(done), "tasks": tasks, "people": people,
            "missing_actual": sum(1 for t in done if not t["act"])}
