"""Utility bills: import Accounting's Utilities sheet, the monthly board, logins, Send to AppFolio, reminders."""
import base64
import io
import types
from datetime import date

import openpyxl

from app import utilities
from app.db import tx
from conftest import SENT


def _sheet() -> str:
    wb = openpyxl.Workbook()
    wb.active.title = "AP Entry Notes"
    miss = wb.create_sheet("Missing Utilities")
    miss.append(["Login Info3", None, "Reference", "Property Name", "Account", "Description"])
    ws = wb.create_sheet("Utilities")
    ws.append(["Login Info3", None, "Reference", "Property Name", "Account", "Description", None, None, "SEP", "OCT", "NOV"])
    ws.append([None] * 11)
    ws.append(["shared@foresitecre.com", None, "CPS Energy", None, None, None, "Bill Date", "Payment Date"])
    ws.append(["cpsuser", "s3cret!", "300-4780-103 MMYY", "Utility Plaza", "6410-0000 - Electricity", "mm/dd/yy-mm/dd/yy #HM", 1, 22, "x", "x", None])
    ws.append([None, None, "300-4780-104 MMYY", "Utility Plaza", "6420-0000 - Gas", "mm/dd/yy-mm/dd/yy", 25, 10, "x", "N/A", None])
    ws.append([None, None, None, None, None, "Billed on 24th"])
    ws.append([None, None, "SAWS", None, None, None])
    ws.append(["do not download; sent to Accounting email", None, "07-2800-02 MMYY", "Nowhere Plaza", "6430-0000 - Water and Sewer", None, None, None, "x", "FINAL", None])
    buf = io.BytesIO()
    wb.save(buf)
    return base64.b64encode(buf.getvalue()).decode()


S = {}


def test_parse_shapes():
    items = utilities.parse(base64.b64decode(_sheet()), date(2026, 11, 20))
    assert len(items) == 3
    a, b, c = items
    assert (a["provider"], a["account_no"], a["login_user"], a["login_pass"]) == ("CPS Energy", "300-4780-103", "cpsuser", "s3cret!")
    assert a["marks"] == {"2026-09": ("sent", None), "2026-10": ("sent", None)}
    assert b["login_user"] == "shared@foresitecre.com"       # inherits the provider heading's login
    assert b["notes"] == "Billed on 24th" and b["marks"]["2026-10"] == ("na", None)
    assert c["provider"] == "SAWS" and c["notes"].startswith("do not download") and not c["login_user"]
    assert c["active"] is False                               # FINAL last month → closed


def test_reference_and_dates():
    assert utilities.reference_for("300-4780-103 MMYY", "2026-11") == "300-4780-103 1126"
    d = utilities.dates({"bill_day": 25, "pay_day": 10}, "2026-11")
    assert d["pay"] == date(2026, 12, 10) and d["late"] == date(2026, 11, 30)
    d = utilities.dates({"bill_day": 1, "pay_day": 5}, "2026-11")
    assert d["late"] == date(2026, 11, 2)                     # 3 days before payment beats 5 days after the bill
    assert utilities.dates({}, "2026-02")["late"] == date(2026, 3, 1)   # no bill date → only late once the month ends


def test_import_preview_and_apply(mgr, staff):
    pid = mgr.post("/api/properties", json={"address": "1 Utility Way, San Antonio, TX", "name": "Utility Plaza"}).json()
    S["pid"] = pid if isinstance(pid, int) else pid.get("id")
    r = mgr.post("/api/utilities/import", json={"data_b64": _sheet()})
    assert r.status_code == 200, r.text
    r = r.json()
    assert r["total"] == 3 and r["with_login"] == 2 and not r["applied"]
    lab = {x["label"]: x["property_id"] for x in r["labels"]}
    assert lab["Utility Plaza"] == S["pid"] and lab["Nowhere Plaza"] is None
    assert staff.post("/api/utilities/import", json={"data_b64": _sheet()}).status_code == 403
    r = mgr.post("/api/utilities/import", json={"data_b64": _sheet(), "apply": True, "assignee_id": staff.me["id"]}).json()
    assert (r["created"], r["marked"]) == (3, 6)
    r = mgr.post("/api/utilities/import", json={"data_b64": _sheet(), "apply": True, "assignee_id": staff.me["id"]}).json()
    assert (r["created"], r["updated"], r["marked"]) == (0, 3, 0)   # re-import never duplicates
    with tx() as c:
        raw = c.execute("SELECT login_pass FROM utility_accounts WHERE account_no='300-4780-103'").fetchone()[0]
    assert raw and "s3cret" not in raw                                # encrypted at rest


def test_board_and_logins(mgr, staff):
    b = mgr.get("/api/utilities?period=2026-10").json()
    st = {a["account_no"]: a for a in b["accounts"]}
    assert st["300-4780-103"]["status"] == "sent" and st["300-4780-104"]["status"] == "na"
    assert "07-2800-02" not in st                                     # closed (final) accounts drop off
    assert st["300-4780-103"]["ref"] == "300-4780-103 1026"
    S["aid"] = st["300-4780-103"]["id"]
    assert "login_pass" not in st["300-4780-103"] and st["300-4780-103"]["has_login"]
    assert staff.get(f"/api/utilities/{S['aid']}/login").json()["password"] == "s3cret!"   # Sam sends these bills
    other = utilities.create({"provider": "Spectrum", "account_no": "999", "login_pass": "x", "assignee_id": None})
    assert staff.get(f"/api/utilities/{other}/login").status_code == 403
    assert staff.post(f"/api/utilities/{other}/bills", json={"period": "2026-11", "status": "sent"}).status_code == 403
    utilities.delete(other)


def test_mark_and_statuses(staff):
    today = date(2026, 11, 20)
    b = utilities.board("2026-11", today=today)
    st = {a["account_no"]: a["status"] for a in b["accounts"]}
    assert st["300-4780-103"] == "late" and st["300-4780-104"] == "upcoming"
    assert utilities.board("2026-11", today=date(2026, 11, 3))["accounts"][0]["status"] in ("ready", "late")
    r = staff.post(f"/api/utilities/{S['aid']}/bills", json={"period": "2026-11", "status": "sent"})
    assert r.status_code == 200, r.text
    assert {a["account_no"]: a["status"] for a in utilities.board("2026-11", today=today)["accounts"]}["300-4780-103"] == "sent"
    assert staff.delete(f"/api/utilities/{S['aid']}/bills/2026-11").status_code == 200


def test_send_to_appfolio(staff, monkeypatch):
    from app import emails
    pdf = base64.b64encode(b"%PDF-1.4 bill").decode()
    monkeypatch.setattr(emails, "_access_token", lambda uid: None)
    r = staff.post(f"/api/utilities/{S['aid']}/send", json={"period": "2026-11", "filename": "nov.pdf", "data_b64": pdf}).json()
    assert r["sent"] is False and r["need_gmail"] and r["email"]["to"] == "foresitecre@invoices.appfolio.com"
    assert "300-4780-103 1126" in r["email"]["subject"]
    staff.post(f"/api/utilities/{S['aid']}/bills", json={"period": "2026-11", "status": "sent"})   # "I sent it myself"
    assert staff.get(f"/api/utilities/{S['aid']}/bills/2026-11/file").content == b"%PDF-1.4 bill"   # upload kept
    staff.delete(f"/api/utilities/{S['aid']}/bills/2026-11")

    calls = []
    import httpx
    monkeypatch.setattr(emails, "_access_token", lambda uid: "tok")
    monkeypatch.setattr(httpx, "post", lambda url, **kw: calls.append((url, kw)) or types.SimpleNamespace(status_code=200, text="", json=lambda: {"id": "m1"}))
    r = staff.post(f"/api/utilities/{S['aid']}/send", json={"period": "2026-11", "filename": "nov.pdf", "data_b64": pdf}).json()
    assert r["sent"] is True and calls[0][0].endswith("/messages/send")
    raw = base64.urlsafe_b64decode(calls[0][1]["json"]["raw"]).decode(errors="ignore")
    assert "To: foresitecre@invoices.appfolio.com" in raw and "nov.pdf" in raw
    b = utilities.board("2026-11", today=date(2026, 11, 20))
    a = next(x for x in b["accounts"] if x["id"] == S["aid"])
    assert a["status"] == "sent" and a["bill"]["via"] == "gmail" and a["bill"]["has_file"]
    staff.delete(f"/api/utilities/{S['aid']}/bills/2026-11")


def test_daily_reminder(staff, mgr):
    with tx() as c:
        c.execute("UPDATE users SET blink_user_id='blink-sam' WHERE id=?", (staff.me["id"],))
        c.execute("DELETE FROM settings WHERE key='utility.reminded'")
    SENT.clear()
    assert utilities.run_daily(date(2026, 11, 20)) == 1
    msg = [t for b, t in SENT if b == "blink-sam"]
    assert msg and "Utility bills to send to AppFolio" in msg[0] and "300-4780-103" in msg[0] and "late" in msg[0]
    assert "s3cret" not in msg[0]
    assert utilities.run_daily(date(2026, 11, 20)) == 0           # once a day
    assert mgr.get("/api/me/utilities?all=1").json()["counts"]["total"] >= 2


def test_yearly_bills():
    assert utilities.reference_for("001666661-0497312-0003 yyyy", "2027-01") == "001666661-0497312-0003 2027"
    aid = utilities.create({"provider": "SAWS", "account_no": "fire-1", "reference": "fire-1 yyyy", "freq": "annual", "bill_month": 1})
    st = lambda p, t: next(a["status"] for a in utilities.board(p, today=t)["accounts"] if a["id"] == aid)
    assert st("2026-11", date(2026, 11, 20)) == "skip"
    assert st("2027-01", date(2027, 1, 3)) == "ready"
    utilities.delete(aid)


def test_import_matches_by_address(mgr):
    from app import properties
    a = properties.create("777 Sonterra Blvd, San Antonio, TX 78258", name="Sonterra Office")
    b1 = properties.create("5009 Schertz Pkwy, Schertz, TX 78154", name="Schertz Retail A")
    with tx() as c:   # a second building at the same address (the directory import suffixes its norm)
        b2 = c.execute("INSERT INTO properties(name, address, norm) VALUES('Schertz Medical B', '5009 Schertz Pkwy, Schertz, TX 78154', ?)",
                       (properties.norm("5009 Schertz Pkwy, Schertz, TX 78154") + " #schertz-medical-b",)).lastrowid
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Utilities"
    ws.append(["Login Info3", None, "Reference", "Property Name", "Property Address", "Account", "Description", None, None, "OCT", "NOV"])
    ws.append([None, None, "CPS Energy"])
    ws.append(["u1", "p1", "111 MMYY", "Sonterra HQ", "777 Sonterra Boulevard", "6410-0000 - Electricity", "svc", 3, 20, "x", None])
    ws.append([None, None, "222 MMYY", "Schertz - Medical", "5009 Schertz Pkwy", "6410-0000 - Electricity", "svc", 4, 21, "x", None])
    ws.append([None, None, "333 MMYY", "Mystery Place", "1 Nowhere Ln", "6410-0000 - Electricity", "svc", 5, 22, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    items = utilities.parse(data, date(2026, 11, 20))
    assert items[0]["address"] == "777 Sonterra Boulevard" and items[0]["bill_day"] == "3" and items[0]["pay_day"] == "20"
    assert items[0]["login_user"] == "u1" and items[0]["marks"] == {"2026-10": ("sent", None)}
    r = mgr.post("/api/utilities/import", json={"data_b64": base64.b64encode(data).decode()}).json()
    m = {l["label"]: (l["property_id"], l["matched_by"]) for l in r["labels"]}
    assert m["Sonterra HQ"] == (a, "address")          # "Boulevard" vs "Blvd", no city on the sheet
    assert m["Schertz - Medical"] == (b2, "address")   # shared address → the building whose name matches
    assert m["Mystery Place"] == (None, None) and r["has_address"]
    assert b1 != b2
