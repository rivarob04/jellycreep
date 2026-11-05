"""City / country / ISP for the IP addresses streams come from (admins only).

Public addresses are looked up once with ipwho.is (HTTPS, no key) and kept in the database;
private, LAN and VPN-range addresses are never sent anywhere. Set GEOIP=off to switch lookups off.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from datetime import timedelta

import httpx
from sqlalchemy import select

from .config import settings
from .db import GeoIP, SessionLocal, utcnow

log = logging.getLogger("jellycreep.geoip")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_LOCKS: dict[str, asyncio.Lock] = {}


def kind(ip: str | None) -> str | None:
    """'local' (LAN / loopback), 'cgnat' (carrier / Tailscale range), 'public' — or None if not an IP."""
    try:
        a = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return None
    if a.version == 4 and a in _CGNAT:
        return "cgnat"
    if a.is_private or a.is_loopback or a.is_link_local:
        return "local"
    return "public"


def flag(cc: str | None) -> str:
    cc = (cc or "").upper()
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc) if len(cc) == 2 and cc.isalpha() else ""


def describe(ip: str | None, g: GeoIP | dict | None) -> dict | None:
    k = kind(ip)
    if not k:
        return None
    if k == "local":
        return {"label": "Local network", "kind": k, "icon": "🏠"}
    if k == "cgnat":
        return {"label": "VPN / carrier network", "kind": k, "icon": "🛡️"}
    if not g or not getattr(g, "ok", False):
        return {"label": "Location unknown", "kind": k, "icon": "🌐"}
    place = ", ".join(x for x in (g.city, g.region, g.country) if x)
    return {"label": place or "Unknown place", "kind": k, "icon": flag(g.country_code) or "🌐", "isp": g.isp,
            "city": g.city, "region": g.region, "country": g.country, "lat": g.lat, "lon": g.lon,
            "map": f"https://www.openstreetmap.org/?mlat={g.lat}&mlon={g.lon}#map=10/{g.lat}/{g.lon}" if g.lat is not None else None}


async def _fetch(ip: str) -> GeoIP:
    row = GeoIP(ip=ip, ok=False, fetched_at=utcnow())
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(f"https://ipwho.is/{ip}", params={"fields": "success,city,region,country,country_code,latitude,longitude,connection"})
            d = r.json() if r.status_code == 200 else {}
        if d.get("success"):
            conn = d.get("connection") or {}
            row.ok, row.city, row.region, row.country = True, d.get("city"), d.get("region"), d.get("country")
            row.country_code, row.lat, row.lon = d.get("country_code"), d.get("latitude"), d.get("longitude")
            row.isp = conn.get("isp") or conn.get("org")
    except Exception:  # noqa: BLE001
        log.debug("GeoIP lookup failed for %s", ip, exc_info=True)
    return row


_PENDING: set[str] = set()


async def lookup(ips: list[str | None], *, fetch_limit: int = 25, wait: bool = True) -> dict[str, dict | None]:
    """{ip: description} for every IP given; unknown public IPs are looked up (a few at a time).
    With ``wait=False`` the page gets what's already known straight away and the rest is looked up in the
    background (it shows up on the next visit)."""
    uniq = list(dict.fromkeys(i for i in ips if i))
    with SessionLocal() as db:
        have = {g.ip: g for g in db.scalars(select(GeoIP).where(GeoIP.ip.in_(uniq)))} if uniq else {}
    stale = utcnow() - timedelta(days=30)
    need = [i for i in uniq if kind(i) == "public" and (i not in have or (not have[i].ok and have[i].fetched_at < utcnow() - timedelta(days=1))
                                                       or have[i].fetched_at < stale)]
    if need and settings.geoip != "off" and not wait:
        todo = [i for i in need if i not in _PENDING][:fetch_limit]
        if todo:
            _PENDING.update(todo)

            async def bg():
                try:
                    await lookup(todo, fetch_limit=fetch_limit, wait=True)
                finally:
                    _PENDING.difference_update(todo)
            asyncio.get_running_loop().create_task(bg())
        need = []
    if need and settings.geoip != "off":
        sem = asyncio.Semaphore(4)

        async def one(ip):
            lock = _LOCKS.setdefault(ip, asyncio.Lock())
            async with lock, sem:
                return await _fetch(ip)
        got = await asyncio.gather(*(one(i) for i in need[:fetch_limit]))
        with SessionLocal() as db:
            for g in got:
                db.merge(g)
            db.commit()
        for g in got:
            have[g.ip] = g
    return {i: describe(i, have.get(i)) for i in uniq}
