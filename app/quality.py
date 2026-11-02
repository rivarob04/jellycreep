"""Library quality report: resolution, codecs, HDR, audio, subtitles — and what that means for transcoding.

The scan pages through every movie/episode with its MediaSources (a few MB per
thousand items), so it runs in the background, is cached, and refreshes daily.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import func, select

from .db import Play, Service, SessionLocal, get_setting, set_setting, utcnow
from .services import client_for

log = logging.getLogger("jellycreep.quality")
STATUS: dict = {"running": False, "message": "", "done": 0, "total": 0}

LOSSLESS_AUDIO = {"truehd", "dts", "flac", "pcm_s16le", "pcm_s24le", "pcm_s32le", "mlp"}
EASY_CONTAINERS = {"mkv", "mp4", "m4v", "webm", "mov"}


def _res(w: int, h: int) -> str:
    if w >= 3200 or h >= 1700:
        return "4K"
    if w >= 1800 or h >= 1000:
        return "1080p"
    if w >= 1200 or h >= 700:
        return "720p"
    return "SD" if (w or h) else "Unknown"


def _hdr(v: dict) -> str:
    t = (v.get("VideoRangeType") or "").upper()
    if t.startswith("DOVI"):
        return "Dolby Vision"
    if t in ("HDR10PLUS", "HDR10+"):
        return "HDR10+"
    if t == "HDR10":
        return "HDR10"
    if t == "HLG":
        return "HLG"
    if (v.get("VideoRange") or "").upper() == "HDR":
        return "HDR"
    return "SDR"


def _codec(c: str | None) -> str:
    c = (c or "unknown").lower()
    return {"h264": "H.264", "avc": "H.264", "hevc": "HEVC", "h265": "HEVC", "av1": "AV1", "vp9": "VP9",
            "mpeg2video": "MPEG-2", "vc1": "VC-1", "mpeg4": "MPEG-4"}.get(c, c.upper())


def _acodec(c: str | None) -> str:
    c = (c or "unknown").lower()
    return {"aac": "AAC", "ac3": "AC3", "eac3": "EAC3", "truehd": "TrueHD", "dts": "DTS", "flac": "FLAC",
            "opus": "Opus", "mp3": "MP3"}.get(c, c.upper())


def _new() -> dict:
    return {"count": 0, "bytes": 0}


async def scan(svc_id: int) -> None:
    if STATUS["running"]:
        return
    STATUS.update(running=True, message="Starting…", done=0, total=0)
    try:
        with SessionLocal() as db:
            svc = db.get(Service, svc_id)
        client = await client_for(svc)
        agg = {k: defaultdict(_new) for k in ("res", "codec", "hdr", "audio", "container", "channels")}
        by_type = {"Movie": defaultdict(_new), "Episode": defaultdict(_new)}
        risk = defaultdict(_new)
        heavy: list[dict] = []
        total_items = total_bytes = 0
        start, limit = 0, 200
        while True:
            data = await client.media_page(start, limit)
            items = data.get("Items") or []
            STATUS["total"] = int(data.get("TotalRecordCount") or 0)
            for it in items:
                src = (it.get("MediaSources") or [{}])[0]
                streams = src.get("MediaStreams") or []
                v = next((s for s in streams if s.get("Type") == "Video"), None)
                if not v:
                    continue
                auds = [s for s in streams if s.get("Type") == "Audio"]
                a = next((s for s in auds if s.get("IsDefault")), auds[0] if auds else {})
                subs = [s for s in streams if s.get("Type") == "Subtitle"]
                size = int(src.get("Size") or 0)
                bitrate = int(src.get("Bitrate") or v.get("BitRate") or 0)
                res, codec, hdr = _res(int(v.get("Width") or 0), int(v.get("Height") or 0)), _codec(v.get("Codec")), _hdr(v)
                acodec = _acodec(a.get("Codec"))
                container = (src.get("Container") or it.get("Container") or "unknown").split(",")[0].lower()
                ch = int(a.get("Channels") or 0)
                chl = "7.1" if ch >= 8 else ("5.1" if ch >= 6 else ("Stereo" if ch == 2 else ("Mono" if ch == 1 else "Other")))
                for key, val in (("res", res), ("codec", codec), ("hdr", hdr), ("audio", acodec),
                                 ("container", container), ("channels", chl)):
                    agg[key][val]["count"] += 1
                    agg[key][val]["bytes"] += size
                bt = by_type.get(it.get("Type"))
                if bt is not None:
                    bt[res]["count"] += 1
                    bt[res]["bytes"] += size
                flags = {
                    "codec": codec not in ("H.264",),
                    "hevc_av1": codec in ("HEVC", "AV1"),
                    "hdr": hdr != "SDR",
                    "dovi": hdr == "Dolby Vision",
                    "image_subs": any(s.get("IsTextSubtitleStream") is False for s in subs),
                    "lossless_audio": (a.get("Codec") or "").lower() in LOSSLESS_AUDIO,
                    "container": container not in EASY_CONTAINERS,
                    "bitrate_40": bitrate >= 40_000_000,
                    "bitrate_20": bitrate >= 20_000_000,
                    "ten_bit": int(v.get("BitDepth") or 8) >= 10,
                }
                for k, on in flags.items():
                    if on:
                        risk[k]["count"] += 1
                        risk[k]["bytes"] += size
                total_items += 1
                total_bytes += size
                if bitrate:
                    name = it.get("Name") or "?"
                    if it.get("Type") == "Episode" and it.get("SeriesName"):
                        name = f"{it['SeriesName']} – {name}"
                    heavy.append({"name": name, "bitrate": bitrate, "res": res, "codec": codec, "hdr": hdr, "size": size})
            start += len(items)
            STATUS.update(done=start, message=f"Scanned {start} of {STATUS['total']}…")
            if not items or start >= STATUS["total"]:
                break
            await asyncio.sleep(0.15)   # be gentle with Jellyfin
        heavy.sort(key=lambda x: x["bitrate"], reverse=True)

        def rows(d: dict, order: list[str] | None = None) -> list[dict]:
            keys = order if order else sorted(d, key=lambda k: d[k]["count"], reverse=True)
            return [{"label": k, **d[k], "pct": round(d[k]["count"] / total_items * 100, 1) if total_items else 0}
                    for k in keys if k in d]

        result = {
            "server": svc.name, "server_id": svc.id, "scanned_at": utcnow().isoformat(), "items": total_items, "bytes": total_bytes,
            "res": rows(agg["res"], ["4K", "1080p", "720p", "SD", "Unknown"]),
            "codec": rows(agg["codec"]), "hdr": rows(agg["hdr"]), "audio": rows(agg["audio"]),
            "container": rows(agg["container"]), "channels": rows(agg["channels"]),
            "by_type": {t: rows(d, ["4K", "1080p", "720p", "SD", "Unknown"]) for t, d in by_type.items()},
            "risk": {k: {**v, "pct": round(v["count"] / total_items * 100, 1) if total_items else 0} for k, v in risk.items()},
            "heavy": heavy[:12],
        }
        set_setting(f"quality:{svc.id}", result)
        STATUS["message"] = f"Done — {total_items} items analysed."
    except Exception as e:  # noqa: BLE001
        log.exception("Quality scan failed")
        STATUS["message"] = f"Scan failed: {e}"
    finally:
        STATUS["running"] = False


def cached(svc_id: int) -> dict | None:
    return get_setting(f"quality:{svc_id}")


# ---- history side: why do streams actually transcode? ----------------------------------
REASON_LABELS = {
    "VideoCodecNotSupported": ("Video codec not supported", "codec"),
    "VideoProfileNotSupported": ("Video profile not supported", "codec"),
    "VideoLevelNotSupported": ("Video level not supported", "codec"),
    "VideoBitDepthNotSupported": ("10-bit video not supported", "ten_bit"),
    "VideoRangeTypeNotSupported": ("HDR / Dolby Vision not supported", "hdr"),
    "AudioCodecNotSupported": ("Audio codec not supported", "lossless_audio"),
    "AudioChannelsNotSupported": ("Too many audio channels", "lossless_audio"),
    "SubtitleCodecNotSupported": ("Subtitles burned in (image subs)", "image_subs"),
    "ContainerNotSupported": ("Container not supported", "container"),
    "ContainerBitrateExceedsLimit": ("Bitrate above the client's limit", "bitrate_20"),
    "VideoBitrateNotSupported": ("Video bitrate too high", "bitrate_20"),
    "AudioBitrateNotSupported": ("Audio bitrate too high", "lossless_audio"),
    "DirectPlayError": ("Direct play failed", None),
}


def history(days: int = 90) -> dict:
    since = utcnow() - timedelta(days=days)
    with SessionLocal() as db:
        rows = db.execute(select(Play.client, Play.play_method, Play.transcode_reasons)
                          .where(Play.started_at >= since)).all()
    reasons, clients = Counter(), defaultdict(lambda: {"plays": 0, "direct": 0, "reasons": Counter()})
    transcodes = 0
    for client, method, rs in rows:
        c = clients[client or "Unknown"]
        c["plays"] += 1
        if method == "Transcode":
            transcodes += 1
            for r in [x.strip() for x in (rs or "").split(",") if x.strip()]:
                reasons[r] += 1
                c["reasons"][r] += 1
        else:
            c["direct"] += 1
    out_clients = []
    for name, c in clients.items():
        top = c["reasons"].most_common(1)
        out_clients.append({"client": name, "plays": c["plays"], "direct_pct": round(c["direct"] / c["plays"] * 100) if c["plays"] else 0,
                            "top_reason": REASON_LABELS.get(top[0][0], (top[0][0], None))[0] if top else None})
    out_clients.sort(key=lambda x: x["plays"], reverse=True)
    return {
        "plays": len(rows), "transcodes": transcodes,
        "transcode_pct": round(transcodes / len(rows) * 100) if rows else 0,
        "reasons": [{"key": k, "label": REASON_LABELS.get(k, (k, None))[0], "risk": REASON_LABELS.get(k, (k, None))[1], "count": n}
                    for k, n in reasons.most_common(8)],
        "clients": out_clients[:10],
    }
