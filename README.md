# 🪼 JellyCreep

**Tautulli-style monitoring for Jellyfin**, built for homelabs where everything sits behind **Authentik MFA**, and designed first for your **phone**.

- **Live activity:** who's watching what, direct play vs transcode (with codecs, resolution, bitrate and the transcode reason), progress, IP and device. Admins can stop a stream and send the user a message.
- **Watch history it records itself.** It polls Jellyfin's sessions like Tautulli does, counting play time minus pauses. It survives restarts and supports several Jellyfin servers.
- **Users and stats:** top users, movies and shows, plays per day, peak hours, play methods and clients.
- **Automation in one place:** Sonarr/Radarr/Lidarr queues, upcoming calendar, health and disk space; SABnzbd and qBittorrent downloads (failed jobs flagged); Prowlarr indexers; Seerr requests.
- **Invite and manage users with Wizarr:** create an invite on your phone (server, libraries, expiry, access length, downloads/Live TV), then send it through your phone's share sheet. You can also extend, disable or delete Wizarr users.
- **Events timeline:** grabs, imports, requests and health alerts pushed in by webhooks from the *arrs, Seerr and Jellyfin.
- **Connects to your existing Jellystat.** Its stats show up inside JellyCreep, with a link to open its own UI.
- **Every app is a connection, not a container.** Jellyfin, Jellystat, Wizarr, Sonarr, Radarr, Lidarr, Prowlarr, SABnzbd, qBittorrent and Seerr are your existing installs. You add each one by URL and API key, from Settings or from stack variables.
- **Works with apps behind Authentik.** Each upstream app can be reached through Authentik with a service account (app password or OAuth2 client credentials), or with custom headers for Cloudflare Access and similar.
- **Phone-first UI:** bottom tab bar, card layouts, large tap targets, dark theme. *Add to Home Screen* makes it open full-screen like an app.
- **Act from your phone:** approve or decline Seerr requests, pause/resume SABnzbd and qBittorrent (all or per item), retry or remove stuck queue items, and search & add shows/movies to Sonarr/Radarr.
- **Push notifications** to ntfy, Pushover, Discord or any webhook — transcodes, failed downloads, apps going down, new requests, new users.
- **Per-user pages** with favourite shows/movies, devices, watch-time chart, heatmap, streaks and biggest binge.
- **Concurrent-stream stats** (daily peak streams and transcodes) and a local-time viewing heatmap.
- **Import history from Jellystat** so stats go back before JellyCreep was installed.
- **Jellyfin-style glass UI**: frosted panels over a blurred backdrop of what's playing (or last watched), Jellyfin's purple→blue palette, light/dark/auto theme, a per-device "Glass effects" switch for slower phones, and pull-to-refresh.
- **Jellyfin user sign-in** with a personal, privacy-safe dashboard (their own watching, stats, history, upcoming and requests — no other users, no IPs, no admin pages).
- **Jellyfin-style hero on Activity**: full-width backdrop of what's playing with the title logo, poster, progress and "ends at" time — swipe between streams; shows the last thing watched when idle.
- **Library quality report** (Media → Quality): how much is 4K/1080p/720p, HDR10/HDR10+/Dolby Vision, video and audio codecs, channels, containers, image subtitles and bitrates — plus a transcode outlook that ties those to the real reasons your streams transcoded and each client's direct-play rate.
- **Storage on the Media tab**: every drive Jellyfin (10.11+) and Sonarr/Radarr/Lidarr can see, merged per physical disk, with free space, which libraries live where, and a "full in ~N days" estimate from daily readings.
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
   | `JELLYCREEP_SECRET_KEY` | optional: `openssl rand -base64 48`. If you leave it out, one is generated and saved in the data volume |
   | `JELLYCREEP_BASE_URL` | `https://jellycreep.example.com` |
   | `JELLYFIN_URL` / `JELLYFIN_API_KEY` | e.g. `http://jellyfin:8096` / key from Dashboard → API Keys |

   Every other app is optional, and the full list is in [`.env.example`](.env.example). The easiest route is to deploy first and then add apps from **Settings → Add a service** on your phone. Each one has a **Save & test** button.
4. **Deploy.** The stack runs one container, `jellycreep` (port 8484). It reaches your other containers by name over the shared `proxy` network, or by any URL you give it.

### Connecting your apps

| App | URL example | Credential |
|---|---|---|
| Jellyfin | `http://jellyfin:8096` | Dashboard → API Keys |
| Jellystat | `http://jellystat:3000` | Jellystat → Settings → API Keys |
| Wizarr | `http://wizarr:5690` | Wizarr → Settings → API Keys |
| Sonarr / Radarr / Lidarr / Prowlarr | `http://sonarr:8989` or `https://sonarr.example.com` | Settings → General → API Key |
| SABnzbd | `http://sabnzbd:8080` | Config → General → **API Key** (not the NZB key) |
| qBittorrent | `http://qbittorrent:8080` | Web UI username + password |
| Jellyseerr / Seerr | `http://jellyseerr:5055` | Settings → General → API Key |

Any of these can also sit behind Authentik; see below.

> The image is built by GitHub Actions to `ghcr.io/rivarob04/jellycreep`. GHCR packages start out private, so either make the package public (GitHub → Packages → jellycreep → Package settings) or let Dockhand build it. The compose file has `build: .` as a fallback.

Plain Docker works the same way: `cp .env.example .env`, edit it, then `docker compose up -d`.

---

## Putting JellyCreep behind Authentik

### Option A: proxy provider / forward auth (recommended)

**nginx:** a ready-to-use config is in [`deploy/nginx/jellycreep.conf`](deploy/nginx/jellycreep.conf). It protects the UI with Authentik and leaves `/api/` open for token-authenticated webhooks, so you don't need Authentik's Unauthenticated Paths with it.


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

## Letting Jellyfin users sign in

Your Jellyfin users can get their own dashboard by signing in with their **Jellyfin username and password**. It shows what they're watching, their stats and streaks, their history, upcoming episodes, what's new (only from libraries they can access) and the status of their Seerr requests.

They never see other users, IP addresses, devices other than their own, downloads, storage, settings or any admin page. The server enforces this with default-deny: viewers are redirected to `/me`, and admin APIs return 403. A Jellyfin *administrator* who signs in this way is also only a viewer. Admin access always goes through Authentik and MFA.

When a viewer's stream is being transcoded, JellyCreep explains why in plain English ("These subtitles have to be burned into the picture", "Your device can't play this audio track (TRUEHD)") and adds a tip they can act on. Their history marks each converted play with its reason, and the home page sums up how many recent plays were converted, the most common cause and the device it happened on.

### Phone notifications (everyone)

Tap the **🔔** in the top bar (or **Notifications** in the menu), then tap **Turn on for this device**. JellyCreep then sends real notifications with its own icon to that phone or computer, even while the app is closed, using Web Push. Nothing else is needed: no ntfy, no extra app, and no Firebase account.

- **Jellyfin users** can be told when:
  - their requests are approved, declined or ready to watch
  - a new episode of a show they've watched (last 6 months) is added
  - anything new is added (off by default)
  - you send an announcement

  They're only told about things their Jellyfin account can access.
- **Admins and members** get the same alerts as ntfy/Pushover (new Seerr request, download failed, app down/back, new Wizarr user, transcodes, health). They can also get "new movies & episodes" and announcements. Each person picks their own alerts.
- **Admins** can send an **announcement** to everyone, Jellyfin users only, or staff only.
- Tapping a notification opens the right page. Signing out on a phone turns that phone's notifications off, so a shared device doesn't get the previous person's alerts.

Requirements: JellyCreep must be opened over **https://**. The container also needs outbound HTTPS to the browser push services (`fcm.googleapis.com` for Chrome/Android, `*.push.apple.com` for Safari, `updates.push.services.mozilla.com` for Firefox). On iPhone, people must first **Add to Home Screen** (iOS 16.4+). The signing keys are created automatically and kept in the data volume. Set `VAPID_SUBJECT=mailto:you@example.com` if you want a contact address on them.

### Navigation on phones

On phones and tablets, tap **☰** in the top-left corner, or swipe in from the left edge, to open the slide-out menu. It lists every page (the current one highlighted), plus the theme and glass switches and Sign out. Tap outside it, swipe it left or press Esc to close it. Wide screens keep the menu bar across the top.

### Recently added & library pages (like Tautulli Remote)

Everyone gets a **Recently added** list. Admins and members find it under `/library`; Jellyfin users find it under **More → Recently added**, or tap **All →** on their home page. Filter by All, Movies, Shows or Music. Tap anything to open its page:

- **Details:** tagline, overview, network or studio, air date, runtime, rating, genres, director, writers, cast, the **Rotten Tomatoes** critics score (🍅 fresh / 🤢 rotten) and audience rating, IMDb/TMDB/TVDB links, and **file info** for movies and episodes (container, size, bitrate, video, every audio and subtitle track).
- **Seasons:** a poster grid for series. Tap a season for its **Episodes** grid, then tap an episode for its own details and file info.
- **History:** who watched it and when. Jellyfin users see only their own plays.

Jellyfin users browse *as themselves*, so they only see libraries their Jellyfin account can access. File paths are only shown to staff. History rows and home-page posters link straight to these pages.

Rotten Tomatoes and audience scores come from Jellyfin's metadata. If they're missing, enable a metadata provider that supplies critic ratings (e.g. the OMDb plugin) and refresh the library.

### Requesting with Seerr (like Moonfin)

If Seerr is connected, signed-in Jellyfin users also get **Discover** and **Requests** tabs:

- **Discover**: rows for Trending, Popular movies, Popular series, Upcoming movies and Upcoming series, plus search. Each poster shows whether the title is available, requested or pending.
- **Title page**: backdrop, overview, trailer, a **Request** button (and **Request in 4K** when the person is allowed and you have a 4K Radarr/Sonarr), a season picker for series, "Who requested it", and "More like this".
- **Requests**: everyone's requests with the requester's name, status, seasons, 4K and download progress. Filters: All, Waiting, Approved, Available. People can cancel their own pending requests. Seerr managers can approve or decline.
- The home page and Discover start with a **Recently requested** row.

**How it works.** JellyCreep matches each person to their own Seerr account (Jellyfin user ID first, then username). It sends their requests with your Seerr API key and the header `X-API-User`, so Seerr records them as the requester and applies their permissions, quotas and auto-approve rules. They never need a separate Seerr password. Only display names are shown, never email addresses.

Settings → Sign-in → **Seerr** has three switches:
- Discover & request.
- Show who requested what. Turn it off to show people only their own requests.
- Create Seerr accounts automatically for Jellyfin users who don't have one. The alternative is to import them yourself in Seerr → Users → Import Jellyfin users.

1. **Settings → Sign-in → Allow Jellyfin users to sign in**, then pick what users can see. Or set `JELLYFIN_LOGIN=true`.
2. **nginx:** let the user pages bypass Authentik. Everything else stays protected. Add the following to your JellyCreep server block, above `location /`. It's also in [`deploy/nginx/jellycreep.conf`](deploy/nginx/jellycreep.conf).
   ```nginx
   location ~ ^/(login|logout|me|partials/me)(/|$) {
       proxy_set_header X-authentik-username "";
       proxy_set_header X-authentik-groups   "";
       proxy_set_header X-authentik-email    "";
       proxy_pass http://jellycreep;
   }
   location /static/ { proxy_pass http://jellycreep; }
   ```
3. Send users **`https://<your-jellycreep>/me`**. They'll get the sign-in page, and can then *Add to Home Screen*.

How it works:
- Passwords are checked by Jellyfin (`/Users/AuthenticateByName`) and never stored.
- The short-lived Jellyfin session created by the check is logged out straight away, so it doesn't clutter Jellyfin's device list.
- Failed attempts are rate-limited per IP and per username.
- Every few hours JellyCreep re-checks that the account still exists and isn't disabled. If it is gone or disabled, they're signed out.

## Inviting users (Wizarr)

Connect Wizarr in **Settings**, then open **More → Invite a user** (admins only):

1. Pick the media server, how long the invite link stays valid (1, 7 or 30 days, or never) and how long the account lasts (blank means unlimited).
2. Optionally restrict libraries and allow downloads or Live TV.
3. Tap **Create invite**, then **Share…**. Your phone's share sheet opens so you can send it by Messages, WhatsApp or email. On desktop the link is copied instead.

The same page lists every invitation with its status (pending, used or expired) and every Wizarr user. From there you can **extend** a user by N days, **disable**, **enable** or **delete** them.

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
