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

*Screenshot uses synthetic data.*

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

The port is bound to loopback because there is no built-in login and the page shows labels, plan names and spend. Put it behind a reverse proxy that authenticates.

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
| `token` / `token_env` / `token_file` | exactly one | The key, the *name* of an env var holding it, or a path to a file holding it. |
| `poll_seconds` | no | Seconds between polls, `10`–`3600`. Default `60`. |
| `background_url` | no | Page artwork: an `http(s)` URL at the top level of the file (see the example above), `"none"` for no artwork, or empty/absent for the shipped wallpaper (hotlinked, not redistributed). Fetched once at startup, capped at 4K, re-encoded as WebP. |

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
| `QUOTA_BACKGROUND_URL` | the shipped wallpaper | Artwork URL, or `none`/`off`. The config file wins. |
| `QUOTA_BACKGROUND_DIR` | `/tmp/quota-panel` | Where the fetched artwork is stored. |
| `QUOTA_BACKGROUND_TIMEOUT` | `20` | Artwork fetch timeout, in seconds. |

Every provider also has a `*_API_BASE` override so the test suite can point an adapter at a local stub. Nothing in production needs them.

## Endpoints

| Path | Purpose |
|---|---|
| `/` | the card UI |
| `/api/quota` | normalized JSON: every account, every window, with `resets_at` |
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
* The container runs unprivileged (uid 10001) on a read-only rootfs with all capabilities dropped. The static route containment-checks every path and allows a fixed extension list.

## Development

```bash
cp accounts.example.json accounts.json
python3 app.py --check                  # one poll, prints JSON, non-zero if any account is not ok
PORT=8080 python3 app.py
python3 tests/smoke.py                  # boots the app and exercises the HTTP surface
python3 tests/balance.py                # registry, balance adapters, error branches
python3 tests/background.py             # artwork shrink (needs Pillow)
python3 tests/screenshot.py             # renders the real page in Chromium and re-shoots the README image
QUOTA_POLL_SECONDS=10 python3 tests/cadence_soak.py   # the header's countdown must not decay
```

Every suite talks to a local stub instead of a provider, so they run offline and spend no quota. Every push runs them against real Chromium, then builds `linux/amd64` + `linux/arm64` and pushes to GHCR from `main` and `v*` tags. A release is a tag: `git tag v1.0.0 && git push origin v1.0.0`.

## If it does not work

**`no account has a credential configured`, every card is an error.** A key was written into `token_env`. Move it to `token`.

**Docker created a directory where `accounts.json` should be.** The bind mount pointed at a missing path. Remove the directory, write the file, start again.

**One card says the provider rejected the key.** `401` is a bad or revoked key; `403` is usually a valid key missing a scope or a plan (CheaperInference needs `account:read`; on OpenCode Go, `403` means no Go plan). `python3 app.py --check` gives the same answer without the UI.

**Everything is red after moving host and the config looks right.** Check the path *inside* the container, and the permissions: the process runs as uid 10001 and cannot read a `600` file owned by someone else.

**The wallpaper never appears.** `/api/health` reports `background.served: none` and the last error.

## Limits

* No token refresh: the providers' OAuth flows are out of scope, so an expired key is reported as `auth_error` until you re-mint it and update the config.
* Polling is sequential: a cycle takes roughly `sum(requests per account)` × latency. Comfortable to a few dozen accounts.
* Nothing is inferred from a missing field: an amount the provider did not report renders as an error, never as `0.00`.

## License

MIT, see [LICENSE](LICENSE).

The container image installs [Pillow](https://python-pillow.org/) (MIT-CMU) for the artwork resize. The header marks are [Font Awesome Free](https://fontawesome.com/) 7.3.1 solid icons, inlined as SVG paths: CC BY 4.0, Copyright Fonticons, Inc. No icon font or CDN stylesheet is fetched.
