"""JellyCreep – Tautulli-style monitoring for Jellyfin, *arr-aware, built for Authentik."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlencode, urlsplit
import copy
import json
import logging
import math
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, or_, select
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, calendar_view, explain, geoip, importer, library, notify, quality, seerr_portal, webpush, stats, storage, tracker, webhooks
from .auth import (CSRF_COOKIE, LoginRequired, User, ViewerOnly, admin_user, api_user, bust_login_cache, client_ip,
                   csrf_token, current_user, jellyfin_login_enabled, limiter, login_settings, oauth, staff_user,
                   user_from_claims, verify_csrf)
from .config import settings
from .db import (PushSub, PROXY_AUTH_MODES, SERVICE_KINDS, ActiveStream, ApiToken, Event, Play, Service, SessionLocal,
                 get_setting, hash_token, init_db, set_setting, utcnow)
from .services import client_for
from .upstream import UpstreamError, pool

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jellycreep")
BASE = Path(__file__).parent
def _load_tz(name: str | None):
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:  # unknown name or no tz database — don't crash, just use UTC
        logging.getLogger("jellycreep").warning("Unknown TZ %r, falling back to UTC", name)
        return timezone.utc


TZ = _load_tz(settings.tz)


# --- startup ------------------------------------------------------------------------
def seed_services_from_env() -> None:
    """First-run convenience: <KIND>_URL / <KIND>_API_KEY etc. create services if missing."""
    with SessionLocal() as db:
        for kind in SERVICE_KINDS:
            url = os.environ.get(f"{kind.upper()}_URL")
            if not url or db.scalar(select(Service).where(Service.kind == kind)):
                continue
            svc = Service(kind=kind, name=SERVICE_KINDS[kind], url=url.rstrip("/"),
                          public_url=os.environ.get(f"{kind.upper()}_PUBLIC_URL"))
            svc.api_key = os.environ.get(f"{kind.upper()}_API_KEY")
            svc.username = os.environ.get(f"{kind.upper()}_USERNAME")
            svc.password = os.environ.get(f"{kind.upper()}_PASSWORD")
            mode = os.environ.get(f"{kind.upper()}_PROXY_AUTH") or os.environ.get("DEFAULT_PROXY_AUTH") or "none"
            svc.proxy_auth = mode if mode in PROXY_AUTH_MODES else "none"
            svc.proxy_username = os.environ.get(f"{kind.upper()}_PROXY_USERNAME") or os.environ.get("AUTHENTIK_SERVICE_USERNAME")
            svc.proxy_password = os.environ.get(f"{kind.upper()}_PROXY_PASSWORD") or os.environ.get("AUTHENTIK_SERVICE_APP_PASSWORD")
            svc.authentik_url = os.environ.get("AUTHENTIK_URL")
            svc.authentik_client_id = os.environ.get(f"{kind.upper()}_AUTHENTIK_CLIENT_ID")
            db.add(svc)
            log.info("Added %s from environment", svc.name)
        db.commit()


def _fix_progress() -> None:
    """One-time: plays whose app reported position 0 at stop get their progress from the time actually watched."""
    if get_setting("fix_progress_v1"):
        return
    with SessionLocal() as db:
        n = 0
        for p in db.scalars(select(Play).where(Play.runtime_seconds > 0, Play.watched_seconds > 0)):
            by_time = round(min(100.0, (p.watched_seconds or 0) / p.runtime_seconds * 100), 1)
            if by_time > (p.percent or 0):
                p.percent = by_time
                p.completed = by_time >= settings.watched_percent
                n += 1
        db.commit()
    set_setting("fix_progress_v1", True)
    if n:
        log.info("Recalculated progress for %d plays", n)


async def _backfill_runtimes(limit: int = 1000) -> int:
    """Plays without a length (imported from Jellystat, or from apps that didn't send one): look the length up
    in Jellyfin so progress and "finished" can be worked out. Items Jellyfin no longer has are marked so
    they aren't asked about again."""
    done = 0
    for svc in services(("jellyfin",)):
        with SessionLocal() as db:
            rows = list(db.execute(select(Play.id, Play.item_id).where(Play.server_id == svc.id, Play.runtime_seconds.is_(None),
                                                                      Play.item_id.isnot(None)).limit(limit)))
        if not rows:
            continue
        client = await client_for(svc)
        uid = await library.browse_as(client, svc.id, User(name="system", is_admin=True, via="none", role="admin"))
        if not uid:
            continue
        ids = list(dict.fromkeys(library._norm_id(i) for _, i in rows))
        found: dict[str, float] = {}
        for k in range(0, len(ids), 80):
            try:
                found.update(await library.runtimes(client, uid, ids[k:k + 80]))
            except UpstreamError:
                return done
        with SessionLocal() as db:
            for pid, iid in rows:
                p = db.get(Play, pid)
                rt = found.get(library._norm_id(iid))
                p.runtime_seconds = rt or 0.0          # 0 = looked, Jellyfin doesn't have it any more
                if rt:
                    by_time = round(min(100.0, (p.watched_seconds or 0) / rt * 100), 1)
                    if by_time > (p.percent or 0):
                        p.percent = by_time
                    p.completed = (p.percent or 0) >= settings.watched_percent
                    done += 1
            db.commit()
    if done:
        log.info("Filled in length and progress for %d plays", done)
        ttl_forget("uprof"); ttl_forget("profile")
    return done


def _rename_seerr() -> None:
    """One-time: services still named 'Jellyseerr / Seerr' (the old default) become just 'Seerr'."""
    with SessionLocal() as db:
        for svc in db.scalars(select(Service).where(Service.kind == "jellyseerr",
                                                    Service.name.in_(("Jellyseerr / Seerr", "Jellyseerr")))):
            svc.name = "Seerr"
        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_services_from_env()
    _rename_seerr()
    _fix_progress()
    task = asyncio.create_task(tracker.run_forever())
    warm = asyncio.create_task(_keep_warm())
    yield
    task.cancel()
    warm.cancel()
    await pool.close()


app = FastAPI(title="JellyCreep", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None,
              openapi_url="/api/openapi.json")
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="jc_session",
                   same_site="lax", https_only=settings.cookie_secure, max_age=60 * 60 * 24 * 14)
app.add_middleware(GZipMiddleware, minimum_size=1200, compresslevel=6)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    new = getattr(request.state, "new_csrf", None)
    if new:
        response.set_cookie(CSRF_COOKIE, new, httponly=False, samesite="strict", secure=settings.cookie_secure)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    p = request.url.path
    if p.startswith("/static/"):
        # versioned files (?v=…) never change; the rest (icons, manifest) for a day
        response.headers["Cache-Control"] = ("public, max-age=31536000, immutable" if request.query_params.get("v")
                                             else "public, max-age=86400")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return response


@app.exception_handler(ViewerOnly)
async def _viewer_redirect(request: Request, _exc: ViewerOnly):
    if request.method == "GET" and not request.url.path.startswith(("/api/", "/partials/", "/settings/import", "/media/quality/status")):
        return RedirectResponse("/me", status_code=303)
    return JSONResponse({"detail": "Not available"}, status_code=403)


@app.exception_handler(LoginRequired)
async def _login_redirect(request: Request, _exc: LoginRequired):
    nxt = request.url.path
    if request.url.path.startswith(("/partials/", "/api/")) or request.method != "GET":
        return JSONResponse({"detail": "Signed out"}, status_code=401)
    if not jellyfin_login_enabled() and settings.auth_mode == "oidc":
        return RedirectResponse(f"/auth/login?next={nxt}", status_code=302)
    return RedirectResponse(f"/login?next={nxt}", status_code=302)


# --- template helpers ----------------------------------------------------------------
def fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "0m"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m" if m else f"{seconds}s"


def fmt_local(dt: datetime | None, fmt: str = "%b %-d, %Y · %-I:%M %p") -> str:
    if not dt:
        return "—"
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ).strftime(fmt)


def fmt_ago(dt: datetime | None) -> str:
    if not dt:
        return "never"
    s = (utcnow() - dt).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{int(s // size)}{unit} ago"
    return "just now"


def fmt_bytes(n: float | None) -> str:
    if not n:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = min(int(math.log(max(n, 1), 1024)), len(units) - 1)
    return f"{n / 1024 ** i:.1f} {units[i]}"


templates.env.filters.update(duration=fmt_duration, local=fmt_local, ago=fmt_ago, bytes=fmt_bytes)
templates.env.globals.update(explain=explain.explain)


def fmt_device(ua: str | None) -> str:
    ua = ua or ""
    if not ua or "Mozilla" not in ua:
        return ua or "Unknown device"
    os_ = next((n for k, n in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Windows", "Windows"),
                                ("Mac OS", "Mac"), ("CrOS", "Chromebook"), ("Linux", "Linux")) if k in ua), "Device")
    br = next((n for k, n in (("Edg/", "Edge"), ("SamsungBrowser", "Samsung Internet"), ("Firefox", "Firefox"),
                               ("Chrome", "Chrome"), ("Safari", "Safari")) if k in ua), "browser")
    return f"{br} on {os_}"


templates.env.filters.update(device_name=fmt_device)
templates.env.globals.update(version=__version__, kinds=SERVICE_KINDS, proxy_modes=PROXY_AUTH_MODES)


def _day_label(d) -> str:
    """Today / Tomorrow / Fri, Nov 6 — in the server's time zone."""
    today = datetime.now(TZ).date()
    if d == today:
        return "Today"
    if (d - today).days == 1:
        return "Tomorrow"
    return f"{d:%a, %b} {d.day}"


def _ep_pretty(code: str) -> str:
    """'S02E05' -> 'S2 • E5' (anything else is left alone)."""
    m = re.fullmatch(r"S(\d+)E(\d+)", (code or "").strip())
    return f"S{int(m.group(1))} • E{int(m.group(2))}" if m else code


def _nice_ticks(mx: float, n: int = 4, time: bool = False) -> list[float]:
    """Round axis ticks from 0 up to just above ``mx`` (e.g. 0, 2, 4, 6; or 0, 15m, 30m, 45m for time)."""
    import math as _m
    if not mx or mx <= 0:
        return [0, 3600] if time else [0, 1]
    if time:
        raw = mx / n
        steps = [300, 600, 900, 1200, 1800, 3600, 7200, 10800, 14400, 21600, 43200, 86400, 172800, 432000, 864000]
        step = next((x for x in steps if x >= raw), steps[-1])
        top = _m.ceil(mx / step) * step
        return [i * step for i in range(int(top // step) + 1)]
    raw = mx / n
    mag = 10 ** _m.floor(_m.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    step = max(1, round(step)) if step < 1 or step != int(step) else step   # counts: whole numbers only
    top = _m.ceil(mx / step) * step
    return [round(i * step, 6) for i in range(int(round(top / step)) + 1)]


def _hour12(h: int, short: bool = True) -> str:
    h = int(h) % 24
    suf = ("a" if h < 12 else "p") if short else (" AM" if h < 12 else " PM")
    return f"{(h % 12) or 12}{suf}"


def _hours(secs: float) -> str:
    """Axis label for a number of seconds: 45m, 2h, 1.5h."""
    if secs < 3600:
        return f"{round(secs / 60)}m"
    h = secs / 3600
    return f"{h:.0f}h" if abs(h - round(h)) < 0.05 else f"{h:.1f}h"


_ICON_DIR = Path(__file__).parent / "static" / "icons"
_ICONS = {f.stem.lower(): f.name for f in _ICON_DIR.glob("*") if f.suffix in (".svg", ".png", ".webp")} if _ICON_DIR.is_dir() else {}
_ICON_ALIASES = {"jellyseerr": "seerr", "qbit": "qbittorrent", "sab": "sabnzbd"}


def _svc_icon(kind: str | None, name: str | None = None) -> str | None:
    """Logo for a service: by its kind first, then by any word in its name (so 'My Lidarr' finds lidarr.svg)."""
    tries = [_ICON_ALIASES.get(kind or "", kind or "")]
    tries += [w for w in re.split(r"[^a-z0-9]+", (name or "").lower()) if w]
    for t in tries:
        t = _ICON_ALIASES.get(t, t)
        if t in _ICONS:
            return f"/static/icons/{_ICONS[t]}?v={__version__}"
    return None


def _device_kind(client: str | None, device: str | None = None) -> str:
    """tv / desktop / phone / web / other — from the app and device names Jellyfin reports."""
    c, d = (client or "").lower(), (device or "").lower()
    both = c + " " + d
    if any(x in both for x in ("android tv", "shield", "fire tv", "firetv", "roku", "apple tv", "tvos", "webos", "tizen", "kodi",
                               "wholphin", "chromecast", " tv", "bravia", "xbox", "playstation")):
        return "tv"
    if any(x in c for x in ("web", "chrome", "firefox", "safari", "edge")) and "desktop" not in c:
        return "web"
    if any(x in both for x in ("desktop", "theater", "media player", "windows", "macos", "linux", "mpv")):
        return "desktop"
    if any(x in both for x in ("android", "ios", "iphone", "ipad", "mobile", "findroid", "swiftfin", "streamyfin", "pixel", "galaxy")):
        return "phone"
    return "other"


_PLATFORM_DIR = Path(__file__).parent / "static" / "icons" / "platform"
_PLATFORM_ICONS = {f.stem for f in _PLATFORM_DIR.glob("*.svg")} if _PLATFORM_DIR.is_dir() else set()
# (key, label, tile colour, words to look for in "client device") — first match wins
_PLATFORMS = [
    ("playstation", "PlayStation", "#00439c", ("playstation", "ps4", "ps5")),
    ("xbox", "Xbox", "#107c10", ("xbox",)),
    ("roku", "Roku", "#6c3c97", ("roku",)),
    ("appletv", "Apple TV", "#8e8e93", ("apple tv", "appletv", "tvos")),
    ("firetv", "Fire TV", "#ff9900", ("fire tv", "firetv", "aftm", "aftt", "afts", "aftka")),
    ("samsung", "Samsung TV", "#1428a0", ("samsung", "tizen")),
    ("lg", "LG TV", "#a50034", ("webos", " lg ", "lgtv", "lg tv")),
    ("kodi", "Kodi", "#17b2e7", ("kodi",)),
    ("moonfin", "Moonfin", "#4c1d95", ("moonfin",)),
    ("streamyfin", "Streamyfin", "#7c3aed", ("streamyfin",)),
    ("findroid", "Findroid", "#00a4dc", ("findroid",)),
    ("finamp", "Finamp", "#aa5cc3", ("finamp",)),
    ("androidtv", "Android TV", "#3ddc84", ("android tv", "androidtv", "shield", "wholphin", "chromecast", "google tv")),
    ("android", "Android", "#3ddc84", ("android",)),
    ("apple", "iPhone / iPad", "#e5e5ea", ("ios", "iphone", "ipad", "swiftfin", "infuse")),
    ("edge", "Edge", "#0b6bd3", ("edge",)),
    ("firefox", "Firefox", "#20123a", ("firefox",)),
    ("safari", "Safari", "#e5e5ea", ("safari",)),
    ("chrome", "Chrome", "#ffffff", ("chrome", "chromium")),
    ("windows", "Windows", "#0078d4", ("windows",)),
    ("apple", "Mac", "#e5e5ea", ("mac", "macos")),
    ("linux", "Linux", "#fcc624", ("linux",)),
]
_PLATFORM_ALIAS = {"androidtv": "android"}


def _platform(client: str | None, device: str | None) -> dict:
    """Which kind of device a stream is on, with a logo (or a glyph) and a brand colour for its tile."""
    hay = f" {(client or '').lower()} {(device or '').lower()} "
    for key, label, colour, words in _PLATFORMS:
        if any(w in hay for w in words):
            icon = _PLATFORM_ALIAS.get(key, key)
            src = (f"/static/icons/platform/{icon}.svg?v={__version__}" if icon in _PLATFORM_ICONS
                   else (_svc_icon(icon) if icon == "moonfin" else None))
            return {"key": key, "label": label, "colour": colour, "src": src,
                    "mono": key in ("playstation", "xbox", "android", "androidtv", "windows", "edge", "firetv", "appletv", "roku"),
                    "tv": key in
                    ("playstation", "xbox", "roku", "appletv", "firetv", "samsung", "lg", "androidtv", "kodi")}
    return {"key": "jellyfin", "label": client or "Jellyfin", "colour": "#101826", "src": _svc_icon("jellyfin"), "tv": False, "mono": False}


def _left(st: dict) -> str | None:
    rt, pos = st.get("runtime_seconds"), st.get("position_seconds")
    if not rt or pos is None:
        return None
    m = max(0, round((rt - pos) / 60))
    return f"{m // 60}h {m % 60}min left" if m >= 60 else f"{m}min left"


templates.env.globals.update(svc_icon=_svc_icon, device_kind=_device_kind, platform=_platform, time_left=_left,
                             ip_kind=geoip.kind)
templates.env.globals.update(day_label=_day_label, ep_pretty=_ep_pretty, nice_ticks=_nice_ticks, hour12=_hour12, axis_hours=_hours)


_AMBIENT: dict[str, Any] = {}


def ambient_url(user: User | None = None) -> str | None:
    """Backdrop for the glass background: what's playing now, else the last thing watched (cached 60 s).
    Viewers only ever get their own artwork."""
    viewer = user is not None and user.is_viewer
    key = f"v:{user.name}" if viewer else "staff"
    hit = _AMBIENT.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    url = None
    try:
        with SessionLocal() as db:
            for act in db.scalars(select(ActiveStream).order_by(ActiveStream.started_at.desc()).limit(20)):
                d = json.loads(act.data)
                if viewer and not (_norm(d.get("user_id")) == user.jf_id or d.get("user_name") == user.name):
                    continue
                if d.get("poster_id"):
                    url = f"{'/me/img' if viewer else '/img'}/{act.server_id}/{d['poster_id']}?t=Backdrop&h=720"
                    break
            if not url:
                q = select(Play).where(Play.poster_id.is_not(None))
                if viewer:
                    q = q.where(_mine(user))
                p = db.scalar(q.order_by(Play.stopped_at.desc()))
                if p:
                    url = f"{'/me/img' if viewer else '/img'}/{p.server_id}/{p.poster_id}?t=Backdrop&h=720"
    except Exception:  # noqa: BLE001
        url = None
    _AMBIENT[key] = (time.time(), url)
    return url


def render(request: Request, name: str, user: User, **ctx: Any) -> HTMLResponse:
    ctx.update(request=request, user=user, csrf=csrf_token(request), auth_mode=settings.auth_mode,
               ambient=ctx.get("ambient") or ambient_url(user), imgp="/me/img" if user.is_viewer else "/img")
    if user.is_viewer:
        ctx.setdefault("portal_seerr", login_settings()["seerr_discover"] and bool(services(("jellyseerr",))))
        ctx.setdefault("flash", request.session.pop("flash", None))
    return templates.TemplateResponse(request, name, ctx)


def services(kinds: tuple[str, ...] | None = None, enabled_only: bool = True) -> list[Service]:
    with SessionLocal() as db:
        q = select(Service).order_by(Service.kind, Service.name)
        if kinds:
            q = q.where(Service.kind.in_(kinds))
        if enabled_only:
            q = q.where(Service.enabled.is_(True))
        return list(db.scalars(q))


async def safe(coro) -> tuple[Any, str | None]:
    try:
        return await coro, None
    except UpstreamError as e:
        return None, str(e)
    except Exception as e:  # noqa: BLE001 – surface anything to the UI
        log.exception("Upstream call failed")
        return None, f"{e.__class__.__name__}: {e}"


# --- activity ------------------------------------------------------------------------
# --- short-lived cache: identical upstream calls from many phones share one request ----
_TTL: dict[Any, tuple[float, Any]] = {}
_INFLIGHT: dict[Any, asyncio.Future] = {}


_BG: set = set()


async def ttl_cached(key: Any, ttl: float, factory, stale: float = 0) -> Any:
    """Short shared cache. With ``stale``, an expired answer is still returned at once (for up to
    ``stale`` seconds) while a fresh one is fetched in the background."""
    hit = _TTL.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    if stale and hit and hit[0] + stale > now:
        if key not in _BG and key not in _INFLIGHT:
            _BG.add(key)

            async def refresh():
                try:
                    _TTL[key] = (time.monotonic() + ttl, await factory())
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    _BG.discard(key)
            asyncio.get_running_loop().create_task(refresh())
        return hit[1]
    if key in _INFLIGHT:                       # someone is already fetching this: wait for them
        return await asyncio.shield(_INFLIGHT[key])
    fut = asyncio.get_running_loop().create_future()
    _INFLIGHT[key] = fut
    try:
        val = await factory()
        _TTL[key] = (time.monotonic() + ttl, val)
        if len(_TTL) > 2000:
            for k in [k for k, (exp, _) in _TTL.items() if exp + 3600 < now][:1000]:
                _TTL.pop(k, None)
        fut.set_result(val)
        return val
    except BaseException as e:
        fut.set_exception(e)
        fut.exception()                        # mark retrieved
        raise
    finally:
        _INFLIGHT.pop(key, None)


def ttl_peek(key: Any) -> Any:
    """The cached value if it's still fresh, else None (never fetches)."""
    hit = _TTL.get(key)
    return hit[1] if hit and hit[0] > time.monotonic() else None


def ttl_forget(prefix: str) -> None:
    for k in [k for k in _TTL if isinstance(k, tuple) and k and k[0] == prefix]:
        _TTL.pop(k, None)


async def current_activity() -> tuple[list[dict], list[str]]:
    return await ttl_cached(("activity",), 2, _current_activity)


async def _current_activity() -> tuple[list[dict], list[str]]:
    streams, errors = [], []
    servers = services(("jellyfin",))
    with SessionLocal() as db:
        tracked = {s.key: s for s in db.scalars(select(ActiveStream))}
    for svc in servers:
        client = await client_for(svc)
        sessions, err = await safe(client.sessions())
        if err:
            errors.append(f"{svc.name}: {err}")
            continue
        for sess in sessions or []:
            if not sess.get("NowPlayingItem"):
                continue
            snap = tracker.snapshot(sess)
            key = f"{svc.id}:{sess.get('Id')}:{snap['item_id']}"
            t = tracked.get(key)
            rt, pos = snap["runtime_seconds"], snap["position_seconds"]
            ends = None
            if rt and pos is not None and not snap["is_paused"]:
                ends = (datetime.now(TZ) + timedelta(seconds=max(rt - pos, 0))).strftime("%-I:%M %p")
            snap.update(ends_at=ends, remaining=max((rt or 0) - (pos or 0), 0),
                server_id=svc.id, server_name=svc.name, session_id=sess.get("Id"),
                progress=round(pos / rt * 100, 1) if rt and pos is not None else 0,
                watched=t.watched_seconds if t else 0, started=t.started_at if t else None,
                display=Play(**{k: snap.get(k) for k in ("item_type", "title", "series_name", "season", "episode", "year")}).display_title,
            )
            streams.append(snap)
    return streams, errors


@app.get("/", response_class=HTMLResponse)
async def page_activity(request: Request, user: User = Depends(staff_user)):
    streams, errors = await current_activity()
    with SessionLocal() as db:
        recent = list(db.scalars(select(Play).order_by(Play.stopped_at.desc()).limit(8)))
        today = stats.summary(db, 1)
    geo = await geoip.lookup([x.get("ip") for x in streams], wait=False) if user.is_admin else {}
    return render(request, "activity.html", user, streams=streams, errors=errors, recent=recent, today=today, geo=geo,
                  flash=request.session.pop("flash", None),
                  last=recent[0] if recent else None,
                  health=services(),
                  has_servers=bool(services(("jellyfin",), enabled_only=False)))


@app.get("/partials/activity", response_class=HTMLResponse)
async def partial_activity(request: Request, user: User = Depends(staff_user)):
    streams, errors = await current_activity()
    with SessionLocal() as db:
        last = db.scalar(select(Play).order_by(Play.stopped_at.desc()))
    geo = await geoip.lookup([x.get("ip") for x in streams], wait=False) if user.is_admin else {}
    return render(request, "_live.html", user, streams=streams, errors=errors, last=last, geo=geo)


async def _session_caps(client, session_id: str) -> tuple[dict | None, bool]:
    """(the Jellyfin session, whether its app says it can show messages)."""
    try:
        data = await client.get("/Sessions") or []
    except UpstreamError:
        return None, True            # can't tell: try anyway
    s = next((x for x in data if x.get("Id") == session_id), None)
    if not s:
        return None, False
    cmds = s.get("SupportedCommands") or (s.get("Capabilities") or {}).get("SupportedCommands") or []
    return s, ("DisplayMessage" in cmds) if cmds else True


async def _stream_action(request: Request, server_id: int, session_id: str, user: User, stop: bool):
    await verify_csrf(request)
    form = await request.form()
    with SessionLocal() as db:
        svc = db.get(Service, server_id)
    if not svc or svc.kind != "jellyfin":
        raise HTTPException(404)
    client = await client_for(svc)
    msg = str(form.get("message") or "").strip()[:300]
    sess, can_msg = await _session_caps(client, session_id)
    who = (sess or {}).get("UserName") or "them"
    app_name = (sess or {}).get("Client") or "their app"
    notes, ok = [], True
    if msg:
        if not can_msg:
            notes.append(f"{app_name} can't show messages, so {who} won't see it.")
        _, err = await safe(client.message(session_id, "Message from the server" if not stop else "Stream stopped", msg))
        if err:
            ok = False
            notes.append(f"Couldn't send the message: {err}")
        elif can_msg:
            notes.append(f"Message sent to {who} on {app_name}.")
        if stop:
            await asyncio.sleep(2)   # let it appear before playback closes
    elif not stop:
        request.session["flash"] = {"ok": False, "msg": "Type a message first."}
        return RedirectResponse("/", status_code=303)
    if stop:
        _, err = await safe(client.stop(session_id))
        if err:
            ok = False
            notes.append(f"Couldn't stop the stream: {err}")
        else:
            notes.insert(0, f"Stopped {who}'s stream.")
            log.info("%s stopped session %s on %s", user.name, session_id, svc.name)
            ttl_forget("activity")
    request.session["flash"] = {"ok": ok and (can_msg or not msg), "msg": " ".join(notes)}
    return RedirectResponse("/", status_code=303)


@app.post("/activity/{server_id}/{session_id}/stop")
async def stop_stream(server_id: int, session_id: str, request: Request, user: User = Depends(admin_user)):
    return await _stream_action(request, server_id, session_id, user, stop=True)


@app.post("/activity/{server_id}/{session_id}/message")
async def message_stream(server_id: int, session_id: str, request: Request, user: User = Depends(admin_user)):
    return await _stream_action(request, server_id, session_id, user, stop=False)


@app.get("/img/{server_id}/{item_id}")
async def image(server_id: int, item_id: str, h: int = 300, t: str = "Primary", user: User = Depends(current_user)):
    if not item_id.isalnum() or t not in ("Primary", "Backdrop", "Thumb", "Logo"):
        raise HTTPException(400)
    with SessionLocal() as db:
        svc = db.get(Service, server_id)
    if not svc or svc.kind != "jellyfin":
        raise HTTPException(404)
    client = await client_for(svc)
    r, err = await safe(client.image(item_id, kind=t, max_height=min(max(h, 50), 900)))
    if err or r is None:
        return Response(status_code=404)
    return Response(r.content, media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "private, max-age=86400"})


# --- history / users / stats ---------------------------------------------------------
def history_query(user_name: str | None, item_type: str | None, q: str | None, method: str | None):
    stmt = select(Play)
    if user_name:
        stmt = stmt.where(Play.user_name == user_name)
    if item_type:
        stmt = stmt.where(Play.item_type == item_type)
    if method:
        stmt = stmt.where(Play.play_method == method)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Play.title.ilike(like), Play.series_name.ilike(like)))
    return stmt


@app.get("/history", response_class=HTMLResponse)
async def page_history(request: Request, user_name: str | None = None, type: str | None = None, q: str | None = None,
                       method: str | None = None, when: str = "all", status: str = "all", device: str = "",
                       group: str = "1", page: int = 1, user: User = Depends(staff_user)):
    per = 50
    page = max(1, min(page, 1000))
    q = (q or "").strip()[:100]
    legacy = {"Movie": "movie", "Episode": "tv", "Audio": "music"}
    type = legacy.get(type or "", type or "all")
    type = type if type in HIST_TYPES else "all"
    method = method if method in HIST_METHODS else ""
    when = when if when in HIST_WHEN else "all"
    status = status if status in HIST_STATUS else "all"
    grouped = group != "0"
    with SessionLocal() as db:
        users = [u for (u,) in db.execute(select(Play.user_name).distinct().order_by(Play.user_name))]
        devices = sorted({c for c in db.scalars(select(Play.client).distinct()) if c})
        user_name = user_name if user_name in users else ""
        device = device if device in devices else ""
        stmt = history_query(user_name or None, HIST_TYPES[type][1], q or None, method or None)
        if when != "all":
            stmt = stmt.where(Play.started_at >= utcnow() - timedelta(days=int(when)))
        if status == "finished":
            stmt = stmt.where(Play.completed.is_(True))
        elif status == "partial":
            stmt = stmt.where(Play.completed.is_(False))
        if device:
            stmt = stmt.where(Play.client == device)
        sub = stmt.subquery()
        total, secs, done, people = db.execute(select(
            func.count(), func.coalesce(func.sum(sub.c.watched_seconds), 0),
            func.coalesce(func.sum(case((sub.c.completed.is_(True), 1), else_=0)), 0),
            func.count(func.distinct(sub.c.user_name)))).one()
        rows = list(db.scalars(stmt.order_by(Play.started_at.desc()).offset((page - 1) * per).limit(per)))
    days = _history_days(rows, grouped)
    geo = await geoip.lookup([r.ip for r in rows], wait=False) if user.is_admin else {}
    params = {"q": q, "user_name": user_name, "type": type, "method": method, "when": when, "status": status,
              "device": device, "group": "1" if grouped else "0"}
    active = [(k, v) for k, v in (("user_name", user_name or None), ("type", HIST_TYPES[type][0] if type != "all" else None),
                                  ("method", HIST_METHODS.get(method) if method else None),
                                  ("when", HIST_WHEN[when] if when != "all" else None),
                                  ("status", HIST_STATUS[status] if status != "all" else None), ("device", device or None)) if v]

    def link(**kw):
        return "?" + urlencode({k: v for k, v in {**params, **kw}.items() if v not in ("", None, "all")})
    return render(request, "history.html", user, days=days, total=total, secs=secs, done=done, people=people, page=page,
                  pages=max(1, math.ceil(total / per)), q=q, user_name=user_name, type=type, method=method, when=when,
                  status=status, device=device, grouped=grouped, users=users, devices=devices, active=active, link=link,
                  types=HIST_TYPES, whens=HIST_WHEN, statuses=HIST_STATUS, methods=HIST_METHODS, geo=geo)


@app.get("/history/{play_id}", response_class=HTMLResponse)
async def page_play(request: Request, play_id: int, user: User = Depends(staff_user)):
    """Everything recorded about one play: who, when, where from, which app and how it was streamed."""
    with SessionLocal() as db:
        p = db.get(Play, play_id)
        if not p:
            raise HTTPException(404, "That play isn't in the history.")
        same = list(db.scalars(select(Play).where(Play.user_name == p.user_name, Play.id != p.id,
                                                  Play.item_id == p.item_id if p.item_id else Play.title == p.title)
                               .order_by(Play.started_at.desc()).limit(8)))
        prev_p = db.scalar(select(Play).where(Play.user_name == p.user_name, Play.started_at < p.started_at).order_by(Play.started_at.desc()))
        next_p = db.scalar(select(Play).where(Play.user_name == p.user_name, Play.started_at > p.started_at).order_by(Play.started_at))
        ip_uses = db.scalar(select(func.count()).select_from(Play).where(Play.ip == p.ip)) if p.ip else 0
        ip_users = sorted({u for u in db.scalars(select(Play.user_name).where(Play.ip == p.ip).distinct())}) if p.ip else []
    loc = (await geoip.lookup([p.ip])).get(p.ip) if (user.is_admin and p.ip) else None
    back = request.headers.get("referer") or ""
    back = back if "/history" in back and "/history/" not in back else "/history"
    return render(request, "play.html", user, p=p, same=same, prev_p=prev_p, next_p=next_p, loc=loc, ip_uses=ip_uses,
                  ip_users=ip_users, why=explain.explain(p.transcode_reasons, p.video_codec, p.audio_codec, p.resolution) if p.play_method == "Transcode" else [],
                  methods=HIST_METHODS, back=urlsplit(back).path + (("?" + urlsplit(back).query) if urlsplit(back).query else ""))


HIST_METHODS = {"DirectPlay": "Direct play", "DirectStream": "Direct stream", "Transcode": "Transcode"}


@app.get("/users", response_class=HTMLResponse)
async def page_users(request: Request, user: User = Depends(staff_user)):
    def build():
        with SessionLocal() as db:
            return stats.users_table(db, TZ)
    rows = await ttl_cached(("users",), 30, lambda: asyncio.to_thread(build), stale=600)
    sort = request.query_params.get("sort") or "recent"
    sort = sort if sort in USER_SORTS else "recent"
    rows = [{**r, "colour": seerr_portal.colour(r["user"] or "?")} for r in sorted(rows, key=USER_SORTS[sort][1], reverse=sort != "name")]
    streams, _ = await current_activity()
    live = {}
    for x in streams:
        live.setdefault(x.get("user_name"), x)
    geo = await geoip.lookup([r["last_ip"] for r in rows], wait=False) if user.is_admin else {}
    return render(request, "users.html", user, rows=rows, live=live, geo=geo, sort=sort, sorts=USER_SORTS,
                  totals={"people": len(rows), "plays": sum(r["plays"] for r in rows), "secs": sum(r["seconds"] for r in rows),
                          "active30": sum(1 for r in rows if r["plays30"]), "live": sum(1 for r in rows if r["user"] in live)})


USER_SORTS = {"recent": ("Last seen", lambda r: r["last"] or datetime.min), "time": ("Watch time", lambda r: r["seconds"]),
              "plays": ("Plays", lambda r: r["plays"]), "month": ("Last 30 days", lambda r: r["secs30"]),
              "name": ("Name", lambda r: (r["user"] or "").lower())}


@app.get("/stats", response_class=HTMLResponse)
async def page_stats(request: Request, days: int = 30, user: User = Depends(staff_user)):
    days = days if days in (7, 30, 90, 365) else 30
    with SessionLocal() as db:
        ctx = dict(
            days=days, summary=stats.summary(db, days), top_users=stats.top_users(db, days),
            top_movies=stats.top_movies(db, days), top_shows=stats.top_shows(db, days),
            per_day=stats.plays_per_day(db, days, TZ), cmp=stats.compare(db, days), weekdays=stats.by_weekday(db, days, TZ),
            methods=stats.grouped(db, Play.play_method, days), clients=stats.grouped(db, Play.client, days),
            types=stats.grouped(db, Play.item_type, days),
        )
        ctx["heat"], ctx["by_hour"], ctx["heat_peak"] = stats.heatmap(db, days, TZ)
        ctx["conc"] = stats.concurrency(db, days, TZ)
    jellystat = []
    for svc in services(("jellystat",)):
        client = await client_for(svc)
        users, err = await safe(ttl_cached(("jstat", svc.id, days), 300, lambda c=client: c.most_active_users(days), stale=3600))
        jellystat.append({"svc": svc, "users": users, "error": err})
    return render(request, "stats.html", user, jellystat=jellystat, **ctx)


# --- media & automation ----------------------------------------------------------------
@app.get("/media", response_class=HTMLResponse)
async def page_media(request: Request, tab: str = "overview", server: int | None = None,
                     user: User = Depends(staff_user)):
    if tab == "quality":
        return await _media_quality(request, user, server)
    # Jellyfin's library details and the storage report are slow to gather (Jellyfin checks every drive,
    # Sonarr/Radarr go through Authentik), so they're fetched together, remembered, and kept warm.
    servers, (drives, notes) = await asyncio.gather(
        ttl_cached(("media",), 120, _media_servers, stale=6 * 3600),
        ttl_cached(("storage",), 600, _storage_report, stale=6 * 3600))
    svcs = {x.id: x for x in services(("jellyfin",))}
    servers = [{**x, "svc": svcs.get(x["sid"])} for x in servers if svcs.get(x["sid"])]
    # drives you've hidden (and the OS drive, hidden by default) don't show or count toward totals
    prefs = get_setting("storage_drives", {}) or {}
    drives = [dict(d) for d in drives]
    visible, hidden = storage.split_hidden(drives, set(prefs.get("hidden") or []), set(prefs.get("shown") or []))
    show_all = request.query_params.get("drives") == "all"
    return render(request, "media.html", user, servers=servers, drives=(visible + hidden) if show_all else visible,
                  hidden_drives=hidden, show_all=show_all, storage_notes=notes,
                  storage_sum=storage.summary(visible), flash=request.session.pop("flash", None))


@app.post("/media/storage/{action}")
async def storage_drive_pref(action: str, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    form = await request.form()
    key = str(form.get("key") or "")[:300]
    if action not in ("hide", "show") or not key:
        raise HTTPException(400)
    prefs = get_setting("storage_drives", {}) or {}
    hidden, shown = set(prefs.get("hidden") or []), set(prefs.get("shown") or [])
    if action == "hide":
        hidden.add(key); shown.discard(key)
    else:
        hidden.discard(key); shown.add(key)
    set_setting("storage_drives", {"hidden": sorted(hidden), "shown": sorted(shown)})
    return RedirectResponse("/media" + ("?drives=all" if action == "show" and form.get("all") else "") + "#storage", status_code=303)


async def _media_servers() -> list[dict]:
    async def one(svc: Service) -> dict:
        client = await client_for(svc)
        (recent, e1), (libs, e2), (counts, e3), (info, e4) = await asyncio.gather(
            safe(client.recently_added()), safe(client.libraries()), safe(client.counts()), safe(client.system_info()))
        return {"sid": svc.id, "recent": recent or [], "libs": libs or [], "counts": counts or {}, "info": info or {},
                "error": e1 or e2 or e3 or e4}
    return list(await asyncio.gather(*(one(x) for x in services(("jellyfin",)))))


async def _storage_report() -> tuple[list[dict], list[str]]:
    drives, notes = await storage.collect()
    if drives:
        await asyncio.to_thread(storage.record, drives)
        await asyncio.to_thread(storage.forecast, drives)
    return drives, notes


async def _media_quality(request: Request, user: User, server: int | None):
    jfs = services(("jellyfin",))
    svc = next((x for x in jfs if x.id == server), jfs[0] if jfs else None)
    report = quality.cached(svc.id) if svc else None
    if svc and not report and not quality.STATUS["running"]:
        asyncio.create_task(quality.scan(svc.id))   # first visit: start scanning in the background
    hist = await asyncio.to_thread(quality.history, 90)
    return render(request, "quality.html", user, svc=svc, servers=jfs, q=report, hist=hist, status=quality.STATUS)


@app.post("/media/quality/scan/{sid}")
async def quality_rescan(sid: int, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    _svc(sid, "jellyfin")
    if not quality.STATUS["running"]:
        asyncio.create_task(quality.scan(sid))
    return RedirectResponse(f"/media?tab=quality&server={sid}", status_code=303)


@app.get("/media/quality/status")
async def quality_status(user: User = Depends(staff_user)):
    return quality.STATUS


AUTOMATION_TABS = {
    "calendar": ("Calendar", ("sonarr", "radarr", "lidarr")),
    "queue": ("Queue", ("sonarr", "radarr", "lidarr")),
    "downloads": ("Downloads", ("sabnzbd", "qbittorrent")),
    "requests": ("Requests", ("jellyseerr",)),
    "indexers": ("Indexers", ("prowlarr",)),
}


async def _cached_panel(svc: Service, tab: str) -> dict:
    """Sonarr/Radarr/SAB/qBit/Seerr panels go through Authentik and can be slow: show the last answer
    straight away (refreshed every 15 s in the background), and fetch fresh right after an action."""
    data = await ttl_cached(("arrpanel", svc.id, tab), 15, lambda: _arr_panel(svc, tab), stale=600)
    return {**data, "svc": svc}


async def _arr_panel(svc: Service, tab: str) -> dict:
    client = await client_for(svc)
    panel: dict[str, Any] = {"svc": svc}
    if tab == "queue" and svc.kind in ("sonarr", "radarr", "lidarr"):
        # (recent activity and disk space live on Events and Libraries now)
        (panel["queue"], e1), (panel["health"], e2) = await asyncio.gather(safe(client.queue()), safe(client.health()))
        panel["error"] = e1 or e2
    elif svc.kind == "prowlarr":
        (panel["indexers"], e1), (panel["status"], e2), (panel["health"], e3) = await asyncio.gather(
            safe(client.indexers()), safe(client.indexer_status()), safe(client.health()))
        panel["failing"] = {s.get("indexerId") for s in panel["status"] or []}
        panel["error"] = e1 or e2 or e3
    elif svc.kind == "qbittorrent":
        (panel["torrents"], e1), (panel["transfer"], e2) = await asyncio.gather(safe(client.torrents()), safe(client.transfer()))
        panel["error"] = e1 or e2
    elif svc.kind == "sabnzbd":
        (panel["sab"], e1), (panel["sab_history"], e2) = await asyncio.gather(safe(client.queue()), safe(client.history()))
        panel["error"] = e1 or e2
    elif svc.kind == "jellyseerr":
        (panel["requests"], e1), (panel["counts"], e2) = await asyncio.gather(safe(client.requests()), safe(client.counts()))
        panel["error"] = e1 or e2
    return panel


async def _calendar(ym: str | None) -> tuple[dict, list[str]]:
    today = datetime.now(TZ).date()
    year, month = calendar_view.month_bounds(ym, today)
    start, end = calendar_view.grid_range(year, month)
    items, errors = [], []

    async def one(svc: Service):
        client = await client_for(svc)
        # pad a day each side so time-zone shifts don't drop evening episodes
        raw, err = await safe(client.calendar(start - timedelta(days=1), end + timedelta(days=1)))
        if err:
            errors.append(f"{svc.name}: {err}")
        else:
            items.extend(calendar_view.items_for(svc, raw, TZ, start, end))

    await asyncio.gather(*(one(s) for s in services(("sonarr", "radarr", "lidarr"))))
    return calendar_view.build(year, month, items, today), errors


async def _calendar_cached(ym: str | None) -> tuple[dict, list[str]]:
    today = datetime.now(TZ).date()
    key = ("cal", calendar_view.month_bounds(ym, today), today)
    return await ttl_cached(key, 120, lambda: _calendar(ym), stale=1800)


@app.get("/automation", response_class=HTMLResponse)
async def page_automation(request: Request, tab: str = "calendar", m: str | None = None,
                          user: User = Depends(staff_user)):
    available = {k: v for k, v in AUTOMATION_TABS.items() if services(v[1])}
    if tab not in available:
        tab = next(iter(available), "calendar")
    ctx: dict[str, Any] = {"tab": tab, "tabs": available, "panels": [], "cal": None, "errors": [],
                           "flash": request.session.pop("flash", None)}
    if tab == "calendar" and available:
        ctx["cal"], ctx["errors"] = await _calendar_cached(m)
    elif available:
        if ctx["flash"]:                       # just approved / removed / paused something: show it fresh
            ttl_forget("arrpanel")
        ctx["panels"] = await asyncio.gather(*(_cached_panel(s, tab) for s in services(available[tab][1])))
    return render(request, "automation.html", user, **ctx)


@app.get("/arrimg/{sid}/{item_id}")
async def arr_image(sid: int, item_id: int, t: str = "poster", user: User = Depends(current_user)):
    if t not in ("poster", "fanart"):
        raise HTTPException(400)
    with SessionLocal() as db:
        svc = db.get(Service, sid)
    if not svc or svc.kind not in ("sonarr", "radarr"):
        raise HTTPException(404)
    client = await client_for(svc)
    r, err = await safe(client.cover(item_id, t))
    if err or r is None or not r.headers.get("content-type", "").startswith("image"):
        return Response(status_code=404)
    return Response(r.content, media_type=r.headers.get("content-type"),
                    headers={"Cache-Control": "private, max-age=604800"})


@app.get("/events", response_class=HTMLResponse)
async def page_events(request: Request, source: str | None = None, user: User = Depends(staff_user)):
    with SessionLocal() as db:
        stmt = select(Event).order_by(Event.created_at.desc()).limit(200)
        if source:
            stmt = stmt.where(Event.source == source)
        rows = list(db.scalars(stmt))
        sources = [s for (s,) in db.execute(select(Event.source).distinct())]
    return render(request, "events.html", user, rows=rows, sources=sources, source=source or "")


# --- invites (Wizarr) -------------------------------------------------------------------
def _wizarr(sid: int) -> Service:
    with SessionLocal() as db:
        svc = db.get(Service, sid)
    if not svc or svc.kind != "wizarr":
        raise HTTPException(404, "Wizarr service not found")
    return svc


def _clean_libraries(libs: list[dict], servers: list[dict]) -> dict[int, list[dict]]:
    """Wizarr can list the same library more than once (re-scans, or an old unverified
    server entry). Keep one per (server, library), only for verified servers, grouped by server."""
    server_ids = {s.get("id") for s in servers}
    out: dict[int, list[dict]] = {sid: [] for sid in server_ids}
    seen: set = set()
    for lib in sorted(libs, key=lambda l: (l.get("enabled") is False, l.get("id") or 0)):
        sid = lib.get("server_id")
        if lib.get("enabled") is False:
            continue
        if sid is None and len(server_ids) == 1:
            sid = next(iter(server_ids))  # library without server info → the only server
        if servers and sid not in server_ids:
            continue  # belongs to an unverified / removed server
        key = (sid, lib.get("external_id") or (lib.get("name") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(sid, []).append(lib)
    for sid in out:
        out[sid].sort(key=lambda l: (l.get("name") or "").lower())
    return {k: v for k, v in out.items() if v}


def _is_internal(url: str | None) -> bool:
    from urllib.parse import urlsplit
    host = (urlsplit(url or "").hostname or "").lower()
    return bool(url) and ("." not in host or host == "localhost" or host.startswith(("10.", "192.168.", "127.", "172.")))


def _invite_url(svc: Service, url: str | None, code: str | None) -> str | None:
    """Wizarr often answers with just '/j/CODE' (or its internal docker address): make it a full link people can open."""
    from urllib.parse import urlsplit
    base = (svc.public_url or svc.url or "").rstrip("/")
    if not url and code:
        url = f"/j/{code}"
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        u = urlsplit(url)
        if svc.public_url and _is_internal(url):
            return base + (u.path or "") + (f"?{u.query}" if u.query else "")
        return url
    return base + ("" if url.startswith("/") else "/") + url


@app.get("/invites", response_class=HTMLResponse)
async def page_invites(request: Request, user: User = Depends(admin_user)):
    panels = []
    if "flash" in request.session or "new_invite" in request.session:
        ttl_forget("wizarr")                 # just created / removed an invite: show it fresh
    for svc in services(("wizarr",)):
        client = await client_for(svc)

        async def load(client=client):
            return await asyncio.gather(safe(client.status()), safe(client.servers()), safe(client.libraries()),
                                        safe(client.invitations()), safe(client.users()))
        (status, e1), (servers, e2), (libs, e3), (invs, e4), (users, e5) = await ttl_cached(("wizarr", svc.id), 20, load, stale=600)
        invs = sorted(invs or [], key=lambda i: i.get("created") or "", reverse=True)
        invs = [{**i, "url": _invite_url(svc, i.get("url"), i.get("code"))} for i in invs]
        verified = [s for s in servers or [] if s.get("verified", True)]
        panels.append({"svc": svc, "status": status or {}, "servers": verified,
                       "libs": _clean_libraries(libs or [], verified), "invites": invs, "users": users or [],
                       "error": e1 or e2 or e3 or e4 or e5})
    flash = request.session.pop("flash", None)
    new_invite = request.session.pop("new_invite", None)
    return render(request, "invites.html", user, panels=panels, flash=flash, new_invite=new_invite)


@app.post("/invites/{sid}/create")
async def create_invite(sid: int, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    form = await request.form()
    svc = _wizarr(sid)
    server_ids = [int(x) for x in form.getlist("server_ids") if str(x).isdigit()]
    if not server_ids:
        request.session["flash"] = {"ok": False, "msg": "Pick at least one media server for the invite."}
        return RedirectResponse("/invites", status_code=303)
    body: dict[str, Any] = {
        "server_ids": server_ids,
        "library_ids": [int(x) for x in form.getlist("library_ids") if str(x).isdigit()],
        "allow_downloads": form.get("allow_downloads") == "on",
        "allow_live_tv": form.get("allow_live_tv") == "on",
    }
    exp = str(form.get("expires_in_days") or "")
    if exp in ("1", "7", "30"):
        body["expires_in_days"] = int(exp)
    duration = str(form.get("duration") or "").strip()
    if duration.isdigit() and int(duration) > 0:
        body.update(duration=duration, unlimited=False)
    else:
        body.update(duration="unlimited", unlimited=True)
    client = await client_for(svc)
    inv, err = await safe(client.create_invitation(body))
    if err:
        request.session["flash"] = {"ok": False, "msg": err}
    else:
        request.session["new_invite"] = {"url": _invite_url(svc, inv.get("url"), inv.get("code")), "code": inv.get("code"),
                                         "server": inv.get("display_name"), "sid": svc.id,
                                         "no_public": _is_internal(_invite_url(svc, inv.get("url"), inv.get("code")))}
        log.info("%s created Wizarr invite %s", user.name, inv.get("code"))
    return RedirectResponse("/invites", status_code=303)


@app.post("/invites/{sid}/invitation/{inv_id}/delete")
async def delete_invite(sid: int, inv_id: int, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    client = await client_for(_wizarr(sid))
    _, err = await safe(client.delete_invitation(inv_id))
    request.session["flash"] = {"ok": not err, "msg": err or "Invitation deleted."}
    return RedirectResponse("/invites", status_code=303)


@app.post("/invites/{sid}/users/{user_id}/{action}")
async def wizarr_user_action(sid: int, user_id: int, action: str, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    if action not in ("extend", "enable", "disable", "delete"):
        raise HTTPException(400)
    form = await request.form()
    days = int(form.get("days") or 30) if str(form.get("days") or "30").isdigit() else 30
    client = await client_for(_wizarr(sid))
    msg, err = await safe(client.user_action(user_id, action, days))
    request.session["flash"] = {"ok": not err, "msg": err or msg}
    log.info("%s ran Wizarr %s on user %s", user.name, action, user_id)
    return RedirectResponse("/invites#users", status_code=303)


# --- actions: requests & downloads ------------------------------------------------------
def _svc(sid: int, *kinds: str) -> Service:
    with SessionLocal() as db:
        svc = db.get(Service, sid)
    if not svc or svc.kind not in kinds:
        raise HTTPException(404, "Service not found")
    return svc


async def _act(request: Request, coro, ok_msg: str, back: str):
    _, err = await safe(coro)
    request.session["flash"] = {"ok": not err, "msg": err or ok_msg}
    return RedirectResponse(back, status_code=303)


@app.post("/act/seerr/{sid}/{req_id}/{action}")
async def act_seerr(sid: int, req_id: int, action: str, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    if action not in ("approve", "decline"):
        raise HTTPException(400)
    client = await client_for(_svc(sid, "jellyseerr"))
    log.info("%s %sd Seerr request %s", user.name, action, req_id)
    return await _act(request, client.set_status(req_id, action), f"Request {action}d.", "/automation?tab=requests")


@app.post("/act/sab/{sid}/{action}")
async def act_sab(sid: int, action: str, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    form = await request.form()
    nzo = str(form.get("nzo") or "") or None
    client = await client_for(_svc(sid, "sabnzbd"))
    if action in ("pause", "resume"):
        coro, msg = client.set_paused(action == "pause", nzo), ("Paused." if action == "pause" else "Resumed.")
    elif action == "retry" and nzo:
        coro, msg = client.retry(nzo), "Retry started."
    else:
        raise HTTPException(400)
    return await _act(request, coro, msg, "/automation?tab=downloads")


@app.post("/act/qbit/{sid}/{action}")
async def act_qbit(sid: int, action: str, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    if action not in ("pause", "resume"):
        raise HTTPException(400)
    form = await request.form()
    hashes = str(form.get("hash") or "all")
    if hashes != "all" and not hashes.isalnum():
        raise HTTPException(400)
    client = await client_for(_svc(sid, "qbittorrent"))
    return await _act(request, client.set_paused(action == "pause", hashes),
                      "Paused." if action == "pause" else "Resumed.", "/automation?tab=downloads")


@app.post("/act/arr/{sid}/queue/{qid}/{action}")
async def act_arr_queue(sid: int, qid: int, action: str, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    client = await client_for(_svc(sid, "sonarr", "radarr", "lidarr"))
    if action == "retry":
        coro, msg = client.queue_retry(qid), "Removed and blocklisted — searching for another release."
    elif action == "remove":
        coro, msg = client.queue_remove(qid), "Removed from the queue."
    else:
        raise HTTPException(400)
    return await _act(request, coro, msg, "/automation?tab=queue")


# --- search & add ----------------------------------------------------------------------
@app.get("/add", response_class=HTMLResponse)
async def page_add(request: Request, q: str = "", kind: str = "tv", user: User = Depends(admin_user)):
    kind = "movie" if kind == "movie" else "tv"
    svcs = services(("sonarr",) if kind == "tv" else ("radarr",))
    svc = svcs[0] if svcs else None
    results, profiles, roots, error = [], [], [], None
    if svc and q.strip():
        client = await client_for(svc)
        (results, e1), (profiles, e2), (roots, e3) = await asyncio.gather(
            safe(client.lookup(q.strip())), safe(client.profiles()), safe(client.root_folders()))
        error = e1 or e2 or e3
    flash = request.session.pop("flash", None)
    return render(request, "add.html", user, q=q, kind=kind, svc=svc, results=results or [], profiles=profiles or [],
                  roots=roots or [], error=error, flash=flash,
                  have_tv=bool(services(("sonarr",))), have_movie=bool(services(("radarr",))))


@app.post("/add/{sid}")
async def do_add(sid: int, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    form = await request.form()
    svc = _svc(sid, "sonarr", "radarr")
    client = await client_for(svc)
    ext = str(form.get("ext_id") or "")
    if not ext.isdigit():
        raise HTTPException(400)
    term = f"tvdb:{ext}" if svc.kind == "sonarr" else f"tmdb:{ext}"
    found, err = await safe(client.lookup(term))
    q = str(form.get("q") or "")
    kind = "tv" if svc.kind == "sonarr" else "movie"
    if err or not found:
        request.session["flash"] = {"ok": False, "msg": err or "Couldn't find that title any more."}
        return RedirectResponse(f"/add?kind={kind}&q={q}", status_code=303)
    try:
        profile_id = int(str(form.get("profile")))
    except ValueError:
        raise HTTPException(400)
    root = str(form.get("root") or "")
    monitor = str(form.get("monitor") or "all")
    if monitor not in ("all", "future", "missing", "existing", "firstSeason", "latestSeason", "pilot", "none"):
        monitor = "all"
    added, err = await safe(client.add(found[0], profile_id, root, monitor, form.get("search") == "on"))
    title = found[0].get("title")
    request.session["flash"] = {"ok": not err, "msg": err or f"Added {title} to {svc.name}"
                                + (" and started searching." if form.get("search") == "on" else ".")}
    if not err:
        log.info("%s added %s to %s", user.name, title, svc.name)
    return RedirectResponse(f"/add?kind={kind}&q={q}", status_code=303)


# --- user pages -------------------------------------------------------------------------
@app.get("/users/{name}", response_class=HTMLResponse)
async def page_user(name: str, request: Request, user: User = Depends(staff_user)):
    def build():
        with SessionLocal() as db:
            return stats.user_profile(db, name, TZ)
    prof = await ttl_cached(("uprof", name), 60, lambda: asyncio.to_thread(build), stale=900)
    if not prof:
        raise HTTPException(404, "No plays recorded for that user")
    geo = await geoip.lookup([d.get("ip") for d in prof["devices"][:8]], wait=False) if user.is_admin else {}
    return render(request, "user.html", user, u=prof, geo=geo)


# --- viewer (Jellyfin user) dashboard ----------------------------------------------------
def _norm(uid: str | None) -> str:
    return (uid or "").replace("-", "").lower()


def _mine(user: User):
    """SQL filter for the signed-in Jellyfin user's own plays."""
    conds = [Play.user_name == user.name]
    if user.jf_id:
        conds.append(func.replace(func.lower(Play.user_id), "-", "") == user.jf_id)
    return or_(*conds)


async def _my_streams(user: User, with_others: bool = False) -> tuple[list[dict], int, list[dict]]:
    streams, _ = await current_activity()
    is_mine = lambda x: _norm(x.get("user_id")) == user.jf_id or x.get("user_name") == user.name  # noqa: E731
    mine = [{**x, "ip": None} for x in streams if is_mine(x)]   # copies (shared cache); never send IPs to viewers
    rest = [x for x in streams if not is_mine(x)]
    others = await _anon_streams(user, rest) if (with_others and rest and login_settings()["others_titles"]) else []
    return mine, len(rest), others


ANON_FIELDS = ("server_id", "item_id", "poster_id", "item_type", "title", "series_name", "season", "episode", "year",
               "display", "progress", "is_paused", "ends_at", "resolution")


async def _anon_streams(user: User, rest: list[dict]) -> list[dict]:
    """What other people are watching, with nothing about *who*: no name, device, app, IP or location.
    Only titles this person's own Jellyfin account could open are shown; the rest are just counted."""
    rest = [x for x in rest if x.get("server_id") == user.server_id and x.get("item_id")]
    if not rest:
        return []
    svc = next((x for x in services(("jellyfin",)) if x.id == user.server_id), None)
    if not svc:
        return []
    client = await client_for(svc)
    ids = list(dict.fromkeys(x["item_id"] for x in rest))[:30]
    async def check():
        data = await client.get("/Items", params={"userId": user.jf_id, "Ids": ",".join(ids), "Fields": "", "EnableImages": "false"})
        return {i.get("Id") for i in ((data or {}).get("Items") or [])}
    # what someone may open rarely changes, so remember it for a few minutes per set of titles
    visible, err = await safe(ttl_cached(("anonvis", user.jf_id, tuple(sorted(ids))), 300, check, stale=1800))
    visible = visible or set()
    # the kind of device (e.g. "Android TV", "Chrome") is shown, never its name, the person or where they are
    out = [{**{k: x.get(k) for k in ANON_FIELDS},
            "platform": {k: v for k, v in _platform(x.get("client"), x.get("device")).items() if k in ("label", "colour", "src", "mono")}}
           for x in rest if x["item_id"] in visible]
    out.sort(key=lambda x: (x["is_paused"] is True, -(x["progress"] or 0)))
    return out[:12]


async def _upcoming(days: int = 14) -> list[dict]:
    return [dict(i) for i in await ttl_cached(("upcoming", days), 300, lambda: _upcoming_fetch(days), stale=3600)]


async def _keep_warm() -> None:
    """Keep the slow, shared pages (Libraries, Discover rows, Coming up) ready in the background,
    so nobody waits on Jellyfin's drive scan, Sonarr/Radarr or Seerr. Each step stands on its own."""
    async def step(name, coro, timeout):
        try:
            await asyncio.wait_for(coro, timeout=timeout)
        except Exception:  # noqa: BLE001
            log.debug("Warming %s failed", name, exc_info=True)

    async def upcoming():
        for days in (14, 90):
            _TTL[("upcoming", days)] = (time.monotonic() + 330, await _upcoming_fetch(days))
        # and the detail pages for the next two weeks, so tapping a card is instant
        sem = asyncio.Semaphore(4)
        svcs = {s.id: s for s in services(("sonarr", "radarr"))}

        async def one(i):
            svc = svcs.get(i.get("sid"))
            if svc and (svc.kind == "sonarr") == (str(i.get("ref", ""))[:1] == "e"):
                async with sem:
                    try:
                        await upcoming_arr(svc, svc.id, i["ref"])
                    except Exception:  # noqa: BLE001
                        pass
        await asyncio.gather(*(one(i) for i in (await _upcoming(14))[:60]))

    async def discover_rows():
        for svc in services(("jellyseerr",))[:1]:
            c = await client_for(svc)
            rows = await seerr_portal.discover(c, svc.id)
            for gk in ("movie", "tv"):
                try:
                    await seerr_portal.genres(c, svc.id, gk)
                except Exception:  # noqa: BLE001
                    pass
            # the title pages people are most likely to tap next
            sem = asyncio.Semaphore(4)

            async def one(card):
                async with sem:
                    try:
                        await asyncio.gather(seerr_portal.raw_title(c, svc.id, card["type"], card["id"]),
                                             seerr_portal.similar(c, svc.id, card["type"], card["id"]))
                    except Exception:  # noqa: BLE001
                        pass
            await asyncio.gather(*(one(x) for r in rows[:3] for x in r["items"][:8]))

            async def company(kind, cid):
                async with sem:
                    try:
                        await seerr_portal.browse_company(c, svc.id, kind, cid, 1)
                    except Exception:  # noqa: BLE001
                        pass
            await asyncio.gather(*(company(k, t["id"]) for k, ts in seerr_portal.TILES.items() for t in ts))

            async def genre_page(gk, gid):
                async with sem:
                    try:
                        await seerr_portal.browse_genre(c, svc.id, gk, gid, 1)
                    except Exception:  # noqa: BLE001
                        pass
            gls = {gk: await seerr_portal.genres(c, svc.id, gk) for gk in ("movie", "tv")}
            await asyncio.gather(*(genre_page(gk, g["id"]) for gk, gl in gls.items() for g in gl))

    await asyncio.sleep(5)
    while True:
        await step("play lengths", _backfill_runtimes(), 120)
        await step("libraries", ttl_cached(("media",), 120, _media_servers, stale=6 * 3600), 60)
        await step("storage", ttl_cached(("storage",), 600, _storage_report, stale=6 * 3600), 120)
        await step("queue", asyncio.gather(*(_cached_panel(x, "queue") for x in services(("sonarr", "radarr", "lidarr")))), 60)
        if services(("sonarr", "radarr", "lidarr")):
            await step("calendar", _calendar_cached(None), 60)
        if services(("jellyseerr",)):
            await step("requests", asyncio.gather(*(_cached_panel(x, "requests") for x in services(("jellyseerr",)))), 60)
        if login_settings()["enabled"] and login_settings()["seerr_discover"]:
            await step("discover", discover_rows(), 60)
        if login_settings()["enabled"] and login_settings()["upcoming"]:
            await step("coming up", upcoming(), 60)
        await asyncio.sleep(240)


async def _upcoming_fetch(days: int) -> list[dict]:
    today = datetime.now(TZ).date()
    end = today + timedelta(days=days)
    out: list[dict] = []

    async def one(svc: Service):
        client = await client_for(svc)
        raw, err = await safe(client.calendar(today - timedelta(days=1), end + timedelta(days=1)))
        if not err:
            out.extend(calendar_view.items_for(svc, raw, TZ, today, end))

    await asyncio.gather(*(one(x) for x in services(("sonarr", "radarr"))))
    out = [i for i in out if today <= i["day"] <= end]
    out.sort(key=lambda i: (i["day"], i["time"] or "", i["title"]))
    for i in out:
        i["link"] = None    # don't expose *arr admin URLs to viewers
        if i.get("poster"):
            i["poster"] = "/me" + i["poster"]
    return out[:600]


@app.get("/me/img/{server_id}/{item_id}")
async def me_image(server_id: int, item_id: str, h: int = 300, t: str = "Primary", user: User = Depends(current_user)):
    return await image(server_id, item_id, h, t, user)


@app.get("/me/arrimg/{sid}/{item_id}")
async def me_arr_image(sid: int, item_id: int, t: str = "poster", user: User = Depends(current_user)):
    return await arr_image(sid, item_id, t, user)


@app.get("/me/manifest.webmanifest")
async def me_manifest():
    return JSONResponse({
        "name": "JellyCreep", "short_name": "JellyCreep", "start_url": "/me", "scope": "/", "display": "standalone",
        "background_color": "#0e0828", "theme_color": "#0a0a10",
        "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                  {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                  {"src": "/static/icon-maskable-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
                  {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
    }, media_type="application/manifest+json")


@app.get("/logout")
async def logout_alias(request: Request):
    return await logout(request)


@app.get("/me", response_class=HTMLResponse)
async def page_me(request: Request, user: User = Depends(current_user)):
    if not user.is_viewer:
        return RedirectResponse("/", status_code=303)
    cfg = login_settings()
    streams, others, anon = await _my_streams(user, with_others=True)
    with SessionLocal() as db:
        last = db.scalar(select(Play).where(_mine(user)).order_by(Play.stopped_at.desc()))
        recent = list(db.scalars(select(Play).where(_mine(user)).order_by(Play.stopped_at.desc()).limit(10)))
        tips = explain.summary(db.scalars(select(Play).where(_mine(user), Play.started_at >= utcnow() - timedelta(days=30))))
    prof = await _profile(user)
    warm_discover(user)          # get Discover ready in the background while they look at Home
    svc = next((x for x in services(("jellyfin",)) if x.id == user.server_id), None)
    # The rows that need Sonarr/Radarr/Seerr/Jellyfin library calls load after the page (see /partials/me/…),
    # unless they're already cached — then they're drawn straight away.
    sections = {k: (ttl_peek(_section_key(user, k)) if (flag is None or cfg[flag]) else []) for k, flag in
                (("upcoming", "upcoming"), ("added", "recent"), ("requests", "requests"), ("resume", None), ("nextup", None))}
    return render(request, "me.html", user, streams=streams, others=others if cfg["server_activity"] else None, anon=anon,
                  last=last, recent=recent, u=prof, sections=sections, everyone=cfg["seerr_everyone"],
                  server_ok=bool(svc and svc.last_ok is not False), server_name=svc.name if svc else "Jellyfin",
                  tips=tips)


async def _profile(user: User):
    def build():
        with SessionLocal() as db:
            return stats.user_profile(db, user.name, TZ)
    return await ttl_cached(("profile", user.jf_id), 60, lambda: asyncio.to_thread(build))


def _section_key(user: User, name: str) -> tuple:
    return {"upcoming": ("upcoming", 14), "added": ("added", user.server_id, user.jf_id),
            "requests": ("homereq", user.server_id, user.jf_id),
            "resume": ("resume", user.server_id, user.jf_id), "nextup": ("nextup", user.server_id, user.jf_id)}[name]


_JF_FIELDS = "UserData,RunTimeTicks,ProductionYear,ParentIndexNumber,IndexNumber,SeriesId,SeriesName,PrimaryImageAspectRatio"


async def _jf_watch_rows(user: User, name: str) -> list[dict]:
    """Jellyfin's own Continue Watching / Next Up for this person (their account, their libraries)."""
    svc = next((x for x in services(("jellyfin",)) if x.id == user.server_id), None)
    if not svc or not user.jf_id:
        return []
    c = await client_for(svc)
    params = {"userId": user.jf_id, "Limit": "16", "Fields": _JF_FIELDS, "EnableImageTypes": "Primary,Backdrop,Thumb",
              "ImageTypeLimit": "1"}
    if name == "resume":
        params["MediaTypes"] = "Video"
        try:
            data = await c.get("/UserItems/Resume", params=params)
        except UpstreamError:                  # Jellyfin before 10.9
            params.pop("userId")
            data = await c.get(f"/Users/{user.jf_id}/Items/Resume", params=params)
    else:
        params.update(EnableResumable="false", EnableRewatching="false")   # resumable ones are in Continue Watching
        data = await c.get("/Shows/NextUp", params=params)
    out = []
    for i in (data or {}).get("Items") or []:
        ud = i.get("UserData") or {}
        ep = i.get("Type") == "Episode"
        pct = ud.get("PlayedPercentage")
        rt = i.get("RunTimeTicks") or 0
        left = (rt - (ud.get("PlaybackPositionTicks") or 0)) / 10_000_000 if rt and name == "resume" else None
        out.append({"id": i.get("Id"), "type": i.get("Type"), "title": (i.get("SeriesName") if ep else i.get("Name")) or "?",
                    "sub": i.get("Name") if ep else (str(i.get("ProductionYear")) if i.get("ProductionYear") else "Movie"),
                    "code": f"S{i.get('ParentIndexNumber') or 0} • E{i.get('IndexNumber') or 0}" if ep else None,
                    "poster": i.get("SeriesId") if ep else i.get("Id"), "pct": round(pct) if pct else None,
                    "left": fmt_duration(left) if left and left > 60 else None})
    return out


def _warm_home(user: User) -> None:
    """Fetch Continue Watching / Next Up in the background (at sign-in) so Home draws them instantly."""
    async def go():
        for n in ("resume", "nextup"):
            try:
                await asyncio.wait_for(_section_data(user, n), timeout=15)
            except Exception:  # noqa: BLE001
                pass
    asyncio.get_running_loop().create_task(go())


async def _section_data(user: User, name: str):
    if name in ("resume", "nextup"):
        return await ttl_cached(_section_key(user, name), 30, lambda: _jf_watch_rows(user, name), stale=900)
    if name == "upcoming":
        return await _upcoming(14)
    if name == "added":
        svc = next((x for x in services(("jellyfin",)) if x.id == user.server_id), None)
        if not svc:
            return []

        async def fetch():
            items, _ = await safe((await client_for(svc)).recently_added(16, user.jf_id))
            return items or []
        return await ttl_cached(_section_key(user, "added"), 120, fetch, stale=1800)
    return await ttl_cached(_section_key(user, "requests"), 60, lambda: _home_requests(user), stale=1800)


SECTION_FLAGS = {"upcoming": "upcoming", "added": "recent", "requests": "requests", "resume": None, "nextup": None}


@app.get("/partials/me/{name}", response_class=HTMLResponse)
async def partial_me_section(request: Request, name: str, user: User = Depends(current_user)):
    if not user.is_viewer or name not in SECTION_FLAGS:
        raise HTTPException(404)
    cfg = login_settings()
    data = []
    if SECTION_FLAGS[name] is None or cfg[SECTION_FLAGS[name]]:
        try:   # a slow or unreachable app must never hold up the page
            data = await asyncio.wait_for(_section_data(user, name), timeout=8)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            log.warning("Home section %s timed out or failed", name)
            data = []
    svc = next((x for x in services(("jellyfin",)) if x.id == user.server_id), None)
    return render(request, f"_me_{name}.html", user, items=data or [], everyone=cfg["seerr_everyone"],
                  server_name=svc.name if svc else "Jellyfin")


@app.get("/partials/me", response_class=HTMLResponse)
async def partial_me(request: Request, user: User = Depends(current_user)):
    if not user.is_viewer:
        raise HTTPException(404)
    streams, others, anon = await _my_streams(user, with_others=True)
    with SessionLocal() as db:
        last = db.scalar(select(Play).where(_mine(user)).order_by(Play.stopped_at.desc()))
    return render(request, "_hero.html", user, streams=streams, last=last, anon=anon,
                  others=others if login_settings()["server_activity"] else None)


@app.get("/me/history", response_class=HTMLResponse)
async def page_my_history(request: Request, page: int = 1, q: str | None = None, type: str = "all", when: str = "all",
                          status: str = "all", device: str = "", conv: str = "", group: str = "1",
                          user: User = Depends(current_user)):
    if not user.is_viewer:
        return RedirectResponse("/history", status_code=303)
    per = 40
    page = max(1, min(page, 500))
    q = (q or "").strip()[:100]
    type = type if type in HIST_TYPES else "all"
    when = when if when in HIST_WHEN else "all"
    status = status if status in HIST_STATUS else "all"
    grouped = group != "0"
    with SessionLocal() as db:
        devices = sorted({c for c in db.scalars(select(Play.client).where(_mine(user)).distinct()) if c})
        device = device if device in devices else ""
        stmt = select(Play).where(_mine(user))
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Play.title.ilike(like), Play.series_name.ilike(like)))
        if type != "all":
            stmt = stmt.where(Play.item_type == HIST_TYPES[type][1])
        if when != "all":
            stmt = stmt.where(Play.started_at >= utcnow() - timedelta(days=int(when)))
        if status == "finished":
            stmt = stmt.where(Play.completed.is_(True))
        elif status == "partial":
            stmt = stmt.where(Play.completed.is_(False))
        if device:
            stmt = stmt.where(Play.client == device)
        if conv == "1":
            stmt = stmt.where(Play.play_method == "Transcode")
        sub = stmt.subquery()
        total, secs, done = db.execute(select(func.count(), func.coalesce(func.sum(sub.c.watched_seconds), 0),
                                              func.coalesce(func.sum(case((sub.c.completed.is_(True), 1), else_=0)), 0))).one()
        rows = list(db.scalars(stmt.order_by(Play.started_at.desc()).offset((page - 1) * per).limit(per)))
    days = _history_days(rows, grouped)
    params = {"q": q, "type": type, "when": when, "status": status, "device": device, "conv": conv, "group": "1" if grouped else "0"}
    active = [(k, v) for k, v in (("type", HIST_TYPES[type][0] if type != "all" else None),
                                  ("when", HIST_WHEN[when] if when != "all" else None),
                                  ("status", HIST_STATUS[status] if status != "all" else None),
                                  ("device", device or None), ("conv", "Converted only" if conv == "1" else None)) if v]
    def link(**kw):
        return "?" + urlencode({k: v for k, v in {**params, **kw}.items() if v not in ("", None)})
    return render(request, "me_history.html", user, days=days, total=total, secs=secs, done=done, page=page,
                  pages=max(1, math.ceil(total / per)), q=q, type=type, when=when, status=status, device=device, conv=conv,
                  grouped=grouped, devices=devices, active=active, link=link, types=HIST_TYPES, whens=HIST_WHEN,
                  statuses=HIST_STATUS)


HIST_TYPES = {"all": ("All", None), "tv": ("Shows", "Episode"), "movie": ("Movies", "Movie"), "music": ("Music", "Audio")}
HIST_WHEN = {"all": "All time", "7": "Last 7 days", "30": "Last 30 days", "90": "Last 3 months", "365": "Last year"}
HIST_STATUS = {"all": "Any", "finished": "Finished", "partial": "Not finished"}


def _history_days(rows: list[Play], grouped: bool) -> list[dict]:
    """Plays split by local day; back-to-back episodes of one show become a single "binge" entry."""
    today = datetime.now(TZ).date()
    out: list[dict] = []
    for p in rows:
        d = p.started_at.replace(tzinfo=timezone.utc).astimezone(TZ).date()
        if not out or out[-1]["date"] != d:
            gap = (today - d).days
            label = "Today" if gap == 0 else "Yesterday" if gap == 1 else (f"{d:%A}" if gap < 7 else f"{d:%A, %B} {d.day}" + (f", {d.year}" if d.year != today.year else ""))
            out.append({"date": d, "label": label, "entries": [], "secs": 0.0, "plays": 0})
        day = out[-1]
        day["secs"] += p.watched_seconds or 0
        day["plays"] += 1
        last = day["entries"][-1] if day["entries"] else None
        if (grouped and p.item_type == "Episode" and p.series_name and last
                and last["kind"] in ("tv", "binge") and last["series"] == p.series_name and last["who"] == p.user_name):
            if last["kind"] == "tv":
                last.update(kind="binge", plays=[last["play"]])
            last["plays"].append(p)
            last["secs"] += p.watched_seconds or 0
            continue
        kind = "tv" if p.item_type == "Episode" else ("music" if p.item_type == "Audio" else "movie")
        day["entries"].append({"kind": kind, "play": p, "series": p.series_name, "secs": p.watched_seconds or 0,
                               "who": p.user_name})
    for day in out:
        for e in day["entries"]:
            if e["kind"] == "binge":
                eps = [x for x in e["plays"] if x.season is not None]
                e["first"], e["last"] = (eps[-1], eps[0]) if eps else (None, None)   # rows are newest first
                e["play"] = e["plays"][0]
    return out


@app.get("/me/upcoming", response_class=HTMLResponse)
async def page_my_upcoming(request: Request, user: User = Depends(current_user)):
    if not user.is_viewer:
        return RedirectResponse("/automation", status_code=303)
    # how far ahead to look: chosen per person (remembered), from one shared 90-day fetch
    days = request.query_params.get("days")
    if days in UPCOMING_RANGES:
        request.session["up_days"] = days
    days = request.session.get("up_days") or "14"
    kind = request.query_params.get("kind") or "all"
    kind = kind if kind in ("all", "tv", "movie") else "all"
    view = request.query_params.get("view")
    if view in ("list", "calendar"):
        request.session["up_view"] = view
    view = request.session.get("up_view") or "list"
    up, cal = [], None
    if login_settings()["upcoming"]:
        allup = [i for i in await _upcoming(90) if kind == "all" or i["kind"] == kind]
        if view == "calendar":
            cal = _month_grid(allup, request.query_params.get("m"))
        else:
            end = datetime.now(TZ).date() + timedelta(days=int(days))
            up = [i for i in allup if i["day"] <= end]
    return render(request, "me_upcoming.html", user, upcoming=up, days=days, ranges=UPCOMING_RANGES, kind=kind,
                  view=view, cal=cal)


def _month_grid(items: list[dict], ym: str | None) -> dict:
    """A Mon–Sun month calendar of Coming Up items (this month and the next two)."""
    today = datetime.now(TZ).date()
    first_allowed = today.replace(day=1)
    months = [first_allowed]
    for _ in range(2):
        nxt = (months[-1] + timedelta(days=32)).replace(day=1)
        months.append(nxt)
    pick = first_allowed
    if ym:
        try:
            y, m = (int(x) for x in ym.split("-", 1))
            cand = first_allowed.replace(year=y, month=m)
            if cand in months:
                pick = cand
        except ValueError:
            pass
    nxt_month = (pick + timedelta(days=32)).replace(day=1)
    start = pick - timedelta(days=pick.weekday())
    end = nxt_month + timedelta(days=(6 - (nxt_month - timedelta(days=1)).weekday()) - 1)
    by_day: dict = {}
    for i in items:
        by_day.setdefault(i["day"], []).append(i)
    weeks, d = [], start
    while d <= end:
        week = []
        for _ in range(7):
            raw = by_day.get(d, [])
            week.append({"date": d, "items": _cal_groups(raw), "n": len(raw), "in_month": d.month == pick.month,
                         "today": d == today, "past": d < today, "weekend": d.weekday() >= 5})
            d += timedelta(days=1)
        weeks.append(week)
    idx = months.index(pick)
    return {"label": f"{pick:%B %Y}", "weeks": weeks,
            "prev": f"{months[idx - 1]:%Y-%m}" if idx > 0 else None,
            "next": f"{months[idx + 1]:%Y-%m}" if idx + 1 < len(months) else None,
            "count": sum(c["n"] for w in weeks for c in w if c["in_month"]),
            "eps": sum(1 for w in weeks for c in w if c["in_month"] for g in c["items"] for _ in g["all"] if g["kind"] == "tv"),
            "movies": sum(1 for w in weeks for c in w if c["in_month"] for g in c["items"] if g["kind"] == "movie"),
            "have": sum(1 for w in weeks for c in w if c["in_month"] for g in c["items"] for x in g["all"] if x.get("has_file"))}


def _cal_groups(items: list[dict]) -> list[dict]:
    """Several episodes of one show on one day (a season drop) become a single calendar entry."""
    out: list[dict] = []
    for i in items:
        last = next((g for g in out if g["kind"] == "tv" and i["kind"] == "tv" and g["title"] == i["title"]), None)
        if last:
            last["all"].append(i)
            continue
        out.append({**i, "all": [i]})
    for g in out:
        if len(g["all"]) > 1:
            codes = [x["sub"].split(" · ")[0] for x in g["all"]]
            m1, m2 = re.fullmatch(r"S(\d+)E(\d+)", codes[0]), re.fullmatch(r"S(\d+)E(\d+)", codes[-1])
            if m1 and m2 and m1.group(1) == m2.group(1):
                g["range"] = f"S{int(m1.group(1))} · E{int(m1.group(2))}–{int(m2.group(2))}"
            else:
                g["range"] = f"{codes[0]} – {codes[-1]}"
            g["has_file"] = all(x.get("has_file") for x in g["all"])
            g["premiere"] = any(x.get("premiere") for x in g["all"])
    return out


UPCOMING_RANGES = {"7": "1 week", "14": "2 weeks", "30": "1 month", "90": "3 months"}


# --- Phone / browser notifications (Web Push) — everyone -------------------------------
SW_JS = """// JellyCreep notifications
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || 'JellyCreep', {
    body: d.body || '', icon: '/static/icon-192.png', badge: '/static/badge-96.png',
    tag: d.tag || undefined, renotify: !!d.tag, timestamp: d.ts || Date.now(), data: { url: d.url || '/' },
  }));
});
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = new URL(e.notification.data && e.notification.data.url || '/', self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
    for (const w of wins) { if (w.url.startsWith(self.location.origin) && 'focus' in w) { w.navigate(url); return w.focus(); } }
    return self.clients.openWindow(url);
  }));
});
"""


def _sw_response() -> Response:
    return Response(SW_JS, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/sw.js")
async def service_worker_staff():
    return _sw_response()


@app.get("/me/sw.js")
async def service_worker_viewer():
    return _sw_response()


def _push_base(user: User) -> str:
    return "/me" if user.is_viewer else ""


def _check_push_path(request: Request, user: User) -> None:
    # viewers use /me/push/…, staff /push/… (so nginx can keep /push behind Authentik)
    if request.url.path.startswith("/me/") != user.is_viewer:
        raise HTTPException(404)


async def _notif_page(request: Request, user: User):
    key = webpush.user_key(user)
    devices = webpush.subs_for(key)
    ctx = {"vapid": webpush.public_key(), "prefs": webpush.prefs_for(key, user.is_viewer),
           "labels": webpush.VIEWER_PREFS if user.is_viewer else webpush.STAFF_PREFS, "devices": devices,
           "base": _push_base(user), "flash": request.session.pop("flash", None), "totals": None}
    if user.is_admin:
        with SessionLocal() as db:
            rows = db.execute(select(PushSub.role, func.count(func.distinct(PushSub.user_key))).group_by(PushSub.role)).all()
        ctx["totals"] = {"viewers": sum(n for r, n in rows if r == "viewer"), "staff": sum(n for r, n in rows if r != "viewer")}
    return render(request, "notifications.html", user, **ctx)


@app.get("/notifications", response_class=HTMLResponse)
async def page_notifications(request: Request, user: User = Depends(staff_user)):
    return await _notif_page(request, user)


@app.get("/me/notifications", response_class=HTMLResponse)
async def page_my_notifications(request: Request, user: User = Depends(current_user)):
    if not user.is_viewer:
        return RedirectResponse("/notifications", status_code=303)
    return await _notif_page(request, user)


async def _save_notif_prefs(request: Request, user: User):
    _check_push_path(request, user)
    await verify_csrf(request)
    form = await request.form()
    allowed = webpush.VIEWER_PREFS if user.is_viewer else webpush.STAFF_PREFS
    webpush.save_prefs(webpush.user_key(user), user.is_viewer, {k: form.get(k) == "on" for k in allowed})
    request.session["flash"] = {"ok": True, "msg": "Saved."}
    return RedirectResponse(f"{_push_base(user)}/notifications", status_code=303)


@app.post("/notifications")
async def save_notifications(request: Request, user: User = Depends(staff_user)):
    return await _save_notif_prefs(request, user)


@app.post("/me/notifications")
async def save_my_notifications(request: Request, user: User = Depends(current_user)):
    return await _save_notif_prefs(request, user)


async def _push_subscribe(request: Request, user: User):
    _check_push_path(request, user)
    await verify_csrf(request)
    body = await request.json()
    sub = body.get("subscription") or {}
    endpoint, keys = str(sub.get("endpoint") or ""), sub.get("keys") or {}
    if not webpush.valid_endpoint(endpoint) or not keys.get("p256dh") or not keys.get("auth") or len(endpoint) > 1000:
        raise HTTPException(400, "That doesn't look like a browser push subscription.")
    key = webpush.user_key(user)
    if not webpush.subs_for(key):       # first device → main alerts on by default
        webpush.enable_defaults(key, user.is_viewer)
    with SessionLocal() as db:
        row = db.scalar(select(PushSub).where(PushSub.endpoint == endpoint)) or PushSub(endpoint=endpoint)
        row.p256dh, row.auth = str(keys["p256dh"])[:200], str(keys["auth"])[:100]
        row.user_key, row.user_name, row.role = key, user.name, user.role
        row.jf_id, row.server_id = (user.jf_id, user.server_id) if user.is_viewer else (None, None)
        row.device, row.failures = str(body.get("device") or request.headers.get("user-agent", ""))[:200], 0
        db.add(row)
        db.commit()
        mine = list(db.scalars(select(PushSub).where(PushSub.user_key == key).order_by(PushSub.created_at.desc())))
        for old in mine[10:]:       # at most 10 devices per person
            db.delete(old)
        db.commit()
    sent = await webpush.deliver([r for r in webpush.subs_for(key) if r.endpoint == endpoint], "🔔 Notifications are on",
                                 "JellyCreep will let you know when something happens.", f"{_push_base(user)}/notifications", "welcome")
    return JSONResponse({"ok": True, "delivered": sent})


@app.post("/push/subscribe")
async def push_subscribe(request: Request, user: User = Depends(staff_user)):
    return await _push_subscribe(request, user)


@app.post("/me/push/subscribe")
async def my_push_subscribe(request: Request, user: User = Depends(current_user)):
    return await _push_subscribe(request, user)


async def _push_unsubscribe(request: Request, user: User):
    _check_push_path(request, user)
    await verify_csrf(request)
    body = await request.json()
    endpoint = str(body.get("endpoint") or "")
    rid = body.get("id")
    with SessionLocal() as db:
        q = select(PushSub).where(PushSub.user_key == webpush.user_key(user))
        q = q.where(PushSub.id == int(rid)) if str(rid or "").isdigit() else q.where(PushSub.endpoint == endpoint)
        for row in db.scalars(q):
            db.delete(row)
        db.commit()
    return JSONResponse({"ok": True})


@app.post("/push/unsubscribe")
async def push_unsubscribe(request: Request, user: User = Depends(staff_user)):
    return await _push_unsubscribe(request, user)


@app.post("/me/push/unsubscribe")
async def my_push_unsubscribe(request: Request, user: User = Depends(current_user)):
    return await _push_unsubscribe(request, user)


async def _push_test(request: Request, user: User):
    _check_push_path(request, user)
    await verify_csrf(request)
    n = await webpush.deliver(webpush.subs_for(webpush.user_key(user)), "🧪 Test notification",
                              f"Hi {user.name} — this is what JellyCreep notifications look like.",
                              f"{_push_base(user)}/notifications", "test")
    return JSONResponse({"ok": n > 0, "delivered": n})


@app.post("/push/test")
async def push_test(request: Request, user: User = Depends(staff_user)):
    return await _push_test(request, user)


@app.post("/me/push/test")
async def my_push_test(request: Request, user: User = Depends(current_user)):
    return await _push_test(request, user)


@app.post("/push/announce")
async def push_announce(request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    form = await request.form()
    title = str(form.get("title") or "📣 Announcement").strip()[:80]
    msg = str(form.get("message") or "").strip()[:500]
    audience = str(form.get("audience") or "everyone")
    if not msg:
        request.session["flash"] = {"ok": False, "msg": "Write a message first."}
    else:
        n = await webpush.announce(title, msg, audience if audience in ("everyone", "viewers", "staff") else "everyone")
        log.info("Announcement by %s to %s: %s devices", user.name, audience, n)
        request.session["flash"] = {"ok": True, "msg": f"Sent to {n} device{'s' if n != 1 else ''}."}
    return RedirectResponse("/notifications", status_code=303)


# --- Library: recently added, item / season / episode pages (everyone) -----------------
def _lib_base(user: User) -> str:
    return "/me/library" if user.is_viewer else "/library"


def _lib_server(user: User, sid: int | None) -> Service | None:
    jfs = services(("jellyfin",))
    if user.is_viewer:     # viewers only ever browse the server they signed in to
        svc = next((x for x in jfs if x.id == user.server_id), None)
        return None if (sid is not None and svc and sid != svc.id) else svc
    return next((x for x in jfs if x.id == sid), None) if sid else (jfs[0] if jfs else None)


def _day_groups(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """Recently added, split into Today / Yesterday / Monday … / Oct 28 (local time)."""
    today = datetime.now(TZ).date()
    out: list[tuple[str, list[dict]]] = []
    floor = datetime.min.replace(tzinfo=timezone.utc)
    for i in sorted(items, key=lambda x: x.get("added") or floor, reverse=True):
        a = i.get("added")
        d = a.astimezone(TZ).date() if a else None
        if d is None:
            label = "Earlier"
        elif d >= today:
            label = "Today"
        elif (today - d).days == 1:
            label = "Yesterday"
        elif (today - d).days < 7:
            label = f"{d:%A}"
        else:
            label = f"{d:%A, %B} {d.day}"
        if out and out[-1][0] == label:
            out[-1][1].append(i)
        else:
            out.append((label, [i]))
    return out


async def _recent_page(request: Request, user: User, type: str, server: int | None):
    svc = _lib_server(user, server)
    kind = type if type in library.KINDS else "all"
    q = (request.query_params.get("q") or "").strip()[:100]
    items, err = [], None
    if svc:
        client = await client_for(svc)
        uid, err = await safe(library.browse_as(client, svc.id, user))
        if uid and q:
            items, err = await safe(library.search(client, uid, q))
        elif uid:
            items, err = await safe(ttl_cached(("recent", svc.id, uid, kind), 60,
                                               lambda: library.recent_window(client, uid, kind), stale=1800))
    return render(request, "library_recent.html", user, items=items or [], groups=_day_groups(items or []) if not q else [],
                  err=err, kind=kind, kinds=library.KINDS, q=q,
                  svc=svc, servers=[] if user.is_viewer else services(("jellyfin",)), lib=_lib_base(user))


@app.get("/library", response_class=HTMLResponse)
async def page_library(request: Request, type: str = "all", server: int | None = None, user: User = Depends(staff_user)):
    return await _recent_page(request, user, type, server)


@app.get("/me/library", response_class=HTMLResponse)
async def page_my_library(request: Request, type: str = "all", user: User = Depends(current_user)):
    if not user.is_viewer:
        return RedirectResponse(f"/library?type={type}&q={request.query_params.get('q') or ''}", status_code=303)
    return await _recent_page(request, user, type, None)


def _item_history(d: dict, user: User, limit: int = 60) -> list[Play]:
    t = d["type"]
    q = select(Play)
    if t == "Series":
        q = q.where(Play.poster_id == d["id"])
    elif t == "Season":
        q = q.where(Play.poster_id == d["series_id"], Play.season == d["index"])
    else:
        q = q.where(Play.item_id == d["id"])
    if user.is_viewer:
        q = q.where(_mine(user))
    with SessionLocal() as db:
        return list(db.scalars(q.order_by(Play.started_at.desc()).limit(limit)))


async def _watch_ctx(client, svc: Service, uid: str, user: User, d: dict) -> dict | None:
    """'Watch on Jellyfin' link (title page in Jellyfin web) and the person's Jellyfin apps it can start on."""
    resume_ok = user.is_viewer           # staff browse as the admin account, whose resume points aren't theirs
    only = user.jf_id if user.is_viewer else None
    target, jf_sid, devs = await asyncio.gather(
        ttl_cached(("ptarget", svc.id, uid, d.get("id"), resume_ok), 20,
                   lambda: library.play_target(client, uid, d, resume_ok=resume_ok), stale=900),
        library.server_id(client, svc.id),
        ttl_cached(("devs", svc.id, uid, only), 10, lambda: library.devices(client, uid, only_user=only), stale=120)
        if (user.is_viewer or user.is_admin) else asyncio.sleep(0, []))
    if not target:
        return None
    return {**target, "sid": svc.id, "devices": devs, "internal": not svc.public_url,
            "url": f"{svc.link}/web/#/details?id={target['id']}" + (f"&serverId={jf_sid}" if jf_sid else ""),
            "apps": _app_links(target["id"], jf_sid)}


async def _item_page(request: Request, user: User, sid: int, item_id: str, tab: str | None):
    if not item_id.isalnum():
        raise HTTPException(404)
    svc = _lib_server(user, sid)
    if not svc or svc.id != sid:
        raise HTTPException(404)
    client = await client_for(svc)
    uid, err = await safe(library.browse_as(client, svc.id, user))
    paths = not user.is_viewer
    # details barely change, so show the last copy at once and refresh it in the background
    d, err = (await safe(ttl_cached(("libitem", svc.id, uid, item_id, paths), 30,
                                    lambda: library.item(client, uid, item_id, show_paths=paths), stale=1800))) if uid else (None, err)
    if err or not d or not d.get("id"):
        raise HTTPException(404, "That title isn't available, or your account can't access it.")
    tabs = [("details", "Details")]
    if d["type"] == "Series":
        tabs.append(("seasons", "Seasons"))
    if d["type"] == "Season":
        tabs.append(("episodes", "Episodes"))
    tabs.append(("history", "History"))
    keys = [k for k, _ in tabs]
    tab = tab if tab in keys else ("episodes" if d["type"] == "Season" else "details")
    ctx: dict[str, Any] = {"d": d, "tab": tab, "tabs": tabs, "sid": svc.id, "lib": _lib_base(user),
                           "rows": [], "history": [], "parent": None, "watch": None,
                           "flash": request.session.pop("flash", None)}
    async def rows():
        if tab == "seasons":
            return await safe(ttl_cached(("libseasons", svc.id, uid, d["id"]), 30,
                                         lambda: library.seasons(client, uid, d["id"]), stale=1800))
        if tab == "episodes":
            return await safe(ttl_cached(("libeps", svc.id, uid, d["id"]), 30,
                                         lambda: library.episodes(client, uid, d["series_id"], d["id"]), stale=1800))
        return [], None
    if tab == "history":
        ctx["history"] = _item_history(d, user)
    if d["type"] == "Episode" and d.get("season_id"):
        ctx["parent"] = (d["season_id"], f"Season {d['season_index']}" if d.get("season_index") is not None else "Season")
    elif d["type"] == "Season" and d.get("series_id"):
        ctx["parent"] = (d["series_id"], d["title"] or "Series")
    (ctx["rows"], ctx["err"]), ctx["watch"] = await asyncio.gather(rows(), _watch_ctx(client, svc, uid, user, d))
    ctx["rows"] = ctx["rows"] or []
    imgp = "/me/img" if user.is_viewer else "/img"
    if d.get("backdrop_id"):
        ctx["ambient"] = f"{imgp}/{svc.id}/{d['backdrop_id']}?t=Backdrop&h=720"
    return render(request, "library_item.html", user, **ctx)


async def _play_item(request: Request, user: User, sid: int, item_id: str):
    await verify_csrf(request)
    if not item_id.isalnum() or not (user.is_viewer or user.is_admin):
        raise HTTPException(404)
    svc = _lib_server(user, sid)
    if not svc or svc.id != sid:
        raise HTTPException(404)
    form = await request.form()
    session_id, back = str(form.get("session") or ""), str(form.get("back") or "")
    ok_back = ("/me/library/", "/me/upcoming/", "/me/discover/") if user.is_viewer else ("/library/", "/upcoming/")
    if not back.startswith(ok_back) or "//" in back:
        back = f"{_lib_base(user)}/{sid}/{item_id}"
    client = await client_for(svc)
    uid, err = await safe(library.browse_as(client, svc.id, user))
    # the person must be able to see the item, and the device must be one of theirs
    d, err = (await safe(library.item(client, uid, item_id, show_paths=False))) if uid else (None, err)
    devs = await library.devices(client, uid, only_user=user.jf_id if user.is_viewer else None) if uid else []
    dev = next((x for x in devs if x["id"] == session_id), None)
    if err or not d:
        request.session["flash"] = {"ok": False, "msg": "That title isn't available to your account."}
        return RedirectResponse(back, status_code=303)
    if not dev:
        request.session["flash"] = {"ok": False, "msg": "That device isn't available any more — open Jellyfin on it and try again."}
        return RedirectResponse(back, status_code=303)
    if not dev.get("can_play", True):
        request.session["flash"] = {"ok": False, "msg": f"{dev['client']} on {dev['name']} can't be started from another device. "
                                    "Open it there, or use the official Jellyfin app on that device."}
        return RedirectResponse(back, status_code=303)
    start = int(d.get("resume_ticks") or 0) if (form.get("resume") == "1" and user.is_viewer) else 0
    before = await library.now_playing(client, session_id)
    _, err = await safe(library.play_on(client, session_id, item_id, start))
    log.info("%s started %s on %s (%s)", user.name, item_id, dev["name"], dev["client"])
    if err:
        request.session["flash"] = {"ok": False, "msg": f"Couldn't start playback: {err}"}
        return RedirectResponse(back, status_code=303)
    # Jellyfin accepts the request even when the app ignores it, so check it really started
    started = False
    for _ in range(8):
        await asyncio.sleep(1)
        now = await library.now_playing(client, session_id)
        if now and (now == library._norm_id(item_id) or now != before):
            started = True
            break
    request.session["flash"] = ({"ok": True, "msg": f"▶ Playing on {dev['name']}."} if started else
                                {"ok": False, "msg": f"Sent to {dev['name']}, but {dev['client'] or 'the app'} hasn't started it. "
                                 "Some Jellyfin apps ignore requests from other devices — the official Jellyfin app does support it."})
    return RedirectResponse(back, status_code=303)


@app.post("/library/{sid}/{item_id}/play")
async def act_play(request: Request, sid: int, item_id: str, user: User = Depends(admin_user)):
    return await _play_item(request, user, sid, item_id)


@app.post("/me/library/{sid}/{item_id}/play")
async def act_my_play(request: Request, sid: int, item_id: str, user: User = Depends(current_user)):
    if not user.is_viewer:
        raise HTTPException(404)
    return await _play_item(request, user, sid, item_id)


async def _person_page(request: Request, user: User, sid: int, person_id: str):
    if not person_id.isalnum():
        raise HTTPException(404)
    svc = _lib_server(user, sid)
    if not svc or svc.id != sid:
        raise HTTPException(404)
    client = await client_for(svc)
    uid, err = await safe(library.browse_as(client, svc.id, user))
    p, err = (await safe(library.person(client, uid, person_id))) if uid else (None, err)
    if err or not p:
        raise HTTPException(404, "Couldn't find that person.")
    return render(request, "person.html", user, p=p, sid=svc.id, lib=_lib_base(user))


@app.get("/library/{sid}/person/{person_id}", response_class=HTMLResponse)
async def page_person(request: Request, sid: int, person_id: str, user: User = Depends(staff_user)):
    return await _person_page(request, user, sid, person_id)


@app.get("/me/library/{sid}/person/{person_id}", response_class=HTMLResponse)
async def page_my_person(request: Request, sid: int, person_id: str, user: User = Depends(current_user)):
    if not user.is_viewer:
        return RedirectResponse(f"/library/{sid}/person/{person_id}", status_code=303)
    return await _person_page(request, user, sid, person_id)


@app.get("/library/{sid}/{item_id}", response_class=HTMLResponse)
async def page_library_item(request: Request, sid: int, item_id: str, tab: str | None = None,
                            user: User = Depends(staff_user)):
    return await _item_page(request, user, sid, item_id, tab)


@app.get("/me/library/{sid}/{item_id}", response_class=HTMLResponse)
async def page_my_library_item(request: Request, sid: int, item_id: str, tab: str | None = None,
                               user: User = Depends(current_user)):
    if not user.is_viewer:
        return RedirectResponse(f"/library/{sid}/{item_id}" + (f"?tab={tab}" if tab else ""), status_code=303)
    return await _item_page(request, user, sid, item_id, tab)


# --- Seerr inside the Jellyfin-user portal (Discover / Requests) ----------------------
async def _seerr(user: User, need_discover: bool = True) -> dict | None:
    cfg = login_settings()
    svc = next(iter(services(("jellyseerr",))), None)
    if not svc or (need_discover and not cfg["seerr_discover"]):
        return None
    client = await client_for(svc)
    acct, err = await safe(seerr_portal.account(client, svc.id, user, cfg["seerr_autolink"]))
    p = seerr_portal.perms(acct)
    return {"svc": svc, "client": client, "acct": acct, "uid": (acct or {}).get("id"), "perms": p,
            "everyone": cfg["seerr_everyone"], "error": err, "manage": bool(acct) and p["manage"]}


async def _home_requests(user: User) -> list[dict]:
    sc = await _seerr(user, need_discover=False)
    if not sc or sc["error"]:
        return []
    rows, _ = (await safe(seerr_portal.feed(sc["client"], sc["svc"].id, sc["uid"], everyone=sc["everyone"], take=12)))[0] or ([], 0)
    return rows


async def _jf_lookup(user: User) -> dict | None:
    """What the viewer's own Jellyfin libraries hold, for the "Watch on Jellyfin" buttons in Discover."""
    svc = _lib_server(user, None)
    if not svc or not user.jf_id:
        return None
    client = await client_for(svc)
    (idx, err), jf_sid = await asyncio.gather(
        safe(ttl_cached(("jfidx", svc.id, user.jf_id), 300,
                        lambda: library.tmdb_index(client, svc.id, user.jf_id, ttl=0), stale=6 * 3600)),
        library.server_id(client, svc.id))
    if err or not idx:
        return None
    return {"svc": svc, "client": client, "idx": idx, "jf_sid": jf_sid}


MOONFIN_PKG = "org.moonfin.androidtv"
JELLYFIN_PKG = "org.jellyfin.mobile"


def _app_links(item_id: str, server_id: str | None) -> dict:
    """Links that open a title in the phone apps.

    Moonfin understands moonfin://item?id=…&serverId=… (and play?…), so it lands right on the title.
    The official Jellyfin app has no such link, so on Android we can only open the app itself."""
    from urllib.parse import quote
    q = f"id={item_id}" + (f"&serverId={server_id}" if server_id else "")
    store_m = quote(f"https://play.google.com/store/apps/details?id={MOONFIN_PKG}", safe="")
    store_j = quote(f"https://play.google.com/store/apps/details?id={JELLYFIN_PKG}", safe="")
    return {
        "moonfin": f"moonfin://item?{q}",
        "moonfin_play": f"moonfin://play?{q}",
        "moonfin_android": f"intent://item?{q}#Intent;scheme=moonfin;package={MOONFIN_PKG};S.browser_fallback_url={store_m};end",
        "moonfin_android_play": f"intent://play?{q}#Intent;scheme=moonfin;package={MOONFIN_PKG};S.browser_fallback_url={store_m};end",
        "jellyfin_android": f"intent://#Intent;package={JELLYFIN_PKG};action=android.intent.action.MAIN;"
                            f"category=android.intent.category.LAUNCHER;S.browser_fallback_url={store_j};end",
    }


def _jf_url(jf: dict, item_id: str) -> str:
    return f"{jf['svc'].link}/web/#/details?id={item_id}" + (f"&serverId={jf['jf_sid']}" if jf["jf_sid"] else "")


def _mark_watchable(jf: dict | None, cards: list[dict] | None, tmdb_key: str = "id") -> list[dict]:
    """Copies of the Discover cards, each one that's in the viewer's libraries carrying a ``jf`` link.

    Copies, because Seerr rows are cached and shared between viewers with different libraries."""
    out = []
    for c in cards or []:
        iid = library.in_library(jf["idx"], c.get("type"), c.get(tmdb_key), c.get("jf_hint")) if jf else None
        out.append({**c, "jf": {"id": iid, "url": _jf_url(jf, iid), "page": f"/me/library/{jf['svc'].id}/{iid}"}} if iid else c)
    return out


def _viewer_page(user: User, fallback: str = "/automation?tab=requests"):
    return None if user.is_viewer else RedirectResponse(fallback, status_code=303)


async def _discover_data(user: User, sc: dict):
    """Everything the Discover front page needs, fetched at the same time (and mostly cached)."""
    c, sid = sc["client"], sc["svc"].id
    want_added = login_settings()["recent"]

    async def added():
        try:
            return await asyncio.wait_for(_section_data(user, "added"), timeout=6) if want_added else []
        except Exception:  # noqa: BLE001
            return []
    return await asyncio.gather(
        safe(seerr_portal.discover(c, sid)),
        safe(ttl_cached(("discfeed", sid, sc["uid"], sc["everyone"]), 30,
                        lambda: seerr_portal.feed(c, sid, sc["uid"], everyone=sc["everyone"], take=16), stale=1800)),
        ttl_cached(("quota", sid, sc["uid"]), 60, lambda: seerr_portal.quota(c, sc["uid"]), stale=3600)
        if sc["uid"] else asyncio.sleep(0),
        added(), _jf_lookup(user))


_WARMING: set = set()


def warm_discover(user: User) -> None:
    """Prepare someone's Discover page in the background (called from Home), so opening it is instant."""
    if not user.is_viewer or user.jf_id in _WARMING or not login_settings()["seerr_discover"]:
        return
    _WARMING.add(user.jf_id)

    async def go():
        try:
            sc = await _seerr(user)
            if sc:
                jobs = [_discover_data(user, sc)]
                if login_settings()["requests"]:
                    jobs.append(_req_feed(sc, sc["everyone"]))
                await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), timeout=60)
        except Exception:  # noqa: BLE001
            pass
        finally:
            _WARMING.discard(user.jf_id)
    asyncio.get_running_loop().create_task(go())


@app.get("/me/discover", response_class=HTMLResponse)
async def page_discover(request: Request, q: str = "", row: str = "", page: int = 1, network: int = 0, studio: int = 0,
                        genre: int = 0, kind: str = "movie", user: User = Depends(current_user)):
    if (r := _viewer_page(user)):
        return r
    sc = await _seerr(user)
    ctx: dict[str, Any] = {"seerr": sc, "q": q.strip()[:100], "row": row if row in seerr_portal.ROW_BY_KEY else "",
                           "page": max(1, min(page, 50)), "rows": [], "results": [], "recent": [], "error": None, "quota": None,
                           "tiles": seerr_portal.TILES}
    co = (("network", network) if network in seerr_portal.COMPANIES["network"] else
          ("studio", studio) if studio in seerr_portal.COMPANIES["studio"] else None)
    if sc:
        c, sid = sc["client"], sc["svc"].id
        if ctx["q"]:
            ctx["results"], ctx["error"] = await safe(seerr_portal.search(c, ctx["q"], ctx["page"]))
        elif ctx["row"]:
            ctx["results"], ctx["error"] = await safe(seerr_portal.browse(c, sid, ctx["row"], ctx["page"]))
            ctx["row_title"] = seerr_portal.ROW_BY_KEY[ctx["row"]][0]
        elif genre > 0:
            gk = "tv" if kind == "tv" else "movie"
            gl, _ = await safe(seerr_portal.genres(c, sid, gk))
            name = next((g["name"] for g in gl or [] if g["id"] == genre), None)
            if gl and not name:
                return RedirectResponse("/me/discover", status_code=303)
            ctx["results"], ctx["error"] = await safe(seerr_portal.browse_genre(c, sid, gk, genre, ctx["page"]))
            ctx.update(row_title=f"{name or 'Genre'} {'Movies' if gk == 'movie' else 'Series'}", row=f"kind={gk}&genre={genre}")
        elif co:
            kind, cid = co
            ctx["results"], ctx["error"] = await safe(seerr_portal.browse_company(c, sid, kind, cid, ctx["page"]))
            name, logo = seerr_portal.COMPANIES[kind][cid]
            ctx.update(row_title=name, co_logo=logo, row=f"{kind}={cid}")
        else:
            ((rows, e1), (recent, _), quota, jf_added, jf), (mg, _), (tg, _) = await asyncio.gather(
                _discover_data(user, sc), safe(seerr_portal.genres(c, sid, "movie")), safe(seerr_portal.genres(c, sid, "tv")))
            ctx["genres"] = {"movie": mg or [], "tv": tg or []}
            ctx.update(rows=rows or [], recent=(recent or ([], 0))[0], error=e1, quota=quota,
                       jf_added=jf_added, jf_sid=user.server_id)
        if ctx["q"] or ctx["row"]:
            jf = await _jf_lookup(user)
        ctx["results"] = _mark_watchable(jf, ctx["results"])
        ctx["recent"] = _mark_watchable(jf, ctx["recent"], "tmdb")
        ctx["rows"] = [{**r, "items": _mark_watchable(jf, r["items"])} for r in ctx["rows"]]
    return render(request, "me_discover.html", user, **ctx)


@app.get("/me/discover/person/{pid}", response_class=HTMLResponse)
async def page_seerr_person(request: Request, pid: int, user: User = Depends(current_user)):
    """An actor's page in Discover: bio + every movie and show on TMDB, with request / Jellyfin status."""
    if (r := _viewer_page(user)):
        return r
    sc = await _seerr(user)
    if not sc:
        return RedirectResponse("/me", status_code=303)
    (p, err), jf = await asyncio.gather(safe(seerr_portal.person(sc["client"], sc["svc"].id, pid)), _jf_lookup(user))
    if err or not p:
        raise HTTPException(404, "Couldn't find that person.")
    p = {**p, "movies": _mark_watchable(jf, p["movies"]), "shows": _mark_watchable(jf, p["shows"])}
    return render(request, "me_person.html", user, p=p, seerr=sc, ambient=None)


@app.get("/me/discover/{mt}/{tmdb}", response_class=HTMLResponse)
async def page_title(request: Request, mt: str, tmdb: int, user: User = Depends(current_user)):
    if (r := _viewer_page(user)):
        return r
    if mt not in ("movie", "tv"):
        raise HTTPException(404)
    sc = await _seerr(user)
    if not sc:
        return RedirectResponse("/me", status_code=303)
    c, sid = sc["client"], sc["svc"].id
    (t, err), similar, has4k, quota, jf = await asyncio.gather(
        safe(seerr_portal.detail(c, sid, mt, tmdb, sc["uid"], everyone=sc["everyone"], can_manage=sc["manage"])),
        seerr_portal.similar(c, sid, mt, tmdb), seerr_portal.has_4k(c, sid),
        ttl_cached(("quota", sid, sc["uid"]), 60, lambda: seerr_portal.quota(c, sc["uid"]), stale=3600)
        if sc["uid"] else asyncio.sleep(0), _jf_lookup(user))
    if err or not t:
        raise HTTPException(404, "Couldn't load that title from Seerr")
    similar = _mark_watchable(jf, similar)
    watch = await _title_watch(user, jf, t)
    return render(request, "me_title.html", user, seerr=sc, t=t, similar=similar, has4k=has4k, quota=quota,
                  watch=watch, sid=jf["svc"].id if jf else None, lib=_lib_base(user), ambient=t.get("backdrop"))


async def _title_watch(user: User, jf: dict | None, t: dict) -> dict | None:
    """The full "Watch on Jellyfin" bar for a Discover title that's in the viewer's libraries."""
    if not jf:
        return None
    iid = library.in_library(jf["idx"], t.get("type"), t.get("id"), t.get("jf_hint"))
    if not iid:
        return None
    client, uid = jf["client"], user.jf_id
    d, err = await safe(library.item(client, uid, iid, show_paths=False))
    if err or not d or not d.get("id"):
        return None
    target, devs = await asyncio.gather(library.play_target(client, uid, d, resume_ok=True),
                                        library.devices(client, uid, only_user=user.jf_id))
    if not target:
        return None
    return {**target, "url": _jf_url(jf, target["id"]), "apps": _app_links(target["id"], jf["jf_sid"]),
            "devices": devs, "internal": not jf["svc"].public_url,
            "page": f"/me/library/{jf['svc'].id}/{d['id']}"}


@app.post("/me/discover/{mt}/{tmdb}/request")
async def act_request(request: Request, mt: str, tmdb: int, user: User = Depends(current_user)):
    await verify_csrf(request)
    if not user.is_viewer or mt not in ("movie", "tv"):
        raise HTTPException(404)
    sc = await _seerr(user)
    back = RedirectResponse(f"/me/discover/{mt}/{tmdb}", status_code=303)
    if not sc or not sc["uid"]:
        request.session["flash"] = {"ok": False, "msg": "Your account isn't linked to Seerr yet — ask the admin."}
        return back
    form = await request.form()
    is4k = form.get("is4k") == "1"
    seasons = sorted({int(x) for x in form.getlist("season") if str(x).isdigit() and int(x) > 0})
    if mt == "tv" and not seasons:
        request.session["flash"] = {"ok": False, "msg": "Pick at least one season."}
        return back
    try:
        res = await seerr_portal.make_request(sc["client"], sc["uid"], mt, tmdb, seasons, is4k)
    except UpstreamError as e:
        request.session["flash"] = {"ok": False, "msg": seerr_portal.err_text(e)}
        return back
    if not isinstance(res, dict) or not res.get("id"):
        log.warning("Seerr request by %s for %s %s came back without a request: %r", user.name, mt, tmdb, res)
        request.session["flash"] = {"ok": False, "msg": "Seerr didn't confirm the request. Try again, or ask the admin to check Seerr."}
        return back
    seerr_portal.just_requested(sc["svc"].id, mt, tmdb, res)
    seerr_portal.forget("row")
    seerr_portal.forget("similar")
    seerr_portal.forget("title")
    seerr_portal.forget("genre"); seerr_portal.forget("co"); seerr_portal.forget("person")
    ttl_forget("homereq"); ttl_forget("discfeed"); ttl_forget("quota"); ttl_forget("reqfeed")
    log.info("Seerr request by %s: %s %s seasons=%s 4k=%s", user.name, mt, tmdb, seasons, is4k)
    auto = (res or {}).get("status") == 2
    request.session["flash"] = {"ok": True, "msg": "Requested — it was approved automatically and will start downloading."
                                if auto else "Requested! It'll start downloading once it's approved."}
    return back


async def _req_feed(sc: dict, everyone: bool, f: str = "all", page: int = 1, mt: str = "all", sort: str = "added",
                    desc: bool = True):
    c, sid = sc["client"], sc["svc"].id
    return await ttl_cached(("reqfeed", sid, sc["uid"], everyone, sc["manage"], f, page, mt, sort, desc), 20,
                            lambda: seerr_portal.feed(c, sid, sc["uid"], everyone=everyone, can_manage=sc["manage"],
                                                      flt=f, page=page, mtype=mt, sort=sort, desc=desc), stale=600)


@app.get("/me/requests", response_class=HTMLResponse)
async def page_requests(request: Request, view: str = "everyone", f: str = "all", page: int = 1, mt: str = "all",
                        sort: str = "added", dir: str = "desc", user: User = Depends(current_user)):
    if (r := _viewer_page(user)):
        return r
    sc = await _seerr(user, need_discover=False)
    if not sc or not login_settings()["requests"]:
        return RedirectResponse("/me", status_code=303)
    everyone = sc["everyone"] and view != "mine"
    page = max(1, min(page, 200))
    f = f if f in seerr_portal.FILTERS else "all"
    mt = mt if mt in seerr_portal.MEDIA_TYPES else "all"
    sort = sort if sort in seerr_portal.SORTS else "added"
    desc = dir != "asc"
    c, sid = sc["client"], sc["svc"].id
    (res, err), quota = await asyncio.gather(
        safe(_req_feed(sc, everyone, f, page, mt, sort, desc)),
        ttl_cached(("quota", sid, sc["uid"]), 60, lambda: seerr_portal.quota(c, sc["uid"]), stale=3600)
        if sc["uid"] else asyncio.sleep(0))
    rows, pages = res or ([], 1)
    view = "everyone" if everyone else "mine"
    qs = urlencode({"view": view, "f": f, "mt": mt, "sort": sort, "dir": "desc" if desc else "asc"})
    return render(request, "me_requests.html", user, seerr=sc, rows=rows, pages=pages, page=page, f=f, mt=mt,
                  sort=sort, desc=desc, view=view, error=err, quota=quota, qs=qs, filters=seerr_portal.FILTERS,
                  media_types=seerr_portal.MEDIA_TYPES, sorts=seerr_portal.SORTS)


@app.post("/me/requests/{req_id}/{action}")
async def act_my_request(request: Request, req_id: int, action: str, user: User = Depends(current_user)):
    await verify_csrf(request)
    if not user.is_viewer or action not in ("cancel", "approve", "decline"):
        raise HTTPException(404)
    form = await request.form()
    nxt = str(form.get("next") or "/me/requests")
    if not nxt.startswith("/me"):
        nxt = "/me/requests"
    sc = await _seerr(user, need_discover=False)
    if not sc or not sc["uid"] or (action != "cancel" and not sc["manage"]):
        raise HTTPException(403)
    try:
        if action == "cancel":
            await seerr_portal.cancel(sc["client"], sc["uid"], req_id)
        else:
            await seerr_portal.moderate(sc["client"], sc["uid"], req_id, action)
        seerr_portal.forget("row")
        seerr_portal.forget("title")
        ttl_forget("homereq"); ttl_forget("discfeed"); ttl_forget("quota"); ttl_forget("reqfeed")
        request.session["flash"] = {"ok": True, "msg": {"cancel": "Request cancelled.", "approve": "Approved.",
                                                        "decline": "Declined."}[action]}
    except UpstreamError as e:
        request.session["flash"] = {"ok": False, "msg": seerr_portal.err_text(e)}
    return RedirectResponse(nxt, status_code=303)


async def _upcoming_arr(svc: Service, sid: int, ref: str) -> dict:
    """Sonarr/Radarr side of a Coming up page — the same for everyone, so it's cached and warmed."""
    arr = await client_for(svc)
    rid = int(ref[1:])
    u: dict[str, Any] = {"kind": "tv" if ref[0] == "e" else "movie", "dates": [], "links": [], "facts": []}
    if True:
        if ref[0] == "e":
            ep = await arr.get(arr.p(f"episode/{rid}")) or {}
            show = ep.get("series") or await arr.get(arr.p(f"series/{ep.get('seriesId')}")) or {}
            se, en = int(ep.get("seasonNumber") or 0), int(ep.get("episodeNumber") or 0)
            when = calendar_view._parse(ep.get("airDateUtc"), TZ)
            u.update(title=show.get("title") or "Unknown series", year=show.get("year"),
                     code=f"S{se} • E{en}", name=ep.get("title") or "TBA", season=se, number=en,
                     overview=ep.get("overview"), about=show.get("overview"), has_file=bool(ep.get("hasFile")),
                     poster=f"{{A}}/{sid}/{show.get('id')}", fanart=f"{{A}}/{sid}/{show.get('id')}?t=fanart",
                     rating=(show.get("ratings") or {}).get("value") or None, critic=None,
                     premiere=en == 1, finale=ep.get("finaleType"))
            if when:
                u["dates"].append(("Airs", f"{when:%A, %B} {when.day} · {when:%-I:%M %p}"))
            runtime = ep.get("runtime") or show.get("runtime")
            u["facts"] = [("Network", show.get("network")), ("Status", (show.get("status") or "").capitalize() or None),
                          ("Genres", ", ".join((show.get("genres") or [])[:4]) or None),
                          ("Runtime", f"{runtime} minutes" if runtime else None), ("Rated", show.get("certification"))]
            ids = {"Tvdb": show.get("tvdbId"), "Tmdb": show.get("tmdbId"), "Imdb": show.get("imdbId")}
            if show.get("imdbId"):
                u["links"].append(("IMDb", f"https://www.imdb.com/title/{show['imdbId']}/"))
            if show.get("tvdbId"):
                u["links"].append(("TheTVDB", f"https://thetvdb.com/?tab=series&id={show['tvdbId']}"))
            jf_kind = "Series"
        else:
            mv = await arr.get(arr.p(f"movie/{rid}")) or {}
            r = mv.get("ratings") or {}
            u.update(title=mv.get("title") or "Unknown movie", year=mv.get("year"), code=None, name=None,
                     overview=mv.get("overview"), about=None, has_file=bool(mv.get("hasFile")),
                     poster=f"{{A}}/{sid}/{rid}", fanart=f"{{A}}/{sid}/{rid}?t=fanart",
                     rating=((r.get("imdb") or {}).get("value") or (r.get("tmdb") or {}).get("value")) or None,
                     critic=(r.get("rottenTomatoes") or {}).get("value"), premiere=False, finale=None)
            for field, label in (("inCinemas", "In cinemas"), ("digitalRelease", "Digital release"), ("physicalRelease", "Blu-ray / DVD")):
                if mv.get(field):
                    d0 = datetime.fromisoformat(mv[field][:10])
                    u["dates"].append((label, f"{d0:%A, %B} {d0.day}, {d0.year}"))
            u["facts"] = [("Studio", mv.get("studio")), ("Genres", ", ".join((mv.get("genres") or [])[:4]) or None),
                          ("Runtime", f"{mv['runtime']} minutes" if mv.get("runtime") else None), ("Rated", mv.get("certification"))]
            ids = {"Tmdb": mv.get("tmdbId"), "Imdb": mv.get("imdbId")}
            if mv.get("imdbId"):
                u["links"].append(("IMDb", f"https://www.imdb.com/title/{mv['imdbId']}/"))
            if mv.get("tmdbId"):
                u["links"].append(("TMDB", f"https://www.themoviedb.org/movie/{mv['tmdbId']}"))
            if mv.get("youTubeTrailerId"):
                u["links"].insert(0, ("▶ Trailer", f"https://www.youtube.com/watch?v={mv['youTubeTrailerId']}"))
            jf_kind = "Movie"
    return {"u": u, "ids": ids, "jf_kind": jf_kind}


async def upcoming_arr(svc: Service, sid: int, ref: str) -> dict:
    return await ttl_cached(("upitem", sid, ref), 900, lambda: _upcoming_arr(svc, sid, ref), stale=6 * 3600)


async def _upcoming_page(request: Request, user: User, sid: int, ref: str):
    if not (len(ref) > 1 and ref[0] in "em" and ref[1:].isdigit()):
        raise HTTPException(404)
    if user.is_viewer:
        # only things that are actually on this person's Coming up list
        if not login_settings()["upcoming"] or not any(i.get("sid") == sid and i.get("ref") == ref for i in await _upcoming(90)):
            raise HTTPException(404)
    with SessionLocal() as db:
        svc = db.get(Service, sid)
    if not svc or svc.kind not in ("sonarr", "radarr") or (svc.kind == "sonarr") != (ref[0] == "e"):
        raise HTTPException(404)
    arrimg = "/me/arrimg" if user.is_viewer else "/arrimg"
    try:
        got = await upcoming_arr(svc, sid, ref)
    except UpstreamError as e:
        raise HTTPException(404, f"Couldn't load that from {svc.name}: {e}")
    u = copy.deepcopy(got["u"])
    ids, jf_kind = got["ids"], got["jf_kind"]
    for k in ("poster", "fanart"):
        u[k] = u[k].replace("{A}", arrimg)
    u["facts"] = [(k, v) for k, v in u["facts"] if v]
    if u["rating"]:
        u["rating"] = round(float(u["rating"]), 1)
    u["fresh"] = u.get("critic") is not None and u["critic"] >= 60

    # Is it (or the show) in Jellyfin? Then offer Watch / Play on, the cast and a link to its library page.
    ctx: dict[str, Any] = {"u": u, "lib": _lib_base(user), "watch": None, "jf": None, "d": None, "on_jellyfin": False,
                           "flash": request.session.pop("flash", None)}
    jsvc = _lib_server(user, None)
    if jsvc:
        jc = await client_for(jsvc)
        uid, _ = await safe(library.browse_as(jc, jsvc.id, user))
        if uid:
            async def lookup():
                hit = await library.find(jc, uid, jf_kind, u["title"], u.get("year"), ids)
                if not hit:
                    return None
                target_id, on_jf = hit["Id"], True
                if ref[0] == "e":
                    ep_id = await library.find_episode(jc, uid, hit["Id"], u["season"], u["number"])
                    on_jf, target_id = bool(ep_id), ep_id or hit["Id"]
                d, _ = await safe(library.item(jc, uid, target_id, show_paths=False))
                if not (d and d.get("id")):
                    return {"hit": hit["Id"], "on": on_jf, "d": None, "show": None}
                show_d = d
                if ref[0] == "e" and d["type"] == "Episode":   # cast of the show, not just this episode's guests
                    sd, _ = await safe(library.item(jc, uid, hit["Id"], show_paths=False))
                    show_d = {**(sd or {}), "cast": (d.get("cast") or []) if not (sd or {}).get("cast") else
                              (sd["cast"] + [c for c in d.get("cast") or [] if c["id"] not in {x["id"] for x in sd["cast"]}])}
                return {"hit": hit["Id"], "on": on_jf, "d": d, "show": show_d}
            got_jf = await ttl_cached(("upjf", jsvc.id, uid, sid, ref), 120, lookup, stale=1800)
            if got_jf:
                ctx["on_jellyfin"] = got_jf["on"]
                if got_jf["d"]:
                    ctx.update(d=got_jf["show"], jf={"sid": jsvc.id, "id": got_jf["hit"]},
                               watch=await _watch_ctx(jc, jsvc, uid, user, got_jf["d"]))
    return render(request, "upcoming_item.html", user, ambient=u["fanart"], **ctx)


@app.get("/me/upcoming/{sid}/{ref}", response_class=HTMLResponse)
async def page_my_upcoming_item(request: Request, sid: int, ref: str, user: User = Depends(current_user)):
    if not user.is_viewer:
        return RedirectResponse(f"/upcoming/{sid}/{ref}", status_code=303)
    return await _upcoming_page(request, user, sid, ref)


@app.get("/upcoming/{sid}/{ref}", response_class=HTMLResponse)
async def page_upcoming_item(request: Request, sid: int, ref: str, user: User = Depends(staff_user)):
    return await _upcoming_page(request, user, sid, ref)


@app.post("/settings/signin")
async def save_signin(request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    form = await request.form()
    sid = str(form.get("server_id") or "")
    set_setting("jellyfin_login", {
        "enabled": form.get("enabled") == "on", "server_id": int(sid) if sid.isdigit() else None,
        "upcoming": form.get("upcoming") == "on", "recent": form.get("recent") == "on",
        "requests": form.get("requests") == "on", "server_activity": form.get("server_activity") == "on",
        "others_titles": form.get("others_titles") == "on",
        "seerr_discover": form.get("seerr_discover") == "on", "seerr_everyone": form.get("seerr_everyone") == "on",
        "seerr_autolink": form.get("seerr_autolink") == "on",
    })
    bust_login_cache()
    request.session["flash"] = {"ok": True, "msg": "Sign-in settings saved."}
    return RedirectResponse("/settings#signin", status_code=303)


# --- notifications & import ---------------------------------------------------------------
@app.post("/settings/notifications")
async def save_notifications(request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    form = await request.form()
    cur = notify.config()
    ch = cur.get("channels", {})

    def keep(old, field):
        v = str(form.get(field) or "").strip()
        if v == "clear":
            return ""
        return v or (old or "")

    ch["ntfy"] = {"server": str(form.get("ntfy_server") or "").strip(), "topic": str(form.get("ntfy_topic") or "").strip(),
                  "token": keep((ch.get("ntfy") or {}).get("token"), "ntfy_token")}
    ch["pushover"] = {"user": str(form.get("pushover_user") or "").strip(),
                      "token": keep((ch.get("pushover") or {}).get("token"), "pushover_token")}
    ch["discord"] = {"webhook": keep((ch.get("discord") or {}).get("webhook"), "discord_webhook")}
    ch["webhook"] = {"url": keep((ch.get("webhook") or {}).get("url"), "webhook_url")}
    triggers = {k: form.get(f"t_{k}") == "on" for k in notify.TRIGGERS}
    set_setting("notifications", {"channels": ch, "triggers": triggers})
    if form.get("send_test") == "1":
        errs = await notify.test(ch)
        request.session["flash"] = {"ok": not errs, "msg": "; ".join(errs) if errs else "Saved — test notification sent."}
    else:
        request.session["flash"] = {"ok": True, "msg": "Notification settings saved."}
    return RedirectResponse("/settings#notifications", status_code=303)


@app.post("/settings/import/jellystat/{sid}")
async def start_import(sid: int, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    form = await request.form()
    _svc(sid, "jellystat")
    if importer.STATUS.get("running"):
        request.session["flash"] = {"ok": False, "msg": "An import is already running."}
    else:
        asyncio.create_task(importer.run(sid, only_before_jellycreep=form.get("all") != "on"))
        request.session["flash"] = {"ok": True, "msg": "Import started — progress shows below."}
    return RedirectResponse("/settings#import", status_code=303)


@app.get("/settings/import/status")
async def import_status(user: User = Depends(admin_user)):
    return importer.STATUS


# --- settings ------------------------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request, edit: int | None = None, new: str | None = None,
                        user: User = Depends(admin_user)):
    with SessionLocal() as db:
        svcs = list(db.scalars(select(Service).order_by(Service.kind, Service.name)))
        tokens = list(db.scalars(select(ApiToken).order_by(ApiToken.created_at.desc())))
        editing = db.get(Service, edit) if edit else None
        counts = {"plays": db.scalar(select(func.count(Play.id))), "events": db.scalar(select(func.count(Event.id)))}
    new_token = request.session.pop("new_token", None)
    flash = request.session.pop("flash", None)
    return render(request, "settings.html", user, services=svcs, tokens=tokens, editing=editing,
                  new_kind=new if new in SERVICE_KINDS else None, new_token=new_token, counts=counts, cfg=settings,
                  flash=flash, notif=notify.config(), triggers=notify.TRIGGERS, import_status=importer.STATUS,
                  signin=login_settings(), jellyfins=[x for x in svcs if x.kind == "jellyfin"],
                  jellystats=[x for x in svcs if x.kind == "jellystat"])


def _apply_form(svc: Service, form) -> None:
    def val(k: str) -> str | None:
        v = form.get(k)
        return str(v).strip() if v is not None and str(v).strip() != "" else None

    svc.name = val("name") or SERVICE_KINDS.get(svc.kind, svc.kind)
    svc.url = (val("url") or "").rstrip("/")
    svc.public_url = (val("public_url") or "").rstrip("/") or None
    svc.enabled = form.get("enabled") == "on"
    svc.verify_tls = form.get("verify_tls") == "on"
    svc.timeout = int(val("timeout") or 15)
    svc.username = val("username")
    svc.proxy_auth = val("proxy_auth") if val("proxy_auth") in PROXY_AUTH_MODES else "none"
    svc.proxy_username = val("proxy_username")
    svc.authentik_url = (val("authentik_url") or "").rstrip("/") or None
    svc.authentik_client_id = val("authentik_client_id")
    # Secrets: blank means "keep current"; the literal word "clear" removes it.
    for field in ("api_key", "password", "proxy_password"):
        v = val(field)
        if v == "clear":
            setattr(svc, field, None)
        elif v:
            setattr(svc, field, v)
    headers_raw = form.get("extra_headers")
    if headers_raw is not None and str(headers_raw).strip() != "••••":
        headers = {}
        for line in str(headers_raw).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip():
                    headers[k.strip()] = v.strip()
        svc.extra_headers = headers
    svc.updated_at = utcnow()
    if not svc.url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL must start with http:// or https://")


@app.post("/settings/services")
async def save_service(request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    form = await request.form()
    with SessionLocal() as db:
        sid = form.get("id")
        if sid:
            svc = db.get(Service, int(sid))
            if not svc:
                raise HTTPException(404)
        else:
            kind = str(form.get("kind"))
            if kind not in SERVICE_KINDS:
                raise HTTPException(400, "Unknown service type")
            svc = Service(kind=kind)
            db.add(svc)
        _apply_form(svc, form)
        db.commit()
        sid = svc.id
    await pool.drop(sid)
    await test_service(sid)
    return RedirectResponse(f"/settings#svc-{sid}", status_code=303)


@app.post("/settings/services/{sid}/delete")
async def delete_service(sid: int, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    with SessionLocal() as db:
        svc = db.get(Service, sid)
        if svc:
            db.delete(svc)
            db.commit()
    await pool.drop(sid)
    return RedirectResponse("/settings", status_code=303)


async def test_service(sid: int) -> dict:
    with SessionLocal() as db:
        svc = db.get(Service, sid)
        if not svc:
            raise HTTPException(404)
        client = await client_for(svc)
        msg, err = await safe(client.test())
        svc.last_ok = err is None
        svc.last_status = (msg or err or "")[:500]
        svc.last_checked = utcnow()
        db.commit()
        return {"ok": err is None, "message": msg or err}


@app.post("/settings/services/{sid}/test")
async def test_service_route(sid: int, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    return JSONResponse(await test_service(sid))


@app.post("/settings/tokens")
async def create_token(request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    form = await request.form()
    name = str(form.get("name") or "").strip()[:100] or "token"
    token = "jc_" + secrets.token_urlsafe(32)
    with SessionLocal() as db:
        db.add(ApiToken(name=name, prefix=token[:10], token_hash=hash_token(token)))
        db.commit()
    request.session["new_token"] = {"name": name, "token": token}
    return RedirectResponse("/settings#tokens", status_code=303)


@app.post("/settings/tokens/{tid}/delete")
async def delete_token(tid: int, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    with SessionLocal() as db:
        row = db.get(ApiToken, tid)
        if row:
            db.delete(row)
            db.commit()
    return RedirectResponse("/settings#tokens", status_code=303)


# --- OIDC login ------------------------------------------------------------------------
@app.get("/auth/login")
async def login(request: Request, next: str = "/"):
    if settings.auth_mode != "oidc" or oauth is None:
        return RedirectResponse("/")
    request.session["next"] = next if next.startswith("/") and not next.startswith("//") else "/"
    redirect_uri = (settings.base_url or str(request.base_url).rstrip("/")) + "/auth/callback"
    return await oauth.authentik.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if oauth is None:
        raise HTTPException(404)
    token = await oauth.authentik.authorize_access_token(request)
    claims = token.get("userinfo") or await oauth.authentik.userinfo(token=token)
    request.session["user"] = user_from_claims(dict(claims))
    return RedirectResponse(request.session.pop("next", "/"), status_code=302)


@app.get("/auth/logout")
async def logout(request: Request):
    was_viewer = (request.session.get("user") or {}).get("via") == "jellyfin"
    request.session.clear()
    if was_viewer or (jellyfin_login_enabled() and not request.headers.get(settings.user_header)):
        return RedirectResponse("/login?bye=1", status_code=303)
    if settings.auth_mode == "authentik_forward":
        return RedirectResponse("/outpost.goauthentik.io/sign_out")
    return RedirectResponse("/login?bye=1", status_code=303)


# --- Jellyfin sign-in -------------------------------------------------------------------
def _safe_next(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") and not nxt.startswith("/login") else "/me"


def _login_page(request: Request, error: str | None = None, status: int = 200, username: str = "", nxt: str = "/me"):
    return templates.TemplateResponse(request, "login.html", {
        "request": request, "csrf": csrf_token(request), "error": error, "username": username, "next": nxt,
        "jf_enabled": jellyfin_login_enabled(), "oidc": settings.auth_mode == "oidc" and oauth is not None,
        "admin_url": settings.admin_login_url, "bye": request.query_params.get("bye"), "version": __version__,
    }, status_code=status)


def _login_server() -> Service | None:
    cfg = login_settings()
    jfs = services(("jellyfin",))
    return next((x for x in jfs if x.id == cfg.get("server_id")), jfs[0] if jfs else None)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/me"):
    from .auth import browser_user
    u = browser_user(request)
    if u:
        return RedirectResponse("/me" if u.is_viewer else "/", status_code=303)
    return _login_page(request, nxt=_safe_next(next))


@app.post("/login")
async def do_login(request: Request):
    await verify_csrf(request)
    form = await request.form()
    username = str(form.get("username") or "").strip()[:128]
    password = str(form.get("password") or "")[:512]
    nxt = _safe_next(str(form.get("next") or ""))
    if not jellyfin_login_enabled():
        return _login_page(request, "Jellyfin sign-in is turned off.", 403)
    ip = client_ip(request)
    if not username or not password:
        return _login_page(request, "Enter your Jellyfin username and password.", 400, username, nxt)
    if limiter.blocked(ip, username):
        log.warning("Sign-in rate limited for %r from %s", username, ip)
        return _login_page(request, "Too many attempts. Wait 15 minutes and try again.", 429, username, nxt)
    svc = _login_server()
    if not svc:
        return _login_page(request, "No Jellyfin server is connected yet.", 503, username, nxt)
    client = await client_for(svc)
    try:
        res = await client.authenticate(username, password)
    except UpstreamError:
        return _login_page(request, "Couldn't reach Jellyfin right now. Try again in a minute.", 503, username, nxt)
    if not res or not res.get("User"):
        limiter.fail(ip, username)
        log.info("Failed Jellyfin sign-in for %r from %s", username, ip)
        await asyncio.sleep(0.6)
        return _login_page(request, "Wrong username or password.", 401, username, nxt)
    ju = res["User"]
    if (ju.get("Policy") or {}).get("IsDisabled"):
        return _login_page(request, "This Jellyfin account is disabled.", 403, username, nxt)
    if res.get("AccessToken"):
        await client.end_session(res["AccessToken"])
    limiter.ok(ip, username)
    request.session.clear()
    request.session["user"] = {"via": "jellyfin", "name": ju.get("Name") or username,
                               "jf_id": str(ju.get("Id") or "").replace("-", "").lower(),
                               "server_id": svc.id, "checked": time.time()}
    log.info("Jellyfin user %s signed in from %s", ju.get("Name"), ip)
    su = request.session["user"]
    try:                                   # get their Discover / Requests ready while Home loads
        vu = User(name=su["name"], is_admin=False, via="jellyfin", role="viewer", jf_id=su["jf_id"], server_id=su["server_id"])
        warm_discover(vu)
        _warm_home(vu)
    except Exception:  # noqa: BLE001
        pass
    return RedirectResponse(nxt, status_code=303)


# --- API ------------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": __version__}


def _play_dict(p: Play) -> dict:
    d = {c.name: getattr(p, c.name) for c in Play.__table__.columns}
    for k in ("started_at", "stopped_at"):
        d[k] = d[k].replace(tzinfo=timezone.utc).isoformat() if d[k] else None
    d["display_title"] = p.display_title
    return d


@app.get("/api/v1/activity", tags=["data"])
async def api_activity(user: User = Depends(api_user)):
    streams, errors = await current_activity()
    for s in streams:
        if s.get("started"):
            s["started"] = s["started"].replace(tzinfo=timezone.utc).isoformat()
    return {"stream_count": len(streams), "streams": streams, "errors": errors}


@app.get("/api/v1/history", tags=["data"])
async def api_history(user_name: str | None = None, type: str | None = None, q: str | None = None,
                      method: str | None = None, limit: int = 50, offset: int = 0, user: User = Depends(api_user)):
    limit = min(max(limit, 1), 1000)
    with SessionLocal() as db:
        stmt = history_query(user_name, type, q, method)
        total = db.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = db.scalars(stmt.order_by(Play.started_at.desc()).offset(max(offset, 0)).limit(limit))
        return {"total": total, "items": [_play_dict(p) for p in rows]}


@app.get("/api/v1/users", tags=["data"])
async def api_users(user: User = Depends(api_user)):
    with SessionLocal() as db:
        rows = stats.users_table(db)
    for r in rows:
        for k in ("first", "last"):
            r[k] = r[k].replace(tzinfo=timezone.utc).isoformat() if r[k] else None
    return rows


@app.get("/api/v1/stats", tags=["data"])
async def api_stats(days: int = 30, user: User = Depends(api_user)):
    days = min(max(days, 1), 3650)
    with SessionLocal() as db:
        top_users = stats.top_users(db, days)
        for r in top_users:
            r["last"] = r["last"].replace(tzinfo=timezone.utc).isoformat() if r["last"] else None
        return {
            "days": days, "summary": stats.summary(db, days), "top_users": top_users,
            "top_movies": stats.top_movies(db, days), "top_shows": stats.top_shows(db, days),
            "plays_per_day": [{**d, "day": d["day"].isoformat(), "seconds": int(d["seconds"])} for d in stats.plays_per_day(db, days, TZ)],
            "plays_by_hour": stats.plays_by_hour(db, days),
            "play_methods": stats.grouped(db, Play.play_method, days), "clients": stats.grouped(db, Play.client, days),
        }


@app.get("/api/v1/events", tags=["data"])
async def api_events(source: str | None = None, limit: int = 100, user: User = Depends(api_user)):
    with SessionLocal() as db:
        stmt = select(Event).order_by(Event.created_at.desc()).limit(min(max(limit, 1), 1000))
        if source:
            stmt = stmt.where(Event.source == source)
        return [{"id": e.id, "source": e.source, "event_type": e.event_type, "title": e.title, "detail": e.detail,
                 "created_at": e.created_at.replace(tzinfo=timezone.utc).isoformat()} for e in db.scalars(stmt)]


@app.get("/api/v1/services", tags=["data"])
async def api_services(user: User = Depends(api_user)):
    return [{"id": s.id, "kind": s.kind, "name": s.name, "enabled": s.enabled, "ok": s.last_ok,
             "status": s.last_status, "checked": s.last_checked.isoformat() if s.last_checked else None}
            for s in services(enabled_only=False)]


@app.post("/api/v1/webhook/{source}", tags=["webhooks"])
async def api_webhook(source: str, request: Request, user: User = Depends(api_user)):
    source = source.lower()[:32]
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(400, "Expected a JSON body")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected a JSON object")
    etype, title, detail = webhooks.parse(source, payload)
    with SessionLocal() as db:
        ev = Event(source=source, event_type=str(etype)[:64], title=str(title)[:500], detail=detail,
                   payload=json.dumps(payload)[:20000])
        db.add(ev)
        db.commit()
        ev_id = ev.id
    et = str(etype)
    base = settings.base_url or ""
    if source in ("sonarr", "radarr", "lidarr") and et in ("DownloadFailed", "ManualInteractionRequired", "ImportFailed"):
        notify.send_later("download_failed", f"❌ {source.capitalize()}: download failed", f"{title}\n{detail or ''}".strip(),
                          url=f"{base}/automation?tab=queue", priority="high")
    elif source in ("sonarr", "radarr", "lidarr", "prowlarr") and et == "Health":
        notify.send_later("health", f"⚠️ {source.capitalize()} health", str(title), url=f"{base}/automation?tab=queue")
    elif source in ("jellyseerr", "seerr", "overseerr") and et == "MEDIA_PENDING":
        notify.send_later("new_request", "🙋 New request", f"{title}\n{detail or ''}".strip(),
                          url=f"{base}/automation?tab=requests")
    elif source == "jellyfin" and et in ("UserCreated", "UserAdded"):
        notify.send_later("new_user", "👋 New Jellyfin user", str(title), url=f"{base}/users")
    return {"ok": True, "id": ev_id}
