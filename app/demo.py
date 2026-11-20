"""One-click demo data for trying the app: sample teammates, templates, projects and realistic activity.

Everything created is recorded in a manifest (settings key 'demo.manifest') so 'Remove demo data' deletes exactly
that and nothing else. No Blink messages are sent while loading (blink.quiet()).
"""
import json
import re
import random
from datetime import date, timedelta

from . import blink, collab, dashboard, services, starter_templates, team, timing
from .db import get_setting, set_setting, tx

# Fictional teammates (no Blink accounts, so nobody is messaged while you demo)
PEOPLE = [("Sam Patel", "sam.patel"), ("Jordan Lee", "jordan.lee"), ("Maria Garza", "maria.garza"), ("Alex Rivera", "alex.rivera"),
          ("Dana Whitfield", "dana.whitfield"), ("Priya Shah", "priya.shah"), ("Chris Ogden", "chris.ogden"),
          ("Taylor Brooks", "taylor.brooks")]
# department → (head, members)
DEPARTMENTS = {"Property Management": ("Dana Whitfield", ["Dana Whitfield", "Alex Rivera", "Priya Shah", "Sam Patel"]),
               "Accounting": ("Jordan Lee", ["Jordan Lee", "Maria Garza", "Chris Ogden"]),
               "Leasing": ("Taylor Brooks", ["Taylor Brooks"])}
DEMO_DOMAIN = "demo.example.com"   # never a real inbox


def manifest() -> dict | None:
    raw = get_setting("demo.manifest")
    return json.loads(raw) if raw else None


# Fictional contacts for the sample properties (example.com emails, 555 numbers — none of these are real).
SAMPLE_CONTACTS = {
    "777 E Sonterra Blvd, San Antonio, TX 78258": [
        ("Owner contact", "Margaret Ellison", "Sonterra Commons Owner LLC", "(210) 555-0142", "Prefers email; monthly report by the 15th"),
        ("Asset manager", "Derek Huang", "Hill Country Capital Partners", "(210) 555-0178", "Approves CapEx over threshold"),
        ("CPA", "Linda Okafor, CPA", "Okafor & Reyes CPAs", "(210) 555-0119", "Year-end package & 1099 questions"),
        ("Bank", "Raymond Ortiz", "Lone Star Community Bank", "(210) 555-0163", "Treasury services — operating & deposit accounts"),
        ("Insurance agent", "Karen Whitlock", "Mission Risk Insurance Agency", "(210) 555-0187", "Property & GL renewals"),
        ("Lender", "Paul Brennan", "Alamo Heights Lending", "(210) 555-0131", "Loan servicing — escrow & covenant reporting"),
        ("Tax protest company", "Steve Morales", "Bexar Tax Advisors", "(210) 555-0156", "Files protest by May 15"),
        ("Lawyer", "Rachel Kim", "Kim & Pruitt PLLC", "(210) 555-0124", "Leases, estoppels, collections")],
    "4500 Fredericksburg Rd, San Antonio, TX": [
        ("Owner contact", "Victor Salas", "Fredericksburg Center LLC", "(210) 555-0105", "Seller — call before emailing"),
        ("Owner contact", "Hannah Price", "Alamo Retail Fund II", "(210) 555-0198", "Buyer's contact for the sale"),
        ("CPA", "Greg Lindqvist, CPA", "Lindqvist Accounting", "(210) 555-0137", None),
        ("Bank", "Anita Desai", "River City Bank", "(210) 555-0174", None),
        ("Insurance agent", "Tom Gallegos", "Mission Risk Insurance Agency", "(210) 555-0149", None),
        ("Lawyer", "Brian Foster", "Foster Real Estate Law", "(210) 555-0112", "Handling the purchase & sale agreement")],
    "8800 Research Blvd, Austin, TX": [
        ("Owner contact", "Olivia Grant", "Research Plaza Owner LP", "(512) 555-0161", "Quarterly owner calls"),
        ("Asset manager", "Marcus Webb", "Capitol Ridge Investments", "(512) 555-0127", None),
        ("CPA", "Jennifer Tran, CPA", "Tran & Associates", "(512) 555-0183", None),
        ("Lender", "Chris Delgado", "Hill Country Lending", "(512) 555-0109", "DSCR reporting quarterly"),
        ("Tax protest company", "Laura Benson", "Travis Property Tax Consultants", "(512) 555-0145", None)],
    "1200 Broadway St, San Antonio, TX": [
        ("Owner contact", "Daniel Reyes", "Broadway Lofts LP", "(210) 555-0115", None),
        ("CPA", "Linda Okafor, CPA", "Okafor & Reyes CPAs", "(210) 555-0119", None),
        ("Bank", "Raymond Ortiz", "Lone Star Community Bank", "(210) 555-0163", None),
        ("Insurance agent", "Karen Whitlock", "Mission Risk Insurance Agency", "(210) 555-0187", None)],
}


def add_sample_contacts(m: dict):
    """Adds the fictional contacts to the sample properties (skips ones already there) and records them for Remove."""
    from . import properties
    m.setdefault("contacts", [])
    for addr, people in SAMPLE_CONTACTS.items():
        pid = properties.property_for_address(addr)
        if not pid:
            continue
        with tx() as c:
            have = {(r[0], r[1]) for r in c.execute("SELECT role, name FROM property_contacts WHERE property_id=?", (pid,))}
        for role, name, company, phone, notes in people:
            if (role, name) in have:
                continue
            email = re.sub(r"[^a-z]", "", name.split(",")[0].split()[0].lower()) + "." + \
                re.sub(r"[^a-z]", "", name.split(",")[0].split()[-1].lower()) + "@example.com"
            m["contacts"].append(properties.add_contact(pid, {"role": role, "name": name, "company": company,
                                                               "email": email, "phone": phone, "notes": notes or ""}))


# Backup property managers and property assistants for the sample properties (demo people)
EXTRA_TEAM = {"777 E Sonterra Blvd, San Antonio, TX 78258": {"Backup Property Manager": "Alex Rivera", "Property Assistant": "Priya Shah"},
              "4500 Fredericksburg Rd, San Antonio, TX": {"Backup Property Manager": "Priya Shah", "Property Assistant": "Dana Whitfield"},
              "8800 Research Blvd, Austin, TX": {"Backup Property Manager": "Alex Rivera", "Property Assistant": "Dana Whitfield",
                                                 "Asset Manager": "Sam Patel"},
              "1200 Broadway St, San Antonio, TX": {"Backup Property Manager": "Priya Shah", "Property Assistant": "Dana Whitfield",
                                                    "Asset Manager": "Sam Patel"}}


def add_extra_team(m: dict):
    from . import properties
    handles = dict(PEOPLE)
    with tx() as c:
        roles = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM project_roles")}
        users = {r["email"]: r["id"] for r in c.execute("SELECT id, email FROM users")}
    for addr, team_ in EXTRA_TEAM.items():
        pid = properties.property_for_address(addr)
        if not pid:
            continue
        have = properties.team_map(pid)
        for role, name in team_.items():
            uid = users.get(f"{handles[name]}@{DEMO_DOMAIN}")
            if role in roles and uid and not have.get(roles[role]):
                properties.set_member(pid, roles[role], uid)
    m["extra_team"] = True


# Fictional team wins for demos: (person, icon-kind, what they did, detail). "me" = whoever loaded the demo.
SAMPLE_WINS = [
    ("Chris Ogden", "Collected $18,400 past-due rent from Lone Star Dental", "Suite 300 · 4500 Fredericksburg Rd"),
    ("Taylor Brooks", "Got every tenant COI at 777 E Sonterra current", "All 14 tenants · before the insurance renewal"),
    ("Sam Patel", "Won the tax protest — 777 E Sonterra value cut 8%", "Saves the owner about $21,000 this year"),
    ("Priya Shah", "Roof leak at 8800 Research fixed the same day", "No tenant damage · vendor on site in 2 hours"),
    ("Maria Garza", "Collected every vendor W-9 before year-end", "1099s ready to go in January"),
    ("Jordan Lee", "Sent October owner reports 3 days early", "All 4 properties"),
    ("Alex Rivera", "Cut landscaping cost 12% after rebidding", "4500 Fredericksburg Rd · same scope"),
    ("Dana Whitfield", "Trained two new coordinators on the Task Manager", "Property Management team"),
    ("me", "Onboarded 777 E Sonterra in 28 days", "Every takeover step done before day 30"),
]


def add_sample_wins(m: dict, me_id: int):
    import random as _r
    rnd = _r.Random(23)
    handles = dict(PEOPLE)
    m.setdefault("sample_wins", [])
    with tx() as c:
        users = {r["email"]: r["id"] for r in c.execute("SELECT id, email FROM users")}
        everyone = list(m["users"]) + ([me_id] if me_id else [])
        for who, title, detail in SAMPLE_WINS:
            uid = me_id if who == "me" else users.get(f"{handles[who]}@{DEMO_DOMAIN}")
            if who == "me" and not me_id:
                continue
            if not uid:
                continue
            wid = c.execute("INSERT INTO wins(user_id, kind, ref_id, title, detail, at) VALUES(?,?,?,?,?,datetime('now', ?))",
                            (uid, "shoutout", None, title, detail, f"-{rnd.randint(20, 36 * 60)} minutes")).lastrowid
            m["sample_wins"].append(wid)
            for k in rnd.sample(everyone, k=rnd.randint(1, 4)):
                if k != uid:
                    c.execute("INSERT OR IGNORE INTO win_kudos(win_id, user_id) VALUES(?,?)", (wid, k))


def refresh_wins():
    """The wins feed shows the last 7 days — keep the demo's sample wins recent (once a day)."""
    m = manifest()
    if not m or not m.get("sample_wins") or get_setting("demo.wins_day") == date.today().isoformat():
        return
    import random as _r
    rnd = _r.Random(date.today().toordinal())
    with tx() as c:
        for wid in m["sample_wins"]:
            c.execute("UPDATE wins SET at=datetime('now', ?) WHERE id=?", (f"-{rnd.randint(20, 36 * 60)} minutes", wid))
    set_setting("demo.wins_day", date.today().isoformat())


def fill(me_id: int | None = None) -> dict:
    """Admin button: add whatever an older demo is missing, re-date the wins so they're recent, report counts."""
    m = manifest()
    if not m:
        return {"loaded": False, "wins": 0, "contacts": 0}
    if me_id and not m.get("loaded_by"):
        m["loaded_by"] = me_id
        set_setting("demo.manifest", json.dumps(m))
    backfill_contacts()
    m = manifest()
    with tx() as c:   # wins someone deleted by hand: add them again
        alive = [w for w in m.get("sample_wins", []) if c.execute("SELECT 1 FROM wins WHERE id=?", (w,)).fetchone()]
    if not alive:
        m["sample_wins"] = []
        add_sample_wins(m, m.get("loaded_by") or me_id)
        set_setting("demo.manifest", json.dumps(m))
    set_setting("demo.wins_day", "")
    refresh_wins()
    return {"loaded": True, "wins": len(manifest().get("sample_wins", [])), "contacts": len(m.get("contacts", []))}


def backfill_contacts():
    """Demo data loaded before sample contacts existed gets them once."""
    m = manifest()
    if m and ("contacts" not in m or "extra_team" not in m or not m.get("sample_wins")):
        if "contacts" not in m:
            add_sample_contacts(m)
        if not m.get("sample_wins"):
            me = m.get("loaded_by")
            if not me:   # who loaded the demo: the non-demo property manager, else the first admin
                with tx() as c:
                    r = c.execute("""SELECT user_id FROM property_team WHERE role_id=(SELECT id FROM project_roles WHERE name='Property Manager')
                                     AND user_id NOT IN (SELECT id FROM users WHERE email LIKE ?) LIMIT 1""", (f"%@{DEMO_DOMAIN}",)).fetchone() \
                        or c.execute("SELECT id FROM users WHERE role='admin' AND email NOT LIKE ? ORDER BY id LIMIT 1",
                                     (f"%@{DEMO_DOMAIN}",)).fetchone() \
                        or c.execute("SELECT id FROM users WHERE email NOT LIKE ? ORDER BY id LIMIT 1", (f"%@{DEMO_DOMAIN}",)).fetchone()
                me = r[0] if r else None
            m["sample_wins"] = []
            add_sample_wins(m, me)
        with blink.quiet():
            add_extra_team(m)
        set_setting("demo.manifest", json.dumps(m))


def _task(pid, title_part):
    with tx() as c:
        r = c.execute("SELECT id FROM tasks WHERE project_id=? AND title LIKE ? ORDER BY id LIMIT 1",
                      (pid, f"%{title_part}%")).fetchone()
    return r["id"] if r else None


def load(me_id: int) -> dict:
    if manifest():
        raise ValueError("Demo data is already loaded — remove it first to reload.")
    rnd = random.Random(7)
    m = {"users": [], "projects": [], "tasks": [], "reminders": [], "recurrences": [], "roles_before": {}}
    with blink.quiet():
        # --- people
        ids = {}
        with tx() as c:
            for name, handle in PEOPLE:
                r = c.execute("SELECT id FROM users WHERE email=?", (f"{handle}@{DEMO_DOMAIN}",)).fetchone()
                uid = r["id"] if r else c.execute("INSERT INTO users(name, email, role) VALUES(?,?,'staff')",
                                                  (f"{name} (demo)", f"{handle}@{DEMO_DOMAIN}")).lastrowid
                ids[name] = uid
                m["users"].append(uid)
        # --- role defaults for the demo (remembered so they can be restored)
        plan = {"Property Manager": me_id, "Head of Accounting": ids["Jordan Lee"], "AP": ids["Maria Garza"],
                "AR": ids["Maria Garza"], "Leasing": ids["Alex Rivera"], "Asset Manager": ids["Sam Patel"]}
        for r in team.list_roles():
            if r["name"] in plan:
                m["roles_before"][str(r["id"])] = r["default_user_id"]
                team.update_role(r["id"], {"default_user_id": plan[r["name"]]})

        # --- templates + sample projects
        keys = [s["key"] for s in starter_templates.STARTERS]
        starter_templates._install_templates(keys)
        names = starter_templates.create_samples(keys)
        with tx() as c:
            m["projects"] = [r[0] for r in c.execute(
                f"SELECT id FROM projects WHERE name IN ({','.join('?' * len(names))})", names)] if names else []
        proj = {}
        with tx() as c:
            for pid in m["projects"]:
                proj[c.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()["name"]] = pid
        onb = next((v for k, v in proj.items() if "Sonterra Blvd (Onboarding)" in k), None)
        retain = next((v for k, v in proj.items() if "retain" in k), None)
        movein = next((v for k, v in proj.items() if "Move-In" in k), None)
        close = next((v for k, v in proj.items() if "Month-End" in k), None)

        # --- realistic activity
        with tx() as c:  # varied actual times on completed work, so Time insights has something to show
            done = [r[0] for r in c.execute(
                f"SELECT id FROM tasks WHERE status='done' AND project_id IN ({','.join('?' * len(m['projects']))})",
                m["projects"])] if m["projects"] else []
        notes = ["prior manager slow to send files", "first time with this owner's format", "went smoothly",
                 "waited on bank signature cards", None, None]
        for tid in done:
            with tx() as c:
                est = c.execute("SELECT estimate_minutes FROM tasks WHERE id=?", (tid,)).fetchone()[0] or 60
            timing.set_times(tid, actual=max(15, int(est * rnd.choice([0.6, 0.8, 1.0, 1.2, 1.5, 1.9]))),
                             notes=rnd.choice(notes))
        if onb:
            t = _task(onb, "Open operating")
            if t:
                services.apply_update(t, ids["Jordan Lee"], "web", "blocked", "Bank needs owner's signed resolution")
            t = _task(onb, "Load tenants")
            if t:
                services.apply_update(t, ids["Maria Garza"], "blink", "in_progress", "12 of 18 tenants loaded")
            t = _task(onb, "Lease abstracts")
            if t:
                services.apply_update(t, ids["Alex Rivera"], "web", "in_progress", None)
            t = _task(onb, "Collect property documents")
            if t:
                handle = collab.handle_for({"email": _email(me_id), "name": ""})
                collab.add_comment(t, ids["Jordan Lee"],
                                   f"@{handle} prior manager says the security deposit ledger comes Friday — OK to start tenant setup without it?")
                collab.add_comment(t, ids["Sam Patel"], "Owner asked for a call once we have the rent roll tied out.")
            t = _task(onb, "Send tenant notification")
            if t:
                handle = collab.handle_for({"email": _email(me_id), "name": ""})
                collab.add_comment(t, ids["Maria Garza"], f"@{handle} draft letter is in Drive — need your sign-off on the payment address.")
        if retain:
            t = _task(retain, "Tenant estoppel")
            if t:
                services.apply_update(t, ids["Alex Rivera"], "blink", "blocked",
                                      "3 of 12 estoppels outstanding: Lone Star Dental, Coastal Coffee, Suite 140")
        if movein:
            t = _task(movein, "Tenant COI")
            if t:
                services.apply_update(t, me_id, "web", "in_progress", "Asked tenant's agent for revised COI")
        if close:
            t = _task(close, "Bank reconciliation")
            if t:
                services.apply_update(t, ids["Jordan Lee"], "web", "in_progress", None)

        # --- tasks without a project
        gen = team.general_project_id()
        for title, who, days, est in [("Renew notary commission", me_id, 5, 30),
                                      ("Order new property signage templates", ids["Alex Rivera"], -2, 60),
                                      ("Update owner contact list", ids["Maria Garza"], 3, 45),
                                      ("Review Q4 vendor insurance certificates", me_id, 9, 90)]:
            with tx() as c:
                tid = c.execute("INSERT INTO tasks(project_id, title, assignee_id, due_date, estimate_minutes) VALUES(?,?,?,?,?)",
                                (gen, title, who, (date.today() + timedelta(days=days)).isoformat(), est)).lastrowid
            m["tasks"].append(tid)

        # --- reminders for you
        tomorrow = date.today() + timedelta(days=1)
        for text, d, hm in [("Call Sonterra owner about keys & access cards", tomorrow, "09:30"),
                            ("Quarterly review prep — 777 E Sonterra", date.today() + timedelta(days=3), "14:00")]:
            m["reminders"].append(dashboard.add_reminder(me_id, text, f"{d.isoformat()}T{hm}"))

        # --- property pages + critical dates (777 E Sonterra gets a realistic set)
        from . import properties
        with tx() as c:
            before = {r[0] for r in c.execute("SELECT id FROM properties")}
        properties.link_all()
        with tx() as c:
            m["properties"] = [r[0] for r in c.execute("SELECT id FROM properties") if r[0] not in before]
            hq = c.execute("SELECT property_id FROM projects WHERE id=?", (onb,)).fetchone() if onb else None
        if hq and hq["property_id"]:
            t0 = date.today()
            may15 = date(t0.year + (t0 >= date(t0.year, 5, 15)), 5, 15)
            nxt15 = date(t0.year, t0.month, 15) if t0.day < 15 else (date(t0.year + (t0.month == 12), t0.month % 12 + 1, 15))
            for kind, title, when, rep, who, notes in [
                ("owner_report", "Monthly owner report due", nxt15, "monthly", "Jordan Lee",
                 "P&L, balance sheet, rent roll, aged delinquency and GL detail as one PDF to owner + CPA."),
                ("insurance_renewal", "Property insurance renewal", t0 + timedelta(days=40), "yearly", None,
                 "Get renewal quotes; confirm Foresite stays named additional insured on the new policy."),
                ("loan", "Quarterly lender reporting (rent roll + operating statement)", t0 + timedelta(days=55), "quarterly", "Jordan Lee",
                 "Send to Frost Bank loan servicing within 45 days of quarter end."),
                ("tax_protest", "Property tax protest deadline (Bexar CAD)", may15, "yearly", None,
                 "Review the appraisal notice; file protest or send to the tax protest company."),
                ("budget_due", "Annual operating budget due to owner", t0 + timedelta(days=150), "yearly", "Maria Garza",
                 "Draft budget with CAM estimates; owner wants it 60 days before year start."),
                ("cam_recon", "CAM reconciliation due to tenants", t0 + timedelta(days=130), "yearly", "Jordan Lee",
                 "Within 120 days of year-end.")]:
                did = properties.add_date(hq["property_id"], {
                    "kind": kind, "title": title, "date": when.isoformat(), "repeat": rep, "notes": notes,
                    "assignee_id": ids.get(who) if who else me_id,
                    "quote": "Manager shall deliver monthly reports to Owner no later than the fifteenth (15th) day of each month"
                             if kind == "owner_report" else ""}, me_id, "lease" if kind == "owner_report" else "manual")
                m.setdefault("dates", []).append(did)
            properties.update(hq["property_id"], {
                "spend_limit": 5000,
                "report_notes": "One combined PDF by the 15th: P&L (month + YTD vs budget), balance sheet, rent roll, aged "
                                "delinquency, GL detail. Email to owner and copy the CPA. Variance comments on anything over "
                                "10% / $1,000.",
                "notes": "Owner prefers calls before 10am. Emergency repairs: call owner first if over the spend limit.",
                "bank_name": "Broadway Bank", "bank_accounts": "Operating ••4417 · Security deposits ••9023"})
            for ct in [("Owner contact", "Rebecca Torres", "Sonterra Commons Owner LLC", "rtorres@example.com", "(210) 555-0142"),
                       ("CPA", "Dana Cho", "Cho & Partners CPAs", "dana@example.com", "(210) 555-0188"),
                       ("Bank", "Sam Ortiz (treasury management)", "Broadway Bank", "sortiz@example.com", "(210) 555-0121"),
                       ("Insurance agent", "Pat Lee", "Alamo Commercial Insurance", "pat@example.com", "(210) 555-0110"),
                       ("Lender", "Loan servicing", "Frost Bank", "servicing@example.com", ""),
                       ("Asset manager", "Chris Nguyen", "Alamo Retail Fund II", "cnguyen@example.com", "(210) 555-0175"),
                       ("Tax protest company", "Property tax team", "Texas Tax Protest Partners", "protests@example.com", "(210) 555-0133"),
                       ("Lawyer", "Morgan Ellis", "Ellis & Ruiz PLLC", "mellis@example.com", "(210) 555-0160")]:
                properties.add_contact(hq["property_id"], dict(zip(("role", "name", "company", "email", "phone"), ct)))
            properties.run_alerts()
            with tx() as c:
                m["tasks"] += [r[0] for r in c.execute(
                    f"SELECT task_id FROM critical_dates WHERE task_id IS NOT NULL AND id IN ({','.join('?' * len(m['dates']))})",
                    m["dates"])]

        # --- month-end close board: this month's close at two more properties (different progress) and last month's
        #     close at a third that hasn't started this month yet
        with tx() as c:
            close_tpl = c.execute("SELECT id FROM templates WHERE name='Month-End Close'").fetchone()
        if close_tpl:
            first = date.today().replace(day=1)
            last_month = (first - timedelta(days=1)).replace(day=1)
            for addr, client, start, done_n, blocked in [
                    ("4500 Fredericksburg Rd, San Antonio, TX", "Fredericksburg Center LLC", first, 5, None),
                    ("8800 Research Blvd, Austin, TX", "Research Plaza Owner LP", first, 1, "Enter & approve payables"),
                    ("1200 Broadway St, San Antonio, TX", "Broadway Lofts LP", last_month, 99, None)]:
                pid = services.create_project_from_template(
                    close_tpl[0], f"{start:%B %Y} – Month-End Close ({addr.split(',')[0]})", start,
                    details={"property": addr, "client": client})
                m["projects"].append(pid)
                with tx() as c:
                    tids = [r[0] for r in c.execute("SELECT id FROM tasks WHERE project_id=? AND parent_id IS NULL ORDER BY position, id", (pid,))]
                for n, tid in enumerate(tids):
                    if n < done_n:
                        services.apply_update(tid, ids["Jordan Lee"], "web", "done", None)
                if blocked:
                    t = _task(pid, blocked)
                    if t:
                        services.apply_update(t, ids["Maria Garza"], "web", "blocked", "Waiting on owner to approve the roof invoice")
            properties.link_all()
            with tx() as c:
                m["properties"] += [r[0] for r in c.execute("SELECT id FROM properties") if r[0] not in before and r[0] not in m["properties"]]

        # --- vendors: ratings, current/past use at properties, one on a project
        from . import vendors as V
        if hq and hq["property_id"]:
            hq_id = hq["property_id"]
            fred = properties.property_for_address("4500 Fredericksburg Rd, San Antonio, TX")
            res = properties.property_for_address("8800 Research Blvd, Austin, TX")
            m["vendors"] = []
            def vend(name, trades, contact, phone, notes=""):
                with tx() as c:
                    r = c.execute("SELECT id FROM vendors WHERE lower(name)=lower(?)", (name,)).fetchone()
                if r:
                    return r["id"]
                slug = "".join(ch for ch in name.lower() if ch.isalnum())[:18]
                vid = V.create({"name": name, "trades": trades, "contact_name": contact, "phone": phone, "notes": notes,
                                "email": f"office@{slug}.example.com"}, me_id)
                m["vendors"].append(vid)
                return vid
            green = vend("Green Leaf Landscaping", ["Landscaping"], "Marco Ruiz", "(210) 555-0301", "Weekly service; irrigation checks included.")
            lone = vend("Lone Star Lawn Care", ["Landscaping"], "Dale Pruitt", "(210) 555-0302")
            cps = vend("CPS Energy", ["Utilities – Electric", "Utilities – Gas"], "Commercial accounts", "(210) 353-2222")
            saws = vend("SAWS", ["Utilities – Water"], "Commercial service", "(210) 704-7297")
            wm = vend("Alamo City Waste & Recycling", ["Trash / recycling"], "Kim Tran", "(210) 555-0305", "2x weekly pickup, 8yd containers.")
            wash = vend("Alamo Power Wash", ["Power washing"], "Jess Ortega", "(210) 555-0306", "Quarterly storefronts + sidewalks.")
            roof = vend("Hill Country Roofing", ["Roofing"], "Brian Cole", "(210) 555-0307")
            hvac = vend("Cool Breeze HVAC", ["HVAC"], "Tony Vega", "(512) 555-0308")
            hvac2 = vend("Alamo Air Mechanical", ["HVAC"], "Rita Shah", "(210) 555-0309", "Preventive maintenance contracts; 24/7 line.")
            pest = vend("Bexar Pest Pros", ["Pest control"], "Luis Garza", "(210) 555-0310")
            for prop_id, trade, vid in [(hq_id, "Landscaping", lone), (hq_id, "Landscaping", green), (hq_id, "Utilities – Electric", cps),
                                        (hq_id, "Utilities – Gas", cps), (hq_id, "Utilities – Water", saws), (hq_id, "Trash / recycling", wm),
                                        (hq_id, "Power washing", wash), (hq_id, "Roofing", roof), (hq_id, "HVAC", hvac2),
                                        (hq_id, "Pest control", pest), (fred, "Landscaping", green), (fred, "HVAC", hvac),
                                        (res, "HVAC", hvac), (res, "Power washing", wash)]:
                if prop_id:
                    V.set_property_vendor(prop_id, trade, vid, me_id)
            with tx() as c:   # Lone Star was replaced by Green Leaf in March
                c.execute("UPDATE property_vendors SET started='2024-04-01', ended='2026-03-15' WHERE vendor_id=? AND property_id=?", (lone, hq_id))
                c.execute("UPDATE property_vendors SET started='2026-03-16' WHERE vendor_id=? AND property_id=?", (green, hq_id))
                c.execute("UPDATE property_vendors SET started='2025-01-10' WHERE property_id=? AND started=?", (hq_id, date.today().isoformat()))
            for vid, who, stars, price, note, prop_id in [
                    (green, "Alex Rivera", 5, 4, "Fixed the irrigation leak same day, beds look great.", hq_id),
                    (green, "Sam Patel", 5, 4, "Reliable every week; photos after each visit.", fred),
                    (green, None, 4, 5, "Slightly pricier but worth it.", hq_id),
                    (lone, "Alex Rivera", 2, 2, "Missed two mowing visits in July and slow to call back.", hq_id),
                    (lone, None, 3, 1, "OK on price, inconsistent crews.", hq_id),
                    (wash, "Maria Garza", 5, 3, "Storefronts spotless, worked after hours as asked.", hq_id),
                    (wash, "Sam Patel", 5, 3, "", res),
                    (wm, "Jordan Lee (demo)", 4, 3, "Billing errors twice this year but service is fine.", hq_id),
                    (roof, None, 4, 4, "Good leak repair; report with photos.", hq_id),
                    (hvac, "Sam Patel", 1, 4, "Billed for a compressor replacement that wasn't done. Disputed.", res),
                    (hvac, "Alex Rivera", 3, 3, "Fine for small calls.", fred),
                    (hvac2, "Maria Garza", 5, 4, "PM visits on schedule, clear quotes.", hq_id),
                    (pest, None, 4, 2, "", hq_id)]:
                uid = ids.get(who.replace(" (demo)", "")) if who else me_id
                V.add_review(vid, uid, stars, note, prop_id, price=price)
            if onb and roof:
                V.add_project_vendor(onb, roof, "Roofing", "Roof inspection before takeover", me_id)
            # certificates of insurance at 777 E Sonterra: valid, expiring soon, not naming the property; rest missing
            sample = b"%PDF-1.4\n% sample certificate of insurance (demo)\n"
            for vid, days, named, carrier in [(green, 240, 1, "Travelers"), (wash, 20, 1, "Hartford"), (wm, 300, 0, "Zurich"),
                                              (hvac2, 150, 1, "Liberty Mutual")]:
                k = V.save_coi(hq_id, vid, sample, "ACORD 25 certificate (sample).pdf", "application/pdf", me_id, read=False)
                V.set_coi(k["id"], {"expires": (date.today() + timedelta(days=days)).isoformat(), "insured_ok": named,
                                    "carrier": carrier, "note": "Sample certificate" + ("" if named else
                                    " — owner listed only as certificate holder, not additional insured")})

        # --- the showcase: departments + heads, property teams, blocked reasons, bids + owner approval, wins & badges
        try:
            _showcase(m, me_id, ids, hq["property_id"] if hq else None)
        except Exception:
            import logging
            logging.getLogger("demo").exception("demo showcase step failed")

        # --- a monthly recurring setup (paused, so it doesn't create real projects)
        with tx() as c:
            tpl_id = c.execute("SELECT id FROM templates WHERE name='Month-End Close'").fetchone()
            if tpl_id:
                rid = c.execute("""INSERT INTO recurrences(template_id, name_pattern, day_of_month, active, last_period)
                                   VALUES(?, '{month} {year} – Month-End Close (777 E Sonterra)', 1, 0, ?)""",
                                (tpl_id[0], date.today().strftime("%Y-%m"))).lastrowid
                m["recurrences"].append(rid)

    m["loaded_at"] = date.today().isoformat()
    m["loaded_by"] = me_id
    set_setting("demo.manifest", json.dumps(m))
    return {"people": len(m["users"]), "projects": len(m["projects"]), "loose_tasks": len(m["tasks"]),
            "reminders": len(m["reminders"]), "emails": len(m.get("emails", []))}


def _showcase(m: dict, me_id: int, ids: dict, hq_id):
    """Everything that makes a good live demo, on top of the sample projects."""
    import random as _r
    from . import bids, blockers, emails, properties
    rnd = _r.Random(11)
    U = lambda name: ids[name]
    add_sample_contacts(m)
    # departments and heads (remember what was there so Remove can put it back)
    m.setdefault("dept_before", {})
    m.setdefault("depts_created", [])
    with tx() as c:
        for dname, (head, members) in DEPARTMENTS.items():
            d = c.execute("SELECT id, head_user_id FROM departments WHERE name=?", (dname,)).fetchone()
            if d:
                did = d["id"]
                m["dept_before"][str(did)] = d["head_user_id"]
            else:
                did = c.execute("INSERT INTO departments(name, position) VALUES(?, 99)", (dname,)).lastrowid
                m["depts_created"].append(did)
            if not d or not d["head_user_id"]:
                c.execute("UPDATE departments SET head_user_id=? WHERE id=?", (U(head), did))
            for name in members:
                c.execute("UPDATE users SET department_id=? WHERE id=?", (did, U(name)))
        roles = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM project_roles")}
    # property teams: different property managers own different buildings
    pa = properties.property_for_address
    teams = {hq_id: {"Property Manager": me_id, "Head of Accounting": U("Jordan Lee"), "AP": U("Maria Garza"), "AR": U("Chris Ogden"),
                     "Leasing": U("Taylor Brooks"), "Asset Manager": U("Sam Patel")},
             pa("4500 Fredericksburg Rd, San Antonio, TX"): {"Property Manager": U("Alex Rivera"), "Head of Accounting": U("Jordan Lee"),
                     "AP": U("Maria Garza"), "AR": U("Maria Garza"), "Leasing": U("Taylor Brooks"), "Asset Manager": U("Sam Patel")},
             pa("8800 Research Blvd, Austin, TX"): {"Property Manager": U("Priya Shah"), "Head of Accounting": U("Jordan Lee"),
                     "AP": U("Chris Ogden"), "AR": U("Chris Ogden"), "Leasing": U("Taylor Brooks")},
             pa("1200 Broadway St, San Antonio, TX"): {"Property Manager": U("Alex Rivera"), "Head of Accounting": U("Jordan Lee"),
                     "AP": U("Maria Garza"), "AR": U("Chris Ogden")}}
    for prop, team_ in teams.items():
        if prop:
            for role, uid in team_.items():
                if role in roles:
                    properties.set_member(prop, roles[role], uid)
    add_extra_team(m)
    # blocked tasks that say what they're waiting on
    with tx() as c:
        open_tasks = [dict(r) for r in c.execute(
            f"""SELECT t.id, t.title, t.assignee_id, p.property_id FROM tasks t JOIN projects p ON p.id=t.project_id
                WHERE t.status IN ('todo','in_progress') AND t.parent_id IS NULL AND t.project_id IN ({','.join('?' * len(m['projects']))})
                ORDER BY t.id""", m["projects"])] if m["projects"] else []
        owner = c.execute("SELECT id FROM property_contacts WHERE property_id=? AND role='Owner contact'", (hq_id,)).fetchone() if hq_id else None
        waste = c.execute("SELECT id FROM vendors WHERE name='Alamo City Waste & Recycling'").fetchone()
    plan = [({"kind": "person", "ref": f"contact:{owner['id']}" if owner else None, "label": None if owner else "Owner",
              "reason": "signed management agreement addendum", "follow_up": (date.today() + timedelta(days=2)).isoformat()}, hq_id),
            ({"kind": "person", "ref": f"vendor:{waste['id']}" if waste else None, "label": None if waste else "Waste hauler",
              "reason": "corrected October invoice"}, None),
            ({"kind": "other", "reason": "City of San Antonio sign permit", "follow_up": (date.today() + timedelta(days=5)).isoformat()}, None)]
    used = set()
    for block, prop in plan:
        t = next((x for x in open_tasks if x["id"] not in used and x["assignee_id"] and (prop is None or x["property_id"] == prop)), None)
        if not t:
            continue
        used.add(t["id"])
        try:
            desc = blockers.set_blocked(t["id"], block, t["assignee_id"])
            services.apply_update(t["id"], t["assignee_id"], "web", "blocked", desc)
            with tx() as c:   # make it look like it's been stuck a few days
                c.execute("UPDATE tasks SET blocked_since=datetime('now','-4 days') WHERE id=?", (t["id"],))
                c.execute("UPDATE task_updates SET created_at=datetime('now','-4 days') WHERE task_id=? AND status='blocked'", (t["id"],))
        except ValueError:
            pass
    # a roof project with three bids, a recommendation and an owner approval waiting
    if hq_id:
        with tx() as c:
            addr = c.execute("SELECT address, client FROM properties WHERE id=?", (hq_id,)).fetchone()
            pj = c.execute("""INSERT INTO projects(name, start_date, property, client, description, property_id, created_by)
                              VALUES(?,?,?,?,?,?,?)""", ("777 E Sonterra – Roof replacement", (date.today() - timedelta(days=21)).isoformat(),
                                                          addr["address"], addr["client"], "Roof is 22 years old; two leaks this year.",
                                                          hq_id, me_id)).lastrowid
            roof = c.execute("SELECT id FROM vendors WHERE name='Hill Country Roofing'").fetchone()
        m["projects"].append(pj)
        properties.sync_project(pj)
        for title, who, days, status in [("Roof inspection & moisture survey", me_id, -18, "done"), ("Collect 3 roof bids", me_id, -5, "done"),
                                          ("Owner approval of roof bid", me_id, 3, "blocked"), ("Schedule roof replacement", me_id, 20, "todo"),
                                          ("Notify tenants of roof work", U("Taylor Brooks"), 24, "todo")]:
            with tx() as c:
                tid = c.execute("INSERT INTO tasks(project_id, title, assignee_id, due_date, position) VALUES(?,?,?,?,999)",
                                (pj, title, who, (date.today() + timedelta(days=days)).isoformat())).lastrowid
            if status == "done":
                services.apply_update(tid, who, "web", "done", None)
            elif status == "blocked":
                blockers.set_blocked(tid, {"kind": "person", "ref": f"contact:{owner['id']}" if owner else None,
                                           "label": None if owner else "Owner", "reason": "roof bid approval"}, who)
                services.apply_update(tid, who, "web", "blocked", "Waiting on owner approval of the roof bid")
        b1 = bids.add_bid(pj, {"vendor_id": roof["id"] if roof else None, "vendor_name": None if roof else "Hill Country Roofing",
                               "amount": 48500, "scope": "Tear-off and new 60-mil TPO roof, new flashing and edge metal",
                               "exclusions": "Deck repairs over 200 sq ft", "timeline": "3 weeks, weather permitting",
                               "warranty": "20-year manufacturer NDL + 5-year workmanship",
                               "submitted_on": (date.today() - timedelta(days=6)).isoformat()}, me_id)["id"]
        bids.add_bid(pj, {"vendor_name": "Lone Star Roofing Co", "amount": 41200, "scope": "Silicone coating over existing roof",
                          "exclusions": "Wet insulation replacement", "timeline": "8 working days", "warranty": "10-year coating warranty",
                          "submitted_on": (date.today() - timedelta(days=5)).isoformat()}, me_id)
        bids.add_bid(pj, {"vendor_name": "Apex Commercial Roofing", "amount": 52900, "scope": "Tear-off and new TPO roof, tapered insulation",
                          "timeline": "4 weeks", "warranty": "20-year NDL", "submitted_on": (date.today() - timedelta(days=4)).isoformat()}, me_id)
        bids.set_recommendation(pj, b1, "We recommend Hill Country Roofing at $48,500. Their bid is a full tear-off and replacement "
                                "with a 20-year manufacturer warranty, which addresses the wet insulation found in the moisture survey. "
                                "The $41,200 coating option would trap that moisture and only carries a 10-year warranty. Hill Country "
                                "has repaired leaks here quickly with photo reports, and they're $4,400 below Apex for the same scope.")
        try:
            bids.request_approval(pj, me_id)
        except ValueError:
            pass
    # wins spread over the last few days, high-fives, a few badges and streaks
    add_sample_wins(m, me_id)
    with tx() as c:
        wins = [dict(r) for r in c.execute("SELECT id, user_id FROM wins WHERE user_id IN (%s) OR ref_id IN (%s)" % (
            ",".join("?" * len(m["users"])), ",".join("?" * len(m["projects"]))), m["users"] + m["projects"])]
        for w in wins:
            c.execute("UPDATE wins SET at=datetime('now', ?) WHERE id=?", (f"-{rnd.randint(1, 72 * 60)} minutes", w["id"]))
        everyone = m["users"] + [me_id]
        for w in wins:
            for uid in rnd.sample(everyone, k=rnd.randint(0, 3)):
                if uid != w["user_id"]:
                    c.execute("INSERT OR IGNORE INTO win_kudos(win_id, user_id) VALUES(?,?)", (w["id"], uid))
        from .fun import BADGES
        for name, badges in [("Jordan Lee", ["closer", "early_bird"]), ("Maria Garza", ["early_bird"]),
                             ("Alex Rivera", ["vendor_scout"]), ("Taylor Brooks", ["same_day"]), ("Dana Whitfield", ["unblocker"])]:
            for b in badges:
                c.execute("INSERT OR IGNORE INTO user_badges(user_id, badge, earned_at) VALUES(?,?,datetime('now','-2 days'))", (U(name), b))
                c.execute("INSERT INTO wins(user_id, kind, ref_id, title, detail, at) VALUES(?,?,?,?,?,datetime('now', ?))",
                          (U(name), "badge", None, b, BADGES[b][2], f"-{rnd.randint(60, 3000)} minutes"))
        for name, days in [("Jordan Lee", 12), ("Dana Whitfield", 9), ("Maria Garza", 6), ("Taylor Brooks", 4)]:
            d, n = date.today() - timedelta(days=1), 0
            while n < days:
                if d.weekday() < 5:
                    c.execute("INSERT OR REPLACE INTO user_days(user_id, day, overdue, done) VALUES(?,?,0,?)", (U(name), d.isoformat(), rnd.randint(1, 6)))
                    n += 1
                d -= timedelta(days=1)
    # insurance follow-up emails for the property manager (you, at 777 E Sonterra)
    with tx() as c:
        before = c.execute("SELECT COALESCE(MAX(id),0) FROM email_drafts").fetchone()[0]
    emails.run()
    with tx() as c:
        m["emails"] = [r[0] for r in c.execute("SELECT id FROM email_drafts WHERE id>?", (before,))]


def _email(uid):
    with tx() as c:
        r = c.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
    return (r and r["email"]) or ""


def remove() -> dict:
    m = manifest()
    if not m:
        return {"removed": False}
    for pid in m["projects"]:
        starter_templates.delete_project(pid)
    for tid in m["tasks"]:
        collab.delete_task(tid)
    from . import properties
    for did in m.get("dates", []):
        properties.delete_date(did)
    with tx() as c:
        for vid in m.get("vendors", []):
            for k in c.execute("SELECT path, date_id FROM vendor_cois WHERE vendor_id=?", (vid,)).fetchall():
                properties._remove_file(k["path"])
                if k["date_id"]:
                    c.execute("DELETE FROM critical_dates WHERE id=?", (k["date_id"],))
            for t in ("vendor_reviews", "property_vendors", "project_vendors", "vendor_cois"):
                c.execute(f"DELETE FROM {t} WHERE vendor_id=?", (vid,))
            c.execute("DELETE FROM vendors WHERE id=?", (vid,))
    for pid in m.get("properties", []):   # property pages that only existed because of the sample projects
        with tx() as c:
            empty = not c.execute("SELECT 1 FROM projects WHERE property_id=?", (pid,)).fetchone()
        if empty:
            properties.delete(pid)
    with tx() as c:
        for wid in m.get("sample_wins", []):
            c.execute("DELETE FROM win_kudos WHERE win_id=?", (wid,))
            c.execute("DELETE FROM wins WHERE id=?", (wid,))
        for cid in m.get("contacts", []):
            c.execute("DELETE FROM property_contacts WHERE id=?", (cid,))
        for eid in m.get("emails", []):
            c.execute("DELETE FROM email_drafts WHERE id=?", (eid,))
        for did, head in m.get("dept_before", {}).items():
            c.execute("UPDATE departments SET head_user_id=? WHERE id=?", (head, int(did)))
        for did in m.get("depts_created", []):
            c.execute("UPDATE users SET department_id=NULL WHERE department_id=?", (did,))
            c.execute("DELETE FROM departments WHERE id=?", (did,))
        for rid in m["reminders"]:
            c.execute("DELETE FROM reminders WHERE id=?", (rid,))
        for rid in m["recurrences"]:
            c.execute("DELETE FROM recurrences WHERE id=?", (rid,))
        for role_id, before in m.get("roles_before", {}).items():
            c.execute("UPDATE project_roles SET default_user_id=? WHERE id=?", (before, int(role_id)))
        for kind, ids_ in (("task", m["tasks"]), ("project", m["projects"])):
            c.executemany("DELETE FROM win_kudos WHERE win_id IN (SELECT id FROM wins WHERE kind=? AND ref_id=?)", [(kind, i) for i in ids_])
            c.executemany("DELETE FROM wins WHERE kind=? AND ref_id=?", [(kind, i) for i in ids_])
        for uid in m["users"]:
            for t in ("win_kudos", "user_badges", "user_days"):
                c.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
            c.execute("DELETE FROM win_kudos WHERE win_id IN (SELECT id FROM wins WHERE user_id=?)", (uid,))
            c.execute("DELETE FROM wins WHERE user_id=?", (uid,))
        for uid in m["users"]:  # demo people: unassign anything left and remove them
            c.execute("UPDATE tasks SET assignee_id=NULL WHERE assignee_id=?", (uid,))
            c.execute("UPDATE template_items SET default_assignee_id=NULL WHERE default_assignee_id=?", (uid,))
            c.execute("UPDATE task_updates SET user_id=NULL WHERE user_id=?", (uid,))
            c.execute("UPDATE task_comments SET user_id=NULL WHERE user_id=?", (uid,))
            c.execute("DELETE FROM project_team WHERE user_id=?", (uid,))
            c.execute("DELETE FROM property_team WHERE user_id=?", (uid,))
            c.execute("DELETE FROM users WHERE id=?", (uid,))
    set_setting("demo.manifest", "")
    return {"removed": True, "projects": len(m["projects"]), "people": len(m["users"])}


def status() -> dict:
    m = manifest()
    return {"loaded": bool(m), "loaded_at": (m or {}).get("loaded_at"), "projects": len((m or {}).get("projects", []))}

