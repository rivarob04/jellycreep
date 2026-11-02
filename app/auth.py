"""Authentication for JellyCreep itself.

Roles
-----
admin   Authentik user in ADMIN_GROUP (forward-auth headers or OIDC). Everything.
member  Any other Authentik user. Read-only access to the full dashboard.
viewer  A Jellyfin user who signed in with their Jellyfin username/password.
        Only ever sees their own dashboard (/me) — never admin pages, other
        users, IP addresses or settings. A Jellyfin *administrator* signing in
        this way is still only a viewer: admin access always goes through
        Authentik (MFA).

Other apps (Homepage, scripts, *arr webhooks) use API tokens, sent as
`X-Api-Key: <token>` or `?apikey=<token>`.
"""
from __future__ import annotations

import hmac
import ipaddress
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from fastapi import HTTPException, Request
from sqlalchemy import select

from .config import settings
from .db import ApiToken, SessionLocal, get_setting, hash_token, utcnow


@dataclass
class User:
    name: str
    is_admin: bool
    via: str  # forward | oidc | jellyfin | token | none
    email: str | None = None
    groups: list[str] = field(default_factory=list)
    role: str = "member"  # admin | member | viewer
    jf_id: str | None = None
    server_id: int | None = None

    @property
    def is_viewer(self) -> bool:
        return self.role == "viewer"


class LoginRequired(Exception):
    pass


class ViewerOnly(Exception):
    """A signed-in Jellyfin viewer asked for a page that isn't theirs."""


def _is_admin(groups: list[str]) -> bool:
    return not settings.admin_group or settings.admin_group in groups


def _peer_trusted(request: Request) -> bool:
    host = request.client.host if request.client else None
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in settings.trusted_proxies)


_from_trusted_proxy = _peer_trusted


def client_ip(request: Request) -> str:
    """Best guess at the visitor's IP (for rate limiting only)."""
    if _peer_trusted(request):
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --- settings that can be changed in the UI ---------------------------------------
_CACHE: dict = {"at": 0.0, "val": None}


def login_settings() -> dict:
    """{'enabled': bool, 'server_id': int|None, 'upcoming': bool}, cached for 15 s."""
    if time.time() - _CACHE["at"] < 15 and _CACHE["val"] is not None:
        return _CACHE["val"]
    val = get_setting("jellyfin_login", None) or {}
    env = (settings.jellyfin_login_default or "").lower() in ("1", "true", "yes", "on")
    out = {"enabled": bool(val.get("enabled", env)), "server_id": val.get("server_id"),
           "upcoming": bool(val.get("upcoming", True)), "recent": bool(val.get("recent", True)),
           "requests": bool(val.get("requests", True)), "server_activity": bool(val.get("server_activity", True)),
           "seerr_discover": bool(val.get("seerr_discover", True)), "seerr_everyone": bool(val.get("seerr_everyone", True)),
           "seerr_autolink": bool(val.get("seerr_autolink", False))}
    _CACHE.update(at=time.time(), val=out)
    return out


def bust_login_cache() -> None:
    _CACHE.update(at=0.0, val=None)


def jellyfin_login_enabled() -> bool:
    return login_settings()["enabled"]


# --- resolving the current user ---------------------------------------------------
def _token_user(request: Request) -> User | None:
    token = request.headers.get("x-api-key") or request.query_params.get("apikey")
    if not token:
        return None
    with SessionLocal() as db:
        row = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(token)))
        if not row:
            raise HTTPException(401, "Invalid API token")
        row.last_used_at = utcnow()
        db.commit()
        return User(name=f"token:{row.name}", is_admin=False, via="token", role="member")


def browser_user(request: Request) -> User | None:
    mode = settings.auth_mode
    if mode == "none":
        return User(name="admin", is_admin=True, via="none", role="admin")
    if mode == "authentik_forward":
        name = request.headers.get(settings.user_header)
        if name and _peer_trusted(request):
            groups = [g for g in (request.headers.get(settings.groups_header) or "").split("|") if g]
            admin = _is_admin(groups)
            return User(name=name, is_admin=admin, via="forward", email=request.headers.get(settings.email_header),
                        groups=groups, role="admin" if admin else "member")
    data = request.session.get("user") if "session" in request.scope else None
    if not data:
        return None
    via = data.get("via", "oidc")
    if via == "jellyfin":
        if not jellyfin_login_enabled():
            return None
        return User(name=data["name"], is_admin=False, via="jellyfin", role="viewer",
                    jf_id=data.get("jf_id"), server_id=data.get("server_id"))
    if via == "oidc" and mode == "oidc":
        admin = bool(data.get("is_admin"))
        return User(name=data["name"], is_admin=admin, via="oidc", email=data.get("email"),
                    groups=data.get("groups", []), role="admin" if admin else "member")
    return None


# Paths a viewer may use. Everything else is denied (default-deny).
VIEWER_PREFIXES = ("/me", "/partials/me", "/img/", "/arrimg/", "/auth/logout", "/logout", "/login")


def viewer_allowed(path: str) -> bool:
    for p in VIEWER_PREFIXES:
        if p.endswith("/"):
            if path.startswith(p):
                return True
        elif path == p or path.startswith(p + "/"):
            return True
    return False


REVALIDATE_EVERY = 6 * 3600


async def _revalidate(request: Request, user: User) -> None:
    """Every few hours, make sure the Jellyfin account still exists and isn't disabled."""
    data = request.session.get("user") or {}
    if time.time() - float(data.get("checked") or 0) < REVALIDATE_EVERY:
        return
    from .db import Service
    from .services import client_for
    from .upstream import UpstreamError
    with SessionLocal() as db:
        svc = db.get(Service, user.server_id) if user.server_id else None
    if not svc:
        request.session.clear()
        raise LoginRequired()
    try:
        client = await client_for(svc)
        info = await client.get(f"/Users/{user.jf_id}")
        if (info.get("Policy") or {}).get("IsDisabled"):
            request.session.clear()
            raise LoginRequired()
    except UpstreamError as e:
        if "404" in str(e) or "400" in str(e):
            request.session.clear()
            raise LoginRequired()
        return  # Jellyfin unreachable: don't lock people out, try again next time
    data["checked"] = time.time()
    request.session["user"] = data


async def current_user(request: Request) -> User:
    """Dependency for pages."""
    user = browser_user(request)
    if not user:
        if settings.auth_mode == "oidc" or jellyfin_login_enabled():
            raise LoginRequired()
        raise HTTPException(401, "Not authenticated. Put JellyCreep behind your Authentik outpost "
                                 "(or check TRUSTED_PROXIES / the X-authentik-username header).")
    if user.is_viewer:
        await _revalidate(request, user)
        if not viewer_allowed(request.url.path):
            raise ViewerOnly()
    return user


async def staff_user(request: Request) -> User:
    """Admins and members only — never Jellyfin viewers."""
    user = await current_user(request)
    if user.is_viewer:
        raise ViewerOnly()
    return user


async def viewer_or_staff(request: Request) -> User:
    return await current_user(request)


def api_user(request: Request) -> User:
    """Dependency for /api: API token, or a signed-in Authentik user (never a Jellyfin viewer)."""
    user = _token_user(request) or browser_user(request)
    if not user:
        raise HTTPException(401, "API token required (X-Api-Key header or ?apikey=)")
    if user.is_viewer:
        raise HTTPException(403, "Not available")
    return user


async def admin_user(request: Request) -> User:
    user = await current_user(request)
    if user.is_viewer:
        raise ViewerOnly()
    if not user.is_admin:
        raise HTTPException(403, f"Admin only — add your Authentik user to the '{settings.admin_group}' group")
    return user


# --- login rate limiting ------------------------------------------------------------
class Limiter:
    """Sliding-window limiter: failures per IP and per username."""

    def __init__(self, per_ip: int = 10, per_user: int = 5, window: int = 900) -> None:
        self.per_ip, self.per_user, self.window = per_ip, per_user, window
        self.ip: dict[str, deque] = defaultdict(deque)
        self.user: dict[str, deque] = defaultdict(deque)

    def _trim(self, q: deque) -> None:
        cutoff = time.time() - self.window
        while q and q[0] < cutoff:
            q.popleft()

    def blocked(self, ip: str, username: str) -> bool:
        a, b = self.ip[ip], self.user[username.lower()]
        self._trim(a)
        self._trim(b)
        return len(a) >= self.per_ip or len(b) >= self.per_user

    def fail(self, ip: str, username: str) -> None:
        now = time.time()
        self.ip[ip].append(now)
        self.user[username.lower()].append(now)

    def ok(self, ip: str, username: str) -> None:
        self.user.pop(username.lower(), None)


limiter = Limiter()


# --- CSRF (double-submit cookie) ------------------------------------------------
CSRF_COOKIE = "jc_csrf"


def csrf_token(request: Request) -> str:
    tok = request.cookies.get(CSRF_COOKIE)
    if not tok:
        tok = getattr(request.state, "new_csrf", None) or secrets.token_urlsafe(24)
        request.state.new_csrf = tok
    return tok


async def verify_csrf(request: Request) -> None:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()
        sent = form.get("csrf", "")
    if not cookie or not hmac.compare_digest(cookie, str(sent)):
        raise HTTPException(403, "CSRF check failed — reload the page and try again")


# --- OIDC ------------------------------------------------------------------------
oauth = None
if settings.auth_mode == "oidc":
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        "authentik",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=f"{settings.oidc_issuer}/.well-known/openid-configuration",
        client_kwargs={"scope": settings.oidc_scopes, "code_challenge_method": "S256"},
    )


def user_from_claims(claims: dict) -> dict:
    groups = claims.get("groups") or []
    return {
        "via": "oidc",
        "name": claims.get("preferred_username") or claims.get("name") or claims.get("email") or claims.get("sub"),
        "email": claims.get("email"),
        "groups": groups,
        "is_admin": _is_admin(groups),
    }
