"""Starter checklist templates for a commercial property-management team.

Each item: title, role (project role name) or none, days (due = project start + days), est (minutes),
optional checklist lines, subtasks (same shape), and after (titles in the same template it waits on).
Managers add these from Tasks → Templates → "Add starter templates"; they become normal, editable templates.
"""
from .db import tx

PM, ACCT, AP, AR, LEASING, AM = "Property Manager", "Head of Accounting", "Accounting 2", "Accounting 1", "Leasing", "Asset Manager"

from . import templates_v2 as V2

# ---------- Property Onboarding Document Requests (our Management Transition Documentation Request) ----------
# One task; every document the prior manager provides is a subtask, labeled by section. "Prior to takeover" items
# are due right after the request; "at takeover" items (accounting that keeps coming as months close) are due later.
DOC_TASK = "Property Onboarding Document Requests"
EARLY, LATE = 5, 55     # days after the template start date
_DOCS = [  # (section, document, role, prior-to-takeover?, at-takeover?)
    ("Tenant", "Current rent roll", PM, 1, 0),
    ("Tenant", "All tenant leases, amendments & letter agreements", PM, 1, 0),
    ("Tenant", "Tenant notice addresses, contact names & emails", PM, 1, 0),
    ("Tenant", "Aged tenant receivables / delinquency", AR, 1, 1),
    ("Tenant", "Tenant ledgers through the assumption date", AR, 1, 0),
    ("Tenant", "List & status of security deposits", AR, 1, 0),
    ("Tenant", "Current tenant COIs", PM, 1, 0),
    ("Tenant", "Prior & current-year reconciliations, escalations & statements (with CAM caps)", ACCT, 1, 0),
    ("Accounting", "Monthly trial balance (Excel) — YTD now, remaining months as completed", ACCT, 1, 1),
    ("Accounting", "Current-year operating budget(s)", ACCT, 1, 0),
    ("Accounting", "Prior-year & current YTD general ledger (Excel) — remaining months as completed", ACCT, 1, 1),
    ("Accounting", "Most recent monthly report pack — remaining months as completed", ACCT, 1, 1),
    ("Accounting", "Current-year AP invoices (incl. utilities, for the billing change)", AP, 0, 1),
    ("Accounting", "Recurring journal entry schedule", ACCT, 0, 1),
    ("Accounting", "Depreciation schedule(s)", ACCT, 0, 1),
    ("Accounting", "Balance sheet", ACCT, 0, 1),
    ("Banking", "List of operating / deposit account numbers", ACCT, 1, 0),
    ("Banking", "Banking contact information", ACCT, 1, 0),
    ("Banking", "YTD deposit register — remaining months as completed", ACCT, 1, 1),
    ("Banking", "Current-year bank statements & reconciliations — remaining months as completed", ACCT, 1, 1),
    ("Taxes & insurance", "Current property insurance certificate & agent contact", PM, 1, 0),
    ("Taxes & insurance", "Copy of current insurance policy", PM, 1, 0),
    ("Taxes & insurance", "List of insurance claims in the last year, if any", PM, 1, 0),
    ("Taxes & insurance", "Owner W-9", ACCT, 1, 0),
    ("Taxes & insurance", "Property tax statements (prior 2 years)", PM, 1, 0),
    ("Taxes & insurance", "Franchise tax statements (prior 2 years)", ACCT, 1, 0),
    ("Building", "All building site & floor plans — paper & electronic", PM, 1, 0),
    ("Building", "Warranties (roof, HVAC, etc.)", PM, 1, 0),
    ("Loan", "Most recent loan statement(s)", ACCT, 1, 0),
    ("Loan", "Amortization schedules", ACCT, 0, 1),
    ("Loan", "List of escrow accounts & balances", ACCT, 1, 0),
    ("Vendors / AP", "Utility & vendor files (copies of utility bills, contracts, COIs & W-9s)", AP, 1, 0),
    ("Vendors / AP", "YTD aged payables report", AP, 1, 0),
    ("Vendors / AP", "YTD check register — remaining months as completed", AP, 1, 1),
    ("Vendors / AP", "Vendor list with email, phone & point of contact", AP, 1, 0),
    ("Vendors / AP", "Emergency vendor list", PM, 1, 0),
    ("Vendors / AP", "Maintenance contracts", PM, 1, 0),
    ("Vendors / AP", "Online payment logins (utilities, bank, etc.) — keep in our password manager, not in the Task Manager", AP, 1, 0),
]


def _doc_subtasks() -> list[dict]:
    subs = []
    for sec, doc, role, early, late in _DOCS + V2.EXTRA_DOCS:
        base = doc.split(" — ")[0]
        if early:
            subs.append({"title": f"{sec} · {base} — so far" if late else f"{sec} · {doc}", "role": role, "days": EARLY, "est": 10})
        if late:
            subs.append({"title": f"{sec} · {doc} (at takeover)" if not early else f"{sec} · {base} — final / remaining months (at takeover)",
                         "role": role, "days": LATE, "est": 10})
    return subs


DOC_REQUEST_ITEM = {
    "title": DOC_TASK, "role": PM, "days": LATE, "est": 60, "after": ["Fully executed management agreement on file"],
    "checklist": ["Request sent to prior management with the assumption date & due dates",
                  "Documents go to Nancy Braun (property management) and Lisa Jimenez (accounting)",
                  "Prior manager told to skip items that don't apply"],
    "subtasks": _doc_subtasks()}

STARTERS = [
    {
        "key": "onboarding",
        "board": "none",
        "name": "Property Onboarding (Management Takeover)",
        "description": "New management assignment, built from our New Assignment Check List, Transition Plan and Document Request. "
                       "Start date = management commencement (about 30 days before the assumption date, so pre-takeover items land in "
                       "time). Adjust days to your process.",
        "items": [DOC_REQUEST_ITEM if x == "DOC_REQUESTS" else x for x in V2.ONBOARDING_ITEMS],
    },
    {
        "key": "sale_handoff",
        "board": "none",
        "name": "Property Sale – Hand Off (management ends at closing)",
        "description": "Owner is selling and the buyer is bringing their own manager. Start date = PSA effective date; "
                       "closing assumed around day 45 — shift due dates to your actual closing.",
        "items": [
            {"title": "Kick-off: confirm closing date, buyer & buyer's manager contacts", "role": AM, "days": 0, "est": 45},
            {"title": "Deliver due-diligence package to buyer", "role": PM, "days": 5, "est": 300,
             "checklist": ["Rent roll & aging", "Leases & amendments", "Trailing 12 & YTD operating statements",
                           "Service contracts", "CAM reconciliations (2 yrs)", "Property tax & insurance info", "Open capital projects / warranties"]},
            {"title": "Tenant estoppel certificates", "role": LEASING, "days": 20, "est": 360,
             "subtasks": [{"title": "Send estoppels to all tenants", "role": LEASING, "days": 10, "est": 120},
                          {"title": "Chase & log returned estoppels; flag discrepancies", "role": LEASING, "days": 25, "est": 180}]},
            {"title": "SNDAs if lender requires", "role": LEASING, "days": 25, "est": 120},
            {"title": "Notify vendors: contracts terminating or assigning at closing", "role": PM, "days": 30, "est": 120,
             "checklist": ["Check each contract's termination notice period", "Send notices", "Confirm which buyer is assuming"]},
            {"title": "Prepare closing prorations support", "role": ACCT, "days": 38, "est": 300,
             "checklist": ["Rents collected / delinquent by tenant", "CAM, tax & insurance estimates vs actual (YTD)",
                           "Security deposits by tenant", "Prepaid rents", "Outstanding leasing commissions / TI obligations"]},
            {"title": "Tenant notice letters: new owner & payment instructions", "role": PM, "days": 45, "est": 90,
             "after": ["Kick-off: confirm closing date, buyer & buyer's manager contacts"]},
            {"title": "Closing day: keys, codes & files to buyer's manager", "role": PM, "days": 45, "est": 180,
             "after": ["Deliver due-diligence package to buyer"],
             "checklist": ["Keys & access devices (signed receipt)", "Tenant files", "Vendor & utility contact list",
                           "Warranties, plans & as-builts", "Open work orders"]},
            {"title": "Transfer utilities to buyer", "role": PM, "days": 46, "est": 90},
            {"title": "Forward post-closing rent receipts to buyer", "role": AR, "days": 60, "est": 120},
            {"title": "Pay final vendor invoices through closing", "role": AP, "days": 60, "est": 180},
            {"title": "Post-closing CAM reconciliation true-up with buyer", "role": ACCT, "days": 120, "est": 300},
            {"title": "Final owner report & distribution", "role": ACCT, "days": 75, "est": 240,
             "after": ["Pay final vendor invoices through closing", "Forward post-closing rent receipts to buyer"]},
            {"title": "Close bank accounts & archive property in systems", "role": ACCT, "days": 90, "est": 120,
             "after": ["Final owner report & distribution"],
             "checklist": ["Operating account closed", "Security deposit account closed", "1099 responsibility confirmed",
                           "Property set inactive in accounting system", "Files archived per retention policy"]},
            {"title": "Cancel management's insurance coverage for the property", "role": AM, "days": 46, "est": 30},
        ],
    },
    {
        "key": "sale_retain",
        "name": "Property Sale – Retain Management (new owner keeps us)",
        "description": "Property is selling and we stay on as manager for the buyer. Start date = PSA effective date; "
                       "closing assumed around day 45.",
        "items": [
            {"title": "Kick-off with seller and buyer: closing date & expectations", "role": AM, "days": 0, "est": 60},
            {"title": "New management agreement executed with buyer", "role": AM, "days": 30, "est": 120,
             "checklist": ["Fees & leasing commissions", "Reporting package & deadlines", "Approval limits",
                           "Budget timing", "Insurance requirements"]},
            {"title": "Deliver due-diligence package to buyer", "role": PM, "days": 5, "est": 300},
            {"title": "Tenant estoppel certificates", "role": LEASING, "days": 20, "est": 360,
             "subtasks": [{"title": "Send estoppels to all tenants", "role": LEASING, "days": 10, "est": 120},
                          {"title": "Chase & log returned estoppels; flag discrepancies", "role": LEASING, "days": 25, "est": 180}]},
            {"title": "Set up new owner entity in accounting system", "role": ACCT, "days": 35, "est": 180,
             "after": ["New management agreement executed with buyer"],
             "checklist": ["Owner entity & W-9", "New property ledger effective at closing", "Fee setup per new agreement",
                           "Reporting template per buyer's requirements"]},
            {"title": "Open new operating & security deposit accounts for buyer", "role": ACCT, "days": 38, "est": 120},
            {"title": "Prepare closing prorations support", "role": ACCT, "days": 38, "est": 300},
            {"title": "Tenant notice letters: new owner, same manager, new payment instructions", "role": PM, "days": 45, "est": 90,
             "after": ["Open new operating & security deposit accounts for buyer"]},
            {"title": "Transfer security deposits to buyer's trust account", "role": ACCT, "days": 47, "est": 90,
             "after": ["Open new operating & security deposit accounts for buyer"]},
            {"title": "Update insurance: buyer named insured, management additional insured", "role": AM, "days": 45, "est": 60},
            {"title": "Update vendor contracts & utilities to new owner", "role": PM, "days": 50, "est": 180},
            {"title": "Book opening balances from closing statement", "role": ACCT, "days": 55, "est": 180,
             "after": ["Set up new owner entity in accounting system"]},
            {"title": "Seller close-out: final report & distribution", "role": ACCT, "days": 75, "est": 240},
            {"title": "Post-closing CAM reconciliation true-up between seller & buyer", "role": ACCT, "days": 120, "est": 300},
            {"title": "Budget & business plan with new owner", "role": AM, "days": 75, "est": 360},
            {"title": "First owner report to buyer", "role": ACCT, "days": 60, "est": 240,
             "after": ["Book opening balances from closing statement"]},
        ],
    },
    {
        "key": "move_in",
        "name": "Tenant Move-In",
        "description": "New lease signed. Start date = lease execution date.",
        "items": V2.MOVE_IN_ITEMS,
    },
    {
        "key": "move_out",
        "name": "Tenant Move-Out",
        "description": "Tenant vacating. Start date = notice received / lease expiration minus 30 days.",
        "items": V2.MOVE_OUT_ITEMS,
    },
    {
        "key": "cam_rec",
        "board": "year",
        "name": "Annual CAM Reconciliation",
        "description": "Year-end common area maintenance reconciliation. Start date = Jan 15.",
        "items": [
            {"title": "Close prior year books for the property", "role": ACCT, "days": 0, "est": 240},
            {"title": "Pull pro-rata shares, caps & exclusions from abstracts", "role": LEASING, "days": 10, "est": 180},
            {"title": "Prepare reconciliation by tenant", "role": ACCT, "days": 30, "est": 480,
             "after": ["Close prior year books for the property", "Pull pro-rata shares, caps & exclusions from abstracts"]},
            {"title": "Owner review & approval", "role": AM, "days": 40, "est": 60, "after": ["Prepare reconciliation by tenant"]},
            {"title": "Send reconciliation statements to tenants", "role": AR, "days": 45, "est": 120, "after": ["Owner review & approval"]},
            {"title": "Bill / credit tenants & update new-year estimates", "role": AR, "days": 60, "est": 120,
             "after": ["Send reconciliation statements to tenants"]},
        ],
    },
    {
        "key": "month_end",
        "board": "month",
        "name": "Month-End Close",
        "description": "Monthly close in AppFolio and the owner report packet, from Accounting's procedures. Start date = 1st of the month (pairs well with Monthly recurring).",
        "items": V2.MONTH_END_ITEMS,
    },
    {
        "key": "budget",
        "board": "year",
        "name": "Annual Budget",
        "description": "Next-year operating & capital budget. Start date = Sept 15.",
        "items": [
            {"title": "Gather vendor pricing & contract renewals", "role": PM, "days": 0, "est": 240},
            {"title": "Property tax & insurance projections", "role": AM, "days": 10, "est": 120},
            {"title": "Leasing assumptions (renewals, vacancy, new deals)", "role": LEASING, "days": 10, "est": 180},
            {"title": "Draft operating budget", "role": ACCT, "days": 25, "est": 480,
             "after": ["Gather vendor pricing & contract renewals", "Property tax & insurance projections",
                       "Leasing assumptions (renewals, vacancy, new deals)"]},
            {"title": "Capital plan", "role": PM, "days": 25, "est": 240},
            {"title": "Owner review & approval", "role": AM, "days": 45, "est": 90, "after": ["Draft operating budget", "Capital plan"]},
            {"title": "Load budget & new CAM estimates", "role": ACCT, "days": 60, "est": 120, "after": ["Owner review & approval"]},
        ],
    },
    {
        "key": "tax_protest",
        "board": "year",
        "name": "Property Tax Protest",
        "description": "Appraisal notice → protest → hearing. Start date = when notices arrive (~April 15); Texas protests are usually due May 15.",
        "items": [
            {"title": "Review notice of appraised value", "role": AM, "days": 0, "est": 45},
            {"title": "Compare to purchase price, comps & income approach", "role": AM, "days": 7, "est": 120,
             "after": ["Review notice of appraised value"]},
            {"title": "Decide: protest ourselves or send to the tax protest company", "role": AM, "days": 10, "est": 30,
             "after": ["Compare to purchase price, comps & income approach"]},
            {"title": "Gather evidence (rent roll, operating statements, photos, repairs)", "role": ACCT, "days": 20, "est": 120},
            {"title": "File protest by the deadline", "role": PM, "days": 25, "est": 30,
             "after": ["Decide: protest ourselves or send to the tax protest company"]},
            {"title": "Informal review / ARB hearing", "role": AM, "days": 50, "est": 120, "after": ["File protest by the deadline"]},
            {"title": "Record result & update budget / escrow", "role": ACCT, "days": 75, "est": 45, "after": ["Informal review / ARB hearing"]},
        ],
    },
    {
        "key": "year_end",
        "board": "year",
        "name": "Year-End Close & 1099s",
        "description": "December close, owner year-end package and vendor 1099s (due Jan 31). Start date = Dec 1.",
        "items": [
            {"title": "Collect missing vendor W-9s", "role": AP, "days": 0, "est": 120},
            {"title": "Confirm year-end accruals & prepaids", "role": ACCT, "days": 25, "est": 120},
            {"title": "Final December close", "role": ACCT, "days": 40, "est": 240, "after": ["Confirm year-end accruals & prepaids"]},
            {"title": "Owner year-end package & tax information", "role": ACCT, "days": 55, "est": 180, "after": ["Final December close"]},
            {"title": "Issue 1099s to vendors (by Jan 31)", "role": AP, "days": 55, "est": 120, "after": ["Collect missing vendor W-9s"]},
            {"title": "File 1099s & 1096 with the IRS", "role": AP, "days": 58, "est": 45, "after": ["Issue 1099s to vendors (by Jan 31)"]},
            {"title": "Archive the year & roll the fiscal year", "role": ACCT, "days": 70, "est": 60, "after": ["Owner year-end package & tax information"]},
        ],
    },
    {
        "key": "inspection",
        "board": "quarter",
        "name": "Quarterly Property Inspection",
        "description": "The same walk at every property each quarter. Start date = first day of the quarter.",
        "items": [
            {"title": "Exterior walk", "role": PM, "days": 10, "est": 60,
             "checklist": ["Roof & drainage", "Parking lot & striping", "Lighting", "Landscaping & irrigation", "Signage", "Dumpster area"]},
            {"title": "Common areas & life safety", "role": PM, "days": 10, "est": 45,
             "checklist": ["Fire extinguishers & tags", "Exit / emergency lights", "Sprinkler & alarm tags", "Restrooms & corridors"]},
            {"title": "Check vacant suites", "role": LEASING, "days": 12, "est": 30},
            {"title": "Photos & inspection report", "role": PM, "days": 14, "est": 45, "after": ["Exterior walk", "Common areas & life safety"]},
            {"title": "Work orders for issues found", "role": PM, "days": 16, "est": 30, "after": ["Photos & inspection report"]},
            {"title": "Send summary to owner / asset manager", "role": PM, "days": 20, "est": 20, "after": ["Photos & inspection report"]},
        ],
    },
    V2.CASH_REFORECAST,
    V2.TRAINING,
]


SAMPLE_PROJECTS = {
    "onboarding": ("Sample – 777 E Sonterra Blvd (Onboarding)", "777 E Sonterra Blvd, San Antonio, TX 78258",
                   "Sonterra Commons Owner LLC", -12),
    "month_end": ("Sample – November Month-End Close (777 E Sonterra)", "777 E Sonterra Blvd, San Antonio, TX 78258",
                  "Sonterra Commons Owner LLC", -17),
    "sale_handoff": ("Sample – 8800 Research Blvd (Sale, hand off)", "8800 Research Blvd, Austin, TX", "Research Plaza Owner LP", -20),
    "sale_retain": ("Sample – 4500 Fredericksburg Rd (Sale, retain mgmt)", "4500 Fredericksburg Rd, San Antonio, TX",
                    "Fredericksburg Center LLC → Alamo Retail Fund II", -8),
    "move_in": ("Sample – Suite 110 Move-In, 777 E Sonterra (Coastal Coffee)", "777 E Sonterra Blvd, San Antonio, TX 78258",
                "Sonterra Commons Owner LLC", -10),
    "move_out": ("Sample – Suite 204 Move-Out", "8800 Research Blvd, Austin, TX", "Research Plaza Owner LP", -5),
    "cam_rec": ("Sample – 2026 CAM Reconciliation (777 E Sonterra)", "777 E Sonterra Blvd, San Antonio, TX 78258",
                "Sonterra Commons Owner LLC", -15),
    "budget": ("Sample – 2027 Budget (8800 Research Blvd)", "8800 Research Blvd, Austin, TX", "Research Plaza Owner LP", -6),
    "cash_forecast": ("Sample – Cash Reforecast for distribution (4500 Fredericksburg Rd)", "4500 Fredericksburg Rd, San Antonio, TX",
                      "Fredericksburg Center LLC", -1),
    "acct_training": ("Sample – Accounting New Hire Training", None, None, -12),
    "tax_protest": ("Sample – 2026 Tax Protest (4500 Fredericksburg Rd)", "4500 Fredericksburg Rd, San Antonio, TX",
                    "Fredericksburg Center LLC", -12),
    "year_end": ("Sample – 2026 Year-End & 1099s (777 E Sonterra)", "777 E Sonterra Blvd, San Antonio, TX 78258",
                 "Sonterra Commons Owner LLC", -5),
    "inspection": ("Sample – Q4 Inspection (8800 Research Blvd)", "8800 Research Blvd, Austin, TX", "Research Plaza Owner LP", -9),
}


def catalog() -> list[dict]:
    with tx() as c:
        have = {r[0] for r in c.execute("SELECT name FROM templates")}
    return [{"key": s["key"], "name": s["name"], "description": s["description"], "items": s["items"],
             "already_added": s["name"] in have} for s in STARTERS]


def install(keys: list[str], sample_projects: bool = False) -> dict:
    """Create the chosen starter templates (skips any whose name already exists) and, optionally, one
    sample project from each so people can see a realistic example. Returns what was added."""
    added = _install_templates(keys)
    samples = []
    if sample_projects:
        samples = create_samples(keys)
    return {"templates": added, "samples": samples}


def create_samples(keys: list[str]) -> list[str]:
    from datetime import date, timedelta

    from . import services
    made = []
    with tx() as c:
        have = {r[0] for r in c.execute("SELECT name FROM projects")}
        tpl_ids = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM templates")}
    for s in STARTERS:
        if s["key"] not in keys or s["key"] not in SAMPLE_PROJECTS:
            continue
        name, prop, client, start_offset = SAMPLE_PROJECTS[s["key"]]
        if name in have or s["name"] not in tpl_ids:
            continue
        pid = services.create_project_from_template(
            tpl_ids[s["name"]], name, date.today() + timedelta(days=start_offset),
            details={"property": prop, "client": client,
                     "description": "Sample project — safe to explore or delete (Projects → Details & team → Delete)."})
        with tx() as c:
            c.execute("UPDATE projects SET is_sample=1 WHERE id=?", (pid,))
            # make it look lived-in: tasks that were due a while ago are done
            c.execute("""UPDATE tasks SET status='done', completed_at=datetime('now'), actual_minutes=estimate_minutes
                         WHERE project_id=? AND due_date < date('now','-3 days')""", (pid,))
            c.execute("""UPDATE task_checklist SET done=1 WHERE task_id IN
                         (SELECT id FROM tasks WHERE project_id=? AND status='done')""", (pid,))
        made.append(name)
    return made


def remove_samples() -> int:
    with tx() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM projects WHERE is_sample=1")]
    for pid in ids:
        delete_project(pid)
    return len(ids)


def delete_project(pid: int):
    from . import collab, photos
    photos.remove_upload(pid)
    with tx() as c:
        tids = [r[0] for r in c.execute("SELECT id FROM tasks WHERE project_id=? AND parent_id IS NULL", (pid,))]
    for tid in tids:
        collab.delete_task(tid)
    with tx() as c:
        c.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
        c.execute("DELETE FROM project_team WHERE project_id=?", (pid,))
        # everything hanging off the project, so a future project that reuses this id starts clean
        for table in ("project_vendors", "project_bids"):
            c.execute(f"DELETE FROM {table} WHERE project_id=?", (pid,))   # bid files stay on disk for Recently deleted
        c.execute("DELETE FROM approval_events WHERE approval_id IN (SELECT id FROM project_approvals WHERE project_id=?)", (pid,))
        c.execute("DELETE FROM project_approvals WHERE project_id=?", (pid,))
        c.execute("DELETE FROM projects WHERE id=?", (pid,))


def _add_item(tid: int, item: dict, roles: dict, ids: dict, parent=None) -> int:
    from . import tpl
    iid = tpl.add_item(tid, item["title"], parent_item_id=parent, due_offset_days=item.get("days", 0),
                       default_role_id=roles.get((item.get("role") or "").lower()))
    tpl.update_item(iid, {"estimate_minutes": item.get("est")})
    for line in item.get("checklist", []):
        tpl.add_checklist(iid, line)
    ids[item["title"]] = iid
    for sub in item.get("subtasks", []):
        _add_item(tid, sub, roles, ids, iid)
    return iid


def _build_items(tid: int, items: list[dict], roles: dict):
    from . import tpl
    ids = {}
    for item in items:
        _add_item(tid, item, roles, ids)
    for item in items:
        for dep in item.get("after", []):
            if dep in ids:
                tpl.add_dependency(ids[item["title"]], ids[dep])


REBUILT_V2 = ("onboarding", "month_end", "move_in", "move_out")
REMOVED_V2 = {"Lease Renewal": "Sample – Suite 300 Renewal (Lone Star Dental)"}


def upgrade_templates_v2() -> list[str]:
    """One time (Nov 2026): rebuild the installed onboarding, month-end, move-in and move-out templates from Foresite's
    own checklists, and remove the Lease Renewal template. Template names and tracking-board settings stay; projects
    already started keep their tasks."""
    from . import team, tpl
    from .db import get_setting, set_setting
    if get_setting("tpl.v2"):
        return []
    roles = {r["name"].lower(): r["id"] for r in team.list_roles()}
    done = []
    for key in REBUILT_V2:
        st = next(x for x in STARTERS if x["key"] == key)
        with tx() as c:
            t = c.execute("SELECT id FROM templates WHERE name=?", (st["name"],)).fetchone()
            if not t:
                continue
            tops = [r[0] for r in c.execute("SELECT id FROM template_items WHERE template_id=? AND parent_item_id IS NULL", (t[0],))]
            c.execute("UPDATE templates SET description=? WHERE id=?", (st["description"], t[0]))
        for iid in tops:
            tpl.delete_item(iid)
        _build_items(t[0], st["items"], roles)
        done.append(st["name"])
    with tx() as c:
        c.execute("UPDATE template_items SET title='File 1099s & 1096 with the IRS' WHERE title='File 1099s with the IRS'")
    for name, sample in REMOVED_V2.items():
        with tx() as c:
            t = c.execute("SELECT id FROM templates WHERE name=?", (name,)).fetchone()
            samples = [r[0] for r in c.execute("SELECT id FROM projects WHERE is_sample=1 AND name=?", (sample,))]
        for pid in samples:
            delete_project(pid)
        if t:
            with tx() as c:
                tops = [r[0] for r in c.execute("SELECT id FROM template_items WHERE template_id=? AND parent_item_id IS NULL", (t[0],))]
            for iid in tops:
                tpl.delete_item(iid)
            with tx() as c:
                c.execute("UPDATE projects SET template_id=NULL WHERE template_id=?", (t[0],))
                c.execute("DELETE FROM recurrences WHERE template_id=?", (t[0],))
                c.execute("DELETE FROM templates WHERE id=?", (t[0],))
            done.append(f"removed {name}")
    with tx() as c:   # anyone already using starter templates also gets the two new ones
        uses_starters = c.execute("SELECT 1 FROM templates WHERE name IN (?,?)",
                                  ("Month-End Close", "Property Onboarding (Management Takeover)")).fetchone()
    if uses_starters:
        done += _install_templates(["cash_forecast", "acct_training"])
    set_setting("tpl.v2", "done")
    return done


def migrate_onboarding_docs() -> bool:
    """One time: in an installed onboarding template, swap the old one-step document checklist for
    "Property Onboarding Document Requests" (one subtask per document). Anything else edited in the template stays.
    Projects already started from the template are not touched."""
    from . import team, tpl
    from .db import get_setting, set_setting
    if get_setting("tpl.onboarding_docs_v1"):
        return False
    onboarding = next(x for x in STARTERS if x["key"] == "onboarding")
    with tx() as c:
        t = c.execute("SELECT id FROM templates WHERE name=?", (onboarding["name"],)).fetchone()
        if not t:
            set_setting("tpl.onboarding_docs_v1", "no template")
            return False
        tid = t[0]
        if c.execute("SELECT 1 FROM template_items WHERE template_id=? AND title=?", (tid, DOC_TASK)).fetchone():
            set_setting("tpl.onboarding_docs_v1", "already there")
            return False
        old = c.execute("""SELECT id, position FROM template_items WHERE template_id=? AND parent_item_id IS NULL
                           AND title='Collect property documents from owner / prior manager'""", (tid,)).fetchone()
        agreement = c.execute("""SELECT id FROM template_items WHERE template_id=? AND parent_item_id IS NULL
                                 AND title='Fully executed management agreement on file'""", (tid,)).fetchone()
        waiting = [r[0] for r in c.execute("SELECT item_id FROM template_dependencies WHERE depends_on_item_id=?",
                                           (old["id"],))] if old else []
    if old:
        tpl.delete_item(old["id"])
    roles = {r["name"].lower(): r["id"] for r in team.list_roles()}
    new_id = _add_item(tid, DOC_REQUEST_ITEM, roles, {})
    with tx() as c:
        if old:   # same spot in the list as the step it replaces
            c.execute("UPDATE template_items SET position=? WHERE id=?", (old["position"], new_id))
    if agreement:
        tpl.add_dependency(new_id, agreement[0])
        for w in waiting:   # steps that waited on the old document step now wait on the management agreement
            try:
                tpl.add_dependency(w, agreement[0])
            except ValueError:
                pass
    set_setting("tpl.onboarding_docs_v1", "done")
    return True


def _install_templates(keys: list[str]) -> list[str]:
    from . import team, tpl
    roles = {r["name"].lower(): r["id"] for r in team.list_roles()}
    added = []
    with tx() as c:
        have = {r[0] for r in c.execute("SELECT name FROM templates")}
    for s in STARTERS:
        if s["key"] not in keys or s["name"] in have:
            continue
        with tx() as c:
            tid = c.execute("INSERT INTO templates(name, description, board_period) VALUES(?,?,?)",
                            (s["name"], s["description"], s.get("board"))).lastrowid
        _build_items(tid, s["items"], roles)
        added.append(s["name"])
    return added
