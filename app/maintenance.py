"""Weekly team summary (Blink) and daily SQLite backups."""
import glob
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta

from . import blink
from .db import DB_PATH, get_setting, rows, set_setting, tx

log = logging.getLogger("maintenance")

SUMMARY_WEEKDAY = int(os.getenv("SUMMARY_WEEKDAY", "0"))  # 0 = Monday
BACKUP_KEEP_DAYS = int(os.getenv("BACKUP_KEEP_DAYS", "14"))
BACKUP_DIR = os.getenv("BACKUP_DIR") or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")


# ---------- weekly summary ----------

def build_summary(today: date | None = None) -> str:
    today = today or date.today()
    week_end = (today + timedelta(days=7)).isoformat()
    week_ago = (today - timedelta(days=7)).isoformat()
    with tx() as c:
        open_tasks = rows(c.execute(
            """SELECT t.id, t.title, t.status, t.due_date, p.name AS project, COALESCE(u.name,'Unassigned') AS who,
                      (SELECT note FROM task_updates tu WHERE tu.task_id=t.id AND note IS NOT NULL
                       ORDER BY tu.id DESC LIMIT 1) AS last_note
               FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN users u ON u.id=t.assignee_id
               WHERE t.status!='done' AND p.status='active' ORDER BY t.due_date IS NULL, t.due_date"""))
        done_last_week = c.execute(
            """SELECT COUNT(DISTINCT task_id) FROM task_updates WHERE status='done' AND created_at>=?""",
            (week_ago,)).fetchone()[0]
        projects_done = [r[0] for r in c.execute(
            """SELECT DISTINCT p.name FROM projects p JOIN tasks t ON t.project_id=p.id
               JOIN task_updates tu ON tu.task_id=t.id
               WHERE p.status='done' AND tu.created_at>=?""", (week_ago,))]

    overdue = [t for t in open_tasks if t["due_date"] and t["due_date"] < today.isoformat()]
    blocked = [t for t in open_tasks if t["status"] == "blocked"]
    due_soon = [t for t in open_tasks if t["due_date"] and today.isoformat() <= t["due_date"] <= week_end]

    def line(t, note=False):
        due = f" – due {date.fromisoformat(t['due_date']):%b %d}" if t["due_date"] else ""
        extra = f" — _{t['last_note']}_" if note and t["last_note"] else ""
        return f"• #{t['id']} {t['title']} · {t['who']} · {t['project']}{due}{extra}"

    def section(title, items, note=False, cap=15):
        if not items:
            return f"**{title}:** none 🎉"
        more = f"\n…and {len(items) - cap} more" if len(items) > cap else ""
        return f"**{title} ({len(items)}):**\n" + "\n".join(line(t, note) for t in items[:cap]) + more

    by_person: dict[str, int] = {}
    for t in overdue:
        by_person[t["who"]] = by_person.get(t["who"], 0) + 1
    people = ", ".join(f"{k} {v}" for k, v in sorted(by_person.items(), key=lambda kv: -kv[1]))

    parts = [
        f"📊 **Weekly task summary – week of {today:%b %d}**",
        f"✅ {done_last_week} task{'' if done_last_week == 1 else 's'} completed last week"
        + (f" · projects finished: {', '.join(projects_done)}" if projects_done else ""),
        section("⚠️ Overdue", overdue) + (f"\nBy person: {people}" if people else ""),
        section("⛔ Blocked", blocked, note=True),
        section("📅 Due in the next 7 days", due_soon),
    ]
    return "\n\n".join(parts)


def send_weekly_summary(force: bool = False, today: date | None = None) -> int:
    """Send to everyone with weekly_summary=1, once per ISO week, on SUMMARY_WEEKDAY."""
    today = today or date.today()
    week = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
    if not force and (today.weekday() != SUMMARY_WEEKDAY or get_setting("last_summary_week") == week):
        return 0
    with tx() as c:
        recipients = [r["blink_user_id"] for r in c.execute(
            "SELECT blink_user_id FROM users WHERE weekly_summary=1 AND blink_user_id IS NOT NULL")]
    if not recipients:
        return 0
    text = build_summary(today)
    for buid in recipients:
        blink.send_dm(buid, text)
    if not force:
        set_setting("last_summary_week", week)
    return len(recipients)


# ---------- backups ----------

def backup_now() -> str:
    """Consistent copy of the live DB (sqlite online backup API) into BACKUP_DIR; prunes old copies."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, f"tasks-{datetime.now():%Y-%m-%d_%H%M}.db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    cutoff = datetime.now() - timedelta(days=BACKUP_KEEP_DAYS)
    for f in glob.glob(os.path.join(BACKUP_DIR, "tasks-*.db")):
        if datetime.fromtimestamp(os.path.getmtime(f)) < cutoff:
            os.remove(f)
    set_setting("last_backup", date.today().isoformat())
    log.info("backup written: %s", dest)
    return dest


def daily_backup():
    if get_setting("last_backup") != date.today().isoformat():
        backup_now()


def list_backups() -> list[dict]:
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "tasks-*.db")), reverse=True)
    return [{"name": os.path.basename(f), "size_kb": round(os.path.getsize(f) / 1024, 1)} for f in files]
