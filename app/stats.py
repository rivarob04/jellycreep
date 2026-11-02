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
