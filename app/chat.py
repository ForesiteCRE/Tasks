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
CMD_COMMENT = re.compile(r"^\s*comment\s*#?(\d+)\s*[:\-]?\s*(.+)$", re.I | re.S)


def parse_rules(text: str) -> dict | None:
    """Returns {"intent": ..., "updates": [...]} or None if not a command."""
    t = text.strip()
    low = t.lower()
    if low in {"help", "?", "commands"}:
        return {"intent": "help", "updates": []}
    if low in {"list", "tasks", "my tasks", "status"}:
        return {"intent": "list", "updates": []}
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
    "required": ["updates", "reply"],
    "properties": {
        "updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task_id", "status", "note"],
                "properties": {
                    "task_id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["unchanged", "todo", "in_progress", "blocked", "done"]},
                    "note": {"type": "string"},
                },
            },
        },
        "reply": {"type": "string"},
    },
}

SYSTEM = """You read a chat message an employee sent to their team's task bot and turn it into status updates on the tasks assigned to them.

Rules:
- Only use task_ids from the employee's task list. Never invent one.
- status: "done" when they say it's finished/sent/submitted/wrapped up; "in_progress" when started or working on it; "blocked" when waiting on someone or something; "todo" only if they say it needs to be redone or reopened; otherwise "unchanged".
- note: a short factual note worth keeping (who/what they're waiting on, dates, amounts). Empty string if nothing to add.
- One message can update several tasks. Match on meaning, not exact wording ("rent roll" matches "Reconcile rent roll").
- If it's unclear which task they mean, or the message isn't a task update (a question, small talk), return no updates and use reply to answer briefly or ask one short clarifying question that names the candidate task numbers.
- Otherwise reply is one short, friendly confirmation line. No emoji spam."""


def parse_with_claude(text: str, open_tasks: list[dict]) -> dict | None:
    """Map free text onto the user's open tasks. Returns {"intent","updates","reply"} or None."""
    if not ai_enabled() or not open_tasks:
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
            messages=[{"role": "user", "content": f"My open tasks:\n{task_list}\n\nMy message:\n{text}"}],
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
        if status or note:
            updates.append({"task_id": u["task_id"], "status": status, "note": note})
    return {"intent": "update", "updates": updates, "reply": data.get("reply", "")}
