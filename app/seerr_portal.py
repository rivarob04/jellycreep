"""Seerr inside the Jellyfin-user portal, in the spirit of Moonfin's integration.

* Each viewer is matched to their own Seerr account (by Jellyfin user id, then username).
* Browsing uses the Seerr API key; anything that *does* something (request, cancel,
  approve) is sent with ``X-API-User: <their Seerr id>`` so Seerr applies that person's
  permissions, quotas and auto-approve rules and records them as the requester.
* "Who requested what" comes from Seerr's request list; only display names are shown
  (never e-mail addresses).
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from .upstream import UpstreamError

TMDB = "https://image.tmdb.org/t/p/"

# Seerr permission bits
P_ADMIN, P_MANAGE, P_REQUEST = 2, 16, 32
P_R4K, P_R4K_MOVIE, P_R4K_TV = 1024, 2048, 4096
P_REQ_MOVIE, P_REQ_TV = 262144, 524288

# media.status / status4k
MEDIA_STATUS = {2: ("Pending", "b-warn"), 3: ("Requested", "b-acc"), 4: ("Partly available", "b-acc"),
                5: ("Available", "b-ok"), 6: ("Blocked", "b-bad"), 7: ("Removed", "b-bad")}
# request.status
REQ_STATUS = {1: ("Waiting for approval", "b-warn"), 2: ("Approved", "b-acc"), 3: ("Declined", "b-bad"),
              4: ("Failed", "b-bad"), 5: ("Available", "b-ok")}

ROWS = [
    ("trending", "Trending", "/api/v1/discover/trending"),
    ("movies", "Popular movies", "/api/v1/discover/movies"),
    ("tv", "Popular series", "/api/v1/discover/tv"),
    ("upcoming-movies", "Upcoming movies", "/api/v1/discover/movies/upcoming"),
    ("upcoming-tv", "Upcoming series", "/api/v1/discover/tv/upcoming"),
]
ROW_BY_KEY = {k: (t, p) for k, t, p in ROWS}

# Seerr's Networks / Studios tiles (same TMDB company ids and logos Seerr uses)
LOGO = "https://image.tmdb.org/t/p/w780_filter(duotone,ffffff,bababa)"
NETWORKS = [
    (213, "Netflix", "/wwemzKWzjKYJFfCeiB57q3r4Bcm.png"), (2739, "Disney+", "/gJ8VX6JSu3ciXHuC2dDGAo2lvwM.png"),
    (1024, "Prime Video", "/ifhbNuuVnlwYy5oXA5VIb2YR8AZ.png"), (2552, "Apple TV+", "/4KAy34EHvRM25Ih8wb82AuGU7zJ.png"),
    (453, "Hulu", "/pqUTCleNUiTLAVlelGxUgWn1ELh.png"), (49, "HBO", "/tuomPhY2UtuPTqqFnKMVHvSb724.png"),
    (4353, "Discovery+", "/1D1bS3Dyw4ScYnFWTlBOvJXC3nb.png"), (2, "ABC", "/ndAvF4JLsliGreX87jAc9GdjmJY.png"),
    (19, "FOX", "/1DSpHrWyOORkL9N2QHX7Adt31mQ.png"), (359, "Cinemax", "/6mSHSquNpfLgDdv6VnOOvC5Uz2h.png"),
    (174, "AMC", "/pmvRmATOCaDykE6JrVoeYxlFHw3.png"), (67, "Showtime", "/Allse9kbjiP6ExaQrnSpIhkurEi.png"),
    (318, "Starz", "/8GJjw3HHsAJYwIWKIPBPfqMxlEa.png"), (71, "The CW", "/ge9hzeaU7nMtQ4PjkFlc68dGAJ9.png"),
    (6, "NBC", "/o3OedEP0f9mfZr33jz2BfXOUK5.png"), (16, "CBS", "/nm8d7P7MJNiBLdgIzUK0gkuEA4r.png"),
    (4330, "Paramount+", "/fi83B1oztoS47xxcemFdPMhIzK.png"), (4, "BBC One", "/mVn7xESaTNmjBUyUtGNvDQd3CT1.png"),
    (56, "Cartoon Network", "/c5OC6oVCg6QP4eqzW6XIq17CQjI.png"), (80, "Adult Swim", "/9AKyspxVzywuaMuZ1Bvilu8sXly.png"),
    (13, "Nickelodeon", "/ikZXxg6GnwpzqiZbRPhJGaZapqB.png"), (3353, "Peacock", "/gIAcGTjKKr0KOHL5s4O36roJ8p7.png"),
]
STUDIOS = [
    (2, "Disney", "/wdrCwmRnLFJhEoH8GSfymY85KHT.png"), (127928, "20th Century Studios", "/h0rjX5vjW5r8yEnUBStFarjcLT4.png"),
    (34, "Sony Pictures", "/GagSvqWlyPdkFHMfQ3pNq6ix9P.png"), (174, "Warner Bros. Pictures", "/ky0xOc5OrhzkZ1N6KyUxacfQsCk.png"),
    (33, "Universal", "/8lvHyhjr8oUKOOy2dKXoALWKdp0.png"), (4, "Paramount", "/fycMZt242LVjagMByZOLUGbCvv3.png"),
    (3, "Pixar", "/1TjvGVDMYsj6JBxOAkUHpPEwLf7.png"), (521, "DreamWorks", "/kP7t6RwGz2AvvTkvnI1uteEwHet.png"),
    (420, "Marvel Studios", "/hUzeosd33nzE5MCNsZxCGEKTXaQ.png"), (9993, "DC", "/2Tc1P3Ac8M479naPp1kYT3izLS5.png"),
    (41077, "A24", "/1ZXsGaFPgrgS6ZZGS37AqD5uU12.png"),
]
COMPANIES = {"network": {i: (n, LOGO + p) for i, n, p in NETWORKS},
             "studio": {i: (n, LOGO + p) for i, n, p in STUDIOS}}
TILES = {k: [{"id": i, "name": n, "logo": l} for i, (n, l) in v.items()] for k, v in COMPANIES.items()}
FILTERS = {"all": "All", "pending": "Waiting", "processing": "Approved", "available": "Available"}

# --------------------------------------------------------------------------- cache
_C: dict[Any, tuple[float, Any]] = {}


_REFRESHING: set = set()


async def cached(key: Any, ttl: float, fn: Callable[[], Awaitable[Any]], stale: float = 6 * 3600) -> Any:
    """Cache with stale-while-revalidate: once something has been fetched, later visits get the last
    answer instantly (for up to ``stale`` seconds past expiry) while a fresh copy loads in the background."""
    hit = _C.get(key)
    now = time.time()
    if hit and hit[0] > now:
        return hit[1]
    if hit and hit[0] + stale > now:
        if key not in _REFRESHING:
            _REFRESHING.add(key)

            async def refresh():
                try:
                    _C[key] = (time.time() + ttl, await fn())
                except Exception:  # noqa: BLE001 – keep serving the old copy
                    pass
                finally:
                    _REFRESHING.discard(key)
            asyncio.get_running_loop().create_task(refresh())
        return hit[1]
    val = await fn()
    _C[key] = (now + ttl, val)
    if len(_C) > 800:
        for k in [k for k, (exp, _) in _C.items() if exp + stale < now][:400]:
            _C.pop(k, None)
    return val


def forget(prefix: str) -> None:
    for k in [k for k in _C if isinstance(k, tuple) and k and k[0] == prefix]:
        _C.pop(k, None)


# --------------------------------------------------------------------------- helpers
def img(path: str | None, size: str = "w300") -> str | None:
    return f"{TMDB}{size}{path}" if path else None


def _norm(x: Any) -> str:
    return str(x or "").replace("-", "").lower()


def who(rb: dict | None) -> str:
    rb = rb or {}
    n = rb.get("displayName") or rb.get("jellyfinUsername") or rb.get("username") or "Someone"
    return n.split("@")[0] if "@" in n else n      # never show an e-mail address


def colour(name: str) -> str:
    h = int(hashlib.md5(name.lower().encode()).hexdigest()[:4], 16) % 360
    return f"hsl({h} 55% 45%)"


def err_text(e: Exception) -> str:
    s = str(e)
    m = re.search(r'"message"\s*:\s*"([^"]+)"', s)
    if m:
        return m.group(1)
    if "403" in s:
        return "Seerr says you don't have permission to do that."
    if "409" in s:
        return "That's already been requested."
    return s[:200]


def _when(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{int(secs // n)}{unit} ago"
    return "just now"


# --------------------------------------------------------------------------- release dates
from datetime import date as _date


def _d(s: str | None) -> _date | None:
    try:
        return _date.fromisoformat((s or "")[:10]) if s else None
    except ValueError:
        return None


def nice_date(d: _date | None, weekday: bool = True) -> str:
    if not d:
        return ""
    return (f"{d:%A}, " if weekday else "") + f"{d:%B} {d.day}, {d.year}"


def short_date(d: _date | None) -> str:
    return f"{d:%b} {d.day}, {d.year}" if d else ""


def relative(d: _date | None) -> str | None:
    if not d:
        return None
    n = (d - _date.today()).days
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n == -1:
        return "yesterday"
    if 0 < n < 60:
        return f"in {n} days"
    if 60 <= n < 400:
        return f"in {round(n / 30)} months"
    if -60 < n < 0:
        return f"{-n} days ago"
    return None


RELEASE_TYPES = {1: "Premiere", 2: "Limited release", 3: "In cinemas", 4: "Digital", 5: "Blu-ray / DVD", 6: "On TV"}
TV_STATUS = {"Returning Series": "Returning series", "Ended": "Ended", "Canceled": "Cancelled",
             "In Production": "In production", "Planned": "Planned", "Pilot": "Pilot"}


async def region(client, svc_id: int) -> str:
    """Seerr's configured region (Settings → General → Discover region), for local release dates."""
    async def load():
        try:
            data = await client.get("/api/v1/settings/main")
        except Exception:  # noqa: BLE001 – only used to pick local dates; never break the page
            return ""
        return str(data.get("region") or "") if isinstance(data, dict) else ""
    return await cached(("region", svc_id), 3600, load)


def _safe_release(d: dict, mt: str, reg: str) -> dict:
    try:
        return release_info(d, mt, reg)
    except Exception:  # noqa: BLE001 – odd data from Seerr: just leave the dates out
        return {"rows": [], "status": None}


def release_info(d: dict, mt: str, reg: str) -> dict:
    rows: list[dict] = []

    def add(label: str, day: _date | None, extra: str | None = None):
        if day:
            rows.append({"label": label, "date": nice_date(day), "rel": relative(day), "extra": extra,
                         "future": day > _date.today()})

    status = None
    if mt == "movie":
        by_country = {r.get("iso_3166_1"): r.get("release_dates") or [] for r in (d.get("releases") or {}).get("results") or []}
        picks = by_country.get(reg) or by_country.get("CA") or by_country.get("US") or []
        seen = set()
        for rd in sorted(picks, key=lambda x: x.get("release_date") or ""):
            t = rd.get("type")
            if t in (3, 4, 5) and t not in seen:          # cinemas, digital, physical
                seen.add(t)
                add(RELEASE_TYPES[t], _d(rd.get("release_date")))
        if not rows:
            add("Release date", _d(d.get("releaseDate")))
        status = d.get("status") if d.get("status") not in (None, "Released") else None
    else:
        first = _d(d.get("firstAirDate"))
        add("Premieres" if first and first > _date.today() else "First aired", first)
        nxt = d.get("nextEpisodeToAir") or {}
        if nxt.get("airDate"):
            code = f"S{nxt.get('seasonNumber')} • E{nxt.get('episodeNumber')}"
            name = nxt.get("name")
            if not (first and _d(nxt["airDate"]) == first):
                add("Next episode", _d(nxt["airDate"]), code + (f" · {name}" if name and not name.startswith("Episode ") else ""))
        if d.get("status") in ("Ended", "Canceled"):
            add("Last aired", _d(d.get("lastAirDate")))
        status = TV_STATUS.get(d.get("status"), d.get("status"))
    return {"rows": rows, "status": status}


def card(r: dict) -> dict | None:
    mt = r.get("mediaType") or ("tv" if r.get("firstAirDate") or (r.get("name") and not r.get("title")) else "movie")
    if mt not in ("movie", "tv") or not r.get("id"):
        return None
    mi = r.get("mediaInfo") or {}
    st = mi.get("status") or 1
    return {"type": mt, "id": r["id"], "title": r.get("title") or r.get("name") or "Untitled",
            "year": (r.get("releaseDate") or r.get("firstAirDate") or "")[:4],
            "poster": img(r.get("posterPath")), "backdrop": img(r.get("backdropPath"), "w1280"),
            "rating": round(r["voteAverage"], 1) if r.get("voteAverage") else None,
            "overview": r.get("overview") or "", "status": st, "badge": MEDIA_STATUS.get(st),
            "status4k": mi.get("status4k") or 1, "jf_hint": mi.get("jellyfinMediaId"),
            **_soon(r.get("releaseDate") or r.get("firstAirDate"))}


def _soon(raw: str | None) -> dict:
    d = _d(raw)
    return {"soon": short_date(d) if d and d > _date.today() else None}


def perms(u: dict | None) -> dict:
    p = int((u or {}).get("permissions") or 0)
    adm = bool(p & P_ADMIN)
    return {"movie": adm or bool(p & (P_REQUEST | P_REQ_MOVIE)), "tv": adm or bool(p & (P_REQUEST | P_REQ_TV)),
            "4k_movie": adm or bool(p & (P_R4K | P_R4K_MOVIE)), "4k_tv": adm or bool(p & (P_R4K | P_R4K_TV)),
            "manage": adm or bool(p & P_MANAGE)}


def progress(media: dict | None, is4k: bool = False) -> int | None:
    ds = (media or {}).get("downloadStatus4k" if is4k else "downloadStatus") or []
    size = sum(float(d.get("size") or 0) for d in ds)
    left = sum(float(d.get("sizeLeft") or 0) for d in ds)
    return max(0, min(100, round(100 * (1 - left / size)))) if size else None


# --------------------------------------------------------------------------- Seerr account
async def _all_users(client) -> list[dict]:
    out, skip = [], 0
    while True:
        page = await client.get("/api/v1/user", params={"take": "200", "skip": str(skip)}) or {}
        res = page.get("results") or []
        out.extend(res)
        skip += len(res)
        if not res or skip >= int((page.get("pageInfo") or {}).get("results") or 0):
            return out


def _match(users: list[dict], jf_id: str, name: str) -> dict | None:
    for u in users:
        if jf_id and _norm(u.get("jellyfinUserId")) == jf_id:
            return u
    for u in users:
        if name.lower() in {str(u.get(k) or "").lower() for k in ("jellyfinUsername", "username")}:
            return u
    return None


async def account(client, svc_id: int, user, autolink: bool) -> dict | None:
    """The viewer's Seerr user, creating it from Jellyfin if allowed."""
    users = await cached(("users", svc_id), 300, lambda: _all_users(client))
    u = _match(users, user.jf_id, user.name)
    if u or not (autolink and user.jf_id):
        return u
    try:
        await client.post("/api/v1/user/import-from-jellyfin", json={"jellyfinUserIds": [user.jf_id]})
    except UpstreamError:
        return None
    forget("users")
    users = await cached(("users", svc_id), 300, lambda: _all_users(client))
    return _match(users, user.jf_id, user.name)


async def quota(client, uid: int) -> dict | None:
    try:
        q = await client.get(f"/api/v1/user/{uid}/quota") or {}
    except UpstreamError:
        return None
    out = {}
    for k in ("movie", "tv"):
        v = q.get(k) or {}
        if v.get("limit"):
            out[k] = {"limit": v["limit"], "days": v.get("days"), "remaining": v.get("remaining"),
                      "restricted": v.get("restricted")}
    return out or None


async def has_4k(client, svc_id: int) -> dict:
    async def load():
        out = {"movie": False, "tv": False}
        for kind, path in (("movie", "radarr"), ("tv", "sonarr")):
            try:
                out[kind] = any(s.get("is4k") for s in (await client.get(f"/api/v1/settings/{path}") or []))
            except UpstreamError:
                pass
        return out
    return await cached(("4k", svc_id), 900, load)


# --------------------------------------------------------------------------- browsing
async def _row(client, svc_id: int, key: str, page: int = 1) -> list[dict]:
    path = ROW_BY_KEY[key][1]

    async def load():
        data = await client.get(path, params={"page": str(page)}) or {}
        return [c for c in (card(r) for r in data.get("results") or []) if c]
    return await cached(("row", svc_id, key, page), 600, load)


async def discover(client, svc_id: int) -> list[dict]:
    async def one(key, title):
        try:
            return {"key": key, "title": title, "items": await _row(client, svc_id, key)}
        except UpstreamError:
            return None
    rows = await asyncio.gather(*(one(k, t) for k, t, _ in ROWS))
    return [r for r in rows if r and r["items"]]


async def browse(client, svc_id: int, key: str, page: int) -> list[dict]:
    return await _row(client, svc_id, key, page)


async def browse_company(client, svc_id: int, kind: str, cid: int, page: int) -> list[dict]:
    """Everything from one TV network or movie studio (Seerr's /discover/tv/network|movies/studio)."""
    path = f"/api/v1/discover/tv/network/{cid}" if kind == "network" else f"/api/v1/discover/movies/studio/{cid}"

    async def load():
        data = await client.get(path, params={"page": str(page)}) or {}
        return [c for c in (card(r) for r in data.get("results") or []) if c]
    return await cached(("co", svc_id, kind, cid, page), 1800, load)


async def search(client, q: str, page: int = 1) -> list[dict]:
    # Seerr wants %20-style encoding, not '+', so build the query string ourselves
    data = await client.get(f"/api/v1/search?query={quote(q, safe='')}&page={page}") or {}
    return [c for c in (card(r) for r in data.get("results") or []) if c]


async def _meta(client, svc_id: int, mt: str, tmdb: int) -> dict:
    async def load():
        try:
            d = await client.get(f"/api/v1/{mt}/{tmdb}") or {}
        except UpstreamError:
            return {}
        return {"title": d.get("title") or d.get("name"), "year": (d.get("releaseDate") or d.get("firstAirDate") or "")[:4],
                "poster": img(d.get("posterPath")), "backdrop": img(d.get("backdropPath"), "w1280")}
    return await cached(("meta", svc_id, mt, tmdb), 86400, load)


def _req_row(r: dict, me: int | None, everyone: bool, can_manage: bool) -> dict | None:
    rb = r.get("requestedBy") or {}
    mine = me is not None and rb.get("id") == me
    if not (mine or everyone):
        return None
    media = r.get("media") or {}
    st = r.get("status") or 1
    mst = media.get("status4k" if r.get("is4k") else "status")
    if st == 2 and mst in (3, 4, 5):
        badge = MEDIA_STATUS[mst] if mst != 3 else ("Downloading" if progress(media, r.get("is4k")) is not None else "Approved", "b-acc")
    else:
        badge = REQ_STATUS.get(st, ("Requested", ""))
    name = who(rb)
    created = _when(r.get("createdAt"))
    return {"id": r.get("id"), "type": "movie" if r.get("type") == "movie" else "tv", "tmdb": media.get("tmdbId"),
            "jf_hint": media.get("jellyfinMediaId"),
            "by": "You" if mine else name, "by_name": name, "initial": name[:1].upper(), "colour": colour(name),
            "mine": mine, "is4k": bool(r.get("is4k")), "badge": badge, "pending": st == 1,
            "seasons": sorted(s.get("seasonNumber") for s in r.get("seasons") or [] if s.get("seasonNumber") is not None),
            "created": created, "ago": ago(created), "progress": progress(media, r.get("is4k")) if st == 2 else None,
            "can_cancel": mine and st == 1, "can_manage": can_manage and st == 1,
            "modified_by": who(r.get("modifiedBy")) if r.get("modifiedBy") and st in (2, 3) else None}


async def feed(client, svc_id: int, me: int | None, *, everyone: bool, can_manage: bool = False,
               flt: str = "all", page: int = 1, take: int = 20) -> tuple[list[dict], int]:
    params = {"take": str(take), "skip": str((page - 1) * take), "sort": "added", "filter": flt if flt in FILTERS else "all"}
    if not everyone:
        if me is None:
            return [], 0
        params["requestedBy"] = str(me)
    data = await client.get("/api/v1/request", params=params) or {}
    rows = [x for x in (_req_row(r, me, everyone, can_manage) for r in data.get("results") or []) if x]
    metas = await asyncio.gather(*(_meta(client, svc_id, x["type"], x["tmdb"]) if x["tmdb"] else asyncio.sleep(0, {}) for x in rows))
    for x, m in zip(rows, metas):
        x.update(title=(m or {}).get("title") or "Unknown title", year=(m or {}).get("year"),
                 poster=(m or {}).get("poster"), backdrop=(m or {}).get("backdrop"))
    total = int((data.get("pageInfo") or {}).get("results") or len(rows))
    return rows, max(1, -(-total // take))


async def raw_title(client, svc_id: int, mt: str, tmdb: int) -> dict:
    """Seerr's full record for one title — shared by everyone, kept warm, dropped after a request."""
    async def load():
        return await client.get(f"/api/v1/{mt}/{tmdb}") or {}
    return await cached(("title", svc_id, mt, tmdb), 120, load, stale=1800)


async def detail(client, svc_id: int, mt: str, tmdb: int, me: int | None, *, everyone: bool, can_manage: bool) -> dict:
    d, reg = await asyncio.gather(raw_title(client, svc_id, mt, tmdb), region(client, svc_id))
    d = d or {}
    c = card({**d, "mediaType": mt}) or {}
    mi = d.get("mediaInfo") or {}
    reqs = [x for x in (_req_row({**r, "media": {**mi, **(r.get("media") or {})}}, me, everyone, can_manage)
                        for r in mi.get("requests") or []) if x]
    hidden = len(mi.get("requests") or []) - len(reqs)
    trailer = next((v.get("url") for v in d.get("relatedVideos") or [] if v.get("type") == "Trailer" and v.get("url")), None)
    out = {**c, "tagline": d.get("tagline"), "genres": [g.get("name") for g in d.get("genres") or []][:3],
           "runtime": d.get("runtime") or ((d.get("episodeRunTime") or [None])[0]),
           "requests": reqs, "hidden_requests": max(0, hidden), "trailer": trailer,
           "network": ((d.get("networks") or [{}])[0] or {}).get("name") if mt == "tv" else None,
           "cast": cast(d), "release": _safe_release(d, mt, reg), "seasons": []}
    if mt == "tv":
        # each season's own status from Seerr: 2 pending, 3 requested/processing, 4 partly, 5 available
        avail = {s.get("seasonNumber"): s.get("status") for s in mi.get("seasons") or []}
        asked: dict[int, int] = {}
        whole_show = False       # a request Seerr didn't list seasons for = the whole show
        for r in mi.get("requests") or []:
            if r.get("is4k") or r.get("status") == 3:      # ignore 4K and declined requests
                continue
            seasons = r.get("seasons") or []
            if not seasons:
                whole_show = True
            for s in seasons:
                asked[s.get("seasonNumber")] = r.get("status") or 1
        if (mi.get("status") or 1) in (2, 3) and not avail and not asked:
            whole_show = True    # Seerr says the show is requested but gives no season detail
        for s in d.get("seasons") or []:
            n = s.get("seasonNumber")
            if not n:
                continue        # skip specials
            st = avail.get(n) or 1
            if st in (4, 5):
                tag = MEDIA_STATUS[st]
            elif st == 3:
                tag = ("Requested", "b-acc")
            elif st == 2:
                tag = ("Waiting", "b-warn")
            elif n in asked:
                tag = ("Waiting", "b-warn") if asked[n] == 1 else ("Requested", "b-acc")
            elif whole_show:
                tag = ("Requested", "b-acc")
            else:
                tag = None
            out["seasons"].append({"n": n, "name": s.get("name") or f"Season {n}", "eps": s.get("episodeCount"),
                                   "year": (s.get("airDate") or "")[:4], "tag": tag, "free": tag is None})
        out["free_seasons"] = [s["n"] for s in out["seasons"] if s["free"]]
    return out


def cast(d: dict, n: int = 24) -> list[dict]:
    """TMDB cast from Seerr's credits, in billing order."""
    out, seen = [], set()
    for c in sorted((d.get("credits") or {}).get("cast") or [], key=lambda c: c.get("order", 999)):
        if not c.get("id") or c["id"] in seen:
            continue
        seen.add(c["id"])
        out.append({"id": c["id"], "name": c.get("name") or "?", "role": c.get("character") or None,
                    "photo": img(c.get("profilePath"), "w185")})
    return out[:n]


async def person(client, svc_id: int, pid: int) -> dict:
    """An actor from TMDB (via Seerr): bio and every movie / show they're in, with request status."""
    async def load():
        p, credits = await asyncio.gather(client.get(f"/api/v1/person/{pid}"),
                                          client.get(f"/api/v1/person/{pid}/combined_credits"))
        p, credits = p or {}, credits or {}
        movies, shows, seen = [], [], set()
        for r in credits.get("cast") or []:
            c = card(r)
            if not c or (c["type"], c["id"]) in seen:
                continue
            seen.add((c["type"], c["id"]))
            c["role"] = r.get("character") or None
            c["date"] = r.get("releaseDate") or r.get("firstAirDate") or ""
            (movies if c["type"] == "movie" else shows).append(c)
        for lst in (movies, shows):
            lst.sort(key=lambda c: c["date"] or "0000", reverse=True)
        born, died = p.get("birthday"), p.get("deathday")
        age = None
        if born:
            try:
                b = datetime.fromisoformat(born[:10]).date()
                e = datetime.fromisoformat(died[:10]).date() if died else datetime.now(timezone.utc).date()
                age = e.year - b.year - ((e.month, e.day) < (b.month, b.day))
            except ValueError:
                pass
        fmt = lambda s: (lambda d: f"{d:%B} {d.day}, {d.year}")(datetime.fromisoformat(s[:10])) if s else None  # noqa: E731
        return {"id": pid, "name": p.get("name") or "Unknown", "bio": p.get("biography") or None,
                "photo": img(p.get("profilePath"), "h632"), "born": fmt(born), "died": fmt(died), "age": age,
                "place": p.get("placeOfBirth"), "known_for": p.get("knownForDepartment"), "imdb": p.get("imdbId"),
                "movies": movies[:150], "shows": shows[:150]}
    return await cached(("person", svc_id, pid), 3600, load)


async def similar(client, svc_id: int, mt: str, tmdb: int) -> list[dict]:
    async def load():
        try:
            data = await client.get(f"/api/v1/{mt}/{tmdb}/recommendations", params={"page": "1"}) or {}
        except UpstreamError:
            return []
        return [c for c in (card({**r, "mediaType": r.get("mediaType") or mt}) for r in data.get("results") or []) if c][:16]
    return await cached(("similar", svc_id, mt, tmdb), 3600, load)


# --------------------------------------------------------------------------- actions (as the viewer)
async def make_request(client, uid: int, mt: str, tmdb: int, seasons: list[int] | None, is4k: bool) -> dict:
    body: dict[str, Any] = {"mediaType": mt, "mediaId": tmdb, "is4k": is4k}
    if mt == "tv":
        body["seasons"] = seasons or "all"
    return await client.request("POST", "/api/v1/request", json=body, headers={"X-API-User": str(uid)}) or {}


async def cancel(client, uid: int, req_id: int) -> None:
    await client.request("DELETE", f"/api/v1/request/{req_id}", headers={"X-API-User": str(uid)})


async def moderate(client, uid: int, req_id: int, action: str) -> None:
    if action not in ("approve", "decline"):
        raise UpstreamError("Unknown action")
    await client.request("POST", f"/api/v1/request/{req_id}/{action}", headers={"X-API-User": str(uid)})
