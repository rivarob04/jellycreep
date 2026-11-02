"""Polls every Jellyfin server and turns live sessions into watch history.

Works like Tautulli's activity monitor: a stream is tracked while it appears in
/Sessions, play time is wall-clock time minus paused time, and when the stream
disappears (or the item changes) it is written to the history if it was watched
for at least MIN_WATCH_SECONDS. In-progress streams are stored in the database,
so a restart doesn't lose them.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

from sqlalchemy import delete, select

from .config import settings
from .db import ActiveStream, Event, Play, Service, SessionLocal, utcnow
from .services import client_for
from .upstream import UpstreamError

log = logging.getLogger("jellycreep.tracker")

TICKS = 10_000_000


def _resolution(height: int | None) -> str | None:
    if not height:
        return None
    for h, label in ((2000, "4K"), (1300, "1440p"), (1000, "1080p"), (700, "720p"), (560, "576p"), (470, "480p")):
        if height >= h:
            return label
    return "SD"


def snapshot(session: dict) -> dict:
    """Pull the fields we keep from a Jellyfin /Sessions entry."""
    item = session.get("NowPlayingItem") or {}
    ps = session.get("PlayState") or {}
    ti = session.get("TranscodingInfo") or {}
    streams = item.get("MediaStreams") or []
    video = next((s for s in streams if s.get("Type") == "Video"), {})
    audio = next((s for s in streams if s.get("Type") == "Audio"), {})

    method = ps.get("PlayMethod")
    if ti and not (ti.get("IsVideoDirect", True) and ti.get("IsAudioDirect", True)):
        method = "Transcode"

    runtime = item.get("RunTimeTicks")
    position = ps.get("PositionTicks")
    item_type = item.get("Type") or "Unknown"
    return {
        "user_id": session.get("UserId"),
        "user_name": session.get("UserName") or "Unknown",
        "item_id": item.get("Id"),
        "poster_id": item.get("SeriesId") if item_type == "Episode" else (item.get("AlbumId") if item_type == "Audio" else item.get("Id")),
        "item_type": item_type,
        "title": item.get("Name") or "Unknown",
        "series_name": item.get("SeriesName") if item_type == "Episode" else (item.get("AlbumArtist") if item_type == "Audio" else None),
        "season": item.get("ParentIndexNumber") if item_type == "Episode" else None,
        "episode": item.get("IndexNumber") if item_type == "Episode" else None,
        "year": item.get("ProductionYear"),
        "runtime_seconds": runtime / TICKS if runtime else None,
        "position_seconds": position / TICKS if position is not None else None,
        "client": session.get("Client"),
        "device": session.get("DeviceName"),
        "ip": session.get("RemoteEndPoint"),
        "play_method": method,
        "video_codec": (ti.get("VideoCodec") if method == "Transcode" else None) or video.get("Codec"),
        "audio_codec": (ti.get("AudioCodec") if method == "Transcode" else None) or audio.get("Codec"),
        "source_video_codec": video.get("Codec"),
        "source_audio_codec": audio.get("Codec"),
        "resolution": _resolution(video.get("Height")),
        "transcode_resolution": _resolution(ti.get("Height")) if ti else None,
        "container": ti.get("Container") or item.get("Container"),
        "bitrate": ti.get("Bitrate") or video.get("BitRate"),
        "transcode_reasons": ", ".join(ti.get("TranscodeReasons") or []) or None,
        "transcode_progress": ti.get("CompletionPercentage"),
        "is_paused": bool(ps.get("IsPaused")),
    }


_PLAY_FIELDS = {c.name for c in Play.__table__.columns} - {"id"}


def _finalize(db, stream: ActiveStream, stopped_at) -> Play | None:
    data = json.loads(stream.data)
    db.delete(stream)
    if stream.watched_seconds < settings.min_watch_seconds:
        return None
    runtime = data.get("runtime_seconds")
    position = data.get("position_seconds")
    percent = round(min(100.0, position / runtime * 100), 1) if runtime and position is not None else None
    play = Play(**{k: v for k, v in data.items() if k in _PLAY_FIELDS})
    play.server_id = stream.server_id
    play.session_id = stream.session_id
    play.started_at = stream.started_at
    play.stopped_at = stopped_at
    play.watched_seconds = round(stream.watched_seconds)
    play.paused_seconds = round(stream.paused_seconds)
    play.percent = percent
    play.completed = bool(percent is not None and percent >= settings.watched_percent)
    db.add(play)
    return play


def process_sessions(server_id: int, sessions: list[dict]) -> None:
    """Apply one poll's worth of sessions for one server (pure DB logic, testable)."""
    now = utcnow()
    max_gap = settings.poll_interval * 3
    with SessionLocal() as db:
        existing = {s.key: s for s in db.scalars(select(ActiveStream).where(ActiveStream.server_id == server_id))}
        seen = set()
        for sess in sessions:
            if not sess.get("NowPlayingItem"):
                continue
            snap = snapshot(sess)
            key = f"{server_id}:{sess.get('Id')}:{snap['item_id']}"
            seen.add(key)
            stream = existing.get(key)
            if stream is None:
                stream = ActiveStream(key=key, server_id=server_id, session_id=sess.get("Id") or "",
                                      started_at=now, last_seen=now, watched_seconds=0, paused_seconds=0)
                db.add(stream)
            else:
                gap = min((now - stream.last_seen).total_seconds(), max_gap)
                # Count the interval in the state the stream was in during it.
                if stream.is_paused:
                    stream.paused_seconds += gap
                else:
                    stream.watched_seconds += gap
                stream.last_seen = now
            stream.is_paused = snap["is_paused"]
            stream.data = json.dumps(snap)

        for key, stream in existing.items():
            if key not in seen:
                _finalize(db, stream, now)
        db.commit()


def finalize_stale() -> None:
    """Close streams we stopped hearing about (server down, or JellyCreep was off)."""
    cutoff = utcnow() - timedelta(seconds=max(60, settings.poll_interval * 6))
    with SessionLocal() as db:
        for stream in db.scalars(select(ActiveStream).where(ActiveStream.last_seen < cutoff)):
            _finalize(db, stream, stream.last_seen)
        db.commit()


def prune() -> None:
    with SessionLocal() as db:
        if settings.history_retention_days > 0:
            db.execute(delete(Play).where(Play.started_at < utcnow() - timedelta(days=settings.history_retention_days)))
        if settings.event_retention_days > 0:
            db.execute(delete(Event).where(Event.created_at < utcnow() - timedelta(days=settings.event_retention_days)))
        db.commit()


async def poll_once() -> None:
    with SessionLocal() as db:
        servers = list(db.scalars(select(Service).where(Service.kind == "jellyfin", Service.enabled.is_(True))))
    for svc in servers:
        try:
            client = await client_for(svc)
            sessions = await client.sessions()
        except UpstreamError as e:
            log.warning("Polling %s failed: %s", svc.name, e)
            continue
        await asyncio.to_thread(process_sessions, svc.id, sessions)
    await asyncio.to_thread(finalize_stale)


async def run_forever() -> None:
    log.info("Activity tracker started (every %ss)", settings.poll_interval)
    ticks = 0
    while True:
        try:
            await poll_once()
            ticks += 1
            if ticks % 360 == 0:
                await asyncio.to_thread(prune)
        except Exception:  # never let the tracker die
            log.exception("Tracker loop error")
        await asyncio.sleep(settings.poll_interval)
