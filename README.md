# 🪼 JellyCreep

**Tautulli-style monitoring for Jellyfin**, built for homelabs where everything sits behind **Authentik MFA**, and designed first for your **phone**.

- **Live activity:** who's watching what, direct play vs transcode (with codecs, resolution, bitrate and the transcode reason), progress, IP and device. Admins can stop a stream and send the user a message.
- **Watch history it records itself.** It polls Jellyfin's sessions like Tautulli does, counting play time minus pauses. It survives restarts and supports several Jellyfin servers.
- **Users and stats:** top users, movies and shows, plays per day, peak hours, play methods and clients.
- **Automation in one place:** Sonarr/Radarr/Lidarr queues, upcoming calendar, health and disk space; qBittorrent downloads; Prowlarr indexers; Seerr requests.
- **Events timeline:** grabs, imports, requests and health alerts pushed in by webhooks from the *arrs, Seerr and Jellyfin.
- **Jellystat bundled** in the same stack. Its stats show up inside JellyCreep, and you can still open its own UI.
- **Works with apps behind Authentik.** Each upstream app can be reached through Authentik with a service account (app password or OAuth2 client credentials), or with custom headers for Cloudflare Access and similar.
- **Phone-first UI:** bottom tab bar, card layouts, large tap targets, dark theme. *Add to Home Screen* makes it open full-screen like an app.
- **Read-only REST API** with tokens, for Homepage, scripts or other apps. Docs are at `/api/docs`.

---

## Deploy with Dockhand

1. **Create the shared network** (once per host) that your reverse proxy / Authentik outpost uses:
   ```sh
   docker network create proxy
   ```
   If you already have one, set `PROXY_NETWORK` to its name.
2. In Dockhand: **Stacks → Add from Git**
   - Repository: `https://github.com/rivarob04/jellycreep`
   - Branch: `main`
   - Compose file: `compose.yaml`
3. Add the **stack variables**, marking the secrets as *secret*:

   | Variable | Value |
   |---|---|
   | `JELLYCREEP_SECRET_KEY` | `openssl rand -base64 48` |
   | `JELLYSTAT_DB_PASSWORD` | a password |
   | `JELLYSTAT_JWT_SECRET` | `openssl rand -base64 48` |
   | `JELLYCREEP_BASE_URL` | `https://jellycreep.example.com` |
   | `JELLYFIN_URL` / `JELLYFIN_API_KEY` | e.g. `http://jellyfin:8096` / key from Dashboard → API Keys |

   Every other option is listed in [`.env.example`](.env.example). You can also add services later from **Settings** on your phone.
4. **Deploy.** The stack runs three containers: `jellycreep` (port 8484), `jellystat` (port 3000) and `jellystat-db`.
5. Open Jellystat once, create its admin account, connect it to Jellyfin, then make an API key (**Settings → API Keys**). Paste the key into JellyCreep under **Settings → Jellystat → Edit**.

> The image is built by GitHub Actions to `ghcr.io/rivarob04/jellycreep`. GHCR packages start out private, so either make the package public (GitHub → Packages → jellycreep → Package settings) or let Dockhand build it. The compose file has `build: .` as a fallback.

Plain Docker works the same way: `cp .env.example .env`, edit it, then `docker compose up -d`.

---

## Putting JellyCreep behind Authentik

### Option A: proxy provider / forward auth (recommended)

1. In Authentik: **Applications → Create with provider → Proxy Provider**
   - *Forward auth (single application)* if you use Traefik, Caddy or NPM, or *Proxy* if the outpost serves traffic directly.
   - External host: `https://jellycreep.example.com`. Internal host (Proxy mode): `http://jellycreep:8484`.
   - **Unauthenticated Paths:**
     ```
     ^/api/.*
     ^/healthz$
     ```
     The API checks its own tokens, so webhooks and other apps get through without MFA. Leave this out if you'd rather send Authentik credentials from those clients too.
2. Create a group named `jellycreep-admins` and add yourself. Members can change settings and stop streams; everyone else gets read-only access.
3. Keep `AUTH_MODE=authentik_forward`. JellyCreep only trusts the `X-authentik-username` and `X-authentik-groups` headers from addresses in `TRUSTED_PROXIES`, which defaults to localhost and Docker networks. Its port is bound to `127.0.0.1`, so nothing on your LAN can skip the login.
   - If your proxy runs on **another machine**, set `JELLYCREEP_BIND=0.0.0.0` and `TRUSTED_PROXIES=<that machine's IP>/32`.

### Option B: log in with OIDC

Create an **OAuth2/OpenID provider** in Authentik with the redirect URI `https://jellycreep.example.com/auth/callback`. Give it the `groups` scope mapping if you want admin detection. Then set:

```
AUTH_MODE=oidc
OIDC_ISSUER=https://auth.example.com/application/o/<app-slug>
OIDC_CLIENT_ID=...
OIDC_CLIENT_SECRET=...
```

---

## Connecting Sonarr, Radarr, qBittorrent and the rest *through* Authentik MFA

A normal API client can't complete an MFA prompt, so JellyCreep logs in to Authentik as a **service account** for each app that sits behind Authentik.

1. In Authentik, **Directory → Users → Create Service Account** (e.g. `svc-jellycreep`). Don't let it create a group.
2. **Directory → Tokens and App passwords → Create**, choose *App password*, and set the user to `svc-jellycreep`.
3. Give that account access to each application (Sonarr, Radarr, etc.) by adding it to the group or user bindings on each app's policy.
4. In JellyCreep **Settings → (service) → Edit**:
   - **URL:** the public URL behind Authentik, e.g. `https://sonarr.example.com`
   - **API key:** the app's own key. It's sent as `X-Api-Key`, so it doesn't clash with Authentik.
   - **Proxy auth:** pick one of the modes below.

| Mode | What JellyCreep sends | Use when |
|---|---|---|
| **Authentik – username + app password** | `Authorization: Basic svc-jellycreep:<app password>` | Simplest choice. Works with proxy and forward-auth providers. |
| **Authentik – OAuth2 client credentials** | Gets a JWT from `/application/o/token/` (needs the proxy provider's *Client ID*) and sends `Authorization: Bearer …`, refreshing it automatically | You want short-lived tokens on the wire. |
| **Custom headers only** | Whatever you list, e.g. `CF-Access-Client-Id` / `CF-Access-Client-Secret` | Cloudflare Access, Pomerium, a shared-secret header… |
| **None** | Only the app's own credentials | LAN/Docker network, VPN, or Authentik already bypasses `^/api/.*` for that app |

Custom headers are added in every mode. The session cookie Authentik sets is reused across requests, as Authentik recommends, so the outpost isn't re-checking credentials on every poll.

**Use Save & test.** If JellyCreep runs into Authentik's login page, it tells you so directly ("Blocked by the login proxy in front of …") instead of showing a confusing JSON error.

> If you use forward auth with Traefik, Caddy or NPM, make sure the proxy passes the `Authorization` header through to the outpost. Authentik needs it to see the app password.

**Same Docker network, no Authentik?** Use the container URL (`http://sonarr:8989`) with proxy auth set to *None*. That's the fastest path.

---

## Webhooks → Events

Create an API token in **Settings → API tokens**, then add a webhook in each app pointing to:

```
https://jellycreep.example.com/api/v1/webhook/<source>?apikey=<token>
```

`<source>` is one of `sonarr`, `radarr`, `lidarr`, `prowlarr`, `jellyseerr` or `jellyfin`. For Jellyfin, install the Webhook plugin and use a Generic destination. Settings shows each URL with a copy button.

## API

All endpoints are read-only and need `X-Api-Key: <token>` (or `?apikey=`):

| Endpoint | |
|---|---|
| `GET /api/v1/activity` | current streams |
| `GET /api/v1/history?user_name=&type=&q=&method=&limit=&offset=` | watch history |
| `GET /api/v1/users` | per-user totals |
| `GET /api/v1/stats?days=30` | aggregates (top users, movies, shows, per day, per hour…) |
| `GET /api/v1/events?source=` | webhook events |
| `GET /api/v1/services` | connection status of each service |

Interactive docs: `/api/docs`.

## On your phone

Open JellyCreep in your phone's browser, sign in through Authentik, then use **Share → Add to Home Screen** on iOS or **⋮ → Install app** on Android. It opens full-screen with a bottom tab bar for **Activity · History · Stats · Arr · More**.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data AUTH_MODE=none COOKIE_SECURE=false uvicorn app.main:app --reload --port 8484
```

Stack: FastAPI, SQLite (WAL), httpx and Jinja. No JavaScript framework and no CDN, so it works fully offline on your LAN.
