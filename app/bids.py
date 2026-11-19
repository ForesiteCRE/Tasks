"""Project bids, the property manager's recommendation, and owner approval.

Bids: upload a vendor's bid (PDF/photo) to a project; Claude reads the total, scope, exclusions, timeline and
warranty and matches the vendor to the vendor list. Each bid shows the vendor's ★ rating / $ level / warnings.

Owner approval: the PM picks the recommended bid and writes why (Claude can draft it). "Request owner approval"
creates a secret approval link and drafts an email from the PM to the owner contact (bids attached when it goes
into Gmail). The owner approves / picks another bid / asks for changes / declines on that page — no login.
The decision is recorded, the PM is told on Blink, the winning vendor is added to the project's vendors.
"""
import json
import logging
import os
import secrets
from datetime import date, datetime, timedelta

from .db import rows, tx

log = logging.getLogger("bids")
APPROVAL_DAYS = 60


def init():
    with tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS project_bids(
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, vendor_id INTEGER, vendor_name TEXT, amount REAL,
            scope TEXT, exclusions TEXT, timeline TEXT, warranty TEXT, notes TEXT, submitted_on TEXT,
            filename TEXT, path TEXT, content_type TEXT, status TEXT NOT NULL DEFAULT 'received',
            uploaded_by INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        c.execute("""CREATE TABLE IF NOT EXISTS project_approvals(
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, token TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending',
            recommended_bid_id INTEGER, recommendation TEXT, bid_ids TEXT, owner_name TEXT, owner_email TEXT,
            requested_by INTEGER, requested_at TEXT NOT NULL DEFAULT (datetime('now')), expires_at TEXT,
            decided_at TEXT, decided_by TEXT, decision_bid_id INTEGER, decision_note TEXT, email_draft_id INTEGER)""")
        cols = {r[1] for r in c.execute("PRAGMA table_info(projects)")}
        for col, typ in [("recommended_bid_id", "INTEGER"), ("recommendation", "TEXT")]:
            if col not in cols:
                c.execute(f"ALTER TABLE projects ADD COLUMN {col} {typ}")


# ---------- files ----------

def _dir():
    from .properties import _doc_dir
    return _doc_dir()


def _save_file(project_id: int, data: bytes, filename: str) -> str:
    ext = os.path.splitext(filename or "")[1][:8].lower() or ".pdf"
    name = f"bid-{project_id}-{secrets.token_hex(5)}{ext}"
    with open(os.path.join(_dir(), name), "wb") as f:
        f.write(data)
    return name


def bid_file(bid_id: int):
    with tx() as c:
        b = c.execute("SELECT * FROM project_bids WHERE id=?", (bid_id,)).fetchone()
    if not b or not b["path"]:
        return None
    p = os.path.join(_dir(), os.path.basename(b["path"]))
    return (dict(b), p) if os.path.exists(p) else None


# ---------- reading a bid ----------

BID_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["vendor_name", "amount", "scope", "exclusions", "timeline", "warranty", "submitted_on"],
    "properties": {
        "vendor_name": {"type": "string", "description": "the company submitting the bid"},
        "amount": {"type": "number", "description": "total bid price in dollars (base bid, excluding optional alternates); 0 if not shown"},
        "scope": {"type": "string", "description": "one or two lines: what work is included"},
        "exclusions": {"type": "string", "description": "notable exclusions / assumptions / alternates, short; empty if none"},
        "timeline": {"type": "string", "description": "start / duration if stated; empty if not"},
        "warranty": {"type": "string", "description": "warranty terms if stated; empty if not"},
        "submitted_on": {"type": "string", "description": "bid date YYYY-MM-DD if shown, else empty"},
    },
}


def read_bid(data: bytes, content_type: str, filename: str) -> dict | None:
    from . import intake
    if not intake.ai_enabled():
        return None
    import anthropic
    try:
        resp = anthropic.Anthropic().beta.messages.create(
            model=intake.MODEL, max_tokens=2000,
            system="You read contractor/vendor bids and proposals for a commercial property manager and pull out the facts needed to compare bids.",
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": BID_SCHEMA}},
            messages=[{"role": "user", "content": [intake.document_block(data, content_type, filename),
                                                   {"type": "text", "text": "Read this bid."}]}])
        if resp.stop_reason != "end_turn":
            return None
        return json.loads(next(b.text for b in resp.content if b.type == "text"))
    except Exception:
        log.exception("bid read failed")
        return None


def match_vendor(name: str | None) -> int | None:
    n = "".join(ch for ch in (name or "").lower() if ch.isalnum() or ch == " ").strip()
    if not n:
        return None
    strip = lambda s: " ".join(w for w in s.split() if w not in ("inc", "llc", "co", "corp", "company", "the", "ltd", "lp"))
    with tx() as c:
        vs = [(r["id"], "".join(ch for ch in r["name"].lower() if ch.isalnum() or ch == " ")) for r in c.execute(
            "SELECT id, name FROM vendors WHERE archived=0")]
    for vid, vn in vs:
        if strip(vn) == strip(n):
            return vid
    for vid, vn in vs:
        if strip(vn) and (strip(vn) in strip(n) or strip(n) in strip(vn)):
            return vid
    return None


# ---------- bids ----------

def add_bid(project_id: int, fields: dict, user_id=None, file: tuple | None = None) -> dict:
    """file = (bytes, filename, content_type). Returns the bid row plus what was read from the file."""
    read = None
    path = None
    if file:
        data, fn, ct = file
        path = _save_file(project_id, data, fn)
        read = read_bid(data, ct, fn)
    f = dict(fields)
    if read:   # fill only what the person didn't type
        for k in ("scope", "exclusions", "timeline", "warranty"):
            if not f.get(k) and read.get(k):
                f[k] = read[k]
        if not f.get("amount") and read.get("amount"):
            f["amount"] = read["amount"]
        if not f.get("vendor_id"):
            f["vendor_id"] = match_vendor(read.get("vendor_name"))
            if not f["vendor_id"] and read.get("vendor_name"):
                f["vendor_name"] = read["vendor_name"]
        if not f.get("submitted_on") and read.get("submitted_on"):
            f["submitted_on"] = read["submitted_on"][:10]
    if not f.get("vendor_id") and not (f.get("vendor_name") or "").strip():
        raise ValueError("Pick the vendor who submitted this bid.")
    amt = f.get("amount")
    try:
        amt = float(str(amt).replace("$", "").replace(",", "")) if amt not in (None, "") else None
    except ValueError:
        raise ValueError("Amount must be a number.")
    with tx() as c:
        bid_id = c.execute("""INSERT INTO project_bids(project_id, vendor_id, vendor_name, amount, scope, exclusions, timeline,
                                  warranty, notes, submitted_on, filename, path, content_type, uploaded_by)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (project_id, f.get("vendor_id") or None, (f.get("vendor_name") or "").strip() or None, amt,
                            f.get("scope"), f.get("exclusions"), f.get("timeline"), f.get("warranty"), f.get("notes"),
                            f.get("submitted_on") or date.today().isoformat(), file[1] if file else None, path,
                            file[2] if file else None, user_id)).lastrowid
    return {"id": bid_id, "read": read}


def update_bid(bid_id: int, fields: dict):
    sets = {k: fields[k] for k in ("vendor_id", "vendor_name", "amount", "scope", "exclusions", "timeline", "warranty",
                                   "notes", "submitted_on", "status") if k in fields}
    if "status" in sets and sets["status"] not in ("received", "selected", "declined"):
        sets.pop("status")
    if sets:
        with tx() as c:
            c.execute(f"UPDATE project_bids SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), bid_id))


def delete_bid(bid_id: int):
    with tx() as c:
        b = c.execute("SELECT path, project_id FROM project_bids WHERE id=?", (bid_id,)).fetchone()
        c.execute("DELETE FROM project_bids WHERE id=?", (bid_id,))
        if b:
            c.execute("UPDATE projects SET recommended_bid_id=NULL WHERE id=? AND recommended_bid_id=?", (b["project_id"], bid_id))
    if b and b["path"]:
        try:
            os.remove(os.path.join(_dir(), os.path.basename(b["path"])))
        except OSError:
            pass


def project_bids(project_id: int) -> dict:
    from .vendors import _stats
    with tx() as c:
        p = c.execute("""SELECT pj.recommended_bid_id, pj.recommendation, pr.spend_limit, pr.id AS property_id
                         FROM projects pj LEFT JOIN properties pr ON pr.id=pj.property_id WHERE pj.id=?""", (project_id,)).fetchone()
        bs = rows(c.execute("""SELECT b.*, COALESCE(v.name, b.vendor_name) AS vendor_label, u.name AS uploaded_by_name
                               FROM project_bids b LEFT JOIN vendors v ON v.id=b.vendor_id LEFT JOIN users u ON u.id=b.uploaded_by
                               WHERE b.project_id=? ORDER BY b.amount IS NULL, b.amount, b.id""", (project_id,)))
        for b in bs:
            if b["vendor_id"]:
                b.update({k: v for k, v in _stats(c, b["vendor_id"]).items()
                          if k in ("avg", "reviews", "badge", "badge_label", "warning", "price", "price_label")})
        approvals = rows(c.execute("""SELECT a.*, u.name AS requested_by_name FROM project_approvals a
                                      LEFT JOIN users u ON u.id=a.requested_by WHERE a.project_id=? ORDER BY a.id DESC""", (project_id,)))
    url = os.getenv("PUBLIC_URL", "").rstrip("/")
    for a in approvals:
        a["link"] = f"{url}/approve/{a['token']}"
    rec = next((b for b in bs if b["id"] == (p["recommended_bid_id"] if p else None)), None)
    limit = p["spend_limit"] if p else None
    return {"bids": bs, "recommended_bid_id": p["recommended_bid_id"] if p else None, "recommendation": p["recommendation"] if p else None,
            "spend_limit": limit, "over_limit": bool(rec and limit and rec["amount"] and rec["amount"] > limit),
            "approvals": approvals, "low": min((b["amount"] for b in bs if b["amount"]), default=None)}


def set_recommendation(project_id: int, bid_id, text: str | None):
    with tx() as c:
        if bid_id is not None:
            c.execute("UPDATE projects SET recommended_bid_id=? WHERE id=?", (bid_id or None, project_id))
        if text is not None:
            c.execute("UPDATE projects SET recommendation=? WHERE id=?", (text.strip() or None, project_id))


def _money(n) -> str:
    return f"${n:,.0f}" if n is not None else "—"


def draft_recommendation(project_id: int) -> str:
    """Claude writes the PM's recommendation from the bids + vendor track record (falls back to a plain summary)."""
    info = project_bids(project_id)
    with tx() as c:
        pj = dict(c.execute("SELECT p.name, pr.name AS property, pr.client FROM projects p LEFT JOIN properties pr ON pr.id=p.property_id WHERE p.id=?",
                            (project_id,)).fetchone())
    bids = info["bids"]
    if not bids:
        raise ValueError("Add at least one bid first.")
    lines = [f"- {b['vendor_label']}: {_money(b['amount'])}; scope: {b['scope'] or '—'}; exclusions: {b['exclusions'] or 'none listed'}; "
             f"timeline: {b['timeline'] or '—'}; warranty: {b['warranty'] or '—'}; team rating: "
             f"{(str(b['avg']) + '★ from ' + str(b['reviews']) + ' reviews') if b.get('reviews') else 'no ratings yet'}"
             f"{'; WARNING: ' + b['warning'] if b.get('warning') and b.get('badge') in ('bad', 'warn') else ''}"
             f"{' (marked as recommended)' if b['id'] == info['recommended_bid_id'] else ''}" for b in bids]
    from . import intake
    if intake.ai_enabled():
        import anthropic
        try:
            resp = anthropic.Anthropic().beta.messages.create(
                model=intake.MODEL, max_tokens=900, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                output_config={"effort": "low"},
                system=("You are a commercial property manager at Foresite Real Estate writing a short recommendation to the property "
                        "owner on which bid to accept. Plain, professional, 4–7 sentences, no headings or bullet points. Compare price, "
                        "scope differences/exclusions, timeline, warranty and the vendor's track record with our team. Recommend one bid "
                        "(the one marked as recommended, if any) and say why; mention anything the owner should know. Don't invent facts."),
                messages=[{"role": "user", "content": f"Project: {pj['name']} at {pj.get('property') or 'the property'} "
                                                      f"(owner: {pj.get('client') or 'unknown'}).\nBids:\n" + "\n".join(lines)}])
            text = next((b.text for b in resp.content if b.type == "text"), "").strip()
            if text:
                return text
        except Exception:
            log.exception("recommendation draft failed")
    rec = next((b for b in bids if b["id"] == info["recommended_bid_id"]), None) or min(
        (b for b in bids if b["amount"]), key=lambda b: b["amount"], default=bids[0])
    return (f"We received {len(bids)} bid{'s' if len(bids) != 1 else ''} for {pj['name']}. We recommend {rec['vendor_label']} at "
            f"{_money(rec['amount'])}" + (f" ({rec['scope']})" if rec.get("scope") else "") + ". "
            + ("They have a strong track record with our team. " if rec.get("badge") == "good" else "")
            + "Please let us know if you approve so we can schedule the work.")


# ---------- owner approval ----------

def request_approval(project_id: int, user_id, owner_name: str | None = None, owner_email: str | None = None) -> dict:
    info = project_bids(project_id)
    if not info["bids"]:
        raise ValueError("Add the bids first.")
    if not info["recommended_bid_id"]:
        raise ValueError("Mark which bid you recommend first.")
    with tx() as c:
        pj = dict(c.execute("""SELECT p.id, p.name, p.property_id, pr.name AS property, pr.address, pr.client
                               FROM projects p LEFT JOIN properties pr ON pr.id=p.property_id WHERE p.id=?""", (project_id,)).fetchone())
        if not owner_email and pj["property_id"]:
            oc = c.execute("""SELECT name, email FROM property_contacts WHERE property_id=? AND role IN ('Owner contact','Asset manager')
                              AND email IS NOT NULL ORDER BY role='Owner contact' DESC, id LIMIT 1""", (pj["property_id"],)).fetchone()
            if oc:
                owner_name, owner_email = owner_name or oc["name"], oc["email"]
        # a new request replaces any still-pending one
        c.execute("UPDATE project_approvals SET status='superseded' WHERE project_id=? AND status='pending'", (project_id,))
        token = secrets.token_urlsafe(24)
        aid = c.execute("""INSERT INTO project_approvals(project_id, token, recommended_bid_id, recommendation, bid_ids, owner_name,
                               owner_email, requested_by, expires_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (project_id, token, info["recommended_bid_id"], info["recommendation"], json.dumps([b["id"] for b in info["bids"]]),
                         owner_name, owner_email, user_id, (date.today() + timedelta(days=APPROVAL_DAYS)).isoformat())).lastrowid
        me = dict(c.execute("SELECT id, name, email FROM users WHERE id=?", (user_id,)).fetchone())
    link = f"{os.getenv('PUBLIC_URL', '').rstrip('/')}/approve/{token}"
    rec = next(b for b in info["bids"] if b["id"] == info["recommended_bid_id"])
    table = "\n".join(f"• {b['vendor_label']}: {_money(b['amount'])}{' — recommended' if b['id'] == rec['id'] else ''}"
                      + (f" ({b['scope']})" if b.get("scope") else "") for b in info["bids"])
    first = (owner_name or "").split()[0] if owner_name and " " in owner_name else (owner_name or "")
    body = f"""Hi {first or 'there'},

We've collected bids for {pj['name']}{' at ' + pj['property'] if pj.get('property') else ''} and need your approval to proceed.

Bids received:
{table}

Our recommendation: {rec['vendor_label']} at {_money(rec['amount'])}.
{info['recommendation'] or ''}

You can review the bids and approve (or choose a different bid / ask for changes) here — no login needed:
{link}

Each bid can be downloaded from that page too. Please let me know if you have any questions.

Thank you,
{me['name']}
Property Manager | Foresite Real Estate
{me['email']}"""
    from . import emails
    files = [{"path": os.path.basename(got[0]["path"]), "filename": got[0]["filename"] or f"bid-{b['id']}.pdf",
              "ctype": got[0]["content_type"] or "application/pdf"} for b in info["bids"] if (got := bid_file(b["id"]))]
    did = emails.create(user_id, "owner_approval", None, pj.get("property_id"), None,
                        {"to_email": owner_email, "to_name": owner_name,
                         "subject": f"Approval needed: {pj['name']} — recommend {rec['vendor_label']} ({_money(rec['amount'])})",
                         "body": body}, notify=False, attachments=files)
    with tx() as c:
        c.execute("UPDATE project_approvals SET email_draft_id=? WHERE id=?", (did, aid))
    return {"id": aid, "link": link, "email_draft_id": did, "in_gmail": bool(emails.get(did) and emails.get(did)["gmail_draft_id"])}


def by_token(token: str) -> dict | None:
    with tx() as c:
        a = c.execute("SELECT * FROM project_approvals WHERE token=?", (token,)).fetchone()
        if not a:
            return None
        a = dict(a)
        pj = dict(c.execute("""SELECT p.id, p.name, pr.name AS property, pr.address, pr.client, pr.spend_limit
                               FROM projects p LEFT JOIN properties pr ON pr.id=p.property_id WHERE p.id=?""", (a["project_id"],)).fetchone())
        ids = json.loads(a["bid_ids"] or "[]")
        bids = rows(c.execute(f"""SELECT b.id, COALESCE(v.name, b.vendor_name) AS vendor, b.amount, b.scope, b.exclusions, b.timeline,
                                         b.warranty, b.filename, b.path FROM project_bids b LEFT JOIN vendors v ON v.id=b.vendor_id
                                  WHERE b.id IN ({','.join('?' * len(ids)) or 'NULL'}) ORDER BY b.amount""", ids)) if ids else []
        req = c.execute("SELECT name, email FROM users WHERE id=?", (a["requested_by"],)).fetchone()
    a.update({"project": pj, "bids": bids, "requested_by_name": req["name"] if req else "", "requested_by_email": req["email"] if req else ""})
    a["expired"] = a["status"] == "pending" and a["expires_at"] and a["expires_at"] < date.today().isoformat()
    return a


def decide(token: str, decision: str, bid_id: int | None, name: str, note: str | None) -> dict:
    """Owner's answer from the approval page."""
    a = by_token(token)
    if not a:
        raise ValueError("This approval link isn't valid.")
    if a["status"] != "pending" or a["expired"]:
        raise ValueError("This request has already been answered or has expired.")
    if decision not in ("approved", "changes_requested", "declined"):
        raise ValueError("Unknown decision.")
    if not (name or "").strip():
        raise ValueError("Please add your name.")
    if decision == "approved":
        bid_id = bid_id or a["recommended_bid_id"]
        if bid_id not in [b["id"] for b in a["bids"]]:
            raise ValueError("Pick one of the bids.")
    else:
        bid_id = None
    with tx() as c:
        c.execute("""UPDATE project_approvals SET status=?, decided_at=datetime('now'), decided_by=?, decision_bid_id=?, decision_note=?
                     WHERE id=?""", (decision, name.strip()[:120], bid_id, (note or "").strip()[:2000] or None, a["id"]))
        if decision == "approved":
            c.execute("UPDATE project_bids SET status=CASE WHEN id=? THEN 'selected' ELSE 'declined' END WHERE project_id=?",
                      (bid_id, a["project_id"]))
            win = c.execute("SELECT vendor_id FROM project_bids WHERE id=?", (bid_id,)).fetchone()
        req = c.execute("SELECT blink_user_id FROM users WHERE id=?", (a["requested_by"],)).fetchone()
    winner = next((b for b in a["bids"] if b["id"] == bid_id), None)
    if decision == "approved" and win and win["vendor_id"]:
        from . import vendors
        vendors.add_project_vendor(a["project_id"], win["vendor_id"], None, f"Approved bid {_money(winner['amount'])}", None)
    from . import audit, automations
    label = {"approved": "✅ approved", "changes_requested": "✏️ asked for changes on", "declined": "❌ declined"}[decision]
    text = (f"{label.split(' ', 1)[0]} **Owner {label.split(' ', 1)[1]} the bids** for *{a['project']['name']}* — {name.strip()}"
            + (f"\nApproved: {winner['vendor']} at {_money(winner['amount'])}" if winner else "")
            + (f"\n“{note.strip()}”" if note and note.strip() else ""))
    if req and req["blink_user_id"]:
        automations._send([req["blink_user_id"]], text)
    audit.log(None, f"owner {decision.replace('_', ' ')}", "project", a["project_id"], a["project"]["name"],
              f"{name.strip()}" + (f": {winner['vendor']} {_money(winner['amount'])}" if winner else "") + (f" — {note.strip()}" if note else ""))
    return {"status": decision, "bid": winner}
