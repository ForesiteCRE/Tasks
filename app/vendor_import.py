"""Import Foresite's Vendor Directory (.xlsx) into the vendor list.

Reads the sheet with Company Name / Name / Address / Phone Numbers / Email and a category column whose values are the
app's trades (HVAC, Landscaping, …). Sheets of tenants/owners ("Removed - At Managed Props") are skipped. Existing
vendors with the same name are updated (blank fields filled, trade added), never duplicated. Sample vendors can be
removed first in the same step.
"""
import io
import re

from . import vendors
from .db import tx


def _s(v) -> str:
    return "" if v is None else re.sub(r"\s+", " ", str(v)).strip()


def _phone(p: str) -> str:
    """'Phone: (210) 555-1234, Office: (210) 555-9999' → '(210) 555-1234 · Office (210) 555-9999'"""
    parts = []
    for i, x in enumerate(x.strip() for x in p.split(",") if x.strip()):
        m = re.match(r"^(\w[\w ]*):\s*(.+)$", x)
        label, num = (m.group(1), m.group(2)) if m else ("", x)
        parts.append(num if i == 0 or label.lower() == "phone" else f"{label} {num}")
    return " · ".join(parts)


def parse(data: bytes) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    best = None
    for ws in wb.worksheets:
        if "removed" in ws.title.lower():
            continue
        it = ws.iter_rows(values_only=True)
        for head in it:
            h = [_s(x).lower() for x in head]
            if "company name" in h:
                break
        else:
            continue
        col = {k: h.index(k) for k in ("company name", "name", "address", "phone numbers", "email", "first name", "last name",
                                        "category", "trade") if k in h}
        trade_col = col.get("trade", col.get("category"))
        out = []
        for r in it:
            g = lambda k: _s(r[col[k]]) if k in col and col[k] < len(r) else ""
            # trade: the category column, or (labeled lists) whichever early column holds a known trade
            trade = ""
            for i in ([trade_col] if trade_col is not None else []) + list(range(min(3, len(r)))):
                v = _s(r[i]) if i is not None and i < len(r) else ""
                if v in vendors.TRADES:
                    trade = v
                    break
            company = g("company name")
            first, last = g("first name"), g("last name")
            contact = g("name")
            if not company:   # e.g. "of Texas, LLC, Mechanical Maintenance" split across first/last
                company = f"{first} {last}".strip() or contact
                contact = ""
            if "," in contact and first and last and contact.lower().startswith(last.lower()):
                contact = f"{first} {last}"   # "Martinez, Andrew" → "Andrew Martinez"
            if not company:
                continue
            out.append({"name": company, "trade": trade or "Other", "contact_name": contact, "phone": _phone(g("phone numbers")),
                        "email": g("email").split(",")[0].strip(), "address": g("address")})
        if out and (best is None or len(out) > len(best)):
            best = out
    if not best:
        raise ValueError("Couldn't find a vendor sheet (needs a Company Name column).")
    return best


def run(data: bytes, apply: bool = False, remove_samples: bool = False, user_id=None) -> dict:
    rows = parse(data)
    with tx() as c:
        have = {r["name"].lower(): dict(r) for r in c.execute("SELECT * FROM vendors WHERE archived=0 AND is_sample=0")}
        samples = c.execute("SELECT COUNT(*) FROM vendors WHERE is_sample=1").fetchone()[0]
    from collections import Counter
    by_trade = Counter(r["trade"] for r in rows)
    out = {"total": len(rows), "new": sum(1 for r in rows if r["name"].lower() not in have),
           "existing": sum(1 for r in rows if r["name"].lower() in have), "samples": samples,
           "by_trade": by_trade.most_common(), "rows": rows[:400], "applied": apply}
    if not apply:
        return out
    if remove_samples:
        out["samples_removed"] = vendors.remove_samples()["removed"]
    with tx() as c:   # match against everything still on the list (a kept sample with the same name becomes the real one)
        have = {r["name"].lower(): dict(r) for r in c.execute("SELECT * FROM vendors WHERE archived=0")}
    created = updated = 0
    for r in rows:
        ex = have.get(r["name"].lower())
        notes = f"Address: {r['address']}" if r["address"] else ""
        if ex:
            trades = [t for t in (ex["trades"] or "").split("|") if t]
            if r["trade"] not in trades:
                trades.append(r["trade"])
            with tx() as c:
                c.execute("""UPDATE vendors SET trades=?, contact_name=COALESCE(contact_name, ?), phone=COALESCE(phone, ?),
                             email=COALESCE(email, ?), notes=CASE WHEN COALESCE(notes,'')='' THEN ? ELSE notes END, is_sample=0 WHERE id=?""",
                          (vendors._clean_trades(trades), r["contact_name"] or None, r["phone"] or None, r["email"] or None,
                           notes or None, ex["id"]))
            updated += 1
        else:
            vid = vendors.create({"name": r["name"], "trades": [r["trade"]], "contact_name": r["contact_name"], "phone": r["phone"],
                                  "email": r["email"], "notes": notes}, user_id)
            have[r["name"].lower()] = {"id": vid, "trades": r["trade"]}
            created += 1
    out.update(created=created, updated=updated)
    return out
