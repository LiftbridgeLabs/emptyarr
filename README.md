# emptyarr

Plex doesn't automatically clean up its library trash when you're using symlinked debrid or usenet media. When a file gets replaced or removed, Plex marks it unavailable — but unless you have "empty trash automatically after every scan" turned on (which you probably don't, because that's risky), those entries just pile up.

emptyarr runs on a schedule, checks that your mounts are actually healthy, and then calls Plex's emptyTrash API. If anything looks wrong — mount missing, symlinks broken, file count dropped — it skips the empty and can notify you through Discord or Apprise.

---

## How it works

Before emptying trash on any library, emptyarr runs:

1. **Mount check** — walks up the path tree to find the nearest mount point and verifies it's accessible
2. **Debrid mount check** — for debrid/usenet paths, reads symlink targets via `os.readlink()` (without resolving them), finds the underlying FUSE mount point, and verifies it is accessible and non-empty. This detects a dead mount even when symlinks point into trash and would otherwise appear broken
3. **File threshold** — compares the count of files on disk to your Plex library count. If the ratio drops below your configured threshold (default 90%), something's wrong and it bails
4. **Combined check** — for mixed libraries (physical + debrid), sums all paths and checks the combined ratio

All checks pass → trash gets emptied. Any check fails → skip, log it, notify if configured.

---

## Installation

### Unraid WebUI (recommended)

Emptyarr is distributed as a prebuilt Docker image. A normal Unraid installation
does not require the terminal, a Git checkout, or a local image build.

If an Emptyarr template is available in your Apps feed, install it there.
Otherwise, open **Docker → Add Container** and configure:

| Setting | Value |
|---|---|
| Name | `emptyarr` |
| Repository | `liftbridgelabs/emptyarr:latest` |
| Network | `bridge` or the custom Docker network used by your media applications |
| Container port | `8222` |
| Host port | `8222` or another available port |
| WebUI | `http://[IP]:[PORT:8222]/` |

Add these path mappings in the container editor:

**Path mappings:**

| Host | Container | Mode |
|---|---|---|
| `/mnt/cache/appdata/emptyarr/data` | `/app/data` | Read/Write |
| `/mnt/symlink_media` | `/mnt/symlink_media` | Read Only - Slave |
| `/mnt/user/media` | `/mnt/user/media` | Read Only |

The host paths are examples; select the paths used by your own Unraid setup.
Container paths are what Emptyarr displays and saves in its library settings.

> The container path for symlink media must match what the symlinks actually
> point to. For example, if their targets begin with `/symlink_media/`, use
> `/symlink_media` as the container path instead of `/mnt/symlink_media`.

> **Slave propagation required for FUSE mounts:** The symlink media volume must use `slave` propagation (`:ro,slave`) so that FUSE mounts created by tools like Decypharr or zurg after the container starts are visible inside the container. Without `slave`, the container sees a stale snapshot of the host mount namespace and the FUSE filesystem will appear empty or missing.

Add the following variables under **Add another Path, Port, Variable, Label or
Device → Variable**:

| Variable | Default | Description |
|---|---|---|
| `PUID` | — | User ID for file permissions. `99` on Unraid (nobody) |
| `PGID` | — | Group ID for file permissions. `100` on Unraid (users) |
| `TZ` | — | Timezone, e.g. `America/New_York` |
| `CONFIG_PATH` | `data/config.yml` | Path to the config file |
| `LOG_DIR` | `data/logs` | Directory where log files are written |
| `BROWSE_ROOTS` | `/mnt,/media,/data,/home` | Comma-separated list of root paths the file browser is allowed to enter |
| `SESSION_COOKIE_SECURE` | `false` | Set to `true` when serving over HTTPS — marks the session cookie as Secure so it's never sent over plain HTTP |

`PUID=99`, `PGID=100`, and your local `TZ` are the only variables most Unraid
installations need. After selecting **Apply**, open the WebUI from the Docker
page.

Plex tokens, provider keys, Discord notifications, web authentication, schedules,
and logging are all configurable in the UI and persist in `data/config.yml`.
The session key is generated once and persisted automatically in the data
directory.

Non-empty environment variables such as `PLEX_TOKEN_<NAME>`, `RD_API_KEY`,
`DISCORD_WEBHOOK`, or `EMPTYARR_USERNAME` remain supported as optional
deployment-managed overrides. A Compose `.env` file only supplies those
environment overrides; it is not emptyarr's primary configuration file.

### Docker Compose

For Unraid Compose Manager or any other Compose installation, create a Compose
file using the published image:

```yaml
services:
  emptyarr:
    image: liftbridgelabs/emptyarr:latest
    container_name: emptyarr
    restart: unless-stopped
    ports:
      - "8222:8222"
    environment:
      PUID: "99"
      PGID: "100"
      TZ: America/Denver
    volumes:
      - /mnt/cache/appdata/emptyarr/data:/app/data
      - /mnt/symlink_media:/mnt/symlink_media:ro,slave
      - /mnt/user/media:/mnt/user/media:ro
```

Change the timezone, port, and host paths for your system. If Emptyarr must
reach Plex by container name, attach it to the same custom Docker network as
Plex.

### First run

Open `http://YOUR_IP:8222` and run through the setup wizard. You can connect
your Plex account in the browser to discover servers and libraries automatically;
emptyarr never receives your Plex password. Manual URL/token setup remains
available as a fallback.

---

## Building from source

Local builds are intended for development or testing changes that are not yet
in the published image:

```bash
git clone https://github.com/LiftbridgeLabs/emptyarr.git
cd emptyarr
docker build -t emptyarr:local .
```

Normal Unraid and Compose installations should use
`liftbridgelabs/emptyarr:latest` instead.

---

## Configuration

Config lives at `/app/data/config.yml` (your host's data directory). The Settings
page can validate and apply changes immediately, including additions, removals,
schedules, paths, credentials, notifications, authentication, and log level.
Restart only after changing container runtime variables or volume mappings.

### Library types

- **physical** — standard files on disk
- **debrid** — symlinked content (Real-Debrid, AllDebrid, etc.)
- **usenet** — usenet downloads with symlinks
- **mixed** — combination of physical and debrid in the same Plex library

For mixed libraries the file threshold check combines all paths before comparing to your Plex count, so individual paths don't need to hold the full library.

### Threshold

`min_threshold` is the percentage of your Plex library count that must exist on disk. Default is 90. If you have 1000 movies in Plex and only 850 files on disk, that's 85% — below 90%, so the empty gets skipped.

### Cron schedules

The Settings page provides a global default plus optional per-library
overrides. Libraries without a `cron` value inherit `schedule.default_cron`.
`0 * * * *` runs every hour on the hour and `*/30 * * * *` runs on the next
half-hour boundary. A daily time selected in the UI is evaluated in the
container timezone (`TZ`).

There is no automatic run immediately at startup. The first run is the next
clock time matching the effective schedule, and the dashboard shows that
countdown as soon as the scheduler starts. **Run now** remains available when
an immediate safety run is wanted.

### Example config

```yaml
log_level: INFO
discord_webhook: https://discord.com/api/webhooks/...
notify:
  on_emptied: true
  on_health_fail: true
  on_error: true
  on_clean: false
  on_skip: false

# Optional. Plex Clean Bundles is server-wide, so it is disabled by default.
# Enable only if you intentionally want it before each library trash operation.
clean_bundles_before_empty: false

# Abort unusually large empty-trash runs (0 disables a limit)
max_trash_items: 1000
max_trash_percent: 25

schedule:
  default_cron: "0 * * * *"

plex_instances:
  - name: My Plex
    url: http://192.168.1.100:32400
    token: ''
    libraries:
      - name: Movies
        type: physical
        paths:
          - path: /mnt/user/media/movies
            type: physical
            min_threshold: 90
      - name: TV Shows
        type: physical
        cron: "*/30 * * * *"  # optional per-library override
        paths:
          - path: /mnt/user/media/tv
            type: physical
            min_threshold: 90

  - name: My Plex Unlimited
    url: http://192.168.1.100:32410
    token: ''
    libraries:
      - name: Movies
        type: mixed
        cron: "0 * * * *"
        paths:
          - path: /mnt/user/media/movies
            type: physical
            min_threshold: 90
          - path: /mnt/symlink_media/symlinks/radarr
            type: debrid
            min_threshold: 90
      - name: TV Shows
        type: debrid
        cron: "0 * * * *"
        paths:
          - path: /mnt/symlink_media/symlinks/sonarr
            type: debrid
            min_threshold: 90
```

---

## Auth

Settings → Security. Enter username and password, save. Takes effect immediately, no restart needed. Stored as a bcrypt hash in config.yml — never plaintext.

You can also set `EMPTYARR_USERNAME` and `EMPTYARR_PASSWORD` env vars instead (these take priority).

API access uses a separate random token; the login password hash is never an
API credential. Generate or rotate the token under Settings â†’ Security and
copy it when shownâ€”emptyarr stores only its hash and cannot display it again.
You may alternatively set `EMPTYARR_API_TOKEN` as an environment override.

The API token is useful for Home Assistant, scripts, health monitors, and
external dashboards. Send it in the `X-API-Token` header to read endpoints such
as `/api/status`, `/api/history`, and `/api/logs`, or to trigger an authorized
run without storing the UI password:

```bash
curl -H "X-API-Token: YOUR_TOKEN" http://EMPTYARR:8222/api/status
```

## Logs

Settings → General → Logging contains the running log viewer and all rotated
log files. Select a prior file to view it or download it. The active file is
`emptyarr.log`; rotations use names such as `emptyarr.1.log` and
`emptyarr.2.log`.

Retention is configured in understandable storage and time units:

- **Rotate each file at** controls the size of an individual file in MB.
- **Maximum total log storage** caps all log files combined in MB.
- **Keep rotated logs for** removes old files after the selected number of days.

The oldest rotated files are removed when either the storage or age limit is
reached. Defaults are 5 MB per file, 50 MB total, and 14 days. Logs remain under
the persistent `LOG_DIR` (`data/logs` by default) and are also written to the
container console for Docker/Unraid.

Logs record scheduled/manual runs, safety checks, skipped operations, Plex
actions and results, configuration changes, provider failures, and operational
errors. Emptyarr does not intentionally log passwords, Plex tokens, provider
keys, or API tokens.

---

## Notifications

Emptyarr supports native Discord embeds plus named Apprise destinations. Friendly
presets in Settings cover Telegram, ntfy, Gotify, email/SMTP, Pushover, and
generic webhooks; the custom preset accepts any
[Apprise service URL](https://appriseit.com/services/).

Each Apprise destination can be enabled independently, tested before saving, and
routed to its own selection of events. The global event controls are master
switches for both Discord and Apprise:

- **Trash emptied** — something was actually removed
- **Health check failed** — checks didn't pass, empty was skipped
- **Error** — the emptyTrash API call failed
- **Already clean** — ran fine, nothing to remove (off by default — gets noisy)
- **Skipped** — scheduling paused, config error, section not found (off by default)

Notification delivery runs outside the library operation so a slow or unavailable
notification provider cannot block trash-protection work. Destination URLs often
contain credentials and are stored in `config.yml`; keep the file private.

Quiet hours, failure/recovery notifications, daily summaries, and digest routing
are planned after the first destination release is proven stable.

---

## Updating

### Unraid WebUI

From the **Docker** page, use **Check for Updates**, then apply the update for
Emptyarr. Unraid pulls the current `liftbridgelabs/emptyarr:latest` image and recreates
the container while preserving everything mapped to `/app/data`.

### Docker Compose

```bash
docker compose pull emptyarr
docker compose up -d emptyarr
```

---

## Privacy

emptyarr talks to your Plex server, Plex's authorization/discovery service when
you choose account linking, configured debrid provider APIs, and notification
services you configure. It sends no telemetry or analytics. See
[PRIVACY.md](PRIVACY.md).

---

## License

MIT
