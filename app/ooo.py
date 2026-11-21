"""Out-of-office coverage.

Someone says "I'm out Dec 22 – Jan 2, Jackie covers". On the first day:
  • their open tasks due before they're back (plus anything overdue) move to the person covering — or, at properties
    where they're the Property Manager, to that property's Backup Property Manager (if they chose that);
  • utility bills assigned to them move to the person covering;
  • the people covering get one Blink message listing what they picked up; managers hear who's out and who covers;
  • the away person's Blink messages are held (nothing pings them on vacation).
Each day while they're out, new tasks given to them that are due before they're back move over too.
The day after they're back, everything still open and still with the person covering goes back to them, they get a
"welcome back" message with what was handed back and what was finished, and the held messages arrive.
"""
import logging
from datetime import date, timedelta

from .db import rows, tx

log = logging.getLogger("ooo")
GRACE_DAYS = 2   # also move tasks due within this many days after they're back


def init():
    with tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS ooo(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                     backup_id INTEGER, use_property_backups INTEGER NOT NULL DEFAULT 1, note TEXT,
                     status TEXT NOT NULL DEFAULT 'scheduled', created_by INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now')),
                     started_at TEXT, ended_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ooo_moves(ooo_id INTEGER NOT NULL, kind TEXT NOT NULL, item_id INTEGER NOT NULL,
                     from_user INTEGER, to_user INTEGER, returned INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(ooo_id, kind, item_id))""")


def _name(c, uid) -> str:
    r = c.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone() if uid else None
    return r["name"] if r else "someone"


def _first(n: str) -> str:
    return (n or "").split(" (")[0].split()[0] if n else ""


def _fmt(d: str) -> str:
    x = date.fromisoformat(d)
    return f"{x.strftime('%a %b')} {x.day}"


def get(oid: int) -> dict | None:
    with tx() as c:
        r = c.execute("""SELECT o.*, u.name AS user_name, b.name AS backup_name FROM ooo o JOIN users u ON u.id=o.user_id
                         LEFT JOIN users b ON b.id=o.backup_id WHERE o.id=?""", (oid,)).fetchone()
    return dict(r) if r else None


def listing(today: date | None = None) -> list[dict]:
    """Who's out now or coming up (and anyone back in the last 3 days)."""
    today = today or date.today()
    with tx() as c:
        out = rows(c.execute("""SELECT o.*, u.name AS user_name, b.name AS backup_name FROM ooo o JOIN users u ON u.id=o.user_id
                                LEFT JOIN users b ON b.id=o.backup_id
                                WHERE o.status IN ('scheduled','active') OR (o.status='ended' AND o.end>=?)
                                ORDER BY o.start""", ((today - timedelta(days=3)).isoformat(),)))
        for o in out:
            o["moved"] = c.execute("SELECT COUNT(*) FROM ooo_moves WHERE ooo_id=?", (o["id"],)).fetchone()[0]
    return out


def is_out(uid, today: date | None = None) -> bool:
    if not uid:
        return False
    with tx() as c:
        return bool(c.execute("SELECT 1 FROM ooo WHERE user_id=? AND status='active'", (uid,)).fetchone())


def out_map(today: date | None = None) -> dict:
    """user_id → {until, backup} for everyone out right now (for badges and warnings in the app)."""
    with tx() as c:
        return {r["user_id"]: {"until": r["end"], "backup": r["backup_name"]} for r in c.execute(
            """SELECT o.user_id, o.end, b.name AS backup_name FROM ooo o LEFT JOIN users b ON b.id=o.backup_id
               WHERE o.status='active'""")}


# ---------- planning who gets what ----------

def _plan(c, uid: int, end: str, backup_id, use_prop: bool, already: set | None = None) -> list[dict]:
    cutoff = (date.fromisoformat(end) + timedelta(days=GRACE_DAYS)).isoformat()
    tasks = rows(c.execute(
        """SELECT t.id, t.title, t.due_date, COALESCE(p.property_id, t.property_id) AS property_id, pr.name AS property_name,
                  p.name AS project_name, p.is_general
           FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN properties pr ON pr.id=COALESCE(p.property_id, t.property_id)
           WHERE t.assignee_id=? AND t.status!='done' AND t.due_date IS NOT NULL AND t.due_date<=?
             AND p.status NOT IN ('archived') AND p.archived_at IS NULL
           ORDER BY t.due_date""", (uid, cutoff)))
    out_now = {r[0] for r in c.execute("SELECT user_id FROM ooo WHERE status='active'")}
    plan = []
    for t in tasks:
        if already and ("task", t["id"]) in already:
            continue
        to = backup_id
        if use_prop and t["property_id"]:
            roles = {r["name"]: r["user_id"] for r in c.execute(
                """SELECT r.name, pt.user_id FROM property_team pt JOIN project_roles r ON r.id=pt.role_id
                   WHERE pt.property_id=? AND r.name IN ('Property Manager','Backup Property Manager')""", (t["property_id"],))}
            bpm = roles.get("Backup Property Manager")
            if roles.get("Property Manager") == uid and bpm and bpm != uid and bpm not in out_now:
                to = bpm
        if to and to != uid:
            plan.append({"kind": "task", "id": t["id"], "to": to, "title": t["title"], "due_date": t["due_date"],
                         "where": t["property_name"] or ("" if t["is_general"] else t["project_name"])})
    try:
        accts = rows(c.execute("SELECT id, provider, account_no FROM utility_accounts WHERE assignee_id=? AND active=1", (uid,)))
    except Exception:
        accts = []
    for a in accts:
        if already and ("utility", a["id"]) in already:
            continue
        if backup_id and backup_id != uid:
            plan.append({"kind": "utility", "id": a["id"], "to": backup_id, "title": f"{a['provider'] or 'Utility'} #{a['account_no'] or ''}"})
    return plan


def preview(uid: int, end: str, backup_id, use_prop: bool) -> dict:
    with tx() as c:
        plan = _plan(c, uid, end, backup_id, use_prop)
        names = {}
        for p in plan:
            names.setdefault(p["to"], _name(c, p["to"]))
    by = {}
    for p in plan:
        k = names[p["to"]]
        by.setdefault(k, {"name": k, "tasks": 0, "utilities": 0})
        by[k]["tasks" if p["kind"] == "task" else "utilities"] += 1
    return {"tasks": sum(p["kind"] == "task" for p in plan), "utilities": sum(p["kind"] == "utility" for p in plan),
            "by_person": list(by.values()), "items": [p for p in plan if p["kind"] == "task"][:40]}


# ---------- create / change ----------

def create(d: dict, by_user: int, today: date | None = None) -> int:
    today = today or date.today()
    uid = int(d.get("user_id") or by_user)
    start, end = str(d.get("start") or ""), str(d.get("end") or "")
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise ValueError("Pick the first and last day you're out.")
    if e < s:
        raise ValueError("The last day out is before the first day.")
    if e < today:
        raise ValueError("Those dates are in the past.")
    backup = int(d["backup_id"]) if d.get("backup_id") else None
    if backup == uid:
        raise ValueError("Pick someone else to cover.")
    if not backup:
        raise ValueError("Pick who covers for you.")
    with tx() as c:
        clash = c.execute("""SELECT 1 FROM ooo WHERE user_id=? AND status IN ('scheduled','active') AND NOT (end<? OR start>?)""",
                          (uid, start, end)).fetchone()
        if clash:
            raise ValueError("There's already out-of-office time for those dates — change or cancel it instead.")
        oid = c.execute("""INSERT INTO ooo(user_id, start, end, backup_id, use_property_backups, note, created_by)
                           VALUES(?,?,?,?,?,?,?)""", (uid, start, end, backup, 1 if d.get("use_property_backups", True) else 0,
                                                       (d.get("note") or "").strip()[:300] or None, by_user)).lastrowid
    run(today)
    return oid


def update(oid: int, d: dict, today: date | None = None):
    """Change the dates / person covering / note. The first day can only change before it starts."""
    o = get(oid)
    if not o or o["status"] not in ("scheduled", "active"):
        raise ValueError("That out-of-office time is over.")
    sets = {}
    if "end" in d:
        try:
            date.fromisoformat(d["end"])
        except ValueError:
            raise ValueError("Bad date")
        if d["end"] < (o["start"] if "start" not in d else d["start"]):
            raise ValueError("The last day out is before the first day.")
        sets["end"] = d["end"]
    if "start" in d and o["status"] == "scheduled":
        sets["start"] = d["start"]
    if "note" in d:
        sets["note"] = (d["note"] or "").strip()[:300] or None
    if d.get("backup_id") and o["status"] == "scheduled":
        sets["backup_id"] = int(d["backup_id"])
    if sets:
        with tx() as c:
            c.execute(f"UPDATE ooo SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), oid))
    run(today)


def cancel(oid: int, today: date | None = None):
    """Cancel before it starts, or end it early (hands everything back now)."""
    o = get(oid)
    if not o:
        return
    if o["status"] == "scheduled":
        with tx() as c:
            c.execute("UPDATE ooo SET status='cancelled' WHERE id=?", (oid,))
    elif o["status"] == "active":
        _end(o, today or date.today())


# ---------- the daily job ----------

def _move(c, o: dict, plan: list[dict], who: str):
    for p in plan:
        if p["kind"] == "task":
            c.execute("UPDATE tasks SET assignee_id=? WHERE id=? AND assignee_id=?", (p["to"], p["id"], o["user_id"]))
            c.execute("INSERT INTO task_updates(task_id, user_id, source, note) VALUES(?,?,?,?)",
                      (p["id"], None, "web", f"🌴 Covering for {who} (out of office until {_fmt(o['end'])})"))
        else:
            c.execute("UPDATE utility_accounts SET assignee_id=? WHERE id=?", (p["to"], p["id"]))
        c.execute("INSERT OR IGNORE INTO ooo_moves(ooo_id, kind, item_id, from_user, to_user) VALUES(?,?,?,?,?)",
                  (o["id"], p["kind"], p["id"], o["user_id"], p["to"]))


def _tell_coverers(o: dict, plan: list[dict], who: str, first: bool):
    from . import notify
    from .auth import PUBLIC_URL
    by = {}
    for p in plan:
        by.setdefault(p["to"], []).append(p)
    for to, items in by.items():
        ts = [p for p in items if p["kind"] == "task"]
        us = [p for p in items if p["kind"] == "utility"]
        head = (f"🌴 **You're covering for {who}** — out {_fmt(o['start'])} → back {_fmt(o['end'])}" if first
                else f"🌴 More for you while {_first(who)} is out:")
        lines = [f"• {p['title']}" + (f" — {p['where']}" if p.get("where") else "") + (f" (due {_fmt(p['due_date'])})" if p.get("due_date") else "")
                 for p in ts[:15]]
        text = head + (f"\n{o['note']}" if o.get("note") and first else "") + \
            (f"\n**{len(ts)} task{'s' if len(ts) != 1 else ''}:**\n" + "\n".join(lines) + (f"\n…and {len(ts) - 15} more" if len(ts) > 15 else "") if ts else "") + \
            (f"\n**{len(us)} utility bill{'s' if len(us) != 1 else ''}** to send to AppFolio — {PUBLIC_URL}/#view-utilities" if us else "") + \
            ("\nThey go back automatically when they return." if first else "")
        notify.deliver(to, "coverage", text)


def _start(o: dict, today: date):
    from . import notify
    with tx() as c:
        who = _name(c, o["user_id"])
        plan = _plan(c, o["user_id"], o["end"], o["backup_id"], bool(o["use_property_backups"]))
        _move(c, o, plan, who)
        c.execute("UPDATE ooo SET status='active', started_at=datetime('now') WHERE id=?", (o["id"],))
        mgrs = [r[0] for r in c.execute("SELECT id FROM users WHERE role IN ('manager','admin') AND id!=?", (o["user_id"],))]
        backup = _name(c, o["backup_id"])
    _tell_coverers(o, plan, who, True)
    covered = {p["to"] for p in plan} | {o["backup_id"]}
    for m in mgrs:
        if m not in covered:
            notify.deliver(m, "team", f"🌴 {who} is out of office {_fmt(o['start'])} → back {_fmt(o['end'])}. "
                                      f"{_first(backup)} is covering ({len(plan)} item{'s' if len(plan) != 1 else ''} handed over).")


def _sweep(o: dict):
    """While they're out: tasks newly given to them (due before they're back) move over too."""
    with tx() as c:
        done = {(r[0], r[1]) for r in c.execute("SELECT kind, item_id FROM ooo_moves WHERE ooo_id=? AND returned=0", (o["id"],))}
        plan = _plan(c, o["user_id"], o["end"], o["backup_id"], bool(o["use_property_backups"]), already=done)
        if not plan:
            return
        who = _name(c, o["user_id"])
        _move(c, o, plan, who)
    _tell_coverers(o, plan, who, False)


def _end(o: dict, today: date):
    from . import notify
    with tx() as c:
        who = _name(c, o["user_id"])
        moves = rows(c.execute("SELECT * FROM ooo_moves WHERE ooo_id=? AND returned=0", (o["id"],)))
        back, finished, by_cover = [], [], {}
        for m in moves:
            if m["kind"] == "task":
                t = c.execute("SELECT id, title, status, assignee_id, due_date FROM tasks WHERE id=?", (m["item_id"],)).fetchone()
                if not t:
                    continue
                if t["status"] == "done":
                    finished.append(t["title"])
                    by_cover.setdefault(m["to_user"], [0, 0])[1] += 1
                elif t["assignee_id"] == m["to_user"]:
                    c.execute("UPDATE tasks SET assignee_id=? WHERE id=?", (o["user_id"], t["id"]))
                    c.execute("INSERT INTO task_updates(task_id, user_id, source, note) VALUES(?,?,?,?)",
                              (t["id"], None, "web", f"👋 Back to {who} (back from out of office)"))
                    back.append(t)
                    by_cover.setdefault(m["to_user"], [0, 0])[0] += 1
            else:
                c.execute("UPDATE utility_accounts SET assignee_id=? WHERE id=? AND assignee_id=?", (o["user_id"], m["item_id"], m["to_user"]))
            c.execute("UPDATE ooo_moves SET returned=1 WHERE ooo_id=? AND kind=? AND item_id=?", (o["id"], m["kind"], m["item_id"]))
        c.execute("UPDATE ooo SET status='ended', ended_at=datetime('now'), end=MIN(end, ?) WHERE id=?",
                  (max(o["start"], (today - timedelta(days=1)).isoformat()) if today.isoformat() <= o["end"] else o["end"], o["id"]))
        names = {u: _name(c, u) for u in by_cover}
    lines = [f"• {t['title']}" + (f" (due {_fmt(t['due_date'])})" if t["due_date"] else "") for t in back[:15]]
    text = (f"👋 **Welcome back, {_first(who)}!**" +
            (f"\n{len(finished)} task{'s' if len(finished) != 1 else ''} got finished while you were out." if finished else "") +
            (f"\n**{len(back)} handed back to you:**\n" + "\n".join(lines) + (f"\n…and {len(back) - 15} more" if len(back) > 15 else "") if back
             else "\nNothing was left open — you're all caught up."))
    notify.deliver(o["user_id"], "coverage", text)
    notify.flush_held(o["user_id"], header="📬 **Messages held while you were out**")
    for u, (b, f) in by_cover.items():
        notify.deliver(u, "coverage", f"🙌 Thanks for covering for {_first(who)}! {f} finished · {b} handed back to them.")


def run(today: date | None = None) -> int:
    today = today or date.today()
    t = today.isoformat()
    with tx() as c:
        todo = rows(c.execute("SELECT * FROM ooo WHERE status IN ('scheduled','active')"))
    n = 0
    for o in todo:
        try:
            if o["end"] < t:
                if o["status"] == "active":
                    _end(o, today)
                else:
                    with tx() as c:
                        c.execute("UPDATE ooo SET status='ended' WHERE id=?", (o["id"],))
                n += 1
            elif o["start"] <= t:
                if o["status"] == "scheduled":
                    _start(o, today)
                    n += 1
                else:
                    _sweep(o)
        except Exception:
            log.exception("out-of-office %s failed", o["id"])
    return n
