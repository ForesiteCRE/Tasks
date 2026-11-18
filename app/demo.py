"""One-click demo data for trying the app: sample teammates, templates, projects and realistic activity.

Everything created is recorded in a manifest (settings key 'demo.manifest') so 'Remove demo data' deletes exactly
that and nothing else. No Blink messages are sent while loading (blink.quiet()).
"""
import json
import random
from datetime import date, timedelta

from . import blink, collab, dashboard, services, starter_templates, team, timing
from .db import get_setting, set_setting, tx

PEOPLE = [("Sam Patel", "sam.patel"), ("Jordan Lee", "jordan.lee"), ("Maria Garza", "maria.garza"), ("Alex Rivera", "alex.rivera")]
DEMO_DOMAIN = "demo.example.com"   # never a real inbox


def manifest() -> dict | None:
    raw = get_setting("demo.manifest")
    return json.loads(raw) if raw else None


def _task(pid, title_part):
    with tx() as c:
        r = c.execute("SELECT id FROM tasks WHERE project_id=? AND title LIKE ? ORDER BY id LIMIT 1",
                      (pid, f"%{title_part}%")).fetchone()
    return r["id"] if r else None


def load(me_id: int) -> dict:
    if manifest():
        raise ValueError("Demo data is already loaded — remove it first to reload.")
    rnd = random.Random(7)
    m = {"users": [], "projects": [], "tasks": [], "reminders": [], "recurrences": [], "roles_before": {}}
    with blink.quiet():
        # --- people
        ids = {}
        with tx() as c:
            for name, handle in PEOPLE:
                r = c.execute("SELECT id FROM users WHERE email=?", (f"{handle}@{DEMO_DOMAIN}",)).fetchone()
                uid = r["id"] if r else c.execute("INSERT INTO users(name, email, role) VALUES(?,?,'staff')",
                                                  (f"{name} (demo)", f"{handle}@{DEMO_DOMAIN}")).lastrowid
                ids[name] = uid
                m["users"].append(uid)
        # --- role defaults for the demo (remembered so they can be restored)
        plan = {"Property Manager": me_id, "Head of Accounting": ids["Jordan Lee"], "AP": ids["Maria Garza"],
                "AR": ids["Maria Garza"], "Leasing": ids["Alex Rivera"], "Asset Manager": ids["Sam Patel"]}
        for r in team.list_roles():
            if r["name"] in plan:
                m["roles_before"][str(r["id"])] = r["default_user_id"]
                team.update_role(r["id"], {"default_user_id": plan[r["name"]]})

        # --- templates + sample projects
        keys = [s["key"] for s in starter_templates.STARTERS]
        starter_templates._install_templates(keys)
        names = starter_templates.create_samples(keys)
        with tx() as c:
            m["projects"] = [r[0] for r in c.execute(
                f"SELECT id FROM projects WHERE name IN ({','.join('?' * len(names))})", names)] if names else []
        proj = {}
        with tx() as c:
            for pid in m["projects"]:
                proj[c.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()["name"]] = pid
        onb = next((v for k, v in proj.items() if "Sonterra Blvd (Onboarding)" in k), None)
        retain = next((v for k, v in proj.items() if "retain" in k), None)
        movein = next((v for k, v in proj.items() if "Move-In" in k), None)
        close = next((v for k, v in proj.items() if "Month-End" in k), None)

        # --- realistic activity
        with tx() as c:  # varied actual times on completed work, so Time insights has something to show
            done = [r[0] for r in c.execute(
                f"SELECT id FROM tasks WHERE status='done' AND project_id IN ({','.join('?' * len(m['projects']))})",
                m["projects"])] if m["projects"] else []
        notes = ["prior manager slow to send files", "first time with this owner's format", "went smoothly",
                 "waited on bank signature cards", None, None]
        for tid in done:
            with tx() as c:
                est = c.execute("SELECT estimate_minutes FROM tasks WHERE id=?", (tid,)).fetchone()[0] or 60
            timing.set_times(tid, actual=max(15, int(est * rnd.choice([0.6, 0.8, 1.0, 1.2, 1.5, 1.9]))),
                             notes=rnd.choice(notes))
        if onb:
            t = _task(onb, "Open operating")
            if t:
                services.apply_update(t, ids["Jordan Lee"], "web", "blocked", "Bank needs owner's signed resolution")
            t = _task(onb, "Load tenants")
            if t:
                services.apply_update(t, ids["Maria Garza"], "blink", "in_progress", "12 of 18 tenants loaded")
            t = _task(onb, "Lease abstracts")
            if t:
                services.apply_update(t, ids["Alex Rivera"], "web", "in_progress", None)
            t = _task(onb, "Collect property documents")
            if t:
                handle = collab.handle_for({"email": _email(me_id), "name": ""})
                collab.add_comment(t, ids["Jordan Lee"],
                                   f"@{handle} prior manager says the security deposit ledger comes Friday — OK to start tenant setup without it?")
                collab.add_comment(t, ids["Sam Patel"], "Owner asked for a call once we have the rent roll tied out.")
            t = _task(onb, "Send tenant notification")
            if t:
                handle = collab.handle_for({"email": _email(me_id), "name": ""})
                collab.add_comment(t, ids["Maria Garza"], f"@{handle} draft letter is in Drive — need your sign-off on the payment address.")
        if retain:
            t = _task(retain, "Tenant estoppel")
            if t:
                services.apply_update(t, ids["Alex Rivera"], "blink", "blocked",
                                      "3 of 12 estoppels outstanding: Lone Star Dental, Coastal Coffee, Suite 140")
        if movein:
            t = _task(movein, "Tenant COI")
            if t:
                services.apply_update(t, me_id, "web", "in_progress", "Asked tenant's agent for revised COI")
        if close:
            t = _task(close, "Bank reconciliation")
            if t:
                services.apply_update(t, ids["Jordan Lee"], "web", "in_progress", None)

        # --- tasks without a project
        gen = team.general_project_id()
        for title, who, days, est in [("Renew notary commission", me_id, 5, 30),
                                      ("Order new property signage templates", ids["Alex Rivera"], -2, 60),
                                      ("Update owner contact list", ids["Maria Garza"], 3, 45),
                                      ("Review Q4 vendor insurance certificates", me_id, 9, 90)]:
            with tx() as c:
                tid = c.execute("INSERT INTO tasks(project_id, title, assignee_id, due_date, estimate_minutes) VALUES(?,?,?,?,?)",
                                (gen, title, who, (date.today() + timedelta(days=days)).isoformat(), est)).lastrowid
            m["tasks"].append(tid)

        # --- reminders for you
        tomorrow = date.today() + timedelta(days=1)
        for text, d, hm in [("Call Sonterra owner about keys & access cards", tomorrow, "09:30"),
                            ("Quarterly review prep — 777 E Sonterra", date.today() + timedelta(days=3), "14:00")]:
            m["reminders"].append(dashboard.add_reminder(me_id, text, f"{d.isoformat()}T{hm}"))

        # --- a monthly recurring setup (paused, so it doesn't create real projects)
        with tx() as c:
            tpl_id = c.execute("SELECT id FROM templates WHERE name='Month-End Close'").fetchone()
            if tpl_id:
                rid = c.execute("""INSERT INTO recurrences(template_id, name_pattern, day_of_month, active, last_period)
                                   VALUES(?, '{month} {year} – Month-End Close (777 E Sonterra)', 1, 0, ?)""",
                                (tpl_id[0], date.today().strftime("%Y-%m"))).lastrowid
                m["recurrences"].append(rid)

    m["loaded_at"] = date.today().isoformat()
    set_setting("demo.manifest", json.dumps(m))
    return {"people": len(m["users"]), "projects": len(m["projects"]), "loose_tasks": len(m["tasks"]),
            "reminders": len(m["reminders"])}


def _email(uid):
    with tx() as c:
        r = c.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
    return (r and r["email"]) or ""


def remove() -> dict:
    m = manifest()
    if not m:
        return {"removed": False}
    for pid in m["projects"]:
        starter_templates.delete_project(pid)
    for tid in m["tasks"]:
        collab.delete_task(tid)
    with tx() as c:
        for rid in m["reminders"]:
            c.execute("DELETE FROM reminders WHERE id=?", (rid,))
        for rid in m["recurrences"]:
            c.execute("DELETE FROM recurrences WHERE id=?", (rid,))
        for role_id, before in m.get("roles_before", {}).items():
            c.execute("UPDATE project_roles SET default_user_id=? WHERE id=?", (before, int(role_id)))
        for uid in m["users"]:  # demo people: unassign anything left and remove them
            c.execute("UPDATE tasks SET assignee_id=NULL WHERE assignee_id=?", (uid,))
            c.execute("UPDATE template_items SET default_assignee_id=NULL WHERE default_assignee_id=?", (uid,))
            c.execute("UPDATE task_updates SET user_id=NULL WHERE user_id=?", (uid,))
            c.execute("UPDATE task_comments SET user_id=NULL WHERE user_id=?", (uid,))
            c.execute("DELETE FROM project_team WHERE user_id=?", (uid,))
            c.execute("DELETE FROM users WHERE id=?", (uid,))
    set_setting("demo.manifest", "")
    return {"removed": True, "projects": len(m["projects"]), "people": len(m["users"])}


def status() -> dict:
    m = manifest()
    return {"loaded": bool(m), "loaded_at": (m or {}).get("loaded_at"), "projects": len((m or {}).get("projects", []))}

