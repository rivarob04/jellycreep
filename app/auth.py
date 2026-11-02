"""Authentication for JellyCreep itself.

* Browser users come through Authentik: either forward-auth/proxy headers
  (AUTH_MODE=authentik_forward) or a native OIDC login (AUTH_MODE=oidc).
* Other apps (JellyWatch, Homepage, scripts, *arr webhooks) use API tokens,
  sent as `X-Api-Key: <token>` or `?apikey=<token>`.
"""
from __future__ import annotations

import hmac
import ipaddress
import secrets
from dataclasses import dataclass, field

from fastapi import HTTPException, Request
from sqlalchemy import select

from .config import settings
from .db import ApiToken, SessionLocal, hash_token, utcnow


@dataclass
class User:
    name: str
    is_admin: bool
    via: str  # forward | oidc | token | none
    email: str | None = None
    groups: list[str] = field(default_factory=list)


class LoginRequired(Exception):
    pass


def _is_admin(groups: list[str]) -> bool:
    return not settings.admin_group or settings.admin_group in groups


def _from_trusted_proxy(request: Request) -> bool:
    host = request.client.host if request.client else None
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in settings.trusted_proxies)


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
        return User(name=f"token:{row.name}", is_admin=False, via="token")


def browser_user(request: Request) -> User | None:
    mode = settings.auth_mode
    if mode == "none":
        return User(name="admin", is_admin=True, via="none")
    if mode == "authentik_forward":
        name = request.headers.get(settings.user_header)
        if name and _from_trusted_proxy(request):
            groups = [g for g in (request.headers.get(settings.groups_header) or "").split("|") if g]
            return User(name=name, is_admin=_is_admin(groups), via="forward",
                        email=request.headers.get(settings.email_header), groups=groups)
        return None
    if mode == "oidc":
        data = request.session.get("user")
        if data:
            return User(name=data["name"], is_admin=data["is_admin"], via="oidc", email=data.get("email"),
                        groups=data.get("groups", []))
        return None
    return None


def current_user(request: Request) -> User:
    """Dependency for pages: redirects to login (OIDC) or 401s."""
    user = browser_user(request)
    if user:
        return user
    if settings.auth_mode == "oidc":
        raise LoginRequired()
    raise HTTPException(401, "Not authenticated. Put JellyCreep behind your Authentik outpost "
                             "(or check TRUSTED_PROXIES / the X-authentik-username header).")


def api_user(request: Request) -> User:
    """Dependency for /api: API token, or a logged-in browser user."""
    user = _token_user(request) or browser_user(request)
    if not user:
        raise HTTPException(401, "API token required (X-Api-Key header or ?apikey=)")
    return user


def admin_user(request: Request) -> User:
    user = current_user(request)
    if not user.is_admin:
        raise HTTPException(403, f"Admin only — add your Authentik user to the '{settings.admin_group}' group")
    return user


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
        "name": claims.get("preferred_username") or claims.get("name") or claims.get("email") or claims.get("sub"),
        "email": claims.get("email"),
        "groups": groups,
        "is_admin": _is_admin(groups),
    }
