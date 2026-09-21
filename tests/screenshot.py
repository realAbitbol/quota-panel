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
BROWSER = os.environ.get(
    "SCREENSHOT_BROWSER", "/Applications/Helium.app/Contents/MacOS/Helium"
)
WIDTH, HEIGHT, SCALE = 1440, 900, 2

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
    "background_url": "",
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


class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        body = json.dumps(RESPONSES.get(path, {})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
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

    # stub provider server
    stub_port = free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", stub_port), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

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

    app = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py")], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = "http://127.0.0.1:%d" % app_port

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
        deadline = time.time() + 25
        while time.time() < deadline and ws is None:
            try:
                raw = urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % cdp_port, timeout=3).read()
                for target in json.loads(raw):
                    if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                        ws = target["webSocketDebuggerUrl"]
                        break
            except Exception:
                time.sleep(0.4)
        if not ws:
            check("the browser exposes a CDP page target", False, "timed out")
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
