"""Project roles (Property Manager, Head of Accounting, AP, AR, ...) and each project's team.

- project_roles: the list of roles, each with an optional default person.
- project_team: who holds each role on a given project.
- tasks.role_id / template_items.default_role_id: a task assigned "by role" goes to whoever holds that
  role on its project, and follows the role if the person changes. Assigning a task to someone directly
  detaches it from the role.
"""
from . import blink
from .db import rows, tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS project_roles (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    default_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS project_team (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES project_roles(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (project_id, role_id)
);
"""
DEFAULT_ROLES = ["Property Manager", "Head of Accounting", "AP", "AR", "Leasing", "Asset Manager"]
# The property team card shows these (plus the owner contact); added once if missing.
CORE_ROLES = ["Asset Manager", "Property Manager", "Backup Property Manager", "AR", "AP", "Property Assistant"]


def init():
    with tx() as c:
        c.executescript(SCHEMA)
        for table, col, typ in [("projects", "is_sample", "INTEGER NOT NULL DEFAULT 0"), ("projects", "is_general", "INTEGER NOT NULL DEFAULT 0"), ("projects", "description", "TEXT"), ("projects", "property", "TEXT"),
                                ("projects", "client", "TEXT"), ("tasks", "role_id", "INTEGER"),
                                ("template_items", "default_role_id", "INTEGER")]:
            if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        if not c.execute("SELECT 1 FROM project_roles").fetchone():
            c.executemany("INSERT INTO project_roles(name, position) VALUES(?,?)",
                          [(n, i) for i, n in enumerate(DEFAULT_ROLES)])
        have = {r[0].lower() for r in c.execute("SELECT name FROM project_roles")}
        for n in CORE_ROLES:
            if n.lower() not in have:
                pos = c.execute("SELECT COALESCE(MAX(position),0)+1 FROM project_roles").fetchone()[0]
                c.execute("INSERT INTO project_roles(name, position) VALUES(?,?)", (n, pos))


# ---------- tasks without a project ----------

def general_project_id() -> int:
    """Tasks that don't belong to any project live in one hidden 'No project' bucket (is_general=1)."""
    with tx() as c:
        r = c.execute("SELECT id FROM projects WHERE is_general=1").fetchone()
        if r:
            c.execute("UPDATE projects SET status='active' WHERE id=?", (r["id"],))
            return r["id"]
        return c.execute("INSERT INTO projects(name, start_date, status, is_general) VALUES('No project', date('now'), 'active', 1)"
                         ).lastrowid


# ---------- roles ----------

def list_roles() -> list[dict]:
    with tx() as c:
        return rows(c.execute("""SELECT r.*, u.name AS default_user_name FROM project_roles r
                                 LEFT JOIN users u ON u.id=r.default_user_id ORDER BY r.position, r.id"""))


def add_role(name: str, default_user_id=None) -> int:
    with tx() as c:
        pos = c.execute("SELECT COALESCE(MAX(position),0)+1 FROM project_roles").fetchone()[0]
        return c.execute("INSERT INTO project_roles(name, default_user_id, position) VALUES(?,?,?)",
                         (name.strip(), default_user_id, pos)).lastrowid


def update_role(role_id: int, fields: dict):
    with tx() as c:
        if fields.get("name"):
            c.execute("UPDATE project_roles SET name=? WHERE id=?", (fields["name"].strip(), role_id))
        if "default_user_id" in fields:
            c.execute("UPDATE project_roles SET default_user_id=? WHERE id=?", (fields["default_user_id"] or None, role_id))


def delete_role(role_id: int):
    with tx() as c:
        c.execute("UPDATE tasks SET role_id=NULL WHERE role_id=?", (role_id,))
        c.execute("UPDATE template_items SET default_role_id=NULL WHERE default_role_id=?", (role_id,))
        c.execute("DELETE FROM project_team WHERE role_id=?", (role_id,))
        c.execute("DELETE FROM project_roles WHERE id=?", (role_id,))


# ---------- a project's team ----------

def resolve_team(overrides: dict | None, address: str | None = None, property_wins: bool = False) -> dict:
    """role_id -> user_id for a new project: role defaults, then the property's team (if the address is a known
    property), then what was picked. property_wins=True (repeating projects) lets the property's current team
    beat the team saved when the repeat was set up."""
    from . import properties
    team = {r["id"]: r["default_user_id"] for r in list_roles()}
    prop = properties.team_map(properties.property_for_address(address))
    picked = {int(k): (int(v) if v else None) for k, v in (overrides or {}).items()}
    for layer in ([picked, prop] if property_wins else [prop, picked]):
        team.update({k: v for k, v in layer.items() if v or layer is picked})
    return team


def save_team(project_id: int, team: dict):
    with tx() as c:
        c.executemany("INSERT OR REPLACE INTO project_team(project_id, role_id, user_id) VALUES(?,?,?)",
                      [(project_id, r, u) for r, u in team.items() if u])


def project_team(project_id: int) -> list[dict]:
    with tx() as c:
        return rows(c.execute(
            """SELECT r.id AS role_id, r.name AS role, t.user_id, u.name AS user_name FROM project_roles r
               LEFT JOIN project_team t ON t.role_id=r.id AND t.project_id=?
               LEFT JOIN users u ON u.id=t.user_id ORDER BY r.position, r.id""", (project_id,)))


def set_member(project_id: int, role_id: int, user_id, _from_property: bool = False) -> int:
    """Change who holds a role on a project; open tasks assigned by that role move to the new person.
    Projects at a property follow the property's team, so a change there goes to the property instead.
    Returns how many tasks moved."""
    if not _from_property:
        from . import properties
        with tx() as c:
            p = c.execute("SELECT property_id FROM projects WHERE id=?", (project_id,)).fetchone()
        if p and p["property_id"]:
            return properties.set_member(p["property_id"], role_id, user_id)
    with tx() as c:
        if user_id:
            c.execute("INSERT OR REPLACE INTO project_team(project_id, role_id, user_id) VALUES(?,?,?)",
                      (project_id, role_id, user_id))
        else:
            c.execute("DELETE FROM project_team WHERE project_id=? AND role_id=?", (project_id, role_id))
        moved = rows(c.execute(
            "SELECT id, title, due_date FROM tasks WHERE project_id=? AND role_id=? AND status!='done'",
            (project_id, role_id)))
        c.execute("UPDATE tasks SET assignee_id=? WHERE project_id=? AND role_id=? AND status!='done'",
                  (user_id or None, project_id, role_id))
        u = c.execute("SELECT blink_user_id FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None
        role = c.execute("SELECT name FROM project_roles WHERE id=?", (role_id,)).fetchone()
        proj = c.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
    if moved and u and u["blink_user_id"]:
        lines = "\n".join(f"#{t['id']} {t['title']}" + (f" – due {t['due_date']}" if t["due_date"] else "") for t in moved)
        try:
            from . import notify
            notify.deliver_blink([u["blink_user_id"]], "team",
                          f"👤 You're now **{role['name']}** on *{proj['name']}* — these tasks are yours:\n{lines}")
        except Exception:
            pass
    return len(moved)
