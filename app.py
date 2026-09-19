#!/usr/bin/env python3
"""quota-panel — read-only quota dashboard for CommandCode + OpenCode Go accounts.

Polls each provider's own usage API for every configured account and serves:
  GET /                dark card UI (progress bars + live reset countdowns)
  GET /api/quota       normalized JSON (accounts -> windows)
  GET /api/homepage    flat widget list for a gethomepage customapi tile
  GET /api/health      liveness + poll age

Design constraints (deliberate):
  * stdlib only — no pip deps, no lockfile drift, tiny image.
  * read-only: every provider call is a GET; nothing is ever written upstream.
  * a failing account degrades alone; the panel keeps serving the others.

CLI: `app.py --check` polls once, prints JSON, exits (no server, no DB write).
"""

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------- config

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("QUOTA_CONFIG", os.path.join(ROOT, "accounts.json"))
DB_PATH = os.environ.get("QUOTA_DB", os.path.join(ROOT, "data", "quota.db"))
STATIC_DIR = os.path.join(ROOT, "static")
POLL_SECONDS = int(os.environ.get("QUOTA_POLL_SECONDS", "60"))
HTTP_TIMEOUT = int(os.environ.get("QUOTA_HTTP_TIMEOUT", "20"))
PORT = int(os.environ.get("PORT", "8080"))

# opencode.ai sits behind Cloudflare and answers 403 (error 1010) to
# non-browser signatures — a plain python-urllib UA is refused.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# CommandCode /alpha/* requires the CLI session key (the value the official
# `command-code` CLI stores in ~/.commandcode/auth.json -> apiKey), NOT a
# provider API key. Contract cross-checked against the official CLI bundle
# (command-code@1.58.0), Jovan1666/dsh-commandcode-quota and nanvon/cc-bar.
CC_API_BASE = os.environ.get("COMMANDCODE_API_BASE", "https://api.commandcode.ai")
CC_CLI_VERSION = "1.54.2"

# planId -> (display name, nominal monthly credits)
CC_PLANS = {
    "individual-go": ("Go", 10),
    "individual-goat": ("GOAT", 70),
    "individual-pro": ("Pro", 30),
    "individual-pro-v1": ("Pro", 80),
    "individual-provider": ("Provider", 15),
    "individual-max": ("Max", 150),
    "individual-ultra": ("Ultra", 300),
    "teams-pro": ("Teams Pro", 40),
}

OG_USAGE_URL = os.environ.get(
    "OPENCODE_GO_USAGE_URL", "https://opencode.ai/zen/go/v1/usage"
)

STATE_LOCK = threading.Lock()
STATE = {"accounts": [], "generated_at": None, "errors": []}
# Set once the first poll has landed. A page opened during a restart would otherwise
# stare at an empty grid for a full poll interval — the classic reason someone reloads.
FIRST_POLL = threading.Event()


def log(msg):
    print("[quota-panel] %s" % msg, flush=True)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ----------------------------------------------------------------------------- config load


class ConfigError(Exception):
    pass


def _looks_like_literal_key(value):
    """True when `value` looks like a credential rather than an env-var NAME.

    Providers hand out `user_…` (CommandCode) and `sk-…` (OpenCode) keys; a variable
    name is upper-case with underscores. Long opaque strings count too.
    """
    if not isinstance(value, str) or not value:
        return False
    if value.startswith(("user_", "sk-", "sk_", "cmd_", "cc_")):
        return True
    return len(value) > 40 and not value.isupper()


def load_accounts(path=CONFIG_PATH):
    """Read the account list. Tokens are resolved from env/secret-file, never logged."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise ConfigError("config file not found: %s" % path)
    except ValueError as exc:
        raise ConfigError("config file is not valid JSON (%s): %s" % (path, exc))

    accounts = raw.get("accounts") if isinstance(raw, dict) else raw
    if not isinstance(accounts, list) or not accounts:
        raise ConfigError("config must hold a non-empty 'accounts' list")

    out = []
    for idx, item in enumerate(accounts):
        if not isinstance(item, dict):
            raise ConfigError("accounts[%d] is not an object" % idx)
        provider = item.get("provider")
        if provider not in ("commandcode", "opencode_go"):
            raise ConfigError(
                "accounts[%d].provider must be 'commandcode' or 'opencode_go' (got %r)"
                % (idx, provider)
            )
        token = item.get("token")
        if not token and item.get("token_env"):
            holder = item["token_env"]
            token = os.environ.get(holder, "")
            if not token and _looks_like_literal_key(holder):
                # Measured foot-gun (19/09/2026): the key pasted INTO token_env.
                # The config parses and the panel renders, so it looks like it worked,
                # but every lookup returns "" and the account silently has no credential.
                # Never echo the value: in this failure mode it IS the credential.
                log(
                    "config: accounts[%d] (%s) — 'token_env' holds what looks like a KEY "
                    "(%s…, %d chars), not an environment variable name. For an inline key "
                    "the field is 'token'. This account has no credential."
                    % (idx, item.get("id") or provider,
                       holder[:4], len(holder))
                )
        if not token and item.get("token_file"):
            try:
                with open(os.path.expanduser(item["token_file"]), "r", encoding="utf-8") as fh:
                    token = fh.read().strip()
            except OSError as exc:
                raise ConfigError(
                    "accounts[%d]: token_file unreadable (%s)" % (idx, exc)
                )
        if not token:
            # Not fatal: the account is reported with state=auth_error so the
            # panel still renders (and shows *which* account is unconfigured).
            token = ""
        out.append(
            {
                "id": item.get("id") or "%s-%d" % (provider, idx + 1),
                "provider": provider,
                "label": item.get("label") or item.get("id") or provider,
                "token": token,
            }
        )
    ids = [a["id"] for a in out]
    if len(set(ids)) != len(ids):
        raise ConfigError("account ids must be unique: %s" % ids)
    return out


def declared_poll_seconds(path=CONFIG_PATH):
    """`poll_seconds` from the config file, or None.

    The example config has always declared it; silently ignoring it meant the file
    and the running interval could disagree. Bounded to a sane range.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("poll_seconds") in (None, ""):
        return None
    try:
        value = int(raw["poll_seconds"])
    except (TypeError, ValueError):
        log("config: poll_seconds is not an integer — ignored (%r)" % raw["poll_seconds"])
        return None
    if not 10 <= value <= 3600:
        log("config: poll_seconds %d out of range 10..3600 — ignored" % value)
        return None
    return value


# ----------------------------------------------------------------------------- http


def http_get_json(url, headers, timeout=HTTP_TIMEOUT):
    """Return (status, body_json_or_None, error_string_or_None). Never raises."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - body may be unreadable
            body = ""
        return exc.code, None, "HTTP %s: %s" % (exc.code, body[:200].replace("\n", " "))
    except Exception as exc:  # noqa: BLE001 - network layer, any failure is reported
        return None, None, "network error: %s" % exc

    if status != 200:
        return status, None, "HTTP %s: %s" % (status, body[:200].replace("\n", " "))
    try:
        return status, json.loads(body), None
    except ValueError:
        return status, None, "non-JSON response: %s" % body[:120].replace("\n", " ")


def unwrap(obj, *keys):
    """Return the deepest dict reachable through `keys`, tolerating an envelope.

    The CommandCode /alpha routes answer with either a flat object or the same
    object wrapped once in `data`, and the CLI transport unwraps it itself —
    so both shapes are handled rather than guessed.
    """
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        nxt = cur.get(key)
        if nxt is None and isinstance(cur.get("data"), dict):
            nxt = cur["data"].get(key)
        cur = nxt
    return cur if isinstance(cur, dict) else None


def num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def iso_from_reset(value):
    """Accept ISO-8601, seconds or milliseconds epoch. 0/negative means 'no window'."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z") or "T" in text:
            return text
        value = num(text)
        if value is None:
            return None
    ts = num(value)
    if ts is None or ts <= 0:
        return None
    if ts > 1e12:  # milliseconds
        ts = ts / 1000.0
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def window_entry(key, label, percent, used=None, cap=None, resets_at=None, note=None):
    return {
        "key": key,
        "label": label,
        "percent": None if percent is None else round(percent, 2),
        "used": used,
        "cap": cap,
        "resets_at": resets_at,
        "note": note,
    }


# ----------------------------------------------------------------------------- providers


def fetch_commandcode(account):
    headers = {
        "Authorization": "Bearer %s" % account["token"],
        "Accept": "application/json",
        "User-Agent": "quota-panel/1.0",
        "x-command-code-version": CC_CLI_VERSION,
        "x-cli-environment": "production",
    }
    base = CC_API_BASE.rstrip("/")

    status, whoami, err_w = http_get_json(base + "/alpha/whoami?limits=1", headers)
    if err_w and status in (401, 403):
        return _fail(account, "auth_error", "CommandCode rejected the key (401/403) — it is invalid, "
                                          "revoked, or belongs to another account. Verify it in the "
                                          "CommandCode studio.")
    if err_w and status == 404:
        return _fail(account, "no_access", "CommandCode /alpha/whoami returned 404 — "
                                           "this plan may not include API access.")
    if err_w and whoami is None and status is None:
        return _fail(account, "network_error", err_w)

    org_id = None
    org = unwrap(whoami, "org")
    if org:
        org_id = org.get("id")
    qs = ("?orgId=%s" % org_id) if org_id else ""

    _, credits, err_c = http_get_json(base + "/alpha/billing/credits" + qs, headers)
    _, subs, err_s = http_get_json(base + "/alpha/billing/subscriptions" + qs, headers)

    if credits is None and subs is None:
        return _fail(
            account,
            "provider_error",
            "; ".join(x for x in [err_w, err_c, err_s] if x) or "no data from CommandCode",
        )

    credits_root = unwrap(credits) or {}
    if isinstance(credits, dict) and isinstance(credits.get("data"), dict):
        credits_root = credits["data"]
    credit_data = unwrap(credits, "credits") or {}
    window_limits = unwrap(credits, "windowLimits") or {}
    sub_data = unwrap(subs, "data") or {}

    plan_id = sub_data.get("planId") or credit_data.get("planId")
    plan_key = plan_id.lower().replace("_", "-") if isinstance(plan_id, str) else None
    plan_name, plan_credits = None, None
    if plan_key:
        plan_name, plan_credits = CC_PLANS.get(plan_key, (plan_id, None))

    windows = []
    for key, label, source in (
        ("five_hour", "5 h", window_limits.get("fiveHour") or credits_root.get("fiveHour")),
        ("weekly", "Week", window_limits.get("weekly") or credits_root.get("weekly")),
    ):
        if not isinstance(source, dict):
            continue
        used, cap = num(source.get("used")), num(source.get("cap"))
        percent = None
        if used is not None and cap:
            percent = min(100.0, used / cap * 100.0)
        windows.append(
            window_entry(key, label, percent, used, cap, iso_from_reset(source.get("resetAt")))
        )

    used_credits = num(credit_data.get("monthlyCredits"))
    remaining = None
    # CommandCode reports the *remaining* monthly allowance as monthlyCredits and
    # the spend for the period separately; without the spend there is no percent.
    spend = None
    _, usage, _ = http_get_json(base + "/alpha/usage/summary" + qs, headers)
    usage_data = unwrap(usage) or {}
    spend = num(usage_data.get("totalCredits"))
    if spend is not None and used_credits is not None:
        cap = spend + used_credits
        pct = min(100.0, spend / cap * 100.0) if cap > 0 else None
        windows.append(
            window_entry(
                "monthly",
                "Month",
                pct,
                spend,
                cap,
                iso_from_reset(sub_data.get("currentPeriodEnd")),
                note="credits",
            )
        )
        remaining = used_credits
    elif used_credits is not None:
        windows.append(
            window_entry(
                "monthly",
                "Month",
                None,
                None,
                (plan_credits or 0) or None,
                iso_from_reset(sub_data.get("currentPeriodEnd")),
                note="%s credits left" % _fmt(used_credits),
            )
        )
        remaining = used_credits

    user = unwrap(whoami, "user") or {}
    return {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
        "state": "ok",
        "error": None,
        "plan": plan_name or plan_id,
        "account_name": user.get("name") or user.get("userName") or user.get("email") or org_id,
        "monthly_remaining": remaining,
        "period_end": iso_from_reset(sub_data.get("currentPeriodEnd")),
        "totals": {
            "requests": num(usage_data.get("totalCount")),
            "tokens_in": num(usage_data.get("totalTokensIn")),
            "tokens_out": num(usage_data.get("totalTokensOut")),
            "success_rate": num(usage_data.get("successRate")),
        },
        "windows": windows,
        "fetched_at": now_iso(),
    }


def fetch_opencode_go(account):
    headers = {
        "Authorization": "Bearer %s" % account["token"],
        "Accept": "application/json",
        "User-Agent": BROWSER_UA,
    }
    status, body, err = http_get_json(OG_USAGE_URL, headers)
    if err and status in (401, 403):
        return _fail(
            account,
            "auth_error",
            "OpenCode Go rejected the key (HTTP %s). 403 = key valid but no Go plan on this "
            "workspace; 401 = key invalid." % status,
        )
    if err and status is None:
        return _fail(account, "network_error", err)
    usage = unwrap(body, "usage")
    if usage is None:
        return _fail(account, "provider_error", err or "no 'usage' object in response")

    windows = []
    for key, label, src_key in (
        ("five_hour", "5 h", "rolling"),
        ("weekly", "Week", "weekly"),
        ("monthly", "Month", "monthly"),
    ):
        block = usage.get(src_key)
        if not isinstance(block, dict):
            continue
        percent = num(block.get("percent"))
        # percent == 0 carries a placeholder reset (now + window length): showing a
        # countdown there would be a fiction, so it is dropped.
        resets_at = iso_from_reset(block.get("resetsAt")) if percent else None
        windows.append(window_entry(key, label, percent, None, None, resets_at))

    if not windows:
        return _fail(account, "shape_unknown", "unrecognised OpenCode Go response shape")
    return {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
        "state": "ok",
        "error": None,
        "plan": "OpenCode Go",
        "account_name": None,
        "monthly_remaining": None,
        "period_end": None,
        "totals": {},
        "windows": windows,
        "fetched_at": now_iso(),
    }


def _fail(account, state, message):
    return {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
        "state": state,
        "error": message,
        "plan": None,
        "account_name": None,
        "monthly_remaining": None,
        "period_end": None,
        "totals": {},
        "windows": [],
        "fetched_at": now_iso(),
    }


def _fmt(value):
    if value is None:
        return "—"
    if abs(value - round(value)) < 0.005:
        return "%d" % round(value)
    return "%.2f" % value


FETCHERS = {"commandcode": fetch_commandcode, "opencode_go": fetch_opencode_go}


def poll_account(account):
    if not account.get("token"):
        return _fail(account, "auth_error", "no credential configured for this account")
    try:
        return FETCHERS[account["provider"]](account)
    except Exception as exc:  # noqa: BLE001 - a provider must never kill the poller
        return _fail(account, "internal_error", "%s: %s" % (type(exc).__name__, exc))


# ----------------------------------------------------------------------------- storage


def db_connect():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snapshots (
               ts TEXT NOT NULL,
               account_id TEXT NOT NULL,
               window_key TEXT NOT NULL,
               percent REAL,
               used REAL,
               cap REAL,
               resets_at TEXT
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_account ON snapshots(account_id, window_key, ts)")
    conn.commit()
    return conn


def store_results(results):
    conn = db_connect()
    try:
        rows = []
        for res in results:
            for win in res["windows"]:
                rows.append(
                    (res["fetched_at"], res["id"], win["key"], win["percent"], win["used"],
                     win["cap"], win["resets_at"])
                )
        if rows:
            conn.executemany(
                "INSERT INTO snapshots (ts, account_id, window_key, percent, used, cap, resets_at) "
                "VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
        cutoff = datetime.now(timezone.utc).timestamp() - 30 * 86400
        conn.execute(
            "DELETE FROM snapshots WHERE ts < ?",
            (datetime.fromtimestamp(cutoff, timezone.utc).replace(microsecond=0)
             .isoformat().replace("+00:00", "Z"),),
        )
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------- poller


def refresh_all(accounts, store=True):
    """Poll every account, publish the state, then persist history.

    `store=False` is used by `--check`: a probe must not mutate the history
    (it also makes the probe usable without the /data volume mounted).
    """
    results = [poll_account(account) for account in accounts]
    bad = [r for r in results if r["state"] != "ok"]
    with STATE_LOCK:
        STATE["accounts"] = results
        STATE["generated_at"] = now_iso()
        STATE["errors"] = [{"id": r["id"], "state": r["state"], "error": r["error"]} for r in bad]
    FIRST_POLL.set()
    if store:
        try:
            store_results(results)
        except Exception as exc:  # noqa: BLE001 - history is best-effort
            log("history write failed: %s" % exc)
    log(
        "polled %d account(s), %d ok"
        % (len(results), len(results) - len(bad))
    )
    return results


def poller_loop(accounts, stop_event):
    # Poll immediately, then keep a steady cadence measured from the END of each poll:
    # a plain `wait(POLL_SECONDS)` after the work stretches the interval by the fetch
    # time (measured: 60s config -> 66-68s actual).
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            refresh_all(accounts)
        except Exception as exc:  # noqa: BLE001
            log("poll cycle failed: %s" % exc)
        elapsed = time.monotonic() - started
        stop_event.wait(max(1.0, POLL_SECONDS - elapsed))


# ----------------------------------------------------------------------------- server


def homepage_widgets():
    """Two shapes in one payload.

    `widgets` is an ordered list (humans, mqtt-style consumers).
    `items` is the same data keyed by "<account_id>_<window>" because the
    gethomepage `customapi` widget maps fields by path and needs stable keys.
    """
    with STATE_LOCK:
        accounts = list(STATE["accounts"])
        generated_at = STATE["generated_at"]
    widgets, items = [], {}
    for res in accounts:
        if res["state"] != "ok":
            entry = {
                "label": res["label"],
                "value": "error",
                "detail": res["error"] or res["state"],
                "percent": None,
                "resets_at": "",
            }
            widgets.append(entry)
            items["%s_error" % res["id"]] = entry
            continue
        for win in res["windows"]:
            percent = win["percent"]
            if percent is None and win.get("note"):
                value = win["note"]
            elif percent is None:
                continue
            else:
                value = "%.0f%% used" % percent
            entry = {
                "label": "%s · %s" % (res["label"], win["label"]),
                "value": value,
                "resets_at": win["resets_at"] or "",
                "percent": percent,
            }
            widgets.append(entry)
            items["%s_%s" % (res["id"], win["key"])] = entry
    return {"generated_at": generated_at, "widgets": widgets, "items": items}


class Handler(BaseHTTPRequestHandler):
    server_version = "quota-panel/1.0"

    def _send(self, status, payload, content_type):
        body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    # Extension -> MIME. Everything the UI needs is here; anything else is refused
    # rather than guessed, so the route can never hand out an unexpected type.
    STATIC_TYPES = {
        ".webp": "image/webp",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
    }

    def _serve_static(self, path):
        """Serve files under STATIC_DIR, read-only and path-traversal safe."""
        rel = path[len("/static/"):]
        target = os.path.realpath(os.path.join(STATIC_DIR, rel))
        # Containment check on the resolved path: `..`, symlinks and absolute
        # segments all collapse before the prefix test, so /static/../../etc/passwd
        # resolves outside STATIC_DIR and is rejected.
        if not target.startswith(os.path.realpath(STATIC_DIR) + os.sep):
            self._send(403, "forbidden", "text/plain; charset=utf-8")
            return
        ctype = self.STATIC_TYPES.get(os.path.splitext(target)[1].lower())
        if not ctype:
            self._send(403, "forbidden", "text/plain; charset=utf-8")
            return
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        self._send(200, body, ctype)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "UI not found", "text/plain; charset=utf-8")
            return
        if path.startswith("/static/"):
            self._serve_static(path)
            return
        if path == "/api/quota":
            # A page opened while the first poll is still in flight should get real data,
            # not an empty grid. Bounded so a dead provider can never hang the request.
            FIRST_POLL.wait(8)
            with STATE_LOCK:
                self._json(
                    200,
                    {
                        "generated_at": STATE["generated_at"],
                        "poll_seconds": POLL_SECONDS,
                        "accounts": list(STATE["accounts"]),
                    },
                )
            return
        if path == "/api/homepage":
            self._json(200, homepage_widgets())
            return
        if path == "/api/health":
            with STATE_LOCK:
                generated_at = STATE["generated_at"]
                accounts = list(STATE["accounts"])
            age = None
            if generated_at:
                age = round(
                    datetime.now(timezone.utc).timestamp()
                    - datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc).timestamp(),
                    1,
                )
            ok = age is not None and age < POLL_SECONDS * 3
            self._json(
                200 if ok else 503,
                {
                    "status": "ok" if ok else "stale",
                    "accounts": len(accounts),
                    "ok_accounts": len([a for a in accounts if a["state"] == "ok"]),
                    "last_poll_age_s": age,
                },
            )
            return
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # keep the container log signal-rich
        if "/api/health" not in (args[0] if args else ""):
            log("%s - %s" % (self.address_string(), fmt % args))


def main(argv):
    global POLL_SECONDS
    check_only = "--check" in argv
    try:
        accounts = load_accounts()
        declared = declared_poll_seconds()
    except ConfigError as exc:
        log("config error: %s" % exc)
        return 2
    if declared:
        POLL_SECONDS = declared

    log("loaded %d account(s): %s" % (len(accounts), ", ".join(
        "%s(%s)" % (a["label"], a["provider"]) for a in accounts)))
    if not any(a["token"] for a in accounts):
        log("WARNING: no account has a credential configured")

    if check_only:
        results = refresh_all(accounts, store=False)
        print(json.dumps({"accounts": results}, indent=2, ensure_ascii=False))
        return 0 if all(r["state"] == "ok" for r in results) else 1

    stop_event = threading.Event()
    thread = threading.Thread(target=poller_loop, args=(accounts, stop_event), daemon=True)
    thread.start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log("listening on 0.0.0.0:%d (poll every %ds)" % (PORT, POLL_SECONDS))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
