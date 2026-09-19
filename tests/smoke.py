#!/usr/bin/env python3
"""Smoke test: boot the app on an ephemeral port and exercise the HTTP surface.

No network access to any provider is required — the config below carries
deliberately invalid credentials, so every account is expected to come back as
an error card. What is asserted is the behaviour that must hold regardless of
provider state: the process boots, the UI and static assets are served, the JSON
endpoints keep their shape, and the static route refuses to escape its directory.

Runs in a couple of seconds and needs nothing but the standard library.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "accounts.example.json")

failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    if not ok:
        failures.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def get(url, expect=(200,)):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def main():
    port = free_port()
    env = dict(os.environ)
    env.update(
        {
            "PORT": str(port),
            "QUOTA_CONFIG": CFG,
            "QUOTA_DB": os.path.join("/tmp", "quota-panel-smoke-%d.db" % port),
            "QUOTA_HTTP_TIMEOUT": "2",
            "PYTHONUNBUFFERED": "1",
        }
    )
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py")],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = "http://127.0.0.1:%d" % port
    try:
        # Wait for the socket to answer, up to ~15 s.
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2):
                    break
            except urllib.error.HTTPError:
                break  # 503 while the first poll is pending is a valid answer
            except Exception:
                time.sleep(0.3)
        else:
            check("server boots", False, "no response on %s" % base)
            return

        check("server boots", True)

        status, ctype, body = get(base + "/")
        check("GET / is HTML", status == 200 and "text/html" in ctype, "%s %s" % (status, ctype))
        check("GET / renders the panel", b"Quota Panel" in body)
        check("GET / references the favicon", b"favicon" in body)

        status, ctype, body = get(base + "/static/favicon.svg")
        check("GET /static/favicon.svg", status == 200 and "image/svg" in ctype, "%s %s" % (status, ctype))

        status, ctype, body = get(base + "/api/quota")
        payload = json.loads(body)
        check("GET /api/quota shape",
              status == 200 and "accounts" in payload and "poll_seconds" in payload)
        check("GET /api/quota account count",
              len(payload["accounts"]) == len(json.load(open(CFG))["accounts"]),
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
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            os.unlink(env["QUOTA_DB"])
        except OSError:
            pass

    print()
    if failures:
        print("%d check(s) FAILED: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
