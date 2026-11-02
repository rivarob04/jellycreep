"""One-off import of past watch history from Jellystat.

Jellystat's /api/getHistory groups plays per (item, user) and nests every
individual playback in `results`. We import each individual playback once
(keyed by its Jellystat Id), and by default only history from before
JellyCreep's own first recorded play, so nothing is counted twice.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .db import Play, Service, SessionLocal, utcnow
from .services import client_for
from .upstream import UpstreamError

log = logging.getLogger("jellycreep.import")

STATUS: dict = {"running": False, "message": "", "imported": 0, "skipped": 0, "pages": 0, "done": False}


def _ts(v) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _method(row: dict) -> str | None:
    pm = row.get("PlayMethod")
    if pm:
        return "Transcode" if str(pm).lower().startswith("transcode") else pm
    ti = row.get("TranscodingInfo") or {}
    if isinstance(ti, dict) and ti and not (ti.get("IsVideoDirect", True) and ti.get("IsAudioDirect", True)):
        return "Transcode"
    return None


def _to_play(row: dict, server_id: int) -> Play | None:
    stopped = _ts(row.get("ActivityDateInserted"))
    dur = float(row.get("PlaybackDuration") or 0)
    if not stopped or dur <= 0:
        return None
    is_ep = bool(row.get("SeriesName"))
    ti = row.get("TranscodingInfo") if isinstance(row.get("TranscodingInfo"), dict) else {}
    def _int(v):
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    return Play(
        server_id=server_id, session_id=f"jellystat:{row.get('Id')}", user_id=row.get("UserId"),
        user_name=row.get("UserName") or "Unknown", item_id=row.get("EpisodeId") or row.get("NowPlayingItemId"),
        poster_id=row.get("NowPlayingItemId"), item_type="Episode" if is_ep else "Movie",
        title=row.get("NowPlayingItemName") or "Unknown", series_name=row.get("SeriesName") if is_ep else None,
        season=_int(row.get("SeasonNumber")) if is_ep else None, episode=_int(row.get("EpisodeNumber")) if is_ep else None,
        started_at=stopped - timedelta(seconds=dur), stopped_at=stopped, watched_seconds=round(dur), paused_seconds=0,
        client=row.get("Client"), device=row.get("DeviceName"), ip=row.get("RemoteEndPoint"),
        play_method=_method(row), video_codec=(ti or {}).get("VideoCodec"), audio_codec=(ti or {}).get("AudioCodec"),
        transcode_reasons=", ".join((ti or {}).get("TranscodeReasons") or []) or None,
    )


async def run(jellystat_id: int, only_before_jellycreep: bool = True) -> None:
    STATUS.update(running=True, message="Starting…", imported=0, skipped=0, pages=0, done=False)
    try:
        with SessionLocal() as db:
            svc = db.get(Service, jellystat_id)
            jf = db.scalar(select(Service).where(Service.kind == "jellyfin").order_by(Service.id))
            cutoff = db.scalar(select(func.min(Play.started_at)).where(
                (Play.session_id.is_(None)) | (~Play.session_id.like("jellystat:%")))) if only_before_jellycreep else None
            existing = {sid for (sid,) in db.execute(select(Play.session_id).where(Play.session_id.like("jellystat:%")))}
        if not svc or svc.kind != "jellystat":
            raise UpstreamError("Jellystat service not found")
        server_id = jf.id if jf else 0
        client = await client_for(svc)
        page, pages = 1, 1
        while page <= pages:
            data = await client.history_page(page)
            pages = int(data.get("pages") or 1)
            batch = []
            for group in data.get("results") or []:
                for row in group.get("results") or [group]:
                    key = f"jellystat:{row.get('Id')}"
                    if key in existing:
                        STATUS["skipped"] += 1
                        continue
                    play = _to_play(row, server_id)
                    if not play or (cutoff and play.started_at >= cutoff):
                        STATUS["skipped"] += 1
                        continue
                    existing.add(key)
                    batch.append(play)
            if batch:
                with SessionLocal() as db:
                    db.add_all(batch)
                    db.commit()
            STATUS["imported"] += len(batch)
            STATUS["pages"] = page
            STATUS["message"] = f"Page {page} of {pages}…"
            page += 1
            await asyncio.sleep(0)
        STATUS["message"] = f"Done — imported {STATUS['imported']} plays, skipped {STATUS['skipped']}."
        STATUS["done"] = True
    except Exception as e:  # noqa: BLE001
        log.exception("Jellystat import failed")
        STATUS["message"] = f"Import failed: {e}"
    finally:
        STATUS["running"] = False
        STATUS["finished_at"] = utcnow().isoformat()
