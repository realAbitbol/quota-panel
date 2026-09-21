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

![quota-panel dashboard](docs/screenshot.png)

*Screenshot uses synthetic data, and renders with the bundled artwork: the shipped wallpaper is fetched at runtime.*

## What you get

* Eight providers on one page: percent windows for plans that refill, money balances for prepaid credit.
* One container, no database. State lives in memory, so there is nothing to back up or migrate.
* The browser talks only to the panel, so ten open tabs cost the same as one and no page load triggers a provider request.
* `python3 app.py --check` polls once, prints the normalised JSON, and exits non-zero if an account is unhappy: a config validator and a cron probe.
* Read-only by construction. Only `GET` requests, credentials never leave the host, and no endpoint can echo one: a test stub echoes the `Authorization` header it received and no served body may contain it.
* A gethomepage tile through `/api/homepage`.

Coding subscriptions meter you in rolling windows (5 h, weekly, monthly) and mostly only tell you where you stand if you go and ask a CLI. This polls the provider APIs directly and renders one page you can leave open.

## Install

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

Write the config first: the container has nothing to poll without it, and bind-mounting a path that does not exist yet leaves a *directory* in its place. Then open <http://localhost:8080>.

The port is bound to loopback on purpose. There is no built-in login, and the page shows account labels, plan names and spend, so it belongs behind a reverse proxy that authenticates (see [Security](#security)).

`latest` is convenient, not reproducible. For production, pin a digest: `docker buildx imagetools inspect ghcr.io/realabitbol/quota-panel:latest`.

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
    read_only: true
    tmpfs:
      # 32m: an artwork fetch is read (8 MB cap) and written back as WebP, so both copies must fit.
      - "/tmp:rw,size=32m,noexec,nosuid,nodev"
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    # Measured: a 6000x4000 wallpaper shrunk to 3240x2160 peaks at 323-346 MB RSS, because Pillow
    # decodes the whole bitmap before resizing. Below 512m the shrink is OOM-killed, with the container.
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
      start_period: 60s              # /api/health answers 503 until the first poll finishes
```

`docker-compose.yml` in this repo is the same service with the fuller comments.

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

Nothing to compile. `python3 app.py` runs the same code outside a container; the only dependency is Pillow, for the artwork resize.

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

| Field | Required | Meaning |
|---|---|---|
| `id` | yes, in practice | The account's identity key. Unique, and it should not change once the panel is running. |
| `provider` | yes | One of the [providers](#providers) below. Anything else is refused at startup. |
| `label` | no | Card title. Cosmetic, defaults to `id`. |
| `token` / `token_env` / `token_file` | exactly one | The key itself, the *name* of an environment variable holding it, or a path to a file holding it. |

`id` keys everything: the `/api/quota` payload, the Homepage widget map (`items["<id>_<window>"]`, e.g. `cc-work_five_hour`) and the error list. Set it explicitly, because omitting it falls back to a positional `<provider>-<index>`, so inserting an account silently reassigns ids — with lowercase letters, digits and dashes, since the value ends up inside a widget key. Duplicate ids are fatal.

Any number of accounts, any mix of providers, polled in one pass. An account whose credential is missing or refused renders as an error card and never takes the panel down.

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

The Contract column is the provenance of the parse, and the panel serves its own word rather than re-deriving one:

* `verified live`: exercised against a real response in daily use, from the maintainer's own account (`commandcode`, `opencode_go`).
* `documented`: built from the vendor's published contract, tested against a recorded fixture. A first real key may still reveal a field nobody documents.
* `third-party`: the vendor publishes no contract for that route and the shape comes from independent implementations (`zai`; its own page documents quota *policy*, not the monitor API). Calling that `documented` would claim a vendor promise that does not exist.

`contract_note` repeats the provenance per provider, and each fixture in `tests/fixtures/providers/` records its own. `openrouter` is the one provider that can render either shape: a key with a `limit` set gets a real percent window, a key without one gets a balance and the note *"no spend cap set on this key"*.

### Icons

Every card wears its provider's mark from `static/logos/<provider>.svg`, forced white with a CSS filter (`currentColor` cannot cross an `<img>` boundary, so it would resolve to the file's own default). A missing mark falls back to `_fallback.svg` rather than an empty box, and an account can override it with `"logo": "my.svg"`. A credential-shaped label returned by OpenRouter is dropped in favour of the plain provider name, so a card never prints a key.

### `poll_seconds`

How often the providers are polled, bounded to `10`–`3600` (out-of-range values are logged and ignored). The page follows the same cadence, and its header counts down to the next update. Overridable per container with `QUOTA_POLL_SECONDS`.

> **Trap:** writing the key *into* `token_env` looks like it works: the config parses, the accounts load, the panel renders — but every lookup returns `""` and the log says `no account has a credential configured`, because the field holds the *name* of a variable. The value goes in `token`. The app detects the mistake and warns (masked) at startup.

### `background_url`

The page artwork. The panel ships with a wallpaper it fetches once at startup, and your own image can replace it:

| Value | What `/background` serves |
|---|---|
| an `http(s)` URL | that image, fetched once at startup |
| `"none"` or `"off"` | the bundled artwork, and no fetch at all |
| absent or `""` | the wallpaper the panel ships with |

A config still needs at least one account, since the panel refuses to start with none; the artwork alone is not enough.

The shipped wallpaper is
`https://r4.wallpaperflare.com/wallpaper/65/18/546/ai-art-city-street-lofi-japan-hd-wallpaper-d8618916d8ff4e5a70f17a71496ff810.jpg`,
fetched with the panel's own user agent (`200`, `image/jpeg`, 316 KB, 2912×1632 measured). It is **hotlinked, not redistributed**: the repository carries no copy, so the container asks that host for the image exactly as it would ask yours, and the bundled artwork covers the day the host stops answering. That host sits behind Cloudflare, which refuses some automated user agents with a `403` while the panel's own fetcher gets a `200` today; set `"none"` if you would rather the container talked to nobody but your providers, or you are on a metered link.

Whichever image is chosen, it is downloaded **once at container startup** and served from `/background` out of the container's non-persistent `/tmp` (a tmpfs in the compose files), so nothing about it survives a restart and the bundled image is what you get back the moment the URL stops working. Served types: `webp`, `png`, `jpeg`, `avif`, `gif`, up to 8 MB; the host's `Content-Type` decides, the URL extension is the fallback.

The download is capped at 4K and re-encoded as WebP (an image already 4K-or-smaller WebP is served untouched, and a re-encode larger than the original is discarded). Without Pillow the bytes are served as-is. How much smaller the result is is a property of the image, not a promise: a smooth 6000×4000 wallpaper measured 497 KB in and 46 KB out, a noisy photo of the same size roughly 866 KB, so size your own tmpfs and memory limit for your own image.

A fetch that fails is logged and the bundled image is served instead, retried in the background at most once every 5 minutes. The URL may be signed, so it is **never** logged and never returned by `/api/health`, which reports only whether one is configured, which image is served, and the last error. `QUOTA_BACKGROUND_DIR` and `QUOTA_BACKGROUND_TIMEOUT` tune where the copy lives and how long the fetch may take.

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

Every provider also has a `*_API_BASE` override (`COMMANDCODE_API_BASE`, `DEEPSEEK_API_BASE`, `Z_AI_API_BASE`, `MOONSHOT_API_BASE` and the rest), so the test suite can point an adapter at a local stub and run with no provider call at all. Nothing in production needs them.

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

## Homepage (gethomepage) integration

`/api/homepage` returns a `widgets` array and a flat `items` map, so either the `customapi` widget or the `customapi` + `mappings` form works:

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

Each item also carries `percent`, `resets_at` and a `detail` string, so a tile can render a progress bar instead of a bare value. An account in error publishes a single `items["<id>_error"]` entry rather than pretending to have numbers.

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

The standard library does the work, with Pillow for the artwork resize. Percentages are never invented: the monthly CommandCode figure, for instance, is derived from spend and remaining credit, and when those numbers cannot be reconciled the window renders without a percentage rather than with a wrong one. A balance the provider did not report renders as an error, never as `0.00`, because "unreadable" and "empty" mean opposite things.

The UI degrades honestly too: a failed fetch keeps the last good render on screen and labels itself (`reconnecting`, `data stale`, `feed down`) with the data's age, instead of showing stale numbers as if they were current. Background tabs get their timers throttled by the browser, so the page also refreshes on `visibilitychange`, `focus` and `online`.

<details>
<summary>Provider contracts</summary>

**CommandCode**: `https://api.commandcode.ai`

```
Authorization: Bearer *** API key>      # the user_… key from the studio
x-command-code-version: 1.54.2
x-cli-environment: production

GET /alpha/whoami?limits=1
GET /alpha/billing/credits[?orgId=<id>]
GET /alpha/billing/subscriptions[?orgId=<id>]
GET /alpha/usage/summary[?orgId=<id>]
```

* Three independent implementations agree: the `command-code` CLI bundle, the `cmd-usage` crate, and a macOS quota-bar app.
* The version header is sensitive. `/alpha/*` is CLI-internal, and `x-command-code-version` has to track the CLI: the value kept here is the one the adapter has actually been run against, and its disagreement with `command-code@1.58.0` is recorded in `app.py` rather than resolved by a guess.
* ⚠️ `/provider/v1/models` is **unauthenticated**: it answers `200` even with a bogus key, so it proves nothing about a key being alive. `/provider/v1/chat/completions` and every `/alpha/*` route answer `401` for a dead key. Verify a key with `--check`, never with `/models`.
* `billing/credits` returns `{windowLimits:{fiveHour,weekly}, credits:{monthlyCredits,…}}` and `usage/summary` returns the spend for the period; the monthly percentage is `spend / (spend + remaining)`.

**OpenCode Go**: `GET https://opencode.ai/zen/go/v1/usage`

```
Authorization: Bearer *** key>
User-Agent: <a browser UA>     # Cloudflare answers 403 (error 1010) otherwise
Accept: application/json
```

→ `{"usage":{"rolling":{percent,resetsAt},"weekly":{…},"monthly":{…}}}`. `401` = invalid key, `403` = key valid but the workspace has no Go plan. A `resetsAt` returned alongside `percent == 0` is a placeholder and is discarded.

</details>

## Security

* Read-only, outbound only: one `GET` per configured provider (up to eight hosts), plus one artwork fetch at startup: the shipped wallpaper, your own URL, or nothing at all with `none`. No write path, no upstream state change.
* Credentials stay in the config: read at startup, held in memory, never logged, never returned by any endpoint, never sent anywhere but the provider that owns them. Redaction happens in one place on the whole result, and the suite proves it end to end: the stub echoes the `Authorization` header it received and no served body may contain it.
* There is no built-in login. Run it behind something that authenticates and keep the published port on loopback. With inline `token`s, keep `accounts.json` at mode `600`/`640`, owned by the container user.
* The container is hardened: unprivileged user (uid 10001), read-only rootfs, all capabilities dropped, `no-new-privileges`, a 32 MB `noexec` tmpfs, log rotation so a crash-loop cannot fill the disk. The static route resolves and containment-checks every path, then allows a fixed extension list; `..`, symlink escapes and unexpected types are refused.

## Development

```bash
git clone https://github.com/realAbitbol/quota-panel.git
cd quota-panel
cp accounts.example.json accounts.json
python3 app.py --check                  # one poll, prints JSON, non-zero if any account is not ok
PORT=8080 python3 app.py                # run it
python3 tests/smoke.py                  # boots the app and exercises the HTTP surface
python3 tests/balance.py                # registry, balance adapters, error branches
python3 tests/background.py             # artwork shrink + URL resolution (needs Pillow)
python3 tests/screenshot.py             # renders the real page in Chromium and re-shoots the README image
```

Every suite talks to a local stub instead of a provider, so they run offline and cannot spend anyone's quota. Each one had a mutation lab behind it: a regression is reapplied to a throwaway copy of the tree, and a mutation the suite stays green on counts as a finding.

`--check` is side-effect free: it reads the config, hits the providers once, prints the normalized JSON and writes nothing — so it doubles as a config validator and a cron probe:

```bash
docker compose run --rm --no-deps --entrypoint python3 quota-panel /app/app.py --check
```

### CI and releases

Every push and pull request runs the suites and the screenshot harness against real Chromium, then builds the image for `linux/amd64` + `linux/arm64` and pushes to GHCR from `main` and from `v*` tags. Every action is pinned to a commit SHA, the workflow asks for `contents: read`, and a newer push cancels an older run so a stale build cannot publish itself as `latest`.

A release is a tag: `git tag v1.0.0 && git push origin v1.0.0` publishes `1.0.0`, `1.0`, the short commit SHA and `latest`. No version tag has been published yet, so today the registry holds `main`, `latest` and a short SHA per commit. To update a deployment, pull the image and restart; restart only when `accounts.json` changed.

## If it does not work

**The panel starts, the log says `no account has a credential configured`, every card is an error.** A key was written into `token_env`, which holds the *name* of a variable. Move the value to `token`, or set the variable you named there.

**Docker created a directory where `accounts.json` should be.** The bind mount pointed at a path that did not exist. Stop the container, remove the directory, write the file, start again.

**One card says the provider rejected the key.** `401` is a bad or revoked key; `403` is usually a valid key missing a scope or a plan (CheaperInference needs `account:read`; on OpenCode Go a `403` means the workspace has no Go plan). `python3 app.py --check` gives the same answer without the UI.

**Everything is red after moving host and the config looks right.** Check the mounted path inside the container rather than on the host, and check permissions: the process runs as uid 10001 and cannot read a `600` file owned by someone else.

**The wallpaper never appears.** The artwork fetch fails quietly and the bundled image is served instead; `/api/health` reports `background.served` and the last error.

## Limits

* Read-only: only `GET`s, nothing written upstream.
* No token refresh: the providers' OAuth flows are out of scope by design, so an expired key is reported as `auth_error` for that account until you re-mint it and update the config.
* Polling is sequential, so a cycle takes roughly `sum(requests per account)` × latency. Comfortable to a few dozen accounts; a hanging account can stretch a cycle, bounded by `QUOTA_HTTP_TIMEOUT`.
* Nothing is inferred from a missing field or a top-up: an amount the provider did not report renders as an error, never as `0.00`.

## License

MIT, see [LICENSE](LICENSE).

The container image installs [Pillow](https://python-pillow.org/) (MIT-CMU) for the optional background resize. The two marks in the header are [Font Awesome Free](https://fontawesome.com/) 6.7.2 solid icons (`clock`, `arrows-rotate`), inlined as SVG paths: those icons are CC BY 4.0, Copyright Fonticons, Inc. No icon font or CDN stylesheet is fetched, so the page asks for nothing off-origin.
