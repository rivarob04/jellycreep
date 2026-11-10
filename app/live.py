"""Instant activity: know the moment something starts, pauses or stops, and tell open pages right away.

Two sources, either is enough:
  * Jellyfin's own WebSocket (the one its dashboard uses). No plugin needed: JellyCreep subscribes to
    session updates and notices when a stream starts, stops, pauses or changes item.
  * The tracker's regular poll (and Jellyfin webhooks), as a fallback when the socket can't connect.

Pages listen on a Server-Sent Events stream and re-fetch their live section when told something changed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit, urlencode

log = logging.getLogger("jellycreep.live")

VERSION = 0
_event: asyncio.Event | None = None
_listeners: list[Callable[[str], Awaitable[None] | None]] = []
_sigs: dict[int, frozenset] = {}
_status: dict[int, str] = {}          # server id -> "socket" | "polling"


def _evt() -> asyncio.Event:
    global _event
    if _event is None:
        _event = asyncio.Event()
    return _event


def on_change(fn: Callable[[str], Awaitable[None] | None]) -> None:
    """Run fn(source) (e.g. clear the activity cache, record the play now) whenever playback changes."""
    _listeners.append(fn)


def bump(reason: str = "") -> None:
    """Something changed: wake every open page."""
    global VERSION, _event
    VERSION += 1
    old = _evt()
    _event = asyncio.Event()
    old.set()
    log.debug("Live change %s (%s)", VERSION, reason)


async def wait(since: int, timeout: float) -> int:
    """Wait until the version moves past `since` (or the timeout passes). Returns the current version."""
    if VERSION != since:
        return VERSION
    ev = _evt()
    try:
        await asyncio.wait_for(ev.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    return VERSION


def signature(sessions: list[dict]) -> frozenset:
    """What matters for 'did something start, stop, pause or switch item' (not the position ticking)."""
    out = set()
    for s in sessions or []:
        item = s.get("NowPlayingItem") or {}
        if not item:
            continue
        ps = s.get("PlayState") or {}
        out.add((s.get("Id"), item.get("Id"), bool(ps.get("IsPaused")), ps.get("PlayMethod")))
    return frozenset(out)


async def sessions_seen(server_id: int, sessions: list[dict], source: str) -> bool:
    """Compare with what we saw last; announce a change. Returns True if something changed."""
    sig = signature(sessions)
    if _sigs.get(server_id) == sig:
        return False
    first = server_id not in _sigs
    _sigs[server_id] = sig
    if first:
        return False
    await changed(source)
    return True


async def changed(source: str) -> None:
    bump(source)
    for fn in list(_listeners):
        try:
            r = fn(source)
            if asyncio.iscoroutine(r):
                asyncio.create_task(r)
        except Exception:  # noqa: BLE001
            log.exception("Live listener failed")


def status() -> dict[int, str]:
    return dict(_status)


# --------------------------------------------------------------------------- Jellyfin WebSocket
async def watch_jellyfin(svc, client) -> None:
    """Stay subscribed to one Jellyfin server's session updates, reconnecting with backoff."""
    try:
        import websockets
    except ImportError:  # pragma: no cover
        log.warning("websockets isn't installed: falling back to polling for live activity")
        return
    delay = 2
    while True:
        try:
            base = urlsplit(svc.url.rstrip("/"))
            scheme = "wss" if base.scheme == "https" else "ws"
            q = urlencode({"api_key": (svc.api_key or "").strip(), "deviceId": f"jellycreep-live-{svc.id}"})
            url = urlunsplit((scheme, base.netloc, base.path + "/socket", q, ""))
            headers = {**client.app_headers, **(await client._proxy_headers())}
            ctx = None
            if scheme == "wss" and not svc.verify_tls:
                ctx = ssl.create_default_context()
                ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            async with websockets.connect(url, additional_headers=headers, ssl=ctx, open_timeout=15,
                                          ping_interval=30, ping_timeout=20, max_size=8 * 1024 * 1024,
                                          user_agent_header="JellyCreep") as ws:
                await ws.send(json.dumps({"MessageType": "SessionsStart", "Data": "0,1500"}))
                if _status.get(svc.id) != "socket":
                    log.info("Live activity: connected to %s's WebSocket", svc.name)
                _status[svc.id] = "socket"
                delay = 2
                keepalive = None
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    kind = msg.get("MessageType")
                    if kind == "Sessions":
                        await sessions_seen(svc.id, msg.get("Data") or [], "jellyfin socket")
                    elif kind == "ForceKeepAlive" and keepalive is None:
                        every = max(10, int(msg.get("Data") or 60) // 2)

                        async def keep(ws=ws, every=every):
                            try:
                                while True:
                                    await asyncio.sleep(every)
                                    await ws.send(json.dumps({"MessageType": "KeepAlive"}))
                            except Exception:  # noqa: BLE001 – the socket closed: the reconnect loop takes over
                                pass
                        keepalive = asyncio.create_task(keep())
                    elif kind in ("PlaybackStart", "PlaybackStopped", "Play", "Playstate"):
                        await changed(f"jellyfin {kind}")
                if keepalive:
                    keepalive.cancel()
                raise ConnectionError("Jellyfin closed the live connection")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 – keep trying, polling covers us meanwhile
            if _status.get(svc.id) != "polling":
                log.warning("Live activity: can't use %s's WebSocket (%s). Using polling until it's back.", svc.name, e)
            _status[svc.id] = "polling"
        await asyncio.sleep(delay)
        delay = min(delay * 2, 120)
