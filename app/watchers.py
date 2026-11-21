"""Task watchers (like ClickUp's "watchers").

Whoever creates a task for someone else watches it automatically — Donna adds a task for Dez, and Donna hears when
Dez finishes it. Anyone can also click 👀 Watch on a task (or add a teammate). Watchers get a Blink message when the
task is done, blocked, or someone comments (Blink updates setting: "Tasks I'm watching").
"""
from .db import rows, tx


def init():
    with tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS task_watchers(task_id INTEGER NOT NULL, user_id INTEGER NOT NULL, added_by INTEGER,
                     created_at TEXT NOT NULL DEFAULT (datetime('now')), PRIMARY KEY(task_id, user_id))""")


def add(task_id: int, user_id: int, added_by=None):
    if not user_id:
        return
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO task_watchers(task_id, user_id, added_by) VALUES(?,?,?)", (task_id, user_id, added_by))


def remove(task_id: int, user_id: int):
    with tx() as c:
        c.execute("DELETE FROM task_watchers WHERE task_id=? AND user_id=?", (task_id, user_id))


def auto_watch(task_id: int, creator_id, assignee_id):
    """The creator watches a task they made for someone else."""
    if creator_id and assignee_id and creator_id != assignee_id:
        add(task_id, creator_id, creator_id)


def for_task(task_id: int) -> list[dict]:
    with tx() as c:
        return rows(c.execute("""SELECT w.user_id, u.name FROM task_watchers w JOIN users u ON u.id=w.user_id
                                 WHERE w.task_id=? ORDER BY w.created_at""", (task_id,)))


def _ids(task_id: int) -> list[int]:
    with tx() as c:
        return [r[0] for r in c.execute("SELECT user_id FROM task_watchers WHERE task_id=?", (task_id,))]


def notify(task_id: int, text: str, skip=()):
    from . import notify as nt
    for uid in _ids(task_id):
        if uid not in skip:
            nt.deliver(uid, "watching", text)


def on_status(t: dict, new: str, actor_id, who: str, note: str | None = None):
    from .auth import PUBLIC_URL
    if new not in ("done", "blocked"):
        return
    link = f"{PUBLIC_URL}/#task-{t['id']}"
    by = f" by {who}" if who and who != "someone" else ""
    text = (f"✅ **Done:** #{t['id']} {t['title']}{by}" if new == "done" else
            f"⛔ **Blocked:** #{t['id']} {t['title']}{by}" + (f"\n_{note}_" if note else "")) + f"\n{link}"
    notify(t["id"], text, skip={actor_id, t.get("assignee_id")} if new == "blocked" else {actor_id})


def mine(uid: int) -> dict:
    """Home widget: what I'm watching — still open, and finished in the last 7 days."""
    with tx() as c:
        q = """SELECT t.id, t.title, t.status, t.due_date, t.assignee_id, u.name AS assignee_name, p.name AS project_name, p.is_general,
                      pr.name AS property_name,
                      (SELECT MAX(x.created_at) FROM task_updates x WHERE x.task_id=t.id AND x.status='done') AS done_at
               FROM task_watchers w JOIN tasks t ON t.id=w.task_id JOIN projects p ON p.id=t.project_id
               LEFT JOIN users u ON u.id=t.assignee_id LEFT JOIN properties pr ON pr.id=COALESCE(p.property_id, t.property_id)
               WHERE w.user_id=? AND p.archived_at IS NULL"""
        open_ = rows(c.execute(q + " AND t.status!='done' ORDER BY t.due_date IS NULL, t.due_date", (uid,)))
        done = rows(c.execute(q + " AND t.status='done' ORDER BY done_at DESC LIMIT 30", (uid,)))
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    return {"open": open_, "done": [d for d in done if (d["done_at"] or "") >= cutoff]}
