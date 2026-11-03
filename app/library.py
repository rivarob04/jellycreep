"""Browse the Jellyfin library, Tautulli Remote style: recently added, item details
(ratings, cast, file info), seasons and episodes.

Everything is fetched *as a Jellyfin user* (``userId=…``) so people who signed in with
Jellyfin only ever see libraries their own account can access. Staff browse as the
first Jellyfin administrator.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from .upstream import UpstreamError

KINDS = {"all": ("All", None), "movies": ("Movies", "Movie"), "shows": ("Shows", "Episode"), "music": ("Music", "MusicAlbum")}
_ADMIN: dict[int, tuple[float, str | None]] = {}


async def browse_as(client, svc_id: int, user) -> str | None:
    """Jellyfin user id to browse as."""
    if user.is_viewer:
        return user.jf_id
    hit = _ADMIN.get(svc_id)
    if hit and hit[0] > time.time():
        return hit[1]
    users = await client.get("/Users") or []
    adm = next((u for u in users if (u.get("Policy") or {}).get("IsAdministrator")), users[0] if users else None)
    uid = (adm or {}).get("Id")
    _ADMIN[svc_id] = (time.time() + 3600, uid)
    return uid


async def _get(client, path: str, uid: str, params: dict | None = None) -> Any:
    """10.9+ takes ?userId=; older servers only have /Users/{id}/… routes."""
    params = dict(params or {})
    try:
        return await client.get(path, params={**params, "userId": uid})
    except UpstreamError as e:
        if "404" in str(e) and path.startswith("/Items"):
            return await client.get(f"/Users/{uid}{path}", params=params)
        raise


def _when(s: str | None) -> datetime | None:
    """Jellyfin dates look like 2024-05-01T12:34:56.1234567Z."""
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    secs = max(0, (datetime.now(timezone.utc) - dt).total_seconds())
    for unit, n in (("year", 31536000), ("month", 2592000), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= n:
            v = int(secs // n)
            return f"{v} {unit}{'s' if v != 1 else ''} ago"
    return "just now"


def ep_code(i: dict) -> str:
    s, e = i.get("ParentIndexNumber"), i.get("IndexNumber")
    if s is None and e is None:
        return ""
    return f"S{s if s is not None else '?'} • E{e if e is not None else '?'}"


def _poster_id(i: dict) -> str | None:
    if (i.get("ImageTags") or {}).get("Primary") and i.get("Type") != "Episode":
        return i.get("Id")
    return i.get("SeriesId") or i.get("ParentId") or i.get("Id") if i.get("Type") in ("Episode", "Season") else i.get("Id")


def _backdrop_id(i: dict) -> str | None:
    if i.get("BackdropImageTags"):
        return i.get("Id")
    return i.get("ParentBackdropItemId") or i.get("SeriesId")


def card(i: dict) -> dict:
    t = i.get("Type")
    if t == "Episode":
        title, sub = i.get("SeriesName") or i.get("Name"), [i.get("Name"), ep_code(i)]
    elif t == "Season":
        title, sub = i.get("SeriesName") or i.get("Name"), [i.get("Name")]
    elif t == "Series":
        n = i.get("ChildCount") or 0
        title, sub = i.get("Name"), [f"{n} new episodes" if n > 1 else "Series"]
    elif t == "MusicAlbum":
        title, sub = i.get("Name"), [i.get("AlbumArtist")]
    else:
        title, sub = i.get("Name"), [str(i.get("ProductionYear") or "")]
    added = _when(i.get("DateCreated"))
    return {"id": i.get("Id"), "type": t, "title": title or "Untitled", "sub": [s for s in sub if s],
            "added": added, "ago": ago(added), "poster_id": _poster_id(i), "backdrop_id": _backdrop_id(i),
            "icon": "tv" if t in ("Episode", "Series", "Season") else ("music" if t in ("MusicAlbum", "Audio") else "movie")}


async def latest(client, uid: str, kind: str = "all", limit: int = 80) -> list[dict]:
    params = {"Limit": str(limit), "GroupItems": "true", "ImageTypeLimit": "1",
              "Fields": "DateCreated,ProductionYear,ChildCount,ParentId,PremiereDate",
              "EnableImageTypes": "Primary,Backdrop,Thumb"}
    t = KINDS.get(kind, KINDS["all"])[1]
    if t:
        params["IncludeItemTypes"] = t
    data = await _get(client, "/Items/Latest", uid, params)
    if isinstance(data, dict):           # some versions wrap it
        data = data.get("Items")
    return [card(i) for i in data or []]


# ----------------------------------------------------------------------------- details
def _fmt_date(s: str | None) -> str | None:
    dt = _when(s)
    return f"{dt:%B} {dt.day}, {dt.year}" if dt else None


def _mins(ticks: int | None) -> str | None:
    if not ticks:
        return None
    m = round(ticks / 600_000_000)
    return f"{m // 60} hr {m % 60} min" if m >= 90 else f"{m} minutes"


def _size(n: float | None) -> str | None:
    if not n:
        return None
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit not in ("B", "KB") else f"{n:.0f} {unit}"
        n /= 1024
    return None


def _kbps(b: int | None) -> str | None:
    if not b:
        return None
    return f"{b / 1_000_000:.1f} Mbps" if b >= 1_000_000 else f"{b // 1000} kbps"


def _media(d: dict, show_paths: bool) -> list[dict]:
    out = []
    for src in d.get("MediaSources") or []:
        streams = src.get("MediaStreams") or d.get("MediaStreams") or []
        vids, auds, subs = [], [], []
        for s in streams:
            kind = s.get("Type")
            if kind == "Video":
                res = f"{s.get('Width')}×{s.get('Height')}" if s.get("Width") else None
                fps = s.get("RealFrameRate") or s.get("AverageFrameRate")
                bits = [s.get("DisplayTitle") or (s.get("Codec") or "").upper(), res,
                        f"{round(fps, 3):g} fps" if fps else None,
                        f"{s.get('BitDepth')}-bit" if s.get("BitDepth") else None,
                        s.get("VideoRangeType") if s.get("VideoRangeType") not in (None, "SDR", "Unknown") else None,
                        _kbps(s.get("BitRate"))]
                vids.append(" · ".join(dict.fromkeys(b for b in bits if b)))
            elif kind == "Audio":
                title = s.get("DisplayTitle") or s.get("Codec") or "Audio"
                auds.append(title + (" (default)" if s.get("IsDefault") and "default" not in title.lower() else ""))
            elif kind == "Subtitle":
                subs.append((s.get("DisplayTitle") or s.get("Language") or "Subtitle") + (" (external)" if s.get("IsExternal") else ""))
        path = src.get("Path") or ""
        out.append({"name": src.get("Name"), "container": (src.get("Container") or "").upper() or None,
                    "size": _size(src.get("Size")), "bitrate": _kbps(src.get("Bitrate")),
                    "file": path.replace("\\", "/").rsplit("/", 1)[-1] if show_paths and path else None,
                    "path": path if show_paths else None, "video": vids, "audio": auds, "subs": subs})
    return out


def _people(d: dict, kind: str, n: int) -> str | None:
    names = [p.get("Name") for p in d.get("People") or [] if p.get("Type") == kind and p.get("Name")]
    return ", ".join(list(dict.fromkeys(names))[:n]) or None


def cast(d: dict, n: int = 24) -> list[dict]:
    """Actors (and an episode's guest stars) with their photos, in billing order."""
    out, seen = [], set()
    for p in d.get("People") or []:
        if p.get("Type") not in ("Actor", "GuestStar") or not p.get("Id") or p["Id"] in seen:
            continue
        seen.add(p["Id"])
        out.append({"id": p["Id"], "name": p.get("Name") or "?", "role": p.get("Role"),
                    "has_img": bool(p.get("PrimaryImageTag")), "guest": p.get("Type") == "GuestStar" and not p.get("Role")})
    return out[:n]


def details(d: dict, *, show_paths: bool) -> dict:
    t = d.get("Type")
    pid = d.get("ProviderIds") or {}
    year = d.get("ProductionYear")
    years = None
    if t == "Series" and year:
        end = _when(d.get("EndDate"))
        years = (f"{year}–{end.year}" if end and end.year != year else str(year)) if d.get("Status") == "Ended" else (
            f"{year}–" if d.get("Status") == "Continuing" else str(year))
    facts = [
        ("Network" if t == "Series" else "Studio", ", ".join(s.get("Name") for s in (d.get("Studios") or [])[:2] if s.get("Name")) or None),
        ("Aired" if t in ("Series", "Episode", "Season") else "Released", _fmt_date(d.get("PremiereDate"))),
        ("Status", d.get("Status") if t == "Series" else None),
        ("Runtime", _mins(d.get("RunTimeTicks"))),
        ("Rated", d.get("OfficialRating")),
        ("Genres", ", ".join((d.get("Genres") or [])[:5]) or None),
        ("Directed by", _people(d, "Director", 3)),
        ("Written by", _people(d, "Writer", 3)),
        ("Starring", _people(d, "Actor", 4)),
        ("Seasons", str(d.get("ChildCount")) if t == "Series" and d.get("ChildCount") else None),
        ("Episodes", str(d.get("RecursiveItemCount") or d.get("ChildCount")) if t in ("Series", "Season") and (d.get("RecursiveItemCount") or d.get("ChildCount")) else None),
        ("Added", _fmt_date(d.get("DateCreated"))),
    ]
    links = []
    if pid.get("Imdb"):
        links.append(("IMDb", f"https://www.imdb.com/title/{pid['Imdb']}/"))
    if pid.get("Tmdb") and t in ("Movie", "Series"):
        links.append(("TMDB", f"https://www.themoviedb.org/{'movie' if t == 'Movie' else 'tv'}/{pid['Tmdb']}"))
    if pid.get("Tvdb") and t == "Series":
        links.append(("TheTVDB", f"https://thetvdb.com/?tab=series&id={pid['Tvdb']}"))
    critic = d.get("CriticRating")
    is_ep = t == "Episode"
    return {
        "id": d.get("Id"), "type": t, "name": d.get("Name"),
        "title": d.get("SeriesName") if t in ("Episode", "Season") else d.get("Name"),
        "subtitle": (f"{d.get('Name')}" if t == "Season" else
                     (f"{ep_code(d)} · {d.get('Name')}" if is_ep else (years or (str(year) if year else "")))),
        "tagline": (d.get("Taglines") or [None])[0], "overview": d.get("Overview"),
        "facts": [(k, v) for k, v in facts if v],
        "community": round(d["CommunityRating"], 1) if d.get("CommunityRating") else None,
        "critic": int(critic) if critic is not None else None, "fresh": critic is not None and critic >= 60,
        "links": links,
        "media": _media(d, show_paths) if t in ("Movie", "Episode", "Video", "MusicVideo") else [],
        "series_id": d.get("SeriesId"), "season_id": d.get("SeasonId") or (d.get("ParentId") if is_ep else None),
        "index": d.get("IndexNumber"), "season_index": d.get("ParentIndexNumber"),
        "poster_id": (d.get("Id") if (d.get("ImageTags") or {}).get("Primary") and not is_ep else
                      (d.get("SeasonId") or d.get("SeriesId") or d.get("Id"))),
        "thumb_id": d.get("Id") if is_ep and (d.get("ImageTags") or {}).get("Primary") else None,
        "backdrop_id": _backdrop_id(d), "played": (d.get("UserData") or {}).get("Played"),
        "resume_ticks": (d.get("UserData") or {}).get("PlaybackPositionTicks") or 0,
        "cast": cast(d),
    }


async def item(client, uid: str, item_id: str, *, show_paths: bool) -> dict:
    return details(await _get(client, f"/Items/{item_id}", uid) or {}, show_paths=show_paths)


async def seasons(client, uid: str, series_id: str) -> list[dict]:
    data = await client.get(f"/Shows/{series_id}/Seasons", params={"userId": uid, "Fields": "ChildCount,PremiereDate"}) or {}
    out = []
    for s in data.get("Items") or []:
        out.append({"id": s.get("Id"), "name": s.get("Name") or f"Season {s.get('IndexNumber')}",
                    "index": s.get("IndexNumber"), "count": s.get("ChildCount"),
                    "has_img": bool((s.get("ImageTags") or {}).get("Primary")), "series_id": series_id,
                    "played": (s.get("UserData") or {}).get("Played")})
    return out


async def episodes(client, uid: str, series_id: str, season_id: str) -> list[dict]:
    data = await client.get(f"/Shows/{series_id}/Episodes", params={"userId": uid, "seasonId": season_id,
                                                                    "Fields": "PremiereDate,Overview"}) or {}
    out = []
    for e in data.get("Items") or []:
        out.append({"id": e.get("Id"), "name": e.get("Name"), "index": e.get("IndexNumber"),
                    "has_img": bool((e.get("ImageTags") or {}).get("Primary")),
                    "aired": _fmt_date(e.get("PremiereDate")), "runtime": _mins(e.get("RunTimeTicks")),
                    "played": (e.get("UserData") or {}).get("Played")})
    return out


# ----------------------------------------------------------------------------- watching
_SERVER_IDS: dict[int, tuple[float, str | None]] = {}


async def server_id(client, svc_id: int) -> str | None:
    """Jellyfin's own server id (used in its web links)."""
    hit = _SERVER_IDS.get(svc_id)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        sid = (await client.get("/System/Info/Public") or {}).get("Id")
    except UpstreamError:
        sid = None
    _SERVER_IDS[svc_id] = (time.time() + 3600, sid)
    return sid


def clock(ticks: int) -> str:
    secs = int(ticks / 10_000_000)
    h, m, s = secs // 3600, secs // 60 % 60, secs % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _target(e: dict, resume_ok: bool) -> dict:
    ud = e.get("UserData") or {}
    pos = ud.get("PlaybackPositionTicks") or 0 if resume_ok else 0
    code = ep_code(e).replace(" • ", "") if e.get("Type") == "Episode" else ""
    return {"id": e.get("Id"), "code": code, "name": e.get("Name"), "resume_ticks": pos,
            "resume": clock(pos) if pos > 600_000_000 else None}   # ignore the first minute


async def play_target(client, uid: str, d: dict, *, resume_ok: bool) -> dict | None:
    """What 'Watch' should start: the title itself, or for a show/season the next episode to watch."""
    t = d["type"]
    if t in ("Movie", "Episode", "Video", "MusicVideo"):
        return _target({"Id": d["id"], "Type": t, "Name": d["name"], "ParentIndexNumber": d.get("season_index"),
                        "IndexNumber": d.get("index"), "UserData": {"PlaybackPositionTicks": d.get("resume_ticks")}}, resume_ok)
    try:
        if t == "Series":
            nxt = await client.get("/Shows/NextUp", params={"userId": uid, "seriesId": d["id"], "Limit": "1",
                                                            "Fields": "UserData", "EnableResumable": "true"}) or {}
            items = nxt.get("Items") or []
            if not items:
                eps = await client.get(f"/Shows/{d['id']}/Episodes", params={"userId": uid, "Limit": "1", "Fields": "UserData"}) or {}
                items = eps.get("Items") or []
        elif t == "Season":
            eps = await client.get(f"/Shows/{d['series_id']}/Episodes", params={"userId": uid, "seasonId": d["id"], "Fields": "UserData"}) or {}
            all_eps = eps.get("Items") or []
            items = [e for e in all_eps if not (e.get("UserData") or {}).get("Played")][:1] or all_eps[:1]
        else:
            return None
    except UpstreamError:
        return None
    return _target(items[0], resume_ok) if items else None


async def devices(client, uid: str, *, only_user: str | None) -> list[dict]:
    """Jellyfin apps that can be told to start playing (TV apps, phone apps, browsers)."""
    try:
        data = await client.get("/Sessions", params={"controllableByUserId": uid, "activeWithinSeconds": "1800"}) or []
    except UpstreamError:
        return []
    out = []
    for s in data:
        if not s.get("SupportsRemoteControl") or (s.get("Client") or "").startswith("JellyCreep"):
            continue
        if only_user and (s.get("UserId") or "").replace("-", "").lower() != only_user:
            continue
        playing = (s.get("NowPlayingItem") or {}).get("Name")
        out.append({"id": s.get("Id"), "name": s.get("DeviceName") or s.get("Client") or "Jellyfin",
                    "client": s.get("Client"), "user": s.get("UserName"), "playing": playing})
    return out


async def play_on(client, session_id: str, item_id: str, start_ticks: int = 0) -> None:
    params = {"playCommand": "PlayNow", "itemIds": item_id}
    if start_ticks:
        params["startPositionTicks"] = str(int(start_ticks))
    await client.post(f"/Sessions/{session_id}/Playing", params=params, expect_json=False)


# ----------------------------------------------------------------------------- TMDB -> library
_TMDB_IDX: dict[tuple[int, str], tuple[float, dict]] = {}


def _norm_id(s: str | None) -> str:
    return (s or "").replace("-", "").lower()


async def tmdb_index(client, svc_id: int, uid: str, ttl: float = 300) -> dict:
    """Which movies and series this Jellyfin user can see, keyed by TMDB id.

    Returns ``{"movie": {tmdb: item_id}, "tv": {tmdb: item_id}, "ids": {item_id, …}}``.
    Fetched as the user, so it only holds titles from libraries their account can open.
    """
    key = (svc_id, uid)
    hit = _TMDB_IDX.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    data = await _get(client, "/Items", uid, {"Recursive": "true", "IncludeItemTypes": "Movie,Series",
                                              "Fields": "ProviderIds", "EnableImages": "false",
                                              "EnableUserData": "false", "IsMissing": "false"}) or {}
    idx: dict = {"movie": {}, "tv": {}, "ids": set()}
    for i in data.get("Items") or []:
        iid = _norm_id(i.get("Id"))
        if not iid:
            continue
        idx["ids"].add(iid)
        pid = {k.lower(): v for k, v in (i.get("ProviderIds") or {}).items()}
        tmdb = str(pid.get("tmdb") or "").strip()
        if tmdb.isdigit():
            idx["movie" if i.get("Type") == "Movie" else "tv"].setdefault(int(tmdb), iid)
    _TMDB_IDX[key] = (time.time() + ttl, idx)
    return idx


def in_library(idx: dict | None, mt: str, tmdb: Any, jf_hint: str | None = None) -> str | None:
    """The Jellyfin item id for a TMDB title, or None. ``jf_hint`` is Seerr's own jellyfinMediaId."""
    if not idx:
        return None
    try:
        hit = idx.get(mt, {}).get(int(tmdb))
    except (TypeError, ValueError):
        hit = None
    if hit:
        return hit
    h = _norm_id(jf_hint)
    return h if h and h in idx["ids"] else None


# ----------------------------------------------------------------------------- matching *arr titles to Jellyfin
async def find(client, uid: str, kind: str, title: str, year: int | None, ids: dict) -> dict | None:
    """Find a Series/Movie in Jellyfin (as this user) by TVDB/TMDB/IMDb id, falling back to title + year."""
    try:
        data = await client.get("/Items", params={"userId": uid, "Recursive": "true", "IncludeItemTypes": kind,
                                                  "searchTerm": title, "Fields": "ProviderIds,ProductionYear", "Limit": "25"}) or {}
    except UpstreamError:
        return None
    items = data.get("Items") or []
    want = {k.lower(): str(v).lower() for k, v in ids.items() if v}
    for i in items:
        have = {k.lower(): str(v).lower() for k, v in (i.get("ProviderIds") or {}).items()}
        if any(have.get(k) == v for k, v in want.items()):
            return i
    for i in items:
        if (i.get("Name") or "").lower() == title.lower() and (not year or not i.get("ProductionYear") or abs(i["ProductionYear"] - year) <= 1):
            return i
    return None


async def find_episode(client, uid: str, series_id: str, season: int, number: int) -> str | None:
    try:
        data = await client.get(f"/Shows/{series_id}/Episodes", params={"userId": uid, "season": str(season)}) or {}
    except UpstreamError:
        return None
    return next((e.get("Id") for e in data.get("Items") or [] if e.get("IndexNumber") == number
                 and (e.get("ParentIndexNumber") in (None, season))), None)


# ----------------------------------------------------------------------------- people
async def person(client, uid: str, person_id: str) -> dict:
    """An actor's details and everything they're in that this Jellyfin user can see."""
    p = await _get(client, f"/Items/{person_id}", uid) or {}
    if p.get("Type") != "Person":
        raise UpstreamError("404 not a person")
    base = {"Recursive": "true", "PersonIds": person_id, "Fields": "ProductionYear,PremiereDate,ChildCount",
            "SortBy": "PremiereDate,ProductionYear,SortName", "SortOrder": "Descending", "IsMissing": "false"}

    async def kind(t: str, limit: int):
        try:
            data = await client.get("/Items", params={**base, "userId": uid, "IncludeItemTypes": t, "Limit": str(limit)}) or {}
        except UpstreamError:
            return []
        return data.get("Items") or []

    movies, shows, eps = await asyncio.gather(kind("Movie", 200), kind("Series", 200), kind("Episode", 60))
    show_ids = {s.get("Id") for s in shows}
    born, died = _when(p.get("PremiereDate")), _when(p.get("EndDate"))
    age = None
    if born:
        end = died or datetime.now(timezone.utc)
        age = end.year - born.year - ((end.month, end.day) < (born.month, born.day))
    return {
        "id": p.get("Id"), "name": p.get("Name"), "overview": p.get("Overview"),
        "has_img": bool((p.get("ImageTags") or {}).get("Primary")),
        "born": _fmt_date(p.get("PremiereDate")), "died": _fmt_date(p.get("EndDate")), "age": age,
        "place": ", ".join(p.get("ProductionLocations") or []) or None,
        "imdb": (p.get("ProviderIds") or {}).get("Imdb"), "tmdb": (p.get("ProviderIds") or {}).get("Tmdb"),
        "movies": [{"id": m.get("Id"), "name": m.get("Name"), "year": m.get("ProductionYear"),
                    "played": (m.get("UserData") or {}).get("Played")} for m in movies],
        "shows": [{"id": s.get("Id"), "name": s.get("Name"), "year": s.get("ProductionYear"),
                   "played": (s.get("UserData") or {}).get("Played")} for s in shows],
        # guest spots on shows they aren't main cast of
        "guest": [{"id": e.get("Id"), "series": e.get("SeriesName"), "series_id": e.get("SeriesId"), "name": e.get("Name"),
                   "code": ep_code(e)} for e in eps if e.get("SeriesId") not in show_ids][:40],
    }
