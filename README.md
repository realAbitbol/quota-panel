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

**Any number of accounts, any mix of providers** — two CommandCode keys, three OpenRouter keys and a
DeepSeek key on one page, each with its own card. An account whose credential is missing or refused
renders as an error card and never takes the panel down.

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
  ghcr.io/realabitbol/quota-panel:latest
```

Write the config first: the container polls nothing without it, and bind-mounting a path that does not exist yet leaves a *directory* in its place. Then open <http://localhost:8080>.

The port is bound to loopback because there is no built-in login and the page shows labels, plan names and spend. Put it behind a reverse proxy that authenticates. A LAN dashboard (Homepage, Dashy, Glance) is the exception: it cannot sit behind that proxy's login, so either give it the published port on the LAN interface or list it in the proxy's bypass rules — a bypass is a stronger commitment than a published port, because it is what answers the public hostname too.

<details>
<summary>docker compose, with the hardening from this repo</summary>

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
      test: ["CMD", "python3", "-c", "import urllib.request,sys\ntry:\n    urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)\n    sys.exit(0)\nexcept Exception:\n    sys.exit(1)"]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 60s              # /api/health answers 503 until the first poll finishes

volumes:
  quota-history:
```

`docker-compose.yml` in this repo is the same service with fuller comments.

</details>


<details>
<summary>Build from source</summary>

```bash
git clone https://github.com/realAbitbol/quota-panel.git
cd quota-panel
cp accounts.example.json accounts.json
docker build -t quota-panel .
docker run -d -p 127.0.0.1:8080:8080 \
  -v "$PWD/accounts.json:/config/accounts.json:ro" quota-panel
```

`python3 app.py` runs the same code outside a container; the only dependency is Pillow, for the artwork resize.

</details>

## Configuration

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
| `provider` | yes | One of the providers below. Anything else is refused at startup. |
| `label` | no | Card title, defaults to `id`. |
| `token` / `token_env` / `token_file` | one of the three | The key, the *name* of an env var holding it, or a path to a file holding it. Precedence: `token` → `token_env` → `token_file`; none at all is a non-fatal `auth_error`. |
| `poll_seconds` | no | Seconds between polls, `10`–`3600`. Default `60`. |
| `background_url` | no | Page artwork: an `http(s)` URL at the top level of the file (see the example above), `"none"` for no artwork, or empty/absent for the default wallpaper URL (hotlinked, not redistributed). Fetched once at startup, capped at 4K, re-encoded as WebP. |

`id` keys everything: `/api/quota`, the Homepage map (`items["<id>_<window>"]`) and the error list. Set it explicitly — omitting it falls back to a positional `<provider>-<index>`, so inserting an account silently reassigns ids. Use lowercase letters, digits and dashes. Duplicate ids are fatal.

Any number of accounts, any mix of providers. An account whose credential is missing or refused renders as an error card and never takes the panel down.

> **Trap:** putting the key *into* `token_env` parses, loads and renders — and every lookup returns `""`, because the field holds the *name* of a variable, not the value. The key goes in `token`.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Listen port. |
| `QUOTA_CONFIG` | `/config/accounts.json` | Config path. |
| `QUOTA_POLL_SECONDS` | `60` | Poll interval **fallback** — `poll_seconds` in the config file wins. |
| `QUOTA_HTTP_TIMEOUT` | `20` | Per-request timeout, in seconds. |
| `QUOTA_CURRENCY` | `USD` | Currency reported for a multi-currency balance. |
| `QUOTA_BACKGROUND_URL` | the default wallpaper URL (hotlinked) | Artwork URL, or `none`/`off`. The config file wins. |
| `QUOTA_BACKGROUND_DIR` | `/tmp/quota-panel` | Where the fetched artwork is stored. |
| `QUOTA_BACKGROUND_TIMEOUT` | `20` | Artwork fetch timeout, in seconds. |

Every provider also has a `*_API_BASE` override so the test suite can point an adapter at a local stub. Nothing in production needs them.

### Usage history

`/history` charts usage over time from one SQLite file. It is a deliberately close reading of
[AIMeter](https://github.com/bugwz/AIMeter)'s Usage History, hand-rolled in SVG — the page vendors
no chart library:

* **Usage trend** — one filled line per account on a real 0–100 scale with the cap drawn and named,
  a clickable legend that isolates a line, a hover readout with sample counts, an **alert threshold**
  line at `alert_percent`, **reset markers** wherever a window reset in range, and a projected cap
  date from the range's own slope. With one account selected, every window it publishes is a line.
* **Weekly comparison** — grouped bars of each account's weekly mean, for stage-by-stage shifts.
* **Share of consumption** — a donut of each account's average consumption.
* **Intensity vs volatility** — a scatter: X average, Y volatility, bubble size active days.
* **Radar matrix** — average load, latest, peak, volatility and cost burn per account.
* **Daily intensity** — one row per account, one column per day, one hue so a shade means the same
  on every row.
* **Account insights** — avg / latest / peak / volatility and a sparkline per account.
* **Window breakdown** — with one account selected, one card per window it publishes: latest,
  average, peak, volatility, a sparkline and the next reset.

An account's colour and mark come from its provider. A day-or-longer range is snapped to whole
calendar days, so a daily bar lines up with a day. The window that stands for an account is the
widest its own label names — the five-hour window resets several times a day and its daily average
describes nothing. A money balance is kept as money, never turned into a percentage: it is recorded
in its own tables, shown as a balance in the insights cards, and its drawdown from the range's own
peak balance is the radar's **cost burn**. Under the controls the page states what the store keeps
(`sampled every … · raw …d · … rollups kept …d`) and what this range is made of (`raw`, `raw+rollup`
or `rollup`, with a row count).

It ships on, in `accounts.example.json` and in `docker-compose.yml`:

```json
"history": { "enabled": true, "path": "/data/quota.db", "sample_seconds": 300,
             "raw_days": 90, "rollup_days": 365, "rollup_seconds": 900,
             "alert_percent": 80, "alert_url": "",
             "alert_retries": 3, "alert_backoff_seconds": 1 }
```

`sample_seconds` is how often a reading is stored (polling is unchanged, only the row write is gated); `raw_days`/`rollup_days`/`rollup_seconds` are retention. `alert_percent` is the crossing the trend draws. Every crossing is written to the store's `alerts` table (status, attempts, error); when `alert_url` is a URL it is POSTed there as JSON (`account_id`, `window_key`, `label`, `previous`, `percent`, `threshold`, `at`) by a background worker that retries `alert_retries` times with exponential backoff from `alert_backoff_seconds`, and anything left pending is retried after a restart — best-effort and never fatal, so a webhook that is down costs a log line, not a poll. The same settings come from `QUOTA_HISTORY_ENABLED`, `QUOTA_DB`, `QUOTA_HISTORY_SAMPLE_SECONDS`, `QUOTA_RETENTION_RAW_DAYS`, `QUOTA_RETENTION_ROLLUP_DAYS`, `QUOTA_ROLLUP_SECONDS`, `QUOTA_HISTORY_ALERT_PERCENT`, `QUOTA_HISTORY_ALERT_URL`, `QUOTA_HISTORY_ALERT_RETRIES`, `QUOTA_HISTORY_ALERT_BACKOFF_SECONDS`. Set `enabled: false` for a panel that keeps no state at all — no database, no volume, `/api/history` answers `404`.

**Give it a directory, not a file.** sqlite writes its journal beside the database, so `/data` must be writable: `install -d -o 10001 -g 10001 ./data` on the host, then `- ./data:/data` in compose. CIFS/NFS is refused — no working locks.

Cost: ~0.35 GB a year at 300 s sampling (1.7 GB at 60 s) plus ~116 MB of rollups (money balances add a little). A failed poll or an unreadable window leaves a **gap**, never a zero. The trend is drawn against **real percents**: a window reading tenths of a percent sits near the axis because that is what it is, and the legend and the hover readout carry its exact number. The intensity map shades each cell by that day's real average, so the same shade means the same usage on every row and every day.

## Endpoints

| Path | Purpose |
|---|---|
| `/` | the card UI |
| `/history` | usage over time |
| `/api/quota` | normalized JSON: every account, every window, with `resets_at` |
| `/api/history` | the time series (`hours`/`days`/`since`/`until`, `max_points`, `bucket_seconds`, `account_id`, `series`) |
| `/api/providers` | the provider registry: id, label, card kind, contract, logo |
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

## Security

* Read-only, outbound only: one `GET` per configured provider, plus one artwork fetch at startup. No write path.
* Credentials are read at startup, held in memory, never logged, never returned by any endpoint, and sent only to the provider that owns them. A test stub echoes the `Authorization` header it received and no served body may contain it.
* No built-in login. Run it behind something that authenticates and keep the port on loopback. With inline `token`s, keep `accounts.json` at mode `600`/`640`.
* The container runs unprivileged (uid 10001). `docker-compose.yml` (and the snippet under Install) also makes the rootfs read-only, drops all capabilities and sets `no-new-privileges`; a bare `docker run` from this README gets the uid and the read-only config mount but not those three — add `--read-only --tmpfs /tmp:rw,size=32m,noexec,nosuid,nodev --cap-drop ALL --security-opt no-new-privileges:true` for the full set. The static route containment-checks every path and allows a fixed extension list.

## Development

```bash
cp accounts.example.json accounts.json
python3 app.py --check                  # one poll, prints JSON, non-zero if any account is not ok
PORT=8080 python3 app.py
python3 tests/smoke.py                  # boots the app and exercises the HTTP surface
python3 tests/history.py                # the optional store: config, sampling, rollups, /api/history, alert log
python3 tests/balance.py                # registry, balance adapters, error branches
python3 tests/background.py             # artwork shrink (needs Pillow)
python3 tests/screenshot.py             # renders the real pages in Chromium and re-shoots docs/screenshot.png + docs/history.png
python3 tests/tab_switch.py             # a burst of tab switches must keep refreshing
QUOTA_POLL_SECONDS=10 python3 tests/cadence_soak.py   # the header's countdown must not decay
```

Every suite talks to a local stub instead of a provider, so they run offline and spend no quota. Every push runs them against real Chromium, then builds `linux/amd64` + `linux/arm64` and pushes to GHCR from `main` and `v*` tags. A release is a tag: `git tag v1.0.0 && git push origin v1.0.0`.

## If it does not work

**`no account has a credential configured`, every card is an error.** A key was written into `token_env`. Move it to `token`.

**Docker created a directory where `accounts.json` should be.** The bind mount pointed at a missing path. Remove the directory, write the file, start again.

**One card says the provider rejected the key.** `401` is a bad or revoked key; `403` is usually a valid key missing a scope or a plan (CheaperInference needs `account:read`; on OpenCode Go, `403` means no Go plan). `python3 app.py --check` gives the same answer without the UI.

**Everything is red after moving host and the config looks right.** Check the path *inside* the container, and the permissions: the process runs as uid 10001 and cannot read a `600` file owned by someone else.

**The wallpaper never appears.** `/api/health` reports `background.served: none` and the last error.

**`History is enabled but not readable … (unable to open database file)`.** The database file is writable, its *directory* is not. Mount a directory at `/data` rather than a single file, restart.

## Limits

* No token refresh: the providers' OAuth flows are out of scope, so an expired key is reported as `auth_error` until you re-mint it and update the config.
* Polling is sequential: a cycle takes roughly `sum(requests per account)` × latency. Comfortable to a few dozen accounts.
* Nothing is inferred from a missing field: an amount the provider did not report renders as an error, never as `0.00`.

## License

MIT, see [LICENSE](LICENSE).

The container image installs [Pillow](https://python-pillow.org/) (MIT-CMU) for the artwork resize. The header marks are [Font Awesome Free](https://fontawesome.com/) 7.3.1 solid icons, inlined as SVG paths: CC BY 4.0, Copyright Fonticons, Inc. No icon font or CDN stylesheet is fetched.
