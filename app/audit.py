"""Who-did-what log + the 🗑 Recently deleted bin.

log(): one line per meaningful change (status, assignee, due date, team, deletes, restores …).
Deleting a task / project / property page first takes a snapshot of every row involved and keeps it for 30 days;
restore() puts it back (with fresh ids where needed, so nothing created since then is disturbed).
"""
import json
import logging
from datetime import date, timedelta

from .db import rows, tx

log_ = logging.getLogger("audit")
KEEP_DAYS = 30
ICON = {"task": "✓", "project": "📁", "property": "🏢"}


def init():
    with tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS activity_log(
            id INTEGER PRIMARY KEY, at TEXT NOT NULL DEFAULT (datetime('now')), user_id INTEGER, action TEXT NOT NULL,
            entity TEXT, entity_id INTEGER, label TEXT, detail TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS activity_at ON activity_log(at)")
        c.execute("""CREATE TABLE IF NOT EXISTS trash(
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, label TEXT, deleted_by INTEGER,
            deleted_at TEXT NOT NULL DEFAULT (datetime('now')), payload TEXT NOT NULL, restored_at TEXT, restored_by INTEGER,
            restored_id INTEGER)""")


def log(user_id, action: str, entity: str | None = None, entity_id=None, label: str | None = None, detail: str | None = None):
    try:
        with tx() as c:
            c.execute("INSERT INTO activity_log(user_id, action, entity, entity_id, label, detail) VALUES(?,?,?,?,?,?)",
                      (user_id, action, entity, entity_id, (label or "")[:300] or None, (detail or "")[:500] or None))
    except Exception:
        log_.exception("activity log write failed")


def activity(limit: int = 200, user_id: int | None = None, entity: str | None = None) -> list[dict]:
    q = """SELECT a.*, COALESCE(u.name, 'System') AS who FROM activity_log a LEFT JOIN users u ON u.id=a.user_id WHERE 1=1"""
    args = []
    if user_id:
        q += " AND a.user_id=?"
        args.append(user_id)
    if entity:
        q += " AND a.entity=?"
        args.append(entity)
    with tx() as c:
        return rows(c.execute(q + " ORDER BY a.id DESC LIMIT ?", (*args, min(limit, 1000))))


# ---------- snapshots ----------

def _all(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args)]


def _task_tree(c, root_ids: list[int]) -> list[int]:
    ids, stack = [], list(root_ids)
    while stack:
        cur = stack.pop()
        ids.append(cur)
        stack += [r[0] for r in c.execute("SELECT id FROM tasks WHERE parent_id=?", (cur,))]
    return ids


def _task_rows(c, ids: list[int]) -> dict:
    if not ids:
        return {"tasks": [], "task_checklist": [], "task_comments": [], "task_updates": [], "task_dependencies": []}
    q = ",".join("?" * len(ids))
    tasks = _all(c, f"SELECT * FROM tasks WHERE id IN ({q})", ids)
    depth = {}
    by_id = {t["id"]: t for t in tasks}
    def d(t):
        if t["id"] not in depth:
            depth[t["id"]] = 0 if t["parent_id"] not in by_id else d(by_id[t["parent_id"]]) + 1
        return depth[t["id"]]
    tasks.sort(key=lambda t: (d(t), t["id"]))   # parents before children
    return {"tasks": tasks,
            "task_checklist": _all(c, f"SELECT * FROM task_checklist WHERE task_id IN ({q})", ids),
            "task_comments": _all(c, f"SELECT * FROM task_comments WHERE task_id IN ({q})", ids),
            "task_updates": _all(c, f"SELECT * FROM task_updates WHERE task_id IN ({q})", ids),
            "task_dependencies": _all(c, f"SELECT * FROM task_dependencies WHERE task_id IN ({q}) OR depends_on_id IN ({q})",
                                      ids + ids)}


def snapshot_task(task_id: int) -> dict:
    with tx() as c:
        return _task_rows(c, _task_tree(c, [task_id]))


def snapshot_project(project_id: int) -> dict:
    with tx() as c:
        snap = _task_rows(c, [r[0] for r in c.execute("SELECT id FROM tasks WHERE project_id=?", (project_id,))])
        snap["projects"] = _all(c, "SELECT * FROM projects WHERE id=?", (project_id,))
        snap["project_team"] = _all(c, "SELECT * FROM project_team WHERE project_id=?", (project_id,))
    return snap


def snapshot_property(property_id: int) -> dict:
    with tx() as c:
        return {"properties": _all(c, "SELECT * FROM properties WHERE id=?", (property_id,)),
                "critical_dates": _all(c, "SELECT * FROM critical_dates WHERE property_id=?", (property_id,)),
                "property_contacts": _all(c, "SELECT * FROM property_contacts WHERE property_id=?", (property_id,)),
                "property_docs": _all(c, "SELECT * FROM property_docs WHERE property_id=?", (property_id,)),
                "property_team": _all(c, "SELECT * FROM property_team WHERE property_id=?", (property_id,)),
                "property_vendors": _all(c, "SELECT * FROM property_vendors WHERE property_id=?", (property_id,)),
                "property_vendor_slots": _all(c, "SELECT * FROM property_vendor_slots WHERE property_id=?", (property_id,)),
                "linked_projects": [r[0] for r in c.execute("SELECT id FROM projects WHERE property_id=?", (property_id,))]}


def to_trash(kind: str, label: str, payload: dict, user_id) -> int:
    with tx() as c:
        return c.execute("INSERT INTO trash(kind, label, deleted_by, payload) VALUES(?,?,?,?)",
                         (kind, label, user_id, json.dumps(payload))).lastrowid


# ---------- restore ----------

def _insert(c, table: str, row: dict, drop=("id",)) -> int:
    cols = [k for k in row if k not in drop]
    have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    cols = [k for k in cols if k in have]
    return c.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                     [row[k] for k in cols]).lastrowid


def _restore_tasks(c, snap: dict, project_map: dict | None = None) -> dict:
    """Insert snapshot tasks with new ids; returns old→new id map."""
    tmap = {}
    for t in snap["tasks"]:
        t = dict(t)
        t["project_id"] = (project_map or {}).get(t["project_id"], t["project_id"])
        if not c.execute("SELECT 1 FROM projects WHERE id=?", (t["project_id"],)).fetchone():
            from . import team
            t["project_id"] = team.general_project_id()
        t["parent_id"] = tmap.get(t["parent_id"], t["parent_id"]) if t["parent_id"] else None
        if t["parent_id"] and not c.execute("SELECT 1 FROM tasks WHERE id=?", (t["parent_id"],)).fetchone():
            t["parent_id"] = None
        if "repeat_next_id" in t:
            t["repeat_next_id"] = None
        tmap[t["id"]] = _insert(c, "tasks", t)
    for table in ("task_checklist", "task_comments", "task_updates"):
        for r in snap.get(table, []):
            if r["task_id"] in tmap:
                _insert(c, table, {**r, "task_id": tmap[r["task_id"]]})
    for r in snap.get("task_dependencies", []):
        a, b = tmap.get(r["task_id"], r["task_id"]), tmap.get(r["depends_on_id"], r["depends_on_id"])
        if c.execute("SELECT 1 FROM tasks WHERE id=?", (a,)).fetchone() and c.execute("SELECT 1 FROM tasks WHERE id=?", (b,)).fetchone():
            c.execute("INSERT OR IGNORE INTO task_dependencies(task_id, depends_on_id) VALUES(?,?)", (a, b))
    return tmap


def restore(trash_id: int, user_id) -> dict:
    with tx() as c:
        t = c.execute("SELECT * FROM trash WHERE id=?", (trash_id,)).fetchone()
        if not t:
            raise ValueError("Not found in Recently deleted.")
        if t["restored_at"]:
            raise ValueError("That was already restored.")
        snap = json.loads(t["payload"])
        new_id = None
        if t["kind"] == "task":
            tmap = _restore_tasks(c, snap)
            new_id = tmap.get(snap["tasks"][0]["id"]) if snap["tasks"] else None
        elif t["kind"] == "project":
            p = dict(snap["projects"][0])
            if p.get("property_id") and not c.execute("SELECT 1 FROM properties WHERE id=?", (p["property_id"],)).fetchone():
                p["property_id"] = None
            new_id = _insert(c, "projects", p)
            for r in snap.get("project_team", []):
                c.execute("INSERT OR REPLACE INTO project_team(project_id, role_id, user_id) VALUES(?,?,?)",
                          (new_id, r["role_id"], r["user_id"]))
            _restore_tasks(c, snap, {p["id"]: new_id})
        elif t["kind"] == "property":
            p = dict(snap["properties"][0])
            if c.execute("SELECT 1 FROM properties WHERE norm=?", (p["norm"],)).fetchone():
                raise ValueError("A property with that address exists again — restore isn't possible.")
            new_id = _insert(c, "properties", p)
            for r in snap.get("property_vendor_slots", []):
                c.execute("INSERT OR IGNORE INTO property_vendor_slots(property_id, trade) VALUES(?,?)", (new_id, r["trade"]))
            for table in ("critical_dates", "property_contacts", "property_docs", "property_vendors"):
                for r in snap.get(table, []):
                    _insert(c, table, {**r, "property_id": new_id, **({"task_id": r.get("task_id")} if table == "critical_dates" else {})})
            for r in snap.get("property_team", []):
                c.execute("INSERT OR REPLACE INTO property_team(property_id, role_id, user_id) VALUES(?,?,?)",
                          (new_id, r["role_id"], r["user_id"]))
            for pid in snap.get("linked_projects", []):
                c.execute("UPDATE projects SET property_id=? WHERE id=? AND property_id IS NULL", (new_id, pid))
        c.execute("UPDATE trash SET restored_at=datetime('now'), restored_by=?, restored_id=? WHERE id=?",
                  (user_id, new_id, trash_id))
    log(user_id, "restored", t["kind"], new_id, t["label"])
    return {"kind": t["kind"], "id": new_id, "label": t["label"]}


def bin_items() -> list[dict]:
    with tx() as c:
        items = rows(c.execute("""SELECT t.id, t.kind, t.label, t.deleted_at, t.restored_at, t.restored_id,
                                         COALESCE(u.name,'Someone') AS deleted_by_name, r.name AS restored_by_name
                                  FROM trash t LEFT JOIN users u ON u.id=t.deleted_by LEFT JOIN users r ON r.id=t.restored_by
                                  ORDER BY t.id DESC LIMIT 300"""))
    today = date.today()
    for i in items:
        i["icon"] = ICON.get(i["kind"], "•")
        i["days_left"] = max(0, KEEP_DAYS - (today - date.fromisoformat(i["deleted_at"][:10])).days)
    return items


def purge(today: date | None = None) -> int:
    """Daily: forget bin items (and their files) older than 30 days; trim the log to a year."""
    cutoff = ((today or date.today()) - timedelta(days=KEEP_DAYS)).isoformat()
    with tx() as c:
        old = rows(c.execute("SELECT id, kind, payload, restored_at FROM trash WHERE deleted_at < ?", (cutoff,)))
        c.execute("DELETE FROM trash WHERE deleted_at < ?", (cutoff,))
        c.execute("DELETE FROM activity_log WHERE at < datetime('now','-365 days')")
    from . import properties
    for t in old:   # property documents are kept on disk until the bin forgets them
        if t["kind"] == "property" and not t["restored_at"]:
            for d in json.loads(t["payload"]).get("property_docs", []):
                properties._remove_file(d["path"])
    return len(old)
