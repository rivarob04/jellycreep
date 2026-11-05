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
         .where(Play.started_at >= _since(days), Play.item_type == "Episode", Play.series_name.isnot(None))
         .group_by(Play.series_name).order_by(func.count(Play.id).desc()).limit(limit))
    return [{"title": t, "poster": pid, "server": sid, "plays": p, "users": u, "seconds": int(s or 0)}
            for t, pid, sid, p, u, s in db.execute(q)]


def grouped(db, column, days: int, limit: int = 10) -> list[dict]:
    q = (select(column, func.count(Play.id), func.sum(Play.watched_seconds))
         .where(Play.started_at >= _since(days)).group_by(column).order_by(func.count(Play.id).desc()).limit(limit))
    return [{"label": label or "Unknown", "plays": p, "seconds": int(s or 0)} for label, p, s in db.execute(q)]


def plays_per_day(db, days: int, tz=None) -> list[dict]:
    """Plays and watch time per local day, split into movies / episodes / other."""
    from collections import defaultdict
    from datetime import timedelta as _td
    rows = db.execute(select(Play.started_at, Play.item_type, Play.watched_seconds).where(Play.started_at >= _since(days))).all()
    agg = defaultdict(lambda: {"plays": 0, "movies": 0, "episodes": 0, "other": 0, "s_movies": 0.0, "s_episodes": 0.0, "s_other": 0.0})
    for st, t, secs in rows:
        d = _local(st, tz).date() if tz else st.date()
        k = "movies" if t == "Movie" else ("episodes" if t == "Episode" else "other")
        b = agg[d]
        b["plays"] += 1
        b[k] += 1
        b["s_" + k] += secs or 0
    today = (_local(utcnow(), tz) if tz else utcnow()).date()
    out = []
    for i in range(days - 1, -1, -1):
        d = today - _td(days=i)
        b = agg.get(d) or {"plays": 0, "movies": 0, "episodes": 0, "other": 0, "s_movies": 0.0, "s_episodes": 0.0, "s_other": 0.0}
        out.append({"day": d, **b, "seconds": b["s_movies"] + b["s_episodes"] + b["s_other"]})
    return out


def compare(db, days: int) -> dict:
    """This period vs the one before it, plus finish / transcode rates."""
    now = utcnow()
    cur_start, prev_start = now - timedelta(days=days), now - timedelta(days=2 * days)

    def agg(a, b):
        q = select(func.count(Play.id), func.coalesce(func.sum(Play.watched_seconds), 0),
                   func.coalesce(func.sum(case((Play.completed.is_(True), 1), else_=0)), 0),
                   func.coalesce(func.sum(case((Play.play_method == "Transcode", 1), else_=0)), 0),
                   func.count(func.distinct(Play.user_name))).where(Play.started_at >= a, Play.started_at < b)
        return db.execute(q).one()
    cp, cs, cdone, ctc, cu = agg(cur_start, now + timedelta(days=1))
    pp, ps, _, _, pu = agg(prev_start, cur_start)

    def pct(a, b):
        return None if not b else round((a - b) / b * 100)
    return {"plays_delta": pct(cp, pp), "secs_delta": pct(cs, ps), "users_delta": pct(cu, pu),
            "finish_rate": round(cdone / cp * 100) if cp else 0, "transcode_rate": round(ctc / cp * 100) if cp else 0,
            "avg_session": (cs / cp) if cp else 0}


def by_weekday(db, days: int, tz) -> list[dict]:
    rows = db.execute(select(Play.started_at, Play.watched_seconds).where(Play.started_at >= _since(days))).all()
    out = [{"label": d, "plays": 0, "seconds": 0.0} for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")]
    for st, secs in rows:
        o = out[_local(st, tz).weekday()]
        o["plays"] += 1
        o["seconds"] += secs or 0
    return out


def plays_by_hour(db, days: int) -> list[int]:
    hour = func.strftime("%H", Play.started_at)
    rows = dict(db.execute(select(hour, func.count(Play.id)).where(Play.started_at >= _since(days)).group_by(hour)).all())
    return [rows.get(f"{h:02d}", 0) for h in range(24)]


def users_table(db, tz=None) -> list[dict]:
    from collections import defaultdict
    from datetime import timedelta as _td
    q = (select(Play.user_name, func.count(Play.id), func.sum(Play.watched_seconds), func.min(Play.started_at),
                func.max(Play.stopped_at), func.max(Play.id))
         .group_by(Play.user_name).order_by(func.max(Play.stopped_at).desc()))
    # last 14 days of watch time per person (local days), for the little sparkline
    today = (_local(utcnow(), tz) if tz else utcnow()).date()
    spark = defaultdict(lambda: [0.0] * 14)
    recent30 = defaultdict(lambda: [0, 0.0])
    for name, st, secs in db.execute(select(Play.user_name, Play.started_at, Play.watched_seconds)
                                     .where(Play.started_at >= _since(31))):
        d = (_local(st, tz) if tz else st).date()
        age = (today - d).days
        if 0 <= age < 14:
            spark[name][13 - age] += secs or 0
        if age < 30:
            recent30[name][0] += 1
            recent30[name][1] += secs or 0
    out = []
    for name, plays, secs, first, last, last_id in db.execute(q):
        lp = db.get(Play, last_id)
        out.append({"user": name, "plays": plays, "seconds": int(secs or 0), "first": first, "last": last,
                    "last_title": lp.display_title if lp else None,
                    "last_client": lp.client if lp else None, "last_device": lp.device if lp else None,
                    "last_ip": lp.ip if lp else None, "last_id": lp.id if lp else None,
                    "last_name": (lp.series_name or lp.title) if lp else None,
                    "last_sub": ((f"S{lp.season} • E{lp.episode} · " if lp.season is not None else "") + lp.title) if lp and lp.item_type == "Episode"
                                else ((str(lp.year) if lp.year else "Movie") if lp else None),
                    "last_kind": ("tv" if lp.item_type == "Episode" else "movie") if lp else "movie",
                    "poster": (lp.server_id, lp.poster_id) if lp and lp.poster_id else None,
                    "spark": spark[name], "plays30": recent30[name][0], "secs30": int(recent30[name][1])})
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
    # last 30 days of watch time, split by kind
    per_day = defaultdict(lambda: {"episodes": 0.0, "movies": 0.0, "other": 0.0, "plays": 0, "titles": Counter()})
    for p in plays:
        b = per_day[_local(p.started_at, tz).date()]
        k = "episodes" if p.item_type == "Episode" else ("movies" if p.item_type == "Movie" else "other")
        b[k] += p.watched_seconds or 0
        b["plays"] += 1
        b["titles"][p.series_name or p.title] += 1
    last30 = []
    for i in range(29, -1, -1):
        dd = today - _td(days=i)
        b = per_day.get(dd)
        e, m, o = (b["episodes"], b["movies"], b["other"]) if b else (0, 0, 0)
        last30.append({"day": dd, "seconds": e + m + o, "episodes": e, "movies": m, "other": o,
                       "plays": b["plays"] if b else 0, "top": b["titles"].most_common(1)[0][0] if b else None})
    secs30 = [d["seconds"] for d in last30]
    best30 = max(last30, key=lambda d: d["seconds"])
    month = {"total": sum(secs30), "active": sum(1 for x in secs30 if x > 0), "avg": sum(secs30) / 30,
             "best": best30 if best30["seconds"] else None, "plays": sum(d["plays"] for d in last30)}
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
        "last30": last30, "month": month, "heat": grid, "heat_peak": max(max(r) for r in grid),
        "methods": [{"label": k, "plays": v, "seconds": 0} for k, v in methods.most_common()],
        "by_hour": [sum(grid[d][h] for d in range(7)) for h in range(24)],
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
