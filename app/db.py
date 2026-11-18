"""SQLite storage. Plain sqlite3 keeps the prototype dependency-free."""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "tasks.db")
# On Railway, always keep the database on the attached volume (Railway sets RAILWAY_VOLUME_MOUNT_PATH);
# otherwise it lives inside the container and is wiped on every deploy.
_VOLUME = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
if _VOLUME and not os.path.abspath(DB_PATH).startswith(os.path.abspath(_VOLUME) + os.sep):
    DB_PATH = os.path.join(_VOLUME, os.path.basename(DB_PATH) or "tasks.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    blink_user_id TEXT UNIQUE,         -- Blink user id (u-…)
    blink_chat_id TEXT                 -- cached 1:1 chat with the app (c-…)
);

-- Reusable checklist ("Monthly Rent Roll", "New Listing Launch", ...)
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS template_items (
    id INTEGER PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    default_assignee_id INTEGER REFERENCES users(id),
    due_offset_days INTEGER DEFAULT 0, -- days after project start
    position INTEGER DEFAULT 0
);

-- Recurring rule: spin up a project from a template every month
CREATE TABLE IF NOT EXISTS recurrences (
    id INTEGER PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
    name_pattern TEXT NOT NULL,        -- e.g. "Month-End Close – {month} {year}"
    day_of_month INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    last_period TEXT                   -- "YYYY-MM" last generated
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    template_id INTEGER REFERENCES templates(id),
    recurrence_id INTEGER REFERENCES recurrences(id),
    start_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'   -- active | done | archived
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    assignee_id INTEGER REFERENCES users(id),
    due_date TEXT,
    status TEXT NOT NULL DEFAULT 'todo',    -- todo | in_progress | blocked | done
    position INTEGER DEFAULT 0,
    last_reminded_on TEXT
);

-- Every status change / note, whether from the web UI or Blink chat
CREATE TABLE IF NOT EXISTS task_updates (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id),
    source TEXT NOT NULL,                   -- web | blink
    status TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Checklist items inside a task (lightweight; no assignee/due)
CREATE TABLE IF NOT EXISTS task_checklist (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0
);

-- Discussion on a task; @mentions are notified in Blink
CREATE TABLE IF NOT EXISTS task_comments (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id),
    body TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'web',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- task_id can't really start until depends_on_id is done
CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_id)
);

-- Which automation already fired for which task (so managers aren't messaged twice)
CREATE TABLE IF NOT EXISTS automation_log (
    rule TEXT NOT NULL,
    task_id INTEGER NOT NULL,
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (rule, task_id)
);

-- Small key/value store for scheduler bookkeeping (last backup date, last summary week, ...)
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Log of every message to/from Blink (doubles as the mock outbox)
CREATE TABLE IF NOT EXISTS blink_messages (
    id INTEGER PRIMARY KEY,
    direction TEXT NOT NULL,                -- out | in
    blink_user_id TEXT NOT NULL,
    text TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with tx() as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        if "blink_chat_id" not in cols:  # migrate DBs created before this column existed
            c.execute("ALTER TABLE users ADD COLUMN blink_chat_id TEXT")
        tcols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
        if "parent_id" not in tcols:  # subtasks are tasks with a parent
            c.execute("ALTER TABLE tasks ADD COLUMN parent_id INTEGER")
        if "description" not in tcols:
            c.execute("ALTER TABLE tasks ADD COLUMN description TEXT")
        if "role" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'staff'")  # manager | staff
        if "weekly_summary" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN weekly_summary INTEGER NOT NULL DEFAULT 0")
    _init_extras()


def _init_extras():
    from . import tpl  # template structure tables (kept in tpl.py)
    tpl.init()


def get_setting(key: str) -> str | None:
    with tx() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def set_setting(key: str, value: str):
    with tx() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]
