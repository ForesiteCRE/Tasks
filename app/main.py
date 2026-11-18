import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import blink, services
from .db import init_db, rows, tx

logging.basicConfig(level=logging.INFO)
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))
WEBHOOK_SECRET = os.getenv("BLINK_WEBHOOK_SECRET", "")
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

@app.post("/api/users")
def create_user(u: UserIn):
    with tx() as c:
        return {"id": c.execute("INSERT INTO users(name,email,blink_user_id) VALUES(?,?,?)",
                                (u.name, u.email, u.blink_user_id)).lastrowid}


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
def patch_task(tid: int, p: TaskPatch):
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
def blink_webhook(msg: InboundMessage, x_webhook_secret: str = Header(default="")):
    """Inbound chat message from Blink (directly, or via a relay such as Make/Zapier/tray.ai)."""
    if WEBHOOK_SECRET and x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(401, "bad secret")
    return {"reply": services.handle_inbound(msg.blink_user_id, msg.text)}

@app.post("/api/blink/simulate")
def blink_simulate(msg: InboundMessage):
    """Used by the web UI's chat simulator (no secret needed; disable in production)."""
    return {"reply": services.handle_inbound(msg.blink_user_id, msg.text)}

@app.get("/api/blink/messages")
def blink_messages(blink_user_id: str):
    with tx() as c:
        return rows(c.execute("SELECT * FROM blink_messages WHERE blink_user_id=? ORDER BY id", (blink_user_id,)))

@app.post("/api/reminders/run")
def run_reminders():
    return {"sent": services.send_reminders(force=True), "mock": blink.MOCK}
