import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import auth, blink, services
from .db import init_db, rows, tx

logging.basicConfig(level=logging.INFO)
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))
STATIC = os.path.join(os.path.dirname(__file__), "..", "static")


async def scheduler_loop():
    """Every 10 min: spawn due monthly projects; after REMINDER_HOUR send daily reminders (once/day/task)."""
    while True:
        try:
            services.run_recurrences()
            if datetime.now().hour >= REMINDER_HOUR:
                services.send_reminders()
        except Exception:
            logging.exception("scheduler tick failed")
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(_):
    init_db()
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


@app.get("/healthz")
def healthz():
    """Which settings the running app can see (yes/no only - never values)."""
    names = ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "PUBLIC_URL", "SESSION_SECRET",
             "BLINK_CLIENT_ID", "BLINK_CLIENT_SECRET", "BLINK_WEBHOOK_SECRET", "BLINK_LEGACY_TOKEN", "DB_PATH", "TZ"]
    return {"ok": True, "sign_in_enabled": auth.ENABLED, "blink_connected": not blink.MOCK,
            "settings_present": {n: bool(os.getenv(n, "").strip()) for n in names},
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
    name: str
    template_id: int | None = None
    start_date: date | None = None

class TaskIn(BaseModel):
    project_id: int
    title: str
    assignee_id: int | None = None
    due_date: date | None = None

class TaskPatch(BaseModel):
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
        return rows(c.execute("SELECT * FROM users ORDER BY name"))

class UserPatch(BaseModel):
    name: str | None = None
    blink_user_id: str | None = None

@app.patch("/api/users/{uid}")
def patch_user(uid: int, p: UserPatch):
    with tx() as c:
        if p.name:
            c.execute("UPDATE users SET name=? WHERE id=?", (p.name, uid))
        if p.blink_user_id is not None:  # changing the Blink id resets the cached 1:1 chat
            c.execute("UPDATE users SET blink_user_id=?, blink_chat_id=NULL WHERE id=?",
                      (p.blink_user_id.strip() or None, uid))
    return {"ok": True}

@app.get("/api/config")
def config():
    return {"simulator": SIMULATOR, "blink_mock": blink.MOCK}

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
    """Look up Blink ids by email for everyone who doesn't have one yet."""
    with tx() as c:
        todo = rows(c.execute("SELECT id, email FROM users WHERE blink_user_id IS NULL AND email IS NOT NULL"))
    found, missing = 0, []
    for u in todo:
        try:
            buid = blink.find_user_id_by_email(u["email"])
        except PermissionError as e:
            return {"error": str(e), "found": found}
        if buid:
            with tx() as c:
                c.execute("UPDATE users SET blink_user_id=? WHERE id=?", (buid, u["id"]))
            found += 1
        else:
            missing.append(u["email"])
    return {"found": found, "not_found": missing}


# ---------- templates ----------
@app.get("/api/templates")
def list_templates():
    with tx() as c:
        ts = rows(c.execute("SELECT * FROM templates ORDER BY name"))
        for t in ts:
            t["items"] = rows(c.execute("SELECT * FROM template_items WHERE template_id=? ORDER BY position,id", (t["id"],)))
        return ts

@app.post("/api/templates")
def create_template(t: TemplateIn):
    with tx() as c:
        tid = c.execute("INSERT INTO templates(name,description) VALUES(?,?)", (t.name, t.description)).lastrowid
        c.executemany(
            "INSERT INTO template_items(template_id,title,default_assignee_id,due_offset_days,position) VALUES(?,?,?,?,?)",
            [(tid, i.title, i.default_assignee_id, i.due_offset_days, n) for n, i in enumerate(t.items)])
    return {"id": tid}

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
                """SELECT t.*, u.name AS assignee_name FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id
                   WHERE project_id=? ORDER BY position,id""", (p["id"],)))
        return ps

@app.post("/api/projects")
def create_project(p: ProjectIn):
    start = p.start_date or date.today()
    if p.template_id:
        return {"id": services.create_project_from_template(p.template_id, p.name, start)}
    with tx() as c:
        return {"id": c.execute("INSERT INTO projects(name,start_date) VALUES(?,?)", (p.name, start.isoformat())).lastrowid}

@app.post("/api/projects/{pid}/save-as-template")
def save_as_template(pid: int, body: SaveAsTemplate):
    return {"id": services.save_project_as_template(pid, body.name)}


# ---------- tasks ----------
@app.post("/api/tasks")
def create_task(t: TaskIn):
    with tx() as c:
        tid = c.execute("INSERT INTO tasks(project_id,title,assignee_id,due_date,position) VALUES(?,?,?,?,999)",
                        (t.project_id, t.title, t.assignee_id, t.due_date and t.due_date.isoformat())).lastrowid
        u = c.execute("SELECT blink_user_id FROM users WHERE id=?", (t.assignee_id,)).fetchone() if t.assignee_id else None
    if u and u["blink_user_id"]:
        blink.send_dm(u["blink_user_id"], f"📌 New task assigned: #{tid} {t.title}"
                                          + (f" – due {t.due_date:%b %d}" if t.due_date else ""))
    return {"id": tid}

@app.patch("/api/tasks/{tid}")
def patch_task(tid: int, p: TaskPatch, request: Request):
    p.user_id = (auth.current_user(request) or {}).get("id")
    with tx() as c:
        if p.assignee_id is not None:
            c.execute("UPDATE tasks SET assignee_id=? WHERE id=?", (p.assignee_id, tid))
        if p.due_date is not None:
            c.execute("UPDATE tasks SET due_date=? WHERE id=?", (p.due_date.isoformat(), tid))
    if p.status or p.note:
        services.apply_update(tid, p.user_id, "web", p.status, p.note)
    return {"ok": True}

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
    if event.get("@type") == "blink.chat.message-sent":
        mid = event.get("detail", {}).get("message_id", {})
        background.add_task(_process_chat_message, mid.get("chat_id"), mid.get("local_message_id"))
    return {"ok": True}  # ack fast; Blink retries slow/failed deliveries


def _process_chat_message(chat_id: str, message_id: str):
    try:
        msg = blink.get_message(chat_id, message_id)
    except Exception:
        logging.exception("couldn't fetch Blink message %s/%s", chat_id, message_id)
        return
    sender = msg.get("sender", {})
    if sender.get("@type") != "user":  # ignore our own (and other apps') messages
        return
    user_id = sender.get("user_id")
    with tx() as c:
        u = c.execute("SELECT blink_chat_id FROM users WHERE blink_user_id=?", (user_id,)).fetchone()
    # Only act on the person's 1:1 chat with the bot, not group chats the app was /addapp'ed to
    if u and u["blink_chat_id"] and u["blink_chat_id"] != chat_id:
        return
    if u and not u["blink_chat_id"]:
        with tx() as c:
            c.execute("UPDATE users SET blink_chat_id=? WHERE blink_user_id=?", (chat_id, user_id))
    text = blink.message_text(msg)
    if text:
        services.handle_inbound(user_id, text)


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
