"""Foresite's OWN certificates of insurance, one per property.

Management agreements require Foresite to carry insurance (GL, professional/E&O, crime/fidelity, workers comp, auto…)
naming each owner as additional insured, with certain limits. For each property we keep:
  • what the agreement requires (properties.our_coi_requirements — typed or read from the management agreement)
  • our certificate(s) for that property, checked by Claude: owner named as additional insured? limits meet the
    requirements (gaps listed)? earliest expiration → a critical date with 30/7-day reminders to the property manager
  • an email to Foresite's insurance agent asking for a certificate (agent set once in settings)
"""
import json
import logging
import os
import secrets
from datetime import date

from .db import get_setting, rows, set_setting, tx

log = logging.getLogger("ourcoi")
WARN_DAYS = 30


def init():
    with tx() as c:
        if "our_coi_requirements" not in {r[1] for r in c.execute("PRAGMA table_info(properties)")}:
            c.execute("ALTER TABLE properties ADD COLUMN our_coi_requirements TEXT")
        c.execute("""CREATE TABLE IF NOT EXISTS our_cois(
            id INTEGER PRIMARY KEY, property_id INTEGER NOT NULL, filename TEXT NOT NULL, path TEXT NOT NULL, content_type TEXT,
            expires TEXT, carrier TEXT, owner_named INTEGER, meets_reqs INTEGER, gaps TEXT, note TEXT, date_id INTEGER,
            verified_by INTEGER, uploaded_by INTEGER, uploaded_at TEXT NOT NULL DEFAULT (datetime('now')))""")


def agent() -> dict:
    return {"name": get_setting("ourcoi.agent_name") or "", "email": get_setting("ourcoi.agent_email") or "",
            "company": get_setting("ourcoi.agent_company") or ""}


def set_agent(d: dict):
    for k in ("name", "email", "company"):
        if k in d and d[k] is not None:
            set_setting(f"ourcoi.agent_{k}", d[k].strip())


def status(c, property_id: int) -> dict:
    d = c.execute("""SELECT * FROM our_cois WHERE property_id=? ORDER BY COALESCE(expires,'') DESC, id DESC LIMIT 1""",
                  (property_id,)).fetchone()
    if not d:
        return {"state": "missing", "label": "Our COI not on file"}
    d = dict(d)
    today = date.today()
    exp = date.fromisoformat(d["expires"]) if d["expires"] else None
    if exp and exp < today:
        st, lb = "expired", f"Our COI expired {exp:%b %d, %Y}"
    elif d["owner_named"] == 0:
        st, lb = "not_named", "Owner not named additional insured on our COI"
    elif d["meets_reqs"] == 0:
        st, lb = "gaps", "Our COI doesn't meet the agreement's requirements"
    elif exp and (exp - today).days <= WARN_DAYS:
        st, lb = "expiring", f"Our COI expires {exp:%b %d} ({(exp - today).days} days)"
    elif d["owner_named"] == 1 and exp:
        st, lb = "ok", f"Our COI valid until {exp:%b %d, %Y}"
    else:
        st, lb = "unverified", "Our COI on file — not verified"
    return {"state": st, "label": lb, "coi_id": d["id"], "expires": d["expires"], "gaps": d["gaps"], "note": d["note"]}


def status_id(property_id: int) -> dict:
    with tx() as c:
        return status(c, property_id)


def listing(property_id: int) -> list[dict]:
    with tx() as c:
        return rows(c.execute("""SELECT k.*, u.name AS uploaded_by_name, v.name AS verified_by_name FROM our_cois k
                                 LEFT JOIN users u ON u.id=k.uploaded_by LEFT JOIN users v ON v.id=k.verified_by
                                 WHERE k.property_id=? ORDER BY k.id DESC""", (property_id,)))


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["is_coi", "named_insured", "carrier", "expires", "owner_additional_insured", "meets_requirements", "gaps", "evidence"],
    "properties": {
        "is_coi": {"type": "boolean"},
        "named_insured": {"type": "string", "description": "the insured on the certificate (should be Foresite)"},
        "carrier": {"type": "string"},
        "expires": {"type": "string", "description": "EARLIEST expiration of the listed policies, YYYY-MM-DD; empty if none"},
        "owner_additional_insured": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "meets_requirements": {"type": "string", "enum": ["yes", "no", "unclear", "no_requirements_given"]},
        "gaps": {"type": "string", "description": "missing coverages / limits below what's required / wrong names; empty if none"},
        "evidence": {"type": "string", "description": "short: the coverages and limits shown and the additional insured wording relied on"},
    },
}


def read(data: bytes, content_type: str, filename: str, prop: dict) -> dict | None:
    from . import intake
    if not intake.ai_enabled():
        return None
    import anthropic
    try:
        resp = anthropic.Anthropic().beta.messages.create(
            model=intake.MODEL, max_tokens=2000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=("You check a certificate of insurance issued for FORESITE REAL ESTATE (the property manager) for one property. "
                    "The management agreement requires Foresite to carry certain coverages and to name the property OWNER as "
                    "additional insured. Check the ADDL INSD column, Description of Operations and endorsements — being only the "
                    "certificate holder is not additional insured. Compare each required coverage and limit with what the "
                    "certificate shows and list anything missing or too low."),
            messages=[{"role": "user", "content": [
                intake.document_block(data, content_type, filename),
                {"type": "text", "text": f"Property: {prop.get('name')} — {prop.get('address')}\nOwner: {prop.get('client') or 'unknown'}\n"
                                         f"Requirements from the management agreement:\n{prop.get('our_coi_requirements') or '(none recorded)'}\n"
                                         f"Today: {date.today():%Y-%m-%d}"}]}])
        if resp.stop_reason != "end_turn":
            return None
        return json.loads(next(b.text for b in resp.content if b.type == "text"))
    except Exception:
        log.exception("our COI read failed")
        return None


def save(property_id: int, data: bytes, filename: str, content_type: str, user_id=None) -> dict:
    from . import properties
    ext = os.path.splitext(filename or "")[1][:8].lower() or ".pdf"
    name = f"ourcoi-{property_id}-{secrets.token_hex(5)}{ext}"
    with open(os.path.join(properties._doc_dir(), name), "wb") as f:
        f.write(data)
    with tx() as c:
        prop = dict(c.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone())
        cid = c.execute("INSERT INTO our_cois(property_id, filename, path, content_type, uploaded_by) VALUES(?,?,?,?,?)",
                        (property_id, filename or "certificate", name, content_type, user_id)).lastrowid
    out = read(data, content_type, filename, prop)
    if out:
        exp = None
        try:
            exp = date.fromisoformat((out.get("expires") or "")[:10]).isoformat()
        except ValueError:
            pass
        yn = {"yes": 1, "no": 0}
        update(cid, {"expires": exp, "owner_named": yn.get(out.get("owner_additional_insured")),
                     "meets_reqs": yn.get(out.get("meets_requirements")), "gaps": out.get("gaps") or None,
                     "carrier": out.get("carrier") or None, "note": out.get("evidence") or None})
    return {"id": cid, "read": out, "status": status_id(property_id)}


def update(coi_id: int, fields: dict, user_id=None):
    from . import properties
    sets = {k: fields[k] for k in ("expires", "owner_named", "meets_reqs", "gaps", "carrier", "note") if k in fields}
    for k in ("owner_named", "meets_reqs"):
        if k in sets and sets[k] is not None:
            sets[k] = int(bool(sets[k]))
    if user_id and ({"owner_named", "meets_reqs"} & set(fields)):
        sets["verified_by"] = user_id
    with tx() as c:
        if sets:
            c.execute(f"UPDATE our_cois SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), coi_id))
        k = dict(c.execute("SELECT * FROM our_cois WHERE id=?", (coi_id,)).fetchone())
        for r in c.execute("SELECT date_id FROM our_cois WHERE property_id=? AND id!=? AND date_id IS NOT NULL",
                           (k["property_id"], coi_id)).fetchall():   # older certificates' reminders retire
            c.execute("UPDATE critical_dates SET status='handled', handled_at=date('now') WHERE id=? AND status='open'", (r[0],))
    if k["expires"]:
        if k["date_id"]:
            properties.update_date(k["date_id"], {"date": k["expires"]})
        else:
            did = properties.add_date(k["property_id"], {
                "kind": "our_coi", "title": "Our certificate of insurance expires", "date": k["expires"], "remind_days": "30,7",
                "notes": "Ask our insurance agent for the renewal certificate naming the owner as additional insured "
                         "(✉️ Request certificate on the property page)."}, source="coi")
            with tx() as c:
                c.execute("UPDATE our_cois SET date_id=? WHERE id=?", (did, coi_id))


def delete(coi_id: int):
    from . import properties
    with tx() as c:
        d = c.execute("SELECT path, date_id FROM our_cois WHERE id=?", (coi_id,)).fetchone()
        c.execute("DELETE FROM our_cois WHERE id=?", (coi_id,))
    if d:
        properties._remove_file(d["path"])
        if d["date_id"]:
            properties.delete_date(d["date_id"])


def file(coi_id: int):
    from . import properties
    with tx() as c:
        d = c.execute("SELECT * FROM our_cois WHERE id=?", (coi_id,)).fetchone()
    if not d:
        return None
    p = os.path.join(properties._doc_dir(), os.path.basename(d["path"]))
    return (dict(d), p) if os.path.exists(p) else None


def draft_request(property_id: int, user_id) -> int:
    """Email from the PM (or whoever clicks) to Foresite's insurance agent asking for this property's certificate."""
    from . import emails
    ag = agent()
    with tx() as c:
        prop = dict(c.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone())
        me = dict(c.execute("SELECT id, name, email FROM users WHERE id=?", (user_id,)).fetchone())
        st = status(c, property_id)
    first = ag["name"].split()[0] if ag["name"] else ""
    why = {"expired": f"Our certificate on file for this property expired on {st.get('expires')}.",
           "expiring": f"Our certificate on file for this property expires on {st.get('expires')}.",
           "not_named": "The certificate on file doesn't name the owner as additional insured.",
           "gaps": f"The certificate on file doesn't meet the management agreement's requirements: {st.get('gaps') or ''}"}.get(st["state"], "")
    body = f"""Hi {first or 'there'},

Could you please issue a certificate of insurance for Foresite Real Estate, Inc. for {emails._where(prop)}? {why}

• Additional insured: {prop.get('client') or 'the property owner'}, and their respective officers, employees and agents
• Certificate holder: {prop.get('client') or 'the property owner'}
• Property address in the Description of Operations
• Please include the additional insured endorsements

Coverage the management agreement requires:
{prop.get('our_coi_requirements') or '(please match our standard management-agreement coverage)'}

Please send it to me by {emails._due()}.

Thank you,
{me['name']}
Foresite Real Estate
{me['email']}"""
    return emails.create(me["id"], "our_coi", None, property_id, None,
                         {"to_email": ag["email"] or None, "to_name": ag["name"] or ag["company"] or None,
                          "subject": f"Certificate of insurance request – {prop['name']}", "body": body}, notify=False)
