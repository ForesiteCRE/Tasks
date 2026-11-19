"""A little fun: today's progress ring, streaks, badges, the Team wins feed and 👏 high-fives.

No points leaderboard on purpose — streaks and badges reward consistency and the behaviours we want; wins and
high-fives are about recognising each other, not ranking.
"""
import logging
from datetime import date, datetime, timedelta

from . import automations
from .db import get_setting, rows, set_setting, tx

log = logging.getLogger("fun")

BADGES = {
    "early_bird":   ("⏰", "Early Bird", "Finished 10 tasks before their due date"),
    "closer":       ("📊", "Closer", "Worked on a month-end close that was fully done by the 10th"),
    "fully_insured": ("🛡", "Fully Insured", "Every vendor at your properties has a valid COI naming the property"),
    "unblocker":    ("🔓", "Unblocker", "Finished 5 tasks that had been blocked"),
    "vendor_scout": ("⭐", "Vendor Scout", "Rated 10 vendors"),
    "same_day":     ("⚡", "Same-Day", "Finished 5 tasks the same day they were created"),
}
STREAK_MILESTONES = (5, 10, 20, 40, 60, 100)


def init():
    with tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS wins(
            id INTEGER PRIMARY KEY, user_id INTEGER, kind TEXT NOT NULL, ref_id INTEGER, title TEXT NOT NULL, detail TEXT,
            at TEXT NOT NULL DEFAULT (datetime('now')))""")
        c.execute("CREATE INDEX IF NOT EXISTS wins_at ON wins(at)")
        c.execute("""CREATE TABLE IF NOT EXISTS win_kudos(win_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            at TEXT NOT NULL DEFAULT (datetime('now')), PRIMARY KEY(win_id, user_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS user_badges(user_id INTEGER NOT NULL, badge TEXT NOT NULL,
            earned_at TEXT NOT NULL DEFAULT (datetime('now')), PRIMARY KEY(user_id, badge))""")
        c.execute("""CREATE TABLE IF NOT EXISTS user_days(user_id INTEGER NOT NULL, day TEXT NOT NULL, overdue INTEGER NOT NULL,
            done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id, day))""")


# ---------- wins (called from apply_update) ----------

def on_done(task_id: int, actor_id):
    """A task was just finished → a win for whoever did it (or the assignee); project wins when it was the last one."""
    with tx() as c:
        t = c.execute("""SELECT t.id, t.title, t.assignee_id, t.project_id, p.name AS project, p.status AS pstatus, p.is_general
                         FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=?""", (task_id,)).fetchone()
        if not t:
            return
        who = t["assignee_id"] or actor_id
        if c.execute("SELECT 1 FROM wins WHERE kind='task' AND ref_id=? AND at >= datetime('now','-1 day')", (task_id,)).fetchone():
            return   # unticked and re-ticked: no duplicate win
        c.execute("INSERT INTO wins(user_id, kind, ref_id, title, detail) VALUES(?,?,?,?,?)",
                  (who, "task", task_id, t["title"], None if t["is_general"] else t["project"]))
        if t["pstatus"] == "done" and not t["is_general"] and not c.execute(
                "SELECT 1 FROM wins WHERE kind='project' AND ref_id=?", (t["project_id"],)).fetchone():
            c.execute("INSERT INTO wins(user_id, kind, ref_id, title, detail) VALUES(?,?,?,?,?)",
                      (who, "project", t["project_id"], t["project"], "Project complete"))
    try:
        check_badges(who)
    except Exception:
        log.exception("badge check failed")


def feed(limit: int = 30, me: int | None = None) -> list[dict]:
    with tx() as c:
        ws = rows(c.execute("""SELECT w.*, COALESCE(u.name,'Someone') AS who,
                                      (SELECT COUNT(*) FROM win_kudos k WHERE k.win_id=w.id) AS kudos,
                                      (SELECT GROUP_CONCAT(ku.name, ', ') FROM win_kudos k JOIN users ku ON ku.id=k.user_id WHERE k.win_id=w.id) AS kudos_from,
                                      EXISTS(SELECT 1 FROM win_kudos k WHERE k.win_id=w.id AND k.user_id=?) AS mine_kudo
                               FROM wins w LEFT JOIN users u ON u.id=w.user_id
                               WHERE w.at >= datetime('now','-7 days') ORDER BY (w.kind='task'), w.at DESC, w.id DESC LIMIT ?""", (me, limit)))
    for w in ws:
        if w["kind"] == "badge":
            ico, name, _ = BADGES.get(w["title"], ("🏅", w["title"], ""))
            w["title"], w["icon"] = f"earned {name}", ico
        else:
            w["icon"] = {"task": "✅", "project": "🎉", "streak": "🔥", "shoutout": "🏆"}.get(w["kind"], "✅")
    return ws


def kudo(win_id: int, user_id: int) -> dict:
    with tx() as c:
        w = c.execute("""SELECT w.*, u.blink_user_id, u.name AS owner FROM wins w LEFT JOIN users u ON u.id=w.user_id
                         WHERE w.id=?""", (win_id,)).fetchone()
        if not w:
            raise ValueError("Not found.")
        if w["user_id"] == user_id:
            raise ValueError("You can't high-five yourself 🙂")
        new = c.execute("INSERT OR IGNORE INTO win_kudos(win_id, user_id) VALUES(?,?)", (win_id, user_id)).rowcount
        me = c.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone()
        n = c.execute("SELECT COUNT(*) FROM win_kudos WHERE win_id=?", (win_id,)).fetchone()[0]
    if new and w["blink_user_id"]:
        what = {"task": f"finishing *{w['title']}*", "project": f"completing *{w['title']}*", "streak": "your streak",
                "badge": f"earning {BADGES.get(w['title'], ('', w['title'], ''))[1]}"}.get(w["kind"], f"*{w['title']}*")
        automations._send([w["blink_user_id"]], f"👏 **{me['name'] if me else 'Someone'} gave you a high-five** for {what}!", cat="wins")
    return {"kudos": n}


# ---------- today's ring & streaks ----------

def today_progress(user_id: int, today: date | None = None) -> dict:
    """Today's list = my tasks due today or earlier that are still open + the ones I finished today that were due by today."""
    t0 = (today or date.today()).isoformat()
    with tx() as c:
        open_ = c.execute("""SELECT COUNT(*) FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.assignee_id=? AND t.status!='done'
                             AND p.status='active' AND t.due_date IS NOT NULL AND t.due_date<=?""", (user_id, t0)).fetchone()[0]
        done_due = c.execute("""SELECT COUNT(*) FROM tasks WHERE assignee_id=? AND status='done' AND substr(completed_at,1,10)=?
                                AND due_date IS NOT NULL AND due_date<=?""", (user_id, t0, t0)).fetchone()[0]
        done_today = c.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id=? AND status='done' AND substr(completed_at,1,10)=?",
                               (user_id, t0)).fetchone()[0]
    total = open_ + done_due
    return {"done": done_due, "total": total, "done_today": done_today, "open": open_,
            "pct": round(100 * done_due / total) if total else (100 if done_today else 0), "cleared": total > 0 and open_ == 0}


def _overdue_now(c, user_id: int, t0: str) -> int:
    return c.execute("""SELECT COUNT(*) FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.assignee_id=? AND t.status!='done'
                        AND p.status='active' AND t.due_date IS NOT NULL AND t.due_date<?""", (user_id, t0)).fetchone()[0]


def snapshot(today: date | None = None):
    """Every ~10 min: remember each person's overdue count for today (the last value of the day is what counts)."""
    now = datetime.now()
    if (get_setting("fun.snap") or "") >= now.strftime("%Y-%m-%dT%H:%M")[:15] and not today:
        return
    set_setting("fun.snap", now.strftime("%Y-%m-%dT%H:%M")[:15])
    t0 = (today or date.today()).isoformat()
    with tx() as c:
        users = [r[0] for r in c.execute("SELECT id FROM users")]
        for uid in users:
            od = _overdue_now(c, uid, t0)
            dn = c.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id=? AND status='done' AND substr(completed_at,1,10)=?",
                           (uid, t0)).fetchone()[0]
            c.execute("""INSERT INTO user_days(user_id, day, overdue, done) VALUES(?,?,?,?)
                         ON CONFLICT(user_id, day) DO UPDATE SET overdue=excluded.overdue, done=excluded.done""", (uid, t0, od, dn))
    for uid in users:
        _streak_milestone(uid)


def streak(user_id: int, today: date | None = None) -> int:
    """Workdays in a row (Mon–Fri) with nothing overdue, counting today if nothing is overdue right now."""
    d = today or date.today()
    with tx() as c:
        days = {r["day"]: r["overdue"] for r in c.execute("SELECT day, overdue FROM user_days WHERE user_id=? AND day>=?",
                                                          (user_id, (d - timedelta(days=400)).isoformat()))}
        has_open = c.execute("SELECT 1 FROM tasks WHERE assignee_id=? LIMIT 1", (user_id,)).fetchone()
        now_over = _overdue_now(c, user_id, d.isoformat())
    if not has_open:
        return 0
    n = 0
    if d.weekday() < 5:
        if now_over:
            return 0
        n = 1
    cur = d - timedelta(days=1)
    for _ in range(400):
        if cur.weekday() < 5:
            k = cur.isoformat()
            if k not in days or days[k] > 0:
                break
            n += 1
        cur -= timedelta(days=1)
    return n


def _streak_milestone(uid: int):
    s = streak(uid)
    if s in STREAK_MILESTONES:
        with tx() as c:
            if c.execute("SELECT 1 FROM wins WHERE kind='streak' AND user_id=? AND ref_id=? AND at >= datetime('now','-3 days')",
                         (uid, s)).fetchone():
                return
            c.execute("INSERT INTO wins(user_id, kind, ref_id, title, detail) VALUES(?,?,?,?,?)",
                      (uid, "streak", s, f"{s}-day streak — nothing overdue", None))


# ---------- badges ----------

def _count(c, sql, args) -> int:
    return c.execute(sql, args).fetchone()[0]


def _earned(c, uid: int) -> set:
    return {r[0] for r in c.execute("SELECT badge FROM user_badges WHERE user_id=?", (uid,))}


def check_badges(uid) -> list[str]:
    """Award any newly earned badges (a win in the feed + a Blink message). Returns the new ones."""
    if not uid:
        return []
    new = []
    with tx() as c:
        have = _earned(c, uid)
        tests = {
            "early_bird": lambda: _count(c, """SELECT COUNT(*) FROM tasks WHERE assignee_id=? AND status='done' AND due_date IS NOT NULL
                                               AND substr(completed_at,1,10) < due_date""", (uid,)) >= 10,
            "same_day": lambda: _count(c, """SELECT COUNT(*) FROM tasks WHERE assignee_id=? AND status='done' AND created_at IS NOT NULL
                                             AND substr(completed_at,1,10)=substr(datetime(created_at,'localtime'),1,10)""", (uid,)) >= 5,
            "vendor_scout": lambda: _count(c, "SELECT COUNT(DISTINCT vendor_id) FROM vendor_reviews WHERE user_id=?", (uid,)) >= 10,
            "unblocker": lambda: _count(c, """SELECT COUNT(DISTINCT t.id) FROM tasks t WHERE t.assignee_id=? AND t.status='done'
                                              AND EXISTS(SELECT 1 FROM task_updates x WHERE x.task_id=t.id AND x.status='blocked')""", (uid,)) >= 5,
            "closer": lambda: _count(c, """SELECT COUNT(*) FROM projects p JOIN templates tp ON tp.id=p.template_id
                                           WHERE lower(tp.name) LIKE '%close%' AND p.status='done'
                                             AND EXISTS(SELECT 1 FROM tasks t WHERE t.project_id=p.id AND t.assignee_id=?)
                                             AND (SELECT MAX(substr(completed_at,1,10)) FROM tasks t WHERE t.project_id=p.id)
                                                 <= substr(p.start_date,1,8) || '10'""", (uid,)) >= 1,
            "fully_insured": lambda: _fully_insured(c, uid),
        }
        for b, test in tests.items():
            if b in have:
                continue
            try:
                ok = test()
            except Exception:
                log.exception("badge %s", b)
                ok = False
            if ok:
                c.execute("INSERT OR IGNORE INTO user_badges(user_id, badge) VALUES(?,?)", (uid, b))
                c.execute("INSERT INTO wins(user_id, kind, ref_id, title, detail) VALUES(?,?,?,?,?)", (uid, "badge", None, b, BADGES[b][2]))
                new.append(b)
        u = c.execute("SELECT blink_user_id FROM users WHERE id=?", (uid,)).fetchone()
    for b in new:
        if u and u["blink_user_id"]:
            ico, name, why = BADGES[b]
            automations._send([u["blink_user_id"]], f"🏅 **You earned a badge: {ico} {name}!**\n_{why}_", cat="wins")
    return new


def _fully_insured(c, uid: int) -> bool:
    from .vendors import coi_status
    props = [r[0] for r in c.execute("""SELECT pt.property_id FROM property_team pt JOIN project_roles r ON r.id=pt.role_id
                                        WHERE pt.user_id=? AND r.name='Property Manager'""", (uid,))]
    pairs = [(p, v) for p in props for (v,) in c.execute("""SELECT DISTINCT vendor_id FROM property_vendors WHERE property_id=?
                                                             AND status='current' AND trade NOT LIKE 'Utilities%'""", (p,))]
    return bool(pairs) and all(coi_status(c, p, v)["state"] == "ok" for p, v in pairs)


def run_daily():
    """Scheduler: snapshots for streaks + badge checks that don't follow a single task (COIs, closes)."""
    snapshot()
    if get_setting("fun.badges_day") == date.today().isoformat():
        return
    set_setting("fun.badges_day", date.today().isoformat())
    with tx() as c:
        users = [r[0] for r in c.execute("SELECT id FROM users")]
    for uid in users:
        check_badges(uid)


def badges_for(uid: int) -> list[dict]:
    with tx() as c:
        got = {r["badge"]: r["earned_at"] for r in c.execute("SELECT badge, earned_at FROM user_badges WHERE user_id=?", (uid,))}
    return [{"key": k, "icon": i, "name": n, "how": h, "earned_at": got.get(k)} for k, (i, n, h) in BADGES.items()]


def me(uid: int) -> dict:
    return {"today": today_progress(uid), "streak": streak(uid), "badges": badges_for(uid)}


def team_today() -> list[dict]:
    with tx() as c:
        users = rows(c.execute("""SELECT DISTINCT u.id, u.name FROM users u JOIN tasks t ON t.assignee_id=u.id
                                  JOIN projects p ON p.id=t.project_id WHERE p.status='active' ORDER BY lower(u.name)"""))
    out = []
    for u in users:
        tp = today_progress(u["id"])
        if not tp["total"] and not tp["done_today"]:
            continue
        with tx() as c:
            nb = c.execute("SELECT COUNT(*) FROM user_badges WHERE user_id=?", (u["id"],)).fetchone()[0]
        out.append({**u, **tp, "streak": streak(u["id"]), "badges": nb})
    return out
