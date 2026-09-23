<p align="center">
  <img src="static/favicon.svg" width="84" alt="quota-panel">
</p>

<h1 align="center">Quota Panel</h1>

<p align="center">A small read-only dashboard that shows <b>how much of your AI coding subscription is left</b>, right now, for every account you have, with a live countdown to each reset.</p>

<p align="center">
  <a href="https://github.com/realAbitbol/quota-panel/actions/workflows/ci.yml"><img alt="ci: passing" src="https://github.com/realAbitbol/quota-panel/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/realAbitbol/quota-panel/pkgs/container/quota-panel"><img alt="ghcr.io/quota-panel" src="https://img.shields.io/badge/ghcr.io-quota--panel-2496ED?logo=docker&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="license: MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
  <a href="https://www.python.org/"><img alt="python: 3.12" src="https://img.shields.io/badge/python-3.12-3776AB?logo=python&amp;logoColor=white"></a>
</p>

![quota-panel dashboard](docs/screenshot.png)

![usage history](docs/history.png)

*Screenshots use synthetic data.*

Any number of accounts, any mix of providers — each gets its own card. An account whose credential is
missing or refused renders as an error card and never takes the panel down.

### Supported providers

| | `provider` | Card | Reports |
|---|---|---|---|
| <img src="static/logos/commandcode.png" height="22" alt="CommandCode"> | `commandcode` | window | 5 h / weekly / monthly percent |
| <img src="static/logos/opencode_go.svg" height="22" alt="OpenCode"> | `opencode_go` | window | rolling / weekly / monthly percent |
| <img src="static/logos/zai.svg" height="22" alt="z.ai"> | `zai` | window | session / weekly / web-search quota |
| <img src="static/logos/synthetic.svg" height="22" alt="Synthetic"> | `synthetic` | window | request allowance percent |
| <img src="static/logos/openrouter.svg" height="22" alt="OpenRouter"> | `openrouter` | balance | credit balance; percent only if the key has a spend limit |
| <img src="static/logos/cheaperinference.svg" height="22" alt="CheaperInference"> | `cheaperinference` | balance | wallet balance |
| <img src="static/logos/deepseek.png" height="22" alt="DeepSeek"> | `deepseek` | balance | balance in CNY or USD |
| <img src="static/logos/kimi.svg" height="22" alt="Kimi"> | `kimi` | balance | available / voucher / cash balance |

## Install

```bash
curl -o accounts.json \
  https://raw.githubusercontent.com/realAbitbol/quota-panel/main/accounts.example.json
$EDITOR accounts.json            # one entry per account

docker run -d \
  --name quota-panel \
  --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v "$PWD/accounts.json:/config/accounts.json:ro" \
  -v quota-panel-data:/data \
  ghcr.io/realabitbol/quota-panel:latest
```

Write the config first: the container polls nothing without it, and a bind mount onto a missing path
leaves a *directory* in its place. Then open <http://localhost:8080>.

The repo's `docker-compose.yml` is this service with the hardening (read-only rootfs, `cap_drop: ALL`,
`no-new-privileges`, healthcheck) and fuller comments; the `Dockerfile` builds the same code.

## Configuration

`accounts.json`, mounted read-only at `/config/accounts.json`:

```json
{
  "poll_seconds": 60,
  "background_url": "https://example.com/wallpaper.jpg",
  "accounts": [
    { "id": "cc-work", "provider": "commandcode", "label": "CommandCode — work", "token": "user_…" },
    { "id": "or-main", "provider": "openrouter",  "label": "OpenRouter",         "token": "sk-or-…" }
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes, in practice | Identity key. Unique; don't change it once running. |
| `provider` | yes | One of the providers above. Anything else is refused at startup. |
| `label` | no | Card title, defaults to `id`. |
| `token` / `token_env` / `token_file` | one of the three | The key, the *name* of an env var holding it, or a path to a file holding it. Precedence: `token` → `token_env` → `token_file`; none at all is a non-fatal `auth_error`. |
| `poll_seconds` | no | Seconds between polls, `10`–`3600`. Default `60`. Top level of the file, not per account. |
| `background_url` | no | Page artwork: an `http(s)` URL (top level of the file), `"none"` for no artwork, or absent for the default wallpaper (hotlinked). Fetched once, capped at 4K, re-encoded as WebP. |

`id` keys everything (`/api/quota`, the Homepage map, the error list), so set it explicitly: the
positional fallback is `<provider>-<index>`, and duplicates are fatal.

> **Trap:** the key goes in `token`. `token_env` holds the variable's *name*, so a key put there
> renders every lookup as `""`.

### Environment variables

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

Every provider also has a base-URL override (`COMMANDCODE_API_BASE`, `OPENCODE_GO_USAGE_URL`,
`Z_AI_API_BASE`, …) for pointing an adapter at a local stub; production never needs them.

### Usage history

`/history` charts usage over time from one SQLite file — hand-rolled SVG, no chart library. It ships on
in `accounts.example.json`:

```json
"history": { "enabled": true, "path": "/data/quota.db", "sample_seconds": 300,
             "raw_days": 90, "rollup_days": 365, "rollup_seconds": 900,
             "alert_percent": 80, "alert_url": "",
             "alert_retries": 3, "alert_backoff_seconds": 1 }
```

`sample_seconds` gates the row write; `raw_days`/`rollup_days`/`rollup_seconds` are retention.
Crossings of `alert_percent` are logged, and with `alert_url` set a background worker POSTs them as
JSON, retrying after a restart — best-effort. All of it is also settable by environment
(`QUOTA_HISTORY_ENABLED`, `QUOTA_DB`, `QUOTA_HISTORY_SAMPLE_SECONDS`, `QUOTA_RETENTION_RAW_DAYS`,
`QUOTA_RETENTION_ROLLUP_DAYS`, `QUOTA_ROLLUP_SECONDS`, `QUOTA_HISTORY_ALERT_*`).

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
widget:
  type: customapi
  url: https://quota.example.com/api/homepage
  refreshInterval: 60000
  mappings:
    - field: items.cc-work_five_hour.value
      label: Work — 5 h
```

Each item also carries `percent` and `resets_at`, so a tile can render a progress bar instead of a bare value.

## Security

* Read-only, outbound only: a `GET` per provider call (CommandCode issues up to four), one artwork
  fetch at startup, and — only when `alert_url` is set — a `POST` per alert. No write path.
* Credentials are read at startup, held in memory, never logged or returned by any endpoint, and sent
  only to the provider that owns them.
* No built-in login: keep the port on loopback, run it behind an authenticating proxy, and keep an
  inline-token config at mode `600`/`640` owned by uid 10001 (`chown 10001 accounts.json`).
* The container runs unprivileged (uid 10001); `docker-compose.yml` adds a read-only rootfs,
  `cap_drop: ALL` and `no-new-privileges`. Static serving containment-checks every path.

## Development

```bash
cp accounts.example.json accounts.json
python3 app.py --check                  # one poll, prints JSON, non-zero if any account is not ok
PORT=8080 python3 app.py
python3 tests/smoke.py                  # the HTTP surface
python3 tests/history.py                # the optional store: config, sampling, rollups, alert log
python3 tests/balance.py                # registry, balance adapters, error branches
python3 tests/background.py             # artwork shrink (needs Pillow)
python3 tests/screenshot.py             # renders both pages in Chromium, re-shoots docs/*.png
python3 tests/tab_switch.py             # a burst of tab switches must keep refreshing
QUOTA_POLL_SECONDS=10 python3 tests/cadence_soak.py   # the countdown must not decay
```

Every suite talks to a local stub instead of a provider, so they run offline and spend no quota. CI
runs them in real Chromium, then builds `linux/amd64` + `linux/arm64` and pushes to GHCR from `main`
and `v*` tags. A release is a tag.

## If it does not work

**`no account has a credential configured`, every card is an error.** A key was written into `token_env`. Move it to `token`.

**Docker created a directory where `accounts.json` should be.** The bind mount pointed at a missing path. Remove the directory, write the file, start again.

**One card says the provider rejected the key.** `401` is a bad or revoked key; `403` is usually a valid key missing a scope or a plan. `python3 app.py --check` gives the same answer without the UI.

**Everything is red after moving host and the config looks right.** Check the path *inside* the container, and the permissions: the process runs as uid 10001 and cannot read a `600` file owned by someone else.

**The wallpaper never appears.** `/api/health` reports `background.served: none` and the last error.

**`History is enabled but not readable … (unable to open database file)`.** The database file is writable, its *directory* is not. Mount a directory at `/data` rather than a single file, restart.

## Limits

* No token refresh: an expired key is reported as `auth_error` until you re-mint it and update the config.
* Polling is sequential: comfortable to a few dozen accounts.
* Nothing is inferred from a missing field: an amount the provider did not report renders as an error, never as `0.00`.

## License

MIT, see [LICENSE](LICENSE).

The container image installs [Pillow](https://python-pillow.org/) (MIT-CMU) for the artwork resize. The header marks are [Font Awesome Free](https://fontawesome.com/) 7.3.1 solid icons, inlined as SVG paths: CC BY 4.0, Copyright Fonticons, Inc. No icon font or CDN stylesheet is fetched.
