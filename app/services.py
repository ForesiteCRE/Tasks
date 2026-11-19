"""Business logic: templates, recurring projects, reminders, chat updates."""
import calendar
import json
import os
import re
from datetime import date, datetime, timedelta

from . import automations, blink, chat, collab, dashboard, intake, recur, timing, tpl
from .db import rows, tx

LOOKAHEAD = int(os.getenv("REMINDER_LOOKAHEAD_DAYS", "2"))
STATUS_LABEL = {"todo": "To do", "in_progress": "In progress", "blocked": "Blocked", "done": "Done"}


# ---------- projects & templates ----------

def create_project_from_template(template_id: int, name: str, start: date, recurrence_id=None,
                                 assignee_overrides: dict | None = None, team: dict | None = None,
                                 details: dict | None = None) -> int:
    """New project from a template: tasks, subtasks, checklist items, 'waiting on' links and role-based assignees."""
    from . import properties
    pid = tpl.create_project(template_id, name, start, recurrence_id, assignee_overrides, team, details)
    properties.after_project_created(pid, None if recurrence_id else team)
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
        if not recur.due_this_month(r, today):          # quarterly / yearly: only in the right months
            continue
        day = min(r["day_of_month"], calendar.monthrange(today.year, today.month)[1])
        if today.day < day:
            continue
        start = today.replace(day=day)
        name = r["name_pattern"].format(month=start.strftime("%B"), year=start.year, mm=start.strftime("%m"))
        team = json.loads(r["team_json"]) if r.get("team_json") else None
        created.append(create_project_from_template(r["template_id"], name, start, recurrence_id=r["id"], team=team,
                                                    details={"property": r.get("property"), "client": r.get("client")}))
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
        timing.on_status(c, task_id, old, status)
        t = dict(c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        # Auto-complete project when every task is done
        open_left = c.execute("SELECT COUNT(*) FROM tasks WHERE project_id=? AND status!='done'",
                              (t["project_id"],)).fetchone()[0]
        if open_left == 0:
            c.execute("UPDATE projects SET status='done' WHERE id=?", (t["project_id"],))
        elif status and status != "done":  # reopening a task reopens its project
            c.execute("UPDATE projects SET status='active' WHERE id=? AND status='done'", (t["project_id"],))
    from . import blockers
    if old == "blocked" and status and status != "blocked":
        blockers.clear(task_id)
    elif note and (status == "blocked" or (not status and old == "blocked")):
        blockers.note_as_reason(task_id, note)       # e.g. a reason given in Blink
    automations.on_status_change(task_id, old, status, user_id, note)
    if old == "blocked" and status and status != "blocked":
        from . import departments
        departments.reset("dept_blocked", task_id)
    if status and status != old:
        from . import audit
        audit.log(user_id, f"status → {STATUS_LABEL.get(status, status)}", "task", task_id, t["title"],
                  (f"from {STATUS_LABEL.get(old, old)}" + (f" · via Blink" if source == "blink" else "")) + (f" · “{note}”" if note else ""))
    if status == "done" and old != "done":
        from . import fun
        try:
            fun.on_done(task_id, user_id)
        except Exception:
            import logging
            logging.getLogger("fun").exception("win failed")
    if status == "done" and old != "done" and t.get("repeat"):
        t["next_occurrence"] = recur.on_done(task_id)
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
    howto = f"\n   🎓 How-to: {t['tutorial_url']}" if t.get("tutorial_url") and t.get("status") != "done" else ""
    wait += " 🔁" if t.get("repeat") else ""
    return f"#{t['id']} {t['title']} [{STATUS_LABEL[t['status']]}] · {t['project_name']}{flag}{wait}{howto}"


# ---------- Blink messaging ----------

def notify_new_assignments(project_id: int):
    with tx() as c:
        p = c.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
        grouped = rows(c.execute(
            """SELECT u.blink_user_id, u.name, t.id, t.title, t.due_date, t.status, t.tutorial_url, ? AS project_name
               FROM tasks t JOIN users u ON u.id=t.assignee_id
               WHERE t.project_id=? AND u.blink_user_id IS NOT NULL ORDER BY u.id, t.position""",
            (p["name"], project_id)))
    by_user: dict[str, list] = {}
    for t in grouped:
        by_user.setdefault(t["blink_user_id"], []).append(t)
    for buid, ts in by_user.items():
        lines = "\n".join(fmt_task(t) for t in ts)
        from . import notify
        notify.deliver_blink([buid], "assigned", f"📋 New project *{p['name']}* — you've been assigned:\n{lines}\n\n{chat.help_text()}")


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
        from . import notify
        notify.deliver_blink([buid], "checkin", f"⏰ Task check-in — how are these going?\n{lines}\n\n"
                            "Reply here with an update (e.g. `done 12` or just tell me what's happening).")
        with tx() as c:
            c.executemany("UPDATE tasks SET last_reminded_on=? WHERE id=?", [(today.isoformat(), t["id"]) for t in ts])
    return sum(map(len, by_user.values()))


def handle_inbound(blink_user_id: str, text: str, files: list[dict] | None = None) -> str:
    """Process a chat message from Blink; reply is DM'd back and returned."""
    blink.log_inbound(blink_user_id, text or f"📎 {len(files or [])} attachment(s)")
    with tx() as c:
        u = c.execute("SELECT * FROM users WHERE blink_user_id=?", (blink_user_id,)).fetchone()
    if not u:
        url = os.getenv("PUBLIC_URL", "").rstrip("/")
        reply = ("I don't recognize your Blink account yet. Sign in to the task app once"
                 + (f" at {url}" if url else "") + " with your work Google account and I'll link you automatically.")
        blink.send_dm(blink_user_id, reply)
        return reply

    # Personal reminders: "remind me Friday 9am to call the owner"
    if re.match(r"^\s*remind\b", text or "", re.I):
        r = dashboard.parse_reminder(text)
        if not r:
            reply = "Try `remind me 2026-12-01 9:00 call the owner` (or turn on plain-English mode for “remind me Friday 9am …”)."
        elif not r.get("ok"):
            reply = r.get("reply") or "When should I remind you?"
        else:
            dashboard.add_reminder(u["id"], r["text"], r["remind_at"])
            when = datetime.fromisoformat(r["remind_at"])
            reply = f"⏰ Got it — I'll remind you {when:%a %b %d at %-I:%M %p}: {r['text']}"
        blink.send_dm(blink_user_id, reply)
        return reply

    # "add task …" — property and project optional; managers can assign anyone
    from . import newtask
    desc = None if chat.parse_rules(text or "") else newtask.wanted(text or "")
    if desc is not None:
        reply = newtask.handle(dict(u), desc)
        chat.remember(u["id"], text, reply)
        blink.send_dm(blink_user_id, reply)
        return reply

    # Managers can start a project by chatting (document, pasted terms, or "new project for ...").
    # Task commands like "done 12" still work in the middle of that conversation.
    rule = chat.parse_rules(text or "")
    if u["role"] in ("manager", "admin") and rule is None and (
            intake.get_session(u["id"]) or intake.wants_to_start(text or "", bool(files))):
        reply = intake.handle(dict(u), text or "", files)
        blink.send_dm(blink_user_id, reply)
        return reply

    # Questions about the app or the data → LJ, the help assistant (updates like "done 12" keep going to the task parser)
    if rule is None and helpbot_wanted(text or "") and chat.ai_enabled():
        from . import helpbot
        hist = []
        for h in chat.recent(u["id"]):
            if h["user_text"]:
                hist += [{"role": "user", "content": h["user_text"]}, {"role": "assistant", "content": h["reply"] or ""}]
        ans = helpbot.ask(hist + [{"role": "user", "content": text}], dict(u), "blink")["reply"]
        reply = "🙋 **LJ:** " + blink_links(ans)
        chat.remember(u["id"], text, reply)
        blink.send_dm(blink_user_id, reply)
        return reply

    mine = open_tasks_for(u["id"])
    history = chat.recent(u["id"])
    parsed = rule or chat.parse_with_claude(text, mine, history, _referenced_tasks(text, history))

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
    elif parsed["intent"] == "time":
        tid = parsed["task_id"]
        with tx() as c:
            t = c.execute("SELECT title, assignee_id, estimate_minutes FROM tasks WHERE id=?", (tid,)).fetchone()
        if not t or t["assignee_id"] != u["id"]:
            reply = f"Task #{tid} isn't assigned to you."
        elif not parsed["minutes"]:
            reply = "I couldn't read the time — try `time 12 1h30 waited on owner` or `time 12 45m`."
        else:
            timing.set_times(tid, actual=parsed["minutes"], notes=parsed["note"] if parsed["note"] else None)
            est = f" (estimate {timing.fmt(t['estimate_minutes'])})" if t["estimate_minutes"] else ""
            reply = f"⏱ Logged {timing.fmt(parsed['minutes'])} on #{tid} {t['title']}{est}" + (" with your note." if parsed["note"] else ".")
    elif parsed["intent"] == "list":
        reply = ("Your open tasks:\n" + "\n".join(map(fmt_task, mine))) if mine else "🎉 You have no open tasks."
    else:
        allowed = {t["id"] for t in mine} | _recently_done_ids(u["id"])
        commentable = allowed | {t["id"] for t in _referenced_tasks(text, history)}
        done_lines, skipped = [], []
        for up in parsed["updates"]:
            if up["task_id"] not in allowed:
                skipped.append(up["task_id"])
                continue
            t = apply_update(up["task_id"], u["id"], "blink", up.get("status"), up.get("note"))
            nxt = t.get("next_occurrence")
            if up.get("actual_minutes"):
                timing.set_times(t["id"], actual=up["actual_minutes"], notes=up.get("time_note"))
                t = {**t, "actual_minutes": up["actual_minutes"]}
            elif t["status"] == "done":
                with tx() as c:
                    t = dict(c.execute("SELECT * FROM tasks WHERE id=?", (t["id"],)).fetchone())
            done_lines.append(f"✅ #{t['id']} {t['title']} → {STATUS_LABEL[t['status']]}"
                              + (f" (note: {up['note']})" if up.get("note") else ""))
            if nxt:
                done_lines.append(f"🔁 Next one scheduled: #{nxt['id']} due {date.fromisoformat(nxt['due_date']):%a %b %d}")
        for cm in parsed.get("comments") or []:
            if cm["task_id"] not in commentable:
                skipped.append(cm["task_id"])
                continue
            r = collab.add_comment(cm["task_id"], u["id"], cm["body"], source="blink")
            with tx() as c:
                title = c.execute("SELECT title FROM tasks WHERE id=?", (cm["task_id"],)).fetchone()["title"]
            done_lines.append(f"💬 Comment added to #{cm['task_id']} {title}: “{cm['body']}”"
                              + (f" (notified {', '.join(r['mentioned'])})" if r["mentioned"] else ""))
        from . import blockers as _bl
        with tx() as c:
            ask = [r["id"] for up in parsed["updates"] if up.get("status") == "blocked" and up["task_id"] in allowed
                   for r in [c.execute("SELECT id, blocked_kind FROM tasks WHERE id=?", (up["task_id"],)).fetchone()] if r and not r["blocked_kind"]]
        if ask:
            done_lines.append(f"⛔ What is #{ask[0]} waiting on — a person (owner, vendor, tenant…), another task, or something else? "
                              "Just reply and I'll note it (add a date if you want a follow-up reminder).")
        reply = "\n".join(done_lines) or parsed.get("reply") or "No changes made."
        need_time = [up["task_id"] for up in parsed["updates"] if up.get("status") == "done" and not up.get("actual_minutes")
                     and up["task_id"] in allowed]
        with tx() as c:
            need_time = [i for i in need_time if not c.execute(
                "SELECT actual_minutes FROM tasks WHERE id=?", (i,)).fetchone()["actual_minutes"]]
        if need_time:
            reply += (f"\n⏱ How long did it take? Reply `time {need_time[0]} 1h30` (add a note after it if useful).")
        if skipped:
            reply += f"\n(Skipped #{', #'.join(map(str, skipped))} — not assigned to you.)"
        if parsed.get("reply") and done_lines:
            reply = parsed["reply"] + "\n" + reply
    chat.remember(u["id"], text, reply)
    blink.send_dm(blink_user_id, reply)
    return reply


_Q_START = re.compile(r"^\s*(how|what|what's|whats|where|who|who's|whos|when|which|why|can|could|does|do|is|are|should|tell me|show me|"
                      r"find|list|explain|help)\b", re.I)
_UPDATE_WORDS = re.compile(r"\b(done|finished|completed|complete|blocked|started|starting|working on|sent|submitted|waiting on|"
                           r"took|spent|reopen)\b", re.I)


def helpbot_wanted(text: str) -> bool:
    """Is this a question for LJ (the help assistant) rather than a task update?"""
    t = (text or "").strip()
    if not t:
        return False
    if re.match(r"^\s*(hey\s+|hi\s+)?@?l\.?j\.?\b", t, re.I):
        return True                                   # "LJ, how do I…" always goes to the assistant
    if re.search(r"#\d+|^\s*\d+\b", t) and not t.endswith("?"):
        return False                                  # mentions a task number → an update
    if _UPDATE_WORDS.search(t) and not t.endswith("?"):
        return False
    if len(t.split()) < 2 and not t.endswith("?"):
        return False                                  # "list", "help" etc. are Blink commands
    return t.endswith("?") or bool(_Q_START.match(t))


def blink_links(text: str) -> str:
    """LJ's in-app links (#task-12) → web links people can tap in Blink."""
    url = os.getenv("PUBLIC_URL", "").rstrip("/")
    def sub(m):
        return f"{m.group(1)} ({url}/{m.group(2)})" if url else m.group(1)
    return re.sub(r"\[([^\]]+)\]\((#(?:task|project|property|vendor|view)-[\w-]+)\)", sub, text)


def _referenced_tasks(text: str, history: list[dict]) -> list[dict]:
    """Tasks named by #number in this message or the last few chat messages (any assignee) — they can be commented on."""
    blob = " ".join([text or ""] + [(h.get("user_text") or "") + " " + (h.get("reply") or "") for h in history[-4:]])
    ids = {int(x) for x in re.findall(r"#(\d+)", blob)} | {int(x) for x in re.findall(r"^\s*(\d+)\b", text or "")}
    if not ids:
        return []
    with tx() as c:
        q = ",".join("?" * len(ids))
        return rows(c.execute(f"""SELECT t.id, t.title, t.status, p.name AS project_name, u.name AS assignee_name
                                  FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN users u ON u.id=t.assignee_id
                                  WHERE t.id IN ({q})""", list(ids)))


def _recently_done_ids(user_id: int) -> set[int]:
    with tx() as c:
        return {r[0] for r in c.execute("SELECT id FROM tasks WHERE assignee_id=? AND status='done'", (user_id,))}
