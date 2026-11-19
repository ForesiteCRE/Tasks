"""Customizable Home: each person's widget layout and their sticky notes.

Layout = which widgets show and where: {"top": [...], "left": [...], "right": [...], "hidden": [...]}.
The browser owns the list of widgets; the server just keeps (and sanity-checks) each person's choice, so it
follows them from computer to phone. Notes are private to the person who wrote them.
"""
import json

from .db import rows, tx

ZONES = ("top", "left", "right", "hidden")
COLORS = ("yellow", "pink", "blue", "green", "purple")
MAX_NOTES = 50


def init():
    with tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS user_prefs(user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT,
                     PRIMARY KEY(user_id, key))""")
        c.execute("""CREATE TABLE IF NOT EXISTS sticky_notes(
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, text TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT 'yellow',
            position INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT (datetime('now')))""")


def get(user_id: int) -> dict:
    with tx() as c:
        r = c.execute("SELECT value FROM user_prefs WHERE user_id=? AND key='home.layout'", (user_id,)).fetchone()
        notes = rows(c.execute("SELECT id, text, color, updated_at FROM sticky_notes WHERE user_id=? ORDER BY position, id",
                               (user_id,)))
        my_props = [x[0] for x in c.execute("SELECT DISTINCT property_id FROM property_team WHERE user_id=?", (user_id,))]
    return {"layout": json.loads(r[0]) if r and r[0] else None, "notes": notes, "my_properties": my_props}


def set_layout(user_id: int, layout: dict | None):
    if layout is None:   # back to the default
        with tx() as c:
            c.execute("DELETE FROM user_prefs WHERE user_id=? AND key='home.layout'", (user_id,))
        return
    clean, seen = {}, set()
    for z in ZONES:
        ids = []
        for w in (layout.get(z) or [])[:40]:
            w = str(w)[:30]
            if w.replace("_", "").isalnum() and w not in seen:
                seen.add(w)
                ids.append(w)
        clean[z] = ids
    with tx() as c:
        c.execute("""INSERT INTO user_prefs(user_id, key, value) VALUES(?, 'home.layout', ?)
                     ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value""", (user_id, json.dumps(clean)))


def add_note(user_id: int, text: str = "", color: str = "yellow") -> int:
    with tx() as c:
        if c.execute("SELECT COUNT(*) FROM sticky_notes WHERE user_id=?", (user_id,)).fetchone()[0] >= MAX_NOTES:
            raise ValueError(f"That's {MAX_NOTES} notes — clear a few old ones first.")
        pos = c.execute("SELECT COALESCE(MIN(position),0)-1 FROM sticky_notes WHERE user_id=?", (user_id,)).fetchone()[0]
        return c.execute("INSERT INTO sticky_notes(user_id, text, color, position) VALUES(?,?,?,?)",
                         (user_id, (text or "")[:2000], color if color in COLORS else "yellow", pos)).lastrowid


def update_note(user_id: int, note_id: int, fields: dict) -> bool:
    sets = {}
    if "text" in fields:
        sets["text"] = (fields["text"] or "")[:2000]
    if fields.get("color") in COLORS:
        sets["color"] = fields["color"]
    if not sets:
        return True
    with tx() as c:
        return c.execute(f"UPDATE sticky_notes SET {', '.join(k + '=?' for k in sets)}, updated_at=datetime('now') "
                         "WHERE id=? AND user_id=?", (*sets.values(), note_id, user_id)).rowcount > 0


def delete_note(user_id: int, note_id: int) -> bool:
    with tx() as c:
        return c.execute("DELETE FROM sticky_notes WHERE id=? AND user_id=?", (note_id, user_id)).rowcount > 0
