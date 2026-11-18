"""Checklist templates with full structure: items, subtasks, checklist items and 'waiting on' links.

template_items.parent_item_id  -> subtask of another item
template_checklist             -> checklist lines inside an item
template_dependencies          -> item can't start until another item is done
"""
from datetime import date, timedelta

from .db import rows, tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS template_checklist (
    id INTEGER PRIMARY KEY,
    template_item_id INTEGER NOT NULL REFERENCES template_items(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS template_dependencies (
    item_id INTEGER NOT NULL REFERENCES template_items(id) ON DELETE CASCADE,
    depends_on_item_id INTEGER NOT NULL REFERENCES template_items(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, depends_on_item_id)
);
"""


def init():
    from . import team
    team.init()
    with tx() as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(template_items)")}
        if "parent_item_id" not in cols:
            c.execute("ALTER TABLE template_items ADD COLUMN parent_item_id INTEGER")
        if "tutorial_url" not in cols:   # how-to video/doc link shown on every task made from this item
            c.execute("ALTER TABLE template_items ADD COLUMN tutorial_url TEXT")
        if "tutorial_url" not in {r[1] for r in c.execute("PRAGMA table_info(tasks)")}:
            c.execute("ALTER TABLE tasks ADD COLUMN tutorial_url TEXT")


def _ordered(items: list[dict]) -> list[dict]:
    """Parents before children (depth-first), keeping position order."""
    kids: dict = {}
    for it in items:
        kids.setdefault(it.get("parent_item_id") or it.get("parent_id"), []).append(it)
    ids = {it["id"] for it in items}
    out = []

    def walk(it, depth):
        out.append({**it, "depth": depth})
        for k in kids.get(it["id"], []):
            walk(k, depth + 1)

    for it in items:
        p = it.get("parent_item_id") or it.get("parent_id")
        if not p or p not in ids:
            walk(it, 0)
    return out


# ---------- read ----------

def list_templates() -> list[dict]:
    with tx() as c:
        ts = rows(c.execute("SELECT * FROM templates ORDER BY name"))
        for t in ts:
            items = rows(c.execute("SELECT * FROM template_items WHERE template_id=? ORDER BY position, id", (t["id"],)))
            for it in items:
                it["checklist"] = rows(c.execute(
                    "SELECT id, text FROM template_checklist WHERE template_item_id=? ORDER BY position, id", (it["id"],)))
                it["waiting_on"] = [r[0] for r in c.execute(
                    "SELECT depends_on_item_id FROM template_dependencies WHERE item_id=?", (it["id"],))]
            t["items"] = _ordered(items)
    return ts


# ---------- project <-> template ----------

def create_project(template_id: int, name: str, start: date, recurrence_id=None, overrides: dict | None = None,
                   team_overrides: dict | None = None, details: dict | None = None) -> int:
    from . import team as team_mod
    overrides, details = overrides or {}, details or {}
    team = team_mod.resolve_team(team_overrides)
    with tx() as c:
        pid = c.execute(
            "INSERT INTO projects(name, template_id, recurrence_id, start_date, property, client, description) VALUES(?,?,?,?,?,?,?)",
            (name, template_id, recurrence_id, start.isoformat(), details.get("property"), details.get("client"),
             details.get("description"))).lastrowid
        items = _ordered(rows(c.execute(
            "SELECT * FROM template_items WHERE template_id=? ORDER BY position, id", (template_id,))))
        task_of = {}
        for it in items:  # parents first, so parent task ids exist
            role = it.get("default_role_id")
            who = overrides.get(it["id"]) or (team.get(role) if role else None) or it["default_assignee_id"]
            task_of[it["id"]] = c.execute(
                """INSERT INTO tasks(project_id, parent_id, title, assignee_id, role_id, due_date, position, estimate_minutes,
                                     tutorial_url, repeat) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (pid, task_of.get(it["parent_item_id"]), it["title"], who, role,
                 (start + timedelta(days=it["due_offset_days"] or 0)).isoformat(), it["position"],
                 it.get("estimate_minutes"), it.get("tutorial_url"), it.get("repeat"))).lastrowid
            for n, ck in enumerate(rows(c.execute(
                    "SELECT text FROM template_checklist WHERE template_item_id=? ORDER BY position, id", (it["id"],)))):
                c.execute("INSERT INTO task_checklist(task_id, text, position) VALUES(?,?,?)",
                          (task_of[it["id"]], ck["text"], n))
        for d in rows(c.execute(
                """SELECT d.item_id, d.depends_on_item_id FROM template_dependencies d
                   JOIN template_items i ON i.id=d.item_id WHERE i.template_id=?""", (template_id,))):
            if d["item_id"] in task_of and d["depends_on_item_id"] in task_of:
                c.execute("INSERT OR IGNORE INTO task_dependencies(task_id, depends_on_id) VALUES(?,?)",
                          (task_of[d["item_id"]], task_of[d["depends_on_item_id"]]))
    team_mod.save_team(pid, team)
    return pid


def save_from_project(project_id: int, name: str) -> int:
    with tx() as c:
        p = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        start = date.fromisoformat(p["start_date"])
        tid = c.execute("INSERT INTO templates(name, description) VALUES(?,?)",
                        (name, f"Saved from project '{p['name']}'")).lastrowid
        tasks = _ordered(rows(c.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY position, id", (project_id,))))
        item_of = {}
        for i, t in enumerate(tasks):
            offset = (date.fromisoformat(t["due_date"]) - start).days if t["due_date"] else 0
            by_role = t.get("role_id")
            item_of[t["id"]] = c.execute(
                """INSERT INTO template_items(template_id, parent_item_id, title, default_assignee_id, default_role_id,
                                              due_offset_days, position, estimate_minutes, tutorial_url, repeat)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (tid, item_of.get(t["parent_id"]), t["title"], None if by_role else t["assignee_id"], by_role,
                 max(offset, 0), i, t.get("estimate_minutes") or t.get("actual_minutes"), t.get("tutorial_url"),
                 t.get("repeat"))).lastrowid
            for n, ck in enumerate(rows(c.execute(
                    "SELECT text FROM task_checklist WHERE task_id=? ORDER BY position, id", (t["id"],)))):
                c.execute("INSERT INTO template_checklist(template_item_id, text, position) VALUES(?,?,?)",
                          (item_of[t["id"]], ck["text"], n))
        for d in rows(c.execute(
                """SELECT d.task_id, d.depends_on_id FROM task_dependencies d JOIN tasks t ON t.id=d.task_id
                   WHERE t.project_id=?""", (project_id,))):
            if d["task_id"] in item_of and d["depends_on_id"] in item_of:
                c.execute("INSERT OR IGNORE INTO template_dependencies(item_id, depends_on_item_id) VALUES(?,?)",
                          (item_of[d["task_id"]], item_of[d["depends_on_id"]]))
    return tid


# ---------- editing a template ----------

def add_item(template_id: int, title: str, parent_item_id=None, default_assignee_id=None, due_offset_days=0,
             default_role_id=None) -> int:
    with tx() as c:
        pos = c.execute("SELECT COALESCE(MAX(position),0)+1 FROM template_items WHERE template_id=?",
                        (template_id,)).fetchone()[0]
        return c.execute(
            """INSERT INTO template_items(template_id, parent_item_id, title, default_assignee_id, default_role_id,
                                          due_offset_days, position) VALUES(?,?,?,?,?,?,?)""",
            (template_id, parent_item_id, title.strip(), default_assignee_id, default_role_id,
             due_offset_days or 0, pos)).lastrowid


def clean_url(url) -> str | None:
    """Accept http(s) links only (a bare 'loom.com/…' gets https:// added); blank clears."""
    url = (url or "").strip()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    if " " in url or "." not in url.split("//", 1)[1].split("/", 1)[0]:
        raise ValueError("That doesn't look like a web link.")
    return url


def update_template(template_id: int, fields: dict):
    with tx() as c:
        if fields.get("name"):
            c.execute("UPDATE templates SET name=? WHERE id=?", (fields["name"].strip(), template_id))
        if "description" in fields:
            c.execute("UPDATE templates SET description=? WHERE id=?", (fields["description"] or "", template_id))


def move_item(item_id: int, direction: int):
    """Swap with the previous/next sibling (same parent)."""
    with tx() as c:
        it = c.execute("SELECT * FROM template_items WHERE id=?", (item_id,)).fetchone()
        if not it:
            return
        sibs = [r["id"] for r in c.execute(
            """SELECT id FROM template_items WHERE template_id=? AND parent_item_id IS ? ORDER BY position, id""",
            (it["template_id"], it["parent_item_id"]))]
        i = sibs.index(item_id)
        j = i + (1 if direction > 0 else -1)
        if 0 <= j < len(sibs):
            sibs[i], sibs[j] = sibs[j], sibs[i]
            c.executemany("UPDATE template_items SET position=? WHERE id=?", [(n, sid) for n, sid in enumerate(sibs)])


def update_item(item_id: int, fields: dict):
    # An item is assigned either to a person or to a project role, never both.
    if fields.get("default_role_id"):
        fields["default_assignee_id"] = None
    elif fields.get("default_assignee_id"):
        fields["default_role_id"] = None
    allowed = {k: v for k, v in fields.items()
               if k in ("title", "default_assignee_id", "default_role_id", "due_offset_days", "estimate_minutes", "tutorial_url",
                        "repeat")}
    if "repeat" in allowed:
        from . import recur
        allowed["repeat"] = recur.clean(allowed["repeat"])
    if "tutorial_url" in allowed:
        allowed["tutorial_url"] = clean_url(allowed["tutorial_url"])
    if not allowed:
        return
    with tx() as c:
        c.execute(f"UPDATE template_items SET {', '.join(k + '=?' for k in allowed)} WHERE id=?",
                  (*allowed.values(), item_id))


def delete_item(item_id: int):
    with tx() as c:
        ids, stack = [], [item_id]
        while stack:
            cur = stack.pop()
            ids.append(cur)
            stack += [r[0] for r in c.execute("SELECT id FROM template_items WHERE parent_item_id=?", (cur,))]
        q = ",".join("?" * len(ids))
        c.execute(f"DELETE FROM template_checklist WHERE template_item_id IN ({q})", ids)
        c.execute(f"DELETE FROM template_dependencies WHERE item_id IN ({q}) OR depends_on_item_id IN ({q})", ids + ids)
        c.execute(f"DELETE FROM template_items WHERE id IN ({q})", ids)


def add_checklist(item_id: int, text: str) -> int:
    with tx() as c:
        pos = c.execute("SELECT COALESCE(MAX(position),0)+1 FROM template_checklist WHERE template_item_id=?",
                        (item_id,)).fetchone()[0]
        return c.execute("INSERT INTO template_checklist(template_item_id, text, position) VALUES(?,?,?)",
                         (item_id, text.strip(), pos)).lastrowid


def delete_checklist(cid: int):
    with tx() as c:
        c.execute("DELETE FROM template_checklist WHERE id=?", (cid,))


def _reachable(start: int, target: int) -> bool:
    seen, stack = set(), [start]
    with tx() as c:
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack += [r[0] for r in c.execute("SELECT depends_on_item_id FROM template_dependencies WHERE item_id=?", (cur,))]
    return False


def add_dependency(item_id: int, depends_on_item_id: int):
    if item_id == depends_on_item_id:
        raise ValueError("An item can't wait on itself.")
    if _reachable(depends_on_item_id, item_id):
        raise ValueError("That would create a loop.")
    with tx() as c:
        a = c.execute("SELECT template_id FROM template_items WHERE id=?", (item_id,)).fetchone()
        b = c.execute("SELECT template_id FROM template_items WHERE id=?", (depends_on_item_id,)).fetchone()
        if not a or not b or a[0] != b[0]:
            raise ValueError("Both items must be in the same template.")
        c.execute("INSERT OR IGNORE INTO template_dependencies(item_id, depends_on_item_id) VALUES(?,?)",
                  (item_id, depends_on_item_id))


def remove_dependency(item_id: int, depends_on_item_id: int):
    with tx() as c:
        c.execute("DELETE FROM template_dependencies WHERE item_id=? AND depends_on_item_id=?", (item_id, depends_on_item_id))
