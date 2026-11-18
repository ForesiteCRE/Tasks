import asyncio
import logging
import os
import re
import urllib.parse
from contextlib import asynccontextmanager
from datetime import date, datetime
from datetime import date as _date

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import (audit, auth, automations, blink, chat, closeboard, vendors, collab, dashboard, demo, extras, intake, maintenance, photos, properties, recur, services,
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
                    dashboard.send_daily_digests):
            try:
                job()
            except Exception:
                logging.exception("scheduler job %s failed", job.__name__)
        if datetime.now().hour >= REMINDER_HOUR:
            for job in (services.send_reminders, maintenance.send_weekly_summary, automations.overdue_escalation,
                        extras.send_week_ahead, properties.run_alerts, audit.purge):
                try:
                    job()
                except Exception:
                    logging.exception("scheduler job %s failed", job.__name__)
        await asyncio.sleep(60)  # every job is idempotent (guarded per day/week/task)


@asynccontextmanager
async def lifespan(_):
    init_db()
    tpl.init()
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
    return FileResponse(os.path.join(STATIC, "index.html"))


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
        c.execute(f"UPDATE tasks SET project_id=?, role_id=NULL WHERE id IN ({','.join('?' * len(ids_))})", (pid, *ids_))
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
        tid = c.execute(
            "INSERT INTO tasks(project_id,parent_id,title,description,assignee_id,due_date,position) VALUES(?,?,?,?,?,?,999)",
            (t.project_id, t.parent_id, t.title, t.description, t.assignee_id,
             t.due_date and t.due_date.isoformat())).lastrowid
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
        blink.send_dm(blink_user_id, text)
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
    if p.estimate is not None or p.actual is not None or p.time_notes is not None:
        def mins(v):
            if v is None:
                return None
            m = timing.parse_duration(v)
            if m is None and str(v).strip():
                raise HTTPException(400, f"Couldn't read “{v}” as a duration — try 1h30, 45m or 2h.")
            return m or 0
        timing.set_times(tid, estimate=mins(p.estimate), actual=mins(p.actual), notes=p.time_notes)
    if p.status or p.note:
        services.apply_update(tid, p.user_id, "web", p.status, p.note)
    return {"ok": True}

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
    return {"vendors": vendors.list_all(trade, q, archived), "trades": vendors.TRADES}


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


# ---------- month-end close board ----------

@app.get("/api/close-board")
def close_board(month: str | None = None, template_id: int | None = None):
    import re as _re
    if month and not _re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(400, "month must look like 2026-11")
    return closeboard.board(month, template_id)


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

@app.post("/api/me/week-ahead-preview")
def week_ahead_preview(request: Request):
    uid = _me_id(request)
    return {"sent": extras.send_week_ahead(force=True, only_user=uid), "text": extras.build_week_ahead(uid)}

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
