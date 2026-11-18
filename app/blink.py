"""Blink Chat API connector (App Identity).

Docs: https://developer.joinblink.com/docs/using-chat-api
- Auth: OAuth client_credentials at /oidc/token with the App Identity's Client ID (aid-…) and
  Client Secret; tokens last 5 minutes and are cached/refreshed here.
- DMs: an *untitled* private chat with members {this app, one user} is unique per member set, so
  "create chat" doubles as "get or create the 1:1 chat". Its chat_id is cached on the user row.
- Inbound: Blink POSTs `blink.chat.message-sent` webhooks (signed via Hook0, X-Signature header);
  the payload only has ids, so the message is fetched with GET /chats/{chat}/messages/{msg}.

With no BLINK_CLIENT_SECRET set, runs in MOCK mode: messages are only logged to the
blink_messages table and shown in the web UI's "Blink chat" panel.
"""
import base64
import json
import logging
import os
import threading
import time

import httpx

from .db import tx

log = logging.getLogger("blink")

API = os.getenv("BLINK_API_BASE", "https://api.joinblink.com").rstrip("/")
PLATFORM = f"{API}/api/platform"
CLIENT_ID = os.getenv("BLINK_CLIENT_ID", "")          # App Identity ID, "aid-…"
CLIENT_SECRET = os.getenv("BLINK_CLIENT_SECRET", "")
WEBHOOK_SECRET = os.getenv("BLINK_WEBHOOK_SECRET", "")
ORG_ID = os.getenv("BLINK_ORG_ID", "")                # optional; read from the token if blank
MOCK = not (CLIENT_ID and CLIENT_SECRET)

_token = {"value": "", "exp": 0.0}
_lock = threading.Lock()


class BlinkError(Exception):
    pass


# ---------- auth ----------

def _access_token() -> str:
    with _lock:
        if _token["value"] and time.time() < _token["exp"] - 30:
            return _token["value"]
        r = httpx.post(f"{API}/oidc/token", timeout=15, data={
            "grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        if r.status_code != 200:
            raise BlinkError(f"token request failed: {r.status_code} {r.text[:200]}")
        d = r.json()
        _token.update(value=d["access_token"], exp=time.time() + d.get("expires_in", 300))
        return _token["value"]


def _claims(jwt: str) -> dict:
    part = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def organisation_id() -> str:
    """Org id ("o-…"): BLINK_ORG_ID, else the first o- prefixed claim in the access token."""
    global ORG_ID
    if not ORG_ID:
        for v in _claims(_access_token()).values():
            if isinstance(v, str) and v.startswith("o-"):
                ORG_ID = v
                break
        else:
            raise BlinkError("couldn't find organisation id in token; set BLINK_ORG_ID")
    return ORG_ID


def _req(method: str, path: str, **kw) -> dict:
    r = httpx.request(method, f"{PLATFORM}{path}", timeout=15,
                      headers={"Authorization": f"Bearer {_access_token()}", "Accept": "application/json"}, **kw)
    if r.status_code >= 400:
        raise BlinkError(f"{method} {path} -> {r.status_code} {r.text[:300]}")
    return r.json() if r.content else {}


# ---------- chats & messages ----------

def dm_chat_id(blink_user_id: str) -> str:
    """Get (or create) the 1:1 chat between this app and a user; cached on users.blink_chat_id."""
    with tx() as c:
        row = c.execute("SELECT blink_chat_id FROM users WHERE blink_user_id=?", (blink_user_id,)).fetchone()
    if row and row["blink_chat_id"]:
        return row["blink_chat_id"]
    chat = _req("POST", "/chats", json={
        "access_rule": {"@type": "private"},
        "members": [{"@type": "app", "app_identity_id": CLIENT_ID},
                    {"@type": "user", "user_id": blink_user_id}],
        "organisation_id": organisation_id(),
        # no title -> Blink returns the existing chat for this exact member set instead of a duplicate
    })
    with tx() as c:
        c.execute("UPDATE users SET blink_chat_id=? WHERE blink_user_id=?", (chat["chat_id"], blink_user_id))
    return chat["chat_id"]


def send_dm(blink_user_id: str, text: str) -> bool:
    delivered = False
    if MOCK:
        log.info("[MOCK Blink DM -> %s]\n%s", blink_user_id, text)
    else:
        try:
            _req("POST", f"/chats/{dm_chat_id(blink_user_id)}/messages", json={
                "sender": {"@type": "app", "app_identity_id": CLIENT_ID},
                "body": {"@type": "markdown", "markdown": text},
                "unfurl_links": True,
            })
            delivered = True
        except (BlinkError, httpx.HTTPError) as e:
            log.error("Blink send failed for %s: %s", blink_user_id, e)
    with tx() as c:
        c.execute("INSERT INTO blink_messages(direction, blink_user_id, text, delivered) VALUES('out',?,?,?)",
                  (blink_user_id, text, int(delivered)))
    return delivered


def get_message(chat_id: str, message_id: str) -> dict:
    return _req("GET", f"/chats/{chat_id}/messages/{message_id}")


def message_text(msg: dict) -> str:
    body = msg.get("body") or {}
    return (body.get("markdown") or body.get("text") or "").strip()


def log_inbound(blink_user_id: str, text: str):
    with tx() as c:
        c.execute("INSERT INTO blink_messages(direction, blink_user_id, text, delivered) VALUES('in',?,?,1)",
                  (blink_user_id, text))


# ---------- webhook signature ----------

def verify_webhook(raw_body: bytes, headers) -> bool:
    """Validate Blink's Hook0 X-Signature header. Fails closed if no secret is configured."""
    from hook0.errors import Hook0ClientError
    from hook0.signature import verify_webhook_signature

    sig = headers.get("x-signature", "")
    if not (WEBHOOK_SECRET and sig):
        return False
    try:
        return verify_webhook_signature(sig, raw_body, list(headers.items()), WEBHOOK_SECRET, tolerance=300)
    except Hook0ClientError as e:
        log.warning("webhook signature rejected: %s", e)
        return False
