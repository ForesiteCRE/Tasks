"""Subtasks, checklists, comments with @mentions, and dependencies."""
import re

from . import automations
from .db import rows, tx

MENTION_RE = re.compile(r"@([A-Za-z][\w.\-]*)")


# ---------- people handles for @mentions ----------

def handle_for(user: dict) -> str:
    """@handle = email local part (unique), else name without spaces."""
    if user.get("email"):
        return user["email"].split("@")[0].lower()
    return re.sub(r"\W+", "", user["name"]).lower()


def _users() -> list[dict]:
    with tx() as c:
        return rows(c.execute("SELECT id, name, email, blink_user_id FROM users"))


def find_mentions(body: str) -> list[dict]:
    """Match @handle, or a unique @firstname, against People."""
    users = _users()
    by_handle = {handle_for(u): u for u in users}
    first: dict[str, list] = {}
    for u in users:
        first.setdefault(u["name"].split()[0].lower(), []).append(u)
    found = {}
    for raw in MENTION_RE.findall(body):
        key = raw.lower().rstrip(".-")
        u = by_handle.get(key) or (first[key][0] if len(first.get(key, [])) == 1 else None)
        if u:
            found[u["id"]] = u
    return list(found.values())


# ---------- task detail ----------

def task_detail(task_id: int) -> dict | None:
    with tx() as c:
        t = c.execute(
            """SELECT t.*, p.name AS project_name, u.name AS assignee_name FROM tasks t
               JOIN projects p ON p.id=t.project_id LEFT JOIN users u ON u.id=t.assignee_id WHERE t.id=?""",
            (task_id,)).fetchone()
        if not t:
            return None
        d = dict(t)
        d["checklist"] = rows(c.execute("SELECT * FROM task_checklist WHERE task_id=? ORDER BY position, id", (task_id,)))
        d["subtasks"] = rows(c.execute(
            """SELECT t.id, t.title, t.status, t.due_date, t.assignee_id, u.name AS assignee_name
               FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id WHERE t.parent_id=? ORDER BY t.position, t.id""",
            (task_id,)))
        d["comments"] = rows(c.execute(
            """SELECT cm.*, u.name AS user_name FROM task_comments cm LEFT JOIN users u ON u.id=cm.user_id
               WHERE cm.task_id=? ORDER BY cm.id""", (task_id,)))
        d["waiting_on"] = rows(c.execute(
            """SELECT b.id, b.title, b.status, u.name AS assignee_name FROM task_dependencies x
               JOIN tasks b ON b.id=x.depends_on_id LEFT JOIN users u ON u.id=b.assignee_id
               WHERE x.task_id=? ORDER BY b.id""", (task_id,)))
        d["blocking"] = rows(c.execute(
            """SELECT b.id, b.title, b.status, u.name AS assignee_name FROM task_dependencies x
               JOIN tasks b ON b.id=x.task_id LEFT JOIN users u ON u.id=b.assignee_id
               WHERE x.depends_on_id=? ORDER BY b.id""", (task_id,)))
        d["updates"] = rows(c.execute(
            """SELECT tu.*, u.name AS user_name FROM task_updates tu LEFT JOIN users u ON u.id=tu.user_id
               WHERE task_id=? ORDER BY tu.id DESC LIMIT 20""", (task_id,)))
        if d["parent_id"]:
            p = c.execute("SELECT id, title FROM tasks WHERE id=?", (d["parent_id"],)).fetchone()
            d["parent"] = dict(p) if p else None
    return d


# ---------- checklist ----------

def add_checklist_item(task_id: int, text: str) -> int:
    with tx() as c:
        pos = c.execute("SELECT COALESCE(MAX(position),0)+1 FROM task_checklist WHERE task_id=?", (task_id,)).fetchone()[0]
        return c.execute("INSERT INTO task_checklist(task_id, text, position) VALUES(?,?,?)",
                         (task_id, text.strip(), pos)).lastrowid


def checklist_task_id(item_id: int) -> int | None:
    with tx() as c:
        r = c.execute("SELECT task_id FROM task_checklist WHERE id=?", (item_id,)).fetchone()
    return r["task_id"] if r else None


def update_checklist_item(item_id: int, done: bool | None = None, text: str | None = None):
    with tx() as c:
        if done is not None:
            c.execute("UPDATE task_checklist SET done=? WHERE id=?", (int(done), item_id))
        if text:
            c.execute("UPDATE task_checklist SET text=? WHERE id=?", (text.strip(), item_id))


def delete_checklist_item(item_id: int):
    with tx() as c:
        c.execute("DELETE FROM task_checklist WHERE id=?", (item_id,))


# ---------- comments & mentions ----------

def add_comment(task_id: int, user_id, body: str, source: str = "web") -> dict:
    body = body.strip()
    with tx() as c:
        cid = c.execute("INSERT INTO task_comments(task_id, user_id, body, source) VALUES(?,?,?,?)",
                        (task_id, user_id, body, source)).lastrowid
    t = automations._task(task_id)
    author = automations._user_name(user_id)
    mentioned = [u for u in find_mentions(body) if u["id"] != user_id]
    snippet = body if len(body) <= 300 else body[:297] + "…"
    notified = []
    from . import chat
    for u in mentioned:
        if u["blink_user_id"]:
            msg = (f"💬 **{author} mentioned you** on #{t['id']} {t['title']} · {t['project_name']}\n"
                   f"“{snippet}”\n\nJust reply here to answer on the task.")
            automations._send([u["blink_user_id"]], msg, cat="mentions")
            chat.remember(u["id"], "", msg)      # so a plain reply ("I see it") is understood
            notified.append(u["id"])
    # Tell the assignee about comments from others, unless they were already @mentioned
    if (automations._on("comment_assignee") and t["assignee_blink"] and t["assignee_id"] != user_id
            and t["assignee_id"] not in notified):
        msg = (f"💬 **{author} commented** on your task #{t['id']} {t['title']}\n“{snippet}”\n\n"
               "Just reply here to answer on the task.")
        automations._send([t["assignee_blink"]], msg, cat="comments")
        chat.remember(t["assignee_id"], "", msg)
    from . import watchers
    watchers.notify(task_id, f"💬 **{author} commented** on #{t['id']} {t['title']} (you're watching)\n“{snippet}”",
                    skip={user_id, t["assignee_id"], *notified})
    return {"id": cid, "mentioned": [u["name"] for u in mentioned]}


# ---------- dependencies ----------

def _reachable(start: int, target: int) -> bool:
    """Is `target` among the tasks `start` (transitively) waits on? Used to refuse cycles."""
    seen, stack = set(), [start]
    with tx() as c:
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack += [r[0] for r in c.execute("SELECT depends_on_id FROM task_dependencies WHERE task_id=?", (cur,))]
    return False


def add_dependency(task_id: int, depends_on_id: int):
    if task_id == depends_on_id:
        raise ValueError("A task can't wait on itself.")
    if _reachable(depends_on_id, task_id):
        raise ValueError(f"That would create a loop: #{depends_on_id} already waits on #{task_id}.")
    with tx() as c:
        if not c.execute("SELECT 1 FROM tasks WHERE id=?", (depends_on_id,)).fetchone():
            raise ValueError(f"Task #{depends_on_id} doesn't exist.")
        c.execute("INSERT OR IGNORE INTO task_dependencies(task_id, depends_on_id) VALUES(?,?)", (task_id, depends_on_id))


def remove_dependency(task_id: int, depends_on_id: int):
    with tx() as c:
        c.execute("DELETE FROM task_dependencies WHERE task_id=? AND depends_on_id=?", (task_id, depends_on_id))


# ---------- delete ----------

def delete_task(task_id: int):
    """Delete a task and its subtasks (and their checklists, comments, links, history)."""
    with tx() as c:
        ids, stack = [], [task_id]
        while stack:
            cur = stack.pop()
            ids.append(cur)
            stack += [r[0] for r in c.execute("SELECT id FROM tasks WHERE parent_id=?", (cur,))]
        q = ",".join("?" * len(ids))
        for table, col in [("task_checklist", "task_id"), ("task_comments", "task_id"), ("task_updates", "task_id"),
                           ("task_dependencies", "task_id"), ("task_dependencies", "depends_on_id")]:
            c.execute(f"DELETE FROM {table} WHERE {col} IN ({q})", ids)
        c.execute(f"DELETE FROM tasks WHERE id IN ({q})", ids)
