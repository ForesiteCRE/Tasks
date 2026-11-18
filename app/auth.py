"""Google sign-in restricted to the company's Google Workspace domain.

Flow: /login -> Google consent -> /auth/callback -> signed session cookie.
Only accounts whose verified email ends in @ALLOWED_EMAIL_DOMAIN (and whose Workspace `hd`
claim matches) get in. If GOOGLE_CLIENT_ID is unset, auth is disabled (local dev only).
"""
import base64
import json
import os
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .db import tx

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
DOMAIN = os.getenv("ALLOWED_EMAIL_DOMAIN", "foresitecre.com").lower()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # e.g. https://tasks.up.railway.app
ENABLED = bool(CLIENT_ID and CLIENT_SECRET)
# On a hosted deploy (Railway sets RAILWAY_ENVIRONMENT) with sign-in not configured, lock everything.
LOCKED = not ENABLED and bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("REQUIRE_LOGIN"))

# Paths reachable without signing in
PUBLIC_PATHS = {"/login", "/auth/callback", "/logout", "/blink/webhook", "/healthz"}

router = APIRouter()


def _redirect_uri(request: Request) -> str:
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    return f"{base}/auth/callback"


def current_user(request: Request) -> dict | None:
    if not ENABLED:
        return {"id": None, "name": "Local dev (sign-in disabled)", "email": ""}
    return request.session.get("user")


async def require_login(request: Request, call_next):
    """Middleware: everything except PUBLIC_PATHS needs a signed-in user."""
    if LOCKED and request.url.path not in ("/healthz", "/blink/webhook"):
        return HTMLResponse("<p style='font:16px system-ui;text-align:center;margin-top:20vh'>"
                            "Sign-in isn't configured yet — set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.</p>",
                            status_code=503)
    if request.url.path in PUBLIC_PATHS or current_user(request):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "not signed in"}, status_code=401)
    return RedirectResponse("/login")


@router.get("/login")
def login(request: Request):
    if not ENABLED:
        return RedirectResponse("/")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "hd": DOMAIN,              # Google shows only company accounts in the picker
        "prompt": "select_account",
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@router.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code or state != request.session.pop("oauth_state", None):
        return _denied("Sign-in was cancelled or expired. Please try again.")
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=15, data={
        "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": _redirect_uri(request), "grant_type": "authorization_code"})
    if r.status_code != 200:
        return _denied("Google sign-in failed. Please try again.")
    # ID token came straight from Google's token endpoint over TLS, so its claims can be read directly.
    payload = r.json()["id_token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    email = (claims.get("email") or "").lower()
    if (claims.get("aud") != CLIENT_ID or not claims.get("email_verified")
            or claims.get("hd", "").lower() != DOMAIN or not email.endswith("@" + DOMAIN)):
        return _denied(f"Please sign in with your @{DOMAIN} account.")

    name = claims.get("name") or email.split("@")[0]
    with tx() as c:
        row = c.execute("SELECT id, name FROM users WHERE lower(email)=?", (email,)).fetchone()
        uid = row["id"] if row else c.execute(
            "INSERT INTO users(name, email) VALUES(?,?)", (name, email)).lastrowid
    request.session["user"] = {"id": uid, "name": row["name"] if row else name, "email": email}
    link_blink_account(uid, email)
    return RedirectResponse("/")


def link_blink_account(uid: int, email: str) -> str | None:
    """Fill in the person's Blink user id from their email, if we don't have it yet."""
    from . import blink
    with tx() as c:
        have = c.execute("SELECT blink_user_id FROM users WHERE id=?", (uid,)).fetchone()["blink_user_id"]
    if have:
        return have
    try:
        buid = blink.find_user_id_by_email(email)
    except PermissionError:
        return None
    if buid:
        with tx() as c:
            c.execute("UPDATE users SET blink_user_id=? WHERE id=? AND blink_user_id IS NULL", (buid, uid))
    return buid


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login" if ENABLED else "/")


@router.get("/api/me")
def me(request: Request):
    return {**current_user(request), "auth_enabled": ENABLED}


def _denied(msg: str) -> HTMLResponse:
    return HTMLResponse(
        f"<div style='font:16px system-ui;max-width:420px;margin:15vh auto;text-align:center'>"
        f"<p>{msg}</p><p><a href='/login'>Try again</a></p></div>", status_code=403)
