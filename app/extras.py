"""Monday 'week ahead' message, unread @mention counts, and a private calendar feed (Google Calendar etc.)."""
import logging
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone

from . import blink
from .db import get_setting, rows, set_setting, tx

log = logging.getLogger("extras")
WEEK_AHEAD_HOUR = int(os.getenv("WEEK_AHEAD_HOUR", "8"))
WEEK_AHEAD_DAY = int(os.getenv("WEEK_AHEAD_DAY", "0"))  # 0 = Monday


def init():
    with tx() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        for col, typ in [("week_ahead", "INTEGER NOT NULL DEFAULT 1"), ("mentions_seen_at", "TEXT"), ("ics_token", "TEXT")]:
            if col not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")


def _q(sql, args=()):
    with tx() as c:
        return rows(c.execute(sql, args))


# ---------- Monday week ahead ----------

def build_week_ahead(uid: int, today: date | None = None) -> str | None:
    today = today or date.today()
    end = today + timedelta(days=6)
    first = ((_q("SELECT name FROM users WHERE id=?", (uid,)) or [{"name": ""}])[0]["name"] or "").split(" ")[0]
    tasks = _q("""SELECT t.id, t.title, t.due_date, t.status, t.estimate_minutes, p.name AS project_name, p.is_general,
                         (SELECT COUNT(*) FROM task_dependencies d JOIN tasks b ON b.id=d.depends_on_id
                          WHERE d.task_id=t.id AND b.status!='done') AS blocked_by
                  FROM tasks t JOIN projects p ON p.id=t.project_id
                  WHERE t.assignee_id=? AND t.status!='done' AND p.status='active' AND t.due_date IS NOT NULL
                    AND t.due_date<=? ORDER BY t.due_date, t.id""", (uid, end.isoformat()))
    overdue = [t for t in tasks if t["due_date"] < today.isoformat()]
    week = [t for t in tasks if t["due_date"] >= today.isoformat()]
    starts = _q("""SELECT DISTINCT p.name, p.start_date FROM projects p
                   WHERE p.is_general=0 AND p.start_date BETWEEN ? AND ? AND (
                     EXISTS (SELECT 1 FROM tasks t WHERE t.project_id=p.id AND t.assignee_id=?)
                     OR EXISTS (SELECT 1 FROM project_team pt WHERE pt.project_id=p.id AND pt.user_id=?))""",
                (today.isoformat(), end.isoformat(), uid, uid))
    rems = _q("""SELECT text, remind_at FROM reminders WHERE user_id=? AND done=0 AND date(remind_at) BETWEEN ? AND ?
                 ORDER BY remind_at""", (uid, today.isoformat(), end.isoformat()))
    waiting = _q("""SELECT COUNT(DISTINCT t.id) AS n FROM task_dependencies d JOIN tasks t ON t.id=d.task_id
                    JOIN tasks mine ON mine.id=d.depends_on_id
                    WHERE mine.assignee_id=? AND mine.status!='done' AND t.status!='done'""", (uid,))[0]["n"]
    if not (tasks or starts or rems):
        return None

    def line(t):
        proj = "" if t["is_general"] else f" · {t['project_name']}"
        est = f" (~{t['estimate_minutes'] // 60}h{t['estimate_minutes'] % 60:02d})" if t["estimate_minutes"] else ""
        return f"• #{t['id']} {t['title']}{proj}{est}{' 🔒' if t['blocked_by'] else ''}"

    parts = [f"📅 **Your week ahead, {first}** — {today:%b %d} to {end:%b %d}"]
    total_est = sum(t["estimate_minutes"] or 0 for t in week)
    parts.append(f"{len(week)} task{'s' if len(week) != 1 else ''} due this week"
                 + (f", about {total_est // 60}h of work" if total_est >= 60 else "")
                 + (f", plus **{len(overdue)} overdue** to catch up on" if overdue else "") + ".")
    if overdue:
        parts.append("**⚠️ Carry-over (overdue)**\n" + "\n".join(line(t) for t in overdue[:8]))
    by_day: dict = {}
    for t in week:
        by_day.setdefault(t["due_date"], []).append(t)
    for d, ts in by_day.items():
        dd = date.fromisoformat(d)
        label = "Today" if dd == today else f"{dd:%A %b %d}"
        parts.append(f"**{label}**\n" + "\n".join(line(t) for t in ts))
    if starts:
        parts.append("🚀 **Starting this week:** " + "; ".join(f"{s['name']} ({date.fromisoformat(s['start_date']):%a})" for s in starts))
    if rems:
        parts.append("⏰ **Reminders:** " + "; ".join(
            f"{r['text']} ({datetime.fromisoformat(r['remind_at']):%a %-I:%M %p})" for r in rems))
    if waiting:
        parts.append(f"⏳ {waiting} teammate task{'s are' if waiting != 1 else ' is'} waiting on yours — worth doing those first.")
    parts.append("Have a great week! Reply `list` anytime.")
    return "\n\n".join(parts)


def send_week_ahead(force: bool = False, only_user: int | None = None) -> int:
    now = datetime.now()
    if not force and (now.weekday() != WEEK_AHEAD_DAY or now.hour < WEEK_AHEAD_HOUR):
        return 0
    week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    people = _q("SELECT id, blink_user_id FROM users WHERE blink_user_id IS NOT NULL AND week_ahead=1"
                + (" AND id=?" if only_user else ""), (only_user,) if only_user else ())
    sent = 0
    for u in people:
        if not force and get_setting(f"weekahead.{u['id']}") == week:
            continue
        text = build_week_ahead(u["id"])
        if text:
            try:
                if force:
                    blink.send_dm(u["blink_user_id"], text)
                else:
                    from . import notify
                    notify.deliver(u["id"], "week_ahead", text, blink_id=u["blink_user_id"])
                sent += 1
            except Exception:
                log.exception("week ahead for %s failed", u["id"])
        if not force:
            set_setting(f"weekahead.{u['id']}", week)
    return sent


# ---------- unread @mentions ----------

def _mention_filter(uid: int):
    u = (_q("SELECT name, email, mentions_seen_at FROM users WHERE id=?", (uid,)) or [{}])[0]
    handle = (u.get("email") or "").split("@")[0].lower()
    first = (u.get("name") or "").split(" ")[0].lower()
    return u.get("mentions_seen_at") or "1970-01-01", handle, first


def unread_mentions(uid: int) -> list[dict]:
    """Comments by others that @mention me (by handle or first name) since I last looked."""
    seen, handle, first = _mention_filter(uid)
    cands = _q("""SELECT cm.id, cm.task_id, cm.body, cm.created_at FROM task_comments cm
                  WHERE cm.user_id IS NOT ? AND cm.created_at > ? AND cm.body LIKE '%@%'""", (uid, seen))
    pat = re.compile(rf"@({'|'.join(re.escape(x) for x in (handle, first) if x)})\b", re.I) if (handle or first) else None
    return [c for c in cands if pat and pat.search(c["body"])]


def mark_mentions_seen(uid: int):
    with tx() as c:
        c.execute("UPDATE users SET mentions_seen_at=datetime('now') WHERE id=?", (uid,))


# ---------- calendar feed (iCalendar) ----------

def calendar_token(uid: int, regenerate: bool = False) -> str:
    with tx() as c:
        tok = c.execute("SELECT ics_token FROM users WHERE id=?", (uid,)).fetchone()["ics_token"]
        if regenerate or not tok:
            tok = secrets.token_urlsafe(24)
            c.execute("UPDATE users SET ics_token=? WHERE id=?", (tok, uid))
    return tok


def user_for_token(token: str) -> dict | None:
    r = _q("SELECT id, name FROM users WHERE ics_token=?", (token,)) if token else []
    return r[0] if r else None


def _esc(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    out, cur = [], line.encode()
    while len(cur) > 75:
        cut = 75
        while cut and (cur[cut] & 0xC0) == 0x80:   # don't split a UTF-8 character
            cut -= 1
        out.append(cur[:cut].decode())
        cur = b" " + cur[cut:]
    out.append(cur.decode())
    return "\r\n".join(out)


def build_ics(uid: int, base_url: str = "") -> str:
    """Open tasks with due dates (all-day), reminders (timed) and project starts for one person."""
    name = _q("SELECT name FROM users WHERE id=?", (uid,))[0]["name"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    host = re.sub(r"^https?://", "", base_url).strip("/") or "tasks"
    ev = []

    def event(uid_, summary, start, end, desc="", allday=True):
        fmt = (lambda d: d.strftime("%Y%m%d")) if allday else (lambda d: d.strftime("%Y%m%dT%H%M%S"))
        kind = ";VALUE=DATE" if allday else ""
        ev.append("\r\n".join(_fold(x) for x in [
            "BEGIN:VEVENT", f"UID:{uid_}@{host}", f"DTSTAMP:{stamp}", f"DTSTART{kind}:{fmt(start)}",
            f"DTEND{kind}:{fmt(end)}", f"SUMMARY:{_esc(summary)}", f"DESCRIPTION:{_esc(desc)}",
            *([f"URL:{base_url}/"] if base_url else []), "END:VEVENT"]))

    for t in _q("""SELECT t.id, t.title, t.due_date, t.status, p.name AS project_name, p.is_general FROM tasks t
                   JOIN projects p ON p.id=t.project_id
                   WHERE t.assignee_id=? AND t.status!='done' AND p.status='active' AND t.due_date IS NOT NULL""", (uid,)):
        d = date.fromisoformat(t["due_date"])
        proj = "" if t["is_general"] else f" · {t['project_name']}"
        event(f"task-{t['id']}", f"#{t['id']} {t['title']}{proj}", d, d + timedelta(days=1),
              f"Status: {t['status'].replace('_', ' ')}. Update it in Blink: done {t['id']}")
    for r in _q("SELECT id, text, remind_at FROM reminders WHERE user_id=? AND done=0", (uid,)):
        s = datetime.fromisoformat(r["remind_at"])
        event(f"reminder-{r['id']}", f"⏰ {r['text']}", s, s + timedelta(minutes=15), "Reminder", allday=False)
    for p in _q("""SELECT DISTINCT p.id, p.name, p.start_date FROM projects p WHERE p.is_general=0 AND p.status='active'
                   AND p.start_date >= date('now','-7 days') AND (
                     EXISTS (SELECT 1 FROM tasks t WHERE t.project_id=p.id AND t.assignee_id=?)
                     OR EXISTS (SELECT 1 FROM project_team pt WHERE pt.project_id=p.id AND pt.user_id=?))""", (uid, uid)):
        d = date.fromisoformat(p["start_date"])
        event(f"project-{p['id']}", f"🚀 {p['name']} starts", d, d + timedelta(days=1), "Project start")
    head = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Task Manager//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
            _fold(f"X-WR-CALNAME:Tasks – {_esc(name)}"), "X-PUBLISHED-TTL:PT1H", "REFRESH-INTERVAL;VALUE=DURATION:PT1H"]
    return "\r\n".join(head + ev + ["END:VCALENDAR"]) + "\r\n"
