"""Runtime configuration, read from environment variables.

Everything here can be set as a stack variable in Dockhand (or any .env file).
"""
from __future__ import annotations

import ipaddress
import os
import secrets
from pathlib import Path


def _env(key: str, default: str | None = None) -> str | None:
    # Support Docker secrets: FILE__VAR=/run/secrets/x reads the value from a file.
    file_path = os.environ.get(f"FILE__{key}")
    if file_path and Path(file_path).is_file():
        return Path(file_path).read_text().strip()
    val = os.environ.get(key)
    return val if val not in (None, "") else default


def _bool(key: str, default: bool) -> bool:
    val = _env(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(_env("DATA_DIR", "/data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.secret_key = _env("SECRET_KEY") or self._persisted_secret()
        self.base_url = (_env("BASE_URL", "") or "").rstrip("/")
        self.tz = _env("TZ", "UTC")

        # --- Auth for JellyCreep itself -------------------------------------
        # authentik_forward : sit behind an Authentik proxy/forward-auth outpost and
        #                     trust its X-authentik-* headers (only from TRUSTED_PROXIES)
        # oidc              : log in with Authentik (or any OIDC provider) directly
        # none              : no login (LAN testing only)
        self.auth_mode = (_env("AUTH_MODE", "authentik_forward") or "").lower()
        self.trusted_proxies = []
        for n in (_env("TRUSTED_PROXIES", "127.0.0.1/32,172.16.0.0/12") or "").replace(" ", ",").split(","):
            if not n.strip():
                continue
            try:
                self.trusted_proxies.append(ipaddress.ip_network(n.strip(), strict=False))
            except ValueError:
                print(f"[jellycreep] WARNING: ignoring invalid TRUSTED_PROXIES entry {n.strip()!r} "
                      "(use an IP or CIDR like 192.168.0.85/32)", flush=True)
        self.user_header = _env("AUTHENTIK_USER_HEADER", "X-authentik-username")
        self.groups_header = _env("AUTHENTIK_GROUPS_HEADER", "X-authentik-groups")
        self.email_header = _env("AUTHENTIK_EMAIL_HEADER", "X-authentik-email")
        # Members of this group get admin rights (settings, stopping streams).
        # Leave empty to make every authenticated user an admin.
        self.admin_group = _env("ADMIN_GROUP", "jellycreep-admins") or ""

        # Jellyfin sign-in for regular users (can also be switched on in Settings)
        self.jellyfin_login_default = _env("JELLYFIN_LOGIN", "false")
        # Optional link on the login page for admins, e.g. https://jellycreep-admin.example.com
        self.admin_login_url = _env("ADMIN_LOGIN_URL", "") or ""

        self.oidc_issuer = (_env("OIDC_ISSUER", "") or "").rstrip("/")
        self.oidc_client_id = _env("OIDC_CLIENT_ID", "")
        self.oidc_client_secret = _env("OIDC_CLIENT_SECRET", "")
        self.oidc_scopes = _env("OIDC_SCOPES", "openid profile email groups")

        # --- Tracking ----------------------------------------------------------
        self.poll_interval = max(3, int(_env("POLL_INTERVAL", "10")))
        self.min_watch_seconds = int(_env("MIN_WATCH_SECONDS", "30"))
        self.watched_percent = int(_env("WATCHED_PERCENT", "90"))
        self.history_retention_days = int(_env("HISTORY_RETENTION_DAYS", "0"))  # 0 = forever
        self.event_retention_days = int(_env("EVENT_RETENTION_DAYS", "90"))

        self.cookie_secure = _bool("COOKIE_SECURE", True)

    def _persisted_secret(self) -> str:
        path = self.data_dir / "secret.key"
        if path.exists():
            return path.read_text().strip()
        key = secrets.token_urlsafe(48)
        path.write_text(key)
        path.chmod(0o600)
        return key


settings = Settings()
