"""Home dashboards (personal + team), personal reminders, and the 6pm daily recap in Blink."""
import json
import logging
import os
import re
from datetime import date, datetime, timedelta

from . import blink
from .db import get_setting, rows, set_setting, tx

log = logging.getLogger("dashboard")
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "18"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    remind_at TEXT NOT NULL,            -- local time, ISO "YYYY-MM-DDTHH:MM"
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init():
    with tx() as c:
        c.executescript(SCHEMA)
        if "daily_digest" not in {r[1] for r in c.execute("PRAGMA table_info(users)")}:
            c.execute("ALTER TABLE users ADD COLUMN daily_digest INTEGER NOT NULL DEFAULT 1")


# ---------- shared query bits ----------

TASK_COLS = """t.id, t.title, t.status, t.due_date, t.project_id, t.parent_id, t.assignee_id, t.estimate_minutes,
               t.blocked_kind, t.blocked_label, t.blocked_reason, t.blocked_follow_up, t.blocked_since,
               t.actual_minutes, t.completed_at, p.name AS project_name, COALESCE(u.name,'Unassigned') AS assignee_name,
               (SELECT note FROM task_updates tu WHERE tu.task_id=t.id AND note IS NOT NULL ORDER BY tu.id DESC LIMIT 1) AS last_note,
               (SELECT GROUP_CONCAT(b.id) FROM task_dependencies d JOIN tasks b ON b.id=d.depends_on_id
                WHERE d.task_id=t.id AND b.status!='done') AS waiting_on"""
FROM = "FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN users u ON u.id=t.assignee_id"


def _q(sql, args=()):
    with tx() as c:
        return rows(c.execute(sql, args))


def _blockers(task_id):
    return _q("""SELECT b.id, b.title, COALESCE(u.name,'Unassigned') AS assignee_name FROM task_dependencies d
                 JOIN tasks b ON b.id=d.depends_on_id LEFT JOIN users u ON u.id=b.assignee_id
                 WHERE d.task_id=? AND b.status!='done'""", (task_id,))


# ---------- my dashboard ----------

def my_dashboard(uid: int) -> dict:
    today = date.today()
    t_iso, wk, horizon = today.isoformat(), (today + timedelta(days=7)).isoformat(), (today + timedelta(days=21)).isoformat()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    user = _q("SELECT id, name, email, role, daily_digest, week_ahead FROM users WHERE id=?", (uid,))
    user = user[0] if user else {"id": uid, "name": "", "role": "staff", "daily_digest": 1, "week_ahead": 1}

    open_mine = _q(f"SELECT {TASK_COLS} {FROM} WHERE t.assignee_id=? AND t.status!='done' AND p.status='active' "
                   "ORDER BY t.due_date IS NULL, t.due_date, t.id", (uid,))
    for t in open_mine:
        t["overdue"] = bool(t["due_date"] and t["due_date"] < t_iso)

    reminders = _q("SELECT * FROM reminders WHERE user_id=? AND done=0 ORDER BY remind_at", (uid,))

    # Agenda: every open task of mine with a due date (far-off ones land in "Later"), plus the next 3 weeks of
    # reminders and project start dates
    agenda = [{"kind": "task", "date": t["due_date"], **t} for t in open_mine if t["due_date"]]
    agenda += [{"kind": "reminder", "date": r["remind_at"][:10], "time": r["remind_at"][11:16], "id": r["id"],
                "title": r["text"], "task_id": r["task_id"]} for r in reminders if r["remind_at"][:10] <= horizon]
    my_projects = _q(
        """SELECT DISTINCT p.* FROM projects p WHERE p.status='active' AND p.is_general=0 AND (
             EXISTS (SELECT 1 FROM tasks t WHERE t.project_id=p.id AND t.assignee_id=?)
             OR EXISTS (SELECT 1 FROM project_team pt WHERE pt.project_id=p.id AND pt.user_id=?))
           ORDER BY p.start_date DESC""", (uid, uid))
    agenda += [{"kind": "project_start", "date": p["start_date"], "id": p["id"], "title": p["name"]}
               for p in my_projects if t_iso <= p["start_date"] <= horizon]
    agenda.sort(key=lambda a: (a["date"] or "9999", a.get("time") or ""))

    blocked = []
    for t in open_mine:
        bl = _blockers(t["id"]) if t["waiting_on"] else []
        if t["status"] == "blocked" or bl:
            blocked.append({**t, "blockers": bl})

    waiting_on_me = _q(
        f"""SELECT {TASK_COLS}, mine.id AS my_task_id, mine.title AS my_task_title, mine.due_date AS my_due
            {FROM} JOIN task_dependencies d ON d.task_id=t.id JOIN tasks mine ON mine.id=d.depends_on_id
            WHERE mine.assignee_id=? AND mine.status!='done' AND t.status!='done' AND p.status='active'
            ORDER BY mine.due_date IS NULL, mine.due_date""", (uid,))

    projects = []
    for p in my_projects:
        stats = _q("""SELECT COUNT(*) AS total, SUM(status='done') AS done,
                             SUM(assignee_id=? AND status!='done') AS my_open,
                             SUM(status!='done' AND due_date < ?) AS overdue,
                             MIN(CASE WHEN status!='done' AND assignee_id=? THEN due_date END) AS my_next_due
                      FROM tasks WHERE project_id=?""", (uid, t_iso, uid, p["id"]))[0]
        roles = [r["name"] for r in _q("""SELECT r.name FROM project_team pt JOIN project_roles r ON r.id=pt.role_id
                                          WHERE pt.project_id=? AND pt.user_id=?""", (p["id"], uid))]
        projects.append({"id": p["id"], "name": p["name"], "property": p.get("property"), "client": p.get("client"),
                         "roles": roles, **{k: stats[k] or 0 for k in ("total", "done", "my_open", "overdue")},
                         "my_next_due": stats["my_next_due"]})

    handle = (user.get("email") or "").split("@")[0].lower()
    first = (user.get("name") or "").split(" ")[0].lower()
    activity = _q(
        """SELECT cm.id, cm.task_id, cm.body, cm.created_at, cm.source, COALESCE(u.name,'Someone') AS who, t.title AS task_title
           FROM task_comments cm JOIN tasks t ON t.id=cm.task_id LEFT JOIN users u ON u.id=cm.user_id
           WHERE cm.user_id IS NOT ? AND cm.created_at >= datetime('now','-10 days')
             AND (t.assignee_id=? OR lower(cm.body) LIKE ? OR (?!='' AND lower(cm.body) LIKE ?))
           ORDER BY cm.id DESC LIMIT 12""", (uid, uid, f"%@{handle}%" if handle else "\x00", first, f"%@{first}%"))
    for a in activity:
        a["mentioned"] = bool(re.search(rf"@({re.escape(handle)}|{re.escape(first)})\b", a["body"].lower())) if handle or first else False

    done_week = _q(f"SELECT {TASK_COLS} {FROM} WHERE t.assignee_id=? AND t.status='done' AND t.completed_at >= ? "
                   "ORDER BY t.completed_at DESC", (uid, week_start))
    return {
        "user": user, "today": t_iso,
        "counts": {
            "overdue": sum(t["overdue"] for t in open_mine),
            "due_today": sum(1 for t in open_mine if t["due_date"] == t_iso),
            "due_week": sum(1 for t in open_mine if t["due_date"] and t_iso <= t["due_date"] <= wk),
            "open": len(open_mine), "blocked": len(blocked), "waiting_on_me": len(waiting_on_me),
            "done_week": len(done_week), "time_week": sum(t["actual_minutes"] or 0 for t in done_week),
        },
        "agenda": agenda[:150], "no_date": [t for t in open_mine if not t["due_date"]][:10],
        "blocked": blocked, "waiting_on_me": waiting_on_me, "projects": projects,
        "reminders": reminders, "activity": activity, "done_week": done_week[:8],
    }


# ---------- team dashboard (managers) ----------

def team_dashboard() -> dict:
    today = date.today()
    t_iso, wk = today.isoformat(), (today + timedelta(days=7)).isoformat()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    open_all = _q(f"SELECT {TASK_COLS} {FROM} WHERE t.status!='done' AND p.status='active' "
                  "ORDER BY t.due_date IS NULL, t.due_date")
    for t in open_all:
        t["overdue"] = bool(t["due_date"] and t["due_date"] < t_iso)
    done_week = _q(f"SELECT {TASK_COLS} {FROM} WHERE t.status='done' AND t.completed_at >= ? ORDER BY t.completed_at DESC",
                   (week_start,))
    people = _q("SELECT id, name FROM users ORDER BY lower(name)")
    workload = []
    for u in people:
        mine = [t for t in open_all if t["assignee_id"] == u["id"]]
        workload.append({"id": u["id"], "name": u["name"], "open": len(mine),
                         "overdue": sum(t["overdue"] for t in mine),
                         "blocked": sum(1 for t in mine if t["status"] == "blocked" or t["waiting_on"]),
                         "due_week": sum(1 for t in mine if t["due_date"] and t_iso <= t["due_date"] <= wk),
                         "est_open": sum(t["estimate_minutes"] or 0 for t in mine),
                         "done_week": sum(1 for t in done_week if t["assignee_id"] == u["id"])})
    workload.sort(key=lambda w: (-w["overdue"], -w["open"]))

    projects = _q("SELECT * FROM projects WHERE status='active' AND is_general=0 ORDER BY start_date DESC")
    at_risk = []
    for p in projects:
        ts = _q("SELECT status, due_date FROM tasks WHERE project_id=?", (p["id"],))
        total = len(ts)
        done = sum(1 for t in ts if t["status"] == "done")
        overdue = sum(1 for t in ts if t["status"] != "done" and t["due_date"] and t["due_date"] < t_iso)
        blocked = sum(1 for t in ts if t["status"] == "blocked")
        nxt = min((t["due_date"] for t in ts if t["status"] != "done" and t["due_date"]), default=None)
        item = {"id": p["id"], "name": p["name"], "property": p.get("property"), "total": total, "done": done,
                "overdue": overdue, "blocked": blocked, "next_due": nxt}
        item["risk"] = overdue * 2 + blocked * 3
        at_risk.append(item)
    at_risk.sort(key=lambda x: (-x["risk"], x["next_due"] or "9999"))

    blocked_list = [t for t in open_all if t["status"] == "blocked"]
    for t in blocked_list:
        t["blockers"] = _blockers(t["id"])
    return {
        "today": t_iso,
        "counts": {"open": len(open_all), "overdue": sum(t["overdue"] for t in open_all), "blocked": len(blocked_list),
                   "due_week": sum(1 for t in open_all if t["due_date"] and t_iso <= t["due_date"] <= wk),
                   "unassigned": sum(1 for t in open_all if not t["assignee_id"]), "done_week": len(done_week),
                   "projects": len(projects), "time_week": sum(t["actual_minutes"] or 0 for t in done_week)},
        "workload": workload, "projects": at_risk[:12], "blocked": blocked_list[:20],
        "overdue": [t for t in open_all if t["overdue"]][:20],
        "unassigned": [t for t in open_all if not t["assignee_id"]][:15],
        "done_week": done_week[:15],
        "missing_time": [t for t in done_week if not t["actual_minutes"]][:15],
    }


# ---------- reminders ----------

def add_reminder(uid: int, text: str, remind_at: str, task_id=None) -> int:
    datetime.fromisoformat(remind_at)  # validates
    with tx() as c:
        return c.execute("INSERT INTO reminders(user_id, text, remind_at, task_id) VALUES(?,?,?,?)",
                         (uid, text.strip(), remind_at[:16], task_id)).lastrowid


def send_due_reminders() -> int:
    now = datetime.now().isoformat(timespec="minutes")
    due = _q("""SELECT r.*, u.blink_user_id FROM reminders r JOIN users u ON u.id=r.user_id
                WHERE r.sent=0 AND r.done=0 AND r.remind_at <= ?""", (now,))
    for r in due:
        if r["blink_user_id"]:
            extra = ""
            if r["task_id"]:
                t = _q("SELECT title FROM tasks WHERE id=?", (r["task_id"],))
                extra = f"\n(task #{r['task_id']} {t[0]['title']})" if t else ""
            try:
                blink.send_dm(r["blink_user_id"], f"⏰ **Reminder:** {r['text']}{extra}")
            except Exception:
                log.exception("reminder %s failed", r["id"])
        with tx() as c:
            c.execute("UPDATE reminders SET sent=1 WHERE id=?", (r["id"],))
    return len(due)


REMIND_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok", "remind_at", "text", "reply"],
                 "properties": {"ok": {"type": "boolean"}, "remind_at": {"type": "string"}, "text": {"type": "string"},
                                "reply": {"type": "string"}}}


def parse_reminder(message: str) -> dict | None:
    """'remind me Friday 9am to call the owner' -> {remind_at, text}. Uses Claude; falls back to
    'remind YYYY-MM-DD HH:MM text'."""
    m = re.match(r"^\s*remind(?:\s+me)?\s+(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?\s+(?:to\s+)?(.+)$", message, re.I | re.S)
    if m:
        return {"ok": True, "remind_at": f"{m[1]}T{(m[2] or '09:00').zfill(5)}", "text": m[3].strip()}
    from .chat import MODEL, ai_enabled
    if not ai_enabled():
        return None
    import anthropic
    now = datetime.now()
    try:
        resp = anthropic.Anthropic().beta.messages.create(
            model=MODEL, max_tokens=800, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": REMIND_SCHEMA}},
            system=("Turn a reminder request into a local date-time and a short reminder text. "
                    f"Now is {now:%A %Y-%m-%d %H:%M} (local). No time given -> 09:00. 'tonight' -> 18:00. "
                    "remind_at format YYYY-MM-DDTHH:MM. If you can't tell when, ok=false and reply asks when."),
            messages=[{"role": "user", "content": message}])
        out = json.loads(next(b.text for b in resp.content if b.type == "text"))
    except Exception:
        log.exception("reminder parse failed")
        return None
    return out


# ---------- 6pm daily recap ----------

def build_digest(uid: int) -> str | None:
    today = date.today()
    t_iso, wk = today.isoformat(), (today + timedelta(days=7)).isoformat()
    done_today = _q(f"SELECT {TASK_COLS} {FROM} WHERE t.assignee_id=? AND t.status='done' AND date(t.completed_at)=?",
                    (uid, t_iso))
    open_mine = _q(f"SELECT {TASK_COLS} {FROM} WHERE t.assignee_id=? AND t.status!='done' AND p.status='active' "
                   "ORDER BY t.due_date IS NULL, t.due_date", (uid,))
    overdue = [t for t in open_mine if t["due_date"] and t["due_date"] < t_iso]
    today_open = [t for t in open_mine if t["due_date"] == t_iso]
    upcoming = [t for t in open_mine if t["due_date"] and t_iso < t["due_date"] <= wk]
    others_open = [t for t in open_mine if t not in overdue and t not in today_open and t not in upcoming]
    waiting = _q("""SELECT COUNT(DISTINCT t.id) FROM task_dependencies d JOIN tasks t ON t.id=d.task_id
                    JOIN tasks mine ON mine.id=d.depends_on_id
                    WHERE mine.assignee_id=? AND mine.status!='done' AND t.status!='done'""", (uid,))[0]
    waiting = list(waiting.values())[0]
    tomorrow_rem = _q("SELECT text, remind_at FROM reminders WHERE user_id=? AND done=0 AND sent=0 AND date(remind_at)=?",
                      (uid, (today + timedelta(days=1)).isoformat()))
    if not (done_today or open_mine or tomorrow_rem):
        return None

    def line(t):
        due = f" – {date.fromisoformat(t['due_date']):%a %b %d}" if t["due_date"] else ""
        lock = " 🔒" if t["waiting_on"] else ""
        return f"• #{t['id']} {t['title']} · {t['project_name']}{due}{lock}"

    def block(title, items, cap=8):
        if not items:
            return None
        more = f"\n  …and {len(items) - cap} more" if len(items) > cap else ""
        return f"**{title} ({len(items)})**\n" + "\n".join(line(t) for t in items[:cap]) + more

    first = (_q("SELECT name FROM users WHERE id=?", (uid,))[0]["name"] or "").split(" ")[0]
    mins = sum(t["actual_minutes"] or 0 for t in done_today)
    head = f"🌆 **Your day, {first}** — {today:%A %b %d}"
    if done_today:
        head += f"\n✅ You finished {len(done_today)} task{'s' if len(done_today) != 1 else ''} today" + \
                (f" ({mins // 60}h {mins % 60}m logged)" if mins else "") + ". Nice work!"
    parts = [head,
             block("✅ Completed today", done_today),
             block("⚠️ Overdue", overdue),
             block("📌 Still due today", today_open),
             block("📅 Coming up (next 7 days)", upcoming),
             block("🗂 Other open tasks", others_open, cap=4)]
    if waiting:
        parts.append(f"⏳ {waiting} teammate task{'s are' if waiting != 1 else ' is'} waiting on yours.")
    if tomorrow_rem:
        parts.append("⏰ Tomorrow: " + "; ".join(f"{r['text']} ({r['remind_at'][11:16]})" for r in tomorrow_rem))
    missing = [t for t in done_today if not t["actual_minutes"]]
    if missing:
        parts.append(f"⏱ Log time with `time {missing[0]['id']} 1h30`" + (" (and the others)" if len(missing) > 1 else "") + ".")
    parts.append("Reply `list` anytime, or tell me what you got done.")
    return "\n\n".join(p for p in parts if p)


def send_daily_digests(force: bool = False, only_user: int | None = None) -> int:
    if not force and datetime.now().hour < DIGEST_HOUR:
        return 0
    key_day = date.today().isoformat()
    people = _q("SELECT id, blink_user_id FROM users WHERE blink_user_id IS NOT NULL AND daily_digest=1"
                + (" AND id=?" if only_user else ""), (only_user,) if only_user else ())
    sent = 0
    for u in people:
        if not force and get_setting(f"digest.{u['id']}") == key_day:
            continue
        text = build_digest(u["id"])
        if text:
            try:
                blink.send_dm(u["blink_user_id"], text)
                sent += 1
            except Exception:
                log.exception("digest for %s failed", u["id"])
        if not force:
            set_setting(f"digest.{u['id']}", key_day)
    return sent
