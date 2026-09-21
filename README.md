<p align="center">
  <img src="static/favicon.svg" width="84" alt="quota-panel">
</p>

<h1 align="center">quota-panel</h1>

<p align="center">A small read-only dashboard that shows <b>how much of your AI coding subscription is left</b>, right now, for every account you have, with a live countdown to each reset.</p>

<p align="center">
  <a href="https://github.com/realAbitbol/quota-panel/actions/workflows/ci.yml"><img alt="ci: passing" src="https://github.com/realAbitbol/quota-panel/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/realAbitbol/quota-panel/pkgs/container/quota-panel"><img alt="ghcr.io/quota-panel" src="https://img.shields.io/badge/ghcr.io-quota--panel-2496ED?logo=docker&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="license: MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
  <a href="https://www.python.org/"><img alt="python: 3.12" src="https://img.shields.io/badge/python-3.12-3776AB?logo=python&amp;logoColor=white"></a>
</p>

<p align="center">
  <a href="#install">Install</a> · <a href="#configuration">Configuration</a> · <a href="#providers">Providers</a> · <a href="#endpoints">Endpoints</a> · <a href="#homepage-gethomepage-integration">Homepage tile</a> · <a href="#security">Security</a> · <a href="#if-it-does-not-work">FAQ</a>
</p>

## What you get

* Eight providers on one page: CommandCode, OpenCode Go, z.ai, Synthetic, OpenRouter, CheaperInference, DeepSeek and Kimi. Percent windows for plans that refill, money balances for prepaid credit.
* One container, no database. State lives in memory, so there is nothing to back up, migrate or vacuum.
* No JavaScript build step. The page is one HTML file inside the image, and ten open tabs cost the same as one because the browser never triggers a provider request.
* `python3 app.py --check` polls once, prints the normalised JSON, and exits non-zero if any account is unhappy, so it doubles as a cron probe.
* Read-only by construction: only `GET` requests, credentials never leave the host, and no endpoint can echo one. A test proves that against a stub that echoes the `Authorization` header it was sent.
* A gethomepage tile through `/api/homepage`, if you would rather see the numbers on a dashboard you already run.
* Provider marks on every card, and an explicit error card instead of a plausible-looking number when a provider's answer cannot be read.

![quota-panel dashboard](docs/screenshot.png)

*Screenshot uses synthetic data. Nothing in it came from a real account, and it renders with the bundled artwork: the shipped wallpaper is fetched at runtime, so an offline capture cannot show it.*

## Why

Coding subscriptions meter you in rolling windows (5 h, weekly, monthly) and most of them only tell you where you stand if you go and ask a CLI. If you pay for more than one account, say a work one and a personal one, the answer lives in two places, and you find out you are out of quota when a request fails.

quota-panel polls the provider APIs directly and renders one page you can leave open. It is read-only by design: only `GET` requests, nothing is written upstream, no state of its own, no telemetry, and credentials never leave the host.

## What it shows

Two card shapes, because providers report two different things.

Window cards, for plans that refill on a clock:

| Provider | Windows | Reported as |
|---|---|---|
| `commandcode` | 5 h rolling, weekly, monthly | credits spent / remaining, plus reset times |
| `opencode_go` | rolling, weekly, monthly | percent used, plus reset times |
| `zai` | session, weekly, and any other window the API returns | percent used, plus reset times |
| `synthetic` | request allowance | requests used / cap |

Per CommandCode account the card also shows the plan, the account name, and request and token totals with a success rate.

Balance cards, for prepaid money with no cap:

| Provider | Reported as |
|---|---|
| `openrouter` | credit balance, or a real percent window instead when the key has a spend limit |
| `cheaperinference` | wallet balance, including money reserved against in-flight requests |
| `deepseek` | balance per currency, with the `is_available` flag |
| `kimi` | available, voucher and cash balance |

A balance card has no percentage to draw, because there is no cap to divide by, so it shows the amount. Nothing is ever inferred from a top-up: a balance the provider did not report renders as an error, never as `0.00`, because "unreadable" and "empty" mean opposite things.

## Install

The image is published for `linux/amd64` and `linux/arm64`. Write the config file first: the container has nothing to poll without it, and bind-mounting a path that does not exist yet leaves a *directory* in its place.

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

Then open <http://localhost:8080>.

The port is bound to loopback on purpose. There is no built-in login, and the page shows account labels, plan names and spend, so it belongs behind a reverse proxy that authenticates (see [Security](#security)).

`latest` is convenient, not reproducible. For production, pin a digest:

```bash
docker buildx imagetools inspect ghcr.io/realabitbol/quota-panel:latest   # prints the digest
```

<details>
<summary>docker compose, with the hardening from this repo</summary>

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
      # 32m, not 16m: one artwork fetch is read (8 MB cap) and then written back as WebP, so the
      # worst case needs room for both copies or the shrink fails on an otherwise healthy container.
      - "/tmp:rw,size=32m,noexec,nosuid,nodev"
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    # Measured: a 6000x4000 wallpaper shrunk to 3240x2160 WebP peaks at 323-346 MB RSS, because
    # Pillow decodes the whole bitmap before resizing. Below 512m the shrink is OOM-killed, and the
    # container with it.
    mem_limit: 512m
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
      # 60s: /api/health answers 503 until the first poll finishes, and that first poll runs
      # sequentially over every account at up to QUOTA_HTTP_TIMEOUT each.
      start_period: 60s
```

`docker-compose.yml` in this repo is the same service with the full comments.

</details>

<details>
<summary>Build from source</summary>

```bash
git clone https://github.com/realAbitbol/quota-panel.git
cd quota-panel
cp accounts.example.json accounts.json   # then edit it
docker build -t quota-panel .
docker run -d -p 127.0.0.1:8080:8080 \
  -v "$PWD/accounts.json:/config/accounts.json:ro" \
  quota-panel
```

There is nothing to compile. `python3 app.py` runs the same code outside a container, and the only dependency is Pillow, for one optional feature.

</details>

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

The four fields that matter:

| Field | Required | Meaning |
|---|---|---|
| `id` | yes, in practice | The account's identity key. Unique per account, and it should not change once the panel is running. |
| `provider` | yes | One of the [providers](#providers) below. Anything else is refused at startup. |
| `label` | no | Card title. Cosmetic, defaults to `id`. |
| `token` / `token_env` / `token_file` | exactly one | The key itself, the *name* of an environment variable holding it, or a path to a file holding it. |

`id` is what everything else is keyed on: the `/api/quota` payload, the Homepage widget map (`items["<id>_<window>"]`, for example `cc-work_five_hour`), and the error list. Duplicate ids are fatal, and the app says so at startup rather than letting two accounts share one identity. If you omit an `id` it falls back to a positional `<provider>-<index>`, which means inserting an account silently reassigns ids, so set it explicitly. Lowercase letters, digits and dashes only, since the value ends up inside a widget key.

Any number of accounts is supported: mixed providers, polled in a single pass. An account whose credential is missing or refused renders as an error card and never takes the panel down.

### Providers

The registry, as the panel serves it from `/api/providers`:

| `provider` | Card | Reports | Contract |
|---|---|---|---|
| `commandcode` | window | 5 h / weekly / monthly percent | verified live |
| `opencode_go` | window | rolling / weekly / monthly percent | verified live |
| `zai` | window | quota windows (session / weekly / web searches) | third-party |
| `synthetic` | window | request allowance percent | documented |
| `openrouter` | balance | credit balance; **percent only if the key has a spend limit** | documented |
| `cheaperinference` | balance | wallet balance, incl. money reserved in flight | documented |
| `deepseek` | balance | balance in CNY or USD (`is_available` flag) | documented |
| `kimi` | balance | available / voucher / cash balance (USD) | documented |

`openrouter` is the one provider that can render either shape: a key with a `limit` set gets a real percent window, a key without one gets a balance and the note *"no spend cap set on this key"*. The Contract column is deliberate, and it has three values:

* `verified live`: the adapter is exercised against a real response from the provider in
  daily use, from the maintainer's own account. That is `commandcode` and `opencode_go`, and
  the registry repeats the provenance per provider in `contract_note`.
* `documented`: built from the vendor's own published contract and not yet hit with a live
  credential. The parse is tested against a recorded fixture, but the first real key may
  reveal a field nobody documents.
* `third-party`: the vendor publishes no contract for that route at all, and the shape
  comes from independent implementations. This is `zai`: its own page documents quota
  *policy*, not the monitor API, and the fields come from two projects that reverse
  engineered it and agree (`contract_note` names them). Calling that "documented" claimed a
  vendor promise that does not exist.

Each fixture carries its provenance in `tests/fixtures/providers/`. Ask the panel directly:

```sh
curl -s localhost:8080/api/providers | jq
```

### Icons

Every card wears its provider's mark, monochrome, from `static/logos/<provider>.svg`. The files are written with `currentColor` so they can be recoloured, but a mark is loaded through an `<img>`, and `currentColor` cannot cross that boundary: inside an `<img>` it resolves to the file's own default rather than the page's colour. The UI therefore forces white with a CSS filter instead of relying on inheritance. A provider with no mark, or a missing file, falls back to `_fallback.svg` rather than rendering an empty box. Per-account override with an optional `"logo": "my.svg"`, relative to `static/logos/`.

Cards never print a credential. OpenRouter's `/key` returns a label that *defaults to the key's own prefix*, so a credential-shaped label is dropped in favour of the plain provider name, while a human label like `personal-laptop-key-2026` is kept. Both directions are covered by the test suite.

### `poll_seconds`

Poll interval, bounded to `10`–`3600` (out-of-range values are logged and
ignored). The page uses half of it as its own refresh rate, so changing it here is
enough. Overridable per container with `QUOTA_POLL_SECONDS`.

> Measured trap: writing the key *into* `token_env` looks like it works. The
> config parses, the accounts load, the panel renders, but every lookup returns
> `""` and the log says `no account has a credential configured`, because
> `token_env` holds the *name* of a variable. If you want the key in the file, the
> field is `token`. The app detects this mistake and warns (masked) at startup.

### `background_url`

The panel ships with a wallpaper, and any image of yours can replace it. A config still needs at least one account, since the panel refuses to start with none:

```json
{
  "poll_seconds": 60,
  "background_url": "https://example.com/wallpaper.webp",
  "accounts": [
    { "id": "cc-work", "provider": "commandcode", "label": "CommandCode — work", "token": "user_…" }
  ]
}
```

| Value | What `/background` serves |
|---|---|
| an `http(s)` URL | that image, fetched once at startup |
| `"none"` or `"off"` | the bundled artwork, and no fetch at all |
| absent or `""` | the wallpaper the panel ships with |

The shipped wallpaper is
`https://r4.wallpaperflare.com/wallpaper/65/18/546/ai-art-city-street-lofi-japan-hd-wallpaper-d8618916d8ff4e5a70f17a71496ff810.jpg`,
fetched with the panel's own user agent (measured: `200`, `image/jpeg`, 316 KB, 2912×1632). It is
**hotlinked, not redistributed**: the repository carries no copy of it, so the container asks that
host for the image exactly as it would ask yours, and the bundled artwork covers the day the host
stops answering. That host sits behind Cloudflare, which refuses some automated user agents with a
`403` while the panel's own fetcher gets a `200` today; if you would rather the container talked to
nobody but your providers, or you are on a metered link, set `"none"`.

Whichever image is chosen, the panel downloads it **once at container startup** and serves it
from `/background` out of the container's non-persistent `/tmp` (a tmpfs in the
compose files), so nothing about it survives a restart and the bundled image is what
you get back the moment the URL stops working. Served types: `webp`, `png`, `jpeg`,
`avif`, `gif`, up to 8 MB.

* The host's `Content-Type` decides the type; the URL extension is the fallback, so a
  host that answers `application/octet-stream` still works.
* A fetch that fails (DNS, TLS, `404`, wrong type, too large) is logged and the
  bundled image is served instead. If the container started before the network was
  ready, the next request retries in the background, at most once every 5 minutes.
* The URL may be signed, so it is **never** logged and never returned by `/api/health`,
  which reports only whether one is configured, which image is being served, and the
  last error.
* Overridable per container with `QUOTA_BACKGROUND_URL` (the config file wins) and `QUOTA_BACKGROUND_DIR`, which is where the fetched copy lives (`/tmp/quota-panel` by default).

The downloaded image is capped at 4K (3840×2160) and re-encoded as WebP before it is served, so a phone photo or a 48-megapixel wallpaper does not become the page's heaviest asset. An image that is already 4K-or-smaller WebP is served untouched, and if the re-encode comes out larger than the original, the original is kept.

How much smaller is a property of the image, not a promise. Measured on a smooth 6000×4000 wallpaper: 497 KB in, 46 KB out at 3240×2160. A noisy photo of the same dimensions re-encodes to roughly 866 KB, so size the tmpfs and any memory limit for your own image rather than for that number. Without Pillow in the image the download is served as-is, since that step never fails.

## Endpoints

| Path | Purpose |
|---|---|
| `/` | the card UI, live countdowns, self-refreshing |
| `/api/quota` | normalized JSON: every account, every window, with `resets_at` |
| `/api/providers` | the provider registry: id, label, card kind, contract and its note, logo path |
| `/api/homepage` | flat `items` map keyed `<account_id>_<window>`, for a gethomepage tile |
| `/api/health` | `200` while the last poll is fresh, `503` when stale |
| `/static/…` | the UI's own assets (image, favicon); path-traversal safe, allow-listed types |
| `/background` | the page artwork: your URL or the shipped wallpaper, the bundled image when the URL is off or the fetch failed |

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
| `QUOTA_CURRENCY` | `USD` | Which currency a multi-currency balance is reported in, since DeepSeek lists several. |
| `QUOTA_BACKGROUND_URL` | the shipped wallpaper | Artwork URL, or `none`/`off` for the bundled image; the config file's `background_url` wins. |
| `QUOTA_BACKGROUND_DIR` | `/tmp/quota-panel` | Where the fetched artwork is stored. Keep it on a writable path, or the panel silently falls back to the bundled image. |
| `QUOTA_BACKGROUND_TIMEOUT` | `20` | Fetch timeout for the artwork, in seconds. |

Every provider also has a `*_API_BASE` override (`COMMANDCODE_API_BASE`, `DEEPSEEK_API_BASE`, `Z_AI_API_BASE`, `MOONSHOT_API_BASE` and the rest). They exist so the test suite can point an adapter at a local stub, which is how it runs with no provider call at all. Nothing in production needs them, and pointing one at the wrong host is a good way to get a 404 you will read twice.

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

The standard library does the work, with Pillow for the one optional feature described above. Percentages are never invented: the monthly CommandCode figure, for instance, is derived from spend and remaining credit, and when those numbers cannot be reconciled the window renders without a percentage rather than with a wrong one.

The UI degrades honestly too. A failed fetch keeps the last good render on screen and labels itself `reconnecting (n)` or `feed down · data Ns old`, instead of showing stale numbers as if they were current. Background tabs get their timers throttled by the browser, so the page also refreshes on `visibilitychange`, `focus` and `online`.

## Provider contracts

Both contracts were verified against upstream implementations, not guessed.

**CommandCode**: `https://api.commandcode.ai`

```
Authorization: Bearer <account API key>      # the user_… key from the studio
x-command-code-version: 1.54.2
x-cli-environment: production

GET /alpha/whoami?limits=1
GET /alpha/billing/credits[?orgId=<id>]
GET /alpha/billing/subscriptions[?orgId=<id>]
GET /alpha/usage/summary[?orgId=<id>]
```

* Three independent implementations agree on this contract: the `command-code`
  CLI bundle, the `cmd-usage` crate, and a macOS quota-bar app.
* The version header is sensitive: `/alpha/*` is CLI-internal, and third-party notes warn
  that `x-command-code-version` has to track the CLI. The contract source shipped as
  `command-code@1.58.0` while the header above is what the adapter has actually been run
  against. The two disagree, so the value that is exercised is the one kept and the
  discrepancy is recorded in `app.py` rather than resolved by a guess.
* ⚠️ `/provider/v1/models` is **unauthenticated**: it answers `200` even with a
  bogus key, so it is *not* proof that a key is alive. `/provider/v1/chat/completions`
  and every `/alpha/*` route answer `401` for a dead key. Verify a key with
  `--check`, never with `/models`.
* `billing/credits` returns `{windowLimits:{fiveHour,weekly}, credits:{monthlyCredits,…}}`
  and `usage/summary` returns the spend for the period; the monthly percentage is
  `spend / (spend + remaining)`.

**OpenCode Go**: `GET https://opencode.ai/zen/go/v1/usage`

```
Authorization: Bearer <workspace key>
User-Agent: <a browser UA>     # Cloudflare answers 403 (error 1010) otherwise
Accept: application/json
```

→ `{"usage":{"rolling":{percent,resetsAt},"weekly":{…},"monthly":{…}}}`.
`401` = invalid key, `403` = key valid but the workspace has no Go plan. A
`resetsAt` returned alongside `percent == 0` is a placeholder and is discarded.

## Security

* Read-only, outbound only. The app issues `GET`s to one host per configured provider, up to eight hosts for the eight providers it supports, plus one artwork fetch at startup — the shipped wallpaper, or your own URL, or nothing at all when you set `none`. That fetch is retried while it keeps failing, at most once every 5 minutes, rather than only at startup. There is no write path and no upstream state change.
* Credentials stay in the config. They are read at startup, held in memory, and never logged, never returned by any endpoint, never sent anywhere but the provider that owns them. Redaction is applied to the whole result in one place rather than field by field, and the suite proves it end to end: the test stub echoes the `Authorization` header it received back inside its response body, and no served body (the page, `/api/quota`, `/api/homepage`, `/api/health`) may contain it.
* There is no built-in login. Run it behind something that authenticates, a reverse proxy with SSO in front, and keep the published port on loopback.
* Config file permissions matter. With inline `token`s, keep `accounts.json` mode `600`/`640`, owned by the container user.
* The container is hardened: unprivileged user (uid 10001), read-only rootfs, all capabilities dropped, `no-new-privileges`, a 32 MB `noexec` tmpfs, and log rotation so a crash-loop cannot fill the disk. No shell is needed at runtime.
* The static route resolves and containment-checks every path, then allows a fixed extension list; `..`, symlink escapes and unexpected file types are refused, which the smoke test covers.

## Development

```bash
git clone https://github.com/realAbitbol/quota-panel.git
cd quota-panel
cp accounts.example.json accounts.json
python3 app.py --check                  # one poll, prints JSON, non-zero if any account is not ok
PORT=8080 python3 app.py                # run it
python3 tests/smoke.py                  # boots the app and exercises the HTTP surface
python3 tests/balance.py                # registry, balance adapters, error branches
python3 tests/background.py             # 4K + WebP shrink of a background (needs Pillow)
python3 tests/screenshot.py             # renders the real page in Chromium and re-shoots the README image
```

Every suite talks to a local stub instead of a provider, so they run offline and cannot spend anyone's quota. While they were being written, each one had a mutation lab behind it: a regression is reapplied to a throwaway copy of the tree, and a mutation the suite stays green on counts as a finding.

`--check` is side-effect free: it reads the config, hits the providers once,
prints the normalized JSON and writes nothing. That makes it a config validator
and a cron health probe. Run it inside the deployed image to
validate a credential change before restarting the service:

```bash
docker compose run --rm --no-deps --entrypoint python3 quota-panel /app/app.py --check
```

### CI and releases

Every push and pull request runs the three suites, the screenshot harness against real Chromium, and the background-shrink test, then builds the image for `linux/amd64` + `linux/arm64` and pushes to GHCR from `main` and from `v*` tags. Every action is pinned to a commit SHA, the workflow asks for `contents: read`, and a newer push cancels an older run so a stale build cannot publish itself as `latest`.

A release is a tag:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

Tagging `v1.0.0` publishes `1.0.0`, `1.0`, the short commit SHA and `latest`. No version tag has been published yet, so today the registry holds `main`, `latest` and a short SHA per commit. To update a deployment, pull the image and restart; restart only when `accounts.json` changed.

## If it does not work

**The panel starts, the log says `no account has a credential configured`, and every card is an error.** A key was written into `token_env`, which holds the *name* of a variable rather than the key. Move the value to `token`, or set the variable you named there.

**Docker created a directory where `accounts.json` should be.** The bind mount pointed at a path that did not exist, and Docker creates a directory for a missing source. Stop the container, remove the directory, write the file, start again.

**One card says the provider rejected the key.** The panel prints the provider's own HTTP status and separates the cases it can: `401` is a bad or revoked key, `403` is usually a key that is valid but missing a scope or a plan (CheaperInference needs `account:read`; on OpenCode Go a `403` means the workspace has no Go plan). `python3 app.py --check` gives the same answer without the UI.

**Everything is red after moving to another host and the config looks right.** Check the mounted path inside the container rather than on the host, and check permissions: the process runs as uid 10001 and cannot read a `600` file owned by someone else.

## Limits

* Read-only: only `GET`s, and nothing is written upstream.
* No token refresh: the providers' OAuth flows are out of scope by design, so an
  expired key is reported as `auth_error` for that account until you re-mint it and
  update the config.
* Polling is sequential, so a cycle takes roughly
  `sum(requests per account)` × latency. Comfortable to a few dozen accounts; a
  hanging account can stretch a cycle, bounded by `QUOTA_HTTP_TIMEOUT`.

## License

MIT, see [LICENSE](LICENSE).

The container image installs [Pillow](https://python-pillow.org/) (MIT-CMU) for the
optional background resize. The two marks in the header are
[Font Awesome Free](https://fontawesome.com/) 6.7.2 solid icons (`clock`, `arrows-rotate`),
inlined as SVG paths: those icons are CC BY 4.0, Copyright Fonticons, Inc. No icon font or
CDN stylesheet is fetched, so the page asks for nothing off-origin; the suites assert that.
