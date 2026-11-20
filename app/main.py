import asyncio
import logging
import os
import re
import urllib.parse
from contextlib import asynccontextmanager
from datetime import date, datetime
from datetime import date as _date

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import (audit, auth, automations, bids, blink, notify, blockers, fun, helpbot, ourcoi, chat, closeboard, departments, emails, vendors, collab, dashboard, demo, extras, intake, maintenance, photos, properties, recur, services,
               starter_templates, team, timing, tpl)
from .db import DB_PATH, get_setting, init_db, rows, tx

logging.basicConfig(level=logging.INFO)
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))
STATIC = os.path.join(os.path.dirname(__file__), "..", "static")


async def scheduler_loop():
    """Every 10 min: daily backup, spawn due monthly projects; after REMINDER_HOUR send daily
    reminders (once/day/task) and, on the summary weekday, the weekly summary (once/week)."""
    while True:
        for job in (maintenance.daily_backup, services.run_recurrences, dashboard.send_due_reminders,
                    notify.run_summaries, fun.run_daily, demo.refresh_wins):
            try:
                job()
            except Exception:
                logging.exception("scheduler job %s failed", job.__name__)
        if datetime.now().hour >= REMINDER_HOUR:
            for job in (services.send_reminders, maintenance.send_weekly_summary, automations.overdue_escalation,
                        extras.send_week_ahead, properties.run_alerts, audit.purge, departments.run_escalations, emails.run):
                try:
                    job()
                except Exception:
                    logging.exception("scheduler job %s failed", job.__name__)
        await asyncio.sleep(60)  # every job is idempotent (guarded per day/week/task)


@asynccontextmanager
async def lifespan(_):
    init_db()
    tpl.init()
    try:
        starter_templates.migrate_onboarding_docs()
        starter_templates.upgrade_templates_v2()
    except Exception:
        logging.getLogger("templates").exception("onboarding document-request update failed")
    try:
        demo.backfill_contacts()
    except Exception:
        logging.getLogger("demo").exception("sample contact backfill failed")
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()


app = FastAPI(title="Blink Task Manager", lifespan=lifespan)
# Order matters: the session middleware must wrap (run before) the login check.
app.add_middleware(BaseHTTPMiddleware, dispatch=auth.require_login)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET") or os.urandom(32).hex(),
                   https_only=bool(auth.PUBLIC_URL.startswith("https")), same_site="lax", max_age=30 * 86400)
app.include_router(auth.router)
# Chat simulator is for testing; on by default only when Blink isn't connected.
SIMULATOR = os.getenv("ENABLE_SIMULATOR", "1" if blink.MOCK else "0") == "1"


from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/brand", StaticFiles(directory=os.path.join(STATIC, "brand")), name="brand")


@app.get("/healthz")
def healthz():
    """Which settings the running app can see (yes/no only - never values)."""
    names = ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "PUBLIC_URL", "SESSION_SECRET",
             "BLINK_CLIENT_ID", "BLINK_CLIENT_SECRET", "BLINK_WEBHOOK_SECRET", "BLINK_LEGACY_TOKEN", "DB_PATH", "TZ"]
    return {"ok": True, "sign_in_enabled": auth.ENABLED, "blink_connected": not blink.MOCK,
            "settings_present": {n: bool(os.getenv(n, "").strip()) for n in names},
            "database_file": DB_PATH, "volume_mount": os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ""),
            "deploy_commit": (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:7]}


@app.get("/")
def index():
    # always re-check for a new version (phones / Blink's in-app browser otherwise keep showing an old page)
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache, must-revalidate"})


# ---------- models ----------
class UserIn(BaseModel):
    name: str
    email: str | None = None
    blink_user_id: str | None = None

class TemplateItemIn(BaseModel):
    title: str
    default_assignee_id: int | None = None
    due_offset_days: int = 0

class TemplateIn(BaseModel):
    name: str
    description: str = ""
    items: list[TemplateItemIn] = []

class RecurrenceIn(BaseModel):
    template_id: int
    name_pattern: str
    day_of_month: int = 1

class ProjectIn(BaseModel):
    repeat: str | None = None                   # "monthly" | "quarterly" | "yearly" (needs a template)
    name: str
    template_id: int | None = None
    start_date: date | None = None
    property: str | None = None
    client: str | None = None
    description: str | None = None
    team: dict[str, int | None] | None = None   # role_id -> user_id

class TaskIn(BaseModel):
    repeat: str | None = None
    project_id: int | None = None
    property_id: int | None = None         # a task can belong to a property with or without a project
    parent_id: int | None = None
    title: str
    description: str | None = None
    estimate: str | int | None = None
    assignee_id: int | None = None
    due_date: date | None = None

class TaskPatch(BaseModel):
    repeat: str | None = None              # weekly|biweekly|monthly|quarterly|yearly; "" stops repeating
    tutorial_url: str | None = None        # "" clears
    estimate: str | int | None = None      # "1h30", "90m", 90; "" clears
    actual: str | int | None = None
    time_notes: str | None = None
    title: str | None = None
    description: str | None = None
    status: str | None = None
    assignee_id: int | None = None
    due_date: date | None = None
    note: str | None = None
    user_id: int | None = None
    block: dict | None = None              # {kind: task|person|other, task_id, ref, label, reason, follow_up}
    property_id: int | None = None         # project-less tasks only; 0 clears

class SaveAsTemplate(BaseModel):
    name: str

class InboundMessage(BaseModel):
    blink_user_id: str
    text: str


# ---------- users ----------
@app.get("/api/users")
def list_users():
    with tx() as c:
        return rows(c.execute(
            """SELECT u.*, (SELECT COUNT(*) FROM tasks t JOIN projects p ON p.id=t.project_id
                            WHERE t.assignee_id=u.id AND t.status!='done' AND p.status='active') AS open_tasks
               FROM users u ORDER BY lower(u.name)"""))


@app.delete("/api/users/{uid}")
def delete_user(uid: int, request: Request, merge_into: int | None = None):
    """Remove a person. Their tasks, template defaults and history move to `merge_into`
    (merging duplicates) or become unassigned. Blink id / email are copied to the target if it lacks them."""
    me = (auth.current_user(request) or {}).get("id")
    if uid == me:
        raise HTTPException(400, "You can't remove yourself while signed in — merge the other entry into yours instead.")
    if merge_into == uid:
        raise HTTPException(400, "Pick a different person to merge into.")
    with tx() as c:
        src = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not src:
            raise HTTPException(404, "Person not found")
        if merge_into:
            dst = c.execute("SELECT * FROM users WHERE id=?", (merge_into,)).fetchone()
            if not dst:
                raise HTTPException(404, "Merge target not found")
        c.execute("UPDATE tasks SET assignee_id=? WHERE assignee_id=?", (merge_into, uid))
        c.execute("UPDATE template_items SET default_assignee_id=? WHERE default_assignee_id=?", (merge_into, uid))
        c.execute("UPDATE task_updates SET user_id=? WHERE user_id=?", (merge_into, uid))
        if merge_into:
            c.execute("UPDATE OR REPLACE property_team SET user_id=? WHERE user_id=?", (merge_into, uid))
        c.execute("DELETE FROM property_team WHERE user_id=?", (uid,))
        c.execute("UPDATE critical_dates SET assignee_id=? WHERE assignee_id=?", (merge_into, uid))
        c.execute("DELETE FROM users WHERE id=?", (uid,))  # delete first: blink_user_id is UNIQUE
        if merge_into:
            c.execute("""UPDATE users SET
                           blink_user_id = COALESCE(blink_user_id, ?),
                           blink_chat_id = CASE WHEN blink_user_id IS NULL THEN ? ELSE blink_chat_id END,
                           email = COALESCE(email, ?),
                           weekly_summary = MAX(weekly_summary, ?)
                         WHERE id=?""",
                      (src["blink_user_id"], src["blink_chat_id"], src["email"], src["weekly_summary"], merge_into))
    return {"ok": True}

class UserPatch(BaseModel):
    department_id: int | None = None
    role: str | None = None
    name: str | None = None
    blink_user_id: str | None = None
    weekly_summary: bool | None = None

@app.patch("/api/users/{uid}")
def patch_user(uid: int, p: UserPatch):
    with tx() as c:
        if p.name:
            c.execute("UPDATE users SET name=? WHERE id=?", (p.name, uid))
        if p.blink_user_id is not None:  # changing the Blink id resets the cached 1:1 chat
            c.execute("UPDATE users SET blink_user_id=?, blink_chat_id=NULL WHERE id=?",
                      (p.blink_user_id.strip() or None, uid))
        if p.role in ("admin", "manager", "staff"):
            if p.role != "admin" and c.execute(
                    "SELECT COUNT(*) FROM users WHERE role='admin' AND id!=?", (uid,)).fetchone()[0] == 0:
                raise HTTPException(400, "There must be at least one admin.")
            c.execute("UPDATE users SET role=? WHERE id=?", (p.role, uid))
        if p.weekly_summary is not None:
            c.execute("UPDATE users SET weekly_summary=? WHERE id=?", (int(p.weekly_summary), uid))
    if "department_id" in p.model_fields_set:
        departments.set_user_department(uid, p.department_id)
    return {"ok": True}


class DeptIn(BaseModel):
    name: str | None = None
    head_user_id: int | None = None

@app.get("/api/departments")
def list_departments():
    return departments.list_all()


@app.post("/api/departments")
def create_department(body: DeptIn):
    try:
        return {"id": departments.create(body.name, body.head_user_id)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/departments/{did}")
def patch_department(did: int, body: DeptIn):
    departments.update(did, body.model_dump(exclude_unset=True))
    return {"ok": True}


@app.delete("/api/departments/{did}")
def delete_department(did: int):
    departments.delete(did)
    return {"ok": True}

@app.get("/api/config")
def config():
    return {"simulator": SIMULATOR, "blink_mock": blink.MOCK, "plain_english": chat.ai_enabled(),
            "streetview": bool(photos.MAPS_KEY),
            "last_backup": get_setting("last_backup"), "last_summary_week": get_setting("last_summary_week")}


# ---------- weekly summary & backups ----------
@app.get("/api/summary/preview")
def summary_preview():
    return {"text": maintenance.build_summary()}

@app.post("/api/summary/send")
def summary_send():
    return {"sent_to": maintenance.send_weekly_summary(force=True)}

@app.get("/api/backups")
def backups_list():
    return maintenance.list_backups()

@app.post("/api/backups/run")
def backups_run():
    return {"file": os.path.basename(maintenance.backup_now())}

@app.get("/api/backups/{name}")
def backups_download(name: str):
    if name not in {b["name"] for b in maintenance.list_backups()}:  # only files we listed - no path tricks
        raise HTTPException(404)
    return FileResponse(os.path.join(maintenance.BACKUP_DIR, name), filename=name,
                        media_type="application/octet-stream")

@app.post("/api/users")
def create_user(u: UserIn):
    with tx() as c:
        uid = c.execute("INSERT INTO users(name,email,blink_user_id) VALUES(?,?,?)",
                        (u.name, (u.email or "").strip().lower() or None, u.blink_user_id or None)).lastrowid
    if u.email and not u.blink_user_id:
        auth.link_blink_account(uid, u.email.strip().lower())
    return {"id": uid}

@app.post("/api/users/match-blink")
def match_blink():
    """Look up Blink ids by email for everyone who doesn't have one yet; report Blink's answer per person."""
    with tx() as c:
        todo = rows(c.execute("SELECT id, email FROM users WHERE blink_user_id IS NULL AND email IS NOT NULL"))
    results = []
    for u in todo:
        res = blink.lookup_user(u["email"])
        if res["id"]:
            with tx() as c:
                c.execute("UPDATE users SET blink_user_id=? WHERE id=?", (res["id"], u["id"]))
        results.append({"email": u["email"], **res})
    return {"checked": len(todo), "found": sum(1 for r in results if r["id"]), "results": results}


# ---------- templates ----------
@app.get("/api/templates")
def list_templates():
    return tpl.list_templates()


class TemplateItemNew(BaseModel):
    title: str
    parent_item_id: int | None = None
    default_assignee_id: int | None = None
    default_role_id: int | None = None
    due_offset_days: int = 0

@app.post("/api/templates/{tid}/items")
def add_template_item(tid: int, body: TemplateItemNew):
    return {"id": tpl.add_item(tid, body.title, body.parent_item_id, body.default_assignee_id, body.due_offset_days,
                               body.default_role_id)}

@app.patch("/api/templates/{tid}")
def patch_template(tid: int, body: dict):
    tpl.update_template(tid, body)
    return {"ok": True}

@app.post("/api/template-items/{iid}/move")
def move_template_item(iid: int, direction: int):
    tpl.move_item(iid, direction)
    return {"ok": True}

@app.patch("/api/template-items/{iid}")
def patch_template_item(iid: int, body: dict):
    try:
        tpl.update_item(iid, body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}

@app.delete("/api/template-items/{iid}")
def delete_template_item(iid: int):
    tpl.delete_item(iid)
    return {"ok": True}

class TemplateChecklistIn(BaseModel):
    text: str

@app.post("/api/template-items/{iid}/checklist")
def add_template_checklist(iid: int, body: TemplateChecklistIn):
    return {"id": tpl.add_checklist(iid, body.text)}

@app.delete("/api/template-checklist/{cid}")
def delete_template_checklist(cid: int):
    tpl.delete_checklist(cid)
    return {"ok": True}

class TemplateDepIn(BaseModel):
    depends_on_item_id: int

@app.post("/api/template-items/{iid}/dependencies")
def add_template_dep(iid: int, body: TemplateDepIn):
    try:
        tpl.add_dependency(iid, body.depends_on_item_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}

@app.delete("/api/template-items/{iid}/dependencies/{dep}")
def remove_template_dep(iid: int, dep: int):
    tpl.remove_dependency(iid, dep)
    return {"ok": True}

@app.post("/api/templates")
def create_template(t: TemplateIn):
    with tx() as c:
        tid = c.execute("INSERT INTO templates(name,description) VALUES(?,?)", (t.name, t.description)).lastrowid
        c.executemany(
            "INSERT INTO template_items(template_id,title,default_assignee_id,due_offset_days,position) VALUES(?,?,?,?,?)",
            [(tid, i.title, i.default_assignee_id, i.due_offset_days, n) for n, i in enumerate(t.items)])
    return {"id": tid}

@app.get("/api/templates/starters")
def starters_catalog():
    return starter_templates.catalog()


class StarterIn(BaseModel):
    keys: list[str]
    sample_projects: bool = False

@app.post("/api/templates/starters")
def starters_install(body: StarterIn):
    return starter_templates.install(body.keys, body.sample_projects)


@app.delete("/api/templates/{tid}")
def delete_template(tid: int):
    with tx() as c:
        c.execute("DELETE FROM templates WHERE id=?", (tid,))
    return {"ok": True}


# ---------- recurrences ----------
@app.get("/api/recurrences")
def list_recurrences():
    with tx() as c:
        return rows(c.execute("SELECT r.*, t.name AS template_name FROM recurrences r JOIN templates t ON t.id=r.template_id"))

@app.post("/api/recurrences")
def create_recurrence(r: RecurrenceIn):
    with tx() as c:
        rid = c.execute("INSERT INTO recurrences(template_id,name_pattern,day_of_month) VALUES(?,?,?)",
                        (r.template_id, r.name_pattern, r.day_of_month)).lastrowid
    services.run_recurrences()  # generate this month's immediately if the day has passed
    return {"id": rid}

@app.get("/api/recurring")
def recurring_overview():
    return recur.overview()

@app.delete("/api/recurrences/{rid}")
def delete_recurrence(rid: int, request: Request):
    if not auth.is_manager(request):
        raise HTTPException(403, "Only managers can stop repeating projects.")
    with tx() as c:
        c.execute("UPDATE projects SET recurrence_id=NULL WHERE recurrence_id=?", (rid,))
        c.execute("DELETE FROM recurrences WHERE id=?", (rid,))
    return {"ok": True}

@app.patch("/api/recurrences/{rid}")
def toggle_recurrence(rid: int, active: bool):
    with tx() as c:
        c.execute("UPDATE recurrences SET active=? WHERE id=?", (int(active), rid))
    return {"ok": True}


# ---------- projects ----------
@app.get("/api/projects")
def list_projects():
    with tx() as c:
        ps = rows(c.execute("SELECT * FROM projects ORDER BY status='done', start_date DESC"))
        for p in ps:
            p["tasks"] = rows(c.execute(
                """SELECT t.*, u.name AS assignee_name,
                          (SELECT note FROM task_updates tu WHERE tu.task_id=t.id AND note IS NOT NULL
                           ORDER BY tu.id DESC LIMIT 1) AS last_note,
                          (SELECT COUNT(*) FROM task_checklist k WHERE k.task_id=t.id) AS check_total,
                          (SELECT COUNT(*) FROM task_checklist k WHERE k.task_id=t.id AND k.done) AS check_done,
                          (SELECT COUNT(*) FROM task_comments m WHERE m.task_id=t.id) AS comments,
                          (SELECT GROUP_CONCAT(b.id) FROM task_dependencies d JOIN tasks b ON b.id=d.depends_on_id
                           WHERE d.task_id=t.id AND b.status!='done') AS waiting_on
                   FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id
                   WHERE project_id=? ORDER BY position,id""", (p["id"],)))
    for p in ps:
        p["team"] = team.project_team(p["id"])
        p["vendors"] = vendors.project_vendors(p["id"])
        with tx() as c:
            ap = c.execute("""SELECT status FROM project_approvals WHERE project_id=? AND status!='superseded'
                              ORDER BY id DESC LIMIT 1""", (p["id"],)).fetchone()
            pr = c.execute("SELECT name FROM properties WHERE id=?", (p["property_id"],)).fetchone() if p.get("property_id") else None
        p["approval"] = ap["status"] if ap else None
        p["property_name"] = pr["name"] if pr else None
    return ps

@app.post("/api/projects")
def create_project(p: ProjectIn, request: Request):
    pid = _create_project(p)
    me = auth.current_user(request) or {}
    with tx() as c:
        c.execute("UPDATE projects SET created_by=? WHERE id=?", (me.get("id"), pid))
        nm = c.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()["name"]
    audit.log(me.get("id"), "created", "project", pid, nm, p.repeat and f"repeats {p.repeat}")
    return {"id": pid}


def _create_project(p: ProjectIn) -> int:
    start = p.start_date or date.today()
    details = {"property": p.property, "client": p.client, "description": p.description}
    every = {"monthly": 1, "quarterly": 3, "yearly": 12}.get((p.repeat or "").lower())
    if p.repeat and not every:
        raise HTTPException(400, "Projects can repeat monthly, quarterly or yearly.")
    if every and not p.template_id:
        raise HTTPException(400, "Choose a template for a repeating project — each new copy is built from it.")
    if p.template_id:
        name = recur.project_name_for(p.name, start) if every else p.name
        pid = services.create_project_from_template(p.template_id, name, start, team=p.team, details=details)
        if every:
            rid = recur.create_project_recurrence(p.template_id, p.name, start, every, p.team, details)
            with tx() as c:
                c.execute("UPDATE projects SET recurrence_id=? WHERE id=?", (rid, pid))
        return pid
    with tx() as c:
        pid = c.execute("INSERT INTO projects(name,start_date,property,client,description) VALUES(?,?,?,?,?)",
                        (p.name, start.isoformat(), p.property, p.client, p.description)).lastrowid
    team.save_team(pid, team.resolve_team(p.team, p.property))
    properties.after_project_created(pid, p.team)
    return pid


@app.delete("/api/projects/samples")
def delete_samples(request: Request):
    if not auth.is_manager(request):
        raise HTTPException(403, "Only managers can delete projects.")
    return {"removed": starter_templates.remove_samples()}


@app.delete("/api/projects/{pid}")
def delete_project(pid: int, request: Request, confirm: str = ""):
    """Deleting is deliberately hard: only managers or the person who created the project, and the caller must
    type the project's exact name (sent as ?confirm=...)."""
    me = auth.current_user(request) or {}
    with tx() as c:
        p = c.execute("SELECT name, is_general, created_by FROM projects WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "Project not found")
    if p["is_general"]:
        raise HTTPException(400, "That's the built-in “No project” list — delete its tasks instead.")
    if not (auth.is_manager(request) or (p["created_by"] and p["created_by"] == me.get("id"))):
        raise HTTPException(403, "Only managers or the person who created this project can delete it.")
    if confirm.strip().casefold() != (p["name"] or "").strip().casefold():
        raise HTTPException(400, "To delete, type the project's exact name to confirm.")
    logging.warning("Project %s “%s” deleted by %s", pid, p["name"], me.get("email"))
    trash_id = audit.to_trash("project", p["name"], audit.snapshot_project(pid), me.get("id"))
    starter_templates.delete_project(pid)
    audit.log(me.get("id"), "deleted", "project", pid, p["name"])
    return {"ok": True, "trash_id": trash_id}


class PhotoIn(BaseModel):
    data_b64: str

@app.post("/api/projects/{pid}/photo")
def upload_photo(pid: int, body: PhotoIn, request: Request):
    if not auth.is_manager(request):
        raise HTTPException(403, "Only managers can change project photos.")
    try:
        return {"photo_path": photos.save_upload(pid, body.data_b64)}
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.delete("/api/projects/{pid}/photo")
def delete_photo(pid: int, request: Request):
    if not auth.is_manager(request):
        raise HTTPException(403, "Only managers can change project photos.")
    photos.remove_upload(pid)
    return {"ok": True}

@app.get("/api/projects/{pid}/photo")
def get_photo(pid: int):
    path = photos.upload_file(pid)
    if not path:
        raise HTTPException(404, "No photo")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

@app.get("/api/projects/{pid}/streetview")
def get_streetview(pid: int):
    with tx() as c:
        p = c.execute("SELECT property FROM projects WHERE id=?", (pid,)).fetchone()
    path = photos.streetview_file(p["property"]) if p else None
    if not path:
        raise HTTPException(404, "No street view")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


class TaskMove(BaseModel):
    project_id: int | None = None   # None = no project

@app.put("/api/tasks/{tid}/project")
def move_task(tid: int, body: TaskMove, request: Request):
    """Move a task (and its subtasks) to another project, or out of any project."""
    if not auth.is_manager(request):
        raise HTTPException(403, "Only managers can move tasks between projects.")
    pid = body.project_id or team.general_project_id()
    with tx() as c:
        ids_, stack = [], [tid]
        while stack:
            cur = stack.pop(); ids_.append(cur)
            stack += [r[0] for r in c.execute("SELECT id FROM tasks WHERE parent_id=?", (cur,))]
        pp = c.execute("SELECT property_id FROM projects WHERE id=? AND is_general=0", (pid,)).fetchone()
        c.execute(f"UPDATE tasks SET project_id=?, role_id=NULL WHERE id IN ({','.join('?' * len(ids_))})", (pid, *ids_))
        if pp:
            c.execute(f"UPDATE tasks SET property_id=? WHERE id IN ({','.join('?' * len(ids_))})", (pp[0], *ids_))
        c.execute("UPDATE tasks SET parent_id=NULL WHERE id=?", (tid,))
        c.execute("UPDATE projects SET status='active' WHERE id=?", (pid,))
    return {"ok": True}


class ProjectPatch(BaseModel):
    name: str | None = None
    property: str | None = None
    client: str | None = None
    description: str | None = None
    start_date: date | None = None

@app.patch("/api/projects/{pid}")
def patch_project(pid: int, p: ProjectPatch, request: Request):
    fields = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in p.model_dump(exclude_unset=True).items()}
    if fields:
        with tx() as c:
            c.execute(f"UPDATE projects SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), pid))
        if "property" in fields:
            properties.relink(pid)
        with tx() as c:
            nm = c.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
        audit.log(_me_id(request), "project edited", "project", pid, nm["name"] if nm else None,
                  ", ".join(f"{k.replace('_', ' ')}: {str(v)[:60]}" for k, v in fields.items()))
    return {"ok": True}


class TeamMemberIn(BaseModel):
    user_id: int | None = None

@app.put("/api/projects/{pid}/team/{role_id}")
def set_team_member(pid: int, role_id: int, body: TeamMemberIn, request: Request):
    moved = team.set_member(pid, role_id, body.user_id)
    _log_role(request, "project", pid, role_id, body.user_id, moved)
    return {"moved": moved}


def _log_role(request, entity, eid, role_id, user_id, moved):
    with tx() as c:
        role = c.execute("SELECT name FROM project_roles WHERE id=?", (role_id,)).fetchone()
        who = c.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None
        label = c.execute(f"SELECT name FROM {'projects' if entity == 'project' else 'properties'} WHERE id=?", (eid,)).fetchone()
    audit.log(_me_id(request), "role changed", entity, eid, label["name"] if label else None,
              f"{role['name'] if role else 'Role'} → {who['name'] if who else 'nobody'}" + (f" · {moved} open task{'s' if moved != 1 else ''} moved" if moved else ""))


# ---------- project roles ----------
class RoleIn(BaseModel):
    name: str
    default_user_id: int | None = None

@app.get("/api/roles")
def roles_list():
    return team.list_roles()

@app.post("/api/roles")
def roles_add(body: RoleIn):
    return {"id": team.add_role(body.name, body.default_user_id)}

@app.patch("/api/roles/{rid}")
def roles_patch(rid: int, body: dict):
    team.update_role(rid, body)
    return {"ok": True}

@app.delete("/api/roles/{rid}")
def roles_delete(rid: int):
    team.delete_role(rid)
    return {"ok": True}

@app.post("/api/projects/{pid}/save-as-template")
def save_as_template(pid: int, body: SaveAsTemplate):
    return {"id": services.save_project_as_template(pid, body.name)}


# ---------- tasks ----------
@app.post("/api/tasks")
def create_task(t: TaskIn, background: BackgroundTasks, request: Request):
    me = auth.current_user(request) or {}
    with tx() as c:
        if t.parent_id:
            parent = c.execute("SELECT project_id, assignee_id FROM tasks WHERE id=?", (t.parent_id,)).fetchone()
            if not parent:
                raise HTTPException(404, "Parent task not found")
            t.project_id = parent["project_id"]
            allowed = auth.at_least(me, "manager") or parent["assignee_id"] == me.get("id")
        elif not t.project_id:   # a task without a project: anyone can add one for themselves
            allowed = auth.at_least(me, "manager") or t.assignee_id in (None, me.get("id"))
            if not auth.at_least(me, "manager"):
                t.assignee_id = me.get("id")
        else:
            allowed = auth.at_least(me, "manager")
        if not allowed:
            raise HTTPException(403, "Only managers can add tasks to projects or for other people.")
    if not t.project_id:
        t.project_id = team.general_project_id()
    with tx() as c:
        proj_prop = c.execute("SELECT property_id FROM projects WHERE id=?", (t.project_id,)).fetchone()
        prop_id = (proj_prop and proj_prop[0]) or t.property_id or None   # a project's property wins
        if prop_id and not c.execute("SELECT 1 FROM properties WHERE id=?", (prop_id,)).fetchone():
            raise HTTPException(400, "Unknown property")
        tid = c.execute(
            "INSERT INTO tasks(project_id,parent_id,title,description,assignee_id,due_date,position,property_id) VALUES(?,?,?,?,?,?,999,?)",
            (t.project_id, t.parent_id, t.title, t.description, t.assignee_id,
             t.due_date and t.due_date.isoformat(), prop_id)).lastrowid
        c.execute("UPDATE projects SET status='active' WHERE id=?", (t.project_id,))
        u = c.execute("SELECT blink_user_id FROM users WHERE id=?", (t.assignee_id,)).fetchone() if t.assignee_id else None
    if t.estimate not in (None, ""):
        timing.set_times(tid, estimate=timing.parse_duration(t.estimate) or 0)
    if t.repeat:
        try:
            rep = recur.clean(t.repeat)
        except ValueError as e:
            raise HTTPException(400, str(e))
        with tx() as c:
            c.execute("UPDATE tasks SET repeat=? WHERE id=?", (rep, tid))
    if u and u["blink_user_id"] and t.assignee_id != me.get("id"):
        kind = "subtask" if t.parent_id else "task"
        text = f"📌 New {kind} assigned: #{tid} {t.title}" + (f" – due {t.due_date:%b %d}" if t.due_date else "")
        background.add_task(_notify, u["blink_user_id"], text)
    return {"id": tid}


def _notify(blink_user_id: str, text: str):
    try:
        notify.deliver_blink([blink_user_id], "assigned", text)
    except Exception:
        logging.exception("Blink notification failed for %s", blink_user_id)


@app.patch("/api/tasks/{tid}")
def patch_task(tid: int, p: TaskPatch, request: Request):
    me = auth.current_user(request) or {}
    p.user_id = me.get("id")
    if not auth.at_least(me, "manager"):
        _require_task_editor(request, tid)
        if p.assignee_id is not None or p.due_date is not None or p.title is not None or p.tutorial_url is not None:
            raise HTTPException(403, "Only managers can rename, reassign, change due dates or tutorial links.")
    with tx() as c:
        before = c.execute("SELECT t.title, t.assignee_id, t.due_date, u.name AS who FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id WHERE t.id=?",
                           (tid,)).fetchone()
        if p.title:
            c.execute("UPDATE tasks SET title=? WHERE id=?", (p.title.strip(), tid))
        if p.repeat is not None:
            try:
                c.execute("UPDATE tasks SET repeat=? WHERE id=?", (recur.clean(p.repeat), tid))
            except ValueError as e:
                raise HTTPException(400, str(e))
        if p.tutorial_url is not None:
            try:
                c.execute("UPDATE tasks SET tutorial_url=? WHERE id=?", (tpl.clean_url(p.tutorial_url), tid))
            except ValueError as e:
                raise HTTPException(400, str(e))
        if p.description is not None:
            c.execute("UPDATE tasks SET description=? WHERE id=?", (p.description, tid))
        if p.property_id is not None:
            c.execute("UPDATE tasks SET property_id=? WHERE id=?", (p.property_id or None, tid))
        if p.assignee_id is not None:  # assigning a person directly detaches the task from its project role
            c.execute("UPDATE tasks SET assignee_id=?, role_id=NULL WHERE id=?", (p.assignee_id or None, tid))
        if p.due_date is not None:
            c.execute("UPDATE tasks SET due_date=? WHERE id=?", (p.due_date.isoformat(), tid))
    if before:
        changes = []
        if p.title and p.title.strip() != before["title"]:
            changes.append(("renamed", f"was “{before['title']}”"))
        if p.assignee_id is not None and (p.assignee_id or None) != before["assignee_id"]:
            with tx() as c:
                new = c.execute("SELECT name FROM users WHERE id=?", (p.assignee_id,)).fetchone() if p.assignee_id else None
            changes.append(("reassigned", f"{before['who'] or 'Unassigned'} → {new['name'] if new else 'Unassigned'}"))
        if p.due_date is not None and p.due_date.isoformat() != before["due_date"]:
            changes.append(("due date changed", f"{before['due_date'] or 'none'} → {p.due_date.isoformat()}"))
        for action, detail in changes:
            audit.log(p.user_id, action, "task", tid, p.title or before["title"], detail)
    if p.due_date is not None:
        automations.reset_overdue_flag(tid)
        departments.reset("dept_overdue", tid)
    if p.estimate is not None or p.actual is not None or p.time_notes is not None:
        def mins(v):
            if v is None:
                return None
            m = timing.parse_duration(v)
            if m is None and str(v).strip():
                raise HTTPException(400, f"Couldn't read “{v}” as a duration — try 1h30, 45m or 2h.")
            return m or 0
        timing.set_times(tid, estimate=mins(p.estimate), actual=mins(p.actual), notes=p.time_notes)
    if p.status == "blocked" or p.block:
        # "Blocked by what?" — required on the web: another task, a person/company, or a short reason
        block = p.block or ({"kind": "other", "reason": p.note} if (p.note or "").strip() else None)
        if not block:
            raise HTTPException(400, "Say what it's blocked by — another task, a person, or a short reason.")
        try:
            desc = blockers.set_blocked(tid, block, p.user_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if p.status == "blocked" and not p.note:
            p.note = desc
    if p.status or p.note:
        services.apply_update(tid, p.user_id, "web", p.status, p.note)
    return {"ok": True}


@app.get("/api/tasks/{tid}/block-options")
def block_options(tid: int):
    return blockers.options(tid)

def _require_task_editor(request: Request, tid: int):
    """Managers, the task's assignee, or the assignee of its parent task."""
    me = auth.current_user(request) or {}
    if auth.at_least(me, "manager"):
        return me
    with tx() as c:
        t = c.execute("""SELECT t.assignee_id, p.assignee_id AS parent_assignee FROM tasks t
                         LEFT JOIN tasks p ON p.id=t.parent_id WHERE t.id=?""", (tid,)).fetchone()
    if not t:
        raise HTTPException(404, "Task not found")
    if me.get("id") not in (t["assignee_id"], t["parent_assignee"]):
        raise HTTPException(403, "You can only change tasks assigned to you.")
    return me


def _require_manager(request: Request):
    if not auth.is_manager(request):
        raise HTTPException(403, "Only managers can do that.")


@app.get("/api/tasks/{tid}")
def get_task(tid: int):
    d = collab.task_detail(tid)
    if not d:
        raise HTTPException(404, "Task not found")
    return d


@app.delete("/api/tasks/{tid}")
def delete_task(tid: int, request: Request):
    _require_manager(request)
    with tx() as c:
        t = c.execute("SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        raise HTTPException(404, "Task not found")
    me = auth.current_user(request) or {}
    logging.warning("Task %s “%s” deleted by %s", tid, t["title"], me.get("email"))
    trash_id = audit.to_trash("task", t["title"], audit.snapshot_task(tid), me.get("id"))
    collab.delete_task(tid)
    audit.log(me.get("id"), "deleted", "task", tid, t["title"])
    return {"ok": True, "trash_id": trash_id}


class ChecklistIn(BaseModel):
    text: str

class ChecklistPatch(BaseModel):
    done: bool | None = None
    text: str | None = None

@app.post("/api/tasks/{tid}/checklist")
def add_checklist(tid: int, body: ChecklistIn, request: Request):
    _require_task_editor(request, tid)
    return {"id": collab.add_checklist_item(tid, body.text)}

@app.patch("/api/checklist/{cid}")
def patch_checklist(cid: int, body: ChecklistPatch, request: Request):
    tid = collab.checklist_task_id(cid) or 0
    _require_task_editor(request, tid)
    collab.update_checklist_item(cid, body.done, body.text)
    return {"ok": True}

@app.delete("/api/checklist/{cid}")
def delete_checklist(cid: int, request: Request):
    tid = collab.checklist_task_id(cid) or 0
    _require_task_editor(request, tid)
    collab.delete_checklist_item(cid)
    return {"ok": True}


class CommentIn(BaseModel):
    body: str

@app.post("/api/tasks/{tid}/comments")
def add_comment(tid: int, body: CommentIn, request: Request):
    """Anyone signed in can comment on any task (that's how people ask each other questions)."""
    if not body.body.strip():
        raise HTTPException(400, "Comment is empty")
    if not collab.task_detail(tid):
        raise HTTPException(404, "Task not found")
    return collab.add_comment(tid, (auth.current_user(request) or {}).get("id"), body.body)

@app.get("/api/handles")
def handles():
    """@mention handles for the comment box autocomplete."""
    with tx() as c:
        us = rows(c.execute("SELECT id, name, email FROM users ORDER BY lower(name)"))
    return [{"id": u["id"], "name": u["name"], "handle": collab.handle_for(u)} for u in us]


class DependencyIn(BaseModel):
    depends_on_id: int

@app.post("/api/tasks/{tid}/dependencies")
def add_dependency(tid: int, body: DependencyIn, request: Request):
    _require_manager(request)
    try:
        collab.add_dependency(tid, body.depends_on_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}

@app.delete("/api/tasks/{tid}/dependencies/{dep}")
def remove_dependency(tid: int, dep: int, request: Request):
    _require_manager(request)
    collab.remove_dependency(tid, dep)
    return {"ok": True}


class DocIn(BaseModel):
    filename: str = ""
    content_type: str = ""
    data_b64: str

@app.post("/api/intake/extract")
def intake_extract(body: DocIn):
    """Web: upload a document, get a pre-filled project draft back (managers only)."""
    import base64
    data = base64.b64decode(body.data_b64)
    if len(data) > intake.MAX_DOC_BYTES:
        raise HTTPException(413, "File too large (25 MB max).")
    try:
        return intake.extract_for_web(data, body.content_type, body.filename)
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.get("/api/insights/time")
def time_insights(days: int = 180):
    return timing.insights(days)


# ---------- properties & critical dates ----------

class PropertyIn(BaseModel):
    address: str
    name: str | None = None
    client: str | None = None
    notes: str | None = None

class PropertyPatch(BaseModel):
    address: str | None = None
    name: str | None = None
    client: str | None = None
    notes: str | None = None
    report_notes: str | None = None
    spend_limit: float | None = None
    bank_name: str | None = None
    bank_accounts: str | None = None
    our_coi_requirements: str | None = None

class DateIn(BaseModel):
    kind: str = "other"
    title: str = ""
    date: _date
    tenant: str | None = None
    notes: str | None = None
    quote: str | None = None
    assignee_id: int | None = None
    remind_days: str | None = None
    repeat: str | None = None

class DatesIn(BaseModel):
    dates: list[DateIn]
    source: str = "manual"

class DatePatch(BaseModel):
    kind: str | None = None
    title: str | None = None
    date: _date | None = None
    tenant: str | None = None
    notes: str | None = None
    assignee_id: int | None = None
    remind_days: str | None = None
    status: str | None = None
    repeat: str | None = None


@app.get("/api/properties")
def list_properties(request: Request):
    return [_hide_bank(p, request) for p in properties.list_all()]


def _hide_bank(p: dict, request: Request) -> dict:
    """Bank account numbers (even masked) are for managers and admins only."""
    if not auth.is_manager(request):
        p["bank_accounts"] = None
        p["bank_hidden"] = True
    return p


@app.post("/api/properties")
def create_property(body: PropertyIn, request: Request):
    _require_manager(request)
    try:
        return {"id": properties.create(body.address, body.name, body.client, body.notes)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/properties/upcoming-dates")
def upcoming_dates(days: int = 120, mine: bool = False, request: Request = None):
    return properties.upcoming(days, _me_id(request) if mine else None)


@app.get("/api/properties/team-for")
def team_for_address(address: str = ""):
    """New project dialog: if this address is a known property, its team (so the pickers can be pre-filled)."""
    pid = properties.property_for_address(address)
    if not pid:
        return {"property_id": None, "team": {}}
    p = properties.get(pid)
    return {"property_id": pid, "name": p["name"], "team": {str(k): v for k, v in properties.team_map(pid).items()}}


class PropTeamIn(BaseModel):
    user_id: int | None = None

@app.put("/api/properties/{pid}/team/{role_id}")
def set_property_member(pid: int, role_id: int, body: PropTeamIn, request: Request):
    _require_manager(request)
    moved = properties.set_member(pid, role_id, body.user_id)
    _log_role(request, "property", pid, role_id, body.user_id, moved)
    return {"moved": moved}


@app.get("/api/properties/{pid}")
def get_property(pid: int, request: Request):
    p = properties.get(pid)
    if not p:
        raise HTTPException(404, "Property not found")
    return _hide_bank(p, request)


@app.patch("/api/properties/{pid}")
def patch_property(pid: int, body: PropertyPatch, request: Request):
    _require_manager(request)
    fields = body.model_dump(exclude_unset=True)
    try:
        properties.update(pid, fields)
    except (ValueError, Exception) as e:
        if "UNIQUE" in str(e):
            raise HTTPException(400, "Another property already has that address.")
        if isinstance(e, ValueError):
            raise HTTPException(400, str(e))
        raise
    p = properties.get(pid)
    audit.log(_me_id(request), "property edited", "property", pid, p["name"] if p else None,
              ", ".join(f"{k.replace('_', ' ')}: {str(v)[:60]}" for k, v in fields.items()))
    return {"ok": True}


@app.delete("/api/properties/{pid}")
def delete_property(pid: int, request: Request, confirm: str = ""):
    _require_manager(request)
    p = properties.get(pid)
    if not p:
        raise HTTPException(404, "Property not found")
    if confirm.strip().casefold() != p["name"].strip().casefold():
        raise HTTPException(400, "To delete, type the property's exact name to confirm.")
    me = auth.current_user(request) or {}
    logging.warning("Property %s “%s” deleted by %s", pid, p["name"], me.get("email"))
    trash_id = audit.to_trash("property", p["name"], audit.snapshot_property(pid), me.get("id"))
    properties.delete(pid, keep_files=True)
    audit.log(me.get("id"), "deleted", "property", pid, p["name"])
    return {"ok": True, "trash_id": trash_id}


@app.post("/api/properties/{pid}/photo")
def property_photo_upload(pid: int, body: PhotoIn, request: Request):
    _require_manager(request)
    if not properties.get(pid):
        raise HTTPException(404, "Property not found")
    try:
        return {"photo_file": photos.save_property_photo(pid, body.data_b64)}
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.delete("/api/properties/{pid}/photo")
def property_photo_delete(pid: int, request: Request):
    _require_manager(request)
    photos.remove_property_photo(pid)
    return {"ok": True}

@app.get("/api/properties/{pid}/photo")
def property_photo(pid: int):
    path = photos.property_photo_file(pid)
    if not path:
        raise HTTPException(404, "No photo")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

# ---------- owner report packages ----------
@app.put("/api/properties/{pid}/report-package")
def put_report_package(pid: int, body: dict, request: Request):
    _require_manager(request)
    from . import reportpkg
    if not properties.get(pid):
        raise HTTPException(404, "Property not found")
    reportpkg.save(pid, body)
    return {"ok": True}

@app.post("/api/properties/report-packages/import")
def import_report_packages(body: dict, request: Request):
    """Accounting's Report Packages spreadsheet (base64 .xlsx). apply=false → preview only."""
    _require_manager(request)
    import base64
    from . import reportpkg
    try:
        data = base64.b64decode((body.get("data_b64") or "").split(",")[-1])
        r = reportpkg.import_rows(data, apply=bool(body.get("apply")), create_missing=bool(body.get("create_missing")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, "Couldn't read that file — please upload the Excel (.xlsx) workbook.")
    if r["applied"]:
        audit.log(_me_id(request), "imported report packages", "property", None, f"{r['with_package']} packages")
    return r

@app.get("/api/projects/{pid}/report-package")
def project_report_package(pid: int):
    from . import reportpkg
    return reportpkg.for_project(pid) or {}

@app.get("/api/properties/{pid}/streetview")
def property_streetview(pid: int):
    p = properties.get(pid)
    path = photos.streetview_file(p["address"]) if p else None
    if not path:
        raise HTTPException(404, "No Street View")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/properties/{pid}/dates")
def add_dates(pid: int, body: DatesIn, request: Request):
    _require_manager(request)
    if not properties.get(pid):
        raise HTTPException(404, "Property not found")
    ids = [properties.add_date(pid, d.model_dump(), _me_id(request), "lease" if body.source == "lease" else "manual")
           for d in body.dates]
    properties.run_alerts()   # anything already inside its reminder window gets its task + Blink message now
    return {"ids": ids}


@app.patch("/api/critical-dates/{did}")
def patch_date(did: int, body: DatePatch, request: Request):
    me = auth.current_user(request) or {}
    with tx() as c:
        d = c.execute("SELECT assignee_id FROM critical_dates WHERE id=?", (did,)).fetchone()
    if not d:
        raise HTTPException(404, "Date not found")
    fields = body.model_dump(exclude_unset=True)
    if not auth.at_least(me, "manager") and not (d["assignee_id"] == me.get("id") and set(fields) <= {"status", "notes"}):
        raise HTTPException(403, "Only managers (or the person responsible, to mark it handled) can change critical dates.")
    try:
        properties.update_date(did, fields)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with tx() as c:
        d2 = c.execute("SELECT title, property_id FROM critical_dates WHERE id=?", (did,)).fetchone()
    audit.log(me.get("id"), "marked handled" if fields.get("status") == "handled" else "critical date edited",
              "property", d2["property_id"] if d2 else None, d2["title"] if d2 else None,
              ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in fields.items() if k != "status"))
    return {"ok": True}


@app.delete("/api/critical-dates/{did}")
def delete_date(did: int, request: Request):
    _require_manager(request)
    with tx() as c:
        d = c.execute("SELECT title, date, property_id FROM critical_dates WHERE id=?", (did,)).fetchone()
    properties.delete_date(did)
    if d:
        audit.log(_me_id(request), "critical date removed", "property", d["property_id"], d["title"], d["date"])
    return {"ok": True}


@app.post("/api/properties/{pid}/read-document")
def read_document(pid: int, body: DocIn, request: Request):
    """Upload a management agreement / insurance policy / tax notice: the file is kept on the property, the
    insurance check is recorded, and dates, spend limit, report notes and contacts come back for review."""
    _require_manager(request)
    import base64
    data = base64.b64decode(body.data_b64)
    if len(data) > intake.MAX_DOC_BYTES:
        raise HTTPException(413, "File too large (25 MB max).")
    try:
        return properties.read_document(pid, data, body.content_type, body.filename, _me_id(request))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.post("/api/properties/{pid}/docs")
def upload_property_doc(pid: int, body: DocIn, request: Request, kind: str = "other"):
    """Just keep a file on the property (no reading)."""
    _require_manager(request)
    import base64
    data = base64.b64decode(body.data_b64)
    if len(data) > intake.MAX_DOC_BYTES:
        raise HTTPException(413, "File too large (25 MB max).")
    did = properties.save_doc(pid, data, body.filename, body.content_type, _me_id(request))
    properties.update_doc(did, {"kind": kind})
    return {"id": did}


@app.get("/api/property-docs/{did}")
def download_property_doc(did: int):
    got = properties.doc_file(did)
    if not got:
        raise HTTPException(404, "File not found")
    d, path = got
    return FileResponse(path, media_type=d["content_type"] or "application/octet-stream", filename=d["filename"])


class DocPatch(BaseModel):
    kind: str | None = None
    insured_ok: bool | None = None
    expires: str | None = None

@app.patch("/api/property-docs/{did}")
def patch_property_doc(did: int, body: DocPatch, request: Request):
    _require_manager(request)
    properties.update_doc(did, body.model_dump(exclude_unset=True), _me_id(request))
    return {"ok": True}


@app.delete("/api/property-docs/{did}")
def delete_property_doc(did: int, request: Request):
    _require_manager(request)
    with tx() as c:
        d = c.execute("SELECT filename, property_id FROM property_docs WHERE id=?", (did,)).fetchone()
    properties.delete_doc(did)
    if d:
        audit.log(_me_id(request), "document deleted", "property", d["property_id"], d["filename"])
    return {"ok": True}


class ContactIn(BaseModel):
    role: str = "Other"
    name: str | None = None
    company: str | None = None
    email: str | None = None
    phone: str | None = None
    notes: str | None = None

@app.post("/api/properties/{pid}/contacts")
def add_property_contact(pid: int, body: ContactIn, request: Request):
    _require_manager(request)
    try:
        return {"id": properties.add_contact(pid, body.model_dump())}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/property-contacts/{cid}")
def patch_property_contact(cid: int, body: ContactIn, request: Request):
    _require_manager(request)
    properties.update_contact(cid, body.model_dump(exclude_unset=True))
    return {"ok": True}


@app.delete("/api/property-contacts/{cid}")
def delete_property_contact(cid: int, request: Request):
    _require_manager(request)
    properties.delete_contact(cid)
    return {"ok": True}


# ---------- vendors ----------

def _can_assign(request: Request, property_id: int | None = None, project_id: int | None = None) -> bool:
    """Managers, or anyone on the property's (or project's) team."""
    me = auth.current_user(request) or {}
    if auth.at_least(me, "manager"):
        return True
    with tx() as c:
        if project_id:
            if c.execute("SELECT 1 FROM project_team WHERE project_id=? AND user_id=?", (project_id, me.get("id"))).fetchone():
                return True
            r = c.execute("SELECT property_id FROM projects WHERE id=?", (project_id,)).fetchone()
            property_id = r["property_id"] if r else None
        return bool(property_id and c.execute("SELECT 1 FROM property_team WHERE property_id=? AND user_id=?",
                                              (property_id, me.get("id"))).fetchone())


class VendorIn(BaseModel):
    name: str | None = None
    trades: list[str] | None = None
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    notes: str | None = None
    archived: bool | None = None

class ReviewIn(BaseModel):
    stars: int
    price: int | None = None
    note: str | None = None
    property_id: int | None = None
    project_id: int | None = None

class PropVendorIn(BaseModel):
    trade: str
    vendor_id: int | None = None
    replace: bool = True
    notes: str | None = None

class ProjVendorIn(BaseModel):
    vendor_id: int
    trade: str | None = None
    notes: str | None = None


@app.get("/api/vendors")
def list_vendors(trade: str | None = None, q: str | None = None, archived: bool = False):
    return {"vendors": vendors.list_all(trade, q, archived), "trades": vendors.TRADES, "samples": vendors.sample_count()}


@app.post("/api/vendors/samples")
def load_sample_vendors(request: Request):
    _require_manager(request)
    out = vendors.load_samples(_me_id(request))
    audit.log(_me_id(request), "loaded sample vendors", "vendor", None, f"{out['added']} vendors")
    return out


@app.delete("/api/vendors/samples")
def remove_sample_vendors(request: Request):
    _require_manager(request)
    out = vendors.remove_samples()
    audit.log(_me_id(request), "removed sample vendors", "vendor", None, f"{out['removed']} vendors")
    return out


@app.post("/api/vendors")
def create_vendor(body: VendorIn, request: Request):
    try:
        vid = vendors.create(body.model_dump(), _me_id(request))
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.log(_me_id(request), "added vendor", "vendor", vid, body.name)
    return {"id": vid}


@app.get("/api/vendors/{vid}")
def get_vendor(vid: int):
    v = vendors.get(vid)
    if not v:
        raise HTTPException(404, "Vendor not found")
    return v


@app.patch("/api/vendors/{vid}")
def patch_vendor(vid: int, body: VendorIn, request: Request):
    me = auth.current_user(request) or {}
    v = vendors.get(vid)
    if not v:
        raise HTTPException(404, "Vendor not found")
    if not auth.at_least(me, "manager") and v["created_by"] != me.get("id"):
        raise HTTPException(403, "Only managers (or whoever added this vendor) can edit it.")
    try:
        vendors.update(vid, body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/vendors/{vid}/reviews")
def review_vendor(vid: int, body: ReviewIn, request: Request):
    try:
        rid = vendors.add_review(vid, _me_id(request), body.stars, body.note, body.property_id, body.project_id, body.price)
    except ValueError as e:
        raise HTTPException(400, str(e))
    v = vendors.get(vid)
    audit.log(_me_id(request), f"rated vendor {body.stars}★" + (f" {'$' * body.price}" if body.price else ""), "vendor", vid,
              v["name"], body.note)
    return {"id": rid}


@app.delete("/api/vendor-reviews/{rid}")
def delete_review(rid: int, request: Request):
    me = auth.current_user(request) or {}
    if not auth.at_least(me, "manager") and vendors.review_owner(rid) != me.get("id"):
        raise HTTPException(403, "You can only remove your own reviews.")
    vendors.delete_review(rid)
    return {"ok": True}


@app.get("/api/properties/{pid}/vendors")
def get_property_vendors(pid: int):
    return vendors.property_vendors(pid)


@app.put("/api/properties/{pid}/vendors")
def set_property_vendor(pid: int, body: PropVendorIn, request: Request):
    if not _can_assign(request, property_id=pid):
        raise HTTPException(403, "Only managers or people on this property's team can change its vendors.")
    vendors.set_property_vendor(pid, body.trade, body.vendor_id, _me_id(request), body.notes, body.replace)
    p = properties.get(pid)
    v = vendors.get(body.vendor_id) if body.vendor_id else None
    audit.log(_me_id(request), "vendor set" if v else "vendor ended", "property", pid, p["name"] if p else None,
              f"{body.trade}: {v['name'] if v else 'none'}")
    return {"ok": True}


@app.post("/api/properties/{pid}/vendor-slots")
def add_vendor_slot(pid: int, body: PropVendorIn, request: Request):
    if not _can_assign(request, property_id=pid):
        raise HTTPException(403, "Only managers or people on this property's team can change its vendors.")
    try:
        vendors.add_slot(pid, body.trade)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/property-vendors/{pvid}/end")
def end_property_vendor(pvid: int, request: Request):
    with tx() as c:
        r = c.execute("SELECT property_id FROM property_vendors WHERE id=?", (pvid,)).fetchone()
    if not r or not _can_assign(request, property_id=r["property_id"]):
        raise HTTPException(403, "Only managers or people on this property's team can change its vendors.")
    vendors.end_property_vendor(pvid)
    return {"ok": True}


@app.delete("/api/property-vendors/{pvid}")
def delete_property_vendor(pvid: int, request: Request):
    _require_manager(request)
    vendors.remove_property_vendor(pvid)
    return {"ok": True}


@app.post("/api/properties/{pid}/vendors/{vid}/coi")
def upload_vendor_coi(pid: int, vid: int, body: DocIn, request: Request):
    """A vendor's certificate of insurance for THIS property (each property needs its own, naming it additional insured)."""
    if not _can_assign(request, property_id=pid):
        raise HTTPException(403, "Only managers or people on this property's team can upload vendor COIs.")
    import base64
    data = base64.b64decode(body.data_b64)
    if len(data) > intake.MAX_DOC_BYTES:
        raise HTTPException(413, "File too large (25 MB max).")
    if not vendors.get(vid) or not properties.get(pid):
        raise HTTPException(404, "Not found")
    out = vendors.save_coi(pid, vid, data, body.filename, body.content_type, _me_id(request))
    if out["status"]["state"] in ("not_named", "expired", "expiring"):
        out["email_draft_id"] = emails.draft_for_vendor(pid, vid)   # drafted for the PM + Blink heads-up
    audit.log(_me_id(request), "vendor COI uploaded", "property", pid, vendors.get(vid)["name"], out["status"]["label"])
    return out


@app.get("/api/properties/{pid}/vendors/{vid}/cois")
def list_vendor_cois(pid: int, vid: int):
    return vendors.cois(pid, vid)


@app.get("/api/vendor-cois/{cid}")
def download_vendor_coi(cid: int):
    got = vendors.coi_file(cid)
    if not got:
        raise HTTPException(404, "File not found")
    d, path = got
    return FileResponse(path, media_type=d["content_type"] or "application/octet-stream", filename=d["filename"])


class CoiPatch(BaseModel):
    expires: str | None = None
    insured_ok: bool | None = None
    foresite_ok: bool | None = None

@app.patch("/api/vendor-cois/{cid}")
def patch_vendor_coi(cid: int, body: CoiPatch, request: Request):
    with tx() as c:
        k = c.execute("SELECT property_id FROM vendor_cois WHERE id=?", (cid,)).fetchone()
    if not k or not _can_assign(request, property_id=k["property_id"]):
        raise HTTPException(403, "Only managers or people on this property's team can update vendor COIs.")
    f = body.model_dump(exclude_unset=True)
    if f.get("expires"):
        try:
            f["expires"] = date.fromisoformat(f["expires"][:10]).isoformat()
        except ValueError:
            raise HTTPException(400, "Expiration must be a date.")
    vendors.set_coi(cid, f, _me_id(request))
    return {"ok": True}


@app.delete("/api/vendor-cois/{cid}")
def delete_vendor_coi(cid: int, request: Request):
    _require_manager(request)
    vendors.delete_coi(cid)
    return {"ok": True}


@app.get("/api/projects/{pid}/vendors")
def get_project_vendors(pid: int):
    return vendors.project_vendors(pid)


@app.post("/api/projects/{pid}/vendors")
def add_project_vendor(pid: int, body: ProjVendorIn, request: Request):
    if not _can_assign(request, project_id=pid):
        raise HTTPException(403, "Only managers or people on this project's team can assign vendors.")
    try:
        rid = vendors.add_project_vendor(pid, body.vendor_id, body.trade, body.notes, _me_id(request))
    except ValueError as e:
        raise HTTPException(400, str(e))
    with tx() as c:
        pj = c.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
    audit.log(_me_id(request), "vendor assigned", "project", pid, pj["name"] if pj else None,
              f"{body.trade + ': ' if body.trade else ''}{vendors.get(body.vendor_id)['name']}")
    return {"id": rid}


@app.delete("/api/project-vendors/{pvid}")
def remove_project_vendor(pvid: int, request: Request):
    with tx() as c:
        r = c.execute("SELECT project_id FROM project_vendors WHERE id=?", (pvid,)).fetchone()
    if not r or not _can_assign(request, project_id=r["project_id"]):
        raise HTTPException(403, "Only managers or people on this project's team can change its vendors.")
    vendors.remove_project_vendor(pvid)
    return {"ok": True}


# ---------- insurance follow-up emails ----------

class EmailNew(BaseModel):
    property_id: int
    vendor_id: int | None = None

class EmailPatch(BaseModel):
    to_email: str | None = None
    to_name: str | None = None
    subject: str | None = None
    body: str | None = None
    status: str | None = None


@app.get("/api/emails")
def list_emails(request: Request, all: bool = False, closed: bool = False):
    me = auth.current_user(request) or {}
    everyone = all and auth.at_least(me, "manager")
    return {"drafts": emails.list_drafts(None if everyone else me.get("id"), closed),
            "gmail_connected": emails.gmail_connected(me.get("id"))}


@app.post("/api/emails")
def new_email(body: EmailNew, request: Request):
    """Draft one now (✉️ button next to a COI / insurance problem)."""
    if not _can_assign(request, property_id=body.property_id):
        raise HTTPException(403, "Only managers or people on this property's team can draft these.")
    did = (emails.draft_for_vendor(body.property_id, body.vendor_id, _me_id(request), manual=True) if body.vendor_id
           else emails.draft_for_property(body.property_id, _me_id(request), manual=True))
    if not did:
        raise HTTPException(400, "Couldn't draft that — set a Property Manager on the property first.")
    audit.log(_me_id(request), "drafted insurance email", "property", body.property_id, emails.get(did)["subject"])
    return emails.get(did)


@app.patch("/api/emails/{did}")
def patch_email(did: int, body: EmailPatch, request: Request):
    me = auth.current_user(request) or {}
    d = emails.get(did)
    if not d:
        raise HTTPException(404, "Draft not found")
    if d["user_id"] != me.get("id") and not auth.at_least(me, "manager"):
        raise HTTPException(403, "That draft belongs to someone else.")
    in_gmail = emails.update(did, body.model_dump(exclude_unset=True))
    return {"ok": True, "in_gmail": in_gmail}


@app.delete("/api/me/gmail")
def disconnect_gmail(request: Request):
    emails.disconnect_gmail(_me_id(request))
    return {"ok": True}


# ---------- bids, recommendation, owner approval ----------

class BidIn(BaseModel):
    vendor_id: int | None = None
    vendor_name: str | None = None
    amount: float | str | None = None
    scope: str | None = None
    exclusions: str | None = None
    timeline: str | None = None
    warranty: str | None = None
    notes: str | None = None
    submitted_on: str | None = None
    status: str | None = None
    filename: str | None = None
    content_type: str | None = None
    data_b64: str | None = None

class RecIn(BaseModel):
    bid_id: int | None = None
    text: str | None = None

class ApprovalIn(BaseModel):
    owner_name: str | None = None
    owner_email: str | None = None


def _bid_project(bid_id: int) -> int:
    with tx() as c:
        r = c.execute("SELECT project_id FROM project_bids WHERE id=?", (bid_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Bid not found")
    return r["project_id"]


def _need_project_team(request, pid):
    if not _can_assign(request, project_id=pid):
        raise HTTPException(403, "Only managers or people on this project's team can manage bids.")


@app.get("/api/projects/{pid}/bids")
def get_bids(pid: int):
    return bids.project_bids(pid)


@app.post("/api/projects/{pid}/bids")
def add_bid(pid: int, body: BidIn, request: Request):
    _need_project_team(request, pid)
    file = None
    if body.data_b64:
        import base64
        data = base64.b64decode(body.data_b64)
        if len(data) > intake.MAX_DOC_BYTES:
            raise HTTPException(413, "File too large (25 MB max).")
        file = (data, body.filename or "bid.pdf", body.content_type or "application/pdf")
    try:
        out = bids.add_bid(pid, body.model_dump(exclude={"filename", "content_type", "data_b64"}), _me_id(request), file)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with tx() as c:
        pj = c.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
    audit.log(_me_id(request), "bid added", "project", pid, pj["name"] if pj else None)
    return out


@app.patch("/api/bids/{bid_id}")
def patch_bid(bid_id: int, body: BidIn, request: Request):
    _need_project_team(request, _bid_project(bid_id))
    f = body.model_dump(exclude_unset=True, exclude={"filename", "content_type", "data_b64"})
    if "amount" in f and f["amount"] not in (None, ""):
        try:
            f["amount"] = float(str(f["amount"]).replace("$", "").replace(",", ""))
        except ValueError:
            raise HTTPException(400, "Amount must be a number.")
    try:
        bids.update_bid(bid_id, f)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/bids/{bid_id}")
def delete_bid(bid_id: int, request: Request):
    _need_project_team(request, _bid_project(bid_id))
    try:
        bids.delete_bid(bid_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/bids/{bid_id}/file")
def bid_file(bid_id: int):
    got = bids.bid_file(bid_id)
    if not got:
        raise HTTPException(404, "No file")
    return FileResponse(got[1], media_type=got[0]["content_type"] or "application/octet-stream", filename=got[0]["filename"])


@app.put("/api/projects/{pid}/recommendation")
def set_recommendation(pid: int, body: RecIn, request: Request):
    _need_project_team(request, pid)
    f = body.model_dump(exclude_unset=True)
    bids.set_recommendation(pid, f.get("bid_id", None) if "bid_id" in f else None, f.get("text"))
    if "bid_id" in f and not f["bid_id"]:
        bids.set_recommendation(pid, 0, None)
    return {"ok": True}


@app.post("/api/projects/{pid}/recommendation-draft")
def recommendation_draft(pid: int, request: Request):
    _need_project_team(request, pid)
    try:
        return {"text": bids.draft_recommendation(pid)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/projects/{pid}/approval")
def request_owner_approval(pid: int, body: ApprovalIn, request: Request):
    _need_project_team(request, pid)
    try:
        out = bids.request_approval(pid, _me_id(request), body.owner_name, body.owner_email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with tx() as c:
        pj = c.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
    audit.log(_me_id(request), "requested owner approval", "project", pid, pj["name"] if pj else None, body.owner_email)
    return out


# --- the owner's approval page (public; the secret link is the key) ---

def _approval_page(a: dict, msg: str = "", status_code: int = 200):
    from html import escape as h
    pj = a["project"]
    m = lambda n: f"${n:,.0f}" if n is not None else "—"
    rows_html = "".join(
        f"""<label class="bid {'rec' if b['id'] == a['recommended_bid_id'] else ''}">
              {'<input type="radio" name="bid_id" value="%d" %s>' % (b['id'], 'checked' if b['id'] == a['recommended_bid_id'] else '') if a['status'] == 'pending' and not a['expired'] else ''}
              <div class="bm"><div class="bv">{h(b['vendor'] or '')}{' <span class="tag">Recommended</span>' if b['id'] == a['recommended_bid_id'] else ''}</div>
              <div class="bs">{h(b['scope'] or '')}</div>
              <div class="bx">{' · '.join(h(x) for x in [('Excludes: ' + b['exclusions']) if b['exclusions'] else '', ('Timeline: ' + b['timeline']) if b['timeline'] else '', ('Warranty: ' + b['warranty']) if b['warranty'] else ''] if x)}</div>
              {f'<a href="/approve/{a["token"]}/bid/{b["id"]}" target="_blank">View bid ({h(b["filename"])})</a>' if b['path'] else ''}</div>
              <div class="ba">{m(b['amount'])}</div></label>""" for b in a["bids"])
    decided = a["status"] in ("approved", "changes_requested", "declined")
    win = next((b for b in a["bids"] if b["id"] == a["decision_bid_id"]), None)
    result = (f"""<div class="done {a['status']}">{ {'approved': '✅ Approved', 'changes_requested': '✏️ Changes requested', 'declined': '❌ Declined'}[a['status']] }
                 by {h(a['decided_by'] or '')} on {h((a['decided_at'] or '')[:10])}{(' — ' + h(win['vendor']) + ' at ' + m(win['amount'])) if win else ''}
                 {('<div class="note">“' + h(a['decision_note']) + '”</div>') if a['decision_note'] else ''}
                 {f'<div class="note"><a href="/approve/{a["token"]}/receipt" target="_blank">View / print your receipt</a></div>' if a.get('evidence_hash') else ''}</div>""" if decided else
              '<div class="done">This request was replaced by a newer one — please use the latest link you received.</div>' if a["status"] == "superseded" else
              '<div class="done">This request has expired — please contact your property manager.</div>' if a["expired"] else "")
    form = "" if result else f"""
      <label class="lbl">Your name</label><input name="name" required placeholder="Full name">
      <label class="lbl">Title (optional)</label><input name="title" placeholder="e.g. Managing Member" style="width:100%;font:inherit;padding:10px;border:1px solid #cfd6dc;border-radius:6px;font-size:16px">
      <label class="lbl">Comments (optional)</label><textarea name="note" rows="3" placeholder="Anything we should know?"></textarea>
      <label class="consent"><input type="checkbox" name="consent" value="1" required> {h(bids.CONSENT_TEXT.format(owner=pj.get('client') or 'the property owner'))}</label>
      <div class="btns"><button name="decision" value="approved" class="ok">Approve selected bid</button>
      <button name="decision" value="changes_requested" class="mid">Request changes</button>
      <button name="decision" value="declined" class="no">Decline</button></div>"""
    over = (pj.get("spend_limit") and any(b["id"] == a["recommended_bid_id"] and (b["amount"] or 0) > pj["spend_limit"] for b in a["bids"]))
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Approval: {h(pj['name'])} · Foresite</title><link rel="icon" href="/brand/favicon.png">
<link href="https://fonts.googleapis.com/css2?family=Lato:wght@300;400;700&display=swap" rel="stylesheet"><style>
*{{box-sizing:border-box}}body{{margin:0;font-family:Lato,Helvetica,Arial,sans-serif;background:#f3f5f7;color:#26323a}}
header{{background:#3a4750;padding:16px 20px}}header img{{height:34px}}main{{max-width:760px;margin:0 auto;padding:20px 16px 40px}}
h1{{font-size:22px;margin:4px 0}}.sub{{color:#6b7680;font-size:14px}}.card{{background:#fff;border:1px solid #e1e6ea;border-radius:10px;padding:16px;margin:14px 0}}
.bid{{display:flex;gap:12px;align-items:flex-start;padding:12px;border:1px solid #e1e6ea;border-radius:8px;margin:8px 0;cursor:pointer}}
.bid.rec{{border-color:#549fc5;background:#f2f8fb}}.bid input{{margin-top:4px}}.bm{{flex:1}}.bv{{font-weight:700}}.bs{{font-size:14px;margin-top:2px}}
.bx{{font-size:12.5px;color:#6b7680;margin-top:2px}}.ba{{font-weight:700;font-size:18px;white-space:nowrap}}.bid a{{font-size:13px;color:#2f7fa8}}
.tag{{font-size:11px;background:#549fc5;color:#fff;border-radius:99px;padding:2px 8px;margin-left:6px;vertical-align:2px}}
.rec-text{{white-space:pre-wrap;line-height:1.5}}.lbl{{display:block;font-size:13px;color:#6b7680;margin:12px 0 4px}}
input[name=name],textarea{{width:100%;font:inherit;padding:10px;border:1px solid #cfd6dc;border-radius:6px;font-size:16px}}
.btns{{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}}button{{font:inherit;font-weight:700;border:0;border-radius:6px;padding:12px 18px;cursor:pointer;font-size:15px}}
.ok{{background:#3d8b3d;color:#fff}}.mid{{background:#e9eef2;color:#26323a}}.no{{background:#fff;border:1px solid #d9534f!important;color:#c0392b}}
.done{{padding:14px;border-radius:8px;background:#eef6e4;font-weight:700}}.done.changes_requested{{background:#fdf6ea}}.done.declined{{background:#fdecec}}.note{{font-weight:400;margin-top:6px}}
.consent{{display:flex;gap:10px;align-items:flex-start;font-size:13.5px;line-height:1.45;margin-top:14px;background:#f6f8fa;padding:10px;border-radius:6px}}.consent input{{margin-top:3px;width:18px;height:18px;flex:none}}
.err{{background:#fdecec;color:#c0392b;padding:10px;border-radius:6px;margin:10px 0}}.warn{{font-size:13px;background:#fdf6ea;padding:8px 10px;border-radius:6px;margin-top:8px}}
</style></head><body><header><img src="/brand/logo-white.png" alt="Foresite Commercial Real Estate"></header><main>
<div class="sub">Owner approval requested by {h(a['requested_by_name'])} · Foresite Real Estate</div>
<h1>{h(pj['name'])}</h1><div class="sub">{h(pj.get('address') or pj.get('property') or '')}{' · ' + h(pj['client']) if pj.get('client') else ''}</div>
{f'<div class="err">{h(msg)}</div>' if msg else ''}
<form method="post"><div class="card"><b>Bids received</b>{rows_html}
{f'<div class="warn">This is above the {m(pj["spend_limit"])} threshold for capital expenditures and non-emergency repairs that you asked us to bring to you for approval.</div>' if over else ''}</div>
<div class="card"><b>Property manager's recommendation</b><div class="rec-text">{h(a['recommendation'] or '')}</div></div>
<div class="card">{result}{form}</div></form>
<div class="sub" style="text-align:center">Questions? Contact {h(a['requested_by_name'])} · <a href="mailto:{h(a['requested_by_email'])}">{h(a['requested_by_email'])}</a></div>
</main></body></html>""", status_code=status_code)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")) or ""


@app.get("/approve/{token}")
def approval_page(token: str, request: Request):
    a = bids.by_token(token)
    if not a:
        return auth.branded_page("Not found", '<p class="msg">This approval link isn\'t valid.</p>', 404)
    staff = auth.current_user(request)   # Foresite staff previewing the page are logged separately from the owner
    bids.log_event(token, f"viewed (preview by {staff['email']})" if staff else "viewed", _client_ip(request),
                   request.headers.get("user-agent"))
    return _approval_page(a)


def _certificate_html(cert: dict, for_owner: bool = False) -> HTMLResponse:
    from html import escape as h
    e = cert["evidence"]
    m = lambda n: f"${n:,.2f}" if n is not None else "—"
    chosen = next((b for b in e["bids_shown"] if b["bid_id"] == e["chosen_bid_id"]), None)
    dec = {"approved": "APPROVED", "changes_requested": "CHANGES REQUESTED", "declined": "DECLINED"}.get(e["decision"], e["decision"])
    v = cert["verify"]
    rows_ = "".join(f"<tr><td>{h(b['vendor'] or '')}{' <b>(chosen)</b>' if chosen and b['bid_id'] == chosen['bid_id'] else ''}"
                    f"{' (recommended)' if b['bid_id'] == e['recommended_bid_id'] else ''}</td><td>{m(b['amount'])}</td>"
                    f"<td>{h(b['scope'] or '')}</td><td class='mono'>{h(b['file'] or '—')}<br>{h(b['file_sha256'] or '')}</td></tr>" for b in e["bids_shown"])
    evs = "".join(f"<tr><td>{h(x['at'])}</td><td>{h(x['event'])}</td><td>{h(x['ip'] or '')}</td><td>{h((x['user_agent'] or '')[:120])}</td></tr>" for x in cert["events"])
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Approval certificate · {h(e['project'])}</title><style>
body{{font-family:Lato,Helvetica,Arial,sans-serif;color:#222;max-width:900px;margin:24px auto;padding:0 16px;font-size:14px}}
h1{{font-size:20px;margin:0}}.k{{color:#666;width:200px;vertical-align:top}}table{{border-collapse:collapse;width:100%;margin:8px 0 16px}}
td,th{{border:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}}th{{background:#f4f6f8}}.mono{{font-family:ui-monospace,Menlo,monospace;font-size:11px;word-break:break-all}}
.big{{font-size:18px;font-weight:700}}.ok{{color:#2e7d32}}.bad{{color:#c62828}}.btn{{display:inline-block;margin:10px 0;padding:8px 14px;background:#3a4750;color:#fff;border-radius:4px;text-decoration:none}}
@media print{{.noprint{{display:none}}}}</style></head><body>
<img src="/brand/logo.png" alt="Foresite" style="height:40px"><h1>Owner approval certificate</h1>
<p class="noprint"><a class="btn" href="#" onclick="print();return false">Print / Save as PDF</a></p>
<p class="big">{dec}{' — ' + h(chosen['vendor']) + ' at ' + m(chosen['amount']) if chosen else ''}</p>
<table><tr><td class="k">Project</td><td>{h(e['project'])}</td></tr><tr><td class="k">Property</td><td>{h(e.get('address') or e.get('property') or '')}</td></tr>
<tr><td class="k">Owner</td><td>{h(e.get('owner_entity') or '')}</td></tr>
<tr><td class="k">Signed by</td><td><b>{h(e['signed_name'])}</b>{', ' + h(e['signed_title']) if e.get('signed_title') else ''}</td></tr>
<tr><td class="k">Signed at (UTC)</td><td>{h(e['decided_at_utc'])}</td></tr>
<tr><td class="k">From</td><td>IP {h(e.get('decided_ip') or '—')}<br><span class="mono">{h(e.get('decided_user_agent') or '')}</span></td></tr>
<tr><td class="k">Link sent to</td><td>{h((e['sent_to'] or {}).get('name') or '')} &lt;{h((e['sent_to'] or {}).get('email') or '—')}&gt; by {h(e['requested_by']['name'])} ({h(e['requested_by']['email'])}) on {h(e['requested_at'] or '')}</td></tr>
<tr><td class="k">Comment</td><td>{h(e.get('comment') or '—')}</td></tr>
<tr><td class="k">Electronic signature consent</td><td>☑ {h(e['consent'])} <span style="color:#888">(v{h(e['consent_version'])})</span></td></tr></table>
<b>Bids shown to the signer</b><table><tr><th>Vendor</th><th>Amount</th><th>Scope</th><th>File &amp; SHA-256 fingerprint</th></tr>{rows_}</table>
<b>Recommendation shown</b><p style="white-space:pre-wrap">{h(e.get('recommendation_shown') or '')}</p>
<b>Link activity</b><table><tr><th>Time (UTC)</th><th>Event</th><th>IP</th><th>Device</th></tr>{evs}</table>
<b>Record fingerprint (SHA-256)</b><p class="mono">{h(cert['hash'])}</p>
<p class="{'ok' if v['ok'] else 'bad'}">{'✔ Verified: this record and the bid files are unchanged since signing.' if v['ok'] else '✖ Warning: the record or a bid file no longer matches its fingerprint.'}</p>
<p style="color:#888;font-size:12px">{'Keep this receipt for your records.' if for_owner else 'Generated by Foresite Task Manager. The fingerprint is computed over the full signed record; any change to it (or to a bid file) changes the fingerprint.'}</p>
</body></html>""")


@app.get("/api/approvals/{aid}/certificate")
def approval_certificate(aid: int):
    cert = bids.certificate(aid)
    if not cert:
        raise HTTPException(404, "No signed record for this approval yet.")
    return _certificate_html(cert)


@app.get("/approve/{token}/receipt")
def approval_receipt(token: str):
    a = bids.by_token(token)
    cert = bids.certificate(a["id"]) if a else None
    if not cert:
        raise HTTPException(404, "Not found")
    return _certificate_html(cert, for_owner=True)


@app.post("/approve/{token}")
async def approval_submit(token: str, request: Request):
    form = await request.form()
    try:
        bids.decide(token, form.get("decision", ""), int(form["bid_id"]) if form.get("bid_id") else None,
                    form.get("name", ""), form.get("note", ""), title=form.get("title", ""), consent=bool(form.get("consent")),
                    ip=_client_ip(request), ua=request.headers.get("user-agent"))
    except ValueError as e:
        a = bids.by_token(token)
        return _approval_page(a, str(e), 400) if a else auth.branded_page("Not found", '<p class="msg">Not found.</p>', 404)
    return _approval_page(bids.by_token(token))


@app.get("/approve/{token}/bid/{bid_id}")
def approval_bid_file(token: str, bid_id: int):
    a = bids.by_token(token)
    if not a or bid_id not in [b["id"] for b in a["bids"]]:
        raise HTTPException(404, "Not found")
    got = bids.bid_file(bid_id)
    if not got:
        raise HTTPException(404, "No file")
    return FileResponse(got[1], media_type=got[0]["content_type"] or "application/octet-stream", filename=got[0]["filename"])


# ---------- fun: today ring, streaks, badges, team wins, high-fives ----------

@app.get("/api/fun/me")
def fun_me(request: Request):
    return fun.me(_me_id(request))


@app.get("/api/fun/team")
def fun_team(request: Request):
    return {"today": fun.team_today(), "wins": fun.feed(30, _me_id(request))}


@app.post("/api/wins/{wid}/kudos")
def give_kudos(wid: int, request: Request):
    try:
        return fun.kudo(wid, _me_id(request))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------- Foresite's own certificates of insurance (per property) ----------

class OurCoiPatch(BaseModel):
    expires: str | None = None
    owner_named: bool | None = None
    meets_reqs: bool | None = None

class AgentIn(BaseModel):
    name: str | None = None
    email: str | None = None
    company: str | None = None


@app.post("/api/properties/{pid}/our-coi")
def upload_our_coi(pid: int, body: DocIn, request: Request):
    if not _can_assign(request, property_id=pid):
        raise HTTPException(403, "Only managers or people on this property's team can upload our certificates.")
    import base64
    data = base64.b64decode(body.data_b64)
    if len(data) > intake.MAX_DOC_BYTES:
        raise HTTPException(413, "File too large (25 MB max).")
    out = ourcoi.save(pid, data, body.filename, body.content_type, _me_id(request))
    p = properties.get(pid)
    audit.log(_me_id(request), "uploaded our COI", "property", pid, p["name"] if p else None, out["status"]["label"])
    return out


@app.get("/api/our-cois/{cid}")
def our_coi_file(cid: int):
    got = ourcoi.file(cid)
    if not got:
        raise HTTPException(404, "File not found")
    return FileResponse(got[1], media_type=got[0]["content_type"] or "application/octet-stream", filename=got[0]["filename"])


@app.patch("/api/our-cois/{cid}")
def patch_our_coi(cid: int, body: OurCoiPatch, request: Request):
    with tx() as c:
        k = c.execute("SELECT property_id FROM our_cois WHERE id=?", (cid,)).fetchone()
    if not k or not _can_assign(request, property_id=k["property_id"]):
        raise HTTPException(403, "Only managers or people on this property's team can update our certificates.")
    f = body.model_dump(exclude_unset=True)
    if f.get("expires"):
        try:
            f["expires"] = date.fromisoformat(f["expires"][:10]).isoformat()
        except ValueError:
            raise HTTPException(400, "Expiration must be a date.")
    ourcoi.update(cid, f, _me_id(request))
    return {"ok": True}


@app.delete("/api/our-cois/{cid}")
def delete_our_coi(cid: int, request: Request):
    _require_manager(request)
    ourcoi.delete(cid)
    return {"ok": True}


@app.post("/api/properties/{pid}/our-coi/request")
def request_our_coi(pid: int, request: Request):
    if not _can_assign(request, property_id=pid):
        raise HTTPException(403, "Only managers or people on this property's team can do that.")
    did = ourcoi.draft_request(pid, _me_id(request))
    return emails.get(did)


@app.get("/api/our-coi/agent")
def get_agent():
    return ourcoi.agent()


@app.put("/api/our-coi/agent")
def put_agent(body: AgentIn, request: Request):
    _require_manager(request)
    ourcoi.set_agent(body.model_dump())
    return ourcoi.agent()


# ---------- in-app help assistant ----------

class HelpIn(BaseModel):
    messages: list[dict]
    page: str | None = None

@app.post("/api/help")
def help_ask(body: HelpIn, request: Request):
    me = auth.current_user(request) or {}
    with tx() as c:
        u = c.execute("SELECT id, name, role FROM users WHERE id=?", (me.get("id"),)).fetchone()
    return helpbot.ask(body.messages, dict(u) if u else {"id": None, "name": "", "role": "staff"}, body.page)


# ---------- month-end close board ----------

@app.get("/api/close-board")
def close_board(period: str | None = None, month: str | None = None, template_id: int | None = None):
    return closeboard.board(period or month, template_id)   # bad/mismatched periods fall back to the current one


# ---------- recently deleted + activity log ----------

@app.get("/api/trash")
def list_trash(request: Request):
    _require_manager(request)
    return audit.bin_items()


@app.post("/api/trash/{trash_id}/restore")
def restore_trash(trash_id: int, request: Request):
    _require_manager(request)
    try:
        return audit.restore(trash_id, _me_id(request))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/activity")
def list_activity(request: Request, limit: int = 200, user_id: int | None = None, entity: str | None = None):
    _require_manager(request)
    return audit.activity(limit, user_id, entity)


# ---------- dashboards, reminders, daily recap ----------
@app.get("/api/dashboard")
def get_dashboard(request: Request, scope: str = "me"):
    me = auth.current_user(request) or {}
    if scope == "team":
        if not auth.at_least(me, "manager"):
            raise HTTPException(403, "Only managers can see the team view.")
        return dashboard.team_dashboard()
    uid = me.get("id")
    if uid is None:  # local dev without sign-in: show the first person
        with tx() as c:
            r = c.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        uid = r["id"] if r else 0
    return dashboard.my_dashboard(uid)


class ReminderIn(BaseModel):
    text: str
    remind_at: str            # "YYYY-MM-DDTHH:MM" local
    task_id: int | None = None

def _me_id(request: Request) -> int:
    uid = (auth.current_user(request) or {}).get("id")
    if uid is None:
        with tx() as c:
            r = c.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        uid = r["id"] if r else None
    if uid is None:
        raise HTTPException(400, "No user")
    return uid

@app.post("/api/reminders")
def add_reminder(body: ReminderIn, request: Request):
    try:
        return {"id": dashboard.add_reminder(_me_id(request), body.text, body.remind_at, body.task_id)}
    except ValueError:
        raise HTTPException(400, "Pick a date and time for the reminder.")

@app.patch("/api/reminders/{rid}")
def done_reminder(rid: int, request: Request):
    with tx() as c:
        c.execute("UPDATE reminders SET done=1 WHERE id=? AND user_id=?", (rid, _me_id(request)))
    return {"ok": True}

@app.delete("/api/reminders/{rid}")
def delete_reminder(rid: int, request: Request):
    with tx() as c:
        c.execute("DELETE FROM reminders WHERE id=? AND user_id=?", (rid, _me_id(request)))
    return {"ok": True}

class PrefsIn(BaseModel):
    daily_digest: bool | None = None
    week_ahead: bool | None = None

@app.patch("/api/me/prefs")
def my_prefs(body: PrefsIn, request: Request):
    if body.daily_digest is not None:
        with tx() as c:
            c.execute("UPDATE users SET daily_digest=? WHERE id=?", (int(body.daily_digest), _me_id(request)))
    if body.week_ahead is not None:
        with tx() as c:
            c.execute("UPDATE users SET week_ahead=? WHERE id=?", (int(body.week_ahead), _me_id(request)))
    return {"ok": True}

# ---------- Blink notification settings (per person) ----------
def _sees_team_alerts(request: Request) -> bool:
    if auth.is_manager(request):
        return True
    with tx() as c:
        return bool(c.execute("SELECT 1 FROM departments WHERE head_user_id=?", (_me_id(request),)).fetchone())

@app.get("/api/me/notifications")
def my_notifications(request: Request):
    return notify.settings_for(_me_id(request), _sees_team_alerts(request))

@app.put("/api/me/notifications")
def my_notifications_save(body: dict, request: Request):
    notify.save(_me_id(request), body)
    return notify.settings_for(_me_id(request), _sees_team_alerts(request))

@app.post("/api/me/notifications/reset")
def my_notifications_reset(request: Request):
    notify.save(_me_id(request), {**{k: v for k, v in notify.DEFAULT.items() if k != "cats"}, "cats": dict(notify.DEFAULT["cats"])})
    return notify.settings_for(_me_id(request), _sees_team_alerts(request))

@app.post("/api/me/notifications/preview")
def my_notifications_preview(request: Request, send: bool = False):
    uid = _me_id(request)
    text = notify.build_summary(uid)
    sent = notify.run_summaries(force_user=uid) if send else 0
    return {"text": text, "sent": sent}


@app.post("/api/me/week-ahead-preview")
def week_ahead_preview(request: Request):
    uid = _me_id(request)
    return {"sent": extras.send_week_ahead(force=True, only_user=uid), "text": extras.build_week_ahead(uid)}

# ---------- customizable Home: layout + sticky notes (per person) ----------
@app.get("/api/me/home")
def my_home(request: Request):
    from . import home
    return home.get(_me_id(request))

@app.put("/api/me/home/layout")
def my_home_layout(body: dict, request: Request):
    from . import home
    home.set_layout(_me_id(request), body.get("layout"))
    return {"ok": True}

@app.post("/api/me/notes")
def my_note_add(body: dict, request: Request):
    from . import home
    try:
        return {"id": home.add_note(_me_id(request), body.get("text") or "", body.get("color") or "yellow")}
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.patch("/api/me/notes/{nid}")
def my_note_edit(nid: int, body: dict, request: Request):
    from . import home
    if not home.update_note(_me_id(request), nid, body):
        raise HTTPException(404, "Note not found")
    return {"ok": True}

@app.delete("/api/me/notes/{nid}")
def my_note_delete(nid: int, request: Request):
    from . import home
    if not home.delete_note(_me_id(request), nid):
        raise HTTPException(404, "Note not found")
    return {"ok": True}


@app.get("/api/me/unread")
def my_unread(request: Request):
    m = extras.unread_mentions(_me_id(request))
    return {"mentions": len(m), "comment_ids": [x["id"] for x in m]}

@app.post("/api/me/mentions-seen")
def mentions_seen(request: Request):
    extras.mark_mentions_seen(_me_id(request))
    return {"ok": True}

def _base_url(request: Request) -> str:
    return (os.getenv("PUBLIC_URL") or str(request.base_url)).rstrip("/")

@app.get("/api/me/calendar")
def my_calendar_link(request: Request, regenerate: bool = False):
    tok = extras.calendar_token(_me_id(request), regenerate)
    url = f"{_base_url(request)}/calendar/{tok}.ics"
    webcal = re.sub(r"^https?://", "webcal://", url)
    return {"url": url, "webcal": webcal,
            "google": "https://calendar.google.com/calendar/r?cid=" + urllib.parse.quote(webcal, safe="")}

@app.get("/calendar/{token}.ics")
def calendar_feed(token: str, request: Request):
    u = extras.user_for_token(token)
    if not u:
        raise HTTPException(404, "Unknown calendar link")
    return Response(extras.build_ics(u["id"], _base_url(request)), media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})

@app.post("/api/me/digest-preview")
def digest_preview(request: Request):
    """Send my 6pm recap to me now (to see what it looks like)."""
    uid = _me_id(request)
    return {"sent": dashboard.send_daily_digests(force=True, only_user=uid), "text": dashboard.build_digest(uid)}


@app.get("/api/demo")
def demo_status():
    return demo.status()

@app.post("/api/demo/load")
def demo_load(request: Request):
    try:
        return demo.load(_me_id(request))
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.post("/api/demo/fill")
def demo_fill(request: Request):
    if not auth.is_admin(request):
        raise HTTPException(403, "Only the admin can do that.")
    return demo.fill(_me_id(request))

@app.delete("/api/demo")
def demo_remove():
    return demo.remove()


@app.get("/api/automations")
def get_automations():
    return automations.settings()

@app.put("/api/automations")
def put_automations(body: dict):
    automations.update_settings(body)
    return automations.settings()


@app.get("/api/tasks/{tid}/updates")
def task_updates(tid: int):
    with tx() as c:
        return rows(c.execute("""SELECT tu.*, u.name AS user_name FROM task_updates tu
                                 LEFT JOIN users u ON u.id=tu.user_id WHERE task_id=? ORDER BY tu.id DESC""", (tid,)))


# ---------- Blink ----------
@app.post("/blink/webhook")
async def blink_webhook(request: Request, background: BackgroundTasks):
    """Webhook URL for the Blink App Identity. Subscribe to `blink.chat.message-sent`."""
    raw = await request.body()
    if not blink.verify_webhook(raw, request.headers):
        raise HTTPException(401, "invalid signature")
    event = await request.json()
    event_type = (_find(event, ("@type", "type", "event_type")) or request.headers.get("x-event-type", ""))
    chat_id = _find(event, ("chat_id",))
    message_id = _find(event, ("local_message_id", "message_id"))
    logging.info("Blink webhook: type=%s chat=%s message=%s top-level keys=%s",
                 event_type, chat_id, message_id, list(event)[:10] if isinstance(event, dict) else type(event))
    if "message-sent" in str(event_type) and chat_id and message_id:
        background.add_task(_process_chat_message, chat_id, message_id)
    return {"ok": True}  # ack fast; Blink retries slow/failed deliveries


def _find(obj, keys):
    """First string value under any of `keys`, searching nested dicts/lists (Blink may wrap the event)."""
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for v in children:
        found = _find(v, keys)
        if found:
            return found
    return None


def _process_chat_message(chat_id: str, message_id: str):
    try:
        msg = blink.get_message(chat_id, message_id)
    except Exception:
        logging.exception("couldn't fetch Blink message %s/%s", chat_id, message_id)
        return
    sender = msg.get("sender", {})
    if sender.get("@type") != "user":  # ignore our own (and other apps') messages
        logging.info("Blink message %s from %s - ignored", message_id, sender.get("@type"))
        return
    user_id = sender.get("user_id")
    logging.info("Blink message from %s in %s", user_id, chat_id)
    with tx() as c:
        u = c.execute("SELECT blink_chat_id FROM users WHERE blink_user_id=?", (user_id,)).fetchone()
    # Only act on the person's 1:1 chat with the bot, not group chats the app was /addapp'ed to
    if u and u["blink_chat_id"] and u["blink_chat_id"] != chat_id:
        logging.info("message in %s is not %s's 1:1 bot chat - ignored", chat_id, user_id)
        return
    if u and not u["blink_chat_id"]:
        with tx() as c:
            c.execute("UPDATE users SET blink_chat_id=? WHERE blink_user_id=?", (chat_id, user_id))
    text = blink.message_text(msg)
    files = [f for f in (msg.get("files") or []) if f.get("attachment_id")]
    if text or files:
        services.handle_inbound(user_id, text, files)


@app.post("/api/blink/simulate")
def blink_simulate(msg: InboundMessage):
    """Used by the web UI's chat simulator. Disabled when connected to real Blink unless ENABLE_SIMULATOR=1."""
    if not SIMULATOR:
        raise HTTPException(404, "simulator disabled")
    return {"reply": services.handle_inbound(msg.blink_user_id, msg.text)}

@app.get("/api/blink/messages")
def blink_messages(blink_user_id: str):
    with tx() as c:
        return rows(c.execute("SELECT * FROM blink_messages WHERE blink_user_id=? ORDER BY id", (blink_user_id,)))

@app.post("/api/reminders/run")
def run_reminders():
    return {"sent": services.send_reminders(force=True), "mock": blink.MOCK}
