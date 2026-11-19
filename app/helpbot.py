"""'Ask the Task Manager' — an in-app help assistant (Claude) that explains how the app works and looks things up
in the live data with read-only tools that respect the asker's permissions.

Links in answers use app routes the page understands: (#task-12) (#project-5) (#property-3) (#vendor-7) (#view-calendar).
"""
import json
import logging
from datetime import date

from .db import rows, tx

log = logging.getLogger("helpbot")

GUIDE = """
FORESITE TASK MANAGER — HOW IT WORKS (for answering staff questions)

MENU (computer: top bar · phone: bottom tabs Home / Properties / Projects / Tasks / More; top bar order Home · Properties ▾ · Projects ▾ · Tasks ▾ · Admin ▾)
- Home: "My day" (today ring = tasks due today or earlier; 🔥 streak = workdays with nothing overdue; badges; agenda; blocked for me;
  waiting on me; reminders; comments & @mentions) and "Team" (managers: workload, projects, overdue, blocked, unassigned). Team wins feed
  with 👏 high-fives and each person's progress ring. Number boxes are clickable.
- Tasks ▾: All tasks (filter by person/project/status; quick add at top; tasks can have no project) · Calendar (Month grid or List;
  phones tap a day) · By person (managers) · In Blink, "add task <what> at <property> for <person> by <day>" creates one (property/project optional; managers can assign anyone and add to a project, staff add for themselves). Add a task (Tasks page): property and project are both optional — pick a property to narrow the project list, or leave both blank for one-off work; it can still be assigned to anyone. Tasks page filters by person, property, project and status. Home agenda: the "showing" menu sets how far ahead it looks (7 days → everything; overdue always shows). · 📊 Tracking boards (properties × a template's steps — Month-End Close monthly, Quarterly Inspection quarterly, Budget/CAM/Tax protest/Year-end yearly, Onboarding and Sale hand-off ongoing; tabs pick the board, ‹ › change the period, "Start" for properties missing this period. Managers choose which templates get a board in Admin ▾ → Templates → "📊 Tracking board". Property team now sits at the top right of each property page).
- Projects ▾: Projects (grouped by property, or Board: Not started / In progress / Waiting on owner / Done; quick filters Mine, Needs
  attention, Waiting on owner, Repeating, Finished; click a row to open the project panel) · Templates (reusable checklists, managers).
- Properties ▾: Properties (one page per building) · Vendors · ✉️ Emails to send.
- Admin ▾: Recently deleted & history (managers) · People & departments, Reports & automations, Blink simulator (admin only).

TASKS
- Open a task to edit: status, assignee, due date, repeats (weekly/2 weeks/monthly/quarterly/yearly — next one is created when done),
  checklist, subtasks, "waiting on" (dependencies), comments with @mentions (sends a Blink message), time estimate/actual, how-to link.
- Marking Blocked asks "Blocked by what?": another task (auto-unblocks when that's done), a person/company (property contacts, vendors,
  teammates), or a short reason; optional follow-up date creates a reminder.
- Deleting: managers only, in the Danger zone at the bottom of the task panel (tick to confirm). Deleted items can be restored for 30
  days from Admin ▾ → Recently deleted & history (Undo also appears right after deleting).

PROJECTS
- "+ New project" (managers): name, template, start date, repeats (monthly/quarterly/yearly from a template), property address, client,
  team (filled from the property's team). "From document": upload e.g. a management agreement and Claude fills it in.
- Project panel: details, task preview + "+ Add task", 💰 Bids & owner approval, 🧰 Vendors, team (from the property), Danger zone.
- Bids & owner approval: add bids (upload a PDF and Claude reads total/scope/exclusions/timeline/warranty), tap the round button to
  mark the recommended bid, write a recommendation (✨ Draft with Claude), then "📨 Request owner approval": creates a no-login approval
  link and drafts the email to the owner contact. The owner approves/chooses another bid/requests changes/declines with an
  e-signature checkbox; a 📜 Approval certificate records time, IP, device and fingerprints of the bids.
- The owner approval threshold on a property is a reminder for capital expenditures and non-emergency repairs only (not operating
  expenses) — nothing is blocked by it.

PROPERTIES (property page)
- Header badges: approval threshold, property insurance, our COI, vendor COIs needed.
- ⚑ Critical dates (tax protest, insurance renewal, owner reports, budget, CAM, lender reporting, management agreement, inspections,
  COI expirations); can repeat; reminders 90/30/7 days to the property's Property Manager; a task is created at the first reminder.
- 📄 Upload & read a document (management agreement, insurance policy, tax notice): Claude pulls dates, approval threshold, reporting
  requirements, bank, contacts, insurance we must carry — you tick what to add.
- 📋 Owner requirements · 🏦 Banking (account numbers last 4 only; managers/admins only) · 🛡 Insurance & documents (checks Foresite is
  additional insured on the property policy) · 🏢 Our insurance (Foresite's own COI for this property: requirements, upload, Claude
  checks owner named + limits; "Request certificate from our agent") · 📇 Contacts (owner, asset manager, CPA, bank, insurance agent,
  lender, tax protest company, lawyer) · 👥 Property team (roles: Property Manager, Head of Accounting, AP, AR, Leasing, Asset Manager —
  every project at the address follows it) · 🧰 Vendors (slots per service; Change keeps history; ⭐ Rate; vendor COI per property with
  📎 Upload COI and ✉️ Email vendor) · 📁 Projects · ✅ Open tasks · Danger zone (type name to delete).

VENDORS
- Vendors page: ★ rating, $–$$$$$ price, 👍 Recommended / ⚠️ Mixed / Not recommended badges, where used now/before. Anyone can add
  vendors and rate them. Vendor COIs are per property (each property must be named). Utilities don't need COIs.
- 🧪 Load sample vendors (managers) for practice.

EMAILS TO SEND
- The app drafts emails for the property manager: vendor COI missing/expired/expiring/not naming the owner, property insurance problems,
  owner approval requests, our COI requests. Review & send → Open in Gmail, Mark sent, or Dismiss. "Connect Gmail" puts drafts straight
  into Gmail Drafts (the app never sends on its own).

BLINK (chat)
- Each person has a private chat with the Task bot. Say things like "done 12", "12 is blocked waiting on the owner", "comment 12 thanks
  @sam", "list", "remind me Friday 9am to call the owner", or plain English. Managers can start a project by chatting or sending a
  document. The bot sends reminders, a 6pm daily recap, a Monday week-ahead, @mention alerts, "your turn" when a task you waited on is done.

ESCALATIONS & AUTOMATIONS
- Department heads (Admin → People & departments) get a daily digest when their department's tasks are overdue/blocked too long or a
  critical date passed unhandled. Managers get Blocked alerts. Settings: Admin → Reports & automations.

ROLES
- Staff: see everything except admin areas; update their own tasks; add tasks for themselves; rate/add vendors; people on a property's or
  project's team can manage its vendors, bids and COIs. Managers: create projects, assign anything, templates, delete, restore.
  Admin: also People & departments, Reports & automations.

MY SETTINGS (Home → ⚙ My settings / More → My settings): 6pm daily recap, Monday week-ahead, calendar feed for Google/Outlook.
"""

TOOLS = [
    {"name": "find_tasks", "description": "Search tasks. Returns up to 15 with id, title, status, assignee, due date, project, blocked reason.",
     "input_schema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "words in the title or project"},
         "person": {"type": "string", "description": "assignee name, or 'me'"},
         "status": {"type": "string", "enum": ["open", "todo", "in_progress", "blocked", "done", "any"]},
         "property": {"type": "string", "description": "property name or address words"},
         "overdue_only": {"type": "boolean"}}}},
    {"name": "property_info", "description": "Facts about one property: team, upcoming critical dates, vendors (ratings, COI status), owner "
                                             "requirements, contacts, insurance status, our COI status, open task counts.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "find_vendors", "description": "Search vendors by name or trade; ratings, price level, badge, where used.",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}, "trade": {"type": "string"}}}},
    {"name": "project_info", "description": "One project's progress, next tasks, blocked tasks, bids and owner-approval status.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "my_day", "description": "The asking user's day: today ring, overdue, due today, blocked, streak.",
     "input_schema": {"type": "object", "properties": {}}},
]


def _like(s):
    return f"%{(s or '').strip().lower()}%"


def _find_property(name: str):
    with tx() as c:
        return c.execute("""SELECT id FROM properties WHERE lower(name) LIKE ? OR lower(address) LIKE ? OR lower(COALESCE(client,'')) LIKE ?
                            ORDER BY length(name) LIMIT 1""", (_like(name), _like(name), _like(name))).fetchone()


def run_tool(name: str, args: dict, user: dict) -> dict:
    from . import bids, fun, properties, vendors
    uid, is_mgr = user["id"], user.get("role") in ("manager", "admin")
    today = date.today().isoformat()
    if name == "find_tasks":
        q = """SELECT t.id, t.title, t.status, t.due_date, COALESCE(u.name,'Unassigned') AS assignee, p.name AS project, p.is_general,
                      pr.name AS property, t.blocked_kind, t.blocked_label, t.blocked_reason
               FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN users u ON u.id=t.assignee_id
               LEFT JOIN properties pr ON pr.id=p.property_id WHERE 1=1"""
        a = []
        st = args.get("status") or "open"
        if st == "open":
            q += " AND t.status!='done'"
        elif st != "any":
            q += " AND t.status=?"
            a.append(st)
        if args.get("text"):
            q += " AND (lower(t.title) LIKE ? OR lower(p.name) LIKE ?)"
            a += [_like(args["text"])] * 2
        person = (args.get("person") or "").strip()
        if person.lower() in ("me", "my", "mine", "i"):
            q += " AND t.assignee_id=?"
            a.append(uid)
        elif person:
            q += " AND lower(COALESCE(u.name,'')) LIKE ?"
            a.append(_like(person))
        if args.get("property"):
            q += " AND (lower(COALESCE(pr.name,'')) LIKE ? OR lower(COALESCE(p.property,'')) LIKE ?)"
            a += [_like(args["property"])] * 2
        if args.get("overdue_only"):
            q += " AND t.status!='done' AND t.due_date IS NOT NULL AND t.due_date<?"
            a.append(today)
        with tx() as c:
            ts = rows(c.execute(q + " ORDER BY t.due_date IS NULL, t.due_date LIMIT 15", a))
        from .blockers import text as bt
        for t in ts:
            t["blocked"] = bt(t) if t["status"] == "blocked" else None
            t["link"] = f"#task-{t['id']}"
            for k in ("blocked_kind", "blocked_label", "blocked_reason"):
                t.pop(k, None)
        return {"tasks": ts, "today": today}
    if name == "property_info":
        r = _find_property(args.get("name", ""))
        if not r:
            return {"error": "No property matches that name."}
        p = properties.get(r["id"])
        out = {"name": p["name"], "address": p["address"], "owner": p.get("client"), "link": f"#property-{p['id']}",
               "approval_threshold_capex": p.get("spend_limit"), "report_notes": p.get("report_notes"),
               "team": {x["role"]: x["user_name"] for x in p["roles"] if x["user_id"]},
               "upcoming_critical_dates": [{"title": d["title"], "date": d["date"], "days_left": d["days_left"], "who": d.get("assignee_name")}
                                           for d in p["dates"] if d["status"] == "open"][:10],
               "property_insurance": p["insurance"]["label"], "our_coi": p["our_coi"]["status"]["label"],
               "our_coi_requirements": p.get("our_coi_requirements"),
               "contacts": [{"role": x["role"], "name": x["name"] or x["company"], "email": x["email"], "phone": x["phone"]} for x in p["contacts"]],
               "vendors": [{"service": s["trade"], "vendor": c_["vendor_name"], "rating": c_.get("avg"), "coi": (c_.get("coi") or {}).get("label")}
                           for s in p["vendors"]["slots"] for c_ in s["current"]],
               "projects": [{"name": x["name"], "done": x["done"], "total": x["total"], "link": f"#project-{x['id']}"} for x in p["projects"]],
               "open_tasks": len(p["tasks"]), "bank": p.get("bank_name")}
        if is_mgr:
            out["bank_accounts"] = p.get("bank_accounts")
        return out
    if name == "find_vendors":
        vs = vendors.list_all(args.get("trade") or None, args.get("text") or None)
        if not vs and args.get("trade"):   # loose trade match ("landscaper" → Landscaping)
            t = args["trade"].lower()[:5]
            vs = [v for v in vendors.list_all() if any(t in x.lower() for x in v["trades"])]
        return {"vendors": [{"name": v["name"], "trades": v["trades"], "rating": v["avg"], "reviews": v["reviews"], "price": v.get("price_label"),
                             "badge": v.get("badge_label"), "warning": v.get("warning"), "used_at": [x["name"] for x in v["current_properties"]],
                             "phone": v.get("phone"), "link": f"#vendor-{v['id']}"} for v in vs[:12]]}
    if name == "project_info":
        with tx() as c:
            r = c.execute("SELECT id FROM projects WHERE is_general=0 AND lower(name) LIKE ? ORDER BY status='done', id DESC LIMIT 1",
                          (_like(args.get("name")),)).fetchone()
        if not r:
            return {"error": "No project matches that name."}
        from .blockers import text as bt
        with tx() as c:
            p = dict(c.execute("SELECT id, name, status, start_date, property FROM projects WHERE id=?", (r["id"],)).fetchone())
            ts = rows(c.execute("""SELECT t.id, t.title, t.status, t.due_date, COALESCE(u.name,'Unassigned') AS assignee, t.blocked_kind,
                                          t.blocked_label, t.blocked_reason FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id
                                   WHERE t.project_id=? ORDER BY t.position, t.id""", (p["id"],)))
        b = bids.project_bids(p["id"])
        return {"project": p, "link": f"#project-{p['id']}", "done": sum(t["status"] == "done" for t in ts), "total": len(ts),
                "next": [{"id": t["id"], "title": t["title"], "due": t["due_date"], "who": t["assignee"], "link": f"#task-{t['id']}"}
                         for t in ts if t["status"] != "done"][:5],
                "blocked": [{"title": t["title"], "why": bt(t)} for t in ts if t["status"] == "blocked"],
                "bids": [{"vendor": x["vendor_label"], "amount": x["amount"], "recommended": x["id"] == b["recommended_bid_id"],
                          "status": x["status"]} for x in b["bids"]],
                "owner_approval": b["approvals"][0]["status"] if b["approvals"] else None}
    if name == "my_day":
        f = fun.me(uid)
        with tx() as c:
            over = rows(c.execute("""SELECT t.id, t.title, t.due_date FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.assignee_id=?
                                     AND t.status!='done' AND p.status='active' AND t.due_date<? ORDER BY t.due_date LIMIT 10""", (uid, today)))
            due = rows(c.execute("""SELECT t.id, t.title FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.assignee_id=?
                                    AND t.status!='done' AND p.status='active' AND t.due_date=? LIMIT 10""", (uid, today)))
        for t in over + due:
            t["link"] = f"#task-{t['id']}"
        return {"today_ring": f["today"], "streak_days": f["streak"], "overdue": over, "due_today": due,
                "badges_earned": [b["name"] for b in f["badges"] if b["earned_at"]]}
    return {"error": "unknown tool"}


def ask(messages: list[dict], user: dict, page: str | None = None) -> dict:
    """messages: [{role: user|assistant, content: str}] (the latest is the question). Returns {reply}."""
    from . import intake
    if not intake.ai_enabled():
        return {"reply": "The help assistant needs Claude (an ANTHROPIC_API_KEY in Railway). Meanwhile: the menu groups everything under "
                         "Properties, Projects and Tasks, and every page has a short description at the top."}
    import anthropic
    client = anthropic.Anthropic()
    system = ("You are LJ, the built-in help assistant for Foresite Real Estate's Task Manager web app. You're an assistant, not a "
              "person on the team; if asked about your name, say it's a friendly nod to the colleague everyone goes to with questions. "
              "Friendly, quick, a touch of warmth — no gushing. "
              f"Today is {date.today():%A %B %d, %Y}. "
              f"You're talking to {user.get('name')} (role: {user.get('role')}). They're on the '{page or 'home'}' page.\n"
              "Answer briefly and concretely (2–6 short sentences or a few bullets). Say exactly where to click (menu ▾ → item → button). "
              "Only describe features in the guide; if something isn't possible, say so and suggest the closest thing. "
              "Use the tools to look up live data when the question is about specific tasks, properties, projects, vendors or their day — "
              "never guess names, dates or numbers. Respect their role (staff can't do manager-only things). "
              "When you mention a specific item, add a markdown link using the 'link' value from the tool, e.g. [Open task #12](#task-12) "
              "or [777 E Sonterra](#property-3). To point at a page use (#view-calendar), (#view-projects), (#view-properties), "
              "(#view-vendors), (#view-emails), (#view-close), (#view-tasks), (#view-home), (#view-templates), (#view-history).\n\n" + GUIDE)
    convo = [{"role": m["role"], "content": str(m["content"])[:4000]} for m in messages[-12:] if m.get("role") in ("user", "assistant")]
    if not convo or convo[-1]["role"] != "user":
        return {"reply": "Ask me anything about the app."}
    for _ in range(5):
        try:
            resp = client.beta.messages.create(model=intake.MODEL, max_tokens=1200, system=system, messages=convo, tools=TOOLS,
                                               betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                               output_config={"effort": "low"})
        except anthropic.APIError as e:
            log.warning("help assistant failed: %s", e)
            return {"reply": "Sorry — I couldn't answer just now. Try again in a moment."}
        if resp.stop_reason != "tool_use":
            text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
            return {"reply": text or "Sorry, I'm not sure — try rephrasing?"}
        convo.append({"role": "assistant", "content": [b.model_dump() if hasattr(b, "model_dump") else b for b in resp.content]})
        results = []
        for b in resp.content:
            if getattr(b, "type", "") == "tool_use":
                try:
                    out = run_tool(b.name, b.input or {}, user)
                except Exception as e:
                    log.exception("help tool %s", b.name)
                    out = {"error": str(e)}
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out, default=str)[:12000]})
        convo.append({"role": "user", "content": results})
    return {"reply": "That took too many lookups — could you narrow the question?"}
