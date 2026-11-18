"""Start a project from a document (management agreement, listing agreement, ...) by chatting.

A manager sends the bot a document (or pastes its text, or says "new project for ...").
Claude reads it, fills a draft (name, property, client, start date, template, team), asks for whatever
is missing ("Who will be the Property Manager?"), shows a summary, and creates the project only after
the manager explicitly confirms. One session per person, kept in the database between messages.
"""
import base64
import io
import json
import logging
import os
import re
import zipfile
from datetime import date, datetime, timedelta

import httpx

from . import blink, team
from .chat import MODEL, ai_enabled
from .db import rows, tx

log = logging.getLogger("intake")
SESSION_HOURS = 48
MAX_DOC_BYTES = 25 * 1024 * 1024

START_RE = re.compile(
    r"\b(management agreement|listing agreement|new (project|property|listing|client|account)|"
    r"(start|set ?up|kick ?off|onboard|create)\b.{0,40}\b(project|property|listing))", re.I)
CANCEL_RE = re.compile(r"^\s*(cancel|stop|never ?mind|abort)\b", re.I)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS intake_sessions (
    user_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def init():
    with tx() as c:
        c.executescript(SCHEMA_SQL)


# ---------- session storage ----------

def get_session(user_id: int) -> dict | None:
    with tx() as c:
        r = c.execute("SELECT state, updated_at FROM intake_sessions WHERE user_id=?", (user_id,)).fetchone()
    if not r or datetime.fromisoformat(r["updated_at"]) < datetime.now() - timedelta(hours=SESSION_HOURS):
        return None
    return json.loads(r["state"])


def save_session(user_id: int, state: dict):
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO intake_sessions(user_id, state, updated_at) VALUES(?,?,?)",
                  (user_id, json.dumps(state), datetime.now().isoformat()))


def end_session(user_id: int):
    with tx() as c:
        c.execute("DELETE FROM intake_sessions WHERE user_id=?", (user_id,))


# ---------- documents ----------

def download_blink_file(attachment_id: str) -> tuple[bytes, str] | None:
    """Try to fetch a chat attachment. Blink doesn't document this for chat files yet, so try the
    feed-attachment URL with each token we have. Returns (bytes, content_type) or None."""
    tokens = []
    try:
        tokens.append(blink._access_token())
    except Exception:
        pass
    if blink.LEGACY_TOKEN:
        tokens.append(blink.LEGACY_TOKEN)
    for tok in tokens:
        try:
            r = httpx.get(f"{blink.API}/file/{attachment_id}", headers={"Authorization": f"Bearer {tok}"},
                          timeout=60, follow_redirects=True)
        except httpx.HTTPError as e:
            log.warning("attachment %s download error: %s", attachment_id, e)
            continue
        if r.status_code == 200 and r.content and len(r.content) <= MAX_DOC_BYTES:
            return r.content, r.headers.get("content-type", "").split(";")[0]
        log.warning("attachment %s download -> %s", attachment_id, r.status_code)
    return None


def _sniff(data: bytes, content_type: str, filename: str = "") -> str:
    name = filename.lower()
    if data[:4] == b"%PDF" or "pdf" in content_type or name.endswith(".pdf"):
        return "pdf"
    if data[:2] == b"PK" and (b"word/" in data[:4000] or name.endswith(".docx")):
        return "docx"
    if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or content_type.startswith("image/"):
        return "image"
    return "text"


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


def document_block(data: bytes, content_type: str = "", filename: str = "") -> dict:
    """Turn raw bytes into a Claude content block (PDF/image natively, Word/text as text)."""
    kind = _sniff(data, content_type, filename)
    if kind == "pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                               "data": base64.b64encode(data).decode()}}
    if kind == "image":
        mt = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        return {"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(data).decode()}}
    text = _docx_text(data) if kind == "docx" else data.decode("utf8", "ignore")
    return {"type": "text", "text": f"<document name=\"{filename or 'attachment'}\">\n{text}\n</document>"}


# ---------- the Claude turn ----------

DRAFT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["project_name", "property", "client", "start_date", "template_id", "team", "notes"],
    "properties": {
        "project_name": {"type": "string"},
        "property": {"type": "string"},
        "client": {"type": "string"},
        "start_date": {"type": "string", "description": "YYYY-MM-DD or empty if unknown"},
        "template_id": {"type": "integer", "description": "0 = blank project / not chosen yet"},
        "team": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["role_id", "user_id"],
            "properties": {"role_id": {"type": "integer"}, "user_id": {"type": "integer"}}}},
        "notes": {"type": "string", "description": "Key terms worth keeping on the project summary"},
    },
}
TURN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["draft", "reply", "asking_to_confirm", "user_confirmed", "cancelled"],
    "properties": {
        "draft": DRAFT_SCHEMA,
        "reply": {"type": "string"},
        "asking_to_confirm": {"type": "boolean"},
        "user_confirmed": {"type": "boolean"},
        "cancelled": {"type": "boolean"},
    },
}

SYSTEM = """You help a commercial real estate brokerage/property-management team start projects in their task system, by chat (Blink).

You keep a draft project and talk with a manager until it's complete, then they confirm and the system creates it.

Draft fields:
- project_name: short, e.g. "100 Elm St – Management Takeover".
- property: name and/or address. client: owner/client entity.
- start_date: when work starts (e.g. management commencement / effective date), YYYY-MM-DD. Resolve relative dates ("next January" = the next January 1 after today) and say what you assumed.
- template_id: the best-fitting checklist template from the list (0 = blank project). Suggest one; let them change it.
- team: role_id -> user_id, ONLY using ids from the lists given. Never invent people. Keep roles already filled unless they change them. Role defaults are pre-filled; mention them so they can change them.
- notes: 2-6 short lines of key terms useful to the team (fees, term, reporting deadlines, notable obligations, owner contacts). No legal advice.

How to talk:
- First turn with a document: say in 2-4 lines what you found, then ask about what's missing.
- Ask about at most 2-3 things per message, the most important first. Always ask about roles the chosen template assigns tasks to that nobody fills yet (e.g. "Who will be the Property Manager?").
- Match names people type to the people list loosely (first names, nicknames). If ambiguous, ask.
- When nothing essential is missing (name, start date, and someone for each role the template needs, or the user said to leave it), send a compact summary and ask "Create it?" with asking_to_confirm=true.
- user_confirmed=true ONLY if your previous message asked to confirm and the user clearly agreed now (yes / create it / go ahead). Any change request means not confirmed.
- cancelled=true if they want to stop.
- Blink chat: short lines, simple **bold**, no tables. Never claim the project exists before confirmation."""


def _context() -> str:
    today = date.today()
    with tx() as c:
        people = rows(c.execute("SELECT id, name, email FROM users ORDER BY name"))
        tpls = rows(c.execute("SELECT id, name, description FROM templates ORDER BY name"))
        for t in tpls:
            t["roles_needed"] = sorted({r[0] for r in c.execute(
                "SELECT DISTINCT default_role_id FROM template_items WHERE template_id=? AND default_role_id IS NOT NULL",
                (t["id"],))})
            t["items"] = [r[0] for r in c.execute(
                "SELECT title FROM template_items WHERE template_id=? AND parent_item_id IS NULL ORDER BY position LIMIT 12",
                (t["id"],))]
    roles = [{"id": r["id"], "name": r["name"], "default_user_id": r["default_user_id"]} for r in team.list_roles()]
    return json.dumps({"today": today.isoformat(), "people": people, "roles": roles, "templates": tpls}, indent=1)


def _empty_draft() -> dict:
    defaults = [{"role_id": r["id"], "user_id": r["default_user_id"]} for r in team.list_roles() if r["default_user_id"]]
    return {"project_name": "", "property": "", "client": "", "start_date": "", "template_id": 0,
            "team": defaults, "notes": ""}


def claude_turn(state: dict, user_text: str, doc_block: dict | None = None) -> dict | None:
    if not ai_enabled():
        return None
    import anthropic
    client = anthropic.Anthropic()
    content = []
    if doc_block:
        content.append(doc_block)
    content.append({"type": "text", "text":
                    f"<context>\n{_context()}\n</context>\n<current_draft>\n{json.dumps(state['draft'])}\n</current_draft>\n"
                    f"<your_previous_message_asked_to_confirm>{str(state.get('asking_to_confirm', False)).lower()}"
                    f"</your_previous_message_asked_to_confirm>\n<user_message>\n{user_text or '(sent a document)'}\n</user_message>"})
    messages = state.get("history", [])[-8:] + [{"role": "user", "content": content}]
    try:
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=4000, system=SYSTEM, messages=messages,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": TURN_SCHEMA}},
        )
    except anthropic.APIError as e:
        log.warning("intake Claude call failed: %s", e)
        return None
    if resp.stop_reason != "end_turn":
        log.warning("intake stopped: %s", resp.stop_reason)
        return None
    try:
        out = json.loads(next(b.text for b in resp.content if b.type == "text"))
    except (StopIteration, json.JSONDecodeError):
        return None
    # Keep a light text-only history (the document itself isn't resent; its facts live in the draft).
    state.setdefault("history", []).extend([
        {"role": "user", "content": user_text or "(sent a document)"},
        {"role": "assistant", "content": out["reply"]}])
    return out


def _validate(draft: dict) -> dict:
    """Drop anything that doesn't point at real templates/roles/people."""
    with tx() as c:
        people = {r[0] for r in c.execute("SELECT id FROM users")}
        tpls = {r[0] for r in c.execute("SELECT id FROM templates")}
    roles = {r["id"] for r in team.list_roles()}
    d = dict(draft)
    if d.get("template_id") not in tpls:
        d["template_id"] = 0
    d["team"] = [m for m in d.get("team", []) if m.get("role_id") in roles and m.get("user_id") in people]
    try:
        date.fromisoformat(d.get("start_date") or "")
    except ValueError:
        d["start_date"] = ""
    return d


def create_from_draft(d: dict) -> int:
    from . import services
    start = date.fromisoformat(d["start_date"]) if d.get("start_date") else date.today()
    details = {"property": d.get("property") or None, "client": d.get("client") or None, "description": d.get("notes") or None}
    team_map = {str(m["role_id"]): m["user_id"] for m in d.get("team", [])}
    name = d.get("project_name") or d.get("property") or "New project"
    if d.get("template_id"):
        return services.create_project_from_template(d["template_id"], name, start, team=team_map, details=details)
    with tx() as c:
        pid = c.execute("INSERT INTO projects(name,start_date,property,client,description) VALUES(?,?,?,?,?)",
                        (name, start.isoformat(), details["property"], details["client"], details["description"])).lastrowid
    team.save_team(pid, team.resolve_team(team_map))
    return pid


# ---------- entry points ----------

def wants_to_start(text: str, has_files: bool) -> bool:
    return has_files or bool(START_RE.search(text or ""))


def handle(user: dict, text: str, files: list[dict] | None = None) -> str:
    """One chat turn for a manager. Returns the reply to send."""
    if not ai_enabled():
        return ("Starting projects from chat needs plain-English mode (an Anthropic API key in Railway). "
                "Meanwhile you can create it on the web app.")
    if CANCEL_RE.match(text or ""):
        end_session(user["id"])
        return "OK, cancelled — no project was created."

    state = get_session(user["id"]) or {"draft": _empty_draft(), "history": []}
    doc = None
    for f in files or []:
        got = download_blink_file(f.get("attachment_id", ""))
        if got:
            doc = document_block(*got, filename=f.get("caption", ""))
            break
    if files and not doc:
        save_session(user["id"], state)
        url = os.getenv("PUBLIC_URL", "").rstrip("/")
        return ("I can't open attachments sent in Blink chat yet (Blink's API doesn't hand them over). Either:\n"
                "• paste the key parts of the agreement here (property, owner, start date, fees), or\n"
                f"• upload it on the web app{(' at ' + url) if url else ''} → Projects → 📄 From document.")

    out = claude_turn(state, text, doc)
    if not out:
        return "Sorry — I couldn't process that just now. Try again, or create the project on the web app."
    state["draft"] = _validate(out["draft"])

    if out["cancelled"]:
        end_session(user["id"])
        return out["reply"] or "OK, cancelled."
    if out["user_confirmed"] and state.get("asking_to_confirm"):
        pid = create_from_draft(state["draft"])
        with tx() as c:
            c.execute("UPDATE projects SET created_by=? WHERE id=?", (user["id"], pid))
        end_session(user["id"])
        with tx() as c:
            n = c.execute("SELECT COUNT(*) FROM tasks WHERE project_id=?", (pid,)).fetchone()[0]
        url = os.getenv("PUBLIC_URL", "").rstrip("/")
        return (f"✅ Created **{state['draft']['project_name'] or 'the project'}** with {n} task{'s' if n != 1 else ''}. "
                "Everyone assigned has been messaged." + (f"\n{url}" if url else ""))
    state["asking_to_confirm"] = bool(out["asking_to_confirm"])
    save_session(user["id"], state)
    return out["reply"]


def extract_for_web(data: bytes, content_type: str, filename: str) -> dict:
    """Web upload: read the document once and return a pre-filled draft (no conversation)."""
    if not ai_enabled():
        raise RuntimeError("Reading documents needs an ANTHROPIC_API_KEY.")
    state = {"draft": _empty_draft(), "history": []}
    out = claude_turn(state, "Fill the draft from this document. Reply with a 2-4 line summary of what you found and "
                             "what still needs choosing.", document_block(data, content_type, filename))
    if not out:
        raise RuntimeError("Couldn't read that document.")
    return {"draft": _validate(out["draft"]), "summary": out["reply"]}
