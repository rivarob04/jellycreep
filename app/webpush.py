"""Web Push: notifications straight to phones and browsers that installed / opened JellyCreep.

Implements RFC 8291 (message encryption, aes128gcm) and RFC 8292 (VAPID) with
`cryptography` + `httpx`, so no extra dependencies. Keys are generated once and kept
(encrypted) in the settings table.

Who gets what is decided per *person* (``user_key``) with preferences stored in
settings; each of their devices is a ``PushSub`` row.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
import time
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete, select

from .config import settings
from .db import PushSub, SessionLocal, get_setting, set_setting, utcnow

log = logging.getLogger("jellycreep.webpush")

# Only ever POST to real push services (the endpoint comes from the browser, so don't trust it blindly).
PUSH_HOSTS = ("fcm.googleapis.com", "android.googleapis.com", "updates.push.services.mozilla.com",
              "push.services.mozilla.com", "web.push.apple.com", "notify.windows.com", "push.apple.com")

STAFF_PREFS = {
    "new_request": "New Seerr request waiting for approval",
    "download_failed": "A download fails",
    "app_down": "A connected app goes down or comes back",
    "new_user": "Someone joins through a Wizarr invite",
    "transcode": "A stream starts transcoding",
    "stream_start": "Any stream starts (chatty)",
    "health": "Sonarr / Radarr health warnings",
    "new_media": "New movies and episodes are added",
}
STAFF_DEFAULTS = {"new_request": True, "download_failed": True, "app_down": True, "new_user": True, "transcode": False,
                  "stream_start": False, "health": False, "new_media": False}
VIEWER_PREFS = {
    "request_status": "My requests are approved, declined or available",
    "new_episode": "New episodes of shows I watch",
    "new_media": "Anything new is added (movies and shows)",
}
VIEWER_DEFAULTS = {"request_status": True, "new_episode": True, "new_media": False}
# switched on again whenever someone turns notifications on (their first device)
ON_BY_DEFAULT = {True: ("request_status", "new_episode"), False: ("new_request", "download_failed", "app_down", "new_user")}
# where a staff notification should open
STAFF_URLS = {"transcode": "/", "stream_start": "/", "download_failed": "/automation?tab=queue",
              "health": "/automation?tab=queue", "app_down": "/settings", "new_request": "/automation?tab=requests",
              "new_user": "/users", "new_media": "/library"}


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# --------------------------------------------------------------------------- keys
_KEYS: dict = {}


def _keys() -> tuple[ec.EllipticCurvePrivateKey, str]:
    if not _KEYS:
        stored = get_setting("vapid", None)
        if not stored:
            priv = ec.generate_private_key(ec.SECP256R1())
            pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode()
            set_setting("vapid", {"private_pem": pem})
            stored = {"private_pem": pem}
        priv = serialization.load_pem_private_key(stored["private_pem"].encode(), None)
        pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        _KEYS.update(priv=priv, pub=_b64(pub))
    return _KEYS["priv"], _KEYS["pub"]


def public_key() -> str:
    return _keys()[1]


def _vapid_header(endpoint: str) -> str:
    priv, pub = _keys()
    u = urlsplit(endpoint)
    sub = os.environ.get("VAPID_SUBJECT") or (settings.base_url if settings.base_url.startswith("https://") else
                                               "mailto:jellycreep@users.noreply.github.com")
    head = _b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = _b64(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": sub},
                           separators=(",", ":")).encode())
    der = priv.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{body}.{sig}, k={pub}"


# --------------------------------------------------------------------------- RFC 8291 encryption
def _hkdf(salt: bytes, ikm: bytes, info: bytes, n: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    ua_pub = _unb64(p256dh)
    auth_secret = _unb64(auth)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    as_priv = ec.generate_private_key(ec.SECP256R1())
    as_pub = as_priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = as_priv.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", 4096, len(as_pub)) + as_pub + ct


def valid_endpoint(endpoint: str) -> bool:
    try:
        u = urlsplit(endpoint)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


# --------------------------------------------------------------------------- sending
async def _send(client: httpx.AsyncClient, sub: PushSub, data: dict, urgency: str) -> int:
    body = encrypt(json.dumps(data).encode(), sub.p256dh, sub.auth)
    headers = {"Authorization": _vapid_header(sub.endpoint), "Content-Encoding": "aes128gcm",
               "Content-Type": "application/octet-stream", "TTL": "86400", "Urgency": urgency}
    if data.get("tag"):
        headers["Topic"] = "".join(ch for ch in str(data["tag"]) if ch.isalnum())[:32] or "jc"
    try:
        r = await client.post(sub.endpoint, content=body, headers=headers)
        return r.status_code
    except httpx.HTTPError as e:
        log.warning("Push to %s failed: %s", urlsplit(sub.endpoint).hostname, e)
        return 0


async def deliver(subs: list[PushSub], title: str, body: str, url: str = "/", tag: str | None = None,
                  urgency: str = "normal") -> int:
    """Send one notification to each subscription; drop the ones the push service says are gone."""
    if not subs:
        return 0
    data = {"title": title, "body": body, "url": url, "tag": tag, "ts": int(time.time() * 1000)}
    async with httpx.AsyncClient(timeout=15) as c:
        codes = await asyncio.gather(*(_send(c, s, data, urgency) for s in subs))
    gone, ok, bad = [], [], []
    for s, code in zip(subs, codes):
        if code in (404, 410):
            gone.append(s.id)
        elif 200 <= code < 300:
            ok.append(s.id)
        else:
            bad.append(s.id)
            if code:
                log.warning("Push service answered %s for %s", code, urlsplit(s.endpoint).hostname)
    with SessionLocal() as db:
        if gone:
            db.execute(delete(PushSub).where(PushSub.id.in_(gone)))
        for row in db.scalars(select(PushSub).where(PushSub.id.in_(ok + bad))):
            if row.id in ok:
                row.last_ok, row.failures = utcnow(), 0
            else:
                row.failures = (row.failures or 0) + 1
        db.execute(delete(PushSub).where(PushSub.failures >= 20))
        db.commit()
    return len(ok)


# --------------------------------------------------------------------------- people & preferences
def user_key(user) -> str:
    return f"viewer:{user.server_id}:{user.jf_id}" if user.is_viewer else f"staff:{user.name.lower()}"


def prefs_for(key: str, viewer: bool) -> dict:
    base = dict(VIEWER_DEFAULTS if viewer else STAFF_DEFAULTS)
    base.update({k: bool(v) for k, v in (get_setting(f"pushprefs:{key}", {}) or {}).items() if k in base})
    return base


def enable_defaults(key: str, viewer: bool) -> None:
    """Notifications were just switched on: tick the main alerts again."""
    cur = prefs_for(key, viewer)
    save_prefs(key, viewer, {**cur, **{k: True for k in ON_BY_DEFAULT[viewer]}})


def save_prefs(key: str, viewer: bool, values: dict) -> None:
    allowed = VIEWER_PREFS if viewer else STAFF_PREFS
    set_setting(f"pushprefs:{key}", {k: bool(values.get(k)) for k in allowed})


def subs_for(key: str) -> list[PushSub]:
    with SessionLocal() as db:
        return list(db.scalars(select(PushSub).where(PushSub.user_key == key)))


def _grouped(where) -> dict[str, list[PushSub]]:
    with SessionLocal() as db:
        rows = list(db.scalars(select(PushSub).where(where)))
    out: dict[str, list[PushSub]] = {}
    for r in rows:
        out.setdefault(r.user_key, []).append(r)
    return out


async def notify_staff(trigger: str, title: str, body: str, url: str | None = None, tag: str | None = None,
                       urgency: str = "normal") -> None:
    try:
        targets = [s for key, subs in _grouped(PushSub.role != "viewer").items() if prefs_for(key, False).get(trigger)
                   for s in subs]
        await deliver(targets, title, body, url or STAFF_URLS.get(trigger, "/"), tag or trigger, urgency)
    except Exception:  # noqa: BLE001 – never break the caller
        log.exception("Staff push failed")


async def notify_viewer(server_id: int | None, jf_id: str, kind: str, title: str, body: str, url: str = "/me",
                        tag: str | None = None) -> int:
    jf = (jf_id or "").replace("-", "").lower()
    if not jf:
        return 0
    key = f"viewer:{server_id}:{jf}"
    try:
        if not prefs_for(key, True).get(kind):
            return 0
        subs = subs_for(key) if server_id is not None else []
        if server_id is None:   # caller doesn't know the server: any device of that Jellyfin user
            with SessionLocal() as db:
                subs = list(db.scalars(select(PushSub).where(PushSub.jf_id == jf, PushSub.role == "viewer")))
        return await deliver(subs, title, body, url, tag)
    except Exception:  # noqa: BLE001
        log.exception("Viewer push failed")
        return 0


async def announce(title: str, body: str, audience: str = "everyone") -> int:
    where = {"viewers": PushSub.role == "viewer", "staff": PushSub.role != "viewer"}.get(audience, PushSub.id > 0)
    # announcements always go out — there's no opt-out
    targets = [s for subs in _grouped(where).values() for s in subs]
    return await deliver(targets, title, body, "/me" if audience == "viewers" else "/", "announce", "high")


def viewer_keys() -> dict[str, tuple[int | None, str]]:
    """Subscribed Jellyfin users: user_key -> (server_id, jf_id)."""
    with SessionLocal() as db:
        rows = db.execute(select(PushSub.user_key, PushSub.server_id, PushSub.jf_id).where(PushSub.role == "viewer")).all()
    return {k: (sid, jf) for k, sid, jf in rows if jf}


def drop_viewer(jf_id: str) -> None:
    with SessionLocal() as db:
        db.execute(delete(PushSub).where(PushSub.jf_id == jf_id, PushSub.role == "viewer"))
        db.commit()
