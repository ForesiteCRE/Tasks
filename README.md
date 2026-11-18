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

## Connecting to real Blink
1. In Blink admin, create an app/integration and generate an API token → `BLINK_API_TOKEN`.
2. Confirm the "send message" endpoint & payload at developer.joinblink.com and set
   `BLINK_SEND_MESSAGE_PATH` / adjust `app/blink.py::send_dm`.
3. Route inbound DMs to `POST /blink/webhook` with body `{"blink_user_id","text"}` and header
   `X-Webhook-Secret`. If Blink can't push webhooks directly, use a relay (Make / tray.ai have Blink
   connectors) or poll the "list chat messages" endpoint.
4. Fill each person's Blink user ID on the People tab.
5. Deploy somewhere always-on (the scheduler runs in-process every 10 min).
