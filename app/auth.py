"""Google sign-in restricted to the company's Google Workspace domain.

Flow: /login -> Google consent -> /auth/callback -> signed session cookie.
Only accounts whose verified email ends in @ALLOWED_EMAIL_DOMAIN (and whose Workspace `hd`
claim matches) get in. If GOOGLE_CLIENT_ID is unset, auth is disabled (local dev only).
"""
import base64
import json
import os
import re
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
                       "/api/blink/simulate", "/api/reminders/run", "/api/users/match-blink", "/api/demo")
ADMIN_ONLY_WRITE_PREFIXES = ("/api/users", "/api/roles", "/api/departments")   # reading the people/role lists is fine for everyone


def _admin_only(request: Request) -> bool:
    path = request.url.path
    return path.startswith(ADMIN_ONLY_PREFIXES) or (request.method != "GET" and path.startswith(ADMIN_ONLY_WRITE_PREFIXES))


# Staff may read everything (except admin areas) but only change their own tasks.
def _staff_allowed(request: Request) -> bool:
    path, method = request.url.path, request.method
    if method == "GET":
        return True
    # Task-level writes: each endpoint checks ownership / manager itself.
    if method == "DELETE" and re.fullmatch(r"/api/projects/\d+", path):
        return True   # delete_project itself only allows the project's creator (or a manager)
    if re.fullmatch(r"/api/projects/\d+/(bids|recommendation|approval|recommendation-draft)", path) or \
            re.fullmatch(r"/api/bids/\d+", path) or \
            re.fullmatch(r"/api/(properties|projects)/\d+/(vendors|vendor-slots)", path) or \
            re.fullmatch(r"/api/properties/\d+/vendors/\d+/coi", path) or \
            re.fullmatch(r"/api/properties/\d+/our-coi(/request)?", path):
        return True   # vendor endpoints check property/project team membership themselves
    return path.startswith(("/api/tasks", "/api/checklist/", "/api/reminders", "/api/me/", "/api/critical-dates/",
                             "/api/vendors", "/api/vendor-reviews/", "/api/property-vendors/", "/api/project-vendors/", "/api/emails", "/api/wins/", "/api/our-cois/",
                             "/api/vendor-cois/"))


_PUBLIC_HOST = PUBLIC_URL.split("://")[-1].split("/")[0].lower()


async def require_login(request: Request, call_next):
    """Middleware: everything except PUBLIC_PATHS needs a signed-in user."""
    # Moved to a custom domain? Send people on the old address to PUBLIC_URL (pages only — webhooks,
    # calendar feeds and health checks keep answering on any address so nothing breaks mid-switch).
    host = request.headers.get("host", "").lower()
    if (_PUBLIC_HOST and host and host != _PUBLIC_HOST and request.method == "GET"
            and not request.url.path.startswith(("/api/", "/blink/", "/calendar/", "/healthz", "/brand/", "/approve/"))):
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"{PUBLIC_URL}{request.url.path}{query}", status_code=301)
    if LOCKED and request.url.path not in ("/healthz", "/blink/webhook"):
        return HTMLResponse("<p style='font:16px system-ui;text-align:center;margin-top:20vh'>"
                            "Sign-in isn't configured yet — set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.</p>",
                            status_code=503)
    if request.url.path in PUBLIC_PATHS or request.url.path.startswith(("/calendar/", "/brand/", "/approve/")):  # secret-token links; logo files
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


def branded_page(title: str, body_html: str, status_code: int = 200) -> HTMLResponse:
    """Full-screen Foresite page (sign-in, access denied) on the skyline photo."""
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · Foresite Task Manager</title>
<link rel="icon" type="image/png" href="/brand/favicon.png">
<link href="https://fonts.googleapis.com/css2?family=Lato:ital,wght@0,300;0,400;0,700;1,300&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;font-family:Lato,Helvetica,Arial,sans-serif;color:#fff;display:flex;align-items:center;justify-content:center;
background:linear-gradient(100deg,rgba(58,71,80,.93),rgba(58,71,80,.78) 50%,rgba(84,159,197,.6)),url(/brand/hero.jpg) center/cover fixed}}
.box{{width:min(440px,92vw);text-align:center;padding:40px 28px}}.box img{{width:260px;max-width:80%}}
.app{{margin:18px 0 4px;font-size:13px;letter-spacing:.28em;text-transform:uppercase;color:#cfe7f3;font-weight:700}}
.tag{{font-style:italic;font-weight:300;font-size:17px;color:#e6eef3;margin:0 0 30px;line-height:1.5}}
.rule{{height:1px;background:rgba(255,255,255,.45);margin:22px auto;width:70%;position:relative}}
.rule:after{{content:"";position:absolute;right:-3px;top:-3px;width:7px;height:7px;border-radius:50%;background:#fff}}
a.g{{display:inline-flex;align-items:center;gap:12px;background:#fff;color:#3a4750;text-decoration:none;font-weight:700;padding:13px 22px;border-radius:3px;
box-shadow:0 6px 24px rgba(0,0,0,.18);letter-spacing:.02em}}a.g:hover{{background:#eef6fa}}
.note{{margin-top:18px;font-size:13px;color:#d7e3ea}}.msg{{font-size:16px;margin:0 0 22px;line-height:1.5}}
.foot{{position:fixed;bottom:14px;left:0;right:0;text-align:center;font-size:12px;color:#c7d5dd;letter-spacing:.04em}}
</style></head><body><div class="box"><img src="/brand/logo-white.png" alt="Foresite Commercial Real Estate">
<div class="app">Task Manager</div><div class="rule"></div>{body_html}</div>
<div class="foot">San Antonio · Austin · Investment Sales. Leasing. Management.</div></body></html>""", status_code=status_code)


G_ICON = ('<svg width="20" height="20" viewBox="0 0 48 48"><path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z"/>'
          '<path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/>'
          '<path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z"/>'
          '<path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z"/></svg>')


@router.get("/login")
def login(request: Request, go: int = 0):
    if not ENABLED:
        return RedirectResponse("/")
    if not go:
        return branded_page("Sign in", f'<p class="tag">A new approach to commercial real estate management, leasing and investment sales.</p>'
                                       f'<a class="g" href="/login?go=1">{G_ICON} Sign in with Google</a>'
                                       f'<div class="note">Use your @{DOMAIN} account</div>')
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


@router.get("/gmail/connect")
def gmail_connect(request: Request):
    """Let the app put drafts in this person's Gmail Drafts folder (gmail.compose: create/edit drafts, send nothing
    on its own). Uses the same OAuth client and redirect URI as sign-in."""
    me = current_user(request)
    if not ENABLED or not me:
        return RedirectResponse("/")
    from .emails import GMAIL_SCOPE
    state = "gmail:" + secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": CLIENT_ID, "redirect_uri": _redirect_uri(request), "response_type": "code",
        "scope": "openid email " + GMAIL_SCOPE, "state": state, "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true", "login_hint": me["email"], "hd": DOMAIN}))


def _gmail_callback(request: Request, code: str):
    me = current_user(request)
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=15, data={
        "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": _redirect_uri(request), "grant_type": "authorization_code"})
    if not me or r.status_code != 200:
        return RedirectResponse("/#emails?gmail=failed")
    tok = r.json()
    payload = tok.get("id_token", "..").split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)) or b"{}")
    if (claims.get("email") or "").lower() != me["email"].lower() or not tok.get("refresh_token"):
        return RedirectResponse("/#emails?gmail=wrong-account")   # must connect your own mailbox
    from . import emails
    emails.save_gmail_token(me["id"], tok["refresh_token"])
    return RedirectResponse("/#emails?gmail=connected")


@router.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    expected = request.session.pop("oauth_state", None)
    if expected and expected.startswith("gmail:") and state == expected:
        return _gmail_callback(request, code) if code and not error else RedirectResponse("/#emails?gmail=cancelled")
    if error or not code or state != expected:
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
    return branded_page("Access", f'<p class="msg">{msg}</p><a class="g" href="/login?go=1">{G_ICON} Try again</a>', 403)
