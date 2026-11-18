"""Vendors: a shared list of the vendors our property managers have worked with, star ratings + notes, which
properties use (and used) them, and which projects they're assigned to.

Ratings drive two badges so people can tell at a glance:
  👍 Recommended      – averages 4.5★+ over at least 2 reviews and nothing poor recently
  ⚠️ Mixed / Not recommended – averages under 3★, or a 1–2★ review in the last 12 months
"""
from datetime import date, timedelta

from .db import rows, tx

TRADES = ["Landscaping", "Utilities – Electric", "Utilities – Water", "Utilities – Gas", "Trash / recycling",
          "Power washing", "Roofing", "HVAC", "Plumbing", "Electrical", "Pest control", "Parking lot / paving",
          "Fire & life safety", "Janitorial", "Security", "Elevator", "Painting", "Signage", "Locksmith",
          "General contractor", "Other"]
PRICE_LABELS = {1: "Budget", 2: "Below average", 3: "Average", 4: "Above average", 5: "Premium"}
# Slots every property page shows (others can be added per property)
PROPERTY_SLOTS = ["Landscaping", "Utilities – Electric", "Utilities – Water", "Utilities – Gas", "Trash / recycling",
                  "Power washing", "Roofing", "HVAC", "Plumbing", "Pest control", "Parking lot / paving",
                  "Fire & life safety", "Janitorial"]


def init():
    with tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vendors(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, trades TEXT NOT NULL DEFAULT '', contact_name TEXT, phone TEXT,
            email TEXT, website TEXT, notes TEXT, archived INTEGER NOT NULL DEFAULT 0, created_by INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        c.execute("""CREATE TABLE IF NOT EXISTS vendor_reviews(
            id INTEGER PRIMARY KEY, vendor_id INTEGER NOT NULL, user_id INTEGER, property_id INTEGER, project_id INTEGER,
            stars INTEGER NOT NULL, note TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        if "price" not in {r[1] for r in c.execute("PRAGMA table_info(vendor_reviews)")}:
            c.execute("ALTER TABLE vendor_reviews ADD COLUMN price INTEGER")   # 1 ($, budget) … 5 ($$$$$, premium)
        c.execute("""CREATE TABLE IF NOT EXISTS property_vendors(
            id INTEGER PRIMARY KEY, property_id INTEGER NOT NULL, trade TEXT NOT NULL, vendor_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'current', started TEXT, ended TEXT, notes TEXT, created_by INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS project_vendors(
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, vendor_id INTEGER NOT NULL, trade TEXT, notes TEXT,
            created_by INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        c.execute("""CREATE TABLE IF NOT EXISTS vendor_cois(
            id INTEGER PRIMARY KEY, property_id INTEGER NOT NULL, vendor_id INTEGER NOT NULL, filename TEXT NOT NULL,
            path TEXT NOT NULL, content_type TEXT, size INTEGER, expires TEXT, insured_ok INTEGER, foresite_ok INTEGER,
            carrier TEXT, note TEXT, date_id INTEGER, verified_by INTEGER, uploaded_by INTEGER,
            uploaded_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        c.execute("""CREATE TABLE IF NOT EXISTS property_vendor_slots(
            property_id INTEGER NOT NULL, trade TEXT NOT NULL, PRIMARY KEY(property_id, trade))""")


def _trades(s: str | None) -> list[str]:
    return [t.strip() for t in (s or "").split("|") if t.strip()]


def _clean_trades(v) -> str:
    items = v if isinstance(v, list) else (v or "").split("|")
    out = []
    for t in items:
        t = (t or "").strip()
        if t and t not in out:
            out.append(t)
    return "|".join(out)


# ---------- ratings & badges ----------

def _stats(c, vendor_id: int) -> dict:
    r = c.execute("SELECT COUNT(*) n, AVG(stars) avg FROM vendor_reviews WHERE vendor_id=?", (vendor_id,)).fetchone()
    recent_bad = c.execute("""SELECT stars, note, created_at FROM vendor_reviews WHERE vendor_id=? AND stars<=2
                              AND created_at >= ? ORDER BY id DESC LIMIT 1""",
                           (vendor_id, (date.today() - timedelta(days=365)).isoformat())).fetchone()
    pr = c.execute("SELECT COUNT(price) n, AVG(price) avg FROM vendor_reviews WHERE vendor_id=? AND price IS NOT NULL",
                   (vendor_id,)).fetchone()
    n, avg = r["n"], round(r["avg"], 1) if r["avg"] is not None else None
    if n and (avg < 3 or recent_bad):
        badge = "warn" if avg >= 3 else "bad"
    elif n >= 2 and avg >= 4.5:
        badge = "good"
    else:
        badge = None
    price = round(pr["avg"]) if pr["avg"] is not None else None
    return {"reviews": n, "avg": avg, "badge": badge, "price": price, "price_votes": pr["n"],
            "price_label": PRICE_LABELS.get(price),
            "badge_label": {"good": "👍 Recommended", "warn": "⚠️ Mixed reviews", "bad": "⚠️ Not recommended"}.get(badge),
            "warning": (f"{recent_bad['stars']}★ on {recent_bad['created_at'][:10]}: “{recent_bad['note']}”"
                        if recent_bad and recent_bad["note"] else
                        f"{recent_bad['stars']}★ review on {recent_bad['created_at'][:10]}" if recent_bad else
                        (f"Averages {avg}★ over {n} review{'s' if n != 1 else ''}" if badge == "bad" else None))}


def list_all(trade: str | None = None, q: str | None = None, include_archived: bool = False) -> list[dict]:
    with tx() as c:
        vs = rows(c.execute("SELECT * FROM vendors" + ("" if include_archived else " WHERE archived=0")
                            + " ORDER BY name COLLATE NOCASE"))
        out = []
        for v in vs:
            v["trades"] = _trades(v["trades"])
            if trade and trade not in v["trades"]:
                continue
            if q and q.lower() not in (v["name"] + " " + " ".join(v["trades"]) + " " + (v["contact_name"] or "")).lower():
                continue
            v.update(_stats(c, v["id"]))
            v["current_properties"] = [dict(r) for r in c.execute(
                """SELECT DISTINCT p.id, p.name, pv.trade FROM property_vendors pv JOIN properties p ON p.id=pv.property_id
                   WHERE pv.vendor_id=? AND pv.status='current' ORDER BY p.name""", (v["id"],))]
            v["past_count"] = c.execute("""SELECT COUNT(DISTINCT property_id) FROM property_vendors WHERE vendor_id=?
                                           AND status='past'""", (v["id"],)).fetchone()[0]
            out.append(v)
    # best first: rated vendors by average, then most used
    out.sort(key=lambda v: (-(v["avg"] or 0) if v["reviews"] else 1, -len(v["current_properties"]), v["name"].lower()))
    return out


def get(vendor_id: int) -> dict | None:
    with tx() as c:
        v = c.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
        if not v:
            return None
        v = dict(v)
        v["trades"] = _trades(v["trades"])
        v.update(_stats(c, vendor_id))
        v["properties"] = rows(c.execute(
            """SELECT pv.id, pv.property_id, p.name AS property_name, pv.trade, pv.status, pv.started, pv.ended, pv.notes,
                      (SELECT ROUND(AVG(stars),1) FROM vendor_reviews r WHERE r.vendor_id=pv.vendor_id AND r.property_id=pv.property_id) AS avg_here,
                      (SELECT COUNT(*) FROM vendor_reviews r WHERE r.vendor_id=pv.vendor_id AND r.property_id=pv.property_id) AS reviews_here
               FROM property_vendors pv JOIN properties p ON p.id=pv.property_id WHERE pv.vendor_id=?
               ORDER BY pv.status='past', COALESCE(pv.ended, '9999') DESC, p.name""", (vendor_id,)))
        for p in v["properties"]:
            p["coi"] = coi_status(c, p["property_id"], vendor_id)
        v["projects"] = rows(c.execute(
            """SELECT pj.id AS project_id, pj.name AS project_name, pj.status, pvj.trade, pvj.notes, pvj.created_at
               FROM project_vendors pvj JOIN projects pj ON pj.id=pvj.project_id WHERE pvj.vendor_id=?
               ORDER BY pvj.id DESC""", (vendor_id,)))
        v["review_list"] = rows(c.execute(
            """SELECT r.*, COALESCE(u.name,'Someone') AS who, p.name AS property_name, pj.name AS project_name
               FROM vendor_reviews r LEFT JOIN users u ON u.id=r.user_id LEFT JOIN properties p ON p.id=r.property_id
               LEFT JOIN projects pj ON pj.id=r.project_id WHERE r.vendor_id=? ORDER BY r.id DESC""", (vendor_id,)))
    return v


def create(d: dict, user_id=None) -> int:
    name = (d.get("name") or "").strip()
    if not name:
        raise ValueError("Give the vendor a name.")
    with tx() as c:
        if c.execute("SELECT 1 FROM vendors WHERE lower(name)=lower(?) AND archived=0", (name,)).fetchone():
            raise ValueError(f"“{name}” is already in the vendor list.")
        return c.execute("""INSERT INTO vendors(name, trades, contact_name, phone, email, website, notes, created_by)
                            VALUES(?,?,?,?,?,?,?,?)""",
                         (name, _clean_trades(d.get("trades")), *[(d.get(k) or "").strip() or None
                                                                  for k in ("contact_name", "phone", "email", "website", "notes")],
                          user_id)).lastrowid


def update(vendor_id: int, d: dict):
    sets = {}
    for k in ("name", "contact_name", "phone", "email", "website", "notes"):
        if k in d and d[k] is not None:
            sets[k] = d[k].strip() or (None if k != "name" else None)
    if "name" in sets and not sets["name"]:
        raise ValueError("Name can't be empty.")
    if "trades" in d and d["trades"] is not None:
        sets["trades"] = _clean_trades(d["trades"])
    if "archived" in d and d["archived"] is not None:
        sets["archived"] = int(bool(d["archived"]))
    if sets:
        with tx() as c:
            c.execute(f"UPDATE vendors SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), vendor_id))


def add_review(vendor_id: int, user_id, stars: int, note: str | None, property_id=None, project_id=None,
               price: int | None = None) -> int:
    stars = int(stars)
    if not 1 <= stars <= 5:
        raise ValueError("Pick 1 to 5 stars.")
    if price is not None and not 1 <= int(price) <= 5:
        raise ValueError("Price is $ to $$$$$.")
    with tx() as c:
        if not c.execute("SELECT 1 FROM vendors WHERE id=?", (vendor_id,)).fetchone():
            raise ValueError("Vendor not found.")
        return c.execute("""INSERT INTO vendor_reviews(vendor_id, user_id, property_id, project_id, stars, note, price)
                            VALUES(?,?,?,?,?,?,?)""", (vendor_id, user_id, property_id or None, project_id or None, stars,
                                                       (note or "").strip() or None, int(price) if price else None)).lastrowid


def delete_review(review_id: int):
    with tx() as c:
        c.execute("DELETE FROM vendor_reviews WHERE id=?", (review_id,))


def review_owner(review_id: int):
    with tx() as c:
        r = c.execute("SELECT user_id FROM vendor_reviews WHERE id=?", (review_id,)).fetchone()
    return r["user_id"] if r else None


# ---------- property slots ----------

def property_vendors(property_id: int) -> dict:
    with tx() as c:
        extra = [r[0] for r in c.execute("SELECT trade FROM property_vendor_slots WHERE property_id=?", (property_id,))]
        assigned = rows(c.execute(
            """SELECT pv.*, v.name AS vendor_name, v.phone, v.email, v.contact_name FROM property_vendors pv
               JOIN vendors v ON v.id=pv.vendor_id WHERE pv.property_id=? ORDER BY pv.status='past', pv.ended DESC, pv.id""",
            (property_id,)))
        for a in assigned:
            a.update({k: v for k, v in _stats(c, a["vendor_id"]).items() if k in ("avg", "reviews", "badge", "badge_label", "warning", "price", "price_label")})
            a["coi"] = coi_status(c, property_id, a["vendor_id"]) if needs_coi(a["trade"]) else {"state": "na", "label": ""}
            a["coi_count"] = c.execute("SELECT COUNT(*) FROM vendor_cois WHERE property_id=? AND vendor_id=?",
                                       (property_id, a["vendor_id"])).fetchone()[0]
    slots = list(PROPERTY_SLOTS)
    for t in extra + [a["trade"] for a in assigned]:
        if t not in slots:
            slots.append(t)
    return {"slots": [{"trade": t, "current": [a for a in assigned if a["trade"] == t and a["status"] == "current"],
                       "past": [a for a in assigned if a["trade"] == t and a["status"] == "past"]} for t in slots],
            "trades": TRADES}


def set_property_vendor(property_id: int, trade: str, vendor_id: int | None, user_id=None, notes: str | None = None,
                        replace: bool = True):
    """Put a vendor in a property's slot. By default the current one moves to 'previously used' (with an end date).
    replace=False adds a second current vendor for the same trade. vendor_id=None just ends the current one(s)."""
    trade = (trade or "").strip() or "Other"
    today = date.today().isoformat()
    with tx() as c:
        if replace or vendor_id is None:
            leaving = [r[0] for r in c.execute("""SELECT vendor_id FROM property_vendors WHERE property_id=? AND trade=?
                                                  AND status='current' AND vendor_id IS NOT ?""", (property_id, trade, vendor_id))]
            c.execute("""UPDATE property_vendors SET status='past', ended=? WHERE property_id=? AND trade=? AND status='current'
                         AND vendor_id IS NOT ?""", (today, property_id, trade, vendor_id))
            for old in leaving:
                _stop_coi_reminders(c, property_id, old)
        if vendor_id:
            if not c.execute("""SELECT 1 FROM property_vendors WHERE property_id=? AND trade=? AND vendor_id=?
                                AND status='current'""", (property_id, trade, vendor_id)).fetchone():
                c.execute("""INSERT INTO property_vendors(property_id, trade, vendor_id, status, started, notes, created_by)
                             VALUES(?,?,?,'current',?,?,?)""", (property_id, trade, vendor_id, today, notes, user_id))
            v = c.execute("SELECT trades FROM vendors WHERE id=?", (vendor_id,)).fetchone()
            if v and trade not in _trades(v["trades"]):   # remember the trade on the vendor too
                c.execute("UPDATE vendors SET trades=? WHERE id=?", (_clean_trades(_trades(v["trades"]) + [trade]), vendor_id))


def end_property_vendor(pv_id: int):
    with tx() as c:
        r = c.execute("SELECT property_id, vendor_id FROM property_vendors WHERE id=?", (pv_id,)).fetchone()
        c.execute("UPDATE property_vendors SET status='past', ended=? WHERE id=? AND status='current'",
                  (date.today().isoformat(), pv_id))
        if r:
            _stop_coi_reminders(c, r["property_id"], r["vendor_id"])


def _stop_coi_reminders(c, property_id: int, vendor_id: int):
    """No longer working there → no more 'COI expires' reminders for that vendor at that property."""
    if c.execute("""SELECT 1 FROM property_vendors WHERE property_id=? AND vendor_id=? AND status='current'""",
                 (property_id, vendor_id)).fetchone():
        return
    c.execute("""UPDATE critical_dates SET status='handled', handled_at=date('now') WHERE status='open' AND id IN
                 (SELECT date_id FROM vendor_cois WHERE property_id=? AND vendor_id=? AND date_id IS NOT NULL)""",
              (property_id, vendor_id))


def remove_property_vendor(pv_id: int):
    with tx() as c:
        c.execute("DELETE FROM property_vendors WHERE id=?", (pv_id,))


def add_slot(property_id: int, trade: str):
    trade = (trade or "").strip()
    if not trade:
        raise ValueError("Name the service.")
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO property_vendor_slots(property_id, trade) VALUES(?,?)", (property_id, trade))


# ---------- projects ----------

def project_vendors(project_id: int) -> list[dict]:
    with tx() as c:
        vs = rows(c.execute("""SELECT pv.*, v.name AS vendor_name, v.phone, v.email, v.contact_name FROM project_vendors pv
                               JOIN vendors v ON v.id=pv.vendor_id WHERE pv.project_id=? ORDER BY pv.id""", (project_id,)))
        for a in vs:
            a.update({k: v for k, v in _stats(c, a["vendor_id"]).items() if k in ("avg", "reviews", "badge", "badge_label", "warning", "price", "price_label")})
    return vs


def add_project_vendor(project_id: int, vendor_id: int, trade: str | None, notes: str | None, user_id=None) -> int:
    with tx() as c:
        if not c.execute("SELECT 1 FROM vendors WHERE id=?", (vendor_id,)).fetchone():
            raise ValueError("Vendor not found.")
        same = c.execute("SELECT id FROM project_vendors WHERE project_id=? AND vendor_id=? AND COALESCE(trade,'')=?",
                         (project_id, vendor_id, trade or "")).fetchone()
        if same:
            return same["id"]
        return c.execute("""INSERT INTO project_vendors(project_id, vendor_id, trade, notes, created_by) VALUES(?,?,?,?,?)""",
                         (project_id, vendor_id, (trade or "").strip() or None, (notes or "").strip() or None, user_id)).lastrowid


def remove_project_vendor(pv_id: int):
    with tx() as c:
        c.execute("DELETE FROM project_vendors WHERE id=?", (pv_id,))


# ---------- certificates of insurance, per vendor per property ----------

COI_WARN_DAYS = 30


def needs_coi(trade: str | None) -> bool:
    """Utility companies don't give certificates of insurance; every other vendor working on site does."""
    return not (trade or "").startswith("Utilities")


def coi_status(c, property_id: int, vendor_id: int) -> dict:
    """Latest COI this vendor gave for this property: valid / expiring / expired / not named / unverified / missing."""
    d = c.execute("""SELECT * FROM vendor_cois WHERE property_id=? AND vendor_id=?
                     ORDER BY COALESCE(expires,'') DESC, id DESC LIMIT 1""", (property_id, vendor_id)).fetchone()
    if not d:
        return {"state": "missing", "label": "No COI on file"}
    d = dict(d)
    today = date.today()
    exp = date.fromisoformat(d["expires"]) if d["expires"] else None
    if exp and exp < today:
        state, label = "expired", f"COI expired {exp:%b %d, %Y}"
    elif d["insured_ok"] == 0:
        state, label = "not_named", "Property not named additional insured"
    elif exp and (exp - today).days <= COI_WARN_DAYS:
        state, label = "expiring", f"COI expires {exp:%b %d} ({(exp - today).days} days)"
    elif d["insured_ok"] == 1 and exp:
        state, label = "ok", f"COI valid until {exp:%b %d, %Y}"
    else:
        state, label = "unverified", "COI on file — not verified" + (f" (expires {exp:%b %d, %Y})" if exp else "")
    return {"state": state, "label": label, "coi_id": d["id"], "expires": d["expires"], "insured_ok": d["insured_ok"],
            "foresite_ok": d["foresite_ok"], "carrier": d["carrier"], "note": d["note"], "filename": d["filename"]}


def cois(property_id: int, vendor_id: int) -> list[dict]:
    with tx() as c:
        return rows(c.execute("""SELECT k.*, u.name AS uploaded_by_name FROM vendor_cois k LEFT JOIN users u ON u.id=k.uploaded_by
                                 WHERE property_id=? AND vendor_id=? ORDER BY id DESC""", (property_id, vendor_id)))


COI_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["is_coi", "named_insured", "carrier", "expires", "property_additional_insured", "foresite_additional_insured", "evidence"],
    "properties": {
        "is_coi": {"type": "boolean"},
        "named_insured": {"type": "string", "description": "the vendor/insured company on the certificate"},
        "carrier": {"type": "string", "description": "insurer(s), short"},
        "expires": {"type": "string", "description": "EARLIEST expiration among the general liability / auto / umbrella / workers comp policies listed, YYYY-MM-DD; empty if none"},
        "property_additional_insured": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "foresite_additional_insured": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "evidence": {"type": "string", "description": "the additional insured / certificate holder / description-of-operations wording you relied on, and anything missing"},
    },
}


def read_coi(data: bytes, content_type: str, filename: str, prop: dict, vendor_name: str) -> dict | None:
    from . import intake
    if not intake.ai_enabled():
        return None
    import json
    import anthropic
    client = anthropic.Anthropic()
    system = ("You check vendor certificates of insurance (ACORD 25 etc.) for a commercial property manager, Foresite Real Estate. "
              "A vendor must name THIS property's owner (or the property itself) as ADDITIONAL INSURED — look at the Additional "
              "Insured box/column (ADDL INSD), the Description of Operations / Locations box and any attached endorsements. Being "
              "listed only as Certificate Holder is NOT additional insured; say 'unclear' if the ADDL INSD box is ticked but nobody "
              "is named. Also report whether Foresite (Foresite Real Estate, Inc. / Foresite Commercial Real Estate) is named.")
    try:
        resp = client.beta.messages.create(
            model=intake.MODEL, max_tokens=2000, system=system,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": COI_SCHEMA}},
            messages=[{"role": "user", "content": [
                intake.document_block(data, content_type, filename),
                {"type": "text", "text": f"Vendor: {vendor_name}\nProperty: {prop.get('name')} — {prop.get('address')}\n"
                                         f"Property owner: {prop.get('client') or 'unknown'}\nToday: {date.today():%Y-%m-%d}"}]}],
        )
        if resp.stop_reason != "end_turn":
            return None
        return json.loads(next(b.text for b in resp.content if b.type == "text"))
    except Exception:
        import logging
        logging.getLogger("vendors").exception("COI read failed")
        return None


def _pm_for(c, property_id: int):
    r = c.execute("""SELECT pt.user_id FROM property_team pt JOIN project_roles r ON r.id=pt.role_id
                     WHERE pt.property_id=? AND r.name='Property Manager'""", (property_id,)).fetchone()
    return r["user_id"] if r else None


def save_coi(property_id: int, vendor_id: int, data: bytes, filename: str, content_type: str, user_id=None,
             read: bool = True) -> dict:
    """Keep the COI for this property+vendor, have Claude check it, and track its expiry as a critical date."""
    import os
    import secrets
    from . import properties
    ext = os.path.splitext(filename or "")[1][:8].lower() or ".pdf"
    name = f"coi-{property_id}-{vendor_id}-{secrets.token_hex(5)}{ext}"
    with open(os.path.join(properties._doc_dir(), name), "wb") as f:
        f.write(data)
    with tx() as c:
        prop = dict(c.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone())
        vendor = c.execute("SELECT name FROM vendors WHERE id=?", (vendor_id,)).fetchone()
        cid = c.execute("""INSERT INTO vendor_cois(property_id, vendor_id, filename, path, content_type, size, uploaded_by)
                           VALUES(?,?,?,?,?,?,?)""", (property_id, vendor_id, filename or "certificate", name, content_type,
                                                      len(data), user_id)).lastrowid
    out = read_coi(data, content_type, filename, prop, vendor["name"]) if read else None
    if out:
        exp = None
        try:
            exp = date.fromisoformat((out.get("expires") or "")[:10]).isoformat()
        except ValueError:
            pass
        yn = {"yes": 1, "no": 0}
        set_coi(cid, {"expires": exp, "insured_ok": yn.get(out.get("property_additional_insured")),
                      "foresite_ok": yn.get(out.get("foresite_additional_insured")),
                      "carrier": out.get("carrier") or None,
                      "note": out.get("evidence") or None}, user_id=None)
    with tx() as c:
        st = coi_status(c, property_id, vendor_id)
    return {"id": cid, "read": out, "status": st}


def set_coi(coi_id: int, fields: dict, user_id=None):
    """Update expiry / verification (by Claude, or a person confirming), and keep the expiry critical date in step."""
    from . import properties
    sets = {k: fields[k] for k in ("expires", "insured_ok", "foresite_ok", "carrier", "note") if k in fields}
    if "insured_ok" in sets and sets["insured_ok"] is not None:
        sets["insured_ok"] = int(bool(sets["insured_ok"]))
    if user_id and "insured_ok" in fields:
        sets["verified_by"] = user_id
    with tx() as c:
        if sets:
            c.execute(f"UPDATE vendor_cois SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), coi_id))
        k = dict(c.execute("SELECT * FROM vendor_cois WHERE id=?", (coi_id,)).fetchone())
        vendor = c.execute("SELECT name FROM vendors WHERE id=?", (k["vendor_id"],)).fetchone()["name"]
        # earlier certificates' expiry reminders are no longer needed
        for r in c.execute("""SELECT date_id FROM vendor_cois WHERE property_id=? AND vendor_id=? AND id!=? AND date_id IS NOT NULL""",
                           (k["property_id"], k["vendor_id"], coi_id)).fetchall():
            c.execute("UPDATE critical_dates SET status='handled', handled_at=date('now') WHERE id=? AND status='open'", (r[0],))
    if k["expires"]:
        if k["date_id"]:
            properties.update_date(k["date_id"], {"date": k["expires"]})
        else:
            did = properties.add_date(k["property_id"], {
                "kind": "vendor_coi", "title": f"COI expires – {vendor}", "date": k["expires"], "assignee_id": None,
                "remind_days": "30,7", "notes": f"Get an updated certificate from {vendor} naming the property as additional insured."},
                source="coi")
            with tx() as c:
                c.execute("UPDATE vendor_cois SET date_id=? WHERE id=?", (did, coi_id))


def coi_file(coi_id: int):
    import os
    from . import properties
    with tx() as c:
        d = c.execute("SELECT * FROM vendor_cois WHERE id=?", (coi_id,)).fetchone()
    if not d:
        return None
    path = os.path.join(properties._doc_dir(), os.path.basename(d["path"]))
    return (dict(d), path) if os.path.exists(path) else None


def delete_coi(coi_id: int):
    from . import properties
    with tx() as c:
        d = c.execute("SELECT path, date_id FROM vendor_cois WHERE id=?", (coi_id,)).fetchone()
        c.execute("DELETE FROM vendor_cois WHERE id=?", (coi_id,))
    if d:
        properties._remove_file(d["path"])
        if d["date_id"]:
            properties.delete_date(d["date_id"])
