"""Blink (joinblink.com) connector.

Outbound: DM a user from your Blink app identity via the Blink REST API
(base https://api.joinblink.com/v2, bearer token).
Inbound: Blink -> POST /blink/webhook on this server (see main.py).

With no BLINK_API_TOKEN set, runs in MOCK mode: messages are only logged to
the blink_messages table and shown in the web UI's "Blink chat" panel.
"""
import logging
import os

import httpx

from .db import tx

log = logging.getLogger("blink")

API_BASE = os.getenv("BLINK_API_BASE", "https://api.joinblink.com/v2").rstrip("/")
TOKEN = os.getenv("BLINK_API_TOKEN", "")
SEND_PATH = os.getenv("BLINK_SEND_MESSAGE_PATH", "/chats/messages")
MOCK = not TOKEN


def send_dm(blink_user_id: str, text: str) -> bool:
    delivered = False
    if MOCK:
        log.info("[MOCK Blink DM -> %s]\n%s", blink_user_id, text)
    else:
        try:
            # NOTE: payload shape must be confirmed against developer.joinblink.com
            r = httpx.post(
                f"{API_BASE}{SEND_PATH}",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={"recipients": [blink_user_id], "text": text},
                timeout=15,
            )
            r.raise_for_status()
            delivered = True
        except httpx.HTTPError as e:
            log.error("Blink send failed for %s: %s", blink_user_id, e)
    with tx() as c:
        c.execute(
            "INSERT INTO blink_messages(direction, blink_user_id, text, delivered) VALUES('out',?,?,?)",
            (blink_user_id, text, int(delivered)),
        )
    return delivered


def log_inbound(blink_user_id: str, text: str):
    with tx() as c:
        c.execute(
            "INSERT INTO blink_messages(direction, blink_user_id, text, delivered) VALUES('in',?,?,1)",
            (blink_user_id, text),
        )
