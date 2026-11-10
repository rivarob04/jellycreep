"""Is there a newer JellyCreep on GitHub? Checked every 6 hours (UPDATE_CHECK=off to disable).

Only reads the public version file from the repository: nothing about your server is sent.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

import httpx

from . import __version__

log = logging.getLogger("jellycreep.updates")

REPO = "rivarob04/jellycreep"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/main/app/__init__.py"
CHANGES_URL = f"https://github.com/{REPO}/commits/main"

_latest: dict = {"version": None, "checked": 0.0, "error": None}


def enabled() -> bool:
    return (os.environ.get("UPDATE_CHECK", "on") or "on").lower() not in ("off", "false", "0", "no")


def _parse(v: str | None) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


def info() -> dict:
    latest = _latest["version"]
    newer = bool(latest) and _parse(latest) > _parse(__version__)
    return {"current": __version__, "latest": latest, "newer": newer, "url": CHANGES_URL,
            "checked": _latest["checked"], "error": _latest["error"], "enabled": enabled()}


async def check() -> None:
    if not enabled():
        return
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": f"JellyCreep/{__version__}"}) as c:
            r = await c.get(VERSION_URL)
        r.raise_for_status()
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', r.text)
        if m:
            if m.group(1) != _latest["version"] and _parse(m.group(1)) > _parse(__version__):
                log.info("A newer JellyCreep is available: v%s (running v%s)", m.group(1), __version__)
            _latest.update(version=m.group(1), error=None)
    except Exception as e:  # noqa: BLE001
        _latest["error"] = str(e)[:200]
        log.debug("Update check failed: %s", e)
    _latest["checked"] = time.time()


async def run_forever() -> None:
    await asyncio.sleep(20)
    while True:
        await check()
        await asyncio.sleep(6 * 3600)
