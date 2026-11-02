"""Builds the release calendar (Sonarr / Radarr / Lidarr) for one month."""
from __future__ import annotations

import calendar as pycal
from datetime import date, datetime, timedelta, timezone
from typing import Any

KIND_OF = {"sonarr": "tv", "radarr": "movie", "lidarr": "music"}


def month_bounds(ym: str | None, today: date) -> tuple[int, int]:
    try:
        y, m = (int(x) for x in (ym or "").split("-", 1))
        if 1 <= m <= 12 and 1970 < y < 2200:
            return y, m
    except ValueError:
        pass
    return today.year, today.month


def grid_range(year: int, month: int) -> tuple[date, date]:
    """Sunday-first weeks covering the whole month."""
    first = date(year, month, 1)
    start = first - timedelta(days=(first.weekday() + 1) % 7)
    last = date(year, month, pycal.monthrange(year, month)[1])
    end = last + timedelta(days=(5 - last.weekday()) % 7)
    return start, end


def _parse(ts: str | None, tz) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def items_for(svc, raw: list[dict], tz, start: date, end: date) -> list[dict[str, Any]]:
    kind = KIND_OF.get(svc.kind)
    out: list[dict[str, Any]] = []
    for c in raw or []:
        if svc.kind == "sonarr":
            when = _parse(c.get("airDateUtc"), tz)
            if not when:
                continue
            series = c.get("series") or {}
            se = f"S{int(c.get('seasonNumber') or 0):02d}E{int(c.get('episodeNumber') or 0):02d}"
            out.append({
                "day": when.date(), "time": when.strftime("%-I:%M %p"), "kind": kind,
                "title": series.get("title") or "Unknown series",
                "sub": f"{se} · {c.get('title') or 'TBA'}", "badge": se,
                "network": series.get("network"), "has_file": bool(c.get("hasFile")),
                "poster": f"/arrimg/{svc.id}/{c.get('seriesId') or series.get('id')}",
                "finale": c.get("finaleType"), "premiere": c.get("episodeNumber") == 1,
                "source": svc.name, "link": f"{svc.link}/series/{series.get('titleSlug', '')}",
            })
        elif svc.kind == "radarr":
            for field, label in (("inCinemas", "In cinemas"), ("digitalRelease", "Digital"), ("physicalRelease", "Physical")):
                raw_ts = c.get(field)
                if not raw_ts:
                    continue
                d = date.fromisoformat(raw_ts[:10])
                if not (start <= d <= end):
                    continue
                out.append({
                    "day": d, "time": None, "kind": kind,
                    "title": c.get("title") or "Unknown movie", "sub": f"{label} release" + (f" · {c['year']}" if c.get("year") else ""),
                    "badge": label, "network": c.get("studio"), "has_file": bool(c.get("hasFile")),
                    "poster": f"/arrimg/{svc.id}/{c.get('id')}", "finale": None, "premiere": False,
                    "source": svc.name, "link": f"{svc.link}/movie/{c.get('tmdbId', '')}",
                })
        elif svc.kind == "lidarr":
            raw_ts = c.get("releaseDate")
            if not raw_ts:
                continue
            d = date.fromisoformat(raw_ts[:10])
            artist = c.get("artist") or {}
            out.append({
                "day": d, "time": None, "kind": kind, "title": c.get("title") or "Unknown album",
                "sub": artist.get("artistName") or "", "badge": c.get("albumType") or "Album", "network": None,
                "has_file": bool((c.get("statistics") or {}).get("trackFileCount")), "poster": None,
                "finale": None, "premiere": False, "source": svc.name, "link": svc.link,
            })
    return out


def build(year: int, month: int, items: list[dict], today: date) -> dict:
    start, end = grid_range(year, month)
    by_day: dict[date, list[dict]] = {}
    for it in items:
        if start <= it["day"] <= end:
            by_day.setdefault(it["day"], []).append(it)
    for lst in by_day.values():
        lst.sort(key=lambda i: (i["time"] is None, i["time"] or "", i["title"]))
    weeks, d = [], start
    while d <= end:
        week = []
        for _ in range(7):
            its = by_day.get(d, [])
            week.append({"date": d, "in_month": d.month == month, "today": d == today, "past": d < today,
                         "entries": its, "kinds": sorted({i["kind"] for i in its})})
            d += timedelta(days=1)
        weeks.append(week)
    prev_m = (date(year, month, 1) - timedelta(days=1)).strftime("%Y-%m")
    next_m = (date(year, month, 28) + timedelta(days=7)).strftime("%Y-%m")
    agenda_days = sorted(k for k in by_day if k.month == month)
    month_items = [i for k in agenda_days for i in by_day[k]]
    return {
        "year": year, "month": month, "label": date(year, month, 1).strftime("%B %Y"),
        "weeks": weeks, "prev": prev_m, "next": next_m, "is_current": (year, month) == (today.year, today.month),
        "agenda": [(k, by_day[k]) for k in agenda_days], "today": today,
        "counts": {
            "tv": sum(1 for i in month_items if i["kind"] == "tv"),
            "movie": sum(1 for i in month_items if i["kind"] == "movie"),
            "music": sum(1 for i in month_items if i["kind"] == "music"),
            "have": sum(1 for i in month_items if i["has_file"]),
        },
    }
