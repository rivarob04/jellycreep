"""Browse the Jellyfin library, Tautulli Remote style: recently added, item details
(ratings, cast, file info), seasons and episodes.

Everything is fetched *as a Jellyfin user* (``userId=…``) so people who signed in with
Jellyfin only ever see libraries their own account can access. Staff browse as the
first Jellyfin administrator.
"""
from __future__ import annotations

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
        ("Starring", _people(d, "Actor", 8)),
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
