# Blink Task Manager (prototype)

Recurring monthly projects + reusable checklists, with reminders and status updates in **Blink chat**.

```
 Web UI ──► FastAPI + SQLite ──► Blink API (DM reminders / assignments)
                 ▲
 Blink chat ─────┘  POST /blink/webhook  ("done 12", or plain English → Claude)
```

## Run
```bash
pip install -r requirements.txt
cp .env.example .env   # leave BLINK_API_TOKEN empty = mock mode
python seed.py         # optional demo data
uvicorn app.main:app --port 8000 --env-file .env
```
Open http://localhost:8000 and use **Blink chat (simulator)** to test.

## Features
- **Checklist templates** – reusable task lists with default assignees and due offsets.
- **Similar projects** – start a project from a template, or "Save as template" from a finished one.
- **Monthly recurring** – auto-creates e.g. "{month} {year} – Month-End Close" on day N each month.
- **Assignments** – people get a Blink DM listing their new tasks.
- **Reminders** – daily DM for tasks due within 2 days or overdue (max once/day per task).
- **Chat updates** – reply in Blink: `done 12`, `12 in progress`, `blocked 7 waiting on lender`,
  `note 5 …`, `list`. With `ANTHROPIC_API_KEY` set, free text ("finished the rent roll, still
  waiting on Joe for CAM") is mapped onto the person's tasks by Claude.
- Web checkboxes and Blink updates share one history (`task_updates`).
- **Task detail panel** – click any task: description, checklist, subtasks, “waiting on” dependencies,
  comments with **@mentions** (the person gets a Blink DM; reply in Blink with `comment 12 …`), history.
- **Dependencies** – a task waiting on others shows 🔒, is skipped by reminders, and its assignee gets a
  “▶️ your turn” Blink message when the last blocker is done.
- **Automations** (Reports & backups → Automations): Blink alerts to managers when a task is Blocked, N days
  overdue, or a project completes; assignee alerts for “your turn” and new comments.
- **Start a project from a document** – managers send the bot a management/listing agreement (or paste terms,
  or say "new project for …"); Claude reads it, asks what's missing ("Who will be the Property Manager?") and
  creates it on "yes". Web: Projects → 📄 From document pre-fills the form.
- **Time tracking** – estimate per task (and per template item), actual time + notes when done (web prompt or
  Blink `time 12 1h30 note` / plain English), turnaround; **Reports → ⏱ Time insights** shows estimate vs actual
  by task and person.
- **🏠 Home dashboards** – *My day*: overdue/today/this-week agenda (tasks, reminders, project starts),
  waiting on me, blocked for me, my projects, reminders, comments & mentions, done this week.
  *Team* (managers): workload per person, projects at risk, blocked, overdue, unassigned, completed, time not logged.
- **Reminders** – on Home or in Blink (“remind me Friday 9am to call the owner”); delivered in Blink.
- **6pm daily recap** – each person gets done today / overdue / due today / next 7 days / waiting on them
  (`DIGEST_HOUR`, opt-out on Home).
- **Menu** – Home · Tasks ▾ (All tasks, Projects, Templates, Monthly recurring) · Calendar · By person · Admin ▾
  (People, Reports & backups, Blink simulator — admin only). Tasks can exist without a project.
- **Monday “week ahead”** – 8am Monday Blink message per person: tasks day by day, overdue carry-over, project
  starts, reminders (`WEEK_AHEAD_HOUR`, opt-out in ⚙︎ My settings).
- **Unread @mentions** – red count on Home; NEW tags on comments; clears when you open Home.
- **Calendar sync** – ⚙︎ My settings → Add to Google Calendar (private iCal feed per person at
  `/calendar/<secret>.ics`; tasks with due dates, reminders, project starts; link can be regenerated).
- **🧪 Demo data** (Admin → Reports & backups): one click loads 4 demo teammates, all starter templates, 9 sample
  projects (incl. onboarding 777 E Sonterra Blvd, San Antonio TX 78258) with blocked tasks, @mentions, logged time,
  reminders and loose tasks; sends nothing to Blink; “Remove demo data” deletes exactly what it created.
- **Starter templates** – Tasks → Templates → Browse: 8 ready-made CRE checklists (onboarding, sale hand-off,
  sale retain, move-in/out, renewal, CAM rec, budget) + optional sample projects (one-click remove).
- **Property photos** – each project shows a header image: uploaded photo → Google Street View
  (`GOOGLE_MAPS_API_KEY`) → map of the address.
- **Weekly summary** – Monday-morning Blink message (overdue / blocked / due this week) to anyone ticked
  "Weekly summary" on People. Preview or send now from **Reports & backups**.
- **Daily backups** – a copy of the database every day in `/data/backups` (kept 14 days), downloadable from
  **Reports & backups**. For off-site safety also enable Railway's volume backups (service → Backups tab).

## Connecting to real Blink (Chat API, App Identity)
Uses Blink's Chat API: https://developer.joinblink.com/docs/using-chat-api

1. **Blink Admin portal → Manage Apps → Created Apps → your App Identity.** Copy into `.env`:
   - `BLINK_CLIENT_ID` (the `aid-…` id), `BLINK_CLIENT_SECRET`, `BLINK_WEBHOOK_SECRET`.
2. **Webhook URL** on the App Identity → `https://<your-server>/blink/webhook`, subscribed to the
   **Chat message sent** event. Requests are signature-checked (Hook0 `X-Signature`); unsigned ones get 401.
3. **People tab:** enter each person's Blink user id (`u-…`; Admin portal → user page, it's in the URL).
4. The app opens a private 1:1 chat with each person automatically the first time it messages them.
5. Deploy somewhere always-on with HTTPS (the scheduler runs in-process every 10 min).

How it flows:
```
reminder/assignment ─► POST /oidc/token ─► POST /api/platform/chats (1:1, untitled = reused)
                                           └► POST /chats/{id}/messages  ──► user's Blink chat
user replies in Blink ─► webhook blink.chat.message-sent ─► GET message ─► update task ─► reply
```

## Google sign-in (@foresitecre.com only)
1. https://console.cloud.google.com → create a project (e.g. "Task Manager").
2. **APIs & Services → OAuth consent screen** → User type **Internal** (only your Workspace can use it).
3. **Credentials → Create credentials → OAuth client ID → Web application.**
   Authorized redirect URI: `https://<your-railway-domain>/auth/callback`
4. Put the client ID/secret in `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, set `PUBLIC_URL` to the
   Railway https address and `SESSION_SECRET` to a long random string.
Anyone who signs in is added to People automatically; add their Blink user ID there for chat reminders.
If `GOOGLE_CLIENT_ID` is blank, sign-in is disabled (local testing only — never deploy like that).

## Deploy (Railway – recommended)
1. Push this repo to GitHub → Railway: **New Project → Deploy from GitHub repo**.
2. Add a **Volume** mounted at `/data`; set variable `DB_PATH=/data/tasks.db` (keeps the database across deploys).
3. Add the variables from `.env.example` (Blink client id/secret, webhook secret, optional Anthropic key).
   Set `TZ` (e.g. `America/Chicago`) so reminders go out at the right local hour.
4. **Settings → Networking → Generate Domain** → put `https://<domain>/blink/webhook` on the Blink App Identity.
Keep to **one instance** (the reminder scheduler runs inside the app).

## Tests
```bash
pip install pytest && cd tests && pytest -q
```
32 end-to-end tests: sign-in & roles (admin/manager/staff permissions), projects/templates/roles, subtasks,
checklists, comments, dependencies, automations, time tracking, Blink chat commands & webhook signatures,
project intake from documents, dashboards, reminders, the 6pm recap and every scheduled job.
