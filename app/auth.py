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

# Roles: staff < manager < admin.
#   ADMIN_EMAILS   – always admins (People, Reports & backups). Optional.
#   MANAGER_EMAILS – always at least managers; if there's no admin yet, the first of them to sign in
#                    becomes the admin. If nobody is an admin, the next person to sign in becomes one,
#                    so the system can never be locked out.
_emails = lambda var: {e.strip().lower() for e in os.getenv(var, "").split(",") if e.strip()}
ADMIN_EMAILS = _emails("ADMIN_EMAILS")
MANAGER_EMAILS = _emails("MANAGER_EMAILS")
RANK = {"staff": 0, "manager": 1, "admin": 2}


def at_least(user: dict | None, role: str) -> bool:
    return RANK.get((user or {}).get("role", "staff"), 0) >= RANK[role]

# Paths reachable without signing in
PUBLIC_PATHS = {"/login", "/auth/callback", "/logout", "/blink/webhook", "/healthz"}

router = APIRouter()


def _redirect_uri(request: Request) -> str:
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    return f"{base}/auth/callback"


def current_user(request: Request) -> dict | None:
    if not ENABLED:
        return {"id": None, "name": "Local dev (sign-in disabled)", "email": "", "role": "admin"}
    u = request.session.get("user")
    if not u:
        return None
    with tx() as c:
        row = c.execute("SELECT role FROM users WHERE id=?", (u["id"],)).fetchone()
    return {**u, "role": row["role"] if row else "staff"}


def is_manager(request: Request) -> bool:
    return at_least(current_user(request), "manager")


def is_admin(request: Request) -> bool:
    return at_least(current_user(request), "admin")


def _apply_role_rules(uid: int, email: str):
    email = (email or "").lower()
    with tx() as c:
        role = (c.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone() or {"role": "staff"})["role"]
        has_admin = c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone()
        if email in ADMIN_EMAILS:
            new = "admin"
        elif not has_admin and (email in MANAGER_EMAILS or (not ADMIN_EMAILS and not MANAGER_EMAILS and role == "manager")
                                or not c.execute("SELECT 1 FROM users WHERE role IN ('manager','admin')").fetchone()):
            new = "admin"
        elif email in MANAGER_EMAILS and role == "staff":
            new = "manager"
        else:
            return
        if new != role:
            c.execute("UPDATE users SET role=? WHERE id=?", (new, uid))


# Admin-only areas: People (managing users & project roles) and Reports & backups.
ADMIN_ONLY_PREFIXES = ("/api/backups", "/api/summary", "/api/automations", "/api/insights", "/api/blink/messages",
                       "/api/blink/simulate", "/api/reminders/run", "/api/users/match-blink")
ADMIN_ONLY_WRITE_PREFIXES = ("/api/users", "/api/roles")   # reading the people/role lists is fine for everyone


def _admin_only(request: Request) -> bool:
    path = request.url.path
    return path.startswith(ADMIN_ONLY_PREFIXES) or (request.method != "GET" and path.startswith(ADMIN_ONLY_WRITE_PREFIXES))


# Staff may read everything (except admin areas) but only change their own tasks.
def _staff_allowed(request: Request) -> bool:
    path, method = request.url.path, request.method
    if method == "GET":
        return True
    # Task-level writes: each endpoint checks ownership / manager itself.
    return path.startswith(("/api/tasks", "/api/checklist/", "/api/reminders", "/api/me/"))


async def require_login(request: Request, call_next):
    """Middleware: everything except PUBLIC_PATHS needs a signed-in user."""
    if LOCKED and request.url.path not in ("/healthz", "/blink/webhook"):
        return HTMLResponse("<p style='font:16px system-ui;text-align:center;margin-top:20vh'>"
                            "Sign-in isn't configured yet — set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.</p>",
                            status_code=503)
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    if ENABLED and (u := request.session.get("user")):
        _ensure_user_row(request, u)
    if (u := current_user(request)):
        if request.url.path.startswith("/api/"):
            if _admin_only(request) and not at_least(u, "admin"):
                return JSONResponse({"detail": "Only the admin can do that."}, status_code=403)
            if not at_least(u, "manager") and not _staff_allowed(request):
                return JSONResponse({"detail": "Only managers can do that."}, status_code=403)
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "not signed in"}, status_code=401)
    return RedirectResponse("/login")


def _ensure_user_row(request: Request, u: dict):
    """A signed-in person whose People row is missing (e.g. database restored/reset) gets it back,
    including their Blink id, instead of silently not existing."""
    with tx() as c:
        row = c.execute("SELECT id FROM users WHERE id=? AND lower(email)=?", (u["id"], u["email"])).fetchone()
    if row:
        _apply_role_rules(u["id"], u["email"])  # e.g. first request after roles were introduced
        return
    with tx() as c:
        row = c.execute("SELECT id FROM users WHERE lower(email)=?", (u["email"],)).fetchone()
        uid = row["id"] if row else c.execute("INSERT INTO users(name, email) VALUES(?,?)",
                                              (u["name"], u["email"])).lastrowid
    request.session["user"] = {**u, "id": uid}
    _apply_role_rules(uid, u["email"])
    link_blink_account(uid, u["email"])


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
    _apply_role_rules(uid, email)
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
