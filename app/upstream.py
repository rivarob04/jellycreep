"""HTTP access to upstream apps, including apps that sit behind Authentik MFA.

Each configured service gets its own httpx client (and cookie jar), so the
`authentik_proxy_*` session cookie the outpost hands back is reused instead of
re-authenticating every request, as Authentik recommends for machine clients.

Proxy auth modes
----------------
authentik_basic   Authorization: Basic <user>:<app password>. The Authentik proxy
                  provider exchanges it for a session; works with MFA enabled
                  because app passwords are tokens, not interactive logins.
authentik_token   We fetch a JWT from Authentik's token endpoint with the
                  client_credentials grant (username + app password) and send
                  Authorization: Bearer <jwt>. Refreshed automatically.
headers           Only the custom headers below are sent (Cloudflare Access
                  service tokens, Pomerium, a shared secret header, ...).

Custom headers are added in every mode. Each app's own credential travels in a
header that is not Authorization (X-Emby-Token, X-Api-Key, cookie...), so it
doesn't collide with the proxy's Authorization header, which Authentik strips
before forwarding.
"""
from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .db import Service


class UpstreamError(Exception):
    pass


class ProxyAuthError(UpstreamError):
    """The request was stopped by the identity proxy in front of the app."""


@dataclass
class _Token:
    value: str
    expires_at: float


_AUTH_WALL_MARKERS = ("/outpost.goauthentik.io", "/application/o/authorize", "/if/flow/", "cloudflareaccess.com", "/oauth2/start")


class Upstream:
    def __init__(self, svc: Service) -> None:
        self.svc = svc
        self.base = svc.url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base,
            timeout=svc.timeout or 15,
            verify=svc.verify_tls,
            follow_redirects=False,
            headers={"User-Agent": "JellyCreep/0.1", "Accept": "application/json"},
        )
        self._token: _Token | None = None
        self._lock = asyncio.Lock()
        self.app_headers: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- proxy auth -------------------------------------------------------------
    async def _fetch_authentik_token(self) -> _Token:
        svc = self.svc
        if not (svc.authentik_url and svc.authentik_client_id and svc.proxy_username and svc.proxy_password):
            raise ProxyAuthError("Authentik token mode needs the Authentik URL, client ID, username and app password")
        url = svc.authentik_url.rstrip("/") + "/application/o/token/"
        async with httpx.AsyncClient(timeout=15, verify=svc.verify_tls) as c:
            r = await c.post(url, data={
                "grant_type": "client_credentials",
                "client_id": svc.authentik_client_id,
                "username": svc.proxy_username,
                "password": svc.proxy_password,
                "scope": "openid profile",
            })
        if r.status_code != 200:
            raise ProxyAuthError(f"Authentik token request failed ({r.status_code}): {r.text[:200]}")
        data = r.json()
        return _Token(data["access_token"], time.time() + int(data.get("expires_in", 300)) - 30)

    async def _proxy_headers(self, force_refresh: bool = False) -> dict[str, str]:
        svc = self.svc
        headers = dict(svc.extra_headers)
        if svc.proxy_auth == "authentik_basic":
            if not (svc.proxy_username and svc.proxy_password):
                raise ProxyAuthError("Authentik basic mode needs a username and app password")
            raw = f"{svc.proxy_username}:{svc.proxy_password}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        elif svc.proxy_auth == "authentik_token":
            async with self._lock:
                if force_refresh or not self._token or self._token.expires_at < time.time():
                    self._token = await self._fetch_authentik_token()
            headers["Authorization"] = f"Bearer {self._token.value}"
        return headers

    # -- requests ---------------------------------------------------------------
    @staticmethod
    def _looks_like_auth_wall(r: httpx.Response) -> bool:
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location", "")
            return any(m in loc for m in _AUTH_WALL_MARKERS) or "authentik" in loc.lower()
        ctype = r.headers.get("content-type", "")
        if "text/html" in ctype and r.status_code in (200, 401, 403):
            body = r.text[:4000].lower()
            return "authentik" in body or "cloudflare access" in body
        return False

    async def request(self, method: str, path: str, *, expect_json: bool = True, _retry: bool = True, **kw: Any) -> Any:
        extra = kw.pop("headers", {})
        headers = {**await self._proxy_headers(), **self.app_headers, **extra}
        try:
            r = await self._client.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as e:
            raise UpstreamError(f"Could not reach {self.base}: {e.__class__.__name__}: {e}") from e

        if self._looks_like_auth_wall(r):
            if _retry and self.svc.proxy_auth == "authentik_token":
                await self._proxy_headers(force_refresh=True)
                return await self.request(method, path, expect_json=expect_json, _retry=False, headers=extra, **kw)
            hint = ("Set 'Proxy auth' for this service, or add an Authentik bypass for the API path."
                    if self.svc.proxy_auth == "none" else "Check the proxy credentials and that the user may access this application in Authentik.")
            raise ProxyAuthError(f"Blocked by the login proxy in front of {self.base}. {hint}")
        if r.status_code in (301, 302, 307, 308):
            raise UpstreamError(f"Unexpected redirect to {r.headers.get('location')} — check the URL (http vs https, base path)")
        if r.status_code == 401:
            raise UpstreamError("401 Unauthorized — check the API key / credentials")
        if r.status_code >= 400:
            raise UpstreamError(f"HTTP {r.status_code}: {r.text[:200]}")
        if not expect_json:
            return r
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            raise UpstreamError(f"Expected JSON but got {r.headers.get('content-type', 'unknown content')} — is the URL right?")

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)


class UpstreamPool:
    """Caches one Upstream per service, rebuilt when the service is edited."""

    def __init__(self) -> None:
        self._items: dict[int, tuple[Any, Upstream]] = {}

    async def get(self, svc: Service, factory) -> Any:
        stamp = svc.updated_at
        cached = self._items.get(svc.id)
        if cached and cached[0] == stamp:
            return cached[1]
        if cached:
            await cached[1].aclose()
        client = factory(svc)
        self._items[svc.id] = (stamp, client)
        return client

    async def drop(self, svc_id: int) -> None:
        cached = self._items.pop(svc_id, None)
        if cached:
            await cached[1].aclose()

    async def close(self) -> None:
        for _, c in self._items.values():
            await c.aclose()
        self._items.clear()


pool = UpstreamPool()
