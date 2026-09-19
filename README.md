# quota-panel

[![ci](https://github.com/realAbitbol/quota-panel/actions/workflows/ci.yml/badge.svg)](https://github.com/realAbitbol/quota-panel/actions/workflows/ci.yml)
[![image](https://img.shields.io/badge/ghcr.io-quota--panel-2496ED?logo=docker&logoColor=white)](https://github.com/realAbitbol/quota-panel/pkgs/container/quota-panel)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)

A small read-only dashboard that shows **how much of your AI coding subscription
is left**, right now, for every account you have — with a live countdown to each
reset. One card per account, one container, no database server, no JavaScript
build step.

![quota-panel dashboard](docs/screenshot.png)

*Screenshot uses synthetic data.*

## Why

Coding subscriptions meter you in rolling windows (5 h, weekly, monthly) and only
tell you where you stand if you go and ask a CLI. If you pay for **more than one
account** — a work one and a personal one, say — the answer lives in two places,
and you find out you are out of quota when a request fails.

quota-panel polls the provider APIs directly, keeps the history in SQLite, and
renders one page you can leave open. It is read-only by design: only `GET`
requests, nothing is ever written upstream, no telemetry, and credentials never
leave the host.

## What it shows

| Window | CommandCode | OpenCode Go |
|---|---|---|
| 5 h (rolling) | ✅ used / cap + reset | ✅ percent + reset |
| Weekly | ✅ used / cap + reset | ✅ percent + reset |
| Monthly | ✅ credits used / remaining + period end | ✅ percent + reset |

Per CommandCode account it also reports the plan (Go / GOAT / Pro / Provider /
Max / Ultra / Teams), the account name, and request + token totals with a success
rate. Each window carries its own `resets_at`, rendered as a live countdown.

## Quick start

The image is published for `linux/amd64` and `linux/arm64`:

```bash
docker run -d \
  --name quota-panel \
  --restart unless-stopped \
  -p 8080:8080 \
  -v "$PWD/accounts.json:/config/accounts.json:ro" \
  -v quota-panel-data:/data \
  ghcr.io/realabitbol/quota-panel:latest
```

Then open <http://localhost:8080>. In production, pin a version tag or a digest
rather than riding `:latest`.

### docker compose

```yaml
services:
  quota-panel:
    image: ghcr.io/realabitbol/quota-panel:latest
    restart: unless-stopped
    ports:
      - "127.0.0.1:8080:8080"          # loopback only, behind a reverse proxy
    volumes:
      - ./accounts.json:/config/accounts.json:ro
      - quota-panel-data:/data
    environment:
      QUOTA_POLL_SECONDS: "60"
    read_only: true
    tmpfs:
      - "/tmp:rw,size=16m,noexec,nosuid,nodev"
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    mem_limit: 128m

volumes:
  quota-panel-data:
```

### Build from source

```bash
git clone https://github.com/realAbitbol/quota-panel.git
cd quota-panel
cp accounts.example.json accounts.json   # then edit it
docker build -t quota-panel .
docker run -d -p 8080:8080 \
  -v "$PWD/accounts.json:/config/accounts.json:ro" \
  -v quota-panel-data:/data quota-panel
```

## Configuration

One file, `accounts.json`, mounted read-only at `/config/accounts.json`:

```json
{
  "poll_seconds": 60,
  "accounts": [
    { "id": "cc-work",     "provider": "commandcode", "label": "CommandCode — work",     "token": "user_…" },
    { "id": "oc-personal", "provider": "opencode_go", "label": "OpenCode Go — personal", "token": "sk-…"   }
  ]
}
```

### `id` — unique, stable, and effectively required

| Field | Required | Meaning |
|---|---|---|
| `id` | **yes, in practice** | The account's identity key. Must be **unique per account** and must not change once the panel is running. |
| `provider` | yes | `commandcode` or `opencode_go`. |
| `label` | no | Free-text card title. Purely cosmetic; change it whenever. Defaults to `id`. |
| `token` | exactly one of these three | The API key itself, inline. |
| `token_env` | " | The **name** of an environment variable holding the key. |
| `token_file` | " | Path to a file holding the key. |

`id` is what the panel keys everything on:

* the SQLite history — `snapshots.account_id`, indexed with the window and timestamp;
* the `/api/quota` payload — each card's identity;
* the Homepage widget map — `items["<id>_<window>"]`, e.g. `cc-work_five_hour`;
* the error list — which account is unconfigured.

Which means:

* **Duplicates are fatal**: the app refuses to start with
  `account ids must be unique: [...]`. That guard exists so two accounts can never
  silently share (and overwrite) one history series.
* **Changing an `id` starts a new account** as far as the panel is concerned: the
  old rows keep the old id and the Homepage widgets are renamed.
* **It is optional only syntactically.** Omit it and it falls back to a positional
  `<provider>-<index>` (`commandcode-1`, `opencode_go-2`, …), so inserting or
  reordering accounts silently reassigns ids and can swap two accounts' history.
  Always set it explicitly.
* Use lowercase letters, digits and dashes — the value ends up inside a widget key.

Any number of accounts is supported: one JSON entry per account, mixed providers,
polled in a single pass. An account whose credential is missing or refused renders
as an error card and never takes the panel down.

### `poll_seconds`

Poll interval, bounded to `10`–`3600` (out-of-range values are logged and
ignored). The page uses half of it as its own refresh rate, so changing it here is
enough. Overridable per container with `QUOTA_POLL_SECONDS`.

> Measured trap: writing the key *into* `token_env` looks like it works — the
> config parses, the accounts load, the panel renders — but every lookup returns
> `""` and the log says `no account has a credential configured`, because
> `token_env` holds the *name* of a variable. If you want the key in the file, the
> field is `token`. The app detects this mistake and warns (masked) at startup.

### `retention`

History is appended on every poll and never read back by the poller, so it grows
without bound — at a 60 s cadence, twelve series is roughly 6.3 M rows and
350–400 MB a year. Raw samples are therefore kept for a bounded window, and older
history survives as **rollups**: pre-aggregated buckets that *are* the long-term
record, not a cache of it.

```json
{
  "poll_seconds": 60,
  "retention": { "raw_days": 90, "rollup_days": 365, "rollup_seconds": 900 },
  "accounts": []
}
```

| Key | Default | Meaning |
|---|---|---|
| `raw_days` | `90` | One sample per poll, kept this long. |
| `rollup_days` | `365` | Aggregates kept this long (raised to `raw_days` if set lower). |
| `rollup_seconds` | `900` | Rollup bucket size. |

Each bucket stores `n`, `pct_sum`, `pct_min` and `pct_max`, so rolling a rollup up
again stays exact instead of averaging averages. Two guarantees hold whatever the
configuration:

* **Aggregate before pruning.** Rollups are committed before any raw row is deleted.
* **Prune only what is saved.** A raw row is dropped only once its bucket exists in
  `rollups`. A rollup that breaks therefore costs disk, never history — and the
  number of rows held back is logged rather than swallowed.

## Endpoints

| Path | Purpose |
|---|---|
| `/` | the card UI, live countdowns, self-refreshing |
| `/api/quota` | normalized JSON: every account, every window, with `resets_at` |
| `/api/history` | usage time series for the chart: raw + rollup on one aligned grid, `null` at gaps |
| `/api/homepage` | flat `items` map keyed `<account_id>_<window>`, for a gethomepage tile |
| `/api/health` | `200` while the last poll is fresh, `503` when stale |
| `/static/…` | the UI's own assets (image, favicon); path-traversal safe, allow-listed types |

```bash
curl -s localhost:8080/api/quota \
  | jq '.accounts[] | {id, plan, windows: [.windows[] | {key, percent, resets_at}]}'
```

#### `/api/history`

| Parameter | Default | Meaning |
|---|---|---|
| `hours` | `24` | Window length, up to 366 days. |
| `since` / `until` | — | Explicit ISO-8601 range; overrides `hours`. |
| `max_points` | `720` | Point budget; the bucket size is picked to fit it (`20`–`2000`). |
| `account_id` | all | Comma-separated filter. |
| `window_key` | all | Comma-separated filter (`five_hour`, `weekly`, `monthly`). |
| `resolution` | `auto` | `auto`, `raw` or `rollup`. |

The response is columnar and chart-ready: one shared `t` array of epoch seconds
plus, per series, `avg`, `min`, `max` and `n` — all the same length, with `null`
where a bucket has no data. That is deliberate: a gap must break the line. Each
series also carries `provider` and `window_key`, so a client can group or filter
without re-reading the config.

```bash
curl -s 'localhost:8080/api/history?hours=168&max_points=400' \
  | jq '{bucket: .bucket_seconds, resolution: .sources.resolution, series: [.series[].key]}'
```

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Listen port. |
| `QUOTA_CONFIG` | `/config/accounts.json` | Config path. |
| `QUOTA_DB` | `/data/quota.db` | SQLite history path. |
| `QUOTA_POLL_SECONDS` | `60` | Poll interval; the config file's `poll_seconds` wins. |
| `QUOTA_HTTP_TIMEOUT` | `20` | Per-request timeout, in seconds. |
| `QUOTA_RETENTION_RAW_DAYS` | `90` | Raw sample retention; the config file wins. |
| `QUOTA_RETENTION_ROLLUP_DAYS` | `365` | Rollup retention; the config file wins. |
| `QUOTA_ROLLUP_SECONDS` | `900` | Rollup bucket size, in seconds. |

## Usage chart

The chart card sits **below** the quota cards, so the current numbers stay the first
thing on the page. It is a single **stacked histogram** of the monthly envelope: one
bar per bucket over a selectable range (1 h → 1 y), one segment per account, whose
height is how many points of *that account's own* monthly quota were consumed *during*
that bucket. [uPlot](https://github.com/leeoniya/uPlot) is vendored under
`static/vendor/uplot/`, so the panel has no CDN dependency and renders offline.

uPlot has no stacked-series support (it says so in its own README), so the stacking is
drawn through its `paths` hook — canvas, not SVG. Each segment starts at the top of the
segment below it, and the bar width follows the bucket size, so it survives a zoom.

* **Consumption, not level.** A segment is the rise between two samples, so a busy hour
  is tall and an idle one is flat. The running percentage is what the cards above are
  for; this answers "how fast is this burning".
* **A reset is a gap, never a negative bar.** When a monthly window refills inside a
  bucket, the drop would be a negative segment, and how much was spent before the reset
  is unknowable. That bucket is left empty — the chart does not invent a number.
* **One colour per account**, assigned in a fixed order, so a segment keeps its colour
  between refreshes and nothing moves under the cursor.
* **Drag to zoom, double-click to reset**, click a legend entry to hide an account.
* **Refreshed on its own clock**: at most once every three minutes, plus on range
  change, focus and reconnect — not on every poll. Twelve series every 30 s would be
  pure waste and would redraw under the cursor.

## Homepage (gethomepage) integration

`/api/homepage` returns a `widgets` array and a flat `items` map, so either the
`customapi` widget or the `customapi` + `mappings` form works:

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
            - field: items.oc-personal_monthly.value
              label: Personal — month
```

Each item also carries `percent`, `resets_at` and a `detail` string, so a tile can
render a progress bar instead of a bare value. An account in error publishes a
single `items["<id>_error"]` entry rather than pretending to have numbers.

## How it works

```
        ┌──────────── poller thread (every poll_seconds) ───────────┐
        │  for each account: GET the provider's usage endpoints,    │
        │  normalize into {id, provider, label, plan, state,        │
        │  windows:[{key,label,percent,used,cap,resets_at}]}        │
        └───────────────┬───────────────────────────┬──────────────┘
                        │                           │
            in-memory state (guarded by a lock)   SQLite /data/quota.db
                        │                    90 d raw, 365 d rollups
        ┌───────────────┴───────────────┐
        │  HTTP handlers (read-only)    │
        │  /  /api/quota  /api/history  │
        │  /api/homepage  /api/health   │
        │  /static/…                    │
        └───────────────────────────────┘
```

* **Python standard library only** — no pip dependencies, so there is no
  dependency drift and nothing to patch beyond the base image.
* **One poll serves every reader.** The page polls `/api/quota`; it never triggers
  a provider request, so ten open tabs cost the same as one.
* **Percentages are never invented.** The monthly CommandCode figure is derived
  from spend and remaining credit; when the API's numbers cannot be reconciled,
  the window is rendered without a percentage instead of with a wrong one.
* **History outlives the raw samples.** Rollups are aggregated *before* the raw rows
  they replace are deleted, and a raw row is only deleted once its bucket is covered.
  Measured on a 400-day, 12-series fixture: 230 256 raw rows rolled up and pruned in
  0.8 s, with the steady-state pass costing 2 ms per poll.
* **The UI degrades honestly**: a failed fetch keeps the last good render on
  screen and labels itself `reconnecting (n)` / `feed down · data Ns old` instead
  of showing stale numbers as if they were current. Background tabs get their
  timers throttled by the browser, so the page also refreshes immediately on
  `visibilitychange`, `focus` and `online`.

## Provider contracts

Both contracts were verified against upstream implementations, not guessed.

**CommandCode** — `https://api.commandcode.ai`

```
Authorization: Bearer <account API key>      # the user_… key from the studio
x-command-code-version: 1.54.2
x-cli-environment: production

GET /alpha/whoami?limits=1
GET /alpha/billing/credits[?orgId=<id>]
GET /alpha/billing/subscriptions[?orgId=<id>]
GET /alpha/usage/summary[?orgId=<id>][&since=<ISO>]
```

* Three independent implementations agree on this contract: the `command-code`
  CLI bundle, the `cmd-usage` crate, and a macOS quota-bar app.
* ⚠️ `/provider/v1/models` is **unauthenticated**: it answers `200` even with a
  bogus key, so it is *not* proof that a key is alive. `/provider/v1/chat/completions`
  and every `/alpha/*` route answer `401` for a dead key — verify a key with
  `--check`, never with `/models`.
* `billing/credits` returns `{windowLimits:{fiveHour,weekly}, credits:{monthlyCredits,…}}`
  and `usage/summary` returns the spend for the period; the monthly percentage is
  `spend / (spend + remaining)`.

**OpenCode Go** — `GET https://opencode.ai/zen/go/v1/usage`

```
Authorization: Bearer <workspace key>
User-Agent: <a browser UA>     # Cloudflare answers 403 (error 1010) otherwise
Accept: application/json
```

→ `{"usage":{"rolling":{percent,resetsAt},"weekly":{…},"monthly":{…}}}`.
`401` = invalid key, `403` = key valid but the workspace has no Go plan. A
`resetsAt` returned alongside `percent == 0` is a placeholder and is discarded.

## Security

* **Read-only, outbound only.** The app issues `GET`s to the two provider hosts and
  serves local pages. There is no write path and no upstream state change.
* **Credentials stay in the config.** They are read at startup, held in memory, and
  never logged, never returned by any endpoint, never sent anywhere but the
  provider that owns them. Redaction is enforced by a test
  (`GET /api/quota has no credential leak`).
* **There is no built-in login.** Run it behind something that authenticates — a
  reverse proxy with SSO in front — and keep the published port on loopback.
* **Config file permissions matter.** If you use inline `token`s, keep
  `accounts.json` mode `600`/`640`, owned by the container user.
* **Hardened container**: unprivileged user (uid 10001), read-only rootfs, all
  capabilities dropped, `no-new-privileges`, 16 MB `noexec` tmpfs, no shell needed
  at runtime.
* The static route resolves and containment-checks every path, then allows a fixed
  extension list — `..`, symlink escapes and unexpected file types are refused
  (covered by the smoke test).

## Development

```bash
git clone https://github.com/realAbitbol/quota-panel.git
cd quota-panel
cp accounts.example.json accounts.json
python3 app.py --check                  # one poll, prints JSON, non-zero if any account is not ok
PORT=8080 python3 app.py                # run it
python3 tests/smoke.py                  # boots the app and exercises the HTTP surface
python3 tests/history.py                # retention, rollups and the /api/history merge
```

`--check` is side-effect free: it reads the config, hits both providers once,
prints the normalized JSON and does **not** touch the history — so it doubles as a
config validator and a cron health probe. Run it inside the deployed image to
validate a credential change before restarting the service:

```bash
docker compose run --rm --no-deps --entrypoint python3 quota-panel /app/app.py --check
```

### CI and releases

Every push and pull request runs the smoke test; the image is then built for
`linux/amd64` + `linux/arm64` and pushed to GHCR on `main` and on `v*` tags — so a
release is just a tag:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

Tags produce `v1.0.0`, `1.0`, `1`, `sha-<short>` and, on the default branch,
`latest`.

## Limits

* Read-only: only `GET`s, and nothing is written upstream.
* No token refresh: the providers' OAuth flows are out of scope by design, so an
  expired key is reported as `auth_error` for that account until you re-mint it and
  update the config.
* History keeps 90 days of raw samples plus up to 365 days of 15-minute rollups;
  both are configurable, and the chart reads whichever covers the requested range.
* Polling is sequential, so a cycle takes roughly
  `sum(requests per account)` × latency. Comfortable to a few dozen accounts; a
  hanging account can stretch a cycle, bounded by `QUOTA_HTTP_TIMEOUT`.

## License

MIT — see [LICENSE](LICENSE).

Bundled third-party code: [uPlot](https://github.com/leeoniya/uPlot) 1.6.32 (MIT),
vendored unmodified under `static/vendor/uplot/` — its own `LICENSE` ships alongside.
