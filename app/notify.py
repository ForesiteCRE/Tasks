"""Per-person Blink notification settings: how often, and what.

Every Blink message the app sends (except chat replies and a person's own "remind me" reminders) goes through
`deliver()` with a category. Each person chooses, per category:
  instant  – send right away
  summary  – save it for their summary message
  off      – don't send it
and how often the summary comes: once a day at a time they pick (default 6pm), twice a day (8am + that time),
or every message instantly / no summary at all.

Default (what the team asked for): one daily summary at 6pm, plus @mentions instantly.
"""
import json
import logging
from datetime import date, datetime

from . import blink
from .db import rows, tx

log = logging.getLogger("notify")

# id: (label, help, default, who sees it: all | mgr)
CATEGORIES = {
    "recap":      ("Daily recap", "what's due, overdue and done today — the heart of your summary", "summary", "all"),
    "mentions":   ("When someone @mentions me", "a teammate tags you in a comment", "instant", "all"),
    "comments":   ("Comments on my tasks", "someone else comments on a task assigned to you", "summary", "all"),
    "assigned":   ("New tasks & projects assigned to me", "including tasks added for you in Blink", "summary", "all"),
    "your_turn":  ("It's my turn", "a task you were waiting on is done, so yours can start", "summary", "all"),
    "checkin":    ("Task check-in", "a daily “how are these going?” list of your due and overdue tasks", "off", "all"),
    "reporting":  ("Owner reports sent at my properties", "Accounting finished and sent the monthly owner report", "summary", "all"),
    "critical":   ("Property critical dates & insurance", "tax protest, renewals, owner reports, COI/insurance issues at your properties", "summary", "all"),
    "approvals":  ("Owner approvals", "an owner approved or asked for changes on a project you requested", "summary", "all"),
    "emails":     ("Emails drafted for me", "COI and insurance follow-ups waiting in Emails to send", "summary", "all"),
    "team":       ("Team changes", "you were added to or changed on a property or project team", "summary", "all"),
    "wins":       ("High-fives & badges", "someone high-fives your win, or you earn a badge", "summary", "all"),
    "week_ahead": ("Monday week-ahead", "what's coming up for you this week", "summary", "all"),
    "manager":    ("Team alerts", "tasks blocked, overdue, projects finished, department escalations", "summary", "mgr"),
    "weekly":     ("Weekly team summary", "Monday overview of overdue, blocked and due-this-week across the team", "summary", "mgr"),
}
MODES = ("instant", "summary", "off")
FREQUENCIES = {"daily": "Once a day", "twice": "Twice a day (8am + your time)", "instant": "No summary — send everything as it happens",
               "none": "No summary — only what's set to Instant"}
DEFAULT = {"frequency": "daily", "time": "18:00", "weekdays_only": False, "cats": {k: v[2] for k, v in CATEGORIES.items()}}


def init():
    with tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS notify_queue(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, category TEXT NOT NULL,
                     text TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        c.execute("""CREATE TABLE IF NOT EXISTS user_prefs(user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT,
                     PRIMARY KEY(user_id, key))""")


def _get(c, uid: int, key: str):
    r = c.execute("SELECT value FROM user_prefs WHERE user_id=? AND key=?", (uid, key)).fetchone()
    return r[0] if r else None


def _put(c, uid: int, key: str, value: str):
    c.execute("""INSERT INTO user_prefs(user_id, key, value) VALUES(?,?,?)
                 ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value""", (uid, key, value))


def prefs(uid: int) -> dict:
    with tx() as c:
        raw = _get(c, uid, "notify")
        legacy = None if raw else c.execute("SELECT daily_digest, week_ahead, weekly_summary FROM users WHERE id=?", (uid,)).fetchone()
    p = json.loads(json.dumps(DEFAULT))
    if raw:
        saved = json.loads(raw)
        p.update({k: saved[k] for k in ("frequency", "time", "weekdays_only") if k in saved})
        p["cats"].update({k: v for k, v in (saved.get("cats") or {}).items() if k in CATEGORIES and v in MODES})
    elif legacy:   # carry over the old on/off switches from My settings
        if legacy["daily_digest"] == 0:
            p["cats"]["recap"] = "off"
        if legacy["week_ahead"] == 0:
            p["cats"]["week_ahead"] = "off"
        if legacy["weekly_summary"] == 0:
            p["cats"]["weekly"] = "off"
    return p


def save(uid: int, new: dict) -> dict:
    p = prefs(uid)
    if new.get("frequency") in FREQUENCIES:
        p["frequency"] = new["frequency"]
    t = str(new.get("time") or "")
    if len(t) == 5 and t[2] == ":" and t[:2].isdigit() and t[3:].isdigit() and int(t[:2]) < 24 and int(t[3:]) < 60:
        p["time"] = t
    if "weekdays_only" in new:
        p["weekdays_only"] = bool(new["weekdays_only"])
    for k, v in (new.get("cats") or {}).items():
        if k in CATEGORIES and v in MODES:
            p["cats"][k] = "summary" if (k == "recap" and v == "instant") else v
    with tx() as c:
        _put(c, uid, "notify", json.dumps(p))
        # keep the old columns in step for anything that still reads them
        c.execute("UPDATE users SET daily_digest=?, week_ahead=?, weekly_summary=? WHERE id=?",
                  (int(p["cats"]["recap"] != "off"), int(p["cats"]["week_ahead"] != "off"), int(p["cats"]["weekly"] != "off"), uid))
    return p


def mode(uid: int, cat: str) -> str:
    p = prefs(uid)
    m = p["cats"].get(cat, "instant")
    if m == "summary" and p["frequency"] == "instant":
        return "instant"
    if m == "summary" and p["frequency"] == "none":
        return "off"
    return m


def deliver(uid: int | None, cat: str, text: str, blink_id: str | None = None) -> str:
    """Send now, save for the summary, or skip — per that person's settings. Returns what happened."""
    if uid is None and blink_id:
        with tx() as c:
            r = c.execute("SELECT id FROM users WHERE blink_user_id=?", (blink_id,)).fetchone()
        uid = r["id"] if r else None
    if uid is None:   # not a known person: just send
        if blink_id:
            blink.send_dm(blink_id, text)
        return "instant"
    if not blink_id:
        with tx() as c:
            r = c.execute("SELECT blink_user_id FROM users WHERE id=?", (uid,)).fetchone()
        blink_id = r["blink_user_id"] if r else None
    if not blink_id:
        return "off"
    m = mode(uid, cat)
    if m == "instant":
        blink.send_dm(blink_id, text)
    elif m == "summary":
        with tx() as c:
            c.execute("INSERT INTO notify_queue(user_id, category, text) VALUES(?,?,?)", (uid, cat, text))
    return m


def deliver_blink(blink_ids, cat: str, text: str):
    for b in dict.fromkeys(x for x in blink_ids if x):
        try:
            deliver(None, cat, text, blink_id=b)
        except Exception:
            log.exception("notification to %s failed", b)


# ---------- the summary message ----------

def _slots(p: dict) -> list[str]:
    if p["frequency"] == "twice":
        return sorted({"08:00", p["time"]})
    if p["frequency"] == "daily":
        return [p["time"]]
    return []


def build_summary(uid: int, include_recap: bool = True) -> str:
    from . import dashboard
    p = prefs(uid)
    parts = []
    if include_recap and p["cats"]["recap"] != "off":
        r = dashboard.build_digest(uid)
        if r:
            parts.append(r)
    with tx() as c:
        q = rows(c.execute("SELECT category, text FROM notify_queue WHERE user_id=? ORDER BY id", (uid,)))
    if q:
        parts.append("📬 **Since your last summary**")
        for cat in CATEGORIES:
            items = [x["text"] for x in q if x["category"] == cat]
            if items:
                label = CATEGORIES[cat][0]
                parts.append(f"**{label}** ({len(items)})\n" + "\n".join("• " + t.replace("\n", "\n   ") for t in items[:12])
                             + (f"\n…and {len(items) - 12} more" if len(items) > 12 else ""))
    return "\n\n".join(parts)


def run_summaries(now: datetime | None = None, force_user: int | None = None) -> int:
    """Every scheduler tick: anyone whose summary time has passed (and hasn't had it yet) gets it."""
    now = now or datetime.now()
    today = now.date().isoformat()
    with tx() as c:
        people = rows(c.execute("SELECT id, blink_user_id FROM users WHERE blink_user_id IS NOT NULL"
                                + (" AND id=?" if force_user else ""), (force_user,) if force_user else ()))
    sent = 0
    for u in people:
        p = prefs(u["id"])
        due = []
        for slot in _slots(p):
            if force_user or (now.strftime("%H:%M") >= slot and not (p["weekdays_only"] and now.weekday() >= 5)):
                due.append(slot)
        if not due:
            continue
        with tx() as c:
            done = json.loads(_get(c, u["id"], "notify.sent") or "{}")
        todo = [s for s in due if force_user or done.get(s) != today]
        if not todo:
            continue
        evening = todo[-1] == _slots(p)[-1]   # the recap goes in the last summary of the day
        text = build_summary(u["id"], include_recap=evening)
        if text:
            try:
                blink.send_dm(u["blink_user_id"], text)
                sent += 1
            except Exception:
                log.exception("summary for %s failed", u["id"])
                continue
        with tx() as c:
            c.execute("DELETE FROM notify_queue WHERE user_id=?", (u["id"],))
            if not force_user:
                done.update({s: today for s in todo})
                _put(c, u["id"], "notify.sent", json.dumps(done))
    return sent


def settings_for(uid: int, is_manager: bool) -> dict:
    p = prefs(uid)
    cats = [{"id": k, "label": v[0], "help": v[1], "mode": p["cats"][k], "default": v[2]}
            for k, v in CATEGORIES.items() if v[3] == "all" or is_manager]
    with tx() as c:
        queued = c.execute("SELECT COUNT(*) FROM notify_queue WHERE user_id=?", (uid,)).fetchone()[0]
    return {"frequency": p["frequency"], "time": p["time"], "weekdays_only": p["weekdays_only"], "categories": cats,
            "frequencies": FREQUENCIES, "queued": queued}
