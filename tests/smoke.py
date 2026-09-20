#!/usr/bin/env python3
"""Smoke test: boot the app on an ephemeral port and exercise the HTTP surface.

No network access to any provider is required — the config below carries
deliberately invalid credentials, so every account is expected to come back as
an error card. What is asserted is the behaviour that must hold regardless of
provider state: the process boots, the UI and static assets are served, the JSON
endpoints keep their shape, the static route refuses to escape its directory, and
the configurable artwork degrades to the bundled image when it cannot be had.

Runs in a few seconds and needs nothing but the standard library.
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "accounts.example.json")
PAGE = os.path.join(ROOT, "static", "index.html")
BUNDLED = os.path.join(ROOT, "static", "background.webp")

failures = []


def check(name, ok, detail=""):
    # `detail` explains a failure; printing it on PASS reads like one.
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("" if ok else " — " + detail)))
    if not ok:
        failures.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def write_config(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


def boot(config_path, env_extra=None):
    """Start the app on a free port and wait for its socket. Returns (proc, base, env)."""
    port = free_port()
    env = dict(os.environ)
    env.update(
        {
            "PORT": str(port),
            "QUOTA_CONFIG": config_path,
            "QUOTA_DB": os.path.join("/tmp", "quota-panel-smoke-%d.db" % port),
            # /tmp is a tmpfs in the container; here a scratch dir per run, so one test
            # never inherits another run's downloaded artwork.
            "QUOTA_BACKGROUND_DIR": os.path.join("/tmp", "quota-panel-smoke-bg-%d" % port),
            "QUOTA_HTTP_TIMEOUT": "2",
            "QUOTA_BACKGROUND_TIMEOUT": "3",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py")],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = "http://127.0.0.1:%d" % port
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2):
                break
        except urllib.error.HTTPError:
            break  # 503 while the first poll is pending is a valid answer
        except Exception:
            time.sleep(0.3)
    return proc, base, env


def stop(proc, env):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    try:
        os.unlink(env["QUOTA_DB"])
    except OSError:
        pass


class ImageHandler(BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, fmt, *args):  # keep the test output readable
        pass


def serve_image(body):
    handler = type("ImageHandler", (ImageHandler,), {"body": body})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def chart_policy_checks():
    """The chart asks for readable bars: one bucket per BAR_MIN_PX, on the ladder.

    Mirrors static/index.html: the constant is read from the page, so the UI and this
    check cannot drift apart. A range that renders hairlines is the regression this
    catches (the page used to ask for ~1.4 px per sample).
    """
    sys.path.insert(0, ROOT)
    from app import pick_bucket

    page = open(PAGE, encoding="utf-8").read()
    match = re.search(r"BAR_MIN_PX\s*=\s*(\d+)", page)
    if not match:
        check("the chart declares its bar-width target", False, "BAR_MIN_PX not found in index.html")
        return
    bar_min_px = int(match.group(1))

    for width in (1400, 900, 420):
        max_points = min(600, max(20, width // bar_min_px))
        worst = None
        for hours in (1, 6, 24, 168, 720, 2160, 8760):
            bucket = pick_bucket(hours * 3600, max_points)
            bars = hours * 3600 // bucket
            px = width / bars
            if bars > max_points + 2 or px < 12:
                worst = "%dh -> %ds bucket, %d bars, %.1f px" % (hours, bucket, bars, px)
        check("chart bars stay readable at %dpx" % width, worst is None, worst or "")

    check("the UI loads the artwork through /background",
          'url("/background")' in page, "CSS does not point at the configurable route")
    check("the chart formats timestamps with the locale",
          "hour12" in page and "toLocaleTimeString" in page, "no locale-aware axis formatter")


def main():
    with open(CFG, encoding="utf-8") as fh:
        base_cfg = json.load(fh)

    proc, base, env = boot(CFG)
    try:
        check("server boots", True)

        status, ctype, body = get(base + "/")
        check("GET / is HTML", status == 200 and "text/html" in ctype, "%s %s" % (status, ctype))
        check("GET / renders the panel", b"Quota Panel" in body)
        check("GET / references the favicon", b"favicon" in body)
        check("header status line keeps the clock and the cadence in one string",
              b"\xe2\x8f\xb0 updated" in body and b"auto-refresh every" in body
              and b"\xc2\xb7 live \xc2\xb7 data" not in body)

        status, ctype, body = get(base + "/static/favicon.svg")
        check("GET /static/favicon.svg", status == 200 and "image/svg" in ctype, "%s %s" % (status, ctype))

        status, ctype, body = get(base + "/api/quota")
        payload = json.loads(body)
        check("GET /api/quota shape",
              status == 200 and "accounts" in payload and "poll_seconds" in payload)
        check("GET /api/quota account count",
              len(payload["accounts"]) == len(base_cfg["accounts"]),
              "%d accounts" % len(payload["accounts"]))
        check("GET /api/quota has no credential leak",
              not any("token" in a for a in payload["accounts"]))

        status, ctype, body = get(base + "/api/homepage")
        payload = json.loads(body)
        check("GET /api/homepage shape",
              status == 200 and "items" in payload and "widgets" in payload)

        status, ctype, body = get(base + "/api/history")
        payload = json.loads(body)
        check("GET /api/history shape",
              status == 200 and all(k in payload for k in ("t", "series", "bucket_seconds",
                                                           "retention", "sources")),
              "%s %s" % (status, ctype))
        check("GET /api/history is safe on a fresh database",
              payload["series"] == [] and isinstance(payload["t"], list)
              and payload["retention"]["raw_days"] == 90,
              "%d series, retention %s" % (len(payload["series"]), payload["retention"]))

        status, _, _ = get(base + "/api/history?since=2026-01-02T00:00:00Z&until=2026-01-01T00:00:00Z")
        check("GET /api/history refuses an inverted range", status == 400, "HTTP %s" % status)

        status, ctype, body = get(base + "/static/vendor/uplot/uPlot.iife.min.js")
        check("GET the vendored chart library",
              status == 200 and len(body) > 20000, "HTTP %s, %d bytes" % (status, len(body)))

        status, ctype, body = get(base + "/")
        check("GET / loads the chart assets",
              b"/static/vendor/uplot/uPlot.iife.min.js" in body and b"/api/history" in body)

        # Path traversal and unexpected types must never be served.
        for path in ("/static/../app.py", "/static/../../etc/passwd", "/static/accounts.json"):
            status, _, _ = get(base + path)
            check("traversal refused: %s" % path, status in (403, 404), "HTTP %s" % status)

        status, ctype, _ = get(base + "/static/favicon.ico")
        check("GET /static/favicon.ico", status == 200 and "image/" in ctype, "%s %s" % (status, ctype))

        status, _, _ = get(base + "/static/missing.png")
        check("static missing file is 404", status == 404, "HTTP %s" % status)

        status, _, _ = get(base + "/static/app.py")
        check("static disallowed type is 403", status == 403, "HTTP %s" % status)

        status, _, body = get(base + "/api/health")
        check("GET /api/health answers", status in (200, 503), "HTTP %s" % status)

        # ---- artwork: bundled by default ----
        with open(BUNDLED, "rb") as fh:
            bundled = fh.read()
        status, ctype, body = get(base + "/background")
        check("GET /background serves the bundled artwork by default",
              status == 200 and ctype == "image/webp" and body == bundled,
              "HTTP %s %s, %d bytes" % (status, ctype, len(body)))
        status, _, raw = get(base + "/api/health")
        bg = json.loads(raw).get("background", {})
        check("health reports which artwork is live",
              bg.get("configured") is False and bg.get("served") == "bundled", str(bg))

        # ---- artwork: configured URL, fetched once at startup, served from tmpfs ----
        artwork = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2      # 520 bytes
        srv, img_port = serve_image(artwork)
        secret = "signature-that-must-not-leak"
        good = write_config(
            os.path.join("/tmp", "quota-panel-smoke-artwork.json"),
            dict(base_cfg, background_url="http://127.0.0.1:%d/bg.png?%s" % (img_port, secret)),
        )
        proc2, base2, env2 = boot(good)
        try:
            deadline = time.time() + 15
            status, ctype, body = 0, "", b""
            while time.time() < deadline:
                status, ctype, body = get(base2 + "/background")
                if body == artwork:
                    break
                time.sleep(0.3)
            check("a configured background_url is fetched and served",
                  status == 200 and ctype == "image/png" and body == artwork,
                  "HTTP %s %s, %d bytes" % (status, ctype, len(body)))
            status, _, raw = get(base2 + "/api/health")
            bg = json.loads(raw).get("background", {})
            check("health reports the fetched artwork",
                  bg.get("configured") is True and bg.get("served") == "remote"
                  and bg.get("bytes") == len(artwork), str(bg))
            check("the artwork URL is never echoed by the API", secret.encode() not in raw,
                  "signature found in /api/health")
            check("the configured artwork is served as its own type, not the bundled one",
                  body != bundled, "served the bundled image instead")
        finally:
            stop(proc2, env2)
            srv.shutdown()

        # ---- artwork: an unreachable URL must cost the image, not the panel ----
        dead = write_config(
            os.path.join("/tmp", "quota-panel-smoke-artwork-dead.json"),
            dict(base_cfg, background_url="http://127.0.0.1:%d/none.png" % free_port()),
        )
        proc3, base3, env3 = boot(dead)
        try:
            status, ctype, body = get(base3 + "/background")
            check("an unreachable background_url falls back to the bundled image",
                  status == 200 and body == bundled,
                  "HTTP %s %s, %d bytes" % (status, ctype, len(body)))
            status, _, raw = get(base3 + "/api/health")
            bg = json.loads(raw).get("background", {})
            check("health names the artwork failure",
                  bg.get("configured") is True and bg.get("served") == "bundled" and bg.get("error"),
                  str(bg))
            status, _, _ = get(base3 + "/api/quota")
            check("the panel still serves quotas with a broken artwork URL",
                  status == 200, "HTTP %s" % status)
        finally:
            stop(proc3, env3)
    finally:
        stop(proc, env)

    chart_policy_checks()

    print()
    if failures:
        print("%d check(s) FAILED: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
