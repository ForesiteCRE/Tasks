"""Owner report packages: which reports each property's owner gets, how often, and how it's sent.

Imported from Accounting's "Report Packages" sheet (Property | Monthly | Quarterly | Submitted Via | Cover Sheet |
Executive Summary | … | Misc Schedules | notes) and editable on each property page. Shown wherever the owner report
packet is put together (the Month-End Close "Compile owner report packet" task).
"""
import io
import json
import re

from . import properties
from .db import rows, tx

# in packet order
ITEMS = ["Cover sheet", "Executive summary", "AppFolio report", "Inspection report", "Site plan", "Insurance report",
         "Delinquency report", "Bank rec & statement", "Distribution schedule", "Loan statement", "Misc schedules"]
FREQS = {"monthly": "Monthly", "quarterly": "Quarterly"}
VIA = ["Email", "Owner portal", "Mail", "Other"]
_HEAD = {"cover sheet": "Cover sheet", "executive summary": "Executive summary", "af report": "AppFolio report",
         "appfolio report": "AppFolio report", "inspection report": "Inspection report", "site plan": "Site plan",
         "insurance report": "Insurance report", "delinquency report": "Delinquency report", "bank rec/ stmt": "Bank rec & statement",
         "bank rec/stmt": "Bank rec & statement", "bank rec": "Bank rec & statement", "distribution schedule": "Distribution schedule",
         "loan stmt": "Loan statement", "loan statement": "Loan statement", "misc schedules": "Misc schedules"}


def init():
    with tx() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(properties)")}
        for col in ("report_freq", "report_via", "report_items"):
            if col not in cols:
                c.execute(f"ALTER TABLE properties ADD COLUMN {col} TEXT")


def package(p: dict) -> dict:
    items = json.loads(p.get("report_items") or "[]")
    return {"freq": p.get("report_freq") or "", "via": p.get("report_via") or "", "items": [i for i in ITEMS if i in items],
            "set": bool(p.get("report_freq") or items)}


def save(pid: int, d: dict):
    sets = {}
    if "freq" in d:
        sets["report_freq"] = d["freq"] if d["freq"] in FREQS else None
    if "via" in d:
        sets["report_via"] = (d["via"] or "").strip()[:40] or None
    if "items" in d:
        sets["report_items"] = json.dumps([i for i in ITEMS if i in (d["items"] or [])])
    if sets:
        with tx() as c:
            c.execute(f"UPDATE properties SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), pid))


# ---------- import from the spreadsheet ----------

def _split(cell: str) -> tuple[str, str]:
    """'Lott's Landing (8336) - AT&T - 8336 FM 78 Converse, TX 78109' → (name, address): the address is the last
    ' - ' part that starts with a street number."""
    parts = [x.strip() for x in str(cell).split(" - ")]
    for i in range(len(parts) - 1, 0, -1):
        if re.match(r"^\d", parts[i]):
            return " - ".join(parts[:i]), " - ".join(parts[i:])
    return parts[0], ""


def parse(data: bytes) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    for ws in wb.worksheets:
        it = ws.iter_rows(values_only=True)
        for head in it:
            h = [str(x or "").strip().lower() for x in head]
            if "property" in h and ("monthly" in h or "submitted via" in h):
                break
        else:
            continue
        col = {name: i for i, name in enumerate(h)}
        out = []
        for r in it:
            if not r or not r[col["property"]]:
                continue
            name, address = _split(r[col["property"]])
            v = lambda key: str(r[col[key]]).strip() if key in col and col[key] < len(r) and r[col[key]] not in (None, "") else ""
            items = [label for key, label in _HEAD.items() if key in col and v(key)]
            freq = "monthly" if v("monthly") else "quarterly" if v("quarterly") else ""
            ni = next((i for i, x in enumerate(h) if i > 0 and x == ""), len(h))   # the unlabeled column = notes
            last = r[ni] if len(r) > ni and r[ni] else None
            notes = str(last).strip() if last else ""
            out.append({"name": name, "address": address, "freq": freq, "via": v("submitted via"),
                        "items": list(dict.fromkeys(items)), "notes": notes})
        return out
    raise ValueError("Couldn't find a sheet with a Property column and report columns (Monthly, Submitted Via, …).")


def _match(c, row: dict):
    pid = properties.property_for_address(row["address"]) if row["address"] else None
    if not pid and row["name"]:
        r = c.execute("SELECT id FROM properties WHERE lower(name)=lower(?)", (row["name"],)).fetchone()
        pid = r[0] if r else None
    return pid


def import_rows(data: bytes, apply: bool = False, create_missing: bool = False) -> dict:
    parsed = parse(data)
    out = {"rows": [], "matched": 0, "new": 0, "with_package": 0, "applied": apply}
    with tx() as c:
        matches = [(_match(c, row), row) for row in parsed]
    for pid, row in matches:
        has = bool(row["freq"] or row["items"])
        out["with_package"] += has
        status = "matched" if pid else ("will create" if create_missing else "not in the app")
        if pid:
            out["matched"] += 1
        else:
            out["new"] += 1
        if apply:
            if not pid and create_missing and row["address"]:
                pid = properties.create(row["address"], name=row["name"])
                status = "created"
            if pid and has:
                save(pid, {"freq": row["freq"], "via": row["via"], "items": row["items"]})
            if pid and row["notes"]:
                with tx() as c:
                    cur = c.execute("SELECT report_notes FROM properties WHERE id=?", (pid,)).fetchone()[0] or ""
                    if row["notes"] not in cur:
                        c.execute("UPDATE properties SET report_notes=? WHERE id=?", ((cur + "\n" if cur else "") + row["notes"], pid))
        out["rows"].append({"name": row["name"], "address": row["address"], "property_id": pid, "status": status,
                            "freq": row["freq"], "via": row["via"], "items": row["items"], "notes": row["notes"]})
    return out


def for_project(project_id: int) -> dict | None:
    with tx() as c:
        p = c.execute("""SELECT pr.* FROM projects p JOIN properties pr ON pr.id=p.property_id WHERE p.id=?""", (project_id,)).fetchone()
    return {"property_id": p["id"], "property": p["name"], **package(dict(p))} if p else None


def overview() -> list[dict]:
    with tx() as c:
        ps = rows(c.execute("SELECT * FROM properties ORDER BY name COLLATE NOCASE"))
    return [{"id": p["id"], "name": p["name"], **package(p)} for p in ps]
