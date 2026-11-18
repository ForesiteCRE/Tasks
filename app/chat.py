"""Turn a Blink chat message into task status updates.

1. Fast rule-based commands:  "done 12", "12 in progress", "blocked 7 waiting on lender",
   "note 5 called the tenant", "list"/"tasks", "help".
2. Anything else -> Claude (if ANTHROPIC_API_KEY is set) maps free text like
   "finished the rent roll, still waiting on the CAM recon from Joe" onto the
   user's open tasks.
"""
import json
import logging
import os
import re

log = logging.getLogger("chat")

STATUS_WORDS = {
    "done": "done", "complete": "done", "completed": "done", "finished": "done", "x": "done",
    "start": "in_progress", "started": "in_progress", "working": "in_progress",
    "wip": "in_progress", "progress": "in_progress", "in progress": "in_progress",
    "blocked": "blocked", "stuck": "blocked",
    "todo": "todo", "reopen": "todo",
}

HELP = (
    "Reply with an update, e.g.\n"
    "• `done 12`\n• `12 in progress`\n• `blocked 7 waiting on lender`\n"
    "• `note 5 called the tenant`\n• `comment 5 @sam can you send the photos?`\n• `list` – show your open tasks"
)


def help_text() -> str:
    return HELP + ("\nOr just tell me in plain English what you got done." if ai_enabled() else "")

_STATUS_RE = "|".join(sorted(map(re.escape, STATUS_WORDS), key=len, reverse=True))
CMD_A = re.compile(rf"^\s*({_STATUS_RE})\s*#?(\d+)\s*[:\-]?\s*(.*)$", re.I)  # done 12 note
CMD_B = re.compile(rf"^\s*#?(\d+)\s*[:\-]?\s*({_STATUS_RE})\b\s*[:\-]?\s*(.*)$", re.I)  # 12 done note
CMD_NOTE = re.compile(r"^\s*note\s*#?(\d+)\s*[:\-]?\s*(.+)$", re.I)
CMD_TIME = re.compile(r"^\s*(?:time|took|spent)\s*#?(\d+)\s*[:\-]?\s*(.+)$", re.I | re.S)
CMD_COMMENT = re.compile(r"^\s*comment\s*#?(\d+)\s*[:\-]?\s*(.+)$", re.I | re.S)


def parse_rules(text: str) -> dict | None:
    """Returns {"intent": ..., "updates": [...]} or None if not a command."""
    t = text.strip()
    low = t.lower()
    if low in {"help", "?", "commands"}:
        return {"intent": "help", "updates": []}
    if low in {"list", "tasks", "my tasks", "status"}:
        return {"intent": "list", "updates": []}
    if m := CMD_TIME.match(t):
        from .timing import split_duration_and_note
        mins, note = split_duration_and_note(m[2])
        return {"intent": "time", "task_id": int(m[1]), "minutes": mins, "note": note, "updates": []}
    if m := CMD_COMMENT.match(t):
        return {"intent": "comment", "task_id": int(m[1]), "body": m[2].strip(), "updates": []}
    updates = []
    for line in filter(None, (l.strip() for l in re.split(r"[\n;]", t))):
        if m := CMD_NOTE.match(line):
            updates.append({"task_id": int(m[1]), "status": None, "note": m[2].strip()})
        elif m := CMD_A.match(line):
            updates.append({"task_id": int(m[2]), "status": STATUS_WORDS[m[1].lower()], "note": m[3].strip() or None})
        elif m := CMD_B.match(line):
            updates.append({"task_id": int(m[1]), "status": STATUS_WORDS[m[2].lower()], "note": m[3].strip() or None})
        else:
            return None  # any non-command line -> treat whole message as free text
    return {"intent": "update", "updates": updates} if updates else None


MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")


def ai_enabled() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


# "unchanged" / "" instead of nulls keeps the schema simple for structured outputs.
UPDATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["updates", "comments", "reply"],
    "properties": {
        "updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task_id", "status", "note", "actual_minutes", "time_note"],
                "properties": {
                    "task_id": {"type": "integer"},
                    "actual_minutes": {"type": "integer", "description": "time they say it took, in minutes; 0 if not mentioned"},
                    "time_note": {"type": "string", "description": "why it took that long, if they said; else empty"},
                    "status": {"type": "string", "enum": ["unchanged", "todo", "in_progress", "blocked", "done"]},
                    "note": {"type": "string"},
                },
            },
        },
        "comments": {
            "type": "array",
            "description": "comments to post on a task's discussion thread (visible to the team, @names notify people)",
            "items": {"type": "object", "additionalProperties": False, "required": ["task_id", "body"],
                      "properties": {"task_id": {"type": "integer"}, "body": {"type": "string"}}},
        },
        "reply": {"type": "string"},
    },
}

SYSTEM = """You read a chat message an employee sent to their team's task bot and turn it into status updates on the tasks assigned to them.

Rules:
- Only use task_ids from the employee's task list. Never invent one.
- status: "done" when they say it's finished/sent/submitted/wrapped up; "in_progress" when started or working on it; "blocked" when waiting on someone or something; "todo" only if they say it needs to be redone or reopened; otherwise "unchanged".
- note: a short factual note worth keeping (who/what they're waiting on, dates, amounts). Empty string if nothing to add.
- If they say how long something took ("took about 2 hours", "spent 45 min"), put it in actual_minutes and any reason in time_note; otherwise 0 and "".
- One message can update several tasks. Match on meaning, not exact wording ("rent roll" matches "Reconcile rent roll").
- comments: when they ask to comment / leave a message / "add this to #12" / tell or ask a teammate something on a task, put the exact text in comments (keep any @names). Don't also copy it into note. A comment alone is a complete action — no status change needed.
- A reply to a bot notification (e.g. "@X mentioned you on #141 …" then "I see it" / "on it" / "thanks") is a comment back on that task: put their reply text in comments for that task.
- "Other tasks from this conversation" can only get comments, never status changes or notes.
- Use the earlier conversation to resolve follow-ups. If you just asked which task or what to add, their next message is the answer: act on it (e.g. you asked "what should I add to #141?" and they send text → that text is a comment on #141). Never treat such an answer as a test or small talk.
- If it's unclear which task they mean, or the message isn't a task update (a question, small talk), return no updates and use reply to answer briefly or ask one short clarifying question that names the candidate task numbers.
- Otherwise reply is one short, friendly confirmation line. No emoji spam."""


def parse_with_claude(text: str, open_tasks: list[dict], history: list[dict] | None = None,
                      other_tasks: list[dict] | None = None) -> dict | None:
    """Map free text onto the user's open tasks. Returns {"intent","updates","reply"} or None."""
    other_tasks = [t for t in other_tasks or [] if t["id"] not in {o["id"] for o in open_tasks}]
    if not ai_enabled() or not (open_tasks or other_tasks):
        return None
    import anthropic

    client = anthropic.Anthropic()
    task_list = "\n".join(
        f"#{t['id']} [{t['status']}] {t['title']} (project: {t['project_name']}, due {t['due_date'] or 'none'})"
        for t in open_tasks)
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=2000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": UPDATE_SCHEMA}},
            system=SYSTEM,
            messages=_with_history(history, f"My open tasks:\n{task_list or '(none)'}\n\n" + (
                "Other tasks from this conversation (not assigned to me — I can only comment on these):\n" + "\n".join(
                    f"#{t['id']} [{t['status']}] {t['title']} (project: {t['project_name']}, assigned to {t.get('assignee_name') or 'nobody'})"
                    for t in other_tasks) + "\n\n" if other_tasks else "") + f"My message:\n{text}"),
        )
    except anthropic.APIError as e:
        log.warning("Claude parse failed: %s", e)
        return None
    if resp.stop_reason != "end_turn":
        log.warning("Claude stopped with %s", resp.stop_reason)
        return None
    raw = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    valid = {t["id"] for t in open_tasks}
    updates = []
    for u in data.get("updates", []):
        if u.get("task_id") not in valid:
            continue
        status = None if u.get("status") in (None, "", "unchanged") else u["status"]
        note = (u.get("note") or "").strip() or None
        mins = u.get("actual_minutes") or 0
        if status or note or mins:
            updates.append({"task_id": u["task_id"], "status": status, "note": note,
                            "actual_minutes": mins if mins > 0 else None,
                            "time_note": (u.get("time_note") or "").strip() or None})
    commentable = valid | {t["id"] for t in other_tasks}
    comments = [{"task_id": c["task_id"], "body": c["body"].strip()} for c in data.get("comments") or []
                if c.get("task_id") in commentable and (c.get("body") or "").strip()]
    return {"intent": "update", "updates": updates, "comments": comments, "reply": data.get("reply", "")}


def _with_history(history, current: str) -> list[dict]:
    """Recent turns (oldest first) so follow-ups like "add it to #141" keep their context."""
    msgs, pending = [], []   # pending: bot messages sent on their own (e.g. "@mentioned you"), not replies
    for h in history or []:
        if h["user_text"]:
            msgs += [{"role": "user", "content": _prefix(pending) + h["user_text"]},
                     {"role": "assistant", "content": h["reply"] or "(ok)"}]
            pending = []
        elif h["reply"]:
            pending.append(h["reply"])
    return msgs + [{"role": "user", "content": _prefix(pending) + current}]


def _prefix(bot_msgs: list[str]) -> str:
    return "".join(f"[The bot sent me this message: {m}]\n\n" for m in bot_msgs)


# ---------- short conversation memory (per person, last 30 minutes) ----------

def init():
    from .db import tx
    with tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS chat_memory(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                     user_text TEXT, reply TEXT, at TEXT NOT NULL DEFAULT (datetime('now')))""")


def remember(user_id: int, user_text: str, reply: str):
    from .db import tx
    with tx() as c:
        c.execute("INSERT INTO chat_memory(user_id, user_text, reply) VALUES(?,?,?)", (user_id, (user_text or "")[:4000], (reply or "")[:4000]))
        c.execute("DELETE FROM chat_memory WHERE at < datetime('now','-1 day')")


def recent(user_id: int, limit: int = 6) -> list[dict]:
    from .db import rows, tx
    with tx() as c:
        return list(reversed(rows(c.execute(
            "SELECT user_text, reply FROM chat_memory WHERE user_id=? AND at >= datetime('now','-30 minutes') ORDER BY id DESC LIMIT ?",
            (user_id, limit)))))
