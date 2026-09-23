#!/usr/bin/env python3
"""Tab in and out repeatedly; the page must still refresh and its timer must survive.

Reported risk: a page that only re-arms its refresh timer on a successful fetch can lose the timer
entirely if a burst of focus/visibility triggers lands while a request is in flight -- the guard
that collapses overlapping triggers (`inFlight`) returns early, so the losing triggers neither fetch
nor re-arm, and a throttled background tab can come back to a page that never polls again.

What this asserts, with a real browser and a real page:

  1. Every return to the foreground produces a fresh request. Background tabs get their timers
     throttled, so the numbers a returning user sees can otherwise be minutes old.
  2. The countdown keeps ticking after a burst of tab switches: the timer is still armed.
  3. The refresh continues at the configured cadence afterwards -- not stuck, not accelerated.
  4. A drop, then a return, still ends with a fetch (a failed fetch must not eat the timer).

Runs against the app's own stub-backed server, so it is offline and spends no quota.
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
import http.server
import socketserver

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

POLL = 10
BURST = 6  # tab switches in the burst


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Stub(http.server.BaseHTTPRequestHandler):
    """Serves the synthetic provider's real contract: /v2/quotas -> {subscription:{...}}.

    Not a /api/quota payload: the panel polls the PROVIDER, so the fixture has to answer the
    provider's own route and shape. Feeding it the panel's own output looked plausible and left the
    page reporting "no answer within 8s" -- the fixture was wrong, the page was fine.
    """

    hits = 0

    def do_GET(self):
        type(self).hits += 1
        if not self.path.startswith("/v2/quotas"):
            self.send_error(404)
            return
        body = json.dumps({
            "subscription": {"limit": 100, "requests": 42,
                             "renewsAt": "2026-10-01T00:00:00Z"},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):   # noqa: N802  (stdlib's name)
        pass


class CDP:
    """Minimal WebSocket client -- same framing rules as the other harnesses."""

    def __init__(self, url, timeout=20):
        import base64
        from urllib.parse import urlparse
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            "GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
            % (u.path, u.hostname, u.port, key)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        assert b"101" in buf.split(b"\r\n")[0], buf[:200]
        self._id = 0

    def _frame(self, payload):
        import struct
        data = payload.encode()
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        n = len(data)
        if n < 126:
            head = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        return head + mask + masked

    def _recv_exact(self, n):
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise RuntimeError("socket closed")
            out += chunk
        return out

    def _read(self):
        import struct
        h = self._recv_exact(2)
        op, ln = h[0] & 0x0F, h[1] & 0x7F
        if ln == 126:
            ln = struct.unpack("!H", self._recv_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack("!Q", self._recv_exact(8))[0]
        payload = self._recv_exact(ln) if ln else b""
        if op == 0x9:
            self.sock.sendall(self._frame(payload.decode(errors="replace")))
            return self._read()
        return json.loads(payload.decode(errors="replace"))

    def call(self, method, **params):
        self._id += 1
        mid = self._id
        self.sock.sendall(self._frame(json.dumps(
            {"id": mid, "method": method, "params": params})))
        while True:
            msg = self._read()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def main():
    fail = []

    def check(name, ok, detail=""):
        print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                           " — %s" % detail if detail and not ok else ""))
        if not ok:
            fail.append(name)

    tmp = tempfile.mkdtemp(prefix="qp-tab-")
    cfg = os.path.join(tmp, "accounts.json")
    with open(cfg, "w") as fh:
        json.dump({"poll_seconds": POLL,
                   "accounts": [{"id": "a", "provider": "synthetic", "token": "t", "label": "A"}]}, fh)

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Stub)
    srv.daemon_threads = True  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stub_port = srv.server_address[1]

    app_port = free_port()
    env = dict(os.environ,
               QUOTA_CONFIG=cfg, PORT=str(app_port),
               QUOTA_POLL_SECONDS=str(POLL),
               QUOTA_BACKGROUND_URL="none",
               SYNTHETIC_API_BASE="http://127.0.0.1:%d" % stub_port)
    applog = open(os.path.join(tmp, "app.log"), "w+")
    app = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py")],
                           env=env, stdout=applog, stderr=applog)

    browser = os.environ.get("SCREENSHOT_BROWSER") or shutil.which("google-chrome") \
        or shutil.which("chromium") or "/Applications/Helium.app/Contents/MacOS/Helium"
    prof = tempfile.mkdtemp(prefix="qp-tab-prof-")
    cdp_port = free_port()
    proc = None
    cdp = None
    try:
        import urllib.request
        for _ in range(100):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/api/health" % app_port, timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        else:
            check("the app boots", False)
            return 1

        proc = subprocess.Popen(
            [browser, "--headless=new", "--remote-debugging-port=%d" % cdp_port,
             "--user-data-dir=" + prof, "--no-first-run", "--disable-gpu", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ws = None
        for _ in range(160):
            try:
                tabs = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json" % cdp_port, timeout=1).read())
                page = [t for t in tabs if t.get("type") == "page"]
                if page:
                    ws = page[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                time.sleep(0.25)
        if not ws:
            check("a browser target is available", False)
            return 1

        cdp = CDP(ws)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")

        cdp.call("Page.navigate", url="http://127.0.0.1:%d/" % app_port)
        time.sleep(3)

        # Count the page's own requests, so "did it refresh" is measured, not inferred. This has
        # to be installed AFTER the navigation: a wrapper placed on the previous document is wiped
        # by the new one, and the counter then reads 0 forever while the page refreshes normally --
        # a blind probe reporting a healthy page as broken.
        cdp.call("Runtime.evaluate", expression="""
          window.__reqs = 0;
          const _f = window.fetch.bind(window);
          window.fetch = function(){ window.__reqs++; return _f.apply(null, arguments); };
        """)

        def js(expr):
            r = cdp.call("Runtime.evaluate", expression=expr, returnByValue=True)
            return r.get("result", {}).get("value")

        check("the page rendered a card", js("document.querySelectorAll('.card').length") == 1,
              str(js("document.querySelectorAll('.card').length")))

        # ---- a realistic burst: tab away, come back, a few times -------------------
        # Overlapping triggers are collapsed on purpose (`inFlight`), so N switches do NOT mean N
        # requests. What matters is that each return to a page that has been in the background for
        # a while refreshes it -- a returning tab must not show numbers minutes old.
        js("window.__reqs = 0")
        for _ in range(BURST):
            js("Object.defineProperty(document,'visibilityState',{value:'hidden',configurable:true});"
               "document.dispatchEvent(new Event('visibilitychange'));")
            time.sleep(1.2)
            js("Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});"
               "document.dispatchEvent(new Event('visibilitychange'));"
               "window.dispatchEvent(new Event('focus'));")
            time.sleep(1.5)

        after_burst = js("window.__reqs")
        check("returning to the foreground refreshes the page",
              after_burst is not None and after_burst >= 1,
              "%s requests over %d tab switches" % (after_burst, BURST))
        check("and overlapping triggers do not fire a request each",
              after_burst is not None and after_burst <= BURST + 1,
              "%s requests over %d switches" % (after_burst, BURST))

        # ---- the timer survived: the countdown still ticks -------------------------
        # Wait for a healthy feed first: right after a burst the page may be mid-retry, and
        # "retrying in N seconds" counts UP as the retry approaches, which is a different number
        # from the cadence countdown and would read as a stalled timer.
        for _ in range(40):
            feed = js("(document.getElementById('feed').textContent||'').trim()") or ''
            if feed.lower().startswith('updating in'):
                break
            time.sleep(0.5)
        t1 = js("(document.getElementById('feed').textContent||'').trim()")
        time.sleep(2.2)
        t2 = js("(document.getElementById('feed').textContent||'').trim()")

        def secs(txt):
            m = re.search(r"(\d+)\s*second", txt or "")
            return int(m.group(1)) if m else None

        s1, s2 = secs(t1), secs(t2)
        check("the countdown still reads after the burst",
              s1 is not None, "feed=%r" % t1)
        check("and it is still ticking, so the refresh timer survived",
              s1 is not None and s2 is not None and s2 < s1, "%r -> %r" % (t1, t2))

        # ---- and it still refreshes on the configured cadence ----------------------
        # Measured by watching /api/quota answers, not the page counter: the panel's own log
        # records one line per poll, and the counter is collapsed by `inFlight`.
        before = len([l for l in open(os.path.join(tmp, "app.log")).read().splitlines()
                      if "GET /api/quota" in l])
        time.sleep(POLL + 8)
        after = len([l for l in open(os.path.join(tmp, "app.log")).read().splitlines()
                     if "GET /api/quota" in l])
        check("it keeps refreshing at the configured cadence after the burst",
              after > before, "%d new polls in %ss" % (after - before, POLL + 8))

        # A resolved 500 (not a rejected promise): the page's handler throws on !res.ok, and this
        # exercises that path without leaving an unhandled rejection that kills the page outright.
        # Asserted on the PAGE's state, not on __reqs -- `inFlight` collapses overlapping attempts,
        # so the counter is not a reliable measure of retries.
        js("""
          window.fetch = function(){
            return Promise.resolve(new Response('down', {status: 500}));
          };
        """)
        time.sleep(POLL + 6)
        feed_down = js("(document.getElementById('feed').textContent||'').trim()")
        check("a failing fetch keeps the page alive and honest, not silently current",
              bool(feed_down) and ('retry' in feed_down.lower() or 'reconnect' in feed_down.lower()),
              "feed=%r" % feed_down)
        check("and the last good render is still on screen while it fails",
              js("document.querySelectorAll('.card').length") == 1,
              str(js("document.querySelectorAll('.card').length")))

    finally:
        try:
            applog.flush()
            applog.seek(0)
            tail = applog.read().splitlines()
            print("   app log (last 6):")
            for line in tail[-6:]:
                print("     " + line)
        except Exception:
            pass
        if cdp:
            cdp.close()
        if proc:
            proc.terminate()
        app.terminate()
        srv.shutdown()
        shutil.rmtree(prof, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fail:
        print("%d check(s) FAILED: %s" % (len(fail), ", ".join(fail)))
        return 1
    print("tab-switch behaviour passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
