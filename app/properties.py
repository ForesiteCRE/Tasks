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

# Property-level dates (offered in the app). Tenant/lease kinds are kept for later and for older data.
KINDS = {
    "tax_protest": "Property tax protest deadline",
    "tax_payment": "Property tax payment due",
    "insurance_renewal": "Property insurance renewal",
    "owner_report": "Owner report due",
    "budget_due": "Annual budget due to owner",
    "cam_recon": "CAM reconciliation due",
    "loan": "Lender reporting / loan deadline",
    "mgmt_agreement": "Management agreement renewal",
    "inspection": "Inspection / permit / license",
    "other": "Other",
    # later (tenant level)
    "lease_expiration": "Lease expiration", "option_notice": "Renewal / option notice deadline",
    "rent_increase": "Rent increase", "coi_expiry": "Tenant insurance (COI) expires",
}
OFFERED = ["tax_protest", "tax_payment", "insurance_renewal", "owner_report", "budget_due", "cam_recon", "loan",
           "mgmt_agreement", "inspection", "other"]
ICONS = {"tax_protest": "🏛", "tax_payment": "💵", "insurance_renewal": "☂️", "owner_report": "📊", "budget_due": "📒",
         "cam_recon": "🧮", "loan": "🏦", "mgmt_agreement": "🤝", "inspection": "🔎", "other": "📌",
         "lease_expiration": "📄", "option_notice": "⚑", "rent_increase": "📈", "coi_expiry": "🛡"}
REPEATS = {"monthly": 1, "quarterly": 3, "yearly": 12}
DOC_KINDS = {"insurance": "Property insurance", "mgmt_agreement": "Management agreement", "tax_notice": "Tax notice / appraisal",
             "loan": "Loan document", "other": "Other"}
CONTACT_ROLES = ["Owner contact", "Asset manager", "CPA", "Insurance agent", "Lender", "Tax protest company", "Lawyer", "Other"]
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
        for table, col, typ in [("properties", "spend_limit", "REAL"), ("properties", "report_notes", "TEXT"),
                                ("critical_dates", "repeat", "TEXT")]:
            if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        c.execute("""CREATE TABLE IF NOT EXISTS property_contacts(
            id INTEGER PRIMARY KEY, property_id INTEGER NOT NULL, role TEXT NOT NULL, name TEXT, company TEXT,
            email TEXT, phone TEXT, notes TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        c.execute("""CREATE TABLE IF NOT EXISTS property_docs(
            id INTEGER PRIMARY KEY, property_id INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'other', filename TEXT NOT NULL,
            path TEXT NOT NULL, content_type TEXT, size INTEGER, summary TEXT, expires TEXT,
            insured_ok INTEGER, insured_note TEXT, verified_by INTEGER, verified_at TEXT,
            uploaded_by INTEGER, uploaded_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        c.execute("UPDATE property_contacts SET role='Lawyer' WHERE role='Attorney'")          # renamed roles
        c.execute("UPDATE property_contacts SET role='Tax protest company' WHERE role='Tax consultant'")
        new_team_table = not c.execute("SELECT 1 FROM sqlite_master WHERE name='property_team'").fetchone()
        c.execute("""CREATE TABLE IF NOT EXISTS property_team(
            property_id INTEGER NOT NULL, role_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            PRIMARY KEY(property_id, role_id))""")
    link_all(quiet=True)
    if new_team_table:
        migrate_teams()


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


def link_all(quiet: bool = False):
    """Attach every project that has an address but no property yet (cheap; safe to call often).
    A newly attached project takes on the property's team."""
    with tx() as c:
        linked = []
        for p in c.execute("""SELECT id, property, client FROM projects WHERE property_id IS NULL AND is_general=0
                              AND COALESCE(TRIM(property),'')!=''""").fetchall():
            c.execute("UPDATE projects SET property_id=? WHERE id=?", (find_or_create(p["property"], p["client"], c), p["id"]))
            linked.append(p["id"])
    for pid in linked:
        if quiet:
            with blink.quiet():
                sync_project(pid)
        else:
            sync_project(pid)


def relink(project_id: int):
    with tx() as c:
        c.execute("UPDATE projects SET property_id=NULL WHERE id=?", (project_id,))
    link_all()


# ---------- the property's team (roles live here; projects at the address follow it) ----------

def team_list(property_id: int) -> list[dict]:
    with tx() as c:
        return rows(c.execute(
            """SELECT r.id AS role_id, r.name AS role, t.user_id, u.name AS user_name FROM project_roles r
               LEFT JOIN property_team t ON t.role_id=r.id AND t.property_id=?
               LEFT JOIN users u ON u.id=t.user_id ORDER BY r.position, r.id""", (property_id,)))


def team_map(property_id: int | None) -> dict:
    if not property_id:
        return {}
    with tx() as c:
        return {r["role_id"]: r["user_id"] for r in c.execute(
            "SELECT role_id, user_id FROM property_team WHERE property_id=?", (property_id,))}


def property_for_address(address: str | None) -> int | None:
    n = norm(address or "")
    if not n:
        return None
    with tx() as c:
        r = c.execute("SELECT id FROM properties WHERE norm=?", (n,)).fetchone()
    return r["id"] if r else None


def set_member(property_id: int, role_id: int, user_id) -> int:
    """Change who holds a role at a property. Every active project there follows (open role tasks move, the new
    person gets a Blink message per project). Returns how many tasks moved."""
    from . import team
    with tx() as c:
        if user_id:
            c.execute("INSERT OR REPLACE INTO property_team(property_id, role_id, user_id) VALUES(?,?,?)",
                      (property_id, role_id, user_id))
        else:
            c.execute("DELETE FROM property_team WHERE property_id=? AND role_id=?", (property_id, role_id))
        projects = [r[0] for r in c.execute(
            "SELECT id FROM projects WHERE property_id=? AND status='active'", (property_id,))]
    return sum(team.set_member(pid, role_id, user_id, _from_property=True) for pid in projects)


def sync_project(project_id: int):
    """Make one project's team match its property: roles the property doesn't have yet are adopted from the
    project; roles the property has are applied to the project (moving open role tasks)."""
    from . import team
    with tx() as c:
        p = c.execute("SELECT property_id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not p or not p["property_id"]:
            return
        prop = p["property_id"]
        mine = {r["role_id"]: r["user_id"] for r in c.execute(
            "SELECT role_id, user_id FROM project_team WHERE project_id=?", (project_id,))}
        theirs = team_map_c(c, prop)
        for role, user in mine.items():
            if user and role not in theirs:
                c.execute("INSERT INTO property_team(property_id, role_id, user_id) VALUES(?,?,?)", (prop, role, user))
                theirs[role] = user
    for role, user in theirs.items():
        if mine.get(role) != user:
            team.set_member(project_id, role, user, _from_property=True)


def team_map_c(c, property_id: int) -> dict:
    return {r["role_id"]: r["user_id"] for r in c.execute(
        "SELECT role_id, user_id FROM property_team WHERE property_id=?", (property_id,))}


def after_project_created(project_id: int, chosen: dict | None):
    """A project was just created with a team picked in the dialog: those picks become the property's team
    (so the whole address follows), then the project is synced."""
    link_all()
    with tx() as c:
        p = c.execute("SELECT property_id FROM projects WHERE id=?", (project_id,)).fetchone()
    if not p or not p["property_id"]:
        return
    current = team_map(p["property_id"])
    for role, user in (chosen or {}).items():
        role, user = int(role), (int(user) if user else None)
        if user and current.get(role) != user:
            set_member(p["property_id"], role, user)
    sync_project(project_id)


def migrate_teams():
    """One-time: build each property's team from its projects (newest first wins), then line projects up."""
    with tx() as c:
        for prop in [r[0] for r in c.execute("SELECT id FROM properties")]:
            for pr in c.execute("""SELECT pt.role_id, pt.user_id FROM project_team pt JOIN projects p ON p.id=pt.project_id
                                   WHERE p.property_id=? AND pt.user_id IS NOT NULL
                                   ORDER BY p.status='done', p.start_date DESC, p.id DESC""", (prop,)).fetchall():
                c.execute("INSERT OR IGNORE INTO property_team(property_id, role_id, user_id) VALUES(?,?,?)",
                          (prop, pr["role_id"], pr["user_id"]))
        projects = [r[0] for r in c.execute("SELECT id FROM projects WHERE property_id IS NOT NULL AND status='active'")]
    with blink.quiet():
        for pid in projects:
            sync_project(pid)


def create(address: str, name: str | None = None, client: str | None = None, notes: str | None = None) -> int:
    pid = find_or_create(address, client)
    if pid is None:
        raise ValueError("Enter the property's address.")
    update(pid, {k: v for k, v in {"name": name, "client": client, "notes": notes}.items() if v})
    return pid


def update(pid: int, fields: dict):
    allowed = {k: v for k, v in fields.items() if k in ("name", "client", "notes", "address", "report_notes") and v is not None}
    if "spend_limit" in fields:
        allowed["spend_limit"] = float(fields["spend_limit"]) if fields["spend_limit"] not in (None, "", 0) else None
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
        c.execute("DELETE FROM property_contacts WHERE property_id=?", (pid,))
        for d in c.execute("SELECT path FROM property_docs WHERE property_id=?", (pid,)).fetchall():
            _remove_file(d["path"])
        c.execute("DELETE FROM property_docs WHERE property_id=?", (pid,))
        c.execute("DELETE FROM properties WHERE id=?", (pid,))


def _date_row(d: dict, today: date) -> dict:
    d["kind_label"] = KINDS.get(d["kind"], d["kind"])
    d["icon"] = ICONS.get(d["kind"], "📌")
    d["repeat_label"] = {"monthly": "Every month", "quarterly": "Every quarter", "yearly": "Every year"}.get(d.get("repeat") or "", "")
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
            p["insurance"] = _insurance_status(c, p["id"])
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
        tasks = rows(c.execute("""SELECT t.id, t.title, t.status, t.due_date, t.assignee_id, t.project_id, t.role_id,
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
            "roles": team_list(pid), "contacts": contacts(pid), "docs": docs(pid),
            "insurance": _insurance_status_id(pid), "offered": OFFERED, "contact_roles": CONTACT_ROLES,
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
    rep = d.get("repeat") if d.get("repeat") in REPEATS else None
    with tx() as c:
        same = c.execute("SELECT id FROM critical_dates WHERE property_id=? AND kind=? AND date=? AND status='open'",
                         (property_id, kind, when)).fetchone()
        if same and source != "manual":   # reading a second document shouldn't double up dates already tracked
            return same["id"]
        return c.execute(
            """INSERT INTO critical_dates(property_id, kind, title, date, tenant, notes, assignee_id, remind_days,
                                          source, quote, created_by, repeat) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (property_id, kind, title, when, (d.get("tenant") or "").strip() or None, (d.get("notes") or "").strip() or None,
             d.get("assignee_id") or None, clean_remind(d.get("remind_days")), source,
             (d.get("quote") or "").strip() or None, created_by, rep)).lastrowid


def update_date(date_id: int, fields: dict):
    sets = {}
    for k in ("title", "tenant", "notes", "kind", "date", "assignee_id", "remind_days", "status", "repeat"):
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
            if k == "repeat":
                v = v if v in REPEATS else None
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
        if old and sets.get("status") == "handled" and old["status"] == "open":
            _roll(c, dict(old))


def _roll(c, d: dict):
    """A repeating date was handled → schedule the next one (same details, fresh reminders and task)."""
    if not d.get("repeat") or d["repeat"] not in REPEATS:
        return
    from .recur import add_months
    nxt = add_months(date.fromisoformat(d["date"]), REPEATS[d["repeat"]])
    while nxt < date.today():
        nxt = add_months(nxt, REPEATS[d["repeat"]])
    if c.execute("SELECT 1 FROM critical_dates WHERE property_id=? AND kind=? AND title=? AND date=?",
                 (d["property_id"], d["kind"], d["title"], nxt.isoformat())).fetchone():
        return
    c.execute("""INSERT INTO critical_dates(property_id, kind, title, date, tenant, notes, assignee_id, remind_days,
                                            source, quote, created_by, repeat) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
              (d["property_id"], d["kind"], d["title"], nxt.isoformat(), d.get("tenant"), d.get("notes"), d.get("assignee_id"),
               d["remind_days"], d.get("source"), d.get("quote"), d.get("created_by"), d["repeat"]))


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
        finished = rows(c.execute("""SELECT * FROM critical_dates WHERE status='open' AND task_id IN
                                     (SELECT id FROM tasks WHERE status='done')"""))
        for d in finished:
            c.execute("UPDATE critical_dates SET status='handled', handled_at=? WHERE id=?", (today.isoformat(), d["id"]))
            _roll(c, d)
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


# ---------- reading a document (management agreement, insurance policy, tax notice …) ----------

EXTRACT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["doc_type", "summary", "dates", "spend_limit", "report_notes", "contacts", "insurance"],
    "properties": {
        "doc_type": {"type": "string", "enum": list(DOC_KINDS)},
        "summary": {"type": "string", "description": "2-4 lines: what this document is and what matters for managing the property"},
        "dates": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "title", "date", "repeat", "notes", "quote", "confidence"],
            "properties": {
                "kind": {"type": "string", "enum": OFFERED},
                "title": {"type": "string", "description": "short, e.g. 'Monthly owner report due'"},
                "date": {"type": "string", "description": "YYYY-MM-DD of the NEXT occurrence on or after today"},
                "repeat": {"type": "string", "enum": ["", "monthly", "quarterly", "yearly"]},
                "notes": {"type": "string", "description": "what has to happen / how the date was worked out"},
                "quote": {"type": "string", "description": "the short wording this comes from"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            }}},
        "spend_limit": {"type": "number", "description": "max the manager may spend on one item/repair without owner approval, in dollars; 0 if not stated"},
        "report_notes": {"type": "string", "description": "how/when the owner wants reports: contents, format, recipients, deadlines. Empty if none."},
        "contacts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["role", "name", "company", "email", "phone"],
            "properties": {"role": {"type": "string", "enum": CONTACT_ROLES}, "name": {"type": "string"},
                           "company": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"}}}},
        "insurance": {
            "type": "object", "additionalProperties": False,
            "required": ["carrier", "policy_number", "expires", "foresite_additional_insured", "evidence"],
            "properties": {
                "carrier": {"type": "string"}, "policy_number": {"type": "string"},
                "expires": {"type": "string", "description": "policy expiration YYYY-MM-DD, empty if not insurance"},
                "foresite_additional_insured": {"type": "string", "enum": ["yes", "no", "unclear", "not_applicable"]},
                "evidence": {"type": "string", "description": "the wording/section that shows it (or what's missing)"},
            }},
    },
}

EXTRACT_SYSTEM = """You help Foresite Real Estate (a commercial property manager; may appear as "Foresite Real Estate, Inc.", "Foresite Commercial Real Estate" or "Foresite") manage a property for its owner. You read property-level documents — management agreements, property insurance policies/certificates/endorsements, tax appraisal notices, loan documents — and pull out what the team must track. Tenant leases are out of scope for now.

Find:
- Property-level critical dates: tax protest deadline, tax payment due, insurance renewal/expiration, owner report due dates (monthly/quarterly — give the next one and set repeat), annual budget due, CAM reconciliation due, lender reporting, management agreement term/renewal/termination notice, required inspections. Use exact dates; when the document gives a rule ("by the 15th of each month", "60 days after year end"), compute the next occurrence on or after today and say how in notes. Skip anything that would be a guess.
- spend_limit: the most the manager may spend without the owner's prior approval (per item/repair/contract — say which in report_notes if it matters). 0 if not stated.
- report_notes: what the owner wants in reports and how (format, contents, who receives them, when).
- contacts: owner contact, the owner's asset manager, CPA/accountant, insurance agent/broker, lender, tax protest company/property tax consultant, lawyer/attorney — only people/companies actually named.
- insurance: for an insurance document, carrier, policy number, expiration, and whether Foresite is named as an ADDITIONAL INSURED (look at additional insured sections, endorsements, certificate holder boxes — being only the certificate holder is NOT additional insured). Otherwise use not_applicable and empty strings.
- confidence: high when stated, medium when calculated, low when ambiguous."""


def read_document(pid: int, data: bytes, content_type: str, filename: str, user_id=None) -> dict:
    """Store the file on the property, have Claude read it, record the insurance check, and return everything
    else for review (nothing besides the file + insurance check is applied until the user confirms)."""
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
                {"type": "text", "text": f"Today is {date.today():%Y-%m-%d}. Read this document for the property."}]}],
        )
    except anthropic.APIError as e:
        log.warning("document read failed: %s", e)
        raise RuntimeError("Couldn't read that document right now — try again in a minute.")
    if resp.stop_reason != "end_turn":
        raise RuntimeError("That document was too long to read in one go — try uploading just the main document.")
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
        d["kind"] = d["kind"] if d.get("kind") in OFFERED else "other"
        d["repeat"] = d.get("repeat") if d.get("repeat") in REPEATS else ""
        good.append(d)
    good.sort(key=lambda d: d["date"])
    out["dates"] = good
    # keep the file, labelled, with the insurance check
    doc_id = save_doc(pid, data, filename, content_type, user_id)
    ins = out.get("insurance") or {}
    exp = None
    try:
        exp = date.fromisoformat((ins.get("expires") or "")[:10]).isoformat()
    except ValueError:
        pass
    ok = {"yes": 1, "no": 0}.get(ins.get("foresite_additional_insured"))
    kind = out.get("doc_type") if out.get("doc_type") in DOC_KINDS else "other"
    note = " · ".join(filter(None, [ins.get("carrier"), ("Policy " + ins["policy_number"]) if ins.get("policy_number") else "",
                                    ins.get("evidence")]))
    with tx() as c:
        c.execute("UPDATE property_docs SET kind=?, summary=?, expires=?, insured_ok=?, insured_note=? WHERE id=?",
                  (kind, out.get("summary"), exp, ok if kind == "insurance" else None, note or None, doc_id))
    # an insurance policy implies a renewal date — offer it if Claude didn't list one
    if kind == "insurance" and exp and not any(d["kind"] == "insurance_renewal" for d in good):
        good.append({"kind": "insurance_renewal", "title": "Property insurance renewal", "date": exp, "repeat": "yearly",
                     "notes": f"Policy expires {exp}. Get renewal quotes and confirm Foresite stays additional insured.",
                     "quote": "", "confidence": "high"})
    out["doc_id"] = doc_id
    out["doc_kind"] = kind
    out["spend_limit"] = out.get("spend_limit") or 0
    return out


# ---------- contacts ----------

def contacts(pid: int) -> list[dict]:
    with tx() as c:
        rs = rows(c.execute("SELECT * FROM property_contacts WHERE property_id=? ORDER BY id", (pid,)))
    order = {r: i for i, r in enumerate(CONTACT_ROLES)}
    return sorted(rs, key=lambda x: (order.get(x["role"], 99), x["id"]))


def add_contact(pid: int, d: dict) -> int:
    if not any((d.get(k) or "").strip() for k in ("name", "company", "email", "phone")):
        raise ValueError("Add at least a name, company, email or phone.")
    with tx() as c:
        return c.execute("""INSERT INTO property_contacts(property_id, role, name, company, email, phone, notes)
                            VALUES(?,?,?,?,?,?,?)""",
                         (pid, (d.get("role") or "Other").strip(), *[(d.get(k) or "").strip() or None
                                                                     for k in ("name", "company", "email", "phone", "notes")])).lastrowid


def update_contact(cid: int, d: dict):
    sets = {k: ((d[k] or "").strip() or None) for k in ("role", "name", "company", "email", "phone", "notes") if k in d}
    if sets:
        with tx() as c:
            c.execute(f"UPDATE property_contacts SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), cid))


def delete_contact(cid: int):
    with tx() as c:
        c.execute("DELETE FROM property_contacts WHERE id=?", (cid,))


# ---------- documents (insurance policy, management agreement, tax notices …) ----------

def _doc_dir():
    import os
    from .photos import UPLOAD_DIR
    d = os.path.join(UPLOAD_DIR, "property-docs")
    os.makedirs(d, exist_ok=True)
    return d


def _remove_file(name: str):
    import os
    try:
        os.remove(os.path.join(_doc_dir(), os.path.basename(name)))
    except OSError:
        pass


def save_doc(pid: int, data: bytes, filename: str, content_type: str, uploaded_by=None) -> int:
    import os
    import secrets
    ext = os.path.splitext(filename or "")[1][:8].lower() or ".bin"
    name = f"prop-{pid}-{secrets.token_hex(6)}{ext}"
    with open(os.path.join(_doc_dir(), name), "wb") as f:
        f.write(data)
    with tx() as c:
        return c.execute("""INSERT INTO property_docs(property_id, filename, path, content_type, size, uploaded_by)
                            VALUES(?,?,?,?,?,?)""", (pid, filename or "document", name, content_type, len(data), uploaded_by)).lastrowid


def doc_file(doc_id: int):
    import os
    with tx() as c:
        d = c.execute("SELECT * FROM property_docs WHERE id=?", (doc_id,)).fetchone()
    if not d:
        return None
    path = os.path.join(_doc_dir(), os.path.basename(d["path"]))
    return (dict(d), path) if os.path.exists(path) else None


def docs(pid: int) -> list[dict]:
    with tx() as c:
        ds = rows(c.execute("""SELECT d.*, u.name AS uploaded_by_name, v.name AS verified_by_name FROM property_docs d
                               LEFT JOIN users u ON u.id=d.uploaded_by LEFT JOIN users v ON v.id=d.verified_by
                               WHERE d.property_id=? ORDER BY d.id DESC""", (pid,)))
    for d in ds:
        d["kind_label"] = DOC_KINDS.get(d["kind"], "Other")
    return ds


def update_doc(doc_id: int, fields: dict, user_id=None):
    sets = {}
    if fields.get("kind") in DOC_KINDS:
        sets["kind"] = fields["kind"]
    if "insured_ok" in fields:
        sets["insured_ok"] = None if fields["insured_ok"] is None else int(bool(fields["insured_ok"]))
        sets["verified_by"], sets["verified_at"] = user_id, date.today().isoformat()
    if "expires" in fields:
        sets["expires"] = fields["expires"] or None
    if sets:
        with tx() as c:
            c.execute(f"UPDATE property_docs SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), doc_id))


def delete_doc(doc_id: int):
    with tx() as c:
        d = c.execute("SELECT path FROM property_docs WHERE id=?", (doc_id,)).fetchone()
        c.execute("DELETE FROM property_docs WHERE id=?", (doc_id,))
    if d:
        _remove_file(d["path"])


def _insurance_status(c, pid: int) -> dict:
    """Latest insurance document: is Foresite named additional insured, and when does it expire."""
    d = c.execute("""SELECT * FROM property_docs WHERE property_id=? AND kind='insurance'
                     ORDER BY COALESCE(expires,'') DESC, id DESC LIMIT 1""", (pid,)).fetchone()
    if not d:
        return {"state": "missing", "label": "No insurance on file"}
    d = dict(d)
    expired = bool(d["expires"] and d["expires"] < date.today().isoformat())
    state = "expired" if expired else {1: "ok", 0: "not_named"}.get(d["insured_ok"], "unverified")
    label = {"ok": "Foresite named additional insured", "not_named": "Foresite NOT named additional insured",
             "unverified": "Additional insured not verified", "expired": "Insurance on file has expired"}[state]
    return {"state": state, "label": label, "expires": d["expires"], "doc_id": d["id"], "note": d["insured_note"]}


def _insurance_status_id(pid: int) -> dict:
    with tx() as c:
        return _insurance_status(c, pid)
