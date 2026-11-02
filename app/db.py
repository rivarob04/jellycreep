"""Database models (SQLite) and secret encryption."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (Boolean, DateTime, Float, Integer, String, Text, create_engine, event, Index)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings

engine = create_engine(
    f"sqlite:///{settings.data_dir / 'jellycreep.db'}",
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()))


def encrypt(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


SERVICE_KINDS = {
    "jellyfin": "Jellyfin",
    "sonarr": "Sonarr",
    "radarr": "Radarr",
    "lidarr": "Lidarr",
    "prowlarr": "Prowlarr",
    "sabnzbd": "SABnzbd",
    "qbittorrent": "qBittorrent",
    "jellyseerr": "Jellyseerr / Seerr",
    "jellystat": "Jellystat",
    "wizarr": "Wizarr",
}

PROXY_AUTH_MODES = {
    "none": "None (direct / LAN / VPN)",
    "authentik_basic": "Authentik – username + app password (HTTP Basic)",
    "authentik_token": "Authentik – OAuth2 client credentials (Bearer JWT)",
    "headers": "Custom headers only (e.g. Cloudflare Access service token)",
}


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(500))
    public_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    timeout: Mapped[int] = mapped_column(Integer, default=15)

    # Credentials for the app itself
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    username: Mapped[str | None] = mapped_column(String(200), nullable=True)
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Credentials for the MFA / identity-aware proxy in front of the app
    proxy_auth: Mapped[str] = mapped_column(String(32), default="none")
    proxy_username: Mapped[str | None] = mapped_column(String(200), nullable=True)
    proxy_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    authentik_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    authentik_client_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    extra_headers_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_status: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    # -- secret accessors --
    @property
    def api_key(self) -> str | None:
        return decrypt(self.api_key_enc)

    @api_key.setter
    def api_key(self, v: str | None) -> None:
        self.api_key_enc = encrypt(v)

    @property
    def password(self) -> str | None:
        return decrypt(self.password_enc)

    @password.setter
    def password(self, v: str | None) -> None:
        self.password_enc = encrypt(v)

    @property
    def proxy_password(self) -> str | None:
        return decrypt(self.proxy_password_enc)

    @proxy_password.setter
    def proxy_password(self, v: str | None) -> None:
        self.proxy_password_enc = encrypt(v)

    @property
    def extra_headers(self) -> dict[str, str]:
        raw = decrypt(self.extra_headers_enc)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return {str(k): str(v) for k, v in data.items()}
        except (ValueError, AttributeError):
            return {}

    @extra_headers.setter
    def extra_headers(self, v: dict[str, str] | None) -> None:
        self.extra_headers_enc = encrypt(json.dumps(v)) if v else None

    @property
    def kind_label(self) -> str:
        return SERVICE_KINDS.get(self.kind, self.kind)

    @property
    def link(self) -> str:
        return (self.public_url or self.url).rstrip("/")


class ActiveStream(Base):
    """A playback session currently being tracked (survives restarts)."""
    __tablename__ = "active_streams"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    server_id: Mapped[int] = mapped_column(Integer)
    session_id: Mapped[str] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime)
    watched_seconds: Mapped[float] = mapped_column(Float, default=0)
    paused_seconds: Mapped[float] = mapped_column(Float, default=0)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[str] = mapped_column(Text)  # JSON snapshot of the play fields


class Play(Base):
    """A finished playback – one row in the history."""
    __tablename__ = "plays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(Integer)
    session_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_name: Mapped[str] = mapped_column(String(200))
    item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    poster_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(500))
    series_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    stopped_at: Mapped[datetime] = mapped_column(DateTime)
    watched_seconds: Mapped[float] = mapped_column(Float, default=0)
    paused_seconds: Mapped[float] = mapped_column(Float, default=0)
    runtime_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    client: Mapped[str | None] = mapped_column(String(200), nullable=True)
    device: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    play_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    container: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcode_reasons: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        Index("ix_plays_started", "started_at"),
        Index("ix_plays_user", "user_name"),
        Index("ix_plays_series", "series_name"),
    )

    @property
    def display_title(self) -> str:
        if self.item_type == "Episode" and self.series_name:
            se = ""
            if self.season is not None and self.episode is not None:
                se = f" – S{self.season:02d}E{self.episode:02d}"
            return f"{self.series_name}{se} – {self.title}"
        if self.year:
            return f"{self.title} ({self.year})"
        return self.title


class Event(Base):
    """Something an *arr / Seerr / Jellyfin told us via webhook."""
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(500))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_events_created", "created_at"),)


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()




class Setting(Base):
    """Small encrypted key/value store for app settings (notifications etc.)."""
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_enc: Mapped[str | None] = mapped_column(Text, nullable=True)


def get_setting(key: str, default=None):
    with SessionLocal() as db:
        row = db.get(Setting, key)
        if not row or not row.value_enc:
            return default
        raw = decrypt(row.value_enc)
        try:
            return json.loads(raw) if raw else default
        except ValueError:
            return default


def set_setting(key: str, value) -> None:
    with SessionLocal() as db:
        row = db.get(Setting, key) or Setting(key=key)
        row.value_enc = encrypt(json.dumps(value))
        db.merge(row)
        db.commit()


def init_db() -> None:
    Base.metadata.create_all(engine)
