"""Insurance follow-up emails, drafted for the property manager.

When a vendor's certificate of insurance (COI) for a property is missing / expired / expiring / doesn't name the
property owner as additional insured — or the property's own policy is missing, expired or doesn't name
Foresite — an email is drafted FROM the property's Property Manager (to the vendor, or to the property's
insurance agent) and the PM gets a Blink message that it's ready to send.

Where the draft lives:
  • the PM's own Gmail Drafts folder, if they've clicked "Connect Gmail" (Gmail API, gmail.compose scope), and
  • always in the app (✉️ Emails to send) with an "Open in Gmail" pre-filled compose link.
Each issue is drafted once (issue_key); fixing the issue marks its open draft resolved.
"""
import base64
import hashlib
import json
import logging
import os
from datetime import date, timedelta
from email.message import EmailMessage
from urllib.parse import quote

import httpx

from .db import rows, tx

log = logging.getLogger("emails")
FORESITE = "Foresite Real Estate, Inc."
FORESITE_ADDR = "777 E Sonterra Blvd, Suite 150, San Antonio, TX 78258"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.compose"


def init():
    with tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS email_drafts(
            id INTEGER PRIMARY KEY, user_id INTEGER, kind TEXT NOT NULL, issue_key TEXT UNIQUE, property_id INTEGER,
            vendor_id INTEGER, to_email TEXT, to_name TEXT, subject TEXT NOT NULL, body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', gmail_draft_id TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT)""")
        if "attachments" not in {r[1] for r in c.execute("PRAGMA table_info(email_drafts)")}:
            c.execute("ALTER TABLE email_drafts ADD COLUMN attachments TEXT")   # [{path, filename, ctype}] in the docs folder
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        if "gmail_token" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN gmail_token TEXT")
        if "gmail_connected_at" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN gmail_connected_at TEXT")


# ---------- token storage (encrypted with a key derived from SESSION_SECRET) ----------

def _fernet():
    from cryptography.fernet import Fernet
    key = hashlib.sha256(("gmail:" + os.getenv("SESSION_SECRET", "dev")).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def save_gmail_token(user_id: int, refresh_token: str):
    with tx() as c:
        c.execute("UPDATE users SET gmail_token=?, gmail_connected_at=datetime('now') WHERE id=?",
                  (_fernet().encrypt(refresh_token.encode()).decode(), user_id))


def disconnect_gmail(user_id: int):
    with tx() as c:
        c.execute("UPDATE users SET gmail_token=NULL, gmail_connected_at=NULL WHERE id=?", (user_id,))


def gmail_connected(user_id) -> bool:
    if not user_id:
        return False
    with tx() as c:
        r = c.execute("SELECT gmail_token FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(r and r["gmail_token"])


def _access_token(user_id: int) -> str | None:
    with tx() as c:
        r = c.execute("SELECT gmail_token FROM users WHERE id=?", (user_id,)).fetchone()
    if not r or not r["gmail_token"]:
        return None
    try:
        refresh = _fernet().decrypt(r["gmail_token"].encode()).decode()
    except Exception:
        return None
    resp = httpx.post("https://oauth2.googleapis.com/token", timeout=15, data={
        "client_id": os.getenv("GOOGLE_CLIENT_ID", ""), "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        "refresh_token": refresh, "grant_type": "refresh_token"})
    if resp.status_code != 200:
        log.warning("Gmail token refresh failed for user %s: %s", user_id, resp.text[:200])
        if resp.status_code in (400, 401):   # revoked → forget it, fall back to in-app drafts
            disconnect_gmail(user_id)
        return None
    return resp.json().get("access_token")


def _mime(d: dict, from_email: str | None) -> str:
    m = EmailMessage()
    if d.get("to_email"):
        m["To"] = f'"{d["to_name"]}" <{d["to_email"]}>' if d.get("to_name") else d["to_email"]
    if from_email:
        m["From"] = from_email
    m["Subject"] = d["subject"]
    m.set_content(d["body"])
    from .properties import _doc_dir
    for a in json.loads(d.get("attachments") or "[]"):
        p = os.path.join(_doc_dir(), os.path.basename(a["path"]))
        if os.path.exists(p):
            mt, _, st = (a.get("ctype") or "application/octet-stream").partition("/")
            with open(p, "rb") as fh:
                m.add_attachment(fh.read(), maintype=mt, subtype=st or "octet-stream", filename=a["filename"])
    return base64.urlsafe_b64encode(m.as_bytes()).decode()


def _push_to_gmail(draft_id: int) -> bool:
    """Create or update the matching draft in the owner's Gmail. Returns True if it's in Gmail."""
    with tx() as c:
        d = c.execute("SELECT e.*, u.email AS from_email FROM email_drafts e LEFT JOIN users u ON u.id=e.user_id WHERE e.id=?",
                      (draft_id,)).fetchone()
    if not d or not d["user_id"]:
        return False
    d = dict(d)
    tok = _access_token(d["user_id"])
    if not tok:
        return False
    body = {"message": {"raw": _mime(d, d["from_email"])}}
    url = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
    try:
        if d["gmail_draft_id"]:
            r = httpx.put(f"{url}/{d['gmail_draft_id']}", json=body, headers={"Authorization": f"Bearer {tok}"}, timeout=20)
            if r.status_code == 404:   # they deleted/sent it in Gmail → make a fresh one
                r = httpx.post(url, json=body, headers={"Authorization": f"Bearer {tok}"}, timeout=20)
        else:
            r = httpx.post(url, json=body, headers={"Authorization": f"Bearer {tok}"}, timeout=20)
    except httpx.HTTPError as e:
        log.warning("Gmail draft failed: %s", e)
        return False
    if r.status_code not in (200, 201):
        log.warning("Gmail draft failed (%s): %s", r.status_code, r.text[:200])
        return False
    with tx() as c:
        c.execute("UPDATE email_drafts SET gmail_draft_id=? WHERE id=?", (r.json().get("id"), draft_id))
    return True


# ---------- composing ----------

def _first(name: str | None) -> str:
    n = (name or "").strip()
    return n.split()[0] if n and " " in n and not n.lower().startswith(("commercial", "loan", "property")) else ""


def _due(business_days: int = 7) -> str:
    d = date.today()
    while business_days:
        d += timedelta(days=1)
        business_days -= d.weekday() < 5
    return d.strftime("%A, %B %d").replace(" 0", " ")


def _where(prop: dict) -> str:
    """'Oak Plaza (123 Main St, …)' — or just the address when the name is the street address."""
    a, n = prop.get("address") or "", prop.get("name") or ""
    return a if not n or a.lower().startswith(n.lower()) else f"{n} ({a})"


def _sign(pm: dict) -> str:
    return f"Thank you,\n{pm['name']}\nProperty Manager | Foresite Real Estate\n{pm.get('email') or ''}".rstrip()


def compose_vendor_coi(prop: dict, vendor: dict, st: dict, pm: dict) -> dict:
    owner = prop.get("client") or "the property owner"
    when = date.fromisoformat(st["expires"]).strftime("%B %d, %Y") if st.get("expires") else ""
    why = {"missing": "We don't have a certificate of insurance on file for your work at this property.",
           "expired": f"The certificate of insurance we have on file for this property expired on {when}.",
           "expiring": f"The certificate of insurance we have on file for this property expires on {when}.",
           "not_named": f"The certificate we have on file doesn't name {owner} as additional insured for this property "
                        f"(being listed only as certificate holder isn't enough)."}.get(st["state"], "We need an updated certificate of insurance.")
    due = _due()
    hi = _first(vendor.get("contact_name"))
    subject = {"missing": "Certificate of insurance needed", "expired": "Expired certificate of insurance",
               "expiring": "Updated certificate of insurance needed", "not_named": "Additional insured needed on your COI"
               }.get(st["state"], "Certificate of insurance") + f" – {prop['name']}"
    body = f"""Hi {hi or vendor['name'] + ' team'},

{why} Each property needs its own certificate, so please have your insurance agent send an ACORD 25 certificate for {_where(prop)} that:

• Names as additional insured: {owner}; {FORESITE}; and their respective officers, employees and agents
• Includes the additional insured endorsement (CG 20 10 / CG 20 37 or a blanket equivalent)
• Lists the property address in the Description of Operations
• Shows current general liability, auto and workers' compensation coverage
• Certificate holder: {owner}, c/o {FORESITE}, {FORESITE_ADDR}

Please send it to me by {due}. We'll need it on file before any further work is scheduled at the property.

{_sign(pm)}"""
    return {"to_email": vendor.get("email"), "to_name": vendor.get("contact_name") or vendor["name"], "subject": subject, "body": body}


def compose_property_insurance(prop: dict, ins: dict, pm: dict, agent: dict | None) -> dict:
    when = date.fromisoformat(ins["expires"]).strftime("%B %d, %Y") if ins.get("expires") else ""
    policy = f" ({ins['note'].split(' · ')[0]})" if ins.get("note") else ""
    ask = {"not_named": f"The current property policy{policy} doesn't name {FORESITE} as additional insured. Please add us "
                        f"and send the additional insured endorsement along with an updated certificate.",
           "expired": f"The property policy we have on file expired on {when}. Please send the renewal policy and an updated "
                      f"certificate naming {FORESITE} as additional insured.",
           "missing": f"We don't have the property insurance policy on file. Please send the current policy (declarations "
                      f"and endorsements) and a certificate naming {FORESITE} as additional insured.",
           "unverified": f"Please confirm {FORESITE} is named as additional insured on the property policy{policy} and send the "
                         f"endorsement."}.get(ins["state"], "Please send the current property insurance.")
    hi = _first((agent or {}).get("name"))
    body = f"""Hi {hi or 'there'},

I'm the property manager for {_where(prop)}{', owned by ' + prop['client'] if prop.get('client') else ''}.

{ask}

Please send everything to me by {_due()}.

{_sign(pm)}"""
    return {"to_email": (agent or {}).get("email"), "to_name": (agent or {}).get("name") or (agent or {}).get("company"),
            "subject": f"Property insurance – {prop['name']}" + (" – additional insured" if ins["state"] == "not_named" else ""),
            "body": body}


# ---------- create / notify ----------

def _pm(c, property_id):
    from .properties import property_manager
    pm = property_manager(c, property_id)
    if pm:
        pm["email"] = c.execute("SELECT email FROM users WHERE id=?", (pm["id"],)).fetchone()["email"]
    return pm


def create(user_id, kind: str, issue_key: str | None, property_id, vendor_id, msg: dict, notify: bool = True,
           attachments: list | None = None) -> int | None:
    """Store the draft (once per issue_key), put it in the owner's Gmail if connected, and tell them on Blink."""
    with tx() as c:
        if issue_key and c.execute("SELECT 1 FROM email_drafts WHERE issue_key=?", (issue_key,)).fetchone():
            return None
        did = c.execute("""INSERT INTO email_drafts(user_id, kind, issue_key, property_id, vendor_id, to_email, to_name, subject, body,
                                                    attachments) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (user_id, kind, issue_key, property_id, vendor_id, msg.get("to_email"), msg.get("to_name"), msg["subject"],
                         msg["body"], json.dumps(attachments) if attachments else None)).lastrowid
        u = c.execute("SELECT blink_user_id FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None
    in_gmail = _push_to_gmail(did)
    if notify and u and u["blink_user_id"]:
        from . import automations
        url = os.getenv("PUBLIC_URL", "").rstrip("/")
        where = ("It's in your **Gmail drafts**, ready to send." if in_gmail else
                 f"Review and send it here: {url}/#emails" if url else "Review and send it under Tasks → ✉️ Emails to send.")
        automations._send([u["blink_user_id"]], f"✉️ **Email drafted:** {msg['subject']}\nTo: {msg.get('to_name') or ''} "
                                                f"{('<' + msg['to_email'] + '>') if msg.get('to_email') else '(add their email)'}\n{where}")
    return did


def draft_for_vendor(property_id: int, vendor_id: int, user_id=None, manual: bool = False) -> int | None:
    from . import vendors
    with tx() as c:
        prop = dict(c.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone())
        v = dict(c.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone())
        st = vendors.coi_status(c, property_id, vendor_id)
        pm = _pm(c, property_id)
        if not pm and user_id:
            me = c.execute("SELECT id, name, email FROM users WHERE id=?", (user_id,)).fetchone()
            pm = dict(me) if me else None
    if not pm or (st["state"] == "ok" and not manual):
        return None
    key = None if manual else f"coi:{property_id}:{vendor_id}:{st['state']}:{st.get('coi_id') or 0}:{st.get('expires') or ''}"
    return create(pm["id"] if not manual else (user_id or pm["id"]), "vendor_coi", key, property_id, vendor_id,
                  compose_vendor_coi(prop, v, st if st["state"] != "ok" else {**st, "state": "expiring"}, pm),
                  notify=not manual)


def draft_for_property(property_id: int, user_id=None, manual: bool = False) -> int | None:
    from . import properties
    with tx() as c:
        prop = dict(c.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone())
        ins = properties._insurance_status(c, property_id)
        pm = _pm(c, property_id)
        if not pm and user_id:
            me = c.execute("SELECT id, name, email FROM users WHERE id=?", (user_id,)).fetchone()
            pm = dict(me) if me else None
        agent = c.execute("""SELECT * FROM property_contacts WHERE property_id=? AND role IN ('Insurance agent','Owner contact')
                             ORDER BY role='Insurance agent' DESC, id LIMIT 1""", (property_id,)).fetchone()
    if not pm or (ins["state"] == "ok" and not manual):
        return None
    key = None if manual else f"ins:{property_id}:{ins['state']}:{ins.get('doc_id') or 0}"
    return create(pm["id"] if not manual else (user_id or pm["id"]), "property_insurance", key, property_id, None,
                  compose_property_insurance(prop, ins, pm, dict(agent) if agent else None), notify=not manual)


def run(today: date | None = None) -> int:
    """Daily: draft emails for insurance problems that don't have one yet; close drafts whose issue is fixed."""
    from . import vendors
    n = 0
    with tx() as c:
        pairs = [tuple(r) for r in c.execute("""SELECT DISTINCT property_id, vendor_id FROM property_vendors
                                                WHERE status='current' AND trade NOT LIKE 'Utilities%'""")]
        props = [r[0] for r in c.execute("""SELECT DISTINCT p.id FROM properties p JOIN property_team pt ON pt.property_id=p.id
                                            JOIN project_roles r ON r.id=pt.role_id WHERE r.name='Property Manager'""")]
        # resolved issues: COI now OK / property insurance OK → close their open auto drafts
        for d in rows(c.execute("SELECT id, kind, property_id, vendor_id FROM email_drafts WHERE status='open' AND issue_key IS NOT NULL")):
            if d["kind"] == "vendor_coi" and vendors.coi_status(c, d["property_id"], d["vendor_id"])["state"] == "ok":
                c.execute("UPDATE email_drafts SET status='resolved', updated_at=datetime('now') WHERE id=?", (d["id"],))
    for pid, vid in pairs:
        n += bool(draft_for_vendor(pid, vid))
    for pid in props:
        n += bool(draft_for_property(pid))
    return n


# ---------- in-app list / edit ----------

def gmail_compose_url(d: dict) -> str:
    return ("https://mail.google.com/mail/?view=cm&fs=1" + (f"&to={quote(d['to_email'])}" if d.get("to_email") else "")
            + f"&su={quote(d['subject'])}&body={quote(d['body'])}")


def list_drafts(user_id=None, include_closed: bool = False) -> list[dict]:
    q = """SELECT e.*, p.name AS property_name, v.name AS vendor_name, u.name AS owner_name,
                  (u.gmail_token IS NOT NULL) AS owner_gmail
           FROM email_drafts e LEFT JOIN properties p ON p.id=e.property_id LEFT JOIN vendors v ON v.id=e.vendor_id
           LEFT JOIN users u ON u.id=e.user_id WHERE 1=1"""
    args = []
    if not include_closed:
        q += " AND e.status='open'"
    if user_id:
        q += " AND e.user_id=?"
        args.append(user_id)
    with tx() as c:
        ds = rows(c.execute(q + " ORDER BY e.status='open' DESC, e.id DESC LIMIT 200", args))
    for d in ds:
        d["compose_url"] = gmail_compose_url(d)
    return ds


def get(draft_id: int) -> dict | None:
    with tx() as c:
        d = c.execute("SELECT * FROM email_drafts WHERE id=?", (draft_id,)).fetchone()
    if not d:
        return None
    d = dict(d)
    d["compose_url"] = gmail_compose_url(d)
    return d


def update(draft_id: int, fields: dict) -> bool:
    sets = {k: fields[k] for k in ("to_email", "to_name", "subject", "body", "status") if k in fields and fields[k] is not None}
    if "status" in sets and sets["status"] not in ("open", "sent", "dismissed", "resolved"):
        sets.pop("status")
    if not sets:
        return False
    with tx() as c:
        c.execute(f"UPDATE email_drafts SET {', '.join(k + '=?' for k in sets)}, updated_at=datetime('now') WHERE id=?",
                  (*sets.values(), draft_id))
    if set(sets) & {"to_email", "to_name", "subject", "body"}:
        return _push_to_gmail(draft_id)
    return False
