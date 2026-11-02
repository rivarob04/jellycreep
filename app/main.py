"""JellyCreep – Tautulli-style monitoring for Jellyfin, *arr-aware, built for Authentik."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, calendar_view, importer, notify, stats, storage, tracker, webhooks
from .auth import (CSRF_COOKIE, LoginRequired, User, admin_user, api_user, csrf_token, current_user, oauth,
                   user_from_claims, verify_csrf)
from .config import settings
from .db import (PROXY_AUTH_MODES, SERVICE_KINDS, ActiveStream, ApiToken, Event, Play, Service, SessionLocal,
                 hash_token, init_db, set_setting, utcnow)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_services_from_env()
    task = asyncio.create_task(tracker.run_forever())
    yield
    task.cancel()
    await pool.close()


app = FastAPI(title="JellyCreep", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None,
              openapi_url="/api/openapi.json")
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="jc_session",
                   same_site="lax", https_only=settings.cookie_secure, max_age=60 * 60 * 24 * 14)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    new = getattr(request.state, "new_csrf", None)
    if new:
        response.set_cookie(CSRF_COOKIE, new, httponly=False, samesite="strict", secure=settings.cookie_secure)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return response


@app.exception_handler(LoginRequired)
async def _login_redirect(request: Request, _exc: LoginRequired):
    return RedirectResponse(f"/auth/login?next={request.url.path}", status_code=302)


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


def fmt_local(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
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
templates.env.globals.update(version=__version__, kinds=SERVICE_KINDS, proxy_modes=PROXY_AUTH_MODES)


_AMBIENT: dict[str, Any] = {"at": 0.0, "url": None}


def ambient_url() -> str | None:
    """Backdrop for the glass background: what's playing now, else the last thing watched (cached 60 s)."""
    import time as _t
    if _t.time() - _AMBIENT["at"] < 60:
        return _AMBIENT["url"]
    url = None
    try:
        with SessionLocal() as db:
            act = db.scalar(select(ActiveStream).order_by(ActiveStream.started_at.desc()))
            if act:
                d = json.loads(act.data)
                if d.get("poster_id"):
                    url = f"/img/{act.server_id}/{d['poster_id']}?t=Backdrop&h=720"
            if not url:
                p = db.scalar(select(Play).where(Play.poster_id.is_not(None)).order_by(Play.stopped_at.desc()))
                if p:
                    url = f"/img/{p.server_id}/{p.poster_id}?t=Backdrop&h=720"
    except Exception:  # noqa: BLE001
        url = None
    _AMBIENT.update(at=_t.time(), url=url)
    return url


def render(request: Request, name: str, user: User, **ctx: Any) -> HTMLResponse:
    ctx.update(request=request, user=user, csrf=csrf_token(request), auth_mode=settings.auth_mode,
               ambient=ambient_url())
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
async def current_activity() -> tuple[list[dict], list[str]]:
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
            snap.update(
                server_id=svc.id, server_name=svc.name, session_id=sess.get("Id"),
                progress=round(pos / rt * 100, 1) if rt and pos is not None else 0,
                watched=t.watched_seconds if t else 0, started=t.started_at if t else None,
                display=Play(**{k: snap.get(k) for k in ("item_type", "title", "series_name", "season", "episode", "year")}).display_title,
            )
            streams.append(snap)
    return streams, errors


@app.get("/", response_class=HTMLResponse)
async def page_activity(request: Request, user: User = Depends(current_user)):
    streams, errors = await current_activity()
    with SessionLocal() as db:
        recent = list(db.scalars(select(Play).order_by(Play.stopped_at.desc()).limit(8)))
        today = stats.summary(db, 1)
    return render(request, "activity.html", user, streams=streams, errors=errors, recent=recent, today=today,
                  health=services(),
                  has_servers=bool(services(("jellyfin",), enabled_only=False)))


@app.get("/partials/activity", response_class=HTMLResponse)
async def partial_activity(request: Request, user: User = Depends(current_user)):
    streams, errors = await current_activity()
    return render(request, "_streams.html", user, streams=streams, errors=errors)


@app.post("/activity/{server_id}/{session_id}/stop")
async def stop_stream(server_id: int, session_id: str, request: Request, user: User = Depends(admin_user)):
    await verify_csrf(request)
    form = await request.form()
    with SessionLocal() as db:
        svc = db.get(Service, server_id)
    if not svc or svc.kind != "jellyfin":
        raise HTTPException(404)
    client = await client_for(svc)
    msg = str(form.get("message") or "").strip()
    if msg:
        await safe(client.message(session_id, "Stream stopped", msg))
    _, err = await safe(client.stop(session_id))
    if err:
        raise HTTPException(502, err)
    log.info("%s stopped session %s on %s", user.name, session_id, svc.name)
    return RedirectResponse("/", status_code=303)


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
                       method: str | None = None, page: int = 1, user: User = Depends(current_user)):
    per = 50
    page = max(1, page)
    with SessionLocal() as db:
        stmt = history_query(user_name, type, q, method)
        total = db.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = list(db.scalars(stmt.order_by(Play.started_at.desc()).offset((page - 1) * per).limit(per)))
        users = [u for (u,) in db.execute(select(Play.user_name).distinct().order_by(Play.user_name))]
    return render(request, "history.html", user, rows=rows, total=total, page=page, pages=max(1, math.ceil(total / per)),
                  users=users, f={"user_name": user_name or "", "type": type or "", "q": q or "", "method": method or ""})


@app.get("/users", response_class=HTMLResponse)
async def page_users(request: Request, user: User = Depends(current_user)):
    with SessionLocal() as db:
        rows = stats.users_table(db)
    return render(request, "users.html", user, rows=rows)


@app.get("/stats", response_class=HTMLResponse)
async def page_stats(request: Request, days: int = 30, user: User = Depends(current_user)):
    days = days if days in (7, 30, 90, 365) else 30
    with SessionLocal() as db:
        ctx = dict(
            days=days, summary=stats.summary(db, days), top_users=stats.top_users(db, days),
            top_movies=stats.top_movies(db, days), top_shows=stats.top_shows(db, days),
            per_day=stats.plays_per_day(db, days),
            methods=stats.grouped(db, Play.play_method, days), clients=stats.grouped(db, Play.client, days),
            types=stats.grouped(db, Play.item_type, days),
        )
        ctx["heat"], ctx["by_hour"], ctx["heat_peak"] = stats.heatmap(db, days, TZ)
        ctx["conc"] = stats.concurrency(db, days, TZ)
    jellystat = []
    for svc in services(("jellystat",)):
        client = await client_for(svc)
        users, err = await safe(client.most_active_users(days))
        jellystat.append({"svc": svc, "users": users, "error": err})
    return render(request, "stats.html", user, jellystat=jellystat, **ctx)


# --- media & automation ----------------------------------------------------------------
@app.get("/media", response_class=HTMLResponse)
async def page_media(request: Request, user: User = Depends(current_user)):
    servers = []
    for svc in services(("jellyfin",)):
        client = await client_for(svc)
        (recent, e1), (libs, e2), (counts, e3), (info, e4) = await asyncio.gather(
            safe(client.recently_added()), safe(client.libraries()), safe(client.counts()), safe(client.system_info()))
        servers.append({"svc": svc, "recent": recent or [], "libs": libs or [], "counts": counts or {}, "info": info or {},
                        "error": e1 or e2 or e3 or e4})
    drives, notes = await storage.collect()
    if drives:
        await asyncio.to_thread(storage.record, drives)
        await asyncio.to_thread(storage.forecast, drives)
    return render(request, "media.html", user, servers=servers, drives=drives, storage_notes=notes,
                  storage_sum=storage.summary(drives))


AUTOMATION_TABS = {
    "calendar": ("Calendar", ("sonarr", "radarr", "lidarr")),
    "queue": ("Queue", ("sonarr", "radarr", "lidarr")),
    "downloads": ("Downloads", ("sabnzbd", "qbittorrent")),
    "requests": ("Requests", ("jellyseerr",)),
    "indexers": ("Indexers", ("prowlarr",)),
}


async def _arr_panel(svc: Service, tab: str) -> dict:
    client = await client_for(svc)
    panel: dict[str, Any] = {"svc": svc}
    if tab == "queue" and svc.kind in ("sonarr", "radarr", "lidarr"):
        (panel["queue"], e1), (panel["history"], e2), (panel["health"], e3), (panel["disk"], e4) = \
            await asyncio.gather(safe(client.queue()), safe(client.history(15)), safe(client.health()),
                                 safe(client.diskspace()))
        panel["error"] = e1 or e2 or e3 or e4
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


@app.get("/automation", response_class=HTMLResponse)
async def page_automation(request: Request, tab: str = "calendar", m: str | None = None,
                          user: User = Depends(current_user)):
    available = {k: v for k, v in AUTOMATION_TABS.items() if services(v[1])}
    if tab not in available:
        tab = next(iter(available), "calendar")
    ctx: dict[str, Any] = {"tab": tab, "tabs": available, "panels": [], "cal": None, "errors": [],
                           "flash": request.session.pop("flash", None)}
    if tab == "calendar" and available:
        ctx["cal"], ctx["errors"] = await _calendar(m)
    elif available:
        ctx["panels"] = await asyncio.gather(*(_arr_panel(s, tab) for s in services(available[tab][1])))
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
async def page_events(request: Request, source: str | None = None, user: User = Depends(current_user)):
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


@app.get("/invites", response_class=HTMLResponse)
async def page_invites(request: Request, user: User = Depends(admin_user)):
    panels = []
    for svc in services(("wizarr",)):
        client = await client_for(svc)
        (status, e1), (servers, e2), (libs, e3), (invs, e4), (users, e5) = await asyncio.gather(
            safe(client.status()), safe(client.servers()), safe(client.libraries()), safe(client.invitations()),
            safe(client.users()))
        invs = sorted(invs or [], key=lambda i: i.get("created") or "", reverse=True)
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
        request.session["new_invite"] = {"url": inv.get("url"), "code": inv.get("code"), "server": inv.get("display_name")}
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
async def page_user(name: str, request: Request, user: User = Depends(current_user)):
    with SessionLocal() as db:
        prof = stats.user_profile(db, name, TZ)
    if not prof:
        raise HTTPException(404, "No plays recorded for that user")
    return render(request, "user.html", user, u=prof)


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
    request.session.clear()
    if settings.auth_mode == "authentik_forward":
        return RedirectResponse("/outpost.goauthentik.io/sign_out")
    return HTMLResponse("<p>Signed out of JellyCreep. <a href='/'>Sign in again</a></p>")


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
            "plays_per_day": stats.plays_per_day(db, days), "plays_by_hour": stats.plays_by_hour(db, days),
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
