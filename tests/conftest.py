"""Test setup: a throwaway database, Google sign-in on (simulated), Blink and Claude replaced by fakes.

Run:  pip install pytest && pytest -q
"""
import base64
import json
import os
import sys
import tempfile
import types
from urllib.parse import parse_qs, urlparse

import pytest

_tmp = tempfile.mkdtemp()
os.environ.update({
    "DB_PATH": os.path.join(_tmp, "test.db"), "GOOGLE_CLIENT_ID": "gid", "GOOGLE_CLIENT_SECRET": "gsecret",
    "SESSION_SECRET": "test", "ANTHROPIC_API_KEY": "test-key", "BLINK_WEBHOOK_SECRET": "whsec",
    "PUBLIC_URL": "http://testserver",
})
os.environ.pop("RAILWAY_ENVIRONMENT", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, blink  # noqa: E402
from app.main import app  # noqa: E402

SENT: list[tuple[str, str]] = []     # every Blink DM: (blink_user_id, text)
CLAUDE: list[dict] = []              # queued fake Claude JSON replies
CLAUDE_CALLS: list[dict] = []


@pytest.fixture(autouse=True, scope="session")
def fakes():
    blink._real_send_dm = blink.send_dm
    def fake_send(buid, text):  # same quiet-mode behaviour as the real function
        if getattr(blink._QUIET, "on", False):
            return False
        SENT.append((buid, text))
        return True
    blink.send_dm = fake_send
    auth.link_blink_account = lambda uid, email: None

    def create(**kw):
        CLAUDE_CALLS.append(kw)
        out = CLAUDE.pop(0) if CLAUDE else {"updates": [], "reply": "(no canned reply)"}
        return types.SimpleNamespace(stop_reason="end_turn",
                                     content=[types.SimpleNamespace(type="text", text=json.dumps(out))])
    fake = types.SimpleNamespace(beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=create)))
    import anthropic
    anthropic.Anthropic = lambda *a, **k: fake
    yield


def login(client: TestClient, email: str, name: str) -> dict:
    """Simulate a Google sign-in for `email`."""
    def post(url, **kw):
        payload = base64.urlsafe_b64encode(json.dumps({
            "aud": "gid", "email": email, "email_verified": True,
            "hd": email.split("@")[1], "name": name}).encode()).decode().rstrip("=")
        return types.SimpleNamespace(status_code=200, json=lambda: {"id_token": f"h.{payload}.s"})
    auth.httpx.post = post
    q = parse_qs(urlparse(client.get("/login", follow_redirects=False).headers["location"]).query)
    client.get(f"/auth/callback?code=x&state={q['state'][0]}", follow_redirects=False)
    return client.get("/api/me").json()


@pytest.fixture(scope="session")
def mgr():
    with TestClient(app) as c:
        c.me = login(c, "boss@foresitecre.com", "Bethany Boss")
        yield c


@pytest.fixture(scope="session")
def staff(mgr):
    with TestClient(app) as c:
        c.me = login(c, "sam@foresitecre.com", "Sam Patel")
        yield c
