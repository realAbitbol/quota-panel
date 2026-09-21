#!/usr/bin/env python3
"""Watch the header countdown across several full cadences and fail on decay.

The bug this exists to catch: the page used to schedule its next fetch from a
stamp-derived instant, and any poll that arrived before the next publication
re-armed it 2.5s out. Nothing reset it to the cadence unless a newer stamp
appeared first, so the header settled into counting 3, 2, 1 forever while the
numbers moved once a minute -- a counter that never got back above a few seconds.

A healthy countdown SAWTOOTHS: it descends to 1 and re-arms at the cadence. So
the descending values are correct and meaningless; what matters is the PEAK of
each cycle. This samples continuously and asserts that the value re-arms to one
cadence every time, which is the property the old code lost. A single sample
cannot see it (it might land at "1s" on a perfectly good page), and a
ceiling-only assertion cannot either -- the failure is a peak that is too SMALL.

Run manually (it is deliberately slow), or from CI with a short cadence:

    QUOTA_POLL_SECONDS=10 python tests/cadence_soak.py

Exits non-zero on any decay, and prints the full sample trace either way.
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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROWSER = os.environ.get("SCREENSHOT_BROWSER", "/Applications/Helium.app/Contents/MacOS/Helium")

# The cadence the app under test is told to publish at. The client polls this plus its skew.
POLL = int(os.environ.get("QUOTA_POLL_SECONDS", "10"))
CADENCE_MS = POLL * 1000
SKEW = 1.1
# How long to watch: three cadences is enough for a self-referential decay to show up, because the
# old bug re-armed every 2.5s and would be unmistakable within one.
WATCH_S = float(os.environ.get("SOAK_SECONDS", str(POLL * 3 + 4)))
SAMPLE_EVERY = 0.25

STUB_PATHS = {
    "/commands": {"object": "list", "data": []},
}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the soak output clean
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        body = json.dumps(STUB_PATHS.get(path, {})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return True
        except Exception:
            time.sleep(0.2)
    return False


def main():
    if not os.path.exists(BROWSER):
        print("SKIP: no browser at %s (set SCREENSHOT_BROWSER)" % BROWSER)
        return 0

    scratch = tempfile.mkdtemp(prefix="quota-panel-soak-")
    stub_port = free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", stub_port), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    cfg = os.path.join(scratch, "config.json")
    with open(cfg, "w", encoding="utf-8") as fh:
        json.dump({
            "poll_seconds": POLL,
            "accounts": [{"id": "cc", "provider": "commandcode",
                          "label": "CommandCode", "token": "x"}],
        }, fh)

    app_port = free_port()
    env = dict(os.environ)
    env.update({
        "PORT": str(app_port),
        "QUOTA_CONFIG": cfg,
        "PYTHONUNBUFFERED": "1",
        "QUOTA_BACKGROUND_DIR": os.path.join(scratch, "bg"),
        "COMMANDCODE_API_BASE": "http://127.0.0.1:%d" % stub_port,
    })
    # Pin the cadence explicitly: the config file wins over the env var by design, so set BOTH or
    # the soak measures 60s while claiming to measure POLL.
    env["QUOTA_POLL_SECONDS"] = str(POLL)

    app = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py")], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = "http://127.0.0.1:%d" % app_port

    browser = None
    try:
        if not wait_for(base + "/api/health"):
            print("FAIL: the app never answered /api/health")
            return 1

        served = json.loads(urllib.request.urlopen(base + "/api/quota", timeout=5).read())
        published = served.get("poll_seconds")
        print("server publishes poll_seconds=%s (asked for %s)" % (published, POLL))
        if published != POLL:
            print("FAIL: the app did not honour the cadence; refusing to measure the wrong thing")
            return 1

        # Drive a real page. Reuse the app's own inline script by loading the page over CDP.
        cdp_port = free_port()
        profile = os.path.join(scratch, "profile")
        browser = subprocess.Popen([
            BROWSER, "--headless=new", "--remote-debugging-port=%d" % cdp_port,
            "--user-data-dir=%s" % profile, "--no-first-run", "--no-default-browser-check",
            "--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ws = None
        deadline = time.time() + 25
        while time.time() < deadline and not ws:
            try:
                targets = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % cdp_port, timeout=3).read())
                for t in targets:
                    if t.get("type") == "page":
                        ws = t.get("webSocketDebuggerUrl")
                        break
            except Exception:
                pass
            if not ws:
                time.sleep(0.4)
        if not ws:
            print("FAIL: no CDP page target")
            return 1

        # Minimal CDP client (no third-party imports): same framing rules as the screenshot
        # harness -- discard interleaved events, and reassemble continuation frames.
        import base64
        import struct

        c = socket.create_connection(("127.0.0.1", cdp_port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        c.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n" % (ws.split("127.0.0.1:%d" % cdp_port, 1)[-1],
                                                          key)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += c.recv(4096)
        assert b"101" in buf.split(b"\r\n")[0], buf[:200]

        def send(payload, _id=[0]):
            _id[0] += 1
            data = json.dumps(payload).encode()
            hdr = bytearray([0x81])
            n = len(data)
            if n < 126:
                hdr.append(0x80 | n)
            elif n < 65536:
                hdr.append(0x80 | 126)
                hdr += struct.pack(">H", n)
            else:
                hdr.append(0x80 | 127)
                hdr += struct.pack(">Q", n)
            mask = os.urandom(4)
            hdr += mask
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            c.sendall(bytes(hdr) + masked)
            return _id[0]

        def recv_exact(n):
            out = b""
            while len(out) < n:
                chunk = c.recv(n - len(out))
                if not chunk:
                    raise RuntimeError("socket closed")
                out += chunk
            return out

        def read_message():
            frags = b""
            while True:
                b1, b2 = recv_exact(2)
                fin = b1 & 0x80
                opcode = b1 & 0x0F
                ln = b2 & 0x7F
                if ln == 126:
                    ln = struct.unpack(">H", recv_exact(2))[0]
                elif ln == 127:
                    ln = struct.unpack(">Q", recv_exact(8))[0]
                payload = recv_exact(ln)
                if opcode == 0x9:
                    continue
                frags += payload
                if fin:
                    break
            return frags

        def evaluate(expr):
            send({"id": 999, "method": "Runtime.evaluate",
                  "params": {"expression": expr, "returnByValue": True}})
            end = time.time() + 10
            while time.time() < end:
                msg = json.loads(read_message())
                if msg.get("id") == 999:
                    return msg.get("result", {}).get("result", {}).get("value")
            return None

        send({"id": 1, "method": "Page.enable", "params": {}})
        send({"id": 2, "method": "Page.navigate", "params": {"url": base + "/"}})
        time.sleep(2.0)
        title = evaluate("document.title")
        print("page loaded, title=%r" % title)

        pattern = re.compile(r"^(updating|retrying) in (\d+) seconds?$")
        samples = []
        t0 = time.time()
        while time.time() - t0 < WATCH_S:
            txt = evaluate("document.getElementById('feed').textContent") or ""
            m = pattern.match(txt.strip())
            if m:
                samples.append((round(time.time() - t0, 2), m.group(1), int(m.group(2))))
            time.sleep(SAMPLE_EVERY)

        c.close()

        print("\n%d samples over %.1fs (cadence %ss, expect ~%d..%d):"
              % (len(samples), WATCH_S, POLL, POLL, int(CADENCE_MS * SKEW / 1000) + 2))
        # Print the trace compactly: value only, so decay is visible at a glance.
        print("  " + " ".join(str(s[2]) for s in samples))
        print("  distinct values: %s" % sorted({s[2] for s in samples}))

        if not samples:
            print("\nFAIL: never saw a countdown; the header is not rendering one")
            return 1

        if len({s[2] for s in samples}) < 3:
            print("\nFAIL: the countdown never moved across %d samples" % len(samples))
            return 1

        # Split the trace into sawtooth cycles at each upward jump, then check every peak. A peak
        # below one cadence means the timer re-armed too early, which is precisely the old bug.
        peaks = []
        run = [samples[0]]
        for prev, cur in zip(samples, samples[1:]):
            if cur[2] > prev[2]:
                peaks.append(run)
                run = [cur]
            else:
                run.append(cur)
        peaks.append(run)

        # The client waits the interval the user configured -- that is the contract now. Nothing
        # is added on the client side, so a peak BELOW the cadence is a decay and a peak far ABOVE
        # it means the page stopped honouring the setting. The old bounds allowed one cadence + 10%
        # of skew, which is exactly the 30s -> 33s behaviour this test now forbids.
        lo = POLL
        hi = POLL + 3
        peak_vals = [p[0][2] for p in peaks]
        print("  peaks per cycle: %s" % peak_vals)

        overshoot = [p for p, v in zip(peaks, peak_vals) if v > hi]
        if overshoot:
            print("\nFAIL: a cycle re-armed above the configured cadence (%s)" % max(peak_vals))
            return 1

        # Let the first cycle be partial (the page may be mid-countdown when sampling starts), and
        # require every subsequent cycle to re-arm at the cadence. That is the assertion the old
        # code failed: its peaks were 3,3,3..., never the cadence.
        # No tolerance here on purpose. A healthy cycle re-arms one cadence (+skew) out, so every
        # peak is >= the cadence. The old code's peaks shrank 11 -> 9 -> 7 ... toward its 2.5s
        # clamp; a ">= cadence - 2" slack let the first step of that decay through, which is how a
        # decay test passes on decaying code.
        settled = peak_vals[1:] if len(peak_vals) > 1 else peak_vals
        short = [v for v in settled if v < lo]
        if short:
            print("\nFAIL: %d cycle(s) re-armed at less than one cadence -- decay bug" % len(short))
            print("      peaks: %s (expected every peak ~%d)" % (settled, POLL))
            for p in peaks[1:]:
                if p[0][2] < lo:
                    print("      cycle from t=%ss re-armed at %ss" % (p[0][0], p[0][2]))
            return 1

        print("\nPASS: %d cycles over %.1fs, every one re-armed at ~%ss (never sub-cadence)"
              % (len(peaks), WATCH_S, POLL))
        return 0
    finally:
        if browser:
            browser.terminate()
        app.terminate()
        stub.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
