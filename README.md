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

quota-panel polls the provider APIs directly and renders one page you can leave open.
It is read-only by design: only `GET` requests, nothing is ever written upstream, no
state of its own, no telemetry, and credentials never leave the host.

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

The image is published for `linux/amd64` and `linux/arm64`. Write the config file
first: the container has nothing to poll without it, and bind-mounting a path that
does not exist yet leaves a *directory* in its place.

```bash
curl -o accounts.json \
  https://raw.githubusercontent.com/realAbitbol/quota-panel/main/accounts.example.json
$EDITOR accounts.json            # one entry per account: token, token_env or token_file

docker run -d \
  --name quota-panel \
  --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v "$PWD/accounts.json:/config/accounts.json:ro" \
  ghcr.io/realabitbol/quota-panel:latest
```

Then open <http://localhost:8080>. The port is bound to loopback on purpose: there
is no built-in login, so the panel belongs behind a reverse proxy that
authenticates (see [Security](#security)). In production, pin a version tag or a
digest rather than riding `:latest`.

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

```

### Build from source

```bash
git clone https://github.com/realAbitbol/quota-panel.git
cd quota-panel
cp accounts.example.json accounts.json   # then edit it
docker build -t quota-panel .
docker run -d -p 8080:8080 \
  -v "$PWD/accounts.json:/config/accounts.json:ro" \
  quota-panel
```

## Configuration

One file, `accounts.json`, mounted read-only at `/config/accounts.json`:

```json
{
  "poll_seconds": 60,
  "accounts": [
    { "id": "cc-work",     "provider": "commandcode",      "label": "CommandCode — work", "token": "user_…"   },
    { "id": "oc-personal", "provider": "opencode_go",      "label": "OpenCode Go — personal", "token": "sk-…" },
    { "id": "or-main",     "provider": "openrouter",       "label": "OpenRouter",         "token": "sk-or-…"  },
    { "id": "ci-main",     "provider": "cheaperinference", "label": "CheaperInference",   "token": "ci_live_…" }
  ]
}
```

### Providers

Two card shapes, because the providers report two different things:

| `provider` | Card | Reports | Contract |
|---|---|---|---|
| `commandcode` | window | 5 h / weekly / monthly percent | verified live |
| `opencode_go` | window | rolling / weekly / monthly percent | verified live |
| `zai` | window | quota windows (session / weekly / web searches) | documented |
| `synthetic` | window | request allowance percent | documented |
| `openrouter` | balance | credit balance; **percent only if the key has a spend limit** | documented |
| `cheaperinference` | balance | wallet balance, incl. money reserved in flight | documented |
| `deepseek` | balance | balance in CNY or USD (`is_available` flag) | documented |
| `kimi` | balance | available / voucher / cash balance (USD) | documented |

* **window** — an envelope that refills on a clock. A percentage is the whole story.
* **balance** — prepaid money with no cap. There is no honest percentage to draw, so the
  card shows the amount. Nothing is ever inferred from a top-up: an unreadable balance
  renders as an error, never as `0.00`, because those two mean opposite things.

`openrouter` is the one provider that can render either: a key with a `limit` set gets a
real percent window; a key without one gets a balance and the note *"no spend cap set on
this key"*.

The **Contract** column is deliberate. `verified live` means the adapter has been
exercised against a real response from the provider. `documented` means it was built from
the vendor's own published contract and not yet hit with a live credential — the parse is
tested against a recorded fixture, but the first real key may reveal a field nobody
documents. Each fixture carries its provenance in `tests/fixtures/providers/`. Ask the
panel directly:

```sh
curl -s localhost:8080/api/providers | jq
```

### Icons

Every card wears its provider's mark, monochrome, from `static/logos/<provider>.svg`, drawn
white. The files are written with `currentColor` so they can be recoloured, but a mark is
loaded through an `<img>` and `currentColor` cannot cross that boundary — inside an `<img>` it
resolves to the file's own default rather than the page's colour — so the UI forces white
with a CSS filter instead of relying on inheritance. A provider with no mark, or a
missing file, falls back to `_fallback.svg` rather than rendering an empty box. Per-account
override with an optional `"logo": "my.svg"` (relative to `static/logos/`).

Cards never print a credential: OpenRouter's `/key` returns a label that *defaults to the
key's own prefix*, so a credential-shaped label is dropped in favour of the plain provider
name. That check is covered by the test suite.

### `id` — unique, stable, and effectively required

| Field | Required | Meaning |
|---|---|---|
| `id` | **yes, in practice** | The account's identity key. Must be **unique per account** and must not change once the panel is running. |
| `provider` | yes | one of the providers in the table above. |
| `label` | no | Free-text card title. Purely cosmetic; change it whenever. Defaults to `id`. |
| `token` | exactly one of these three | The API key itself, inline. |
| `token_env` | " | The **name** of an environment variable holding the key. |
| `token_file` | " | Path to a file holding the key. |

`id` is what the panel keys everything on:

* the `/api/quota` payload — each card's identity;
* the Homepage widget map — `items["<id>_<window>"]`, e.g. `cc-work_five_hour`;
* the error list — which account is unconfigured.

Which means:

* **Duplicates are fatal**: the app refuses to start with
  `account ids must be unique: [...]`. That guard exists so two accounts can never
  silently share one identity.
* **Changing an `id` renames the account** as far as the sidecar tooling is concerned:
  the Homepage widget keys are renamed with it.
* **It is optional only syntactically.** Omit it and it falls back to a positional
  `<provider>-<index>` (`commandcode-1`, `opencode_go-2`, …), so inserting or
  reordering accounts silently reassigns ids between two accounts.
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

### `background_url`

The page artwork is bundled, and can be replaced with your own image:

```json
{
  "poll_seconds": 60,
  "background_url": "https://example.com/wallpaper.webp",
  "accounts": []
}
```

When set, the panel downloads that image **once at container startup** and serves it
from `/background` out of the container's non-persistent `/tmp` (a tmpfs in the
compose files), so nothing about it survives a restart and the bundled image is what
you get back the moment the URL stops working. Served types: `webp`, `png`, `jpeg`,
`avif`, `gif`, up to 8 MB.

* The host's `Content-Type` decides the type; the URL extension is the fallback, so a
  host that answers `application/octet-stream` still works.
* A fetch that fails — DNS, TLS, `404`, wrong type, too large — is logged and the
  bundled image is served instead. If the container started before the network was
  ready, the next request retries in the background, at most once every 5 minutes.
* The URL may be signed, so it is **never** logged and never returned by `/api/health`,
  which reports only whether one is configured, which image is being served, and the
  last error.
* Overridable per container with `QUOTA_BACKGROUND_URL`; the config file wins.

The downloaded image is capped at 4K (3840×2160) and re-encoded as WebP before it is
served, so a phone photo or a 48-megapixel wallpaper does not become the page's heaviest
asset. Measured on a 6000×4000 JPEG: 497 KB in, 46 KB out at 3240×2160. An image that is
already 4K-or-smaller WebP is served untouched, and if the re-encode comes out larger than
the original, the original is kept. Without Pillow in the image the download is served
as-is — that step never fails.
## Endpoints

| Path | Purpose |
|---|---|
| `/` | the card UI, live countdowns, self-refreshing |
| `/api/quota` | normalized JSON: every account, every window, with `resets_at` |
| `/api/homepage` | flat `items` map keyed `<account_id>_<window>`, for a gethomepage tile |
| `/api/health` | `200` while the last poll is fresh, `503` when stale |
| `/static/…` | the UI's own assets (image, favicon); path-traversal safe, allow-listed types |
| `/background` | the page artwork: the configured image when there is one, the bundled one otherwise |

```bash
curl -s localhost:8080/api/quota \
  | jq '.accounts[] | {id, plan, windows: [.windows[] | {key, percent, resets_at}]}'
```

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Listen port. |
| `QUOTA_CONFIG` | `/config/accounts.json` | Config path. |
| `QUOTA_POLL_SECONDS` | `60` | Poll interval; the config file's `poll_seconds` wins. |
| `QUOTA_HTTP_TIMEOUT` | `20` | Per-request timeout, in seconds. |
| `QUOTA_BACKGROUND_URL` | — | Artwork URL; the config file's `background_url` wins. |
| `QUOTA_BACKGROUND_TIMEOUT` | `20` | Fetch timeout for the artwork, in seconds. |

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
            in-memory state (guarded by a lock)
                        │
        ┌───────────────┴───────────────┐
        │  HTTP handlers (read-only)    │
        │  /  /api/quota  /api/homepage │
        │  /api/health  /static/…       │
        │  /background                  │
        └───────────────────────────────┘
```

* **Almost dependency-free** — the standard library, plus Pillow for one optional
  feature: shrinking a configured background image to 4K + WebP. Without Pillow the
  panel runs unchanged and serves that image as downloaded.
* **One poll serves every reader.** The page polls `/api/quota`; it never triggers
  a provider request, so ten open tabs cost the same as one.
* **Percentages are never invented.** The monthly CommandCode figure is derived
  from spend and remaining credit; when the API's numbers cannot be reconciled,
  the window is rendered without a percentage instead of with a wrong one.
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

* **Read-only, outbound only.** The app issues `GET`s to the two provider hosts — plus,
  when `background_url` is set, one `GET` to that URL at startup — and serves local pages.
  There is no write path and no upstream state change.
* **Credentials stay in the config.** They are read at startup, held in memory, and
  never logged, never returned by any endpoint, never sent anywhere but the
  provider that owns them. Redaction is enforced by a test: no served body — the
  page, `/api/quota`, `/api/homepage`, `/api/health` — may contain a configured
  credential value or anything shaped like a key.
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
python3 tests/background.py             # 4K + WebP shrink of a background (needs Pillow)
```

`--check` is side-effect free: it reads the config, hits both providers once,
prints the normalized JSON and writes nothing — so it doubles as a config validator
and a cron health probe. Run it inside the deployed image to
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
* Polling is sequential, so a cycle takes roughly
  `sum(requests per account)` × latency. Comfortable to a few dozen accounts; a
  hanging account can stretch a cycle, bounded by `QUOTA_HTTP_TIMEOUT`.

## License

MIT — see [LICENSE](LICENSE).

The container image installs [Pillow](https://python-pillow.org/) (MIT-CMU) for the
optional background resize. No code is vendored.
