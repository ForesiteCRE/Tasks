"""Utility bills: every property's utility accounts, and the monthly "download the bill → email it to AppFolio" routine.

Each account (imported from Accounting's "Utilities" sheet, or added by hand) has a provider, the utility account
number, the AppFolio reference ("323253874 MMYY" → "323253874 1126"), the GL account, a bill description, the day
the bill comes out and the payment day, the person who sends it (default: one person for all properties), and the
portal login. Logins are encrypted in the database and only shown — on request — to managers, the account's
assigned person and the property's Accounting 1/2.

Each month every active account needs a bill sent to AppFolio's invoice inbox. The board shows, per month:
  upcoming – before the bill date      ready – bill is out, not sent yet      late – 5+ days past the bill date
  (or 3 days before payment)           sent / N/A / FINAL / other – handled.
"Send to AppFolio" stores the PDF and emails it from the sender's own Gmail (if connected), then marks it sent.
Reminders: the assigned person gets one Blink message a day listing ready & late bills; the department head (or
managers) hear once per bill when one goes late.
"""
import base64
import calendar
import hashlib
import io
import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from .db import rows, tx

log = logging.getLogger("utilities")
DEFAULT_TO = "foresitecre@invoices.appfolio.com"
LATE_AFTER = 5          # days after the bill date
LATE_BEFORE_PAY = 3     # …or this many days before the payment date, whichever comes first
STATUSES = {"sent": "Sent", "na": "N/A", "final": "Final bill", "other": "Other"}
FIELDS = ("property_id", "property_label", "provider", "account_no", "reference", "gl_account", "description",
          "bill_day", "pay_day", "assignee_id", "login_url", "notes", "active", "freq", "bill_month")


def init():
    with tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS utility_accounts(id INTEGER PRIMARY KEY, property_id INTEGER, property_label TEXT,
                     provider TEXT, account_no TEXT, reference TEXT, gl_account TEXT, description TEXT, bill_day INTEGER,
                     pay_day INTEGER, assignee_id INTEGER, login_user TEXT, login_pass TEXT, login_url TEXT, notes TEXT,
                     active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        c.execute("""CREATE TABLE IF NOT EXISTS utility_bills(account_id INTEGER NOT NULL, period TEXT NOT NULL, status TEXT NOT NULL,
                     note TEXT, file_name TEXT, file_path TEXT, via TEXT, sent_by INTEGER, sent_at TEXT NOT NULL DEFAULT (datetime('now')),
                     PRIMARY KEY(account_id, period))""")
        cols = {r[1] for r in c.execute("PRAGMA table_info(utility_accounts)")}
        if "freq" not in cols:
            c.execute("ALTER TABLE utility_accounts ADD COLUMN freq TEXT NOT NULL DEFAULT 'monthly'")
        if "bill_month" not in cols:
            c.execute("ALTER TABLE utility_accounts ADD COLUMN bill_month INTEGER")
        c.execute("""CREATE TABLE IF NOT EXISTS utility_alerts(account_id INTEGER NOT NULL, period TEXT NOT NULL, PRIMARY KEY(account_id, period))""")


# ---------- settings ----------

def _setting(key, default=None):
    with tx() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r and r[0] not in (None, "") else default


def _set(key, value):
    with tx() as c:
        c.execute("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def settings() -> dict:
    a = _setting("utility.default_assignee")
    return {"to_email": _setting("utility.to_email", DEFAULT_TO), "default_assignee": int(a) if a else None}


def save_settings(d: dict):
    if "to_email" in d:
        e = (d["to_email"] or "").strip()
        if e and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
            raise ValueError("That doesn't look like an email address.")
        _set("utility.to_email", e or DEFAULT_TO)
    if "default_assignee" in d:
        _set("utility.default_assignee", str(int(d["default_assignee"])) if d["default_assignee"] else "")


# ---------- logins (encrypted at rest) ----------

def _fernet():
    from cryptography.fernet import Fernet
    key = hashlib.sha256(("utility-logins:" + os.getenv("SESSION_SECRET", "dev")).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _enc(v):
    v = (v or "").strip()
    return _fernet().encrypt(v.encode()).decode() if v else None


def _dec(v):
    if not v:
        return ""
    try:
        return _fernet().decrypt(v.encode()).decode()
    except Exception:
        return ""


def can_see_login(uid, is_mgr: bool, acct: dict) -> bool:
    if is_mgr or (uid and acct.get("assignee_id") == uid):
        return True
    if not uid or not acct.get("property_id"):
        return False
    with tx() as c:
        return bool(c.execute("""SELECT 1 FROM property_team t JOIN project_roles r ON r.id=t.role_id
                                 WHERE t.property_id=? AND t.user_id=? AND r.name IN ('Accounting 1','Accounting 2')""",
                              (acct["property_id"], uid)).fetchone())


can_work = can_see_login   # the same people can mark bills sent and edit the account


def login(account_id: int) -> dict:
    a = get(account_id)
    with tx() as c:
        r = c.execute("SELECT login_user, login_pass FROM utility_accounts WHERE id=?", (account_id,)).fetchone()
    return {"username": _dec(r["login_user"]), "password": _dec(r["login_pass"]), "url": (a or {}).get("login_url") or ""}


# ---------- accounts ----------

def _clean(d: dict) -> dict:
    out = {}
    for k in FIELDS:
        if k not in d:
            continue
        v = d[k]
        if k in ("bill_day", "pay_day"):
            try:
                v = int(float(v)) if v not in (None, "") else None
            except (TypeError, ValueError):
                v = None
            v = v if v and 1 <= v <= 31 else None
        elif k == "bill_month":
            try:
                v = int(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                v = None
            v = v if v and 1 <= v <= 12 else None
        elif k == "freq":
            v = "annual" if v == "annual" else "monthly"
        elif k in ("property_id", "assignee_id"):
            v = int(v) if v else None
        elif k == "active":
            v = 1 if v else 0
        else:
            v = (str(v).strip() if v is not None else "") or None
        out[k] = v
    return out


def create(d: dict) -> int:
    f = _clean(d)
    if not (f.get("account_no") or f.get("reference") or f.get("provider")):
        raise ValueError("Add at least the provider or the account number.")
    if "assignee_id" not in f:
        f["assignee_id"] = settings()["default_assignee"]
    f["login_user"], f["login_pass"] = _enc(d.get("login_user")), _enc(d.get("login_pass"))
    with tx() as c:
        return c.execute(f"INSERT INTO utility_accounts({', '.join(f)}) VALUES({', '.join('?' * len(f))})", tuple(f.values())).lastrowid


def update(aid: int, d: dict):
    f = _clean(d)
    for k in ("login_user", "login_pass"):
        if k in d:
            f[k] = _enc(d[k])
    if f:
        with tx() as c:
            c.execute(f"UPDATE utility_accounts SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), aid))


def delete(aid: int):
    with tx() as c:
        c.execute("DELETE FROM utility_bills WHERE account_id=?", (aid,))
        c.execute("DELETE FROM utility_alerts WHERE account_id=?", (aid,))
        c.execute("DELETE FROM utility_accounts WHERE id=?", (aid,))


_SELECT = """SELECT a.id, a.property_id, a.property_label, a.provider, a.account_no, a.reference, a.gl_account, a.description,
             a.bill_day, a.pay_day, a.freq, a.bill_month, a.assignee_id, a.login_url, a.notes, a.active,
             (a.login_user IS NOT NULL OR a.login_pass IS NOT NULL) AS has_login,
             p.name AS property_name, u.name AS assignee_name
             FROM utility_accounts a LEFT JOIN properties p ON p.id=a.property_id LEFT JOIN users u ON u.id=a.assignee_id"""


def get(aid: int) -> dict | None:
    with tx() as c:
        r = c.execute(_SELECT + " WHERE a.id=?", (aid,)).fetchone()
    return dict(r) if r else None


# ---------- the month ----------

def cur_period(today: date | None = None) -> str:
    return (today or date.today()).strftime("%Y-%m")


def _day(period: str, d: int | None) -> date:
    y, m = int(period[:4]), int(period[5:7])
    return date(y, m, min(d or 1, calendar.monthrange(y, m)[1]))


def dates(acct: dict, period: str) -> dict:
    bill = _day(period, acct.get("bill_day"))
    pay = None
    if acct.get("pay_day"):
        pay = _day(period, acct["pay_day"])
        if pay < bill:   # paid the following month
            y, m = (bill.year + (bill.month == 12), bill.month % 12 + 1)
            pay = _day(f"{y}-{m:02d}", acct["pay_day"])
    if not acct.get("bill_day"):   # no bill date on file → only late once the month is over
        late = _day(period, 31) + timedelta(days=1)
    else:
        late = bill + timedelta(days=LATE_AFTER)
    if pay and pay - timedelta(days=LATE_BEFORE_PAY) < late:
        late = max(bill, pay - timedelta(days=LATE_BEFORE_PAY))
    return {"bill": bill, "pay": pay, "late": late}


def reference_for(ref: str | None, period: str) -> str:
    """'323253874 MMYY' → '323253874 1126' for Nov 2026; 'YYYY' → '2026' (yearly bills)."""
    r = re.sub(r"MMYYYY", period[5:7] + period[:4], ref or "", flags=re.I)
    r = re.sub(r"MMYY", period[5:7] + period[2:4], r, flags=re.I)
    return re.sub(r"\bYYYY\b", period[:4], r, flags=re.I)


def due_this_month(acct: dict, period: str) -> bool:
    return acct.get("freq") != "annual" or int(period[5:7]) == (acct.get("bill_month") or 1)


def _status(acct: dict, bill: dict | None, period: str, today: date) -> str:
    if bill:
        return bill["status"]
    if not due_this_month(acct, period):
        return "skip"
    d = dates(acct, period)
    if today < d["bill"]:
        return "upcoming"
    return "late" if today >= d["late"] else "ready"


def _months_back(period: str, n: int) -> list[str]:
    y, m = int(period[:4]), int(period[5:7])
    out = []
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def board(period: str | None = None, assignee_id=None, property_id=None, include_inactive=False, today: date | None = None) -> dict:
    today = today or date.today()
    period = period or cur_period(today)
    where, args = [], []
    if not include_inactive:
        where.append("a.active=1")
    if assignee_id:
        where.append("a.assignee_id=?")
        args.append(assignee_id)
    if property_id:
        where.append("a.property_id=?")
        args.append(property_id)
    with tx() as c:
        accts = rows(c.execute(_SELECT + (" WHERE " + " AND ".join(where) if where else "")
                               + " ORDER BY COALESCE(p.name, a.property_label) COLLATE NOCASE, a.provider, a.id", args))
        hist = _months_back(period, 6)
        ids = [a["id"] for a in accts]
        bills = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for b in c.execute(f"""SELECT b.*, u.name AS sent_by_name FROM utility_bills b LEFT JOIN users u ON u.id=b.sent_by
                                   WHERE b.account_id IN ({','.join('?' * len(chunk))}) AND b.period>=?""", (*chunk, hist[0])):
                bills[(b["account_id"], b["period"])] = dict(b)
    counts = {"upcoming": 0, "ready": 0, "late": 0, "done": 0, "total": len(accts)}
    for a in accts:
        b = bills.get((a["id"], period))
        a["status"] = _status(a, b, period, today) if a["active"] or b else "inactive"
        if period < cur_period(today) and a["status"] in ("ready", "upcoming"):
            a["status"] = "late"   # a past month that never got sent
        d = dates(a, period)
        a.update({"bill_date": d["bill"].isoformat(), "pay_date": d["pay"].isoformat() if d["pay"] else None,
                  "late_date": d["late"].isoformat(), "ref": reference_for(a["reference"], period),
                  "bill": {k: b[k] for k in ("status", "note", "file_name", "via", "sent_at", "sent_by_name")} | {"has_file": bool(b["file_path"])} if b else None,
                  "history": [{"period": p, "status": (bills.get((a["id"], p)) or {}).get("status")} for p in hist]})
        a["has_login"] = bool(a["has_login"])
        if a["status"] not in ("inactive", "skip"):
            counts["done" if a["status"] in STATUSES else a["status"]] += 1
    counts["total"] = sum(counts[k] for k in ("upcoming", "ready", "late", "done"))
    return {"period": period, "accounts": accts, "counts": counts, "history_periods": hist, "settings": settings()}


def mark(aid: int, period: str, status: str, note: str | None, user_id, via="manual", file_name=None, file_path=None):
    if status not in STATUSES:
        raise ValueError("Unknown status")
    if not re.fullmatch(r"\d{4}-\d{2}", period or ""):
        raise ValueError("Bad month")
    with tx() as c:
        old = c.execute("SELECT file_name, file_path FROM utility_bills WHERE account_id=? AND period=?", (aid, period)).fetchone()
        if old and not file_path:
            file_name, file_path = old["file_name"], old["file_path"]
        c.execute("""INSERT INTO utility_bills(account_id, period, status, note, file_name, file_path, via, sent_by) VALUES(?,?,?,?,?,?,?,?)
                     ON CONFLICT(account_id, period) DO UPDATE SET status=excluded.status, note=excluded.note, file_name=excluded.file_name,
                     file_path=excluded.file_path, via=excluded.via, sent_by=excluded.sent_by, sent_at=datetime('now')""",
                  (aid, period, status, (note or "").strip() or None, file_name, file_path, via, user_id))
        if status == "final":
            c.execute("UPDATE utility_accounts SET active=0 WHERE id=?", (aid,))


def unmark(aid: int, period: str):
    with tx() as c:
        c.execute("DELETE FROM utility_bills WHERE account_id=? AND period=?", (aid, period))


def _bill_dir():
    from .photos import UPLOAD_DIR
    d = os.path.join(UPLOAD_DIR, "utility-bills")
    os.makedirs(d, exist_ok=True)
    return d


def bill_file(aid: int, period: str):
    with tx() as c:
        r = c.execute("SELECT file_name, file_path FROM utility_bills WHERE account_id=? AND period=?", (aid, period)).fetchone()
    if not r or not r["file_path"]:
        return None
    p = os.path.join(_bill_dir(), os.path.basename(r["file_path"]))
    return (r["file_name"], p) if os.path.exists(p) else None


def email_for(acct: dict, period: str) -> dict:
    ref = reference_for(acct.get("reference"), period)
    month = datetime.strptime(period, "%Y-%m").strftime("%B %Y")
    where = acct.get("property_name") or acct.get("property_label") or ""
    subject = " · ".join(x for x in ["Utility bill", where, acct.get("provider") or "", ref] if x)
    d = dates(acct, period)
    lines = [f"Utility bill for {month}", "",
             f"Property: {where}" if where else "", f"Provider: {acct['provider']}" if acct.get("provider") else "",
             f"Utility account #: {acct['account_no']}" if acct.get("account_no") else "",
             f"Reference / invoice #: {ref}" if ref else "", f"GL account: {acct['gl_account']}" if acct.get("gl_account") else "",
             f"Description: {acct['description']}" if acct.get("description") else "",
             f"Payment date: {d['pay'].strftime('%m/%d/%Y')}" if d["pay"] else ""]
    return {"to": settings()["to_email"], "subject": subject, "body": "\n".join(x for x in lines if x is not None).replace("\n\n\n", "\n\n")}


def send(aid: int, period: str, filename: str, data: bytes, ctype: str, user_id) -> dict:
    """Save the bill, email it to AppFolio from the sender's Gmail, mark it sent. If Gmail isn't connected, the file
    is kept and nothing is marked — the caller offers "Connect Gmail" or "I sent it myself"."""
    from . import emails
    acct = get(aid)
    if not acct:
        raise ValueError("Account not found")
    if not data:
        raise ValueError("Attach the bill first.")
    if len(data) > 20 * 1024 * 1024:
        raise ValueError("That file is over 20 MB.")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "bill.pdf")[-80:] or "bill.pdf"
    stored = f"{aid}-{period}-{uuid.uuid4().hex[:8]}-{safe}"
    with open(os.path.join(_bill_dir(), stored), "wb") as fh:
        fh.write(data)
    msg = email_for(acct, period)
    tok = emails._access_token(user_id) if user_id else None
    if not tok:
        with tx() as c:   # keep the upload so "I sent it myself" can still attach it to the month
            c.execute("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (f"utility.pending.{aid}.{period}", json.dumps({"file_name": filename, "file_path": stored})))
        return {"sent": False, "need_gmail": True, "email": msg}
    with tx() as c:
        me = c.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
    m = EmailMessage()
    m["To"] = msg["to"]
    if me and me["email"]:
        m["From"] = me["email"]
    m["Subject"] = msg["subject"]
    m.set_content(msg["body"])
    mt, _, st = (ctype or "application/pdf").partition("/")
    m.add_attachment(data, maintype=mt or "application", subtype=st or "octet-stream", filename=filename or "bill.pdf")
    import httpx
    try:
        r = httpx.post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", timeout=30,
                       json={"raw": base64.urlsafe_b64encode(m.as_bytes()).decode()}, headers={"Authorization": f"Bearer {tok}"})
    except httpx.HTTPError as e:
        log.warning("utility bill send failed: %s", e)
        raise ValueError("Couldn't reach Gmail — try again in a minute.")
    if r.status_code not in (200, 201):
        log.warning("utility bill send failed (%s): %s", r.status_code, r.text[:200])
        if r.status_code == 403:
            raise ValueError("Gmail didn't allow sending — disconnect and reconnect Gmail on the Emails page, then try again.")
        raise ValueError("Gmail couldn't send it — try again, or send it yourself and click “I sent it”.")
    mark(aid, period, "sent", None, user_id, via="gmail", file_name=filename, file_path=stored)
    return {"sent": True, "email": msg}


def take_pending(aid: int, period: str):
    """A bill uploaded while Gmail wasn't connected → attach it when they mark it sent themselves."""
    key = f"utility.pending.{aid}.{period}"
    v = _setting(key)
    if not v:
        return None, None
    with tx() as c:
        c.execute("DELETE FROM settings WHERE key=?", (key,))
    d = json.loads(v)
    return d.get("file_name"), d.get("file_path")


def summary_for(uid=None, today: date | None = None) -> dict:
    """Home widget: this month's bills for me (or everyone)."""
    b = board(assignee_id=uid, today=today)
    todo = [a for a in b["accounts"] if a["status"] in ("ready", "late")]
    todo.sort(key=lambda a: (a["status"] != "late", a["late_date"]))
    return {"period": b["period"], "counts": b["counts"], "todo": [
        {k: a[k] for k in ("id", "property_id", "property_name", "property_label", "provider", "account_no", "status", "late_date", "bill_date", "ref")}
        for a in todo]}


# ---------- reminders ----------

def run_daily(today: date | None = None) -> int:
    """Once a day: each assigned person gets their list of ready & late bills; a bill that goes late is flagged
    once to the person's department head (or the managers)."""
    from . import departments, notify
    from .auth import PUBLIC_URL
    today = today or date.today()
    if _setting("utility.reminded") == today.isoformat():
        return 0
    _set("utility.reminded", today.isoformat())
    b = board(today=today)
    by_person = {}
    for a in b["accounts"]:
        if a["status"] in ("ready", "late") and a["assignee_id"]:
            by_person.setdefault(a["assignee_id"], []).append(a)
    link = f"{PUBLIC_URL}/#view-utilities"
    sent = 0
    for uid, items in by_person.items():
        items.sort(key=lambda a: (a["status"] != "late", a["property_name"] or a["property_label"] or ""))
        late = sum(a["status"] == "late" for a in items)
        lines = [f"• {a['property_name'] or a['property_label'] or '—'} — {a['provider'] or ''} {a['account_no'] or a['ref']}"
                 + (" ⚠️ late" if a["status"] == "late" else "") for a in items[:15]]
        text = (f"🧾 **Utility bills to send to AppFolio** — {len(items)} ready" + (f" ({late} late)" if late else "") + "\n"
                + "\n".join(lines) + (f"\n…and {len(items) - 15} more" if len(items) > 15 else "") + f"\nOpen the board: {link}")
        if notify.deliver(uid, "utilities", text) != "off":
            sent += 1
    # late → tell the head once per bill (bundled per head)
    late = [a for a in b["accounts"] if a["status"] == "late"]
    with tx() as c:
        done = {(r[0], r[1]) for r in c.execute("SELECT account_id, period FROM utility_alerts WHERE period=?", (b["period"],))}
        fresh = [a for a in late if (a["id"], b["period"]) not in done]
        for a in fresh:
            c.execute("INSERT OR IGNORE INTO utility_alerts(account_id, period) VALUES(?,?)", (a["id"], b["period"]))
        heads = {}
        for a in fresh:
            h = departments.head_of(c, a["assignee_id"]) if a["assignee_id"] else None
            heads.setdefault(h["id"] if h else None, []).append(a)
        mgrs = [r[0] for r in c.execute("SELECT id FROM users WHERE role IN ('manager','admin')")]
    for hid, items in heads.items():
        text = (f"🧾 {len(items)} utility bill{'s' if len(items) > 1 else ''} not sent to AppFolio yet (late):\n"
                + "\n".join(f"• {a['property_name'] or a['property_label'] or '—'} — {a['provider'] or ''} {a['account_no'] or ''} · {a['assignee_name'] or 'unassigned'}"
                            for a in items[:15]) + f"\n{link}")
        for uid in ([hid] if hid else mgrs):
            notify.deliver(uid, "manager", text)
    return sent


# ---------- import from the spreadsheet ----------

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m} | {m.lower(): i for i, m in enumerate(calendar.month_name) if m} | {"sept": 9}


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _periods(heads: list, today: date) -> dict:
    """Month columns run oldest → newest with no years; walk back from today to give each one its year."""
    cols = [(i, MONTHS.get(_s(h).lower().rstrip("."))) for i, h in enumerate(heads)]
    cols = [(i, m) for i, m in cols if m]
    y, cm = today.year, today.month
    out = {}
    for i, m in reversed(cols):
        if m > cm:
            y -= 1
        out[i] = f"{y}-{m:02d}"
        y, cm = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


def _mark_of(v: str):
    t = v.strip()
    if not t:
        return None
    u = t.upper()
    if u in ("X", "M.", ".", "S", "XX", "✓", "DONE", "SENT"):
        return ("sent", None)
    if u == "FINAL":
        return ("final", None)
    if u in ("N/A", "NA"):
        return ("na", None)
    return ("other", t)


def parse(data: bytes, today: date | None = None) -> list[dict]:
    import openpyxl
    today = today or date.today()
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=False)
    sheets = sorted(wb.worksheets, key=lambda w: (w.title.strip().lower() != "utilities", "missing" in w.title.lower()))
    for ws in sheets:
        if "missing" in ws.title.lower():
            continue
        it = ws.iter_rows(values_only=True)
        for head in it:
            h = [_s(x).lower() for x in head]
            if "reference" in h and "property name" in h:
                break
        else:
            continue
        ci = {k: h.index(k) for k in ("reference", "property name", "account", "description") if k in h}
        addr_i = next((i for i, x in enumerate(h) if "address" in x), None)
        ref_i = ci["reference"]
        periods = _periods(list(head), today)
        known = set(ci.values()) | ({addr_i} if addr_i is not None else set())
        login_cols = [i for i in range(0, ref_i) if i not in known]   # the login columns sit left of Reference
        first_month = min(periods) if periods else len(h)
        named = {x: i for i, x in enumerate(h) if x in ("bill date", "payment date", "pay date", "due date")}
        free = [i for i in range(max(known) + 1, first_month) if not h[i]]   # unlabeled: Bill Date, Payment Date
        bill_i = named.get("bill date", free[0] if free else None)
        pay_i = named.get("payment date", named.get("pay date", named.get("due date", free[1] if len(free) > 1 else None)))
        out, provider, prov_login = [], "", ("", "")
        g = lambda r, i: _s(r[i]) if i is not None and i < len(r) else ""
        for r in it:
            ref, prop, gl, desc = g(r, ref_i), g(r, ci.get("property name")), g(r, ci.get("account")), g(r, ci.get("description"))
            addr = g(r, addr_i)
            lg = [g(r, i) for i in login_cols] + ["", ""]
            if ref and not prop and not gl and not addr and "MMYY" not in ref.upper() and not re.match(r"^[\d-]{5,}", ref):
                provider, prov_login = ref, (lg[0], lg[1])   # a provider heading row
                continue
            if not (ref or prop or gl or addr):
                if desc and out:                           # a note under the previous account
                    out[-1]["notes"] = (out[-1]["notes"] + " · " if out[-1]["notes"] else "") + desc
                continue
            marks = {}
            for i, p in periods.items():
                mk = _mark_of(g(r, i))
                if mk:
                    marks[p] = mk
            user, pw = (lg[0], lg[1]) if (lg[0] or lg[1]) else prov_login
            note = ""
            if user and not pw and (" " in user or user.lower() == provider.lower()):   # a note, not a username
                note, user = ("" if user.lower() == provider.lower() else user), ""
            acct_no = re.sub(r"\s*(MM)?(YY){1,2}\s*$", "", ref, flags=re.I).strip()
            annual = "annual" in (desc + " " + gl).lower() or bool(re.search(r"\bYYYY\s*$", ref, re.I))
            from collections import Counter
            bm = Counter(int(p[5:7]) for p, (st, _) in marks.items() if st != "na").most_common(1)
            last = max(marks) if marks else None
            out.append({"provider": provider, "property_label": prop, "address": addr,
                        "key": f"{prop} | {addr}" if addr else prop, "reference": ref, "account_no": acct_no, "gl_account": gl,
                        "description": desc, "bill_day": g(r, bill_i), "pay_day": g(r, pay_i), "login_user": user,
                        "login_pass": pw, "notes": note, "marks": marks, "freq": "annual" if annual else "monthly",
                        "bill_month": (bm[0][0] if bm else 1) if annual else None, "active": not (last and marks[last][0] == "final")})
        return out
    raise ValueError("Couldn't find the Utilities sheet (needs Reference and Property Name columns).")


def _street(a: str) -> str:
    from . import properties
    return properties.norm((a or "").split(",")[0])


def _by_address(c, address: str, label: str = ""):
    """Match the sheet's address to a property page: exact, else the street part (number + street) — and if several
    buildings share it, the one whose name looks like the sheet's property name."""
    from . import properties
    if not address:
        return None
    n = properties.norm(address)
    ps = rows(c.execute("SELECT id, name, address, norm FROM properties WHERE archived_at IS NULL"))
    hits = [p for p in ps if p["norm"] == n or (p["norm"] or "").startswith(n + " #")]
    if not hits:
        st = _street(address)
        num = st.split(" ")[0] if st else ""
        if not st or not num[:1].isdigit():
            return None
        def same(p):
            ps_ = _street(p["address"])
            return ps_.split(" ")[0] == num and (ps_ == st or ps_.startswith(st + " ") or st.startswith(ps_ + " "))
        hits = [p for p in ps if same(p)]
    if len(hits) == 1:
        return hits[0]["id"]
    if len(hits) > 1 and label:
        words = set(re.findall(r"[a-z]{3,}", label.lower()))
        scored = sorted(hits, key=lambda p: -len(words & set(re.findall(r"[a-z]{3,}", p["name"].lower()))))
        if len(words & set(re.findall(r"[a-z]{3,}", scored[0]["name"].lower()))) > \
                len(words & set(re.findall(r"[a-z]{3,}", scored[1]["name"].lower()))):
            return scored[0]["id"]
    return None


def _guess_property(c, label: str, address: str = ""):
    if address:
        pid = _by_address(c, address, label)
        if pid:
            return pid, "address"
    pid = _guess_by_name(c, label)
    return (pid, "name") if pid else (None, None)


def _guess_by_name(c, label: str):
    if not label:
        return None
    r = c.execute("SELECT id FROM properties WHERE archived_at IS NULL AND lower(name)=lower(?)", (label,)).fetchone()
    if r:
        return r[0]
    hits = [x[0] for x in c.execute("""SELECT id FROM properties WHERE archived_at IS NULL AND
                                       (lower(name) LIKE lower(?) OR lower(?) LIKE '%' || lower(name) || '%') AND length(name)>=4""",
                                    (f"%{label}%", label))]
    if len(hits) == 1:
        return hits[0]
    from . import properties
    return properties.property_for_address(label)


def import_rows(data: bytes, apply=False, mapping: dict | None = None, assignee_id=None, history=True, logins=True,
                user_id=None, today: date | None = None) -> dict:
    items = parse(data, today)
    labels, how = {}, {}
    with tx() as c:
        for it in items:
            k = it["key"]
            if k not in labels:
                labels[k], how[k] = _guess_property(c, it["property_label"], it["address"])
        props = rows(c.execute("SELECT id, name FROM properties WHERE archived_at IS NULL ORDER BY name COLLATE NOCASE"))
        people = rows(c.execute("SELECT id, name FROM users WHERE email IS NULL OR email NOT LIKE '%demo.example.com' ORDER BY name"))
    guess = next((p["id"] for p in people if p["name"].split()[0].lower() == "jennifer"), None)
    hist = sorted({p for it in items for p in it["marks"]})
    out = {"total": len(items), "active": sum(it["active"] for it in items),
           "with_login": sum(bool(it["login_user"] or it["login_pass"]) for it in items),
           "providers": sorted({it["provider"] for it in items if it["provider"]}),
           "has_address": any(it["address"] for it in items),
           "labels": [{"key": k, "label": next(it["property_label"] for it in items if it["key"] == k),
                       "address": next(it["address"] for it in items if it["key"] == k), "property_id": v, "matched_by": how[k],
                       "count": sum(1 for it in items if it["key"] == k)} for k, v in labels.items()],
           "properties": props, "people": people, "assignee_guess": assignee_id or settings()["default_assignee"] or guess,
           "history_from": hist[0] if hist else None, "history_to": hist[-1] if hist else None,
           "marks": sum(len(it["marks"]) for it in items), "applied": False}
    if not apply:
        return out
    from . import maintenance
    maintenance.backup_now()
    mapping = {k: (int(v) if v else None) for k, v in (mapping or {}).items()}
    if assignee_id:
        save_settings({"default_assignee": assignee_id})
    created = updated = marked = 0
    for it in items:
        pid = mapping[it["key"]] if it["key"] in mapping else labels.get(it["key"])
        fields = {k: it[k] for k in ("provider", "property_label", "reference", "account_no", "gl_account", "description",
                                     "bill_day", "pay_day", "notes", "active", "freq", "bill_month")} | {"property_id": pid}
        if assignee_id:
            fields["assignee_id"] = assignee_id
        if logins:
            fields |= {"login_user": it["login_user"], "login_pass": it["login_pass"]}
        with tx() as c:
            ex = c.execute("""SELECT id FROM utility_accounts WHERE COALESCE(reference,'')=? AND COALESCE(gl_account,'')=?
                              AND COALESCE(property_label,'')=? AND COALESCE(provider,'')=?""",
                           (it["reference"], it["gl_account"], it["property_label"], it["provider"])).fetchone()
        if ex:
            aid = ex[0]
            update(aid, fields)
            updated += 1
        else:
            aid = create(fields)
            created += 1
        if history:
            with tx() as c:
                for p, (st, note) in it["marks"].items():
                    n = c.execute("""INSERT OR IGNORE INTO utility_bills(account_id, period, status, note, via, sent_by)
                                     VALUES(?,?,?,?, 'import', ?)""", (aid, p, st, note, user_id)).rowcount
                    marked += n
    out.update({"applied": True, "created": created, "updated": updated, "marked": marked})
    return out
