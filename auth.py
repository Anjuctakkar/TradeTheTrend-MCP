"""
Google sign-in for the multiuser dashboard.

How it works
  * The login page shows Google's sign-in button. Google returns a signed ID token (a JWT) to the
    browser; the browser POSTs it to /api/auth/google.
  * The server verifies the token (signature, audience = GOOGLE_CLIENT_ID, expiry, issuer) and requires
    a verified email. It never trusts anything the browser says about who the user is.
  * Access is by invitation: the email must be in ADMIN_EMAILS or ALLOWED_EMAILS, or its domain in
    ALLOWED_DOMAINS, unless OPEN_SIGNUP=1.
  * The user is created or updated in the users table (keyed by Google's stable "sub" id), and the
    server sets its own signed, expiring session cookie (HttpOnly, SameSite=Lax, Secure over HTTPS).
  * Every request loads the user from the DB, so deactivating a user or bumping session_version
    takes effect at once. Changing DASHBOARD_SECRET signs everyone out.
  * Every page and API route needs a session except /login, /api/auth/*, /health and /favicon.ico.
    Handlers that need to know who is calling use Depends(current_user) or Depends(require_admin).
  * Cross-site POST/PUT/PATCH/DELETE requests are refused, and security headers are added.

With no GOOGLE_CLIENT_ID set (local use) there is no login: every request runs as one local admin user.
With DASHBOARD_PUBLIC=1 the app refuses to start unless Google sign-in is configured.
"""
import asyncio
import collections
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import config
import db


def _csv(name: str) -> set[str]:
    return {x.strip().lower() for x in os.getenv(name, "").split(",") if x.strip()}


GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
SECRET = os.getenv("DASHBOARD_SECRET", "").strip()
PUBLIC = os.getenv("DASHBOARD_PUBLIC", "").lower() in ("1", "true", "yes")
AUTH_ENABLED = bool(GOOGLE_CLIENT_ID)
ADMIN_EMAILS = _csv("ADMIN_EMAILS")
ALLOWED_EMAILS = _csv("ALLOWED_EMAILS")
ALLOWED_DOMAINS = _csv("ALLOWED_DOMAINS")
OPEN_SIGNUP = os.getenv("OPEN_SIGNUP", "").lower() in ("1", "true", "yes")
SESSION_HOURS = float(os.getenv("DASHBOARD_SESSION_HOURS", "12"))
COOKIE_SECURE = os.getenv("DASHBOARD_COOKIE_SECURE", "auto").lower()   # auto | 1 | 0
EXTRA_ORIGINS = {o.strip().rstrip("/") for o in os.getenv("DASHBOARD_ALLOWED_ORIGINS", "").split(",") if o.strip()}
COOKIE = "nse_session"
OPEN_PATHS = {"/login", "/api/auth/google", "/api/auth/status", "/api/logout", "/health", "/favicon.ico"}
LOCAL_EMAIL = "local@localhost"
MAX_LOGIN_TRIES, LOGIN_WINDOW = 20, 10 * 60      # verification attempts per address per 10 minutes

_tries: dict[str, collections.deque] = collections.defaultdict(collections.deque)
_local_user_cache: Optional[dict] = None


# ------------------------------------------------------------------ users
def _public(u: db.User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "picture": u.picture, "role": u.role}


def _load_user(uid: int, session_version: int) -> Optional[dict]:
    with db.SessionLocal() as s:
        u = s.get(db.User, uid)
        if u is None or not u.is_active or u.session_version != session_version:
            return None
        return _public(u)


def _local_user() -> dict:
    """The single admin used when Google sign-in isn't configured (local use)."""
    global _local_user_cache
    if _local_user_cache is None:
        with db.SessionLocal() as s:
            u = s.query(db.User).filter_by(email=LOCAL_EMAIL).one_or_none()
            if u is None:
                u = db.User(email=LOCAL_EMAIL, name="Local user", role="admin", is_active=True, session_version=1)
                s.add(u)
                s.commit()
            _local_user_cache = _public(u)
    return _local_user_cache


def _email_allowed(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1]
    return OPEN_SIGNUP or email in ADMIN_EMAILS or email in ALLOWED_EMAILS or domain in ALLOWED_DOMAINS


def _upsert_user(info: dict) -> Optional[tuple[dict, int]]:
    """Create or update the user for a verified Google identity. None if the account is deactivated."""
    email, sub = info["email"].lower(), info["sub"]
    with db.SessionLocal() as s:
        u = s.query(db.User).filter_by(google_sub=sub).one_or_none() or s.query(db.User).filter_by(email=email).one_or_none()
        if u is None:
            u = db.User(role="admin" if email in ADMIN_EMAILS else "user", is_active=True, session_version=1)
            s.add(u)
        if u.is_active is False:
            return None
        u.google_sub, u.email = sub, email
        u.name, u.picture = info.get("name"), info.get("picture")
        u.last_login_at = dt.datetime.now(dt.timezone.utc)
        if email in ADMIN_EMAILS:
            u.role = "admin"
        s.commit()
        return _public(u), u.session_version


# ------------------------------------------------------------------ sessions
def _sign(msg: str) -> str:
    return hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_token(user_id: int, session_version: int) -> str:
    msg = f"{user_id}.{session_version}.{int(time.time() + SESSION_HOURS * 3600)}.{secrets.token_hex(8)}"
    return f"{msg}.{_sign(msg)}"


def parse_token(token: Optional[str]) -> Optional[tuple[int, int]]:
    """(user_id, session_version) if the cookie is genuine and unexpired, else None."""
    try:
        uid, sv, exp, nonce, sig = (token or "").split(".")
        if not hmac.compare_digest(sig, _sign(f"{uid}.{sv}.{exp}.{nonce}")) or int(exp) <= time.time():
            return None
        return int(uid), int(sv)
    except (ValueError, TypeError):
        return None


def validate() -> None:
    """Called when the app starts."""
    if PUBLIC and not AUTH_ENABLED:
        raise RuntimeError("DASHBOARD_PUBLIC is on, but GOOGLE_CLIENT_ID is not set.")
    if AUTH_ENABLED:
        if len(SECRET) < 32:
            raise RuntimeError("DASHBOARD_SECRET must be at least 32 characters (python -c \"import secrets; print(secrets.token_hex(32))\").")
        if not (ADMIN_EMAILS or ALLOWED_EMAILS or ALLOWED_DOMAINS or OPEN_SIGNUP):
            raise RuntimeError("Nobody could sign in. Set ADMIN_EMAILS (and ALLOWED_EMAILS or ALLOWED_DOMAINS), or OPEN_SIGNUP=1.")


# ------------------------------------------------------------------ request helpers
def client_ip(request: Request) -> str:
    return config.client_ip(request)


def _https(request: Request) -> bool:
    if COOKIE_SECURE in ("1", "true", "yes"):
        return True
    if COOKIE_SECURE in ("0", "false", "no"):
        return False
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https" or request.url.scheme == "https"


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True                      # no Origin header: not a cross-site browser request (curl, etc.)
    host = request.headers.get("host", "")
    return urlparse(origin).netloc == host or origin.rstrip("/") in EXTRA_ORIGINS


def _safe_next(n: Optional[str]) -> str:
    return n if n and n.startswith("/") and not n.startswith("//") and "\\" not in n and "\n" not in n else "/"


def is_authed(request: Request) -> bool:
    return getattr(request.state, "user", None) is not None


def current_user(request: Request) -> dict:
    """Dependency: the signed-in user as {"id", "email", "name", "picture", "role"}."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Please sign in.")
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    """Dependency: like current_user, but only for admins."""
    if user["role"] != "admin":
        raise HTTPException(403, "Only an administrator can do this.")
    return user


# ------------------------------------------------------------------ Google token check
def _verify_google(credential: str) -> dict:
    """Blocking: checks signature, audience, issuer and expiry. Raises ValueError for a bad token."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token
    return id_token.verify_oauth2_token(credential, google_requests.Request(), GOOGLE_CLIENT_ID, clock_skew_in_seconds=10)


# ------------------------------------------------------------------ routes
router = APIRouter()


class GoogleBody(BaseModel):
    credential: str


@router.post("/api/auth/google")
async def google_login(body: GoogleBody, request: Request):
    if not AUTH_ENABLED:
        return {"ok": True}
    ip, now = client_ip(request), time.time()
    q = _tries[ip]
    while q and now - q[0] > LOGIN_WINDOW:
        q.popleft()
    if len(q) >= MAX_LOGIN_TRIES:
        return JSONResponse({"detail": "Too many sign-in attempts. Please wait a few minutes."}, 429)
    q.append(now)

    try:
        info = await asyncio.to_thread(_verify_google, body.credential[:4096])
    except ValueError:
        return JSONResponse({"detail": "Google sign-in could not be verified. Please try again."}, 401)
    except Exception as exc:  # noqa: BLE001 - e.g. Google's key server unreachable
        print(f"[auth] Google verification error: {type(exc).__name__}: {exc}", flush=True)
        return JSONResponse({"detail": "Couldn't reach Google to verify the sign-in. Please try again."}, 502)

    email = str(info.get("email", "")).lower()
    if not email or info.get("email_verified") is not True:
        return JSONResponse({"detail": "Your Google account has no verified email address."}, 403)
    if not _email_allowed(email):
        return JSONResponse({"detail": "This Google account hasn't been invited yet. Ask the administrator for access."}, 403)

    result = await asyncio.to_thread(_upsert_user, {**info, "email": email})
    if result is None:
        return JSONResponse({"detail": "This account has been deactivated."}, 403)
    user, session_version = result
    resp = JSONResponse({"ok": True, "user": user})
    resp.set_cookie(COOKIE, make_token(user["id"], session_version), max_age=int(SESSION_HOURS * 3600),
                    httponly=True, samesite="lax", secure=_https(request), path="/")
    return resp


@router.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/api/auth/status")
async def status(request: Request):
    return {"auth_enabled": AUTH_ENABLED, "user": getattr(request.state, "user", None)}


@router.get("/api/me")
async def me(user: dict = Depends(current_user)):
    return user


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · NSE MCP Dashboard</title><style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d131a;color:#e4ebf2;font-family:"Segoe UI",system-ui,sans-serif}
.card{width:min(360px,92vw);background:#151f2a;border:1px solid #233242;border-radius:10px;padding:26px}
h1{font-size:18px;margin:0 0 4px}p{margin:0 0 18px;color:#8596a6;font-size:13px;line-height:1.5}
#g{min-height:44px;display:flex;justify-content:center}#err{min-height:18px;margin-top:14px;font-size:13px;color:#ef5a5a;text-align:center}
.fine{margin:16px 0 0;font-size:11px;color:#8596a6;text-align:center}
</style></head><body><div class="card"><h1>NSE MCP Dashboard</h1><p>Sign in with your Google account to continue. Access is by invitation.</p>
<div id="g"></div><div id="err" role="alert"></div>
<p class="fine">Information only, not investment advice.</p></div>
<script>
const next = __NEXT__, clientId = __CLIENT_ID__, err = document.getElementById("err");
async function onCredential(resp) {
  err.textContent = "";
  try {
    const r = await fetch("/api/auth/google", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ credential: resp.credential }) });
    const d = await r.json().catch(() => ({}));
    if (r.ok) { location.href = next; return; }
    err.textContent = d.detail || "Couldn't sign in.";
  } catch (x) { err.textContent = "Couldn't reach the server."; }
}
function initGoogle() {
  google.accounts.id.initialize({ client_id: clientId, callback: onCredential });
  google.accounts.id.renderButton(document.getElementById("g"), { theme: "outline", size: "large", text: "signin_with", shape: "pill" });
}
</script>
<script src="https://accounts.google.com/gsi/client" async defer onload="initGoogle()"></script>
</body></html>"""

LOGIN_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/client; "
             "style-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/style; img-src 'self' data:; "
             "connect-src 'self' https://accounts.google.com/gsi/; frame-src https://accounts.google.com/gsi/; "
             "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    nxt = _safe_next(next)
    if not AUTH_ENABLED or is_authed(request):
        return RedirectResponse(nxt, status_code=303)
    html = (LOGIN_HTML.replace("__NEXT__", json.dumps(nxt).replace("<", "\\u003c"))
            .replace("__CLIENT_ID__", json.dumps(GOOGLE_CLIENT_ID).replace("<", "\\u003c")))
    # Own CSP (Google's script and iframe must load here) and a referrer policy Google's button works with.
    return HTMLResponse(html, headers={"Content-Security-Policy": LOGIN_CSP, "Referrer-Policy": "strict-origin-when-cross-origin"})


# ------------------------------------------------------------------ middleware
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"),
}


async def guard(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and not _same_origin(request):
        return JSONResponse({"detail": "Cross-site request blocked."}, 403)
    path = request.url.path
    if not AUTH_ENABLED:
        request.state.user = _local_user_cache or await asyncio.to_thread(_local_user)
    else:
        parsed = parse_token(request.cookies.get(COOKIE))
        request.state.user = await asyncio.to_thread(_load_user, *parsed) if parsed else None
        if request.state.user is None and path not in OPEN_PATHS:
            if path.startswith("/api/") or request.method not in ("GET", "HEAD"):
                return JSONResponse({"detail": "Please sign in.", "code": "auth"}, 401)
            return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if AUTH_ENABLED:
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def install(app: FastAPI) -> None:
    validate()
    db.init_db()
    app.include_router(router)
    app.middleware("http")(guard)
