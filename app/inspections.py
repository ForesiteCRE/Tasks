"""Property inspections, walked on a phone.

Start an inspection from a property page (or Properties ▾ → Inspections). It's a checklist grouped by area — mark
each item ✓ Good, ⚠ Issue or N/A, add a note and photos (taken with the phone camera or uploaded), and add your own
items. Everything saves as you go, so you can stop and pick it up later. "Finish" turns every Issue marked "make a
repair task" into a task for the property's Property Manager (urgent ones due in 2 days, others in 2 weeks) and
produces a printable inspection report (with photos) for the owner packet.
"""
import base64
import os
import re
import uuid
from datetime import date, timedelta

from .db import rows, tx

CHECKLIST = [
    ("Site & parking", ["Parking lot surface (potholes, cracks)", "Striping, ADA spaces & signs", "Parking lot & building lighting",
                        "Landscaping & irrigation", "Drainage / standing water", "Monument & pylon signs", "Dumpster enclosure & trash",
                        "Sidewalks & curbs (trip hazards)", "Graffiti / vandalism", "Fencing & gates"]),
    ("Building exterior", ["Roof (visible damage, debris, ponding)", "Gutters & downspouts", "Facade, paint & stucco",
                           "Storefronts, doors & glass", "Tenant signage", "Canopies & awnings"]),
    ("Common areas", ["Lobby & corridors", "Restrooms", "Elevator (certificate posted)", "Ceiling tiles / water stains", "Janitorial & cleanliness"]),
    ("Life safety & systems", ["Fire extinguishers (tags current)", "Fire riser / sprinkler room", "Exit signs & emergency lights",
                               "Backflow & fire department connection", "Fire lane markings", "Electrical & meter rooms (locked, clear)",
                               "Rooftop HVAC (visible condition)"]),
    ("Vacant suites", ["Vacant suites secure & clean", "Utilities on in vacant suites", "Leasing signage"]),
    ("Tenants", ["Tenant compliance (signs, outdoor storage, lease issues)", "Tenant concerns raised"]),
]
GENERAL = "General photos"
# Template steps the walk-through covers → the checklist areas that must be looked at (None = always, once finished)
STEPS = {"exterior walk": ["Site & parking", "Building exterior"], "common areas & life safety": ["Common areas", "Life safety & systems"],
         "check vacant suites": ["Vacant suites"], "photos & inspection report": None, "work orders for issues found": None,
         "site inspection & property condition walk": None}
RATINGS = ("good", "issue", "na")


def init():
    with tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS inspections(id INTEGER PRIMARY KEY, property_id INTEGER NOT NULL, user_id INTEGER,
                     status TEXT NOT NULL DEFAULT 'draft', notes TEXT, started_at TEXT NOT NULL DEFAULT (datetime('now')), finished_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS inspection_items(id INTEGER PRIMARY KEY, inspection_id INTEGER NOT NULL, section TEXT NOT NULL,
                     label TEXT NOT NULL, rating TEXT, note TEXT, urgent INTEGER NOT NULL DEFAULT 0, make_task INTEGER NOT NULL DEFAULT 1,
                     task_id INTEGER, position INTEGER NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS inspection_photos(id INTEGER PRIMARY KEY, inspection_id INTEGER NOT NULL, item_id INTEGER,
                     file TEXT NOT NULL, caption TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')))""")


def _dir():
    from .photos import UPLOAD_DIR
    d = os.path.join(UPLOAD_DIR, "inspections")
    os.makedirs(d, exist_ok=True)
    return d


def can_inspect(uid, is_mgr: bool, property_id: int) -> bool:
    if is_mgr:
        return True
    with tx() as c:
        return bool(c.execute("SELECT 1 FROM property_team WHERE property_id=? AND user_id=?", (property_id, uid)).fetchone())


def start(property_id: int, uid) -> int:
    """Your unfinished inspection at this property, or a new one."""
    with tx() as c:
        r = c.execute("SELECT id FROM inspections WHERE property_id=? AND user_id IS ? AND status='draft' ORDER BY id DESC",
                      (property_id, uid)).fetchone()
        if r:
            return r[0]
        iid = c.execute("INSERT INTO inspections(property_id, user_id) VALUES(?,?)", (property_id, uid)).lastrowid
        pos = 0
        for sec, items in CHECKLIST:
            for label in items:
                c.execute("INSERT INTO inspection_items(inspection_id, section, label, position) VALUES(?,?,?,?)", (iid, sec, label, pos))
                pos += 1
    return iid


def get(iid: int) -> dict | None:
    with tx() as c:
        i = c.execute("""SELECT i.*, p.name AS property_name, p.address AS property_address, u.name AS inspector
                         FROM inspections i JOIN properties p ON p.id=i.property_id LEFT JOIN users u ON u.id=i.user_id
                         WHERE i.id=?""", (iid,)).fetchone()
        if not i:
            return None
        i = dict(i)
        i["items"] = rows(c.execute("""SELECT it.*, t.status AS task_status FROM inspection_items it LEFT JOIN tasks t ON t.id=it.task_id
                                       WHERE it.inspection_id=? ORDER BY it.position, it.id""", (iid,)))
        i["photos"] = rows(c.execute("SELECT id, item_id, caption, created_at FROM inspection_photos WHERE inspection_id=? ORDER BY id", (iid,)))
    i["counts"] = {k: sum(1 for x in i["items"] if x["rating"] == k) for k in RATINGS}
    i["counts"]["unchecked"] = sum(1 for x in i["items"] if not x["rating"])
    return i


def item_owner(item_id: int):
    with tx() as c:
        r = c.execute("SELECT inspection_id FROM inspection_items WHERE id=?", (item_id,)).fetchone()
    return r[0] if r else None


def photo_owner(photo_id: int):
    with tx() as c:
        r = c.execute("SELECT inspection_id FROM inspection_photos WHERE id=?", (photo_id,)).fetchone()
    return r[0] if r else None


def update_item(item_id: int, d: dict):
    sets = {}
    if "rating" in d:
        sets["rating"] = d["rating"] if d["rating"] in RATINGS else None
    if "note" in d:
        sets["note"] = (d["note"] or "").strip()[:2000] or None
    for k in ("urgent", "make_task"):
        if k in d:
            sets[k] = 1 if d[k] else 0
    if "label" in d and (d["label"] or "").strip():
        sets["label"] = d["label"].strip()[:200]
    if sets:
        with tx() as c:
            c.execute(f"UPDATE inspection_items SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), item_id))


def add_item(iid: int, section: str, label: str) -> int:
    label = (label or "").strip()[:200]
    if not label:
        raise ValueError("What should be checked?")
    with tx() as c:
        pos = c.execute("SELECT COALESCE(MAX(position),0)+1 FROM inspection_items WHERE inspection_id=? AND section=?", (iid, section)).fetchone()[0]
        return c.execute("INSERT INTO inspection_items(inspection_id, section, label, position) VALUES(?,?,?,?)",
                         (iid, (section or "Other").strip()[:80], label, pos)).lastrowid


def delete_item(item_id: int):
    with tx() as c:
        c.execute("UPDATE inspection_photos SET item_id=NULL WHERE item_id=?", (item_id,))
        c.execute("DELETE FROM inspection_items WHERE id=?", (item_id,))


def add_photo(iid: int, item_id, data: bytes, caption: str | None = None) -> int:
    if not data:
        raise ValueError("No photo")
    if len(data) > 15 * 1024 * 1024:
        raise ValueError("That photo is over 15 MB.")
    kind = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
    name = f"{iid}-{uuid.uuid4().hex[:12]}.{kind}"
    with open(os.path.join(_dir(), name), "wb") as fh:
        fh.write(data)
    with tx() as c:
        return c.execute("INSERT INTO inspection_photos(inspection_id, item_id, file, caption) VALUES(?,?,?,?)",
                         (iid, int(item_id) if item_id else None, name, (caption or "").strip()[:200] or None)).lastrowid


def photo_path(photo_id: int):
    with tx() as c:
        r = c.execute("SELECT file FROM inspection_photos WHERE id=?", (photo_id,)).fetchone()
    if not r:
        return None
    p = os.path.join(_dir(), os.path.basename(r[0]))
    return p if os.path.exists(p) else None


def delete_photo(photo_id: int):
    p = photo_path(photo_id)
    with tx() as c:
        c.execute("DELETE FROM inspection_photos WHERE id=?", (photo_id,))
    if p:
        try:
            os.remove(p)
        except OSError:
            pass


def finish(iid: int, uid, notes: str | None = None, assignee_id=None, today: date | None = None) -> dict:
    """Make repair tasks for the issues and close the inspection."""
    from . import team
    from .auth import PUBLIC_URL
    today = today or date.today()
    i = get(iid)
    if not i:
        raise ValueError("Inspection not found")
    with tx() as c:
        pm = c.execute("""SELECT pt.user_id FROM property_team pt JOIN project_roles r ON r.id=pt.role_id
                          WHERE pt.property_id=? AND r.name='Property Manager'""", (i["property_id"],)).fetchone()
    who = int(assignee_id) if assignee_id else (pm[0] if pm else uid)
    gp = team.general_project_id()
    made = []
    link = f"{PUBLIC_URL}/inspections/{iid}/report"
    with tx() as c:
        for it in i["items"]:
            if it["rating"] != "issue" or not it["make_task"] or it["task_id"]:
                continue
            nphotos = sum(1 for p in i["photos"] if p["item_id"] == it["id"])
            due = today + timedelta(days=2 if it["urgent"] else 14)
            desc = (f"Found on the {today.strftime('%b %-d')} inspection ({it['section']}).\n" + (f"{it['note']}\n" if it["note"] else "")
                    + (f"{nphotos} photo{'s' if nphotos > 1 else ''} in the report: " if nphotos else "Report: ") + link)
            tid = c.execute("""INSERT INTO tasks(project_id, title, description, assignee_id, due_date, position, property_id)
                               VALUES(?,?,?,?,?,999,?)""", (gp, ("🚨 " if it["urgent"] else "") + f"Fix: {it['label']}", desc, who,
                                                            due.isoformat(), i["property_id"])).lastrowid
            c.execute("UPDATE inspection_items SET task_id=? WHERE id=?", (tid, it["id"]))
            if uid and uid != who:   # the inspector hears when the repair is done
                c.execute("INSERT OR IGNORE INTO task_watchers(task_id, user_id, added_by) VALUES(?,?,?)", (tid, uid, uid))
            made.append(tid)
        c.execute("UPDATE projects SET status='active' WHERE id=?", (gp,))
        c.execute("UPDATE inspections SET status='done', finished_at=datetime('now'), notes=COALESCE(?, notes) WHERE id=?",
                  ((notes or "").strip() or None, iid))
    steps = complete_steps(iid, uid, len(made), today)
    if made and who != uid:
        from . import notify
        notify.deliver(who, "assigned", f"🔧 {len(made)} repair task{'s' if len(made) > 1 else ''} from the inspection at "
                                        f"{i['property_name']} — {link}")
    return {"tasks": made, "steps": steps, "report": f"/inspections/{iid}/report"}


def _step_tasks(c, property_id: int, today: date):
    """Open inspection steps at this property, in projects that have started (not next quarter's)."""
    return [t for t in rows(c.execute(
        """SELECT t.id, t.title, t.status FROM tasks t JOIN projects p ON p.id=t.project_id
           WHERE COALESCE(p.property_id, t.property_id)=? AND t.status!='done' AND p.archived_at IS NULL
             AND p.status NOT IN ('archived') AND (p.start_date IS NULL OR p.start_date<=?)""", (property_id, today.isoformat())))
        if t["title"].strip().lower() in STEPS]


def step_info(task_id: int) -> dict | None:
    """For a task dialog: is this an inspection step, and is there an inspection to continue / a report?"""
    with tx() as c:
        t = c.execute("""SELECT t.title, t.status, COALESCE(p.property_id, t.property_id) AS property_id, p.start_date
                         FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=?""", (task_id,)).fetchone()
        if not t or not t["property_id"] or t["title"].strip().lower() not in STEPS:
            return None
        draft = c.execute("SELECT id FROM inspections WHERE property_id=? AND status='draft' ORDER BY id DESC", (t["property_id"],)).fetchone()
        last = c.execute("""SELECT id, finished_at FROM inspections WHERE property_id=? AND status='done' AND finished_at>=?
                            ORDER BY id DESC""", (t["property_id"], t["start_date"] or "0000")).fetchone()
    return {"property_id": t["property_id"], "draft_id": draft[0] if draft else None,
            "report_id": last["id"] if last else None, "report_date": last["finished_at"][:10] if last else None}


def complete_steps(iid: int, uid, made: int, today: date | None = None) -> list[int]:
    """Finishing an inspection checks off the matching template steps (if their areas were looked at)."""
    from . import services
    from .auth import PUBLIC_URL
    today = today or date.today()
    i = get(iid)
    link = f"{PUBLIC_URL}/inspections/{iid}/report"
    with tx() as c:
        steps = _step_tasks(c, i["property_id"], today)
    done = []
    for t in steps:
        areas = STEPS[t["title"].strip().lower()]
        if areas and not any(x["rating"] for x in i["items"] if x["section"] in areas):
            continue
        key = t["title"].strip().lower()
        note = (f"✅ Done with the inspection walk-through — {made} repair task{'s' if made != 1 else ''} created. {link}"
                if key == "work orders for issues found" and made else
                "✅ No issues found on the inspection walk-through." if key == "work orders for issues found" else
                f"✅ Done with the inspection walk-through — report: {link}")
        services.apply_update(t["id"], uid, "web", "done", note)
        done.append(t["id"])
    return done


def for_property(pid: int) -> list[dict]:
    with tx() as c:
        out = rows(c.execute("""SELECT i.id, i.status, i.started_at, i.finished_at, i.user_id, u.name AS inspector,
                                (SELECT COUNT(*) FROM inspection_items x WHERE x.inspection_id=i.id AND x.rating='issue') AS issues,
                                (SELECT COUNT(*) FROM inspection_photos x WHERE x.inspection_id=i.id) AS photos
                                FROM inspections i LEFT JOIN users u ON u.id=i.user_id WHERE i.property_id=? ORDER BY i.id DESC""", (pid,)))
    return out


def recent(limit: int = 40) -> list[dict]:
    with tx() as c:
        return rows(c.execute("""SELECT i.id, i.status, i.started_at, i.finished_at, i.property_id, p.name AS property_name, u.name AS inspector,
                                 i.user_id,
                                 (SELECT COUNT(*) FROM inspection_items x WHERE x.inspection_id=i.id AND x.rating='issue') AS issues,
                                 (SELECT COUNT(*) FROM inspection_photos x WHERE x.inspection_id=i.id) AS photos
                                 FROM inspections i JOIN properties p ON p.id=i.property_id LEFT JOIN users u ON u.id=i.user_id
                                 ORDER BY i.id DESC LIMIT ?""", (limit,)))


def delete(iid: int):
    with tx() as c:
        files = [r[0] for r in c.execute("SELECT file FROM inspection_photos WHERE inspection_id=?", (iid,))]
        c.execute("DELETE FROM inspection_photos WHERE inspection_id=?", (iid,))
        c.execute("DELETE FROM inspection_items WHERE inspection_id=?", (iid,))
        c.execute("DELETE FROM inspections WHERE id=?", (iid,))
    for f in files:
        try:
            os.remove(os.path.join(_dir(), os.path.basename(f)))
        except OSError:
            pass


def _e(s) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def report_html(iid: int) -> str:
    i = get(iid)
    when = (i["finished_at"] or i["started_at"])[:10]
    d = date.fromisoformat(when)
    lab = {"good": ("✓ Good", "#3d6b1f", "#eef6e4"), "issue": ("⚠ Needs attention", "#b42318", "#fde8e8"), "na": ("N/A", "#667", "#f1f3f4")}
    pics = lambda item_id: "".join(f'<a href="/api/inspection-photos/{p["id"]}" target="_blank"><img src="/api/inspection-photos/{p["id"]}" alt=""></a>'
                                   for p in i["photos"] if p["item_id"] == item_id)
    secs = []
    for sec in dict.fromkeys(x["section"] for x in i["items"]):
        rows_ = []
        for it in [x for x in i["items"] if x["section"] == sec]:
            if not it["rating"] and not it["note"]:
                continue
            l, fg, bg = lab.get(it["rating"], ("Not checked", "#667", "#f6f7f8"))
            rows_.append(f'<tr><td>{_e(it["label"])}{"<div class=n>" + _e(it["note"]) + "</div>" if it["note"] else ""}'
                         f'{"<div class=ph>" + pics(it["id"]) + "</div>" if any(p["item_id"] == it["id"] for p in i["photos"]) else ""}</td>'
                         f'<td><span class=r style="color:{fg};background:{bg}">{l}{" · urgent" if it["urgent"] and it["rating"] == "issue" else ""}</span></td></tr>')
        if rows_:
            secs.append(f"<h2>{_e(sec)}</h2><table>{''.join(rows_)}</table>")
    gen = pics(None)
    c = i["counts"]
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Inspection — {_e(i['property_name'])} — {d.strftime('%b %-d, %Y')}</title><style>
body{{font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;color:#1f2a30;max-width:900px;margin:24px auto;padding:0 16px;background:#fff}}
header{{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:3px solid #4f9cc4;padding-bottom:10px;gap:12px}}
header img{{height:42px}}h1{{font-size:22px;margin:0}}h2{{font-size:15px;text-transform:uppercase;letter-spacing:.06em;color:#4f6470;margin:22px 0 6px}}
table{{width:100%;border-collapse:collapse}}td{{border-top:1px solid #e3e8eb;padding:8px 6px;vertical-align:top}}td:last-child{{width:170px;text-align:right}}
.r{{font-size:12px;padding:2px 8px;border-radius:99px;white-space:nowrap}}.n{{color:#4f6470;font-size:13px;margin-top:3px}}
.ph{{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}}.ph img{{height:120px;border-radius:6px;object-fit:cover}}
.sum{{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}}.sum span{{background:#f3f6f8;border-radius:8px;padding:6px 12px}}
.mute{{color:#667}}.btn{{background:#4f9cc4;color:#fff;border:0;border-radius:8px;padding:8px 14px;font-size:14px;cursor:pointer}}
@media print{{.btn{{display:none}}body{{margin:0}}.ph img{{height:110px}}tr{{break-inside:avoid}}}}
</style></head><body>
<header><div><h1>Property inspection</h1><div><b>{_e(i['property_name'])}</b></div><div class=mute>{_e(i['property_address'])}</div>
<div class=mute>{d.strftime('%B %-d, %Y')} · Inspected by {_e(i['inspector'] or '')}</div></div>
<div style="text-align:right"><img src="/brand/logo.png" alt="Foresite"><div style="margin-top:8px"><button class=btn onclick="print()">Print / save PDF</button></div></div></header>
<div class=sum><span>✓ {c['good']} good</span><span style="color:#b42318">⚠ {c['issue']} need attention</span><span>{c['na']} N/A</span><span>📷 {len(i['photos'])} photos</span></div>
{f"<h2>Summary</h2><p>{_e(i['notes'])}</p>" if i['notes'] else ""}
{''.join(secs)}
{f"<h2>{GENERAL}</h2><div class=ph>{gen}</div>" if gen else ""}
</body></html>"""
