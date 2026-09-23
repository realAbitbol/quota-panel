# Configuration reference

Everything the README leaves out. Start there for install and the quick start.

## Contents

- [docker compose (hardened)](#docker-compose-hardened)
- [Build from source](#build-from-source)
- [Config file fields](#config-file-fields)
- [Environment variables](#environment-variables)
- [Usage history](#usage-history)
- [Endpoints](#endpoints)
- [Homepage (gethomepage) integration](#homepage-gethomepage-integration)
- [Security details](#security-details)
- [Troubleshooting](#troubleshooting)

## docker compose (hardened)

```yaml
services:
  quota-panel:
    image: ghcr.io/realabitbol/quota-panel:latest
    restart: unless-stopped
    ports:
      - "127.0.0.1:8080:8080"
    volumes:
      - ./accounts.json:/config/accounts.json:ro
      - quota-history:/data
    read_only: true
    tmpfs:
      - "/tmp:rw,size=32m,noexec,nosuid,nodev"
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    mem_limit: 512m                  # artwork resize decodes the whole bitmap
    logging:
      driver: json-file
      options:
        max-size: "5m"
        max-file: "3"
    healthcheck:
      test: ["CMD", "python3", "-c", "import os,urllib.request,sys\nport=os.environ.get('PORT','8080')\ntry:\n    urllib.request.urlopen('http://127.0.0.1:'+port+'/api/health', timeout=4)\n    sys.exit(0)\nexcept Exception:\n    sys.exit(1)"]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 60s              # /api/health answers 503 until the first poll finishes

volumes:
  quota-history:
```

`docker-compose.yml` in this repo is the same service with fuller comments.

## Build from source

```bash
git clone https://github.com/realAbitbol/quota-panel.git
cd quota-panel
cp accounts.example.json accounts.json
docker build -t quota-panel .
docker run -d -p 127.0.0.1:8080:8080 \
  -v "$PWD/accounts.json:/config/accounts.json:ro" quota-panel
```

`python3 app.py` runs the same code outside a container; the only dependency is Pillow, for the artwork resize.

## Config file fields

`accounts.json`, mounted read-only at `/config/accounts.json`:

```json
{
  "poll_seconds": 60,
  "background_url": "https://example.com/wallpaper.jpg",
  "accounts": [
    { "id": "cc-work",     "provider": "commandcode",      "label": "CommandCode — work", "token": "user_…"   },
    { "id": "oc-personal", "provider": "opencode_go",      "label": "OpenCode Go — personal", "token": "sk-…" },
    { "id": "or-main",     "provider": "openrouter",       "label": "OpenRouter",         "token": "sk-or-…"  },
    { "id": "ci-main",     "provider": "cheaperinference", "label": "CheaperInference",   "token": "ci_live_…" }
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes, in practice | Identity key. Unique; don't change it once running. |
| `provider` | yes | One of the providers in the README table. Anything else is refused at startup. |
| `label` | no | Card title, defaults to `id`. |
| `token` / `token_env` / `token_file` | one of the three | The key, the *name* of an env var holding it, or a path to a file holding it. Precedence: `token` → `token_env` → `token_file`; none at all is a non-fatal `auth_error`. |
| `poll_seconds` | no | Seconds between polls, `10`–`3600`. Default `60`. Top level of the file, not per account. |
| `background_url` | no | Page artwork: an `http(s)` URL (top level of the file), `"none"` for no artwork, or absent for the default wallpaper (hotlinked). Fetched once, capped at 4K, re-encoded as WebP. |

`id` keys everything (`/api/quota`, the Homepage map, the error list). Set it explicitly — omitting it
falls back to a positional `<provider>-<index>`, and duplicates are fatal.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Listen port. |
| `QUOTA_CONFIG` | `/config/accounts.json` | Config path (inside the image; outside it, `./accounts.json`). |
| `QUOTA_POLL_SECONDS` | `60` | Poll interval **fallback** — `poll_seconds` in the config file wins. |
| `QUOTA_HTTP_TIMEOUT` | `20` | Per-request timeout, in seconds. |
| `QUOTA_CURRENCY` | `USD` | Currency reported for a multi-currency balance. |
| `QUOTA_BACKGROUND_URL` | the default wallpaper URL (hotlinked) | Artwork URL, or `none`/`off`. The config file wins. |
| `QUOTA_BACKGROUND_DIR` | `/tmp/quota-panel` | Where the fetched artwork is stored. |
| `QUOTA_BACKGROUND_TIMEOUT` | `20` | Artwork fetch timeout, in seconds. |

Every provider also has a base-URL override (`COMMANDCODE_API_BASE`, `OPENCODE_GO_USAGE_URL`, `Z_AI_API_BASE`, …) so the test suite can point an adapter at a local stub; nothing in production needs them.

## Usage history

`/history` charts usage over time from one SQLite file — a hand-rolled-SVG close reading of
[AIMeter](https://github.com/bugwz/AIMeter)'s Usage History that vendors no chart library.

Its panels: a trend with an alert-threshold line, reset markers and a projected cap date; weekly
bars; a share donut; an average/volatility scatter; a radar of average, latest, peak, volatility and
cost burn; a one-hue day map; per-account insight tiles; and a per-window breakdown for one account.

An account's colour and mark come from its provider; a day-or-longer range snaps to whole calendar
days; the window that stands for an account is the widest its label names. A money balance stays money
(its own tables, shown as a balance, drawdown-from-peak as the radar's **cost burn**), and the page
states under the controls what the store keeps and what the range is made of.

It ships on in `accounts.example.json` (`docker-compose.yml` mounts the `/data` volume it writes to):

```json
"history": { "enabled": true, "path": "/data/quota.db", "sample_seconds": 300,
             "raw_days": 90, "rollup_days": 365, "rollup_seconds": 900,
             "alert_percent": 80, "alert_url": "",
             "alert_retries": 3, "alert_backoff_seconds": 1 }
```

`sample_seconds` gates the row write; `raw_days`/`rollup_days`/`rollup_seconds` are retention.
Crossings of `alert_percent` are logged in the store's `alerts` table; with `alert_url` set a
background worker POSTs them as JSON with `alert_retries` retries and `alert_backoff_seconds` backoff,
retrying pending items after a restart — best-effort, so a dead webhook costs a log line, not a poll.
History settings also come from `QUOTA_HISTORY_ENABLED`, `QUOTA_DB`, `QUOTA_HISTORY_SAMPLE_SECONDS`,
`QUOTA_RETENTION_RAW_DAYS`, `QUOTA_RETENTION_ROLLUP_DAYS`, `QUOTA_ROLLUP_SECONDS`,
`QUOTA_HISTORY_ALERT_PERCENT`, `QUOTA_HISTORY_ALERT_URL`, `QUOTA_HISTORY_ALERT_RETRIES`,
`QUOTA_HISTORY_ALERT_BACKOFF_SECONDS`.

The store needs a **directory**, not a file (sqlite writes its journal beside the database): mount a
writable `/data` (`install -d -o 10001 -g 10001 ./data`). CIFS/NFS is refused. At 300 s sampling it is
~0.35 GB/year plus rollups, and a failed poll or unreadable window leaves a **gap**, never a zero.
`enabled: false` keeps no state — `/api/history` answers `404`.

## Endpoints

| Path | Purpose |
|---|---|
| `/` | the card UI |
| `/history` | usage over time |
| `/api/quota` | normalized JSON: every account, every window, with `resets_at` |
| `/api/history` | the time series (`hours`/`days`/`since`/`until`, `max_points`, `bucket_seconds`, `account_id`, `series`) |
| `/api/providers` | the provider registry: id, label, card kind, logo |
| `/api/homepage` | flat `items` map keyed `<account_id>_<window>`, for a gethomepage tile |
| `/api/health` | `200` while the last poll is fresh, `503` when stale |
| `/background` | the page artwork, `404` when off or the fetch failed |

## Homepage (gethomepage) integration

```yaml
    - Quotas:
        icon: https://raw.githubusercontent.com/realAbitbol/quota-panel/main/static/favicon.svg
        href: https://quota.example.com
        widget:
          type: customapi
          url: https://quota.example.com/api/homepage
          refreshInterval: 60000
          mappings:
            - field: items.cc-work_five_hour.value
              label: Work — 5 h
```

Each item also carries `percent` and `resets_at`, so a tile can render a progress bar instead of a bare value.

## Security details

* Read-only, outbound only: a `GET` per provider call (CommandCode issues up to four), a startup artwork fetch that retries until it succeeds, and — only when `alert_url` is set — a `POST` per alert. No write path.
* Credentials are read at startup, held in memory, never logged or returned by any endpoint, and sent
  only to the provider that owns them. The suite's stub echoes the `Authorization` header it received;
  no served body may contain it.
* The pages are served under a Content-Security-Policy whose `script-src` names the SHA-256 of each
  inline block (no `'unsafe-inline'` for scripts), plus `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer` and a restrictive `Permissions-Policy`.
* Static serving containment-checks every path against a fixed extension list.

## Troubleshooting

**`no account has a credential configured`, every card is an error.** A key was written into `token_env`. Move it to `token`.

**Docker created a directory where `accounts.json` should be.** The bind mount pointed at a missing path. Remove the directory, write the file, start again.

**One card says the provider rejected the key.** `401` is a bad or revoked key; `403` is usually a valid key missing a scope or a plan (CheaperInference needs `account:read`; on OpenCode Go, `403` means no Go plan). `python3 app.py --check` gives the same answer without the UI.

**Everything is red after moving host and the config looks right.** Check the path *inside* the container, and the permissions: the process runs as uid 10001 and cannot read a `600` file owned by someone else.

**The wallpaper never appears.** `/api/health` reports `background.served: none` and the last error.

**`History is enabled but not readable … (unable to open database file)`.** The database file is writable, its *directory* is not. Mount a directory at `/data` rather than a single file, restart.
