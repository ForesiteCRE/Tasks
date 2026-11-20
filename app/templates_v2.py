"""Starter templates rebuilt from Foresite's own lists (Nov 2026):
  • New Assignment Check List (Property Manager) — Administrative / Accounting-Property set-up / Site activity
  • Property Assumption/Transition Plan w/ Deadlines — who does what (Manager, Owner, Prior management, All)
  • Accounting Task Checklist — AppFolio procedures, monthly review, owner report packets, cash forecast, training
Imported by starter_templates.py, which owns installing them.
"""
PM, ACCT, AP, AR, LEASING, AM = "Property Manager", "Head of Accounting", "AP", "AR", "Leasing", "Asset Manager"

# ---------------------------------------------------------------- Property Onboarding
# Start date = management commencement (≈30 days before the assumption date). Takeover day = day 30.
ONBOARDING_ITEMS = [
    {"title": "Fully executed management agreement on file", "role": AM, "days": 0, "est": 30,
     "checklist": ["Signed by owner and us", "Copy to Property Management and Accounting", "Fee schedule & leasing commissions noted",
                   "Owner reporting requirements noted", "Approval/spending limits noted", "Insurance requirements noted"]},
    {"title": "Introduction to ownership", "role": PM, "days": 3, "est": 60, "after": ["Fully executed management agreement on file"],
     "checklist": ["Owner's view of what's right and wrong with the property", "Owner's instructions for mortgage payments (before our first payment)",
                   "Owner's instructions for insurance premium payments", "Owner's insurance procedures", "Owner's auditors (if any)",
                   "Owner's real estate tax counsel / protest company", "Owner report preferences (owner packet, monthly or quarterly)",
                   "Chart of accounts — prior manager's COA, owner preferences"]},
    "DOC_REQUESTS",   # Property Onboarding Document Requests (one subtask per document) — inserted by starter_templates
    {"title": "Site inspection & property condition walk", "role": PM, "days": 5, "est": 240, "checklist": [
        "Site inspection", "Vacancies inspected", "Safety inspection", "Verify all meters (current electric index)", "Verify electric sub-metering"],
     "subtasks": [{"title": "Photo-document roof, parking lot, common areas & signage", "role": PM, "days": 5, "est": 120},
                  {"title": "Inventory keys, locks & access systems", "role": PM, "days": 6, "est": 60}]},
    {"title": "Set up property in AppFolio", "role": ACCT, "days": 7, "est": 240, "after": ["Fully executed management agreement on file"],
     "subtasks": [
        {"title": "Add the owner", "role": ACCT, "days": 7, "est": 30, "checklist": [
            "AppFolio → New Owner: company name, “use company name as display name” (no salutation/first/last)",
            "Contact info: Foresite mailing address — leave email & phone blank",
            "Federal tax: taxpayer name & ID (99-9999999 until we have it) — uncheck Send 1099",
            "Owner packet set to the owner's preferences", "Properties owned & ownership %"]},
        {"title": "Add the property, units & square footage", "role": ACCT, "days": 7, "est": 45, "checklist": [
            "Property added", "Units added", "Unit square footage verified (validate building areas)", "Property group (assignment) set"]},
        {"title": "Bank accounts", "role": ACCT, "days": 10, "est": 30, "checklist": [
            "Bank account added (Z-Sample Bank Account until the real one is available)", "Enabled for online payments",
            "Added to check scan", "Don't post security-deposit receipts or JE reversals until the right account is linked"]},
        {"title": "Tenants, leases & charges", "role": AR, "days": 12, "est": 180, "checklist": [
            "Tenants set up", "Lease charges entered (tenant charge register)", "Special billings", "Percentage rent information",
            "TI reimbursements", "Security deposits billed & receipts applied", "Tenants that require deposit interest identified"]},
        {"title": "Opening entries & balances", "role": ACCT, "days": 14, "est": 120, "checklist": [
            "Opening financial entries", "Carry-over receivables from prior management (with backup)", "Prepaid charges by tenant",
            "Security deposit register & funds received", "Approved unpaid bills & commitments"]},
        {"title": "Vendors & payables", "role": AP, "days": 14, "est": 60, "checklist": [
            "Active vendors added", "Vendor W-9s & COI expiration dates", "Recurring bills set up", "Utility accounts"]},
        {"title": "Budget, loan, taxes & insurance", "role": ACCT, "days": 14, "est": 60, "checklist": [
            "Current-year budget entered", "Mortgage information", "Past / pending tax payments", "Past / pending insurance entries",
            "Depreciation & amortization (if applicable)"]},
        {"title": "Reporting set-up", "role": ACCT, "days": 20, "est": 45, "checklist": [
            "Statement template", "Owner report package per owner: cover sheet or executive summary, AppFolio report, bank rec & statement, any extras",
            "Monthly operating & financial reports organized"]},
     ]},
    {"title": "Bank account approved by owner (signature cards)", "role": ACCT, "days": 10, "est": 30,
     "checklist": ["Depository approved by owner (owner may open a new account)", "Signature cards complete", "Online access & positive pay set"]},
    {"title": "Lease administration", "role": PM, "days": 14, "est": 480, "after": ["Fully executed management agreement on file"], "checklist": [
        "Copies of all leases, amendments & letter agreements", "Every lease abstracted (critical dates, options, CAM caps & exclusions, co-tenancy, insurance, notice addresses)",
        "Leases entered in AppFolio", "Completed abstracts sent to the leasing team", "Payment history reviewed — tenants needing close collection flagged"]},
    {"title": "Vendors & service contracts", "role": PM, "days": 14, "est": 240, "checklist": [
        "Service contracts in place obtained", "Vendors notified of new management — intent to review contracts (don't accidentally assume a contract)",
        "Contracts reviewed on cost, form & provisions", "New vendor COIs naming Foresite", "Vendor list & emergency contractor list",
        "Landscaping", "Janitorial / porter", "Parking lot sweeping", "HVAC PM", "Fire/life safety", "Pest control", "Trash", "Security / alarm monitoring"]},
    {"title": "Tenant welcome / notification letter", "role": PM, "days": 15, "est": 120, "after": ["Bank account approved by owner (signature cards)"],
     "checklist": ["Tenant names & addresses in a mail-merge file", "Letter drafted (owner option to sign; owner approves distribution)",
                   "Payment instructions", "Request for tenant info & COI", "Tenant survey included", "Tenant contact list (day/night phones) validated — copy to agent, security & file"]},
    {"title": "Initiate lease billings", "role": AR, "days": 19, "est": 60, "after": ["Set up property in AppFolio"],
     "checklist": ["Statements go out on the 20th of each month (unless noted otherwise)"]},
    {"title": "Notify utilities, agencies & insurance of new management", "role": PM, "days": 20, "est": 90, "checklist": [
        "Utilities — change of management (YTD utility bills on file)", "Local police & fire (emergency contacts)", "Security service",
        "Insurance agent", "Tax assessor — owner's tax ID", "State comptroller — property / franchise taxes"]},
    {"title": "Tenant COIs obtained & verified", "role": PM, "days": 23, "est": 120, "after": ["Tenant welcome / notification letter"]},
    {"title": "Insurance: management named on the owner's policies", "role": AM, "days": 25, "est": 30,
     "checklist": ["Property & liability policies", "Workers' comp & other coverage (owner arranges)", "Certificate on file"]},
    {"title": "Emergency procedures", "role": PM, "days": 25, "est": 60,
     "checklist": ["Emergency lists prepared", "After-hours emergency line", "Emergency contractor services set"]},
    {"title": "Takeover day: keys, signs & confirm everything transferred", "role": PM, "days": 30, "est": 120,
     "after": ["Tenant welcome / notification letter", "Notify utilities, agencies & insurance of new management", "Vendors & service contracts"],
     "checklist": ["Master keys issued to manager & agent (owner)", "Management & leasing signs ordered (entrance, directory)",
                   "Utilities transferred", "Everything from prior management received or scheduled"]},
    {"title": "Meet tenants & contractors", "role": PM, "days": 35, "est": 240,
     "checklist": ["Property manager meets every tenant", "Meet contractors on site"]},
    {"title": "Building systems review", "role": PM, "days": 40, "est": 240, "checklist": [
        "Preventive maintenance program & status (YTD records)", "Mechanical systems in fire mode — staff familiar", "Elevator inspections current",
        "Lease-mandated overtime HVAC scheduled", "Irrigation system working", "Equipment warranties verified", "Fire extinguisher testing current",
        "Kitchen exhaust duct cleaning schedule", "Building areas validated"]},
    {"title": "Receive final accounting & deposits from prior manager", "role": ACCT, "days": 45, "est": 180,
     "after": ["Takeover day: keys, signs & confirm everything transferred"],
     "checklist": ["Carry-over receivables agreed (with backup)", "Security deposit funds transferred & reconciled", "Prepaid charges by tenant",
                   "Monthly accounting reports year to date", "Bank statements through cut-off"]},
    "DOC_FOLLOWUP",   # (none — the document request task already covers "at takeover" items)
    {"title": "First monthly owner report delivered", "role": ACCT, "days": 60, "est": 240,
     "after": ["Receive final accounting & deposits from prior manager"]},
    {"title": "90-day review with owner", "role": AM, "days": 90, "est": 90},
]
ONBOARDING_ITEMS = [x for x in ONBOARDING_ITEMS if x != "DOC_FOLLOWUP"]

# extra documents from the Transition Plan, added to "Property Onboarding Document Requests"
EXTRA_DOCS = [  # (section, document, role, prior-to-takeover?, at-takeover?)
    ("Tenant", "Escalation work papers for the past year, incl. gross-up policy", ACCT, 1, 0),
    ("Tenant", "Escalation (base-year) calculations by tenant, if available", ACCT, 1, 0),
    ("Tenant", "Prepaid charges by tenant", AR, 0, 1),
    ("Tenant", "Backup for carry-over receivables", AR, 0, 1),
    ("Tenant", "Percentage rent information", AR, 1, 0),
    ("Tenant", "TI reimbursements owed", ACCT, 1, 0),
    ("Accounting", "Prior-year audited financial statements", ACCT, 1, 0),
    ("Accounting", "Approved unpaid bills & commitments", AP, 0, 1),
    ("Building", "Base building drawings — mechanical, electrical, plumbing, fire protection & structural", PM, 1, 0),
    ("Building", "Project files", PM, 1, 0),
    ("Building", "Preventive maintenance & inspection records (YTD)", PM, 0, 1),
    ("Owner", "Ground lease (if any)", PM, 1, 0),
    ("Owner", "Mortgage / loan documents", ACCT, 1, 0),
    ("Owner", "Real estate tax data, incl. local & state account numbers / PIN", PM, 1, 0),
    ("Legal", "Detailed report of any cases in the hands of an attorney", PM, 1, 0),
]

# ---------------------------------------------------------------- Month-End Close (Accounting procedures)
MONTH_END_ITEMS = [
    {"title": "Post tenant receipts & bank deposits", "role": AR, "days": 2, "est": 120, "checklist": [
        "ACH receipts entered & allocated to current charges", "Check-scan deposits closed and receipts created",
        "Next month's rent entered as a prepayment", "NSF checks recorded", "Short pays applied the same way as prior months"]},
    {"title": "Late fees & delinquency", "role": AR, "days": 6, "est": 60,
     "checklist": ["Late fees calculated & charged", "Aged receivable detail reviewed — collection issues to the PM"]},
    {"title": "Journal entries & reclasses", "role": ACCT, "days": 6, "est": 60,
     "checklist": ["Recurring journal entries posted", "Reclasses entered", "Bulk JE upload (CSV) if needed"]},
    {"title": "Final bank reconciliation (month end)", "role": ACCT, "days": 8, "est": 60, "after": ["Post tenant receipts & bank deposits"],
     "checklist": ["Bank statement saved to the Bank Recs folder", "Deposits & payments matched — missing items posted",
                   "Reconciliation report saved as “CODE MM.YY Bank Rec”"]},
    {"title": "Monthly financial review", "role": ACCT, "days": 9, "est": 90, "after": ["Final bank reconciliation (month end)"], "checklist": [
        "Financial Diagnostics — security deposit mismatch; apply open credits & prepayments", "Aged receivable detail — collection issues to the PM",
        "Tenant transaction report — ending balances", "General ledger — 3 months: recurring expenses & JEs posted",
        "Annual budget comparison — current-month variances noted", "Rent roll — upcoming expirations; no new leases missing",
        "Ending cash ties to the bank reconciliation"]},
    {"title": "Close the month", "role": ACCT, "days": 10, "est": 30, "after": ["Monthly financial review", "Journal entries & reclasses"]},
    {"title": "Executive summary (or cover page)", "role": PM, "days": 10, "est": 60,
     "checklist": ["Budget vs actual variance commentary", "Leasing & property updates"]},
    {"title": "Compile owner report packet", "role": ACCT, "days": 12, "est": 60, "after": ["Close the month", "Executive summary (or cover page)"],
     "checklist": ["Executive summary or cover page saved as PDF", "AppFolio owner packet — Preview (don't use Print 1 Packet)",
                   "Bank rec & statement copied in", "Any extras for this property (inspection report, site plan, insurance, delinquency, distribution schedule, loan statement)",
                   "Combined into one PDF: “MM.YY Mgmt Report - Property”", "Working files moved to To Delete"]},
    {"title": "Review & send owner report", "role": ACCT, "days": 13, "est": 30, "after": ["Compile owner report packet"],
     "checklist": ["Reviewed", "Sent per the owner's preference (email / portal)", "Owner distribution, if any"]},
    {"title": "Interim bank reconciliation (pre-rec)", "role": ACCT, "days": 16, "est": 45,
     "checklist": ["Deposits & payments matched to bank activity", "Missing items posted or researched"]},
    {"title": "Approve recurring bills (around the 22nd)", "role": AP, "days": 21, "est": 60, "checklist": [
        "AP report by vendor — current + 2 prior months", "Same amounts & coding as prior months", "Changes flagged to the PM (still approve)",
        "Invoices reviewed & approved"]},
    {"title": "Payments processed (25th)", "role": AP, "days": 24, "est": 30, "after": ["Approve recurring bills (around the 22nd)"]},
]

MOVE_IN_ITEMS = [
    {"title": "Fully executed lease & guaranty on file", "role": LEASING, "days": 0, "est": 15},
    {"title": "Lease abstract & critical dates entered", "role": LEASING, "days": 3, "est": 60},
    {"title": "Set up tenant & lease charges in AppFolio", "role": AR, "days": 5, "est": 60, "after": ["Lease abstract & critical dates entered"],
     "checklist": ["Tenant set up", "Lease charges entered (base rent, free-rent periods)", "CAM / tax / insurance estimates",
                   "Security deposit billed & receipt applied"]},
    {"title": "Tenant COI received (meets lease requirements)", "role": PM, "days": 10, "est": 20},
    {"title": "Coordinate TI / make-ready", "role": PM, "days": 20, "est": 240, "checklist": ["Permits", "Contractor COIs", "Inspections", "Punch list"]},
    {"title": "Utilities transferred to tenant", "role": PM, "days": 25, "est": 30},
    {"title": "Move-in walk, keys & welcome packet", "role": PM, "days": 30, "est": 60,
     "after": ["Tenant COI received (meets lease requirements)", "Coordinate TI / make-ready"]},
    {"title": "Pay leasing commission(s)", "role": AP, "days": 35, "est": 20},
]

MOVE_OUT_ITEMS = [
    {"title": "Confirm move-out date & lease surrender obligations", "role": PM, "days": 0, "est": 30,
     "checklist": ["Normal lease end or early termination?", "Termination agreement / fee (early termination)"]},
    {"title": "Notify leasing team: space coming available", "role": LEASING, "days": 1, "est": 10},
    {"title": "Pre-reconciliation for tenant (CAM / tax / insurance)", "role": ACCT, "days": 20, "est": 60},
    {"title": "Move-out inspection & photos", "role": PM, "days": 30, "est": 60},
    {"title": "Keys returned & locks changed", "role": PM, "days": 30, "est": 30},
    {"title": "Utilities back to owner", "role": PM, "days": 31, "est": 30},
    {"title": "Move-out accounting & security deposit refund", "role": AR, "days": 45, "est": 60,
     "after": ["Move-out inspection & photos", "Pre-reconciliation for tenant (CAM / tax / insurance)"],
     "checklist": ["Final charges & credits", "Deposit applied / refund calculated", "Refund or charge letter sent", "Tenant moved out in AppFolio"]},
    {"title": "Final CAM reconciliation for tenant", "role": ACCT, "days": 60, "est": 60},
]

CASH_REFORECAST = {
    "key": "cash_forecast", "name": "Cash Reforecast",
    "description": "At the owner's request or to calculate a distribution: beginning cash ± YTD cash flow → estimated year-end cash.",
    "items": [
        {"title": "Copy the approved budget into the cash flow report", "role": ACCT, "days": 0, "est": 30},
        {"title": "Replace budget with actuals through the last closed month", "role": ACCT, "days": 1, "est": 60,
         "checklist": ["Expenses over $500 budgeted but not yet used moved into the upcoming month (hold the funds)"]},
        {"title": "Update insurance & property tax estimates", "role": ACCT, "days": 1, "est": 30,
         "checklist": ["Insurance (if not yet paid)", "Property taxes — appraisal district estimate if values are set"]},
        {"title": "Confirm large projects with the PM", "role": PM, "days": 2, "est": 15},
        {"title": "Confirm the rent roll & income through year end", "role": ACCT, "days": 2, "est": 45,
         "checklist": ["Move-ins & move-outs updated", "Rent / NNN tabs calculating correctly"]},
        {"title": "Running cash balance & distribution recommendation", "role": ACCT, "days": 3, "est": 30,
         "after": ["Replace budget with actuals through the last closed month", "Confirm the rent roll & income through year end"]},
        {"title": "Send to owner", "role": ACCT, "days": 4, "est": 15, "after": ["Running cash balance & distribution recommendation"]},
    ]}

def _training(section, skills, days):
    return {"title": section, "role": ACCT, "days": days, "est": 60 * len(skills),
            "subtasks": [{"title": f"{section} · {s}", "role": ACCT, "days": days, "est": 45} for s in skills]}

TRAINING = {
    "key": "acct_training", "name": "Accounting New Hire Training",
    "description": "Accounting procedures sign-off for a new team member (AppFolio, banking, reporting). Start date = first day. Assign each item to the trainee.",
    "items": [
        _training("Charges", ["Entering a one-time charge", "Entering / uploading a charge batch", "Setting up recurring charges",
                              "Calculating / charging late fees", "Reversing charges"], 5),
        _training("A/R", ["Entering tenant receipts / new bank deposit", "Scanning tenant checks (check scan)", "Applying prepayments",
                          "Reversing a deposit", "Recording an NSF check", "AR research", "Printing tenant statements",
                          "Printing / emailing tenant ledgers"], 10),
        _training("A/P", ["Adding / editing vendors", "Smart Bill — invoice coding", "Smart Bill — uploading invoices", "Approving recurring bills",
                          "Printing checks in the office", "Reclassing an expense", "Voiding a check", "AP research", "Processing 1099s / 1096s"], 15),
        _training("GL", ["Entering journal entries", "Uploading bulk journal entries", "Closing the month", "Entering budgets",
                         "Bank reconciliations — interim", "Bank reconciliations — end of month"], 20),
        _training("Property set-up", ["Adding an owner", "Adding a property", "Adding units", "Updating unit square footage", "Adding a bank account",
                                      "Enabling a bank account for online payments", "Adding a bank account to check scan",
                                      "Adding / editing a property group (reassignment)"], 25),
        _training("Lease admin", ["Setting up a tenant", "Entering lease charges", "Billing a security deposit / applying the receipt",
                                  "Updating the tenant page with amendments"], 30),
        _training("Move-outs", ["Normal lease end", "Early termination", "Pre-reconciliation for tenant", "Move-out accounting / deposit refund"], 35),
        _training("Excel & reporting", ["Budgets", "CAM reconciliations", "Compiling owner reports", "Cash forecast"], 40),
        _training("Banking & access", ["Bank (TCB) — new user set-up & token", "Bank (TCB) — wire set-up & approval", "Property assignments"], 5),
    ]}
