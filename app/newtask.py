"""Add a task from Blink: "add task call the city about the sign permit at 777 Sonterra for Taylor by Friday".

Claude reads the message and fills in the title, property, project (only that property's open projects), assignee,
due date and estimate. Property and project are optional — anything else is still a task that can be assigned to
anyone — and the reply says so. Without an API key, the rest of the message becomes the title and a property is
picked up if its name appears in the text.

Same permissions as the web: staff can add tasks for themselves (with or without a property); managers can put
tasks in projects and assign anyone (that person gets a Blink message).
"""
import json
import logging
import re
from datetime import date

from . import blink, chat, team, timing
from .db import rows, tx

log = logging.getLogger("newtask")

CMD = re.compile(r"^\s*(?:add|new|create)\s+(?:a\s+)?(?:task|to-?do)\b\s*[:\-]?\s*(.*)$|^\s*(?:task|to-?do)\s*:\s*(.*)$", re.I | re.S)
TIP = ("💡 Property and project are optional — tasks that don't belong to either can still be added and assigned to anyone. "
       "Mention a property (“at 777 Sonterra”) to file it there.")


def wanted(text: str) -> str | None:
    """The task description if this message asks to add a task, else None."""
    m = CMD.match(text or "")
    if not m:
        return None
    return (m[1] if m[1] is not None else m[2] or "").strip()


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "property_id", "project_id", "assignee_id", "due_date", "estimate"],
    "properties": {
        "title": {"type": "string", "description": "short imperative task title, without the who/when/where words"},
        "property_id": {"type": "integer", "description": "id from the property list if one is mentioned or clearly implied, else 0"},
        "project_id": {"type": "integer", "description": "id of one of THAT property's open projects if the task clearly belongs to it, else 0"},
        "assignee_id": {"type": "integer", "description": "id from the people list if someone is named, else 0 (= the sender)"},
        "due_date": {"type": "string", "description": "YYYY-MM-DD if a date/day is given, else empty"},
        "estimate": {"type": "string", "description": "time estimate if given, like 1h30 or 45m, else empty"},
    },
}


def _context(c) -> tuple[list[dict], list[dict], list[dict]]:
    props = rows(c.execute("SELECT id, name, address FROM properties ORDER BY name"))
    projs = rows(c.execute("""SELECT id, name, property_id FROM projects
                              WHERE is_general=0 AND status='active' ORDER BY name"""))
    people = rows(c.execute("SELECT id, name FROM users ORDER BY name"))
    return props, projs, people


def _read(desc: str, user: dict, props, projs, people) -> dict | None:
    if not chat.ai_enabled():
        return None
    import anthropic
    ctx = ("Properties:\n" + "\n".join(f"{p['id']}: {p['name']} — {p['address']}" for p in props) +
           "\n\nOpen projects (property id in brackets):\n" + "\n".join(f"{p['id']}: {p['name']} [{p['property_id'] or '-'}]" for p in projs) +
           "\n\nPeople:\n" + "\n".join(f"{p['id']}: {p['name']}" for p in people))
    try:
        resp = anthropic.Anthropic().beta.messages.create(
            model=chat.MODEL, max_tokens=600, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=("You turn a chat message into one new task for a commercial property management team. "
                    "Pick ids only from the lists given; use 0 when nothing fits. Don't guess a property or project that "
                    "isn't mentioned or clearly implied — many tasks (office errands, admin, follow-ups) have neither."),
            messages=[{"role": "user", "content": f"{ctx}\n\nToday: {date.today():%A %Y-%m-%d}\nSender: {user['name']}\n\nMessage: {desc}"}])
        if resp.stop_reason != "end_turn":
            return None
        return json.loads(next(b.text for b in resp.content if b.type == "text"))
    except Exception:
        log.exception("add-task read failed")
        return None


def _match_property(desc: str, props) -> int:
    low = desc.lower()
    for p in sorted(props, key=lambda p: -len(p["name"])):
        name = p["name"].lower()
        if name in low or name.split(",")[0] in low:
            return p["id"]
    return 0


def handle(user: dict, desc: str) -> str:
    if not desc:
        return ("What's the task? e.g. `add task call the city about the sign permit at 777 Sonterra for Taylor by Friday`\n" + TIP)
    with tx() as c:
        props, projs, people = _context(c)
    d = _read(desc, user, props, projs, people) or {"title": desc, "property_id": _match_property(desc, props),
                                                    "project_id": 0, "assignee_id": 0, "due_date": "", "estimate": ""}
    mgr = user["role"] in ("manager", "admin")
    prop = next((p for p in props if p["id"] == d.get("property_id")), None)
    proj = next((p for p in projs if p["id"] == d.get("project_id") and (not prop or p["property_id"] == prop["id"])), None)
    who = next((p for p in people if p["id"] == d.get("assignee_id")), None) or {"id": user["id"], "name": user["name"]}
    notes = []
    if not mgr:
        if proj:
            notes.append(f"(Only managers can add tasks to projects — I put it on {prop['name'] if prop else 'your list'} instead.)")
            proj = None
        if who["id"] != user["id"]:
            notes.append(f"(Only managers can assign tasks to others — it's on your list; ask a manager to reassign it to {who['name']}.)")
            who = {"id": user["id"], "name": user["name"]}
    if proj and not prop:
        prop = next((p for p in props if p["id"] == proj["property_id"]), None)
    due = None
    try:
        due = date.fromisoformat((d.get("due_date") or "")[:10])
    except ValueError:
        pass
    title = (d.get("title") or desc).strip()[:200]
    with tx() as c:
        tid = c.execute("""INSERT INTO tasks(project_id, title, assignee_id, due_date, position, property_id)
                           VALUES(?,?,?,?,999,?)""",
                        (proj["id"] if proj else team.general_project_id(), title, who["id"],
                         due and due.isoformat(), prop and prop["id"])).lastrowid
        target = c.execute("SELECT blink_user_id FROM users WHERE id=?", (who["id"],)).fetchone()
    from . import watchers
    watchers.auto_watch(tid, user["id"], who["id"])
    if d.get("estimate"):
        mins = timing.parse_duration(d["estimate"])
        if mins:
            timing.set_times(tid, estimate=mins)
    if who["id"] != user["id"] and target and target["blink_user_id"]:
        from . import notify
        notify.deliver(who["id"], "assigned", f"📌 New task assigned by {user['name']}: #{tid} {title}"
                       + (f" – due {due:%b %d}" if due else "") + (f" · 🏢 {prop['name']}" if prop else ""))
    bits = [f"🏢 {prop['name']}" if prop else None, f"📁 {proj['name']}" if proj else None,
            f"👤 {'you' if who['id'] == user['id'] else who['name']}", f"due {due:%a %b %d}" if due else "no due date"]
    reply = f"📌 Added #{tid} {title}\n" + " · ".join(b for b in bits if b)
    if prop and not proj and mgr:
        n = sum(1 for p in projs if p["property_id"] == prop["id"])
        if n:
            reply += f"\n(Not in a project — {prop['name']} has {n} open project{'s' if n > 1 else ''}; you can move it from the task page.)"
    if notes or not prop:
        reply += "\n" + "\n".join(notes + [TIP])
    return reply
