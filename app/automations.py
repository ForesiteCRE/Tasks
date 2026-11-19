"""Automations: Blink messages triggered by task events.

Rules (each can be switched on/off from Reports & backups → Automations):
  your_turn       – a task's last blocking dependency is done → message its assignee
  blocked         – a task is marked Blocked → message managers (with the note)
  overdue         – a task is N+ days overdue → message managers once (daily check)
  project_done    – every task in a project is done → message managers
  comment_assignee– someone else comments on your task → message the assignee
"""
import logging
from datetime import date, timedelta

from . import blink
from .db import get_setting, rows, set_setting, tx

log = logging.getLogger("automations")

DEFAULTS = {"your_turn": "1", "blocked": "1", "overdue_days": "2", "project_done": "1", "comment_assignee": "1"}


def settings() -> dict:
    from .departments import DEFAULTS as DEPT
    return {k: get_setting(f"automation.{k}") or v for k, v in {**DEFAULTS, **DEPT}.items()}


def update_settings(new: dict):
    from .departments import DEFAULTS as DEPT
    for k, v in new.items():
        if k in DEFAULTS or k in DEPT:
            set_setting(f"automation.{k}", str(v))


def _on(rule: str) -> bool:
    return settings().get(rule, "0") not in ("0", "", "false")


def _managers(exclude_user_id=None) -> list[str]:
    with tx() as c:
        return [r["blink_user_id"] for r in c.execute(
            "SELECT blink_user_id FROM users WHERE role IN ('manager','admin') AND blink_user_id IS NOT NULL AND id IS NOT ?",
            (exclude_user_id,))]


def _task(task_id: int) -> dict:
    with tx() as c:
        return dict(c.execute(
            """SELECT t.*, p.name AS project_name, p.status AS project_status, p.is_general, u.name AS assignee_name,
                      u.blink_user_id AS assignee_blink
               FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN users u ON u.id=t.assignee_id
               WHERE t.id=?""", (task_id,)).fetchone())


def _user_name(user_id) -> str:
    if not user_id:
        return "Someone"
    with tx() as c:
        r = c.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone()
    return r["name"] if r else "Someone"


def _send(recipients, text):
    for buid in dict.fromkeys(recipients):  # de-dupe, keep order
        try:
            blink.send_dm(buid, text)
        except Exception:
            log.exception("automation message to %s failed", buid)


def open_blockers(task_id: int) -> list[dict]:
    with tx() as c:
        return rows(c.execute(
            """SELECT b.id, b.title, b.status, u.name AS assignee_name FROM task_dependencies d
               JOIN tasks b ON b.id=d.depends_on_id LEFT JOIN users u ON u.id=b.assignee_id
               WHERE d.task_id=? AND b.status!='done'""", (task_id,)))


# ---------- event hooks (called from services.apply_update) ----------

def on_status_change(task_id: int, old: str | None, new: str | None, actor_id, note: str | None):
    if not new or new == old:
        return
    t = _task(task_id)
    who = _user_name(actor_id)

    if new == "blocked" and _on("blocked"):
        from .blockers import text as blocked_text
        why = blocked_text(t) or note
        _send(_managers(exclude_user_id=actor_id),
              f"⛔ **Blocked:** #{t['id']} {t['title']} · {t['project_name']}\n"
              f"Assigned to {t['assignee_name'] or 'nobody'} · marked by {who}"
              + (f"\n_{why}_" if why else ""))

    if new == "done":
        if _on("your_turn"):
            _notify_unblocked(task_id, who)
        if t["project_status"] == "done" and not t["is_general"] and _on("project_done"):
            _send(_managers(exclude_user_id=actor_id), f"🎉 **Project complete:** {t['project_name']} — all tasks done.")


def _notify_unblocked(done_task_id: int, who: str):
    """Tasks that were waiting on this one and now have nothing left blocking them → tell the assignee."""
    with tx() as c:
        dependents = rows(c.execute(
            """SELECT t.id FROM task_dependencies d JOIN tasks t ON t.id=d.task_id
               WHERE d.depends_on_id=? AND t.status!='done'""", (done_task_id,)))
    done = _task(done_task_id)
    for d in dependents:
        if open_blockers(d["id"]):
            continue
        t = _task(d["id"])
        moved = ""
        if t["status"] == "blocked" and t.get("blocked_kind") == "task":
            # it was only waiting on that task → back to In progress automatically
            from . import blockers
            with tx() as c:
                c.execute("UPDATE tasks SET status='in_progress' WHERE id=?", (t["id"],))
                c.execute("INSERT INTO task_updates(task_id, user_id, source, status, note) VALUES(?,?,?,?,?)",
                          (t["id"], None, "auto", "in_progress", f"Unblocked — #{done['id']} {done['title']} is done"))
            blockers.clear(t["id"])
            moved = "\nIt's moved from Blocked back to In progress."
        if t["assignee_blink"]:
            due = f" – due {date.fromisoformat(t['due_date']):%b %d}" if t["due_date"] else ""
            _send([t["assignee_blink"]],
                  f"▶️ **Your turn:** #{t['id']} {t['title']}{due} · {t['project_name']}\n"
                  f"(#{done['id']} {done['title']} was just completed by {who}){moved}"
                  + (f"\n🎓 How-to: {t['tutorial_url']}" if t.get("tutorial_url") else ""))


# ---------- daily check (scheduler) ----------

def overdue_escalation(today: date | None = None) -> int:
    """Message managers once about tasks that are `overdue_days`+ days overdue (one digest per run)."""
    days = int(settings()["overdue_days"] or 0)
    if days <= 0:
        return 0
    today = today or date.today()
    cutoff = (today - timedelta(days=days)).isoformat()
    with tx() as c:
        late = rows(c.execute(
            """SELECT t.id, t.title, t.due_date, p.name AS project_name, COALESCE(u.name,'Unassigned') AS who
               FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN users u ON u.id=t.assignee_id
               WHERE t.status!='done' AND p.status='active' AND t.due_date IS NOT NULL AND t.due_date<=?
                 AND NOT EXISTS (SELECT 1 FROM automation_log a WHERE a.rule='overdue' AND a.task_id=t.id)
               ORDER BY t.due_date""", (cutoff,)))
    if not late:
        return 0
    recipients = _managers()
    if recipients:
        lines = "\n".join(
            f"• #{t['id']} {t['title']} · {t['who']} · {t['project_name']} – due {date.fromisoformat(t['due_date']):%b %d}"
            f" ({(today - date.fromisoformat(t['due_date'])).days} days late)" for t in late[:25])
        more = f"\n…and {len(late) - 25} more" if len(late) > 25 else ""
        _send(recipients, f"⚠️ **{len(late)} task{'s' if len(late) != 1 else ''} now {days}+ days overdue:**\n{lines}{more}")
    with tx() as c:
        c.executemany("INSERT OR IGNORE INTO automation_log(rule, task_id) VALUES('overdue', ?)", [(t["id"],) for t in late])
    return len(late)


def reset_overdue_flag(task_id: int):
    """When a due date moves, allow a fresh escalation later."""
    with tx() as c:
        c.execute("DELETE FROM automation_log WHERE rule='overdue' AND task_id=?", (task_id,))
