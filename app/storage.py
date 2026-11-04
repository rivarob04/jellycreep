"""Storage overview: drives seen by Jellyfin (10.11+) and the *arrs, merged per physical drive."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from .db import DiskSnapshot, Service, SessionLocal
from .services import client_for
from .upstream import UpstreamError

SERVER_FOLDERS = {
    "TranscodingTempFolder": "Transcodes", "CacheFolder": "Cache", "ImageCacheFolder": "Image cache",
    "InternalMetadataFolder": "Metadata", "ProgramDataFolder": "Jellyfin data", "LogFolder": "Logs",
}


def _drive(total: float, free: float, device: str | None = None) -> dict[str, Any]:
    return {"total": total, "free": free, "used": max(total - free, 0), "device": device,
            "libraries": [], "paths": [], "roles": [], "sources": set()}


def _same(a: dict, total: float, free: float, device: str | None) -> bool:
    if device and a["device"] and device == a["device"]:
        return True
    # Containers see the same disk under different paths: match on size instead.
    if not a["total"] or not total:
        return False
    return abs(a["total"] - total) / a["total"] < 0.002 and abs(a["free"] - free) <= max(a["total"] * 0.002, 2 * 1024 ** 3)


def _add(drives: list[dict], total: float, free: float, device: str | None, source: str,
         path: str | None = None, library: str | None = None, role: str | None = None) -> None:
    if not total or total <= 0:
        return
    d = next((x for x in drives if _same(x, total, free, device)), None)
    if d is None:
        d = _drive(total, free, device)
        drives.append(d)
    d["sources"].add(source)
    if path and path not in d["paths"]:
        d["paths"].append(path)
    if library and library not in d["libraries"]:
        d["libraries"].append(library)
    if role and role not in d["roles"]:
        d["roles"].append(role)


async def collect() -> tuple[list[dict], list[str]]:
    drives: list[dict] = []
    notes: list[str] = []
    with SessionLocal() as db:
        svcs = list(db.scalars(select(Service).where(Service.enabled.is_(True),
                                                      Service.kind.in_(("jellyfin", "sonarr", "radarr", "lidarr")))))

    async def one(svc: Service):
        client = await client_for(svc)
        try:
            if svc.kind == "jellyfin":
                data = await client.storage()
                for lib in data.get("Libraries") or []:
                    for f in lib.get("Folders") or []:
                        free, used = float(f.get("FreeSpace") or 0), float(f.get("UsedSpace") or 0)
                        _add(drives, free + used, free, f.get("DeviceId"), svc.name, f.get("Path"), lib.get("Name"))
                for field, role in SERVER_FOLDERS.items():
                    f = data.get(field) or {}
                    free, used = float(f.get("FreeSpace") or 0), float(f.get("UsedSpace") or 0)
                    _add(drives, free + used, free, f.get("DeviceId"), svc.name, f.get("Path"), role=role)
            else:
                for d in await client.diskspace():
                    _add(drives, float(d.get("totalSpace") or 0), float(d.get("freeSpace") or 0), None, svc.name,
                         d.get("path"), role=None)
        except UpstreamError as e:
            if svc.kind == "jellyfin" and "404" in str(e):
                notes.append(f"{svc.name}: per-library storage needs Jellyfin 10.11 or newer — showing *arr drives only.")
            else:
                notes.append(f"{svc.name}: {e}")

    await asyncio.gather(*(one(s) for s in svcs))
    for d in drives:
        d["key"] = hashlib.sha1(f"{round(d['total'] / 1024 ** 3)}|{d['device'] or ''}".encode()).hexdigest()[:16]
        d["pct"] = round(d["used"] / d["total"] * 100, 1) if d["total"] else 0
        d["sources"] = sorted(d["sources"])
        d["name"] = (", ".join(d["libraries"][:3]) + ("…" if len(d["libraries"]) > 3 else "")) if d["libraries"] \
            else (d["roles"][0] if d["roles"] else (d["paths"][0] if d["paths"] else "Drive"))
    drives.sort(key=lambda d: (not d["libraries"], -d["total"]))
    return drives, notes


def record(drives: list[dict]) -> None:
    today = date.today().isoformat()
    with SessionLocal() as db:
        for d in drives:
            db.merge(DiskSnapshot(key=d["key"], day=today, total=d["total"], free=d["free"]))
        db.commit()


def forecast(drives: list[dict]) -> None:
    """Attach growth per day and an estimated 'full in N days' using up to 30 days of snapshots."""
    since = (date.today() - timedelta(days=30)).isoformat()
    with SessionLocal() as db:
        for d in drives:
            rows = list(db.scalars(select(DiskSnapshot).where(DiskSnapshot.key == d["key"], DiskSnapshot.day >= since)
                                   .order_by(DiskSnapshot.day)))
            d["per_day"], d["days_left"] = None, None
            if len(rows) < 2:
                continue
            first, last = rows[0], rows[-1]
            span = (date.fromisoformat(last.day) - date.fromisoformat(first.day)).days
            if span <= 0:
                continue
            per_day = (first.free - d["free"]) / span   # bytes consumed per day
            d["per_day"] = per_day
            if per_day > 50 * 1024 ** 2:   # ignore noise under ~50 MB/day
                d["days_left"] = int(d["free"] / per_day)


def summary(drives: list[dict]) -> dict:
    total = sum(d["total"] for d in drives)
    free = sum(d["free"] for d in drives)
    lib = [d for d in drives if d["libraries"]]
    soonest = min((d["days_left"] for d in drives if d.get("days_left") is not None), default=None)
    return {"total": total, "free": free, "used": total - free, "pct": round((total - free) / total * 100, 1) if total else 0,
            "drives": len(drives), "media_drives": len(lib), "soonest": soonest}


async def snapshot_now() -> None:
    drives, _ = await collect()
    if drives:
        await asyncio.to_thread(record, drives)


# --------------------------------------------------------------------------- hiding drives
SYSTEM_PATHS = {"c:", "/", "/boot", "/etc", "/var", "/usr", "/opt", "/config", "/app"}


def drive_key(d: dict) -> str:
    return str(d.get("device") or (d["paths"][0] if d.get("paths") else f"size:{int(d['total'] // 1e9)}"))


def is_system(d: dict) -> bool:
    """The OS / app drive (C:\\ or /): no library on it, just Jellyfin's or the *arrs' own files."""
    if d.get("libraries"):
        return False
    paths = [str(p).replace("\\", "/").rstrip("/").lower() or "/" for p in d.get("paths") or []]
    return bool(paths) and any(p in SYSTEM_PATHS or p.startswith("c:") for p in paths)


def split_hidden(drives: list[dict], hidden: set[str], shown: set[str]) -> tuple[list[dict], list[dict]]:
    keep, hide = [], []
    for d in drives:
        k = drive_key(d)
        d["key"] = k
        d["auto_hidden"] = is_system(d) and k not in shown
        (hide if (k in hidden or d["auto_hidden"]) else keep).append(d)
    return keep, hide
