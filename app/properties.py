"""Properties and their critical dates.

A property is one address. Projects are linked to it automatically by their "Property / address" text, so the
property page can show every project, open task and person working on that building.

Critical dates (lease expirations, option-notice deadlines, rent bumps, CAM reconciliations, COI expirations, tax
protest deadlines …) can be typed in or pulled out of an uploaded lease by Claude. A daily job sends Blink reminders
at each "remind before" offset (default 90/30/7 days) and, once the first reminder goes out, adds a task (due on the
date) for the person responsible so it shows up in their Home, calendar and recap. Finishing that task marks the
date handled.
"""
import json
import logging
import re
from datetime import date, timedelta

from . import automations, blink, team
from .db import rows, tx

log = logging.getLogger("properties")

KINDS = {
    "lease_expiration": "Lease expiration",
    "option_notice": "Renewal / option notice deadline",
    "rent_increase": "Rent increase",
    "cam_recon": "CAM reconciliation due",
    "coi_expiry": "Tenant insurance (COI) expires",
    "tax_protest": "Property tax protest deadline",
    "insurance_renewal": "Property insurance renewal",
    "loan": "Loan / lender deadline",
    "other": "Other",
}
ICONS = {"lease_expiration": "📄", "option_notice": "⚑", "rent_increase": "📈", "cam_recon": "🧮", "coi_expiry": "🛡",
         "tax_protest": "🏛", "insurance_renewal": "☂️", "loan": "🏦", "other": "📌"}
DEFAULT_REMIND = "90,30,7"


def init():
    with tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS properties(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL, norm TEXT NOT NULL UNIQUE,
            client TEXT, notes TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        c.execute("""CREATE TABLE IF NOT EXISTS critical_dates(
            id INTEGER PRIMARY KEY, property_id INTEGER NOT NULL REFERENCES properties(id), kind TEXT NOT NULL,
            title TEXT NOT NULL, date TEXT NOT NULL, tenant TEXT, notes TEXT, assignee_id INTEGER,
            remind_days TEXT NOT NULL DEFAULT '90,30,7', status TEXT NOT NULL DEFAULT 'open', source TEXT, quote TEXT,
            task_id INTEGER, handled_at TEXT, created_by INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        c.execute("""CREATE TABLE IF NOT EXISTS critical_date_alerts(
            date_id INTEGER NOT NULL, offset_days INTEGER NOT NULL, sent_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY(date_id, offset_days))""")
        if "property_id" not in {r[1] for r in c.execute("PRAGMA table_info(projects)")}:
            c.execute("ALTER TABLE projects ADD COLUMN property_id INTEGER")
    link_all()


# ---------- properties ----------

def norm(address: str) -> str:
    a = re.sub(r"[^a-z0-9 ]", " ", (address or "").lower())
    for long, short in [("street", "st"), ("boulevard", "blvd"), ("avenue", "ave"), ("road", "rd"), ("drive", "dr"),
                        ("east", "e"), ("west", "w"), ("north", "n"), ("south", "s"), ("suite", "ste"), ("texas", "tx")]:
        a = re.sub(rf"\b{long}\b", short, a)
    return re.sub(r"\s+", " ", a).strip()


def _short_name(address: str) -> str:
    return (address or "").split(",")[0].strip() or address


def find_or_create(address: str, client: str | None = None, c=None) -> int | None:
    n = norm(address)
    if not n:
        return None
    def go(c):
        r = c.execute("SELECT id FROM properties WHERE norm=?", (n,)).fetchone()
        if r:
            return r["id"]
        return c.execute("INSERT INTO properties(name, address, norm, client) VALUES(?,?,?,?)",
                         (_short_name(address), address.strip(), n, client)).lastrowid
    if c is not None:
        return go(c)
    with tx() as c2:
        return go(c2)


def link_all():
    """Attach every project that has an address but no property yet (cheap; safe to call often)."""
    with tx() as c:
        for p in c.execute("""SELECT id, property, client FROM projects WHERE property_id IS NULL AND is_general=0
                              AND COALESCE(TRIM(property),'')!=''""").fetchall():
            c.execute("UPDATE projects SET property_id=? WHERE id=?", (find_or_create(p["property"], p["client"], c), p["id"]))


def relink(project_id: int):
    with tx() as c:
        c.execute("UPDATE projects SET property_id=NULL WHERE id=?", (project_id,))
    link_all()


def create(address: str, name: str | None = None, client: str | None = None, notes: str | None = None) -> int:
    pid = find_or_create(address, client)
    if pid is None:
        raise ValueError("Enter the property's address.")
    update(pid, {k: v for k, v in {"name": name, "client": client, "notes": notes}.items() if v})
    return pid


def update(pid: int, fields: dict):
    allowed = {k: v for k, v in fields.items() if k in ("name", "client", "notes", "address") and v is not None}
    if "address" in allowed:
        if not norm(allowed["address"]):
            raise ValueError("Address can't be empty.")
        allowed["norm"] = norm(allowed["address"])
    if not allowed:
        return
    with tx() as c:
        c.execute(f"UPDATE properties SET {', '.join(k + '=?' for k in allowed)} WHERE id=?", (*allowed.values(), pid))
        if "address" in allowed:  # keep the projects' address text in step
            c.execute("UPDATE projects SET property=? WHERE property_id=?", (allowed["address"], pid))


def delete(pid: int):
    with tx() as c:
        c.execute("UPDATE projects SET property_id=NULL WHERE property_id=?", (pid,))
        ids = [r[0] for r in c.execute("SELECT id FROM critical_dates WHERE property_id=?", (pid,))]
        c.executemany("DELETE FROM critical_date_alerts WHERE date_id=?", [(i,) for i in ids])
        c.execute("DELETE FROM critical_dates WHERE property_id=?", (pid,))
        c.execute("DELETE FROM properties WHERE id=?", (pid,))


def _date_row(d: dict, today: date) -> dict:
    d["kind_label"] = KINDS.get(d["kind"], d["kind"])
    d["icon"] = ICONS.get(d["kind"], "📌")
    d["days_left"] = (date.fromisoformat(d["date"]) - today).days
    return d


def list_all() -> list[dict]:
    link_all()
    today = date.today()
    with tx() as c:
        props = rows(c.execute("SELECT * FROM properties ORDER BY name COLLATE NOCASE"))
        for p in props:
            s = c.execute("""SELECT COUNT(DISTINCT pr.id) AS projects,
                                    SUM(t.status!='done') AS open_tasks,
                                    SUM(t.status!='done' AND t.due_date < ?) AS overdue
                             FROM projects pr LEFT JOIN tasks t ON t.project_id=pr.id
                             WHERE pr.property_id=? AND pr.status='active'""", (today.isoformat(), p["id"])).fetchone()
            p.update({k: s[k] or 0 for k in ("projects", "open_tasks", "overdue")})
            nxt = c.execute("""SELECT * FROM critical_dates WHERE property_id=? AND status='open'
                               ORDER BY date LIMIT 1""", (p["id"],)).fetchone()
            p["next_date"] = _date_row(dict(nxt), today) if nxt else None
            p["open_dates"] = c.execute("SELECT COUNT(*) FROM critical_dates WHERE property_id=? AND status='open'",
                                        (p["id"],)).fetchone()[0]
            ph = c.execute("""SELECT id, photo_path FROM projects WHERE property_id=? ORDER BY photo_path IS NULL, id DESC
                              LIMIT 1""", (p["id"],)).fetchone()
            p["photo_project_id"], p["photo_path"] = (ph["id"], ph["photo_path"]) if ph else (None, None)
    return props


def get(pid: int) -> dict | None:
    link_all()
    today = date.today()
    with tx() as c:
        p = c.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
        if not p:
            return None
        p = dict(p)
        projects = rows(c.execute("""SELECT pr.id, pr.name, pr.status, pr.start_date, pr.photo_path,
                                            COUNT(t.id) AS total, SUM(t.status='done') AS done,
                                            SUM(t.status!='done' AND t.due_date < ?) AS overdue
                                     FROM projects pr LEFT JOIN tasks t ON t.project_id=pr.id
                                     WHERE pr.property_id=? GROUP BY pr.id
                                     ORDER BY pr.status='done', pr.start_date DESC""", (today.isoformat(), pid)))
        tasks = rows(c.execute("""SELECT t.id, t.title, t.status, t.due_date, t.assignee_id, t.project_id,
                                         COALESCE(u.name,'Unassigned') AS assignee_name, pr.name AS project_name
                                  FROM tasks t JOIN projects pr ON pr.id=t.project_id LEFT JOIN users u ON u.id=t.assignee_id
                                  WHERE pr.property_id=? AND t.status!='done' AND pr.status='active'
                                  ORDER BY t.due_date IS NULL, t.due_date, t.id""", (pid,)))
        people = rows(c.execute("""SELECT DISTINCT u.id, u.name, r.name AS role FROM project_team pt
                                   JOIN projects pr ON pr.id=pt.project_id JOIN users u ON u.id=pt.user_id
                                   JOIN project_roles r ON r.id=pt.role_id
                                   WHERE pr.property_id=? AND pr.status='active' ORDER BY r.position, r.name""", (pid,)))
        dates = [_date_row(d, today) for d in rows(c.execute(
            """SELECT cd.*, u.name AS assignee_name FROM critical_dates cd LEFT JOIN users u ON u.id=cd.assignee_id
               WHERE cd.property_id=? ORDER BY cd.status='handled', cd.date""", (pid,)))]
    for pr in projects:
        pr["done"], pr["overdue"] = pr["done"] or 0, pr["overdue"] or 0
    team_by_person = {}
    for x in people:
        team_by_person.setdefault(x["id"], {"id": x["id"], "name": x["name"], "roles": []})
        if x["role"] not in team_by_person[x["id"]]["roles"]:
            team_by_person[x["id"]]["roles"].append(x["role"])
    photo = next((pr for pr in projects if pr["photo_path"]), projects[0] if projects else None)
    return {**p, "projects": projects, "tasks": tasks, "team": list(team_by_person.values()), "dates": dates,
            "photo_project_id": photo["id"] if photo else None, "photo_path": photo["photo_path"] if photo else None,
            "kinds": KINDS}


# ---------- critical dates ----------

def clean_remind(s: str | None) -> str:
    vals = sorted({int(x) for x in re.findall(r"\d+", s or "") if 0 <= int(x) <= 730}, reverse=True)
    return ",".join(map(str, vals)) or DEFAULT_REMIND


def add_date(property_id: int, d: dict, created_by=None, source="manual") -> int:
    kind = d.get("kind") if d.get("kind") in KINDS else "other"
    when = date.fromisoformat(str(d["date"])[:10]).isoformat()
    title = (d.get("title") or "").strip() or KINDS[kind] + (f" – {d['tenant']}" if d.get("tenant") else "")
    with tx() as c:
        return c.execute(
            """INSERT INTO critical_dates(property_id, kind, title, date, tenant, notes, assignee_id, remind_days,
                                          source, quote, created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (property_id, kind, title, when, (d.get("tenant") or "").strip() or None, (d.get("notes") or "").strip() or None,
             d.get("assignee_id") or None, clean_remind(d.get("remind_days")), source,
             (d.get("quote") or "").strip() or None, created_by)).lastrowid


def update_date(date_id: int, fields: dict):
    sets = {}
    for k in ("title", "tenant", "notes", "kind", "date", "assignee_id", "remind_days", "status"):
        if k in fields and fields[k] is not None:
            v = fields[k]
            if k == "kind" and v not in KINDS:
                continue
            if k == "date":
                v = date.fromisoformat(str(v)[:10]).isoformat()
            if k == "remind_days":
                v = clean_remind(v)
            if k == "status" and v not in ("open", "handled"):
                continue
            if k == "assignee_id":
                v = v or None
            sets[k] = v
    if not sets:
        return
    with tx() as c:
        old = c.execute("SELECT * FROM critical_dates WHERE id=?", (date_id,)).fetchone()
        if "status" in sets:
            sets["handled_at"] = None if sets["status"] == "open" else date.today().isoformat()
        c.execute(f"UPDATE critical_dates SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), date_id))
        if old and ("date" in sets and sets["date"] != old["date"] or "remind_days" in sets):
            c.execute("DELETE FROM critical_date_alerts WHERE date_id=?", (date_id,))   # new schedule, fresh reminders
        if old and old["task_id"]:  # keep the linked task in step
            if "date" in sets:
                c.execute("UPDATE tasks SET due_date=? WHERE id=?", (sets["date"], old["task_id"]))
            if "assignee_id" in sets:
                c.execute("UPDATE tasks SET assignee_id=? WHERE id=?", (sets["assignee_id"], old["task_id"]))
            if sets.get("status") == "handled":
                c.execute("UPDATE tasks SET status='done', completed_at=COALESCE(completed_at, datetime('now')) WHERE id=?",
                          (old["task_id"],))


def delete_date(date_id: int):
    with tx() as c:
        c.execute("DELETE FROM critical_date_alerts WHERE date_id=?", (date_id,))
        c.execute("DELETE FROM critical_dates WHERE id=?", (date_id,))


def _ensure_task(c, d: dict) -> int:
    if d["task_id"] and c.execute("SELECT 1 FROM tasks WHERE id=?", (d["task_id"],)).fetchone():
        return d["task_id"]
    prop = c.execute("SELECT name FROM properties WHERE id=?", (d["property_id"],)).fetchone()
    desc = "\n".join(filter(None, [f"Critical date at {prop['name']}: {KINDS.get(d['kind'], '')}",
                                   f"Tenant/suite: {d['tenant']}" if d["tenant"] else "", d["notes"] or "",
                                   f"From the lease: “{d['quote']}”" if d["quote"] else ""]))
    tid = c.execute("""INSERT INTO tasks(project_id, title, description, assignee_id, due_date, position)
                       VALUES(?,?,?,?,?,999)""",
                    (team.general_project_id(), f"{ICONS.get(d['kind'], '📌')} {d['title']} – {prop['name']}", desc,
                     d["assignee_id"], d["date"])).lastrowid
    c.execute("UPDATE critical_dates SET task_id=? WHERE id=?", (tid, d["id"]))
    return tid


def run_alerts(today: date | None = None) -> int:
    """Daily: Blink reminders at each offset + create the linked task at the first one. Idempotent."""
    today = today or date.today()
    sent = 0
    with tx() as c:
        # a finished linked task closes its date
        c.execute("""UPDATE critical_dates SET status='handled', handled_at=? WHERE status='open' AND task_id IN
                     (SELECT id FROM tasks WHERE status='done')""", (today.isoformat(),))
        due = rows(c.execute("""SELECT cd.*, p.name AS property_name, u.blink_user_id AS assignee_blink
                                FROM critical_dates cd JOIN properties p ON p.id=cd.property_id
                                LEFT JOIN users u ON u.id=cd.assignee_id WHERE cd.status='open'"""))
    for d in due:
        left = (date.fromisoformat(d["date"]) - today).days
        offsets = [int(x) for x in d["remind_days"].split(",") if x.strip().isdigit()] or [90, 30, 7]
        crossed = [o for o in offsets if left <= o]
        if not crossed or left < 0:
            continue
        with tx() as c:
            done = {r[0] for r in c.execute("SELECT offset_days FROM critical_date_alerts WHERE date_id=?", (d["id"],))}
            new = [o for o in crossed if o not in done]
            if not new:
                continue
            tid = _ensure_task(c, d)
            c.executemany("INSERT OR IGNORE INTO critical_date_alerts(date_id, offset_days) VALUES(?,?)",
                          [(d["id"], o) for o in new])
        when = "today" if left == 0 else "tomorrow" if left == 1 else f"in {left} days"
        text = (f"{ICONS.get(d['kind'], '📌')} **Critical date {when}:** {d['title']}\n"
                f"{d['property_name']} · {date.fromisoformat(d['date']):%a %b %d, %Y}"
                + (f" · {d['tenant']}" if d["tenant"] else "") + (f"\n_{d['notes']}_" if d["notes"] else "")
                + f"\nTask #{tid} is on your list — mark it done when it's handled.")
        recipients = [d["assignee_blink"]] if d["assignee_blink"] else automations._managers()
        automations._send(recipients, text)
        sent += 1
    return sent


def upcoming(days: int = 120, user_id: int | None = None) -> list[dict]:
    today = date.today()
    q = """SELECT cd.*, p.name AS property_name, u.name AS assignee_name FROM critical_dates cd
           JOIN properties p ON p.id=cd.property_id LEFT JOIN users u ON u.id=cd.assignee_id
           WHERE cd.status='open' AND cd.date <= ?"""
    args = [(today + timedelta(days=days)).isoformat()]
    if user_id:
        q += " AND cd.assignee_id=?"
        args.append(user_id)
    with tx() as c:
        return [_date_row(d, today) for d in rows(c.execute(q + " ORDER BY cd.date", args))]


# ---------- reading a lease ----------

EXTRACT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary", "dates"],
    "properties": {
        "summary": {"type": "string", "description": "2-3 lines: tenant, premises, term, anything unusual"},
        "dates": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "title", "date", "tenant", "notes", "quote", "confidence"],
            "properties": {
                "kind": {"type": "string", "enum": list(KINDS)},
                "title": {"type": "string", "description": "short, e.g. 'Renewal option notice due'"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "tenant": {"type": "string", "description": "tenant name and suite, if any"},
                "notes": {"type": "string", "description": "what has to happen, how it was calculated"},
                "quote": {"type": "string", "description": "the exact lease wording this date comes from (short)"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            }}},
    },
}

EXTRACT_SYSTEM = """You read commercial real estate documents (leases, amendments, insurance certificates, tax notices) for a property management company and list the critical dates they must not miss.

Include, when present: lease commencement/expiration, renewal or extension option notice deadlines (compute them: e.g. "no later than 180 days before expiration"), termination/kick-out notice windows, scheduled rent increases, CAM/operating expense reconciliation deadlines, tenant insurance certificate (COI) expirations, security deposit/LOC expirations, co-tenancy or radius dates, property tax protest deadlines, and loan/lender reporting dates.

Rules:
- Use exact calendar dates (YYYY-MM-DD). When the document gives a rule instead of a date, calculate it and say how in notes.
- If a date depends on something not in the document, skip it — never guess a date.
- Rent increases: one entry per increase date, with the new rent in notes.
- quote: the short lease wording the date comes from.
- confidence: high when the date is stated, medium when calculated, low when the document is ambiguous."""


def extract_from_document(data: bytes, content_type: str, filename: str) -> dict:
    from . import intake
    if not intake.ai_enabled():
        raise RuntimeError("Reading documents needs an ANTHROPIC_API_KEY.")
    import anthropic
    client = anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model=intake.MODEL, max_tokens=8000, system=EXTRACT_SYSTEM,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
            messages=[{"role": "user", "content": [
                intake.document_block(data, content_type, filename),
                {"type": "text", "text": f"Today is {date.today():%Y-%m-%d}. List this document's critical dates."}]}],
        )
    except anthropic.APIError as e:
        log.warning("lease extraction failed: %s", e)
        raise RuntimeError("Couldn't read that document right now — try again in a minute.")
    if resp.stop_reason != "end_turn":
        raise RuntimeError("That document was too long to read in one go — try uploading just the lease (not exhibits).")
    try:
        out = json.loads(next(b.text for b in resp.content if b.type == "text"))
    except (StopIteration, json.JSONDecodeError):
        raise RuntimeError("Couldn't read that document.")
    good = []
    for d in out.get("dates", []):
        try:
            d["date"] = date.fromisoformat(d["date"][:10]).isoformat()
        except (ValueError, TypeError, KeyError):
            continue
        d["kind"] = d["kind"] if d.get("kind") in KINDS else "other"
        good.append(d)
    good.sort(key=lambda d: d["date"])
    return {"summary": out.get("summary", ""), "dates": good}
