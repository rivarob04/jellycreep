"""Turn webhook payloads from Sonarr/Radarr/Lidarr/Prowlarr, Seerr and Jellyfin into events."""
from __future__ import annotations

from typing import Any


def _ep_label(episodes: list[dict]) -> str:
    if not episodes:
        return ""
    e = episodes[0]
    label = f" S{int(e.get('seasonNumber', 0)):02d}E{int(e.get('episodeNumber', 0)):02d}"
    if len(episodes) > 1:
        label += f" (+{len(episodes) - 1} more)"
    return label


def parse(source: str, p: dict[str, Any]) -> tuple[str, str, str | None]:
    """Return (event_type, title, detail)."""
    if source in ("sonarr", "radarr", "lidarr", "readarr", "prowlarr"):
        etype = p.get("eventType", "Unknown")
        if "series" in p:
            title = (p["series"].get("title") or "Unknown series") + _ep_label(p.get("episodes") or [])
        elif "movie" in p:
            m = p["movie"]
            title = f"{m.get('title', 'Unknown movie')} ({m.get('year')})" if m.get("year") else m.get("title", "Unknown movie")
        elif "artist" in p:
            title = p["artist"].get("name") or p["artist"].get("artistName") or "Unknown artist"
            if p.get("albums"):
                title += f" – {p['albums'][0].get('title')}"
        elif etype in ("Health", "HealthRestored"):
            title = p.get("message") or etype
        else:
            title = p.get("message") or p.get("instanceName") or etype
        detail_parts = []
        release = p.get("release") or {}
        if release.get("releaseTitle"):
            detail_parts.append(release["releaseTitle"])
        if release.get("indexer"):
            detail_parts.append(f"via {release['indexer']}")
        if p.get("downloadClient"):
            detail_parts.append(f"→ {p['downloadClient']}")
        if p.get("isUpgrade"):
            detail_parts.append("(upgrade)")
        return etype, title, " ".join(detail_parts) or None

    if source in ("jellyseerr", "seerr", "overseerr"):
        etype = p.get("notification_type") or p.get("event") or "Unknown"
        title = p.get("subject") or "Seerr notification"
        req = p.get("request") or {}
        who = req.get("requestedBy_username")
        detail = f"requested by {who}" if who else p.get("message")
        return etype, title, detail

    if source == "jellyfin":
        etype = p.get("NotificationType") or p.get("Event") or "Unknown"
        name = p.get("Name") or p.get("ItemName") or "Jellyfin"
        if p.get("SeriesName"):
            name = f"{p['SeriesName']} – {name}"
        detail = " · ".join(str(x) for x in (p.get("NotificationUsername"), p.get("DeviceName"), p.get("ClientName")) if x)
        return etype, name, detail or None

    return p.get("eventType") or p.get("event") or "Event", p.get("title") or p.get("message") or source, None
