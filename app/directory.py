"""Import Foresite's Property Directory (Master List) → property pages + property teams.

Columns used: Property · Units · Sqft · Start Date · Mgmt Fee Percent · Minimum Fee · Notes · Bldg Mgmt · % Rent ·
Insurance Expiration · Owner(s) · Property Name · Property Address · Site Manager Name · Accountant.
  Site Manager  → Property Manager role
  Accountant    → "Dez" → Accounting 1; "Jackie/Lisa" → Accounting 1 = Jackie, Accounting 2 = Lisa
People on the sheet are first names; the preview matches them to people in the app (and lets you pick or add).
Runs quietly (no Blink messages) and takes a database backup first.
"""
import io
import json
import re
from datetime import date, datetime

from . import blink, properties, team
from .db import rows, tx

COLS = {"property": "property", "units": "units", "sqft": "sqft", "start date": "start", "mgmt fee percent": "fee_pct",
        "minimum fee": "fee_min", "notes": "notes", "bldg mgmt": "bldg_mgmt", "% rent": "pct_rent",
        "insurance expiration": "ins_exp", "owner(s)": "owner", "property name": "name", "property address": "address",
        "site manager name": "pm", "accountant": "acct"}


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _split(cell: str) -> tuple[str, str]:
    """'Name - 123 Main St City, TX' → (name, address); 'Name 5230 Rogers Rd SA, TX' → split at the street number."""
    parts = [x.strip() for x in cell.split(" - ")]
    for i in range(len(parts) - 1, 0, -1):
        if re.match(r"^\d", parts[i]):
            return " - ".join(parts[:i]), " - ".join(parts[i:])
    m = re.search(r"\s(\d{2,6}\s+\S.*,\s*[A-Z]{2}\b.*)$", cell)
    if m:
        return cell[:m.start()].strip(" -"), m.group(1).strip()
    return cell, ""


def parse(data: bytes) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    for ws in wb.worksheets:
        it = ws.iter_rows(values_only=True)
        for head in it:
            h = [_s(x).lower() for x in head]
            if "property" in h and ("site manager name" in h or "accountant" in h):
                break
        else:
            continue
        idx = {COLS[x]: i for i, x in enumerate(h) if x in COLS}
        out = []
        for r in it:
            g = lambda k: _s(r[idx[k]]) if k in idx and idx[k] < len(r) else ""
            prop = g("property")
            if not prop or prop.lower().startswith("properties:"):
                continue
            name, addr = g("name"), g("address")
            sn, sa = _split(prop)
            name, addr = name or sn, addr or sa or prop
            owner, phone = g("owner"), ""
            m = re.search(r"\s*-?\s*Phone:\s*(.+)$", owner, re.I)
            if m:
                owner, phone = owner[:m.start()].strip(" -"), m.group(1).strip()
            out.append({"name": name, "address": addr, "owner": owner.strip(" -"), "owner_phone": phone, "pm": g("pm"),
                        "acct": [x.strip() for x in re.split(r"[/,&]| and ", g("acct")) if x.strip()],
                        "units": g("units"), "sqft": g("sqft"), "start": g("start")[:10], "fee_pct": g("fee_pct"),
                        "fee_min": g("fee_min"), "notes": g("notes"), "bldg_mgmt": g("bldg_mgmt"), "pct_rent": g("pct_rent"),
                        "ins_exp": g("ins_exp")[:10]})
        return out
    raise ValueError("Couldn't find the Master List sheet (needs Property, Site Manager Name and Accountant columns).")


def _find(c, r: dict, shared: set):
    """Existing page for this row: by property name first, then by address (unless several rows share it)."""
    x = c.execute("SELECT id FROM properties WHERE lower(name)=lower(?)", (r["name"],)).fetchone()
    if x:
        return x[0]
    n = properties.norm(r["address"])
    if n and n not in shared:
        x = c.execute("SELECT id FROM properties WHERE norm=?", (n,)).fetchone()
        return x[0] if x else None
    return None


def _shared(items: list[dict]) -> set:
    from collections import Counter
    cnt = Counter(properties.norm(r["address"]) for r in items)
    return {n for n, k in cnt.items() if k > 1}


def _create(c, r: dict) -> int:
    n = properties.norm(r["address"])
    if c.execute("SELECT 1 FROM properties WHERE norm=?", (n,)).fetchone():   # another building at the same address
        n = f"{n} #{re.sub(r'[^a-z0-9]+', '-', r['name'].lower()).strip('-')}"
    return c.execute("INSERT INTO properties(name, address, norm, client) VALUES(?,?,?,?)",
                     (r["name"], r["address"], n, r["owner"] or None)).lastrowid


def _people(c) -> list[dict]:
    return rows(c.execute("SELECT id, name, email FROM users WHERE email IS NULL OR email NOT LIKE '%demo.example.com' ORDER BY name"))


def _guess(first: str, people: list[dict]):
    f = first.lower().strip()
    hits = [p for p in people if p["name"].split()[0].lower() == f]
    if not hits and f in ("dez",):
        hits = [p for p in people if p["name"].lower().startswith("dezarae")]
    return hits[0]["id"] if len(hits) == 1 else None


def _summary_notes(r: dict) -> str:
    bits = [f"Units: {r['units']}" if r["units"] else "", f"Sq ft: {r['sqft']}" if r["sqft"] else "",
            f"Management start: {r['start']}" if r["start"] else "",
            f"Mgmt fee: {r['fee_pct']}%" if r["fee_pct"] not in ("", "n/a", "0") else "",
            f"Minimum fee: ${r['fee_min']}" if r["fee_min"] not in ("", "n/a") else "",
            f"Building mgmt: {r['bldg_mgmt']}" if r["bldg_mgmt"] else "", f"% rent: {r['pct_rent']}" if r["pct_rent"] else "",
            f"Owner phone: {r['owner_phone']}" if r["owner_phone"] and "817-8331" not in r["owner_phone"] else "", r["notes"]]
    return "From the Property Directory — " + " · ".join(b for b in bits if b) if any(bits) else ""


def preview(data: bytes) -> dict:
    items = parse(data)
    with tx() as c:
        people = _people(c)
        listed = set()
        shared = _shared(items)
        for r in items:
            pid = _find(c, r, shared)
            r["property_id"] = pid
            r["flag"] = "Outside Texas — double-check the address" if not re.search(r"\bTX\b|Texas", r["address"], re.I) else ""
            if pid:
                listed.add(pid)
                cur = {x["role"]: x["user_name"] for x in properties.team_list(pid)}
                r["current"] = {"Property Manager": cur.get("Property Manager"), "Accounting 1": cur.get("Accounting 1"),
                                "Accounting 2": cur.get("Accounting 2")}
        others = rows(c.execute("SELECT id, name, address FROM properties WHERE archived_at IS NULL ORDER BY name"))
    names = {}
    for r in items:
        for n in [r["pm"]] + r["acct"]:
            if n:
                names.setdefault(n, {"name": n, "count": 0, "user_id": _guess(n, people)})["count"] += 1
    return {"rows": items, "names": sorted(names.values(), key=lambda x: -x["count"]), "people": people,
            "not_listed": [o for o in others if o["id"] not in listed],
            "counts": {"total": len(items), "existing": sum(1 for r in items if r["property_id"]),
                       "new": sum(1 for r in items if not r["property_id"])}}


def apply(data: bytes, mapping: dict, new_people: dict, archive_others: bool, user_id=None) -> dict:
    """mapping: sheet name → user id (or None). new_people: sheet name → {name, email} to create."""
    from . import archive, maintenance
    maintenance.backup_now()
    items = parse(data)
    with tx() as c:
        for n, p in (new_people or {}).items():
            full, email = (p.get("name") or "").strip(), (p.get("email") or "").strip().lower()
            if not full:
                continue
            ex = c.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone() if email else None
            mapping[n] = ex[0] if ex else c.execute("INSERT INTO users(name, email, role) VALUES(?,?,'staff')",
                                                    (full, email or None)).lastrowid
        roles = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM project_roles")}
    created = updated = assigned = 0
    listed = set()
    shared = _shared(items)
    with blink.quiet():
        for r in items:
            with tx() as c:
                pid = _find(c, r, shared)
            if pid:
                updated += 1
                with tx() as c:
                    c.execute("UPDATE properties SET archived_at=NULL WHERE id=?", (pid,))
                properties.update(pid, {"name": r["name"], "client": r["owner"] or None})
            else:
                with tx() as c:
                    pid = _create(c, r)
                created += 1
            listed.add(pid)
            note = _summary_notes(r)
            if note:
                with tx() as c:
                    cur = c.execute("SELECT notes FROM properties WHERE id=?", (pid,)).fetchone()[0] or ""
                    if "From the Property Directory" not in cur:
                        c.execute("UPDATE properties SET notes=? WHERE id=?", ((cur + "\n" if cur else "") + note, pid))
            slots = [("Property Manager", r["pm"])] + list(zip(("Accounting 1", "Accounting 2"), r["acct"]))
            for role, who in slots:
                uid = mapping.get(who) if who else None
                if uid and role in roles:
                    properties.set_member(pid, roles[role], int(uid))
                    assigned += 1
            if r["ins_exp"] and r["ins_exp"] >= date.today().isoformat():
                with tx() as c:
                    dup = c.execute("SELECT 1 FROM critical_dates WHERE property_id=? AND kind='insurance_renewal' AND date=?",
                                    (pid, r["ins_exp"])).fetchone()
                if not dup:
                    properties.add_date(pid, {"kind": "insurance_renewal", "title": "Property insurance renewal",
                                              "date": r["ins_exp"], "remind_days": "60,30"}, source="import")
        archived = 0
        if archive_others:
            with tx() as c:
                others = [x[0] for x in c.execute("SELECT id FROM properties WHERE archived_at IS NULL")]
            for o in others:
                if o not in listed:
                    archive.archive_property(o)
                    archived += 1
    return {"created": created, "updated": updated, "assigned": assigned, "archived": archived}
