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
