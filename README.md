# emptyarr

Plex doesn't automatically clean up its library trash when you're using symlinked debrid or usenet media. When a file gets replaced or removed, Plex marks it unavailable — but unless you have "empty trash automatically after every scan" turned on (which you probably don't, because that's risky), those entries just pile up.

emptyarr runs on a schedule, checks that your mounts are actually healthy, and then calls Plex's emptyTrash API. If anything looks wrong — mount missing, symlinks broken, file count dropped — it skips the empty and optionally pings you on Discord.

---

## How it works

Before emptying trash on any library, emptyarr runs:

1. **Mount check** — walks up the path tree to find the nearest mount point and verifies it's accessible
2. **Debrid mount check** — for debrid/usenet paths, reads symlink targets via `os.readlink()` (without resolving them), finds the underlying FUSE mount point, and verifies it is accessible and non-empty. This detects a dead mount even when symlinks point into trash and would otherwise appear broken
3. **File threshold** — compares the count of files on disk to your Plex library count. If the ratio drops below your configured threshold (default 90%), something's wrong and it bails
4. **Combined check** — for mixed libraries (physical + debrid), sums all paths and checks the combined ratio

All checks pass → trash gets emptied. Any check fails → skip, log it, notify if configured.

---

## Setup (Unraid)

### Build

```bash
mkdir -p /mnt/cache/appdata/emptyarr/data
cd /mnt/cache/appdata/emptyarr
git clone https://github.com/LiftbridgeLabs/emptyarr.git .
docker build -t emptyarr:latest .
```

### Container settings

**Network:** your arr network (e.g. `arr_net`)  
**Port:** `8222`

**Path mappings:**

| Host | Container | Mode |
|---|---|---|
| `/mnt/cache/appdata/emptyarr/data` | `/app/data` | Read/Write |
| `/mnt/symlink_media` | `/symlink_media` | Read Only - Slave |
| `/mnt/user/media` | `/mnt/user/media` | Read Only |

> The container path for symlink media needs to match what your symlinks actually point to. Check with `ls -la /mnt/symlink_media/symlinks/radarr/ | head -3` and look at the symlink targets. If they start with `/symlink_media/` (no `/mnt`), use that as the container path.

> **Slave propagation required for FUSE mounts:** The symlink media volume must use `slave` propagation (`:ro,slave`) so that FUSE mounts created by tools like Decypharr or zurg after the container starts are visible inside the container. Without `slave`, the container sees a stale snapshot of the host mount namespace and the FUSE filesystem will appear empty or missing.

**Container runtime variables:**

| Variable | Default | Description |
|---|---|---|
| `PUID` | — | User ID for file permissions. `99` on Unraid (nobody) |
| `PGID` | — | Group ID for file permissions. `100` on Unraid (users) |
| `TZ` | — | Timezone, e.g. `America/New_York` |
| `CONFIG_PATH` | `data/config.yml` | Path to the config file |
| `LOG_DIR` | `data/logs` | Directory where log files are written |
| `BROWSE_ROOTS` | `/mnt,/media,/data,/home` | Comma-separated list of root paths the file browser is allowed to enter |
| `FLASK_HOST` | `127.0.0.1` | Network interface to bind to. Set to `0.0.0.0` if you need external access or are using a reverse proxy |
| `SESSION_COOKIE_SECURE` | `false` | Set to `true` when serving over HTTPS — marks the session cookie as Secure so it's never sent over plain HTTP |

Plex tokens, provider keys, Discord notifications, web authentication, schedules,
and logging are all configurable in the UI and persist in `data/config.yml`.
The session key is generated once and persisted automatically in the data
directory.

Non-empty environment variables such as `PLEX_TOKEN_<NAME>`, `RD_API_KEY`,
`DISCORD_WEBHOOK`, or `EMPTYARR_USERNAME` remain supported as optional
deployment-managed overrides. A Compose `.env` file only supplies those
environment overrides; it is not emptyarr's primary configuration file.

### First run

Open `http://YOUR_IP:8222` and run through the setup wizard. You can connect
your Plex account in the browser to discover servers and libraries automatically;
emptyarr never receives your Plex password. Manual URL/token setup remains
available as a fallback.

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

Per-library. Standard cron syntax. `0 * * * *` runs every hour on the hour, `*/30 * * * *` every 30 minutes.

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

plex_instances:
  - name: My Plex
    url: http://192.168.1.100:32400
    token: ''
    libraries:
      - name: Movies
        type: physical
        cron: "0 * * * *"
        paths:
          - path: /mnt/user/media/movies
            type: physical
            min_threshold: 90
      - name: TV Shows
        type: physical
        cron: "0 * * * *"
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
          - path: /symlink_media/symlinks/radarr
            type: debrid
            min_threshold: 90
      - name: TV Shows
        type: debrid
        cron: "0 * * * *"
        paths:
          - path: /symlink_media/symlinks/sonarr
            type: debrid
            min_threshold: 90
```

---

## Auth

Settings → Security. Enter username and password, save. Takes effect immediately, no restart needed. Stored as a bcrypt hash in config.yml — never plaintext.

You can also set `EMPTYARR_USERNAME` and `EMPTYARR_PASSWORD` env vars instead (these take priority).

---

## Notifications

Five separate Discord notification events you can toggle independently:

- **Trash emptied** — something was actually removed
- **Health check failed** — checks didn't pass, empty was skipped
- **Error** — the emptyTrash API call failed
- **Already clean** — ran fine, nothing to remove (off by default — gets noisy)
- **Skipped** — scheduling paused, config error, section not found (off by default)

---

## Updating

```bash
cd /mnt/cache/appdata/emptyarr
git pull
docker build -t emptyarr:latest .
# restart in Unraid UI
```

---

## Privacy

emptyarr talks to your Plex server, Plex's authorization/discovery service when
you choose account linking, configured debrid provider APIs, and your Discord
webhook. It sends no telemetry or analytics. See [PRIVACY.md](PRIVACY.md).

---

## License

MIT
