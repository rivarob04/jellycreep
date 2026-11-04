"""Clients for each supported app, built on Upstream (so they all work behind Authentik)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .db import Service
from .upstream import Upstream, UpstreamError, pool


class JellyfinClient(Upstream):
    def __init__(self, svc: Service) -> None:
        super().__init__(svc)
        key = (svc.api_key or "").strip()
        mb = (f'MediaBrowser Client="JellyCreep", Device="JellyCreep", DeviceId="jellycreep-{svc.id}", '
              f'Version="0.6.0", Token="{key}"')
        # Jellyfin 10.11+ only accepts `Authorization: MediaBrowser Token=…` by default
        # (legacy X-Emby-Token is off). Older servers still read X-Emby-Token.
        self.app_headers = {"X-Emby-Authorization": mb, "X-Emby-Token": key}
        if svc.proxy_auth in ("none", "headers"):
            # Authorization is free (no Authentik credentials going in it), so use it.
            self.app_headers["Authorization"] = mb

    async def authenticate(self, username: str, password: str) -> dict | None:
        """Check a Jellyfin username/password. Returns the auth result, None if wrong, raises if unreachable.
        Sent WITHOUT JellyCreep's admin API key."""
        import hashlib
        dev = hashlib.sha1(f"jc-login-{username.lower()}".encode()).hexdigest()[:16]
        auth = (f'MediaBrowser Client="JellyCreep", Device="JellyCreep sign-in", DeviceId="{dev}", '
                f'Version="0.6.0"')
        headers = {**await self._proxy_headers(), "X-Emby-Authorization": auth, "Content-Type": "application/json",
                   "Accept": "application/json"}
        if "Authorization" not in headers:
            headers["Authorization"] = auth
        try:
            r = await self._client.post("/Users/AuthenticateByName", json={"Username": username, "Pw": password},
                                        headers=headers)
        except httpx.HTTPError as e:
            raise UpstreamError(f"Could not reach Jellyfin: {e.__class__.__name__}") from e
        if r.status_code in (400, 401, 403, 404):
            return None
        if r.status_code >= 300:
            raise UpstreamError(f"Jellyfin answered HTTP {r.status_code}")
        return r.json()

    async def end_session(self, token: str) -> None:
        """Log out the throwaway session created by authenticate(), so it doesn't pile up under Devices."""
        auth = f'MediaBrowser Client="JellyCreep", Device="JellyCreep sign-in", DeviceId="x", Version="0.6.0", Token="{token}"'
        headers = {**await self._proxy_headers(), "X-Emby-Authorization": auth}
        if "Authorization" not in headers:
            headers["Authorization"] = auth
        try:
            await self._client.post("/Sessions/Logout", headers=headers)
        except httpx.HTTPError:
            pass

    async def test(self) -> str:
        info = await self.get("/System/Info")
        return f"Jellyfin {info.get('Version')} – {info.get('ServerName')}"

    async def sessions(self) -> list[dict]:
        return await self.get("/Sessions") or []

    async def users(self) -> list[dict]:
        return await self.get("/Users") or []

    async def system_info(self) -> dict:
        return await self.get("/System/Info") or {}

    async def libraries(self) -> list[dict]:
        return await self.get("/Library/VirtualFolders") or []

    async def counts(self) -> dict:
        return await self.get("/Items/Counts") or {}

    async def media_page(self, start: int, limit: int = 200) -> dict:
        """Movies + episodes with their media streams, for the quality report."""
        return await self.get("/Items", params={
            "Recursive": "true", "IncludeItemTypes": "Movie,Episode", "Fields": "MediaSources",
            "EnableImages": "false", "EnableUserData": "false", "StartIndex": str(start), "Limit": str(limit),
            "SortBy": "SortName", "IsMissing": "false"}) or {}

    async def storage(self) -> dict:
        """Jellyfin 10.11+: per-library and server-folder disk usage (admin key)."""
        return await self.get("/System/Info/Storage") or {}

    async def recently_added(self, limit: int = 18, user_id: str | None = None) -> list[dict]:
        extra = {"userId": user_id} if user_id else {}
        data = await self.get("/Items", params={**extra,
            "SortBy": "DateCreated", "SortOrder": "Descending", "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode,MusicAlbum", "Limit": str(limit),
            "Fields": "DateCreated,ProductionYear,Overview", "IsMissing": "false",
        })
        return (data or {}).get("Items", [])

    async def stop(self, session_id: str) -> None:
        await self.post(f"/Sessions/{session_id}/Playing/Stop", expect_json=False)

    async def message(self, session_id: str, header: str, text: str) -> None:
        await self.post(f"/Sessions/{session_id}/Message", json={"Header": header, "Text": text, "TimeoutMs": 10000},
                        expect_json=False)

    async def image(self, item_id: str, kind: str = "Primary", max_height: int = 300):
        return await self.get(f"/Items/{item_id}/Images/{kind}", params={"maxHeight": str(max_height), "quality": "85"},
                              expect_json=False, headers={"Accept": "image/*"})


class ArrClient(Upstream):
    """Sonarr / Radarr (v3 API) and Lidarr / Prowlarr (v1 API)."""

    def __init__(self, svc: Service) -> None:
        super().__init__(svc)
        self.app_headers = {"X-Api-Key": svc.api_key or ""}
        self.v = "v1" if svc.kind in ("lidarr", "prowlarr") else "v3"

    def p(self, path: str) -> str:
        return f"/api/{self.v}/{path.lstrip('/')}"

    async def test(self) -> str:
        st = await self.get(self.p("system/status"))
        return f"{st.get('appName', self.svc.kind_label)} {st.get('version')}"

    async def health(self) -> list[dict]:
        return await self.get(self.p("health")) or []

    async def queue(self) -> list[dict]:
        params = {"pageSize": "50"}
        if self.svc.kind == "sonarr":
            params.update(includeSeries="true", includeEpisode="true")
        elif self.svc.kind == "radarr":
            params.update(includeMovie="true")
        elif self.svc.kind == "lidarr":
            params.update(includeArtist="true", includeAlbum="true")
        data = await self.get(self.p("queue"), params=params)
        return (data or {}).get("records", [])

    async def calendar(self, start=None, end=None, days: int = 14) -> list[dict]:
        now = datetime.now(timezone.utc)
        start = start or (now - timedelta(days=1)).date()
        end = end or (now + timedelta(days=days)).date()
        params = {"start": start.isoformat(), "end": end.isoformat(), "unmonitored": "false"}
        if self.svc.kind == "sonarr":
            params.update(includeSeries="true", includeEpisodeFile="false")
        elif self.svc.kind == "lidarr":
            params["includeArtist"] = "true"
        return await self.get(self.p("calendar"), params=params) or []

    async def cover(self, item_id: int, cover: str = "poster"):
        """Poster/fanart for a series (Sonarr) or movie (Radarr), small size."""
        size = "-250" if cover == "poster" else "-360"
        return await self.get(self.p(f"mediacover/{item_id}/{cover}{size}.jpg"), expect_json=False,
                              headers={"Accept": "image/*"})

    async def history(self, size: int = 25) -> list[dict]:
        params = {"pageSize": str(size), "sortKey": "date", "sortDirection": "descending"}
        if self.svc.kind == "sonarr":
            params.update(includeSeries="true", includeEpisode="true")
        elif self.svc.kind == "radarr":
            params.update(includeMovie="true")
        data = await self.get(self.p("history"), params=params)
        return (data or {}).get("records", [])

    async def diskspace(self) -> list[dict]:
        return await self.get(self.p("diskspace")) or []

    async def indexers(self) -> list[dict]:
        return await self.get(self.p("indexer")) or []

    async def indexer_status(self) -> list[dict]:
        return await self.get(self.p("indexerstatus")) or []

    async def queue_retry(self, queue_id: int) -> None:
        """Remove a stuck/failed download, blocklist the release, and let the *arr search for another."""
        await self.request("DELETE", self.p(f"queue/{queue_id}"),
                           params={"removeFromClient": "true", "blocklist": "true", "skipRedownload": "false"})

    async def queue_remove(self, queue_id: int) -> None:
        await self.request("DELETE", self.p(f"queue/{queue_id}"),
                           params={"removeFromClient": "true", "blocklist": "false", "skipRedownload": "true"})

    # --- search & add (Sonarr / Radarr) ---
    async def lookup(self, term: str) -> list[dict]:
        path = "series/lookup" if self.svc.kind == "sonarr" else "movie/lookup"
        return (await self.get(self.p(path), params={"term": term}) or [])[:20]

    async def profiles(self) -> list[dict]:
        return await self.get(self.p("qualityprofile")) or []

    async def root_folders(self) -> list[dict]:
        return await self.get(self.p("rootfolder")) or []

    async def add(self, item: dict, profile_id: int, root: str, monitor: str, search: bool) -> dict:
        body = dict(item)
        body.update(qualityProfileId=profile_id, rootFolderPath=root, monitored=monitor != "none")
        if self.svc.kind == "sonarr":
            body.setdefault("languageProfileId", 1)
            body["seasonFolder"] = True
            body["addOptions"] = {"monitor": monitor if monitor != "none" else "none",
                                  "searchForMissingEpisodes": search, "searchForCutoffUnmetEpisodes": False}
            return await self.post(self.p("series"), json=body) or {}
        body["minimumAvailability"] = body.get("minimumAvailability") or "released"
        body["addOptions"] = {"searchForMovie": search, "monitor": "movieOnly" if monitor != "none" else "none"}
        return await self.post(self.p("movie"), json=body) or {}


class QbitClient(Upstream):
    def __init__(self, svc: Service) -> None:
        super().__init__(svc)
        # qBittorrent's CSRF check compares Referer/Origin with the Host it sees.
        self.app_headers = {"Referer": self.base, "Origin": self.base}
        self._logged_in = False

    async def _login(self) -> None:
        if not self.svc.username:
            self._logged_in = True  # auth bypassed for this subnet in qBittorrent
            return
        r = await self.post("/api/v2/auth/login", data={"username": self.svc.username, "password": self.svc.password or ""},
                            expect_json=False)
        if r.text.strip() != "Ok.":
            raise UpstreamError(f"qBittorrent login failed: {r.text.strip()[:100] or r.status_code}")
        self._logged_in = True

    async def call(self, path: str, **kw: Any) -> Any:
        if not self._logged_in:
            await self._login()
        try:
            return await self.get(path, **kw)
        except UpstreamError as e:
            if "403" in str(e):
                self._logged_in = False
                await self._login()
                return await self.get(path, **kw)
            raise

    async def call_post(self, path: str, data: dict) -> Any:
        if not self._logged_in:
            await self._login()
        try:
            return await self.post(path, data=data, expect_json=False)
        except UpstreamError as e:
            if "403" in str(e):
                self._logged_in = False
                await self._login()
                return await self.post(path, data=data, expect_json=False)
            raise

    async def set_paused(self, paused: bool, hashes: str = "all") -> None:
        # qBittorrent 5 renamed pause/resume to stop/start; try the new name first.
        new, old = ("stop", "pause") if paused else ("start", "resume")
        try:
            await self.call_post(f"/api/v2/torrents/{new}", {"hashes": hashes})
        except UpstreamError as e:
            if "404" not in str(e):
                raise
            await self.call_post(f"/api/v2/torrents/{old}", {"hashes": hashes})

    async def test(self) -> str:
        r = await self.call("/api/v2/app/version", expect_json=False)
        return f"qBittorrent {r.text.strip()}"

    async def torrents(self, limit: int = 50) -> list[dict]:
        return await self.call("/api/v2/torrents/info", params={"sort": "added_on", "reverse": "true", "limit": str(limit)}) or []

    async def transfer(self) -> dict:
        return await self.call("/api/v2/transfer/info") or {}


class SeerrClient(Upstream):
    def __init__(self, svc: Service) -> None:
        super().__init__(svc)
        self.app_headers = {"X-Api-Key": svc.api_key or ""}

    async def test(self) -> str:
        st = await self.get("/api/v1/status")
        return f"Seerr {st.get('version')}"

    async def requests(self, take: int = 20) -> list[dict]:
        data = await self.get("/api/v1/request", params={"take": str(take), "skip": "0", "sort": "added", "filter": "all"})
        results = (data or {}).get("results", [])

        async def title(req: dict) -> None:
            media = req.get("media") or {}
            tmdb = media.get("tmdbId")
            kind = "movie" if req.get("type") == "movie" else "tv"
            if not tmdb:
                return
            req["_link"] = f"{self.svc.link}/{kind}/{tmdb}"
            try:
                d = await self.get(f"/api/v1/{kind}/{tmdb}")
                req["_title"] = d.get("title") or d.get("name")
                req["_year"] = (d.get("releaseDate") or d.get("firstAirDate") or "")[:4]
                if d.get("posterPath"):
                    req["_poster"] = f"https://image.tmdb.org/t/p/w185{d['posterPath']}"
            except UpstreamError:
                pass

        await asyncio.gather(*(title(r) for r in results))
        return results

    async def counts(self) -> dict:
        return await self.get("/api/v1/request/count") or {}

    async def set_status(self, request_id: int, action: str) -> None:
        if action not in ("approve", "decline"):
            raise UpstreamError("Unknown action")
        await self.post(f"/api/v1/request/{request_id}/{action}")


class JellystatClient(Upstream):
    """Jellystat has no formal API docs; these are the endpoints its own UI and
    the Homepage widget use, authenticated with an API key from
    Jellystat → Settings → API Keys."""

    def __init__(self, svc: Service) -> None:
        super().__init__(svc)
        self.app_headers = {"x-api-token": svc.api_key or ""}

    async def test(self) -> str:
        await self.get("/stats/getViewsByLibraryType", params={"days": "30"})
        return "Jellystat reachable"

    async def views_by_type(self, days: int) -> dict:
        return await self.get("/stats/getViewsByLibraryType", params={"days": str(days)}) or {}

    async def most_active_users(self, days: int) -> list[dict]:
        return await self.post("/stats/getMostActiveUsers", json={"days": days}) or []

    async def history_page(self, page: int, size: int = 250) -> dict:
        return await self.get("/api/getHistory", params={"page": str(page), "size": str(size),
                                                         "sort": "ActivityDateInserted", "desc": "true"}) or {}

    async def most_viewed(self, days: int, kind: str) -> list[dict]:
        return await self.post("/stats/getMostViewedByType", json={"days": days, "type": kind}) or []


class SabnzbdClient(Upstream):
    """SABnzbd's API takes the key as a query parameter: /api?mode=…&output=json&apikey=…"""

    async def api(self, mode: str, **params: str) -> dict:
        data = await self.get("/api", params={"mode": mode, "output": "json", "apikey": self.svc.api_key or "", **params})
        if isinstance(data, dict) and data.get("error"):
            raise UpstreamError(f"SABnzbd: {data['error']}")
        return data or {}

    async def test(self) -> str:
        await self.api("queue", limit="1")  # version needs no key; queue proves the key works
        ver = await self.api("version")
        return f"SABnzbd {ver.get('version', '')}".strip()

    async def queue(self, limit: int = 30) -> dict:
        return (await self.api("queue", limit=str(limit))).get("queue", {})

    async def history(self, limit: int = 20) -> list[dict]:
        return (await self.api("history", limit=str(limit))).get("history", {}).get("slots", [])

    async def set_paused(self, paused: bool, nzo_id: str | None = None) -> None:
        if nzo_id:
            await self.api("queue", name="pause" if paused else "resume", value=nzo_id)
        else:
            await self.api("pause" if paused else "resume")

    async def retry(self, nzo_id: str) -> None:
        await self.api("retry", value=nzo_id)


class WizarrClient(Upstream):
    """Wizarr's REST API (/api/docs on your instance). Key from Wizarr → Settings → API Keys."""

    def __init__(self, svc: Service) -> None:
        super().__init__(svc)
        self.app_headers = {"X-API-Key": svc.api_key or ""}

    async def test(self) -> str:
        v = await self.get("/api/version")
        return f"Wizarr {v.get('version', '')}".strip()

    async def status(self) -> dict:
        return await self.get("/api/status") or {}

    async def servers(self) -> list[dict]:
        return (await self.get("/api/servers") or {}).get("servers", [])

    async def libraries(self) -> list[dict]:
        return (await self.get("/api/libraries") or {}).get("libraries", [])

    async def invitations(self) -> list[dict]:
        return (await self.get("/api/invitations") or {}).get("invitations", [])

    async def users(self) -> list[dict]:
        return (await self.get("/api/users") or {}).get("users", [])

    async def create_invitation(self, body: dict) -> dict:
        data = await self.post("/api/invitations", json=body)
        return (data or {}).get("invitation", {})

    async def delete_invitation(self, inv_id: int) -> None:
        await self.request("DELETE", f"/api/invitations/{inv_id}")

    async def user_action(self, user_id: int, action: str, days: int = 30) -> str:
        if action == "delete":
            data = await self.request("DELETE", f"/api/users/{user_id}")
        elif action == "extend":
            data = await self.post(f"/api/users/{user_id}/extend", json={"days": days})
        elif action in ("enable", "disable"):
            data = await self.post(f"/api/users/{user_id}/{action}")
        else:
            raise UpstreamError(f"Unknown action {action}")
        return (data or {}).get("message", "Done")


FACTORIES = {
    "jellyfin": JellyfinClient,
    "sonarr": ArrClient,
    "radarr": ArrClient,
    "lidarr": ArrClient,
    "prowlarr": ArrClient,
    "sabnzbd": SabnzbdClient,
    "qbittorrent": QbitClient,
    "jellyseerr": SeerrClient,
    "jellystat": JellystatClient,
    "wizarr": WizarrClient,
}


async def client_for(svc: Service) -> Any:
    return await pool.get(svc, FACTORIES[svc.kind])
