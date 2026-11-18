"""Turn a Blink chat message into task status updates.

1. Fast rule-based commands:  "done 12", "12 in progress", "blocked 7 waiting on lender",
   "note 5 called the tenant", "list"/"tasks", "help".
2. Anything else -> Claude (if ANTHROPIC_API_KEY is set) maps free text like
   "finished the rent roll, still waiting on the CAM recon from Joe" onto the
   user's open tasks.
"""
import json
import os
import re

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
    "• `note 5 called the tenant`\n• `list` – show your open tasks\n"
    "Or just tell me in plain English what you got done."
)

_STATUS_RE = "|".join(sorted(map(re.escape, STATUS_WORDS), key=len, reverse=True))
CMD_A = re.compile(rf"^\s*({_STATUS_RE})\s*#?(\d+)\s*[:\-]?\s*(.*)$", re.I)  # done 12 note
CMD_B = re.compile(rf"^\s*#?(\d+)\s*[:\-]?\s*({_STATUS_RE})\b\s*[:\-]?\s*(.*)$", re.I)  # 12 done note
CMD_NOTE = re.compile(r"^\s*note\s*#?(\d+)\s*[:\-]?\s*(.+)$", re.I)


def parse_rules(text: str) -> dict | None:
    """Returns {"intent": ..., "updates": [...]} or None if not a command."""
    t = text.strip()
    low = t.lower()
    if low in {"help", "?", "commands"}:
        return {"intent": "help", "updates": []}
    if low in {"list", "tasks", "my tasks", "status"}:
        return {"intent": "list", "updates": []}
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
                    "status": {"type": ["string", "null"], "enum": ["todo", "in_progress", "blocked", "done", None]},
                    "note": {"type": ["string", "null"]},
                },
            },
        },
        "reply": {"type": "string"},
    },
}

SYSTEM = (
    "You turn an employee's chat message into status updates on their assigned tasks. "
    "Only reference task_ids from the provided list. Use status null when the message adds "
    "information without changing status. Put useful details (who/what they're waiting on, "
    "dates) in note. If the message is ambiguous about which task it means, return no updates "
    "and use reply to ask a short clarifying question. Otherwise reply is a one-line friendly confirmation."
)


def parse_with_claude(text: str, open_tasks: list[dict]) -> dict | None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    import anthropic

    client = anthropic.Anthropic()
    task_list = "\n".join(
        f"#{t['id']} [{t['status']}] {t['title']} (project: {t['project_name']}, due {t['due_date']})"
        for t in open_tasks
    )
    try:
        resp = client.beta.messages.create(
            model="claude-opus-5-5",
            max_tokens=2000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": UPDATE_SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": f"My open tasks:\n{task_list}\n\nMy message:\n{text}"}],
        )
    except anthropic.APIError:
        return None
    if resp.stop_reason != "end_turn":
        return None
    raw = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    valid = {t["id"] for t in open_tasks}
    data["updates"] = [u for u in data["updates"] if u["task_id"] in valid]
    data["intent"] = "update"
    return data
