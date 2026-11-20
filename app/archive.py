"""Archive projects and property pages: out of the way, nothing lost.

Archived projects drop off task lists, Home, the calendar, reminders, Blink summaries and tracking boards (every
query already keeps to status='active'). Archived properties drop off the Properties page and pickers and stop
sending critical-date alerts; archiving one also archives its projects, and unarchiving brings those same projects
back. Both can be found under the "Archived" filters and restored in one click.
"""
from .db import tx


def init():
    with tx() as c:
        if "archived_at" not in {r[1] for r in c.execute("PRAGMA table_info(properties)")}:
            c.execute("ALTER TABLE properties ADD COLUMN archived_at TEXT")
        cols = {r[1] for r in c.execute("PRAGMA table_info(projects)")}
        if "archived_at" not in cols:
            c.execute("ALTER TABLE projects ADD COLUMN archived_at TEXT")
        if "archived_with_property" not in cols:
            c.execute("ALTER TABLE projects ADD COLUMN archived_with_property INTEGER NOT NULL DEFAULT 0")


def _reopen(c, pid: int):
    left = c.execute("SELECT COUNT(*) FROM tasks WHERE project_id=? AND status!='done'", (pid,)).fetchone()[0]
    c.execute("UPDATE projects SET status=?, archived_at=NULL, archived_with_property=0 WHERE id=?",
              ("active" if left else "done", pid))


def archive_project(pid: int):
    with tx() as c:
        c.execute("""UPDATE projects SET status='archived', archived_at=datetime('now'), archived_with_property=0
                     WHERE id=? AND is_general=0""", (pid,))


def unarchive_project(pid: int):
    with tx() as c:
        _reopen(c, pid)
        prop = c.execute("SELECT property_id FROM projects WHERE id=?", (pid,)).fetchone()
        if prop and prop[0]:   # a project coming back brings its property page back too
            c.execute("UPDATE properties SET archived_at=NULL WHERE id=?", (prop[0],))


def archive_property(prop_id: int) -> int:
    with tx() as c:
        c.execute("UPDATE properties SET archived_at=datetime('now') WHERE id=?", (prop_id,))
        return c.execute("""UPDATE projects SET status='archived', archived_at=datetime('now'), archived_with_property=1
                            WHERE property_id=? AND status!='archived' AND is_general=0""", (prop_id,)).rowcount


def unarchive_property(prop_id: int) -> int:
    with tx() as c:
        c.execute("UPDATE properties SET archived_at=NULL WHERE id=?", (prop_id,))
        ids = [r[0] for r in c.execute("SELECT id FROM projects WHERE property_id=? AND archived_with_property=1", (prop_id,))]
        for pid in ids:
            _reopen(c, pid)
    return len(ids)
