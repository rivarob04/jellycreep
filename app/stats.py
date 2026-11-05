"""History queries and statistics (all from JellyCreep's own database)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import case, func, select

from .db import Play, utcnow


def _since(days: int):
    return utcnow() - timedelta(days=days)


def summary(db, days: int) -> dict:
    q = select(func.count(Play.id), func.coalesce(func.sum(Play.watched_seconds), 0),
               func.count(func.distinct(Play.user_name))).where(Play.started_at >= _since(days))
    plays, seconds, users = db.execute(q).one()
    return {"plays": plays, "seconds": int(seconds), "users": users}


def top_users(db, days: int, limit: int = 10) -> list[dict]:
    q = (select(Play.user_name, func.count(Play.id), func.sum(Play.watched_seconds), func.max(Play.stopped_at))
         .where(Play.started_at >= _since(days)).group_by(Play.user_name)
         .order_by(func.sum(Play.watched_seconds).desc()).limit(limit))
    return [{"user": u, "plays": p, "seconds": int(s or 0), "last": last} for u, p, s, last in db.execute(q)]


def top_movies(db, days: int, limit: int = 10) -> list[dict]:
    q = (select(Play.title, Play.year, func.max(Play.poster_id), func.max(Play.server_id), func.count(Play.id),
                func.count(func.distinct(Play.user_name)), func.sum(Play.watched_seconds))
         .where(Play.started_at >= _since(days), Play.item_type == "Movie")
         .group_by(Play.title, Play.year).order_by(func.count(Play.id).desc()).limit(limit))
    return [{"title": t, "year": y, "poster": pid, "server": sid, "plays": p, "users": u, "seconds": int(s or 0)}
            for t, y, pid, sid, p, u, s in db.execute(q)]


def top_shows(db, days: int, limit: int = 10) -> list[dict]:
    q = (select(Play.series_name, func.max(Play.poster_id), func.max(Play.server_id), func.count(Play.id),
                func.count(func.distinct(Play.user_name)), func.sum(Play.watched_seconds))
         .where(Play.started_at >= _since(days), Play.item_type == "Episode")
         .group_by(Play.series_name).order_by(func.count(Play.id).desc()).limit(limit))
    return [{"title": t, "poster": pid, "server": sid, "plays": p, "users": u, "seconds": int(s or 0)}
            for t, pid, sid, p, u, s in db.execute(q)]


def grouped(db, column, days: int, limit: int = 10) -> list[dict]:
    q = (select(column, func.count(Play.id), func.sum(Play.watched_seconds))
         .where(Play.started_at >= _since(days)).group_by(column).order_by(func.count(Play.id).desc()).limit(limit))
    return [{"label": label or "Unknown", "plays": p, "seconds": int(s or 0)} for label, p, s in db.execute(q)]


def plays_per_day(db, days: int) -> list[dict]:
    day = func.date(Play.started_at)
    q = (select(day, func.count(Play.id),
                func.sum(case((Play.item_type == "Movie", 1), else_=0)),
                func.sum(case((Play.item_type == "Episode", 1), else_=0)))
         .where(Play.started_at >= _since(days)).group_by(day).order_by(day))
    rows = {d: (p, m, e) for d, p, m, e in db.execute(q)}
    out = []
    start = _since(days).date()
    for i in range(days + 1):
        d = (start + timedelta(days=i)).isoformat()
        p, m, e = rows.get(d, (0, 0, 0))
        out.append({"day": d, "plays": p, "movies": m or 0, "episodes": e or 0, "other": p - (m or 0) - (e or 0)})
    return out


def plays_by_hour(db, days: int) -> list[int]:
    hour = func.strftime("%H", Play.started_at)
    rows = dict(db.execute(select(hour, func.count(Play.id)).where(Play.started_at >= _since(days)).group_by(hour)).all())
    return [rows.get(f"{h:02d}", 0) for h in range(24)]


def users_table(db) -> list[dict]:
    q = (select(Play.user_name, func.count(Play.id), func.sum(Play.watched_seconds), func.min(Play.started_at),
                func.max(Play.stopped_at), func.max(Play.id))
         .group_by(Play.user_name).order_by(func.max(Play.stopped_at).desc()))
    out = []
    for name, plays, secs, first, last, last_id in db.execute(q):
        last_play = db.get(Play, last_id)
        out.append({"user": name, "plays": plays, "seconds": int(secs or 0), "first": first, "last": last,
                    "last_title": last_play.display_title if last_play else None,
                    "last_client": last_play.client if last_play else None,
                    "last_ip": last_play.ip if last_play else None})
    return out


def heatmap(db, days: int, tz) -> tuple[list[list[int]], list[int], int]:
    """Plays by weekday (Sun..Sat) x hour, in the display time zone."""
    from datetime import timezone as _tz
    grid = [[0] * 24 for _ in range(7)]
    rows = db.execute(select(Play.started_at).where(Play.started_at >= _since(days))).all()
    for (ts,) in rows:
        local = ts.replace(tzinfo=_tz.utc).astimezone(tz)
        grid[(local.weekday() + 1) % 7][local.hour] += 1
    by_hour = [sum(grid[d][h] for d in range(7)) for h in range(24)]
    peak = max((max(r) for r in grid), default=0)
    return grid, by_hour, peak


def _local(ts, tz):
    from datetime import timezone as _tz
    return ts.replace(tzinfo=_tz.utc).astimezone(tz)


def user_profile(db, name: str, tz) -> dict | None:
    from collections import Counter, defaultdict
    from datetime import timedelta as _td
    plays = list(db.scalars(select(Play).where(Play.user_name == name).order_by(Play.started_at)))
    if not plays:
        return None
    today = _local(utcnow(), tz).date()
    days = sorted({_local(p.started_at, tz).date() for p in plays})
    # streaks
    longest, run, prev = 1, 1, None
    for d in days:
        if prev and (d - prev).days == 1:
            run += 1
            longest = max(longest, run)
        elif prev:
            run = 1
        prev = d
    current = 0
    if days and (today - days[-1]).days <= 1:
        current, d = 1, days[-1]
        s = set(days)
        while d - _td(days=1) in s:
            current += 1
            d -= _td(days=1)
    # biggest binge: most episodes of one series in one day
    binge = Counter()
    for p in plays:
        if p.item_type == "Episode" and p.series_name:
            binge[(_local(p.started_at, tz).date(), p.series_name)] += 1
    best_binge = None
    if binge:
        (bday, bseries), bcount = binge.most_common(1)[0]
        if bcount >= 2:
            best_binge = {"day": bday, "series": bseries, "count": bcount}
    shows, movies, devices = Counter(), Counter(), {}
    show_secs, show_poster, movie_poster, movie_secs = defaultdict(float), {}, {}, defaultdict(float)
    for p in plays:
        if p.item_type == "Episode" and p.series_name:
            shows[p.series_name] += 1
            show_secs[p.series_name] += p.watched_seconds or 0
            show_poster[p.series_name] = (p.server_id, p.poster_id)
        elif p.item_type == "Movie":
            key = f"{p.title} ({p.year})" if p.year else p.title
            movies[key] += 1
            movie_secs[key] += p.watched_seconds or 0
            movie_poster[key] = (p.server_id, p.poster_id)
        dk = (p.client or "?", p.device or "?")
        d = devices.setdefault(dk, {"client": dk[0], "device": dk[1], "plays": 0, "last": p.stopped_at, "ip": p.ip})
        d["plays"] += 1
        if p.stopped_at and p.stopped_at >= d["last"]:
            d["last"], d["ip"] = p.stopped_at, p.ip
    # last 30 days of watch time
    per_day = defaultdict(float)
    for p in plays:
        per_day[_local(p.started_at, tz).date()] += (p.watched_seconds or 0)
    last30 = [{"day": today - _td(days=i), "seconds": per_day.get(today - _td(days=i), 0)} for i in range(29, -1, -1)]
    grid = [[0] * 24 for _ in range(7)]
    for p in plays:
        lt = _local(p.started_at, tz)
        grid[(lt.weekday() + 1) % 7][lt.hour] += 1
    methods = Counter(p.play_method or "Unknown" for p in plays)
    total = sum(p.watched_seconds or 0 for p in plays)
    return {
        "name": name, "plays": len(plays), "seconds": int(total), "first": plays[0].started_at, "last": plays[-1].stopped_at,
        "active_days": len(days), "streak_current": current, "streak_longest": longest if days else 0,
        "binge": best_binge, "completed": sum(1 for p in plays if p.completed),
        "top_shows": [{"title": t, "plays": n, "seconds": int(show_secs[t]), "poster": show_poster[t]} for t, n in shows.most_common(8)],
        "top_movies": [{"title": t, "plays": n, "seconds": int(movie_secs[t]), "poster": movie_poster[t]} for t, n in movies.most_common(8)],
        "devices": sorted(devices.values(), key=lambda d: d["last"], reverse=True),
        "last30": last30, "heat": grid, "heat_peak": max(max(r) for r in grid),
        "methods": [{"label": k, "plays": v, "seconds": 0} for k, v in methods.most_common()],
        "recent": list(reversed(plays[-15:])),
    }


def concurrency(db, days: int, tz) -> dict:
    """Peak simultaneous streams (and transcodes) per local day, via a sweep over play intervals."""
    from collections import defaultdict
    from datetime import timedelta as _td
    rows = db.execute(select(Play.started_at, Play.stopped_at, Play.play_method)
                      .where(Play.stopped_at >= _since(days))).all()
    events = []
    for st, sp, method in rows:
        if not st or not sp or sp <= st:
            continue
        tc = 1 if method == "Transcode" else 0
        events.append((st, 1, tc))
        events.append((sp, -1, -tc))
    events.sort(key=lambda e: (e[0], e[1]))  # ends before starts at the same instant
    cur = cur_tc = 0
    peak_day, peak_tc_day = defaultdict(int), defaultdict(int)
    best = {"streams": 0, "at": None, "transcodes": 0}
    for ts, d, dtc in events:
        cur += d
        cur_tc += dtc
        day = _local(ts, tz).date()
        if d > 0:
            peak_day[day] = max(peak_day[day], cur)
            peak_tc_day[day] = max(peak_tc_day[day], cur_tc)
            if cur > best["streams"]:
                best = {"streams": cur, "at": _local(ts, tz), "transcodes": cur_tc}
    today = _local(utcnow(), tz).date()
    series = [{"day": today - _td(days=i), "streams": peak_day.get(today - _td(days=i), 0),
               "transcodes": peak_tc_day.get(today - _td(days=i), 0)} for i in range(min(days, 90) - 1, -1, -1)]
    return {"days": series, "best": best, "max": max((s["streams"] for s in series), default=0)}
