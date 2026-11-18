"""Business logic: templates, recurring projects, reminders, chat updates."""
import calendar
import os
from datetime import date, timedelta

from . import automations, blink, chat, collab, tpl
from .db import rows, tx

LOOKAHEAD = int(os.getenv("REMINDER_LOOKAHEAD_DAYS", "2"))
STATUS_LABEL = {"todo": "To do", "in_progress": "In progress", "blocked": "Blocked", "done": "Done"}


# ---------- projects & templates ----------

def create_project_from_template(template_id: int, name: str, start: date, recurrence_id=None,
                                 assignee_overrides: dict | None = None) -> int:
    """New project from a template: tasks, subtasks, checklist items and 'waiting on' links."""
    pid = tpl.create_project(template_id, name, start, recurrence_id, assignee_overrides)
    notify_new_assignments(pid)
    return pid


def save_project_as_template(project_id: int, name: str) -> int:
    """'This project went well — reuse its structure next time.'"""
    return tpl.save_from_project(project_id, name)


def run_recurrences(today: date | None = None) -> list[int]:
    """Create this month's project for each active recurrence once its day arrives."""
    today = today or date.today()
    period = today.strftime("%Y-%m")
    created = []
    with tx() as c:
        due = rows(c.execute(
            "SELECT * FROM recurrences WHERE active=1 AND (last_period IS NULL OR last_period < ?)", (period,)))
    for r in due:
        day = min(r["day_of_month"], calendar.monthrange(today.year, today.month)[1])
        if today.day < day:
            continue
        start = today.replace(day=day)
        name = r["name_pattern"].format(month=start.strftime("%B"), year=start.year, mm=start.strftime("%m"))
        created.append(create_project_from_template(r["template_id"], name, start, recurrence_id=r["id"]))
        with tx() as c:
            c.execute("UPDATE recurrences SET last_period=? WHERE id=?", (period, r["id"]))
    return created


# ---------- task updates ----------

def apply_update(task_id: int, user_id: int | None, source: str, status: str | None, note: str | None) -> dict:
    with tx() as c:
        old = c.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()["status"]
        if status:
            c.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        c.execute("INSERT INTO task_updates(task_id, user_id, source, status, note) VALUES(?,?,?,?,?)",
                  (task_id, user_id, source, status, note))
        t = dict(c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        # Auto-complete project when every task is done
        open_left = c.execute("SELECT COUNT(*) FROM tasks WHERE project_id=? AND status!='done'",
                              (t["project_id"],)).fetchone()[0]
        if open_left == 0:
            c.execute("UPDATE projects SET status='done' WHERE id=?", (t["project_id"],))
        elif status and status != "done":  # reopening a task reopens its project
            c.execute("UPDATE projects SET status='active' WHERE id=? AND status='done'", (t["project_id"],))
    automations.on_status_change(task_id, old, status, user_id, note)
    return t


def open_tasks_for(user_id: int) -> list[dict]:
    with tx() as c:
        return rows(c.execute(
            """SELECT t.*, p.name AS project_name FROM tasks t JOIN projects p ON p.id=t.project_id
               WHERE t.assignee_id=? AND t.status!='done' AND p.status='active'
               ORDER BY t.due_date IS NULL, t.due_date, t.id""", (user_id,)))


def fmt_task(t: dict) -> str:
    flag = ""
    if t.get("due_date"):
        d = date.fromisoformat(t["due_date"])
        flag = " ⚠️ OVERDUE" if d < date.today() else (" (due today)" if d == date.today() else "")
        flag = f" – due {d:%b %d}{flag}"
    blockers = automations.open_blockers(t["id"]) if t.get("status") != "done" else []
    wait = f" 🔒 waiting on {', '.join('#' + str(b['id']) for b in blockers)}" if blockers else ""
    return f"#{t['id']} {t['title']} [{STATUS_LABEL[t['status']]}] · {t['project_name']}{flag}{wait}"


# ---------- Blink messaging ----------

def notify_new_assignments(project_id: int):
    with tx() as c:
        p = c.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
        grouped = rows(c.execute(
            """SELECT u.blink_user_id, u.name, t.id, t.title, t.due_date, t.status, ? AS project_name
               FROM tasks t JOIN users u ON u.id=t.assignee_id
               WHERE t.project_id=? AND u.blink_user_id IS NOT NULL ORDER BY u.id, t.position""",
            (p["name"], project_id)))
    by_user: dict[str, list] = {}
    for t in grouped:
        by_user.setdefault(t["blink_user_id"], []).append(t)
    for buid, ts in by_user.items():
        lines = "\n".join(fmt_task(t) for t in ts)
        blink.send_dm(buid, f"📋 New project *{p['name']}* — you've been assigned:\n{lines}\n\n{chat.help_text()}")


def send_reminders(today: date | None = None, force: bool = False) -> int:
    """Daily nudge: tasks due within LOOKAHEAD days or overdue, at most once per day per task."""
    today = today or date.today()
    horizon = (today + timedelta(days=LOOKAHEAD)).isoformat()
    with tx() as c:
        due = rows(c.execute(
            """SELECT t.*, p.name AS project_name, u.blink_user_id FROM tasks t
               JOIN projects p ON p.id=t.project_id JOIN users u ON u.id=t.assignee_id
               WHERE t.status!='done' AND p.status='active' AND u.blink_user_id IS NOT NULL
                 AND t.due_date IS NOT NULL AND t.due_date<=?
                 AND (? OR t.last_reminded_on IS NULL OR t.last_reminded_on<?)
                 AND NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks b ON b.id=d.depends_on_id
                                 WHERE d.task_id=t.id AND b.status!='done')
               ORDER BY t.due_date""", (horizon, int(force), today.isoformat())))
    by_user: dict[str, list] = {}
    for t in due:
        by_user.setdefault(t["blink_user_id"], []).append(t)
    for buid, ts in by_user.items():
        lines = "\n".join(fmt_task(t) for t in ts)
        blink.send_dm(buid, f"⏰ Task check-in — how are these going?\n{lines}\n\n"
                            "Reply here with an update (e.g. `done 12` or just tell me what's happening).")
        with tx() as c:
            c.executemany("UPDATE tasks SET last_reminded_on=? WHERE id=?", [(today.isoformat(), t["id"]) for t in ts])
    return sum(map(len, by_user.values()))


def handle_inbound(blink_user_id: str, text: str) -> str:
    """Process a chat message from Blink; reply is DM'd back and returned."""
    blink.log_inbound(blink_user_id, text)
    with tx() as c:
        u = c.execute("SELECT * FROM users WHERE blink_user_id=?", (blink_user_id,)).fetchone()
    if not u:
        url = os.getenv("PUBLIC_URL", "").rstrip("/")
        reply = ("I don't recognize your Blink account yet. Sign in to the task app once"
                 + (f" at {url}" if url else "") + " with your work Google account and I'll link you automatically.")
        blink.send_dm(blink_user_id, reply)
        return reply

    mine = open_tasks_for(u["id"])
    parsed = chat.parse_rules(text) or chat.parse_with_claude(text, mine)

    if parsed is None:
        reply = "Sorry, I couldn't match that to a task.\n" + chat.help_text()
    elif parsed["intent"] == "help":
        reply = chat.help_text()
    elif parsed["intent"] == "comment":
        tid, body = parsed["task_id"], parsed["body"]
        with tx() as c:
            exists = c.execute("SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()
        if not exists:
            reply = f"I couldn't find task #{tid}."
        else:
            r = collab.add_comment(tid, u["id"], body, source="blink")
            reply = f"💬 Comment added to #{tid} {exists['title']}" + (
                f" (notified {', '.join(r['mentioned'])})" if r["mentioned"] else "")
    elif parsed["intent"] == "list":
        reply = ("Your open tasks:\n" + "\n".join(map(fmt_task, mine))) if mine else "🎉 You have no open tasks."
    else:
        allowed = {t["id"] for t in mine} | _recently_done_ids(u["id"])
        done_lines, skipped = [], []
        for up in parsed["updates"]:
            if up["task_id"] not in allowed:
                skipped.append(up["task_id"])
                continue
            t = apply_update(up["task_id"], u["id"], "blink", up.get("status"), up.get("note"))
            done_lines.append(f"✅ #{t['id']} {t['title']} → {STATUS_LABEL[t['status']]}"
                              + (f" (note: {up['note']})" if up.get("note") else ""))
        reply = "\n".join(done_lines) or parsed.get("reply") or "No changes made."
        if skipped:
            reply += f"\n(Skipped #{', #'.join(map(str, skipped))} — not assigned to you.)"
        if parsed.get("reply") and done_lines:
            reply = parsed["reply"] + "\n" + reply
    blink.send_dm(blink_user_id, reply)
    return reply


def _recently_done_ids(user_id: int) -> set[int]:
    with tx() as c:
        return {r[0] for r in c.execute("SELECT id FROM tasks WHERE assignee_id=? AND status='done'", (user_id,))}
