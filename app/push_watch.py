"""Background checks that turn changes into phone notifications.

* Seerr: a request was approved, declined, or became available -> the person who asked.
* Jellyfin: new episodes of a show someone has watched -> that person;
  anything new -> people (and staff) who asked for "new media".

The first run only records where things are, so nobody gets a flood of old news.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy import select

from . import seerr_portal, webpush
from .db import Play, Service, SessionLocal, get_setting, set_setting, utcnow
from .services import client_for
from .upstream import UpstreamError

log = logging.getLogger("jellycreep.push_watch")
_OK: dict[str, tuple[float, bool]] = {}     # jf id -> (checked at, still allowed)


def _norm(x) -> str:
    return str(x or "").replace("-", "").lower()


async def _viewer_still_ok(svc: Service, jf_id: str) -> bool:
    """Don't keep notifying deleted / disabled Jellyfin accounts."""
    hit = _OK.get(jf_id)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    ok = True
    try:
        info = await (await client_for(svc)).get(f"/Users/{jf_id}")
        ok = not (info.get("Policy") or {}).get("IsDisabled")
    except UpstreamError as e:
        ok = not ("404" in str(e) or "400" in str(e))
    _OK[jf_id] = (time.time(), ok)
    if not ok:
        webpush.drop_viewer(jf_id)
    return ok


def _jellyfins() -> list[Service]:
    with SessionLocal() as db:
        return list(db.scalars(select(Service).where(Service.kind == "jellyfin", Service.enabled.is_(True))))


# --------------------------------------------------------------------------- Seerr
async def seerr_once() -> None:
    if not webpush.viewer_keys():
        return
    with SessionLocal() as db:
        svc = db.scalars(select(Service).where(Service.kind == "jellyseerr", Service.enabled.is_(True))).first()
    if not svc:
        return
    client = await client_for(svc)
    try:
        data = await client.get("/api/v1/request", params={"take": "50", "skip": "0", "sort": "modified", "filter": "all"}) or {}
    except UpstreamError as e:
        log.debug("Seerr poll failed: %s", e)
        return
    state: dict = get_setting("push_seerr_state", None)
    first = state is None
    state = state or {}
    jfs = {s.id: s for s in _jellyfins()}
    for r in data.get("results") or []:
        rid = str(r.get("id"))
        media = r.get("media") or {}
        now = [r.get("status") or 1, media.get("status4k" if r.get("is4k") else "status") or 1]
        old = state.get(rid)
        state[rid] = now
        if first or old is None or old == now:
            continue
        jf = _norm((r.get("requestedBy") or {}).get("jellyfinUserId"))
        if not jf:
            continue
        mt = "movie" if r.get("type") == "movie" else "tv"
        meta = await seerr_portal._meta(client, svc.id, mt, media.get("tmdbId")) if media.get("tmdbId") else {}
        title = (meta or {}).get("title") or "Your request"
        url = f"/me/discover/{mt}/{media.get('tmdbId')}" if media.get("tmdbId") else "/me/requests"
        msg = None
        if now[1] == 5 and old[1] != 5:
            msg = ("🍿 Ready to watch", f"{title} is now on Jellyfin.")
        elif now[1] == 4 and old[1] not in (4, 5):
            msg = ("🍿 Partly available", f"Some of {title} is now on Jellyfin.")
        elif now[0] == 2 and old[0] == 1:
            msg = ("✅ Request approved", f"{title} was approved and will start downloading.")
        elif now[0] == 3 and old[0] != 3:
            msg = ("❌ Request declined", f"{title} was declined.")
        if not msg:
            continue
        # which server is this person on? any Jellyfin server where they have a subscribed device
        for key, (sid, jid) in webpush.viewer_keys().items():
            if jid == jf and sid in jfs and await _viewer_still_ok(jfs[sid], jf):
                await webpush.notify_viewer(sid, jf, "request_status", msg[0], msg[1], url, tag=f"req{rid}")
    if len(state) > 400:
        keep = sorted(state, key=lambda k: int(k) if k.isdigit() else 0)[-300:]
        state = {k: state[k] for k in keep}
    set_setting("push_seerr_state", state)


# --------------------------------------------------------------------------- new media
def _when(s):
    from .library import _when as w
    return w(s)


async def media_once() -> None:
    wants_staff = any(webpush.prefs_for(k, False).get("new_media") for k in {s.user_key for s in _staff_subs()})
    viewers = webpush.viewer_keys()
    if not wants_staff and not viewers:
        return
    for svc in _jellyfins():
        key = f"push_media_since_{svc.id}"
        since = get_setting(key, None)
        client = await client_for(svc)
        try:
            data = await client.get("/Items", params={"SortBy": "DateCreated", "SortOrder": "Descending", "Recursive": "true",
                                                      "IncludeItemTypes": "Movie,Episode", "Limit": "80", "IsMissing": "false",
                                                      "Fields": "DateCreated,ProductionYear,SeriesId"}) or {}
        except UpstreamError as e:
            log.debug("Jellyfin poll failed: %s", e)
            continue
        items = data.get("Items") or []
        newest = max((i.get("DateCreated") or "" for i in items), default=since or "")
        if since is None:
            set_setting(key, newest or utcnow().strftime("%Y-%m-%dT%H:%M:%S"))
            continue
        fresh = [i for i in items if (i.get("DateCreated") or "")[:19] > since[:19]]
        if not fresh:
            continue
        set_setting(key, newest)
        await _announce_new(svc, fresh, viewers, wants_staff)


def _staff_subs():
    from .db import PushSub
    with SessionLocal() as db:
        return list(db.scalars(select(PushSub).where(PushSub.role != "viewer")))


def _label(i: dict) -> str:
    if i.get("Type") == "Episode":
        return f"{i.get('SeriesName')} S{i.get('ParentIndexNumber', '?')}E{i.get('IndexNumber', '?')}"
    return f"{i.get('Name')}" + (f" ({i['ProductionYear']})" if i.get("ProductionYear") else "")


def _summary(items: list[dict]) -> str:
    names = list(dict.fromkeys(_label(i) for i in items))
    return ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")


async def _announce_new(svc: Service, fresh: list[dict], viewers: dict, wants_staff: bool) -> None:
    if wants_staff:
        await webpush.notify_staff("new_media", "🆕 Added to Jellyfin", _summary(fresh), "/library", tag="newmedia")

    # who watches each series (last 6 months)
    series = {}
    for i in fresh:
        if i.get("Type") == "Episode" and i.get("SeriesId"):
            series.setdefault(i["SeriesId"], []).append(i)
    watchers: dict[str, set[str]] = {}
    if series:
        with SessionLocal() as db:
            rows = db.execute(select(Play.poster_id, Play.user_id).where(
                Play.server_id == svc.id, Play.poster_id.in_(list(series)),
                Play.started_at >= utcnow() - timedelta(days=180))).all()
        for pid, uid in rows:
            if uid:
                watchers.setdefault(_norm(uid), set()).add(pid)

    client = await client_for(svc)
    for key, (sid, jf) in viewers.items():
        if sid != svc.id or not await _viewer_still_ok(svc, jf):
            continue
        prefs = webpush.prefs_for(key, True)
        told: set[str] = set()
        if prefs.get("new_episode"):
            for sid_ in watchers.get(jf, ()):
                eps = sorted(series[sid_], key=lambda e: (e.get("ParentIndexNumber") or 0, e.get("IndexNumber") or 0))
                name = eps[0].get("SeriesName") or "a show you watch"
                if len(eps) == 1:
                    e = eps[0]
                    body = f"S{e.get('ParentIndexNumber', '?')} • E{e.get('IndexNumber', '?')} · {e.get('Name')}"
                    url = f"/me/library/{svc.id}/{e.get('Id')}"
                else:
                    body, url = f"{len(eps)} new episodes", f"/me/library/{svc.id}/{sid_}"
                await webpush.notify_viewer(svc.id, jf, "new_episode", f"📺 New episode of {name}", body, url, tag=f"ep{sid_}")
                told.update(e.get("Id") for e in eps)
        if prefs.get("new_media"):
            # only what this person's account can actually see
            try:
                mine = await client.get("/Items", params={"userId": jf, "Ids": ",".join(i["Id"] for i in fresh if i.get("Id"))}) or {}
            except UpstreamError:
                continue
            visible = [i for i in mine.get("Items") or [] if i.get("Id") not in told]
            if visible:
                await webpush.notify_viewer(svc.id, jf, "new_media", "🆕 New on Jellyfin", _summary(visible), "/me/library", tag="newmedia")
