#!/usr/bin/env python3
"""quota-panel — read-only quota dashboard for CommandCode + OpenCode Go accounts.

Polls each provider's own usage API for every configured account and serves:
  GET /                dark card UI (progress bars + live reset countdowns)
  GET /background      artwork: the configured background_url image, else 404
  GET /api/quota       normalized JSON (accounts -> windows)
  GET /api/homepage    flat widget list for a gethomepage customapi tile
  GET /api/health      liveness + poll age

Design constraints (deliberate):
  * stdlib only — no pip deps, no lockfile drift, tiny image.
  * read-only: every provider call is a GET; nothing is ever written upstream.
  * a failing account degrades alone; the panel keeps serving the others.

CLI: `app.py --check` polls once, prints JSON, exits (no server, no DB write).
"""

import io
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

# ----------------------------------------------------------------------------- config

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("QUOTA_CONFIG", os.path.join(ROOT, "accounts.json"))
STATIC_DIR = os.path.join(ROOT, "static")
def _env_int(name, default):
    """An environment integer, or the default.

    Read at import, before the config loader can report anything: a typo here used to be a
    bare traceback with no hint about which variable was at fault.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        print("[quota-panel] %s=%r is not an integer — using %d" % (name, raw, default), flush=True)
        return default


POLL_SECONDS = _env_int("QUOTA_POLL_SECONDS", 60)
HTTP_TIMEOUT = _env_int("QUOTA_HTTP_TIMEOUT", 20)
PORT = _env_int("PORT", 8080)
# Provider bodies are kilobytes. The timeout bounds latency, not size, so the read is capped
# too: a host that streams forever must not be able to grow this process without bound.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# The panel's artwork comes from `background_url` (or the env var), and falls back to
# BACKGROUND_URL_DEFAULT when neither says anything. It is fetched once at startup into the
# container's non-persistent /tmp — a tmpfs in both compose files — and served from there. The
# image is the only thing that lives in the repository as a URL: a dead or slow host costs the
# panel its artwork and never its function, /background answers 404, and the page keeps the
# backdrop it draws itself. The URL may be signed, so it is never logged and never echoed by
# /api/health.
BACKGROUND_URL_ENV = "QUOTA_BACKGROUND_URL"
# The artwork the panel points at out of the box, as a URL rather than as a copy in the image.
# The app fetches it through exactly the path a configured URL takes, so the repository
# redistributes nobody's file, and the default is as replaceable as any other setting. Because
# this default reaches the network, it needs a switch that is not "edit app.py": the suites, CI
# and an offline install all have to be able to say "no artwork at all", and so does anyone who
# would rather the panel called nobody but their providers.
BACKGROUND_URL_DEFAULT = (
    "https://r4.wallpaperflare.com/wallpaper/65/18/546/"
    "ai-art-city-street-lofi-japan-hd-wallpaper-d8618916d8ff4e5a70f17a71496ff810.jpg"
)
# Written in either the config file or the environment, these mean "no artwork": nothing is
# fetched and /background has nothing to serve. "No opinion" and "nothing, please" are different
# answers and stay different.
BACKGROUND_URL_OFF = ("none", "off")
BACKGROUND_DIR = os.environ.get("QUOTA_BACKGROUND_DIR", "/tmp/quota-panel")
BACKGROUND_TIMEOUT = int(os.environ.get("QUOTA_BACKGROUND_TIMEOUT", "20"))
BACKGROUND_MAX_BYTES = 8 * 1024 * 1024
BACKGROUND_RETRY_SECONDS = 300
# Content type -> the extension the download is stored under. What is *served* comes from
# this same table, so the browser is never told a type the bytes do not claim to be.
BACKGROUND_TYPES = {
    "image/webp": ".webp",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/avif": ".avif",
    "image/gif": ".gif",
}

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
# The header is version-sensitive: /alpha/* is CLI-internal, and a third-party integration note
# warns that `x-command-code-version` must track the CLI (CLIProxyAPI discussion #4007), so a
# header older than the contract source is a silent 4xx risk. The contract source cited below
# shipped as command-code@1.58.0; this constant is what the adapter has been run against. The
# two disagree and it is NOT established which one the route requires, so the honest move is to
# keep the value that is exercised in daily use and record the discrepancy instead of bumping a
# header to a number nobody has tested.
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
# Background state. `served` is what /background hands out right now, which is how
# /api/health proves which image is live without ever echoing the URL.
BACKGROUND = {
    "url": None, "path": None, "ctype": None, "bytes": 0,
    "error": None, "attempted_at": 0.0, "served": "none",
}
BACKGROUND_LOCK = threading.Lock()


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
    except IsADirectoryError:
        # What a missing bind-mount source looks like: docker creates the path as
        # a directory. Reporting it as "not found" is the honest message.
        raise ConfigError(
            "config path is a directory, not a file: %s "
            "(a bind mount whose source does not exist creates one)" % path
        )
    except FileNotFoundError:
        raise ConfigError("config file not found: %s" % path)
    except PermissionError:
        raise ConfigError("config file is not readable by this user: %s" % path)
    except OSError as exc:
        raise ConfigError("config file could not be read (%s): %s" % (path, exc))
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
        if provider not in PROVIDERS:
            raise ConfigError(
                "accounts[%d].provider must be one of %s (got %r)"
                % (idx, ", ".join(sorted(PROVIDERS)), provider)
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
                    "config: accounts[%d] (%s) — 'token_env' holds what looks like a KEY, "
                    "not an environment variable name. For an inline key the field is "
                    "'token'. This account has no credential."
                    % (idx, item.get("id") or provider)
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
                # Optional per-account override of where the mark comes from; the
                # registry default is what makes the common case zero-config.
                "logo": provider_logo(provider),
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


def background_url_setting(raw, source):
    """One artwork setting -> a URL, "" for no artwork, or None when it says nothing.

    "Unset" and "nothing" are separate answers now that a URL ships as the default: an absent
    key has to fall through to the environment and then to BACKGROUND_URL_DEFAULT, while an
    explicit `none`/`off` has to stop that chain and leave the panel with no artwork at all.
    Neither is an error, so neither raises; a value that is neither a URL nor an off word is
    ignored, and logged, as it always was.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        log("config: %s must be an http(s) URL, or none/off — ignored" % source)
        return None
    text = raw.strip()
    if not text:
        return None
    if text.lower() in BACKGROUND_URL_OFF:
        return ""
    if not text.lower().startswith(("http://", "https://")):
        log("config: %s must be an http(s) URL — ignored" % source)
        return None
    # An interior control character survives strip(). It used to ride into the log through the
    # parser's own exception text ("URL can't contain control characters"), on a value this file
    # otherwise never logs because it may be signed. Refuse the value; do not log it.
    if any(ch < " " or ch == "\x7f" for ch in text):
        log("config: %s contains control characters — ignored" % source)
        return None
    return text


def load_background_url(path=CONFIG_PATH):
    """Which artwork to fetch: the shipped default, then the env var, then the config file wins.

    Same precedence as `poll_seconds`, so the two settings cannot behave in opposite ways.
    `none`/`off` in either source means no artwork at all and stops the chain. An absent or
    empty value means "no opinion" — the next source answers, and the shipped default answers
    last, so a config that never mentions the artwork still gets the wallpaper.
    """
    chosen = background_url_setting(os.environ.get(BACKGROUND_URL_ENV), BACKGROUND_URL_ENV)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = None
    if isinstance(doc, dict):
        declared = background_url_setting(doc.get("background_url"), "background_url")
        if declared is not None:
            chosen = declared
    if chosen is None:
        chosen = BACKGROUND_URL_DEFAULT
    return chosen or None


# ------------------------------------------------------------------ custom background
# A configured background is usually a phone photo or a wallpaper straight out of a camera:
# 12 to 48 megapixels, several megabytes, and no better looking at dashboard size.
# Bring it down to 4K and re-encode as WebP. This is the only place the panel spends a
# dependency (Pillow); without it the bytes are served untouched, exactly as before.
BACKGROUND_MAX_EDGE = (3840, 2160)
BACKGROUND_WEBP_QUALITY = 82


def shrink_background(body, ctype):
    """Cap an image at 4K and re-encode it as WebP.

    Returns (bytes, ctype, note), or None when there is nothing to gain — no Pillow, an
    image already 4K-or-smaller WebP, or a re-encode that came out larger than the original.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        log("background: Pillow not installed — serving the image as downloaded")
        return None
    try:
        with Image.open(io.BytesIO(body)) as im:
            width, height = im.size
            resized = width > BACKGROUND_MAX_EDGE[0] or height > BACKGROUND_MAX_EDGE[1]
            if not resized and ctype == "image/webp":
                return None
            # EXIF rotation first, or a portrait photo lands on its side once the tag is lost.
            work = ImageOps.exif_transpose(im)
            if resized:
                # thumbnail() keeps the aspect ratio and never enlarges.
                work.thumbnail(BACKGROUND_MAX_EDGE, Image.LANCZOS)
            out = io.BytesIO()
            # Only the first frame survives: this is a background, not a playback surface.
            work.convert("RGB").save(out, "WEBP", quality=BACKGROUND_WEBP_QUALITY, method=4)
    except Exception as exc:  # noqa: BLE001 - any decode failure must not cost the image
        log("background: could not re-encode (%s: %s) — serving it as downloaded" % (type(exc).__name__, exc))
        return None
    data = out.getvalue()
    if len(data) >= len(body):
        return None
    note = "%s %dx%d -> %dx%d WebP, %d -> %d bytes" % (
        "resized" if resized else "converted", width, height, work.size[0], work.size[1], len(body), len(data)
    )
    return data, "image/webp", note


def fetch_background(force=False):
    """Download the configured artwork into the container's non-persistent /tmp.

    Never raises and never fatal: an unreachable image host must cost the image, not the
    service. Called at startup, then retried lazily (cooldown-bounded) while it has not
    succeeded — a container that started before the network was up heals itself.
    """
    with BACKGROUND_LOCK:
        url = BACKGROUND["url"]
        if not url:
            return
        if BACKGROUND["path"] and not force:
            return
        if not force and time.time() - BACKGROUND["attempted_at"] < BACKGROUND_RETRY_SECONDS:
            return
        BACKGROUND["attempted_at"] = time.time()

    def _give_up(reason):
        # This reason is published by /api/health, and a fetch failure's exception text quotes
        # the URL it failed on — sometimes whole, sometimes as a path+query fragment, and that
        # URL may carry a signature. Scrub every part of it, longest first, before the text is
        # either published or logged.
        parsed = urlparse(url)
        fragments = [url, parsed.netloc]
        if parsed.query:
            fragments.append(parsed.path + "?" + parsed.query)
            fragments.append(parsed.query)
        fragments.append(parsed.path)
        for fragment in sorted({f for f in fragments if f}, key=len, reverse=True):
            reason = reason.replace(fragment, "<the configured image URL>")
        with BACKGROUND_LOCK:
            BACKGROUND["path"] = None
            BACKGROUND["served"] = "none"
            BACKGROUND["error"] = reason
        log("background: %s — serving no artwork" % reason)

    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "quota-panel/1.0", "Accept": "image/*"}, method="GET"
        )
        with urllib.request.urlopen(request, timeout=BACKGROUND_TIMEOUT) as resp:
            header = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            body = resp.read(BACKGROUND_MAX_BYTES + 1)
    except Exception as exc:  # noqa: BLE001 - any network failure is survivable
        _give_up("could not fetch the configured image (%s: %s)" % (type(exc).__name__, exc))
        return

    if len(body) > BACKGROUND_MAX_BYTES:
        _give_up("the configured image is larger than %d MB" % (BACKGROUND_MAX_BYTES // (1024 * 1024)))
        return
    if len(body) < 128:
        _give_up("the configured image is too small to be an image (%d bytes)" % len(body))
        return
    suffix = os.path.splitext(urlparse(url).path)[1].lower()
    ctype = header if header in BACKGROUND_TYPES else {
        ".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".avif": "image/avif", ".gif": "image/gif",
    }.get(suffix)
    if not ctype:
        _give_up("unsupported image type %r" % (header or suffix or "unknown"))
        return

    shrunk = shrink_background(body, ctype)
    if shrunk:
        body, ctype, note = shrunk
        log("background: %s" % note)
    target = os.path.join(BACKGROUND_DIR, "background" + BACKGROUND_TYPES[ctype])
    partial = target + ".part"
    try:
        os.makedirs(BACKGROUND_DIR, exist_ok=True)
        with open(partial, "wb") as fh:
            fh.write(body)
        os.replace(partial, target)
    except OSError as exc:
        # A half-written file left behind is inherited by the next attempt, and /tmp state can
        # outlive the container when the tmpfs is shared with the host.
        try:
            os.unlink(partial)
        except OSError:
            pass
        _give_up("could not store the image in %s (%s)" % (BACKGROUND_DIR, exc))
        return

    with BACKGROUND_LOCK:
        BACKGROUND.update(
            {"path": target, "ctype": ctype, "bytes": len(body), "error": None, "served": "remote"}
        )
    log("background: fetched %d bytes (%s) from the configured URL" % (len(body), ctype))


# ------------------------------------------------------------------------------ helpers


TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def iso_to_epoch(text):
    """Epoch seconds for a 'YYYY-MM-DDTHH:MM:SSZ' stamp, or None if unparseable."""
    if not isinstance(text, str):
        return None
    try:
        return int(datetime.strptime(text, TS_FORMAT).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def http_get_json(url, headers, timeout=HTTP_TIMEOUT):
    """Return (status, body_json_or_None, error_string_or_None). Never raises."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - body may be unreadable
            body = ""
        return exc.code, None, "HTTP %s: %s" % (exc.code, body[:200].replace("\n", " "))
    except Exception as exc:  # noqa: BLE001 - network layer, any failure is reported
        return None, None, "network error: %s" % exc
    if len(raw) > MAX_RESPONSE_BYTES:
        return status, None, "response body exceeded %d bytes" % MAX_RESPONSE_BYTES
    body = raw.decode("utf-8", "replace")

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
                                          "CommandCode studio.", status=status)
    if err_w and status == 404:
        return _fail(account, "no_access", "CommandCode /alpha/whoami returned 404 — the route "
                                           "was not found. A 404 cannot say which of these it "
                                           "is: a plan without API access, a moved or renamed "
                                           "route, or a base URL that is not this API "
                                           "(COMMANDCODE_API_BASE).", status=404)
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
    usage_data = unwrap(usage, "data") or unwrap(usage) or {}
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

    if not windows:
        # Every other adapter refuses an empty result rather than publishing `state: ok` with
        # no window: a green "live" pill with nothing under it reads as "everything is fine"
        # when the truth is that nothing was understood.
        return _fail(
            account,
            "shape_unknown",
            "CommandCode answered but carried no recognisable window (whoami, credits, "
            "subscriptions and usage/summary were all unreadable)",
        )

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
            status=status,
        )
    if err and status is None:
        return _fail(account, "network_error", err)
    usage = unwrap(body, "usage")
    if usage is None:
        return _fail(account, "provider_error", err or "no 'usage' object in response", status=status)

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


# ----------------------------------------------------------------------------- redaction


# What a provider key looks like when a provider quotes it back at you in an error body.
# Used both to redact error text and to refuse a provider-supplied label that is really a
# fragment of the credential. The trailing run is 20+ characters on purpose: it keeps
# account ids and widget keys ("cc-work_error", "ci-main_five_hour") out of the net.
CREDENTIAL_SHAPE = re.compile(r"\b(?:sk|user|cmd|ci_live)[-_][A-Za-z0-9._-]{20,}")


def _redact(text: Any, *secrets: Any) -> Any:
    """Replace credential material in a string.

    A provider that rejects a key often echoes it back, sometimes as a prefix rather than
    the whole value, so the account's own key is redacted along with the slices a padded or
    truncated echo would carry. Anything else shaped like a key goes too.
    """
    if not isinstance(text, str) or not text:
        return text
    for secret in secrets:
        if not isinstance(secret, str) or len(secret) < 6:
            continue
        views = [secret]
        for size in (8, 12, 16, 24, 32, 48):
            if len(secret) > size:
                views.append(secret[:size])
                views.append(secret[-size:])
        for view in views:
            text = text.replace(view, "***")
    return CREDENTIAL_SHAPE.sub("***", text)


def _scrub_result(result: Any, *secrets: Any) -> Any:
    """Redact every string in a result tree, whatever key it sits under.

    Publishing a credential is the one failure this panel cannot recover from, so the scrub
    is total rather than per-field: a future adapter field cannot leak by omission.
    """
    if isinstance(result, str):
        return _redact(result, *secrets)
    if isinstance(result, dict):
        return {key: _scrub_result(value, *secrets) for key, value in result.items()}
    if isinstance(result, list):
        return [_scrub_result(item, *secrets) for item in result]
    return result


def _fail(account, state, message, *, status=None) -> dict:
    return {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
        "kind": provider_kind(account["provider"]),
        "contract": PROVIDERS.get(account["provider"], {}).get("contract"),
        "contract_note": PROVIDERS.get(account["provider"], {}).get("contract_note"),
        "logo": provider_logo(account["provider"]),
        "state": state,
        # `status` is the provider's own HTTP code, kept separate from `state`: a card
        # must be able to say "the provider answered 500" without inventing a reason.
        "http_status": status,
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


# Provider registry. `kind` says which card variant the provider feeds:
#   window  -> an envelope that refills on a clock (percent is the whole story)
#   balance -> prepaid money with no cap (percent would need a denominator
#              nobody publishes, so the card shows money instead)
# `contract` records what the adapter was actually verified against, and is served by
# /api/providers so the public UI can never imply more than was measured:
#   live        -- exercised against a real response from the provider
#   documented  -- built from the vendor's own published contract, not yet hit live
#   third-party -- the vendor publishes no contract for this route; the shape comes from
#                  independent implementations, and contract_note names them rather than
#                  letting "documented" imply a vendor promise nobody made.
# `contract_note` carries the caveat a reader needs to audit the value (which implementation,
# or that the live hit left no committed artifact for CI to re-check).
# `logo` is a file under static/logos/. The UI draws every mark white (an <img> cannot inherit
# `currentColor`, so a `color:` rule on it does nothing — see static/index.html). A provider with no
# mark falls back to _fallback.svg rather than rendering an empty box.
PROVIDERS = {
    "commandcode": {
        "kind": "window", "contract": "live", "logo": "commandcode.svg",
        "label": "CommandCode",
        # "live" is the strongest word in this registry, so the claim names what backs it: this
        # adapter is exercised against the provider, from the maintainer's own account, daily.
        "contract_note": "exercised live from the maintainer's account",
    },
    "opencode_go": {
        "kind": "window", "contract": "live", "logo": "opencode_go.svg",
        "label": "OpenCode Go",
        "contract_note": "exercised live from the maintainer's account",
    },
    "openrouter": {
        "kind": "balance", "contract": "documented", "logo": "openrouter.svg",
        "label": "OpenRouter",
    },
    "cheaperinference": {
        "kind": "balance", "contract": "documented", "logo": "cheaperinference.svg",
        "label": "CheaperInference",
    },
    "deepseek": {
        "kind": "balance", "contract": "documented", "logo": "deepseek.svg",
        "label": "DeepSeek",
    },
    "kimi": {
        "kind": "balance", "contract": "documented", "logo": "kimi.svg",
        "label": "Kimi / Moonshot",
    },
    "zai": {
        # Not "documented": z.ai publishes no API reference for this route. Its own page
        # (docs.z.ai/devpack/notice/usage-revision) documents plan and quota POLICY, and
        # CodexBar's write-up tells readers to open DevTools and watch
        # api/monitor/usage/quota/limit. The shape below comes from two independent
        # implementations agreeing field by field — corroborated, but not a vendor promise.
        "kind": "window", "contract": "third-party", "logo": "zai.svg",
        "label": "z.ai GLM Coding Plan",
        "contract_note": "no vendor API reference for this route; shaped from two independent "
                         "implementations (steipete/CodexBar, bugwz/AIMeter)",
    },
    "synthetic": {
        "kind": "window", "contract": "documented", "logo": "synthetic.svg",
        "label": "Synthetic",
    },
}
LOGO_FALLBACK = "_fallback.svg"


def provider_kind(provider):
    return PROVIDERS.get(provider, {}).get("kind", "window")


def provider_logo(provider):
    """The mark for a provider, falling back to the neutral glyph.

    The fallback is what keeps an unknown provider from rendering a broken-image box
    in a card head; the file itself is asserted to exist by the test suite.
    """
    name = PROVIDERS.get(provider, {}).get("logo") or LOGO_FALLBACK
    if not os.path.exists(os.path.join(STATIC_DIR, "logos", name)):
        return LOGO_FALLBACK
    return name


FETCHERS = {"commandcode": fetch_commandcode, "opencode_go": fetch_opencode_go}

# The money-balance and GLM-plan adapters live in their own module: one file per
# provider family, each carrying the documented contract it was built against.
# Imported lazily so a syntax error in a balance adapter can never stop the panel
# from serving the two providers that were already working.
BALANCE_FETCHERS = {}


def load_balance_fetchers():
    if BALANCE_FETCHERS:
        return BALANCE_FETCHERS
    try:
        from providers_balance import BALANCE_FETCHERS as _fetchers

        BALANCE_FETCHERS.update(_fetchers)
    except Exception as exc:  # noqa: BLE001 - degraded, not dead
        log("providers_balance could not be imported (%s: %s)" % (type(exc).__name__, exc))
    return BALANCE_FETCHERS


def _annotate(account, result) -> dict:
    """Stamp the registry facts every card needs, on the success path too."""
    result["kind"] = provider_kind(account["provider"])
    result["contract"] = PROVIDERS.get(account["provider"], {}).get("contract")
    result["contract_note"] = PROVIDERS.get(account["provider"], {}).get("contract_note")
    result["logo"] = provider_logo(account["provider"])
    # The account-level `kind` must describe what was actually rendered. OpenRouter is
    # registered as a balance provider but emits a real percent window when the key has
    # a spend limit, so the card kind is derived from the windows, not the registry.
    registered = provider_kind(account["provider"])
    for win in result.get("windows", []):
        if win.get("kind") is None:
            # Defaulting to "window" turned a balance adapter that forgot the tag into an
            # unreadable percentage window; the provider's registered kind is the honest
            # default, and an adapter that knows better sets the tag explicitly.
            win["kind"] = "balance" if registered == "balance" else "window"
        if win["kind"] == "window" and win.get("percent") is None and not win.get("note"):
            win["note"] = "no reading"
    kinds = {w["kind"] for w in result.get("windows", [])}
    if kinds:
        result["kind"] = "window" if "window" in kinds else "balance"
    result.setdefault("http_status", None)
    for field in ("plan", "account_name"):
        value = result.get(field)
        if isinstance(value, str) and CREDENTIAL_SHAPE.search(value):
            # A provider-supplied identity that is really a fragment of the key is dropped
            # rather than shown as "***": the card falls back to the provider name, because
            # index.html renders `acc.plan || acc.provider`.
            result[field] = None
    return result


def poll_account(account) -> dict:
    # Publishing a credential is the one failure this panel cannot recover from, so every
    # account dict is scrubbed on the single path that both the window adapters and the
    # lazily loaded balance adapters return through.
    token = account.get("token")
    if not token:
        return _scrub_result(
            _fail(account, "auth_error", "no credential configured for this account"), token
        )
    try:
        fetcher = FETCHERS.get(account["provider"]) or load_balance_fetchers().get(
            account["provider"]
        )
        if fetcher is None:
            return _scrub_result(
                _fail(
                    account,
                    "unsupported",
                    "no adapter for provider %r" % account["provider"],
                ),
                token,
            )
        return _scrub_result(_annotate(account, fetcher(account)), token)
    except Exception as exc:  # noqa: BLE001 - a provider must never kill the poller
        return _scrub_result(
            _fail(account, "internal_error", "%s: %s" % (type(exc).__name__, exc)), token
        )


# ----------------------------------------------------------------------------- storage


def refresh_all(accounts):
    """Poll every account and publish the state the page and widgets read."""
    # Health reads this to tell "a poll is running" from "the poller is wedged": a long cycle is
    # not a fault, but a cycle that outlives its own budget is.
    with STATE_LOCK:
        STATE["poll_started_at"] = time.time()
    results = [poll_account(account) for account in accounts]
    bad = [r for r in results if r["state"] != "ok"]
    with STATE_LOCK:
        STATE["accounts"] = results
        STATE["generated_at"] = now_iso()
        STATE["errors"] = [
            {
                "id": r["id"],
                "state": r["state"],
                "error": r["error"],
                "http_status": r.get("http_status"),
            }
            for r in bad
        ]
    FIRST_POLL.set()
    log(
        "polled %d account(s), %d ok"
        % (len(results), len(results) - len(bad))
    )
    return results


# How far inside the configured interval the server republishes. The client waits the full
# configured interval and no less: a 30-second setting must refresh in 30 seconds, and the header
# prints 30 because that is what it waits. So the ordering that keeps a poll from arriving before
# the publication it is waiting for is bought here, on the server, where it is invisible -- the
# server has fresh data ready marginally before anyone asks. Paying for it on the client instead
# (client waits 1.1x) is what turned a 30s panel into a 33s one.
SERVER_REPUBLISH_MARGIN = 0.9


def poller_loop(accounts, stop_event):
    # Poll immediately, then keep a steady cadence measured from the END of each poll:
    # a plain `wait(POLL_SECONDS)` after the work stretches the interval by the fetch
    # time (measured: 60s config -> 66-68s actual).
    interval = max(1.0, POLL_SECONDS * SERVER_REPUBLISH_MARGIN)
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            refresh_all(accounts)
        except Exception as exc:  # noqa: BLE001
            log("poll cycle failed: %s" % exc)
        elapsed = time.monotonic() - started
        stop_event.wait(max(1.0, interval - elapsed))


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
            kind = win.get("kind") or "window"
            if kind == "balance":
                # A gethomepage customapi tile shows text, so a balance becomes the
                # value: "$12.40" reads correctly in a tile where "40% used" would not.
                amount = win.get("amount")
                currency = win.get("currency") or "USD"
                symbol = {"USD": "$", "CNY": "¥", "EUR": "€"}.get(currency, "")
                if amount is None:
                    value = win.get("note") or "—"
                elif symbol:
                    value = "%s%.2f" % (symbol, amount)
                else:
                    value = "%.2f %s" % (amount, currency)
                entry = {
                    "label": "%s · %s" % (res["label"], win["label"]),
                    "value": value,
                    "resets_at": win.get("resets_at") or "",
                    "percent": None,
                    "kind": "balance",
                    "amount": amount,
                    "currency": currency,
                }
                widgets.append(entry)
                items["%s_%s" % (res["id"], win["key"])] = entry
                continue
            if percent is None and win.get("note"):
                value = win["note"]
            elif percent is None:
                # Unreadable window: publish it as an explicit dash rather than
                # dropping it, so a missing widget also means a visible gap.
                value = "—"
            else:
                value = "%.0f%% used" % percent
            entry = {
                "label": "%s · %s" % (res["label"], win["label"]),
                "value": value,
                "resets_at": win["resets_at"] or "",
                "percent": percent,
                "kind": "window",
            }
            widgets.append(entry)
            items["%s_%s" % (res["id"], win["key"])] = entry
    return {"generated_at": generated_at, "widgets": widgets, "items": items}


class Server(ThreadingHTTPServer):
    """Threaded HTTP/1.0 server with an accept backlog that survives one page load.

    Responses are HTTP/1.0 (no keep-alive), so concurrency maps straight onto the accept
    queue. The stdlib default is 5, below what a single page load asks for at once —
    index.html, the artwork, the favicon, /api/quota, /api/homepage and a logo per card —
    and the losers were reset (measured from 8 simultaneous connections up).
    """

    daemon_threads = True
    request_queue_size = 64


class Handler(BaseHTTPRequestHandler):
    server_version = "quota-panel/1.0"

    def _send(self, status, payload, content_type):
        body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # /background takes its content type from the remote host's Content-Type header, so the
        # browser must not second-guess it into something executable on this origin.
        self.send_header("X-Content-Type-Options", "nosniff")
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

    def _serve_background(self):
        """Serve the artwork when there is one, and answer 404 when there is not.

        A request never waits for a download: if the startup fetch has not landed (or the
        container started before the network was ready) the retry is kicked off in the
        background — cooldown-bounded by fetch_background — and this answers 404 meanwhile.
        The page asks for /background as a decorative layer over a colour it sets itself, so a
        404 costs the artwork and leaves the panel exactly as readable. That is the point of
        shipping no fallback image.
        """
        with BACKGROUND_LOCK:
            path, ctype, url = BACKGROUND["path"], BACKGROUND["ctype"], BACKGROUND["url"]
        if not path:
            if url:
                threading.Thread(target=fetch_background, daemon=True).start()
            self._send(404, "no artwork", "text/plain; charset=utf-8")
            return
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        self._send(200, body, ctype)

    def do_GET(self):
        # A data bug must not read as an outage: an unexpected shape in state used to escape as a
        # closed connection with no status line, which gives the caller no diagnostic at all.
        try:
            self._route()
        except Exception:  # noqa: BLE001 - the boundary is the point
            log("handler error on %s:\n%s" % (self.path, traceback.format_exc()))
            try:
                self._json(500, {"error": "internal error"})
            except Exception:  # noqa: BLE001 - the client is already gone
                pass

    def _route(self):
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
        if path == "/background":
            self._serve_background()
            return
        if path == "/api/quota":
            # A page opened while the first poll is still in flight should get real data,
            # not an empty grid. Bounded so a dead provider can never hang the request.
            FIRST_POLL.wait(8)
            # Snapshot under the lock, then serialise and write outside it: holding the lock
            # across `wfile.write` let one client that stops reading block the poller, the
            # health probe and every other reader.
            with STATE_LOCK:
                payload = {
                    "generated_at": STATE["generated_at"],
                    # The server's clock at response time. A client can only guess the clock
                    # offset from `generated_at`, which folds the AGE OF THE DATA into the clock
                    # — a two-minute-old payload would look like a server two minutes behind, and
                    # every countdown and the freshness readout inherited that error.
                    "now": now_iso(),
                    "poll_seconds": POLL_SECONDS,
                    "accounts": list(STATE["accounts"]),
                }
            self._json(200, payload)
            return
        if path == "/api/homepage":
            self._json(200, homepage_widgets())
            return
        if path == "/api/providers":
            # The registry, so a consumer (or the README generation) never has to
            # guess which card kind a provider feeds or how well it was verified.
            self._json(
                200,
                {
                    "providers": [
                        {
                            "id": key,
                            "label": info["label"],
                            "kind": info["kind"],
                            "contract": info["contract"],
                            "contract_note": info.get("contract_note"),
                            "logo": "/static/logos/" + info["logo"],
                        }
                        for key, info in sorted(PROVIDERS.items())
                    ],
                    "logo_fallback": "/static/logos/" + LOGO_FALLBACK,
                },
            )
            return
        if path == "/api/health":
            with STATE_LOCK:
                generated_at = STATE["generated_at"]
                accounts = list(STATE["accounts"])
            age = None
            born = iso_to_epoch(generated_at)
            if born is not None:
                age = round(time.time() - born, 1)
            # A poll is a serial loop of HTTP calls, so a legitimate cycle outlasts any fixed
            # multiple of the cadence: 3 accounts x 4 calls x HTTP_TIMEOUT(20s) = 240s against a
            # 180s tolerance, i.e. a working panel reported unhealthy for most of its wall time
            # and Docker pulled it out of rotation. Budget from the account count, and treat a
            # poll that is still running inside its own budget as healthy — a genuinely wedged
            # poller still goes stale once that budget expires.
            cycle_budget = max(POLL_SECONDS, len(accounts) * 4 * HTTP_TIMEOUT)
            started = STATE.get("poll_started_at")
            in_flight = started is not None and (born is None or started > born)
            ok = bool(age is not None and age < POLL_SECONDS + cycle_budget)
            if not ok and in_flight and started is not None:
                ok = time.time() - started < cycle_budget
            with BACKGROUND_LOCK:
                # The URL itself is deliberately absent: it may carry a signature.
                background = {
                    "configured": bool(BACKGROUND["url"]),
                    "served": BACKGROUND["served"],
                    "bytes": BACKGROUND["bytes"] or None,
                    "error": BACKGROUND["error"],
                }
            self._json(
                200 if ok else 503,
                {
                    "status": "ok" if ok else "stale",
                    "accounts": len(accounts),
                    "ok_accounts": len([a for a in accounts if a["state"] == "ok"]),
                    "last_poll_age_s": age,
                    "poll_in_flight": in_flight,
                    "background": background,
                },
            )
            return
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # keep the container log signal-rich
        # `args[0]` is an HTTPStatus on every send_error path (501/400/414 …),
        # so the membership test must not assume a string: it used to raise,
        # which turned every refused request into a closed socket and a traceback.
        line = args[0] if args and isinstance(args[0], str) else ""
        if "/api/health" not in line:
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
        results = refresh_all(accounts)
        print(json.dumps({"accounts": results}, indent=2, ensure_ascii=False))
        return 0 if all(r["state"] == "ok" for r in results) else 1

    # Deliberately after the --check early return: a probe must not fetch anything.
    BACKGROUND["url"] = load_background_url()
    if BACKGROUND["url"]:
        log("background: fetching the artwork URL off the startup path")
        threading.Thread(target=fetch_background, args=(True,), daemon=True).start()
    else:
        log("background: no artwork configured")

    stop_event = threading.Event()
    thread = threading.Thread(target=poller_loop, args=(accounts, stop_event), daemon=True)
    thread.start()

    server = Server(("0.0.0.0", PORT), Handler)
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
