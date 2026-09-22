#!/usr/bin/env python3
"""Capture docs/screenshot.png from a real page render.

Why this exists as a repo file: the screenshot is part of the README, and the previous
capture was a throwaway script that quietly shipped a broken logo (the harness refused
/static/* so the header icon 404'd). A committed harness with an explicit assertion on
every asset means that failure cannot recur silently.

Usage:
    python3 tests/screenshot.py                 # writes docs/screenshot.png
    SCREENSHOT_OUT=/tmp/x.png python3 tests/screenshot.py

In CI, set `CI=1`: a missing browser is then a failure instead of a green skip.

Requires a Chromium-family browser with CDP. Set SCREENSHOT_BROWSER to override the
default path. The app is booted on a free port with a synthetic config, so no real
credential and no provider call is involved — the numbers are fixed, so the screenshot is
reproducible instead of drifting with live usage.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("SCREENSHOT_OUT", os.path.join(ROOT, "docs", "screenshot.png"))
HISTORY_OUT = os.environ.get("SCREENSHOT_HISTORY_OUT", os.path.join(ROOT, "docs", "history.png"))
BROWSER = os.environ.get(
    "SCREENSHOT_BROWSER", "/Applications/Helium.app/Contents/MacOS/Helium"
)
WIDTH, HEIGHT, SCALE = 1440, 900, 2

# The history capture needs a store with history in it, and a store filled by waiting would take
# days. So this seeds one through the module's own write path — the same `record()` the poller
# calls — at synthetic timestamps, over the very accounts and windows the running app publishes
# (read from /api/quota, not guessed), and the app then serves it read-only. Deterministic, no
# randomness: the same bytes every run, which is what a committed screenshot has to be.
HISTORY_DAYS = 21
HISTORY_WINDOW_LABELS = {"five_hour": "5 h", "rolling": "5 h", "weekly": "Week", "monthly": "Month"}


def seeded_percent(window_index, day, step, account_index):
    """One reading: a shape a heat map can show off, and the three windows differ by nature.

    The widest window climbs day by day (that is the shape a plan actually has), the middle one
    saw-tooths, the shortest resets often — so a row is never a wall of one shade, which is the
    thing the capture has to demonstrate.
    """
    if window_index == 0:
        return min(2.0 + ((day * 7 + step * 9) % 12) + account_index, 99.0)
    if window_index == 1:
        return min(18.0 + ((day * 11 + step * 6) % 55) + account_index * 4.0, 99.0)
    return min(4.0 * day + (step * 5.0) / 3.0 + account_index * 7.5, 99.9)


def seed_history(path, accounts, now_ts=None):
    """Write `HISTORY_DAYS` of samples for every window the app publishes, and return the config.

    `accounts` is /api/quota's own list. The store is written through the repository's own module
    rather than hand-rolled SQL: if the writer's shape changes, this seeding breaks with it instead
    of quietly producing a screenshot of a store the app can no longer read.
    """
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import history

    config = dict(history.DEFAULTS)
    config.update({"enabled": True, "path": path, "sample_seconds": 300})
    now = int(now_ts if now_ts is not None else time.time())
    day_start = now - HISTORY_DAYS * 86400
    for day in range(HISTORY_DAYS):
        for step in range(6):                       # six readings a day, four hours apart
            stamp = day_start + day * 86400 + step * 14400
            if stamp > now:
                continue
            results = []
            for index, account in enumerate(accounts):
                windows = [w for w in (account.get("windows") or []) if w.get("kind") == "window"]
                if not windows:
                    continue
                results.append({
                    "id": account["id"], "provider": account.get("provider"),
                    "label": account.get("label"), "state": "ok",
                    "windows": [{"key": w.get("key"), "label": w.get("label"), "kind": "window",
                                 "percent": seeded_percent(position, day, step, index)}
                                for position, w in enumerate(windows)],
                })
            if results:
                history.record(results, config, now_ts=stamp)
    return config

# Synthetic provider responses: realistic shapes, fixed numbers. The window values mirror a
# real CommandCode/OpenCode Go response, and the balance values are the documented shape.
RESPONSES = {
    "/alpha/whoami": {"success": True, "user": {"id": "u", "name": "Ada Lovelace",
                                                "email": "ada@example.com", "userName": "ada"},
                      "org": None},
    "/alpha/billing/credits": {
        "credits": {"belowThreshold": False, "creditThreshold": 0, "monthlyCredits": 62.13,
                    "purchasedCredits": 0, "freeCredits": 0},
        "windowLimits": {"limited": True, "exceeded": None,
                         "fiveHour": {"used": 0.35, "cap": 14, "exceeded": False,
                                      "resetAt": 1790004492475},
                         "weekly": {"used": 7.87, "cap": 35, "exceeded": False,
                                    "resetAt": 1790352668025}},
    },
    "/alpha/billing/subscriptions": {"success": True, "data": {
        "id": "sub_x", "status": "active", "userId": "u", "orgId": None,
        "createdAt": "2026-08-18T09:30:47.000Z", "currentPeriodStart": "2026-09-18T09:30:47.000Z",
        "currentPeriodEnd": "2026-10-18T09:30:47.000Z", "planId": "individual-goat",
        "cancelAtPeriodEnd": False, "quantity": 1}},
    "/alpha/usage/summary": {"totalCount": 3008, "totalCost": 7.73, "successRate": 100,
                             "completedCount": 3008, "failedCount": 0,
                             "totalTokensIn": 379798851, "totalTokensOut": 4718163,
                             "totalCredits": 7.73, "periodBasis": "billing-period"},
    "/v1/credits": {"data": {"total_credits": 40, "total_usage": 38.39}},
    "/v1/key": {"data": {"label": "laptop", "limit": None, "limit_remaining": None,
                         "usage": 38.39, "usage_daily": 0.4, "usage_weekly": 3.5,
                         "usage_monthly": 3.5, "is_free_tier": False}},
    "/v1/account/balance": {"object": "account.balance", "balance_usd": 24.5,
                            "available_usd": 22.5, "reserved_usd": 2.0, "currency": "USD",
                            "auto_recharge": {"enabled": False}},
    "/user/balance": {"is_available": True, "balance_infos": [
        {"currency": "CNY", "total_balance": "110.00", "granted_balance": "10.00",
         "topped_up_balance": "100.00"}]},
    "/v1/users/me/balance": {"code": 0, "status": True, "scode": "0x0",
                             "data": {"available_balance": 49.58, "voucher_balance": 46.58,
                                      "cash_balance": 3.00}},
    "/api/monitor/usage/quota/limit": {"code": 200, "msg": "ok", "success": True, "data": {
        "planName": "GLM Coding Plan Pro", "limits": [
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "usage": 1000000,
             "currentValue": 620000, "remaining": 380000, "percentage": 62,
             "nextResetTime": 1790004492000},
            {"type": "TOKENS_LIMIT", "unit": 6, "number": 7, "usage": 5000000,
             "currentValue": 1250000, "remaining": 3750000, "percentage": 25,
             "nextResetTime": 1790352668000}]}},
    "/v2/quotas": {"subscription": {"limit": 135, "requests": 41,
                                    "renewsAt": "2026-09-21T20:36:14.288Z"}},
    # OpenCode Go: the two accounts differ so the screenshot shows a healthy range
    # rather than four identical bars.
    "/zen/go/v1/usage": {"usage": {
        "rolling": {"percent": 2, "resetsAt": 1790004492},
        "weekly": {"percent": 22, "resetsAt": 1790352668},
        "monthly": {"percent": 84, "resetsAt": 1792320000}}},
}

CONFIG = {
    "poll_seconds": 60,
    "accounts": [
        {"id": "cc-pro", "provider": "commandcode", "label": "CommandCode — Pro", "token": "x"},
        {"id": "cc-perso", "provider": "commandcode", "label": "CommandCode — perso", "token": "x"},
        {"id": "og-pro", "provider": "opencode_go", "label": "OpenCode Go — Pro", "token": "x"},
        {"id": "og-perso", "provider": "opencode_go", "label": "OpenCode Go — perso", "token": "x"},
        {"id": "zai", "provider": "zai", "label": "z.ai — GLM Coding Plan", "token": "x"},
        {"id": "syn", "provider": "synthetic", "label": "Synthetic", "token": "x"},
        {"id": "or", "provider": "openrouter", "label": "OpenRouter", "token": "x"},
        {"id": "ci", "provider": "cheaperinference", "label": "CheaperInference", "token": "x"},
        {"id": "ds", "provider": "deepseek", "label": "DeepSeek", "token": "x"},
        {"id": "kimi", "provider": "kimi", "label": "Kimi", "token": "x"},
    ],
    # The suites render offline: the app's shipped artwork default is a URL, so this starts as
    # "none", which is what keeps the harness from fetching a wallpaper host if anything below
    # fails to run. main() points it at the image this harness generates and serves, so the
    # capture renders with real artwork without the repository carrying one.
    "background_url": "none",
}

# Point every adapter at the stub. The commandcode base in app.py is a module constant, so
# it is overridden by the COMMANDCODE_API_BASE env var, which app.py reads at import.
ADAPTER_ENV = (
    "OPENROUTER_API_BASE",
    "CHEAPERINFERENCE_API_BASE",
    "DEEPSEEK_API_BASE",
    "MOONSHOT_API_BASE",
    "Z_AI_API_BASE",
    "SYNTHETIC_API_BASE",
    # A full URL, not a base: the OpenCode Go usage route lives on a fixed path.
    # Served at /zen/go/v1/usage by the stub.
    "OPENCODE_GO_USAGE_URL",
)

failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("" if ok else " — " + detail)))
    if not ok:
        failures.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def generated_artwork(size=(1600, 900)):
    """The capture's backdrop, generated here instead of shipped in the repository.

    The panel used to bundle a wallpaper and this harness leaned on it, which made that file look
    like something the project maintained. Generating one keeps the harness hermetic — no
    wallpaper host, no repo asset — while still exercising the real path: the app fetches this
    over HTTP from the stub below and serves it from /background like any configured image.

    None when Pillow is missing, in which case the capture renders with no artwork. That is what
    the panel itself does without Pillow, so the harness stays honest either way.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    top, bottom = (18, 20, 32), (58, 46, 74)
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(size[1]):
        t = y / float(size[1] - 1)
        draw.line([(0, y), (size[0], y)],
                  fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    # Shapes, so the scrim in index.html has light and dark to sit on: a flat gradient would hide
    # exactly the contrast problem the capture is there to show.
    draw.ellipse([int(size[0] * 0.60), int(size[1] * 0.08),
                  int(size[0] * 0.98), int(size[1] * 0.70)], fill=(128, 106, 154))
    draw.rectangle([0, int(size[1] * 0.80), size[0], size[1]], fill=(12, 12, 20))
    import io
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


ARTWORK = generated_artwork()


class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/artwork.png" and ARTWORK:
            body, ctype = ARTWORK, "image/png"
        else:
            body, ctype = json.dumps(RESPONSES.get(path, {})).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


# --------------------------------------------------------------------------- CDP client


class CDP:
    """Minimal CDP client over the DevTools websocket, stdlib only.

    Two details that a naive version gets wrong and that bit this harness:

      * CDP interleaves events with responses, and the page can emit events for other
        targets. `call()` therefore discards anything whose id is not the one it is
        waiting for, rather than assuming the next frame is its answer.
      * A websocket frame may arrive fragmented, so reads must loop until FIN and
        accumulate the continuation frames. A single recv() is not a frame.

    A committed harness needs no third-party dependency, and the panel's own tests are
    stdlib-only for the same reason.
    """

    def __init__(self, ws_url, timeout=30):
        import base64
        import struct
        from urllib.parse import urlparse

        parsed = urlparse(ws_url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        req = (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
            % (path, parsed.hostname, parsed.port, key)
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        assert b"101" in buf.split(b"\r\n")[0], buf[:120]
        self._id = 0
        self._events = []
        self._struct = struct

    def _send(self, payload):
        import struct

        data = json.dumps(payload).encode()
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("websocket closed")
            buf += chunk
        return buf

    def _read_message(self):
        """Read one complete text message, reassembling continuation frames."""
        import struct

        payload = b""
        while True:
            head = self._read_exact(2)
            fin = bool(head[0] & 0x80)
            opcode = head[0] & 0x0F
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            masked = bool(head[1] & 0x80)
            mask = self._read_exact(4) if masked else None
            chunk = self._read_exact(length) if length else b""
            if mask:
                chunk = bytes(b ^ mask[i % 4] for i, b in enumerate(chunk))
            if opcode == 0x8:  # close
                raise ConnectionError("websocket closed by peer")
            if opcode == 0x9:  # ping -> pong, then keep reading
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode in (0x1, 0x0):
                payload += chunk
                if fin:
                    break
                continue
            # binary or other frame: not used by CDP text APIs
            if fin:
                break
        return payload.decode("utf-8", "replace")

    def call(self, method, **params):
        self._id += 1
        target = self._id
        self._send({"id": target, "method": method, "params": params})
        while True:
            text = self._read_message()
            if not text.strip():
                continue
            msg = json.loads(text)
            if msg.get("id") == target:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})
            if "method" in msg:
                self._events.append(msg)  # buffered, never mistaken for a response


def main():
    if not os.path.exists(BROWSER):
        if os.environ.get("CI"):
            # A green skip in CI is how the most opinionated assertions in this repo went
            # unexercised: "every card is live", "no card renders an error", "every card
            # carries a loaded mark". Where a browser is expected, its absence must fail.
            print("FAIL: no browser at %s (set SCREENSHOT_BROWSER) — CI is set, so this is a "
                  "failure, not a skip" % BROWSER)
            return 1
        print("SKIP: no browser at %s (set SCREENSHOT_BROWSER)" % BROWSER)
        return 0

    # One per-run directory for everything this script writes. The config, the Chromium
    # profile and the artwork scratch were fixed /tmp paths before: two runs on one machine
    # overwrote each other's config mid-flight, and nothing removed any of it.
    scratch = tempfile.mkdtemp(prefix="quota-panel-screenshot-")

    # History on, with its store in this run's scratch directory: the capture of /history needs
    # something to draw, and leaving it off would ship a README image of an empty state.
    history_path = os.path.join(scratch, "quota.db")
    CONFIG["history"] = {"enabled": True, "path": history_path, "sample_seconds": 300}
    CONFIG["history"]["_note"] = "seeded by tests/screenshot.py: 21 days through history.record()"

    # stub provider server
    stub_port = free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", stub_port), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    # The artwork the capture renders with, generated above and served by this same stub. If
    # Pillow is missing there is none, and the config keeps "none": the capture then shows the
    # panel the way an offline install sees it.
    if ARTWORK:
        CONFIG["background_url"] = "http://127.0.0.1:%d/artwork.png" % stub_port

    cfg_path = os.path.join(scratch, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(CONFIG, fh)

    app_port = free_port()
    env = dict(os.environ)
    env.update({
        "PORT": str(app_port),
        "QUOTA_CONFIG": cfg_path,
        "QUOTA_HTTP_TIMEOUT": "5",
        "PYTHONUNBUFFERED": "1",
        "QUOTA_BACKGROUND_DIR": os.path.join(scratch, "bg"),
    })
    for var in ADAPTER_ENV:
        env[var] = "http://127.0.0.1:%d" % stub_port
    env["COMMANDCODE_API_BASE"] = "http://127.0.0.1:%d" % stub_port
    env["OPENCODE_GO_USAGE_URL"] = "http://127.0.0.1:%d/zen/go/v1/usage" % stub_port

    # Seed the history store BEFORE the app that will be captured has ever written to it.
    #
    # The order is not cosmetic: history.record() refuses a sample older than the newest one already
    # stored for that account — the gate that stops a restart from back-filling — so seeding a store
    # that already holds the app's own first poll writes exactly nothing, and the page then draws one
    # lonely dot. Measured: 126 seeding calls, zero rows.
    #
    # The window shapes still come from the app itself, via a throwaway instance on its own port and
    # its own store: the fixture must describe the windows this build actually publishes, not the
    # ones a test file believes it publishes.
    probe_store = os.path.join(scratch, "probe.db")
    probe_cfg = os.path.join(scratch, "probe-config.json")
    probe_document = dict(CONFIG, history={"enabled": True, "path": probe_store,
                                           "sample_seconds": 300})
    with open(probe_cfg, "w", encoding="utf-8") as fh:
        json.dump(probe_document, fh)
    probe_port = free_port()
    probe = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py")],
                             env=dict(env, PORT=str(probe_port), QUOTA_CONFIG=probe_cfg),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    accounts = []
    try:
        deadline = time.time() + 25
        while time.time() < deadline and not accounts:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/quota" % probe_port,
                                            timeout=5) as resp:
                    accounts = json.loads(resp.read().decode()).get("accounts") or []
            except Exception:  # noqa: BLE001 - still booting
                time.sleep(0.3)
    finally:
        probe.terminate()
        try:
            probe.wait(timeout=8)
        except subprocess.TimeoutExpired:
            probe.kill()
    published = [a for a in accounts
                 if [w for w in (a.get("windows") or []) if w.get("kind") == "window"]]
    check("the providers publish windows to seed the store with", bool(published),
          "%d accounts with a window" % len(published))
    if published:
        seeded = seed_history(history_path, published)
        counts = scalar = None
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import history as history_module
        conn = history_module.connect(history_path, read_only=True)
        try:
            counts = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            scalar = conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        finally:
            conn.close()
        check("the seeded store has history in it", (counts or 0) > 10 * len(published),
              "%s rows, %s series (config %s)" % (counts, scalar, seeded["sample_seconds"]))

    app = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py")], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = "http://127.0.0.1:%d" % app_port

    # Wait for the artwork before the browser draws anything: a capture taken a moment too early
    # would show the 404 the panel answers until the download lands, and the screenshot would be
    # of a state no user with a working URL ever sees.
    if ARTWORK:
        # No local `import urllib.request` here: a function-local import binds the name for the
        # whole function, so with Pillow missing (CI installs it a step later) the CDP wait below
        # raised UnboundLocalError, was swallowed by its own except, and the harness reported a
        # 25-second "CDP page target timed out" for a bug that had nothing to do with the browser.
        deadline = time.time() + 15
        served = {}
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=5) as resp:
                    served = json.loads(resp.read().decode()).get("background", {})
            except Exception:  # noqa: BLE001 - the app may not be listening yet
                served = {}
            if served.get("served") == "remote":
                break
            time.sleep(0.3)
        check("the capture renders with artwork fetched from this harness's own stub",
              served.get("served") == "remote", str(served))

    # browser
    profile = os.path.join(scratch, "profile")
    cdp_port = free_port()
    browser = subprocess.Popen([
        BROWSER, "--headless=new", "--remote-debugging-port=%d" % cdp_port,
        "--user-data-dir=%s" % profile, "--no-first-run", "--no-default-browser-check",
        "--hide-scrollbars", "--force-device-scale-factor=%d" % SCALE,
        "--window-size=%d,%d" % (WIDTH, HEIGHT), "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        ws = None
        last_error = None
        deadline = time.time() + 25
        while time.time() < deadline and ws is None:
            try:
                raw = urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % cdp_port, timeout=3).read()
                for target in json.loads(raw):
                    if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                        ws = target["webSocketDebuggerUrl"]
                        break
            except Exception as exc:  # noqa: BLE001 - retried until the deadline
                # Keep the reason: this loop used to swallow every failure and report a bare
                # "timed out" after 25 seconds, which reads as a slow browser even when the
                # cause is a bug in this file or a port the browser never got.
                last_error = "%s: %s" % (type(exc).__name__, exc)
                time.sleep(0.4)
        if not ws:
            check("the browser exposes a CDP page target", False,
                  "timed out on port %d%s" % (cdp_port, " (%s)" % last_error if last_error else ""))
            return 1
        check("the browser exposes a CDP page target", True)

        cdp = CDP(ws)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride", width=WIDTH, height=HEIGHT,
                 deviceScaleFactor=SCALE, mobile=False)

        # wait for the app to answer, then navigate
        deadline = time.time() + 20
        ready = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                    ready = r.status in (200, 503)
                    break
            except urllib.error.HTTPError:
                ready = True
                break
            except Exception:
                time.sleep(0.3)
        check("the app answers before capture", ready)

        cdp.call("Page.navigate", url=base + "/")
        # wait for the grid to have rendered cards
        rendered = 0
        deadline = time.time() + 25
        while time.time() < deadline:
            res = cdp.call("Runtime.evaluate",
                           expression="document.querySelectorAll('.card').length",
                           returnByValue=True)
            rendered = res.get("result", {}).get("value") or 0
            status = cdp.call("Runtime.evaluate",
                              expression="document.readyState", returnByValue=True
                              ).get("result", {}).get("value")
            if rendered >= len(CONFIG["accounts"]) and status == "complete":
                break
            time.sleep(0.4)
        check("every card rendered", rendered == len(CONFIG["accounts"]),
              "%d of %d" % (rendered, len(CONFIG["accounts"])))

        # The assertion that would have caught the broken logo last time. It must WAIT:
        # reading document.images once races the network, and every <img> is momentarily
        # complete=false / naturalWidth=0 while it loads. Poll until the count settles.
        broken = []
        deadline = time.time() + 20
        while time.time() < deadline:
            imgs = cdp.call("Runtime.evaluate", expression="""
              Array.from(document.images).map(i => ({
                src: i.getAttribute('src'),
                ok: i.complete && i.naturalWidth > 0,
                pending: !i.complete
              }))
            """, returnByValue=True).get("result", {}).get("value") or []
            pending = [i for i in imgs if i["pending"]]
            if not pending:
                broken = [i["src"] for i in imgs if not i["ok"]]
                break
            time.sleep(0.3)
        check("no broken images on the page", not broken, ", ".join(broken or ["still loading"]))

        marks = cdp.call("Runtime.evaluate", expression="""
          Array.from(document.querySelectorAll('.card img.mark'))
            .filter(i => i.complete && i.naturalWidth > 0).length
        """, returnByValue=True).get("result", {}).get("value") or 0
        check("every card carries a loaded provider mark",
              marks == len(CONFIG["accounts"]),
              "%d loaded marks for %d cards" % (marks, len(CONFIG["accounts"])))

        balances = cdp.call("Runtime.evaluate",
                            expression="document.querySelectorAll('.bal-amount').length",
                            returnByValue=True).get("result", {}).get("value") or 0
        check("balance cards rendered as money, not percent", balances >= 4,
              "%d balance values" % balances)

        # A README screenshot must not ship an error card: a stub gap is invisible in the
        # harness output but glaring in the image, so it is asserted explicitly.
        errors = cdp.call("Runtime.evaluate", expression="""
          Array.from(document.querySelectorAll('.card')).filter(c => c.querySelector('.err'))
            .map(c => c.querySelector('.label').textContent)
        """, returnByValue=True).get("result", {}).get("value") or []
        check("no card renders an error in the screenshot", not errors, ", ".join(errors))

        # The pill must be the registry's own word, per card. Membership in a set is not enough:
        # a UI that flattened every healthy card to one word would still pass it — and that is
        # exactly how z.ai, registered `third-party` because no vendor publishes its route,
        # rendered as "documented". /api/quota carries the contract the server decided on, so
        # the rendered pill is compared against the served value for each card.
        pills = cdp.call("Runtime.evaluate", expression="""
          Array.from(document.querySelectorAll('.card')).map(c => ({
            label: c.querySelector('.label').textContent,
            pill: c.querySelector('.pill').textContent.trim()
          }))
        """, returnByValue=True).get("result", {}).get("value") or []
        with urllib.request.urlopen(base + "/api/quota", timeout=5) as resp:
            served = json.loads(resp.read().decode())
        expected = {a["label"]: a["contract"] for a in served["accounts"]}
        wrong = [(c["label"], c["pill"], expected.get(c["label"])) for c in pills
                 if expected.get(c["label"]) != c["pill"]]
        check("every card's pill is the contract the API published", not wrong, str(wrong))
        words = sorted({c["pill"] for c in pills})
        check("the page does not flatten the contract vocabulary to one word",
              len(words) > 1, str(words))
        check("a third-party contract renders as third-party", "third-party" in words, str(words))

        # The header's one number is the countdown to the page's next poll. It must read one
        # cadence (the server's period, plus a design skew so a poll never arrives fractionally
        # before the publication it is waiting for), and it must stay that way.
        #
        # The old countdown aligned to the publication stamp. When a poll arrived before the next
        # publication, it re-armed 2.5s out and stayed there — the header counted 3, 2, 1 forever
        # while the numbers moved once a minute. So this asserts a FLOOR, not just a ceiling: the
        # bug was a too-small number, and a ceiling-only check passed it happily.
        def feed_text():
            return cdp.call("Runtime.evaluate",
                            expression="document.getElementById('feed').textContent",
                            returnByValue=True).get("result", {}).get("value") or ""

        pattern = r"^(?:⟳\s*)?(updating|retrying) in (\d+) seconds?$"
        feed = feed_text()
        deadline = time.time() + 6
        while time.time() < deadline and not re.match(pattern, feed.strip()):
            time.sleep(0.4)
            feed = feed_text()
        m = re.match(pattern, feed.strip())
        check("the header counts down to the next update in plain words", bool(m), repr(feed))
        if m:
            cadence = served["poll_seconds"]
            first = int(m.group(2))
            # Floor and ceiling: one cadence plus this page's 1.1 poll margin and rounding. Never
            # the few-second value the stamp-alignment bug produced, and never more than the margin
            # the page actually waits.
            # The client waits the configured cadence itself -- the margin lives on the server,
            # where it does not lengthen what the user asked for.
            step = cadence
            check("  -> is the cadence the user configured, not a spinner",
                  cadence <= first <= step + 1,
                  "countdown says %ss, /api/quota publishes every %ss"
                  % (first, cadence))
            time.sleep(2.4)
            after = feed_text()
            m2 = re.match(pattern, after.strip())
            check("  -> and it actually ticks down",
                  bool(m2) and int(m2.group(2)) < first,
                  "%r then %r" % (feed, after))
            # The regression this file exists to prevent: the value must not decay toward a
            # single-digit retry loop as the page keeps polling. Sample it well past the cadence
            # to be sure the schedule re-arms at the cadence and not at a retry constant.
            check("  -> and never decays toward a few seconds",
                  bool(m2) and int(m2.group(2)) >= cadence - 5,
                  "%r after 2.4s of a %ss cadence" % (after, cadence))
            # The reader must be able to trust the unit: the number can exceed the cadence by the
            # page's own margin, but it must never claim a longer period than that.
            check("  -> the header never claims a period it does not wait",
                  bool(m2) and int(m2.group(2)) <= step + 1,
                  "%r vs a %ss cadence + margin" % (after, cadence))

        # The mark is inline SVG, so it renders with the page's own colour and with no icon font to
        # fetch: a CDN <link> would be a fourth-party request the panel promises not to make. Emoji
        # in the same slot would fall back to whatever font the viewer has.
        #
        # #generated (the "updated HH:MM:SS" stamp) was removed: it repeated what the countdown and
        # the cards already said. Asserting its absence keeps it from creeping back.
        icons = cdp.call("Runtime.evaluate", expression="""
          ({
            feed: document.querySelectorAll('#feed svg').length,
            generated: document.getElementById('generated') ? 1 : 0,
            emoji: /[\\u23f0\\uD83D\\uDD04\\u26a0\\u26d4\\ufe0f]/.test(
                     document.getElementById('feed').textContent),
            external: Array.from(document.querySelectorAll('link[href],script[src],img[src]'))
                        .filter(e => /^https?:/.test(e.getAttribute('href') || e.getAttribute('src')))
                        .map(e => e.getAttribute('href') || e.getAttribute('src')),
            // The gap between the mark and the first word, measured in the real layout. The mark
            // is an inline-block box, so a space in the markup collapses against its edge and the
            // glyph sits glued to the text -- reading the source looks fine and the render is
            // wrong. This measures the rendered pixels instead of trusting the markup.
            gap: (() => {
              const feed = document.getElementById('feed');
              const box = feed.querySelector('.ic');
              if (!box) return null;
              // The text node is a child of #feed, NOT a sibling of the <svg>, which is the only
              // child of the .ic span. Walking from the svg finds nothing and silently reports a
              // null gap, so this looks for the first non-empty text node of #feed itself.
              const t = Array.from(feed.childNodes).find(n => n.nodeType === 3 && n.textContent.trim());
              if (!t) return null;
              const r = document.createRange();
              r.selectNodeContents(t);
              const rects = Array.from(r.getClientRects()).filter(x => x.width > 0);
              if (!rects.length) return null;
              return Math.round((rects[0].left - box.getBoundingClientRect().right) * 10) / 10;
            })()
          })
        """, returnByValue=True).get("result", {}).get("value") or {}
        check("the header mark renders as an inline icon, not emoji",
              icons.get("feed") == 1 and not icons.get("emoji"), str(icons))
        check("the header mark is not glued to the first word",
              (icons.get("gap") or 0) >= 2, "rendered gap=%rpx" % icons.get("gap"))
        check("the redundant 'updated' stamp is gone", icons.get("generated") == 0, str(icons))
        check("the page requests nothing off-origin", not icons.get("external"), str(icons))

        # freeze animations/transitions so the capture is deterministic
        cdp.call("Runtime.evaluate", expression="""
          document.querySelectorAll('.fill').forEach(e => e.style.transition = 'none');
        """)

        shot = cdp.call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        import base64
        data = base64.b64decode(shot["data"])
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "wb") as fh:
            fh.write(data)
        check("screenshot written", os.path.getsize(OUT) > 20000,
              "%d bytes" % os.path.getsize(OUT))
        print("wrote %s (%d bytes)" % (OUT, os.path.getsize(OUT)))

        # ---- the usage history page, shot from the store seeded before this app booted
        with urllib.request.urlopen(base + "/api/quota", timeout=5) as resp:
            live = json.loads(resp.read().decode())

        # Navigation is retried and its own result printed: on this browser a navigate issued while
        # the previous full-page screenshot is still settling comes back with an errorText and
        # silently leaves the old page in place, which is exactly how the first version of this
        # check measured the live panel and called it the history page.
        navigate_result = None
        for attempt in range(3):
            navigate_result = cdp.call("Page.navigate", url=base + "/history") or {}
            if not navigate_result.get("errorText"):
                break
            print("  navigate attempt %d: %s" % (attempt + 1, navigate_result.get("errorText")))
            time.sleep(1.0)
        print("  navigate -> %s" % json.dumps(navigate_result)[:200])

        state = {}
        last_probe = None
        deadline = time.time() + 35
        while time.time() < deadline:
            # Returning null until this is really the history page: an element that does not exist
            # yet makes the whole expression throw, and a thrown expression reads as "no value"
            # rather than as "not ready", which is how the first version of this check failed.
            last_probe = cdp.call("Runtime.evaluate", expression="""(() => {
              const heat = document.getElementById('heat');
              const status = document.getElementById('status-panel');
              if (!heat || !status) return null;
              return {
                ready: document.readyState,
                path: location.pathname,
                cells: document.querySelectorAll('#heat .heat-cell').length,
                rows: Math.max(0, document.querySelectorAll('#heat .heat-grid').length - 1),
                cols: document.querySelectorAll('#heat .heat-head').length,
                lines: document.querySelectorAll('#chart svg path').length,
                empties: document.querySelectorAll('#chart .empty').length,
                window: document.getElementById('window-select').value,
                windowLabels: Array.from(document.getElementById('window-select').options)
                                   .map(o => o.textContent),
                off: status.hidden === false
              };
            })()""", returnByValue=True)
            state = (last_probe.get("result") or {}).get("value") or {}
            if state.get("ready") == "complete" and state.get("cells"):
                break
            time.sleep(0.4)
        # Rows are one per account *of the window on screen*: an account that publishes no Month
        # window has no Month row, which is the point of showing one window at a time.
        selected = state.get("window")
        in_window = [a for a in live["accounts"]
                     if any(w.get("key") == selected and w.get("kind") == "window"
                            for w in (a.get("windows") or []))]
        if not state:
            print("  history probe never saw this page: %s" % json.dumps(last_probe)[:400])
        check("the history page renders its store", state.get("cells", 0) > 0,
              json.dumps(state)[:200])
        check("the history page is the page that was navigated to",
              state.get("path") == "/history" and state.get("off") is False,
              "path=%r off=%r" % (state.get("path"), state.get("off")))
        check("every account in that window has a row", state.get("rows") == len(in_window),
              "%s rows for %d accounts in %r (state %s)"
              % (state.get("rows"), len(in_window), selected, state))
        check("the trend drew a line per account", (state.get("lines") or 0) >= len(in_window),
              str(state.get("lines")))
        check("the window selector offers the windows the providers published",
              len(state.get("windowLabels") or []) >= 2
              and state.get("window") == "monthly",
              "selected %r of %s" % (state.get("window"), state.get("windowLabels")))

        # A week, so the map is a calendar rather than two columns — and because a control that
        # only works on its default value is a control nobody has tested.
        cdp.call("Runtime.evaluate", expression="""(() => {
          const sel = document.getElementById('range-select');
          sel.value = '168';
          sel.dispatchEvent(new Event('change'));
          return sel.value;
        })()""", returnByValue=True)
        week = 0
        deadline = time.time() + 20
        while time.time() < deadline:
            week = cdp.call("Runtime.evaluate", expression=
                            "document.querySelectorAll('#heat .heat-head').length",
                            returnByValue=True).get("result", {}).get("value") or 0
            if week >= 7:
                break
            time.sleep(0.4)
        check("a week draws a column per day", week >= 7, "%s columns" % week)

        # The rule this page was rewritten for, checked on the rendered pixels' own values: a
        # cell's shade is the real percentage, never a row's best day stretched to full.
        shade = cdp.call("Runtime.evaluate", expression="""(() => {
          const cell = Array.from(document.querySelectorAll('#heat .heat-cell')).find(c =>
            c.style.opacity && /: [\\d.]+%$/.test(c.getAttribute('title') || ''));
          if (!cell) return null;
          const value = parseFloat(/: ([\\d.]+)%$/.exec(cell.getAttribute('title'))[1]);
          return {opacity: parseFloat(cell.style.opacity), value: value,
                  expected: Math.max(0.08, Math.min(1, value / 100))};
        })()""", returnByValue=True).get("result", {}).get("value")
        check("a cell's shade is its real percentage",
              bool(shade) and abs(shade["opacity"] - shade["expected"]) < 0.02, str(shade))

        shot = cdp.call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        data = base64.b64decode(shot["data"])
        os.makedirs(os.path.dirname(HISTORY_OUT), exist_ok=True)
        with open(HISTORY_OUT, "wb") as fh:
            fh.write(data)
        check("history screenshot written", os.path.getsize(HISTORY_OUT) > 20000,
              "%d bytes" % os.path.getsize(HISTORY_OUT))
        print("wrote %s (%d bytes)" % (HISTORY_OUT, os.path.getsize(HISTORY_OUT)))
    finally:
        browser.terminate()
        app.terminate()
        stub.shutdown()
        for proc in (browser, app):
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(scratch, ignore_errors=True)

    print()
    if failures:
        print("%d check(s) FAILED: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("screenshot capture passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
