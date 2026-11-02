"""Push notifications: ntfy, Pushover, Discord and a generic JSON webhook."""
from __future__ import annotations

import asyncio
import logging

import httpx

from .db import get_setting

log = logging.getLogger("jellycreep.notify")

TRIGGERS = {
    "transcode": "A stream starts transcoding",
    "stream_start": "Any stream starts (can be chatty)",
    "download_failed": "A download fails (Sonarr/Radarr webhook)",
    "app_down": "A connected app goes down or comes back",
    "new_request": "New Seerr request waiting for approval (Seerr webhook)",
    "health": "Sonarr/Radarr health warnings (webhook)",
    "new_user": "Someone signs up through a Wizarr invite (Jellyfin webhook)",
}
DEFAULT_TRIGGERS = {"transcode": False, "stream_start": False, "download_failed": True, "app_down": True,
                    "new_request": True, "health": False, "new_user": True}


def config() -> dict:
    cfg = get_setting("notifications", {}) or {}
    cfg.setdefault("channels", {})
    trig = dict(DEFAULT_TRIGGERS)
    trig.update(cfg.get("triggers") or {})
    cfg["triggers"] = trig
    return cfg


def enabled(trigger: str) -> bool:
    cfg = config()
    return bool(cfg["triggers"].get(trigger)) and any(_configured(cfg["channels"]).values())


def _configured(ch: dict) -> dict:
    return {
        "ntfy": bool((ch.get("ntfy") or {}).get("topic")),
        "pushover": bool((ch.get("pushover") or {}).get("user") and (ch.get("pushover") or {}).get("token")),
        "discord": bool((ch.get("discord") or {}).get("webhook")),
        "webhook": bool((ch.get("webhook") or {}).get("url")),
    }


async def _send_all(title: str, message: str, url: str | None, priority: str, channels: dict) -> list[str]:
    errors: list[str] = []
    if url and not url.startswith(("http://", "https://")):
        url = None  # relative links (no BASE_URL set) aren't clickable in push apps
    on = _configured(channels)
    async with httpx.AsyncClient(timeout=10) as c:
        async def go(name, coro):
            try:
                r = await coro
                if r.status_code >= 400:
                    errors.append(f"{name}: HTTP {r.status_code} {r.text[:120]}")
            except httpx.HTTPError as e:
                errors.append(f"{name}: {e.__class__.__name__}: {e}")

        jobs = []
        if on["ntfy"]:
            n = channels["ntfy"]
            server = (n.get("server") or "https://ntfy.sh").rstrip("/")
            # JSON publishing keeps emoji/UTF-8 in titles intact (HTTP headers are ASCII-only).
            body = {"topic": n["topic"], "title": title, "message": message,
                    "priority": {"high": 4, "low": 2}.get(priority, 3), "tags": ["jellyfish"]}
            if url:
                body["click"] = url
            headers = {"Authorization": f"Bearer {n['token']}"} if n.get("token") else {}
            jobs.append(go("ntfy", c.post(server, json=body, headers=headers)))
        if on["pushover"]:
            p = channels["pushover"]
            data = {"token": p["token"], "user": p["user"], "title": title, "message": message,
                    "priority": {"high": 1, "low": -1}.get(priority, 0)}
            if url:
                data["url"] = url
            jobs.append(go("Pushover", c.post("https://api.pushover.net/1/messages.json", data=data)))
        if on["discord"]:
            embed = {"title": title, "description": message, "color": 0xA96BFF}
            if url:
                embed["url"] = url
            jobs.append(go("Discord", c.post(channels["discord"]["webhook"], json={"username": "JellyCreep", "embeds": [embed]})))
        if on["webhook"]:
            jobs.append(go("Webhook", c.post(channels["webhook"]["url"],
                                             json={"title": title, "message": message, "url": url, "priority": priority})))
        await asyncio.gather(*jobs)
    return errors


async def send(trigger: str, title: str, message: str, url: str | None = None, priority: str = "default") -> None:
    """Fire-and-forget: never let a notification failure break the caller."""
    try:
        if not enabled(trigger):
            return
        cfg = config()
        errs = await _send_all(title, message, url, priority, cfg["channels"])
        for e in errs:
            log.warning("Notification failed: %s", e)
    except Exception:  # noqa: BLE001
        log.exception("Notification error")


def send_later(trigger: str, title: str, message: str, url: str | None = None, priority: str = "default") -> None:
    try:
        asyncio.get_running_loop().create_task(send(trigger, title, message, url, priority))
    except RuntimeError:
        pass


async def test(channels: dict) -> list[str]:
    if not any(_configured(channels).values()):
        return ["No channel is configured yet"]
    return await _send_all("JellyCreep test", "Notifications are working 🎉", None, "default", channels)
