#!/usr/bin/env python3
"""Smoke test: boot the app on an ephemeral port and exercise the HTTP surface.

Every provider base is pointed at a local stub that answers 401 (echoing the credential, the way
providers do) and replays two recorded success shapes, so this script talks to no provider at all,
each account's state is deterministic, and both a failing card and a healthy one are exercised
through the server. What is asserted is the behaviour that must hold regardless of provider state:
the process boots, the UI and static assets are served, every configured account is really polled
*through the server's registry* (all ten used to be error cards either way, so the seam that makes
six providers visible was untestable), the JSON endpoints keep their shape and their documented key
contracts, no served body carries credential material, the static route refuses to escape its
directory, a bad config is refused with one clean line, and the configurable artwork degrades to
the bundled image when it cannot be had.

Runs in about half a minute and needs nothing but the standard library.
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
CFG = os.path.join(ROOT, "accounts.example.json")
PAGE = os.path.join(ROOT, "static", "index.html")
BUNDLED = os.path.join(ROOT, "static", "background.webp")

# Every base an adapter can be pointed at. Naming only the providers this file happens to
# know about is how a suite silently starts talking to a real vendor the day a seventh
# adapter is added; main() asserts this list plus the registry accounts for every adapter.
PROVIDER_BASE_ENV = (
    "COMMANDCODE_API_BASE",
    "CHEAPERINFERENCE_API_BASE",
    "OPENROUTER_API_BASE",
    "DEEPSEEK_API_BASE",
    "MOONSHOT_API_BASE",
    "Z_AI_API_BASE",
    "SYNTHETIC_API_BASE",
)

# The states a registered adapter may report when the provider answers 401. `unsupported`
# means no adapter was found for the provider — the exact failure a registry rename or a
# broken lazy import produces, and which every account being in *some* error state used to
# hide. `internal_error` means the adapter raised, and `ok` means it read a 401 as data.
ADAPTER_FAILURE_STATES = ("auth_error", "no_access", "provider_error", "shape_unknown")

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


class StubHandler(BaseHTTPRequestHandler):
    """Answers the routes it was given and 401 to everything else (the provider base).

    The 401 default is what makes this file hermetic: a base pointed here never reaches a
    vendor, and every adapter is exercised against a real HTTP round trip and a real
    rejection status. The body echoes the Authorization header back, which is what providers
    actually do — so the redaction is proven end to end (adapter error text -> `_scrub_result`
    -> served body) rather than by a key-name lint.
    """

    routes = {}

    def do_GET(self):
        path = self.path.split("?")[0]
        route = self.routes.get(path)
        if callable(route):              # a route that needs the request (to echo a credential)
            status, ctype, body = route(self)
        elif route:
            status, ctype, body = route
        else:
            status, ctype = 401, "application/json"
            body = json.dumps(
                {"error": "invalid api key: %s" % (self.headers.get("Authorization") or "")}
            ).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # keep the test output readable
        pass


def serve(routes):
    handler = type("Stub", (StubHandler,), {"routes": routes})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def stub_env(port):
    """Point every adapter base at the stub, so no request leaves this machine."""
    env = {var: "http://127.0.0.1:%d" % port for var in PROVIDER_BASE_ENV}
    env["OPENCODE_GO_USAGE_URL"] = "http://127.0.0.1:%d/usage" % port
    return env


def boot(config_path, env_extra=None, scratch=None):
    """Start the app on a free port and wait for its socket.

    Returns (proc, base, env, booted). `booted` is reported rather than assumed: the old
    version returned after a fixed wait whether or not anything had answered, and the first
    line of every run was `check("server boots", True)` — a claim that could not be false.
    """
    port = free_port()
    env = dict(os.environ)
    env.update(
        {
            "PORT": str(port),
            "QUOTA_CONFIG": config_path,
            # Scratch dir per run (under the run's own temp root), so one test never
            # inherits another run's downloaded artwork and nothing is left behind.
            "QUOTA_BACKGROUND_DIR": os.path.join(scratch or tempfile.gettempdir(),
                                                 "bg-%d" % port),
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
    booted = False
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2):
                booted = True
                break
        except urllib.error.HTTPError:
            booted = True  # 503 while the first poll is pending is a valid answer
            break
        except Exception:
            time.sleep(0.3)
    return proc, base, env, booted


def stop(proc, env):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def refuses(name, config_path, expect, env_extra=None, expect_code=2, args=()):
    """Run the app against a config it should refuse (or warn about) and read its one line.

    A config mistake is the most likely thing a user does with this repo, and every one of
    them is supposed to produce a specific, loud refusal — none of which had a test.
    """
    env = dict(os.environ)
    env.update({"PORT": str(free_port()), "QUOTA_CONFIG": config_path, "PYTHONUNBUFFERED": "1"})
    env.pop("QUOTA_POLL_SECONDS", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "app.py")] + list(args),
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, timeout=30)
    out = proc.stdout or ""
    check("config: %s" % name,
          proc.returncode == expect_code and expect in out and "Traceback" not in out,
          "exit=%s (want %s) expect=%r tail=%r"
          % (proc.returncode, expect_code, expect, out.strip().splitlines()[-1:]))


def ui_checks():
    """The page carries no chart and no history route any more: that layer was removed.

    The artwork route check stays here because it is the one CSS detail this script can
    verify without a browser.
    """
    page = open(PAGE, encoding="utf-8").read()
    check("the UI loads the artwork through /background",
          'url("/background")' in page, "CSS does not point at the configurable route")
    check("the page references no chart library", "uplot" not in page.lower())
    check("the page never asks for history", "/api/history" not in page)
    check("the vendored chart library is gone",
          not os.path.exists(os.path.join(ROOT, "static", "vendor", "uplot")))


def main():
    with open(CFG, encoding="utf-8") as fh:
        base_cfg = json.load(fh)
    expected_ids = [a["id"] for a in base_cfg["accounts"]]

    scratch = tempfile.mkdtemp(prefix="quota-panel-smoke-")
    artwork = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2      # 520 bytes
    # Two recorded success shapes, so the suite sees a healthy card as well as failing ones:
    # every account in an error state meant the whole ok path (windows, balance money,
    # `<id>_<window>` widget keys, `state: ok`) was never exercised through the server.
    # The z.ai body is the committed fixture (one source of truth for the vendor shape);
    # CheaperInference has no fixture file, so its two-line documented shape is inline.
    with open(os.path.join(ROOT, "tests", "fixtures", "providers", "zai_quota_limit.json"),
              encoding="utf-8") as fh:
        zai_ok = fh.read().encode()
    cheaper_ok = json.dumps({
        "object": "account.balance", "balance_usd": 40.0, "available_usd": 37.5,
        "reserved_usd": 2.5, "currency": "USD", "auto_recharge_enabled": False,
    }).encode()

    def echo_credential(request):
        """A 200 whose body quotes the credential back, the way a real error page does.

        This is the path that matters: a 401 gets fixed prose from every adapter (the body is
        never interpolated), but an unrecognised *200* body is dumped into the served error
        text — `json.dumps(body)[:200]` — so this is where a key would ride out. Only
        `_scrub_result` stops it, which makes this the end-to-end test of that redaction
        rather than a check on a field name.
        """
        return (200, "application/json", json.dumps({
            "error": "invalid api key %s" % (request.headers.get("Authorization") or ""),
            "detail": "this body carries no balance_infos array",
        }).encode())

    routes = {
        "/api/monitor/usage/quota/limit": (200, "application/json", zai_ok),
        "/v1/account/balance": (200, "application/json", cheaper_ok),
        "/user/balance": echo_credential,
        "/bg.png": (200, "image/png", artwork),
        # 9 MB: over the panel's own cap whatever it is, so the refusal path is exercised
        # rather than the shrink (tests/background.py covers the shrink itself).
        "/huge.png": (200, "image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * (9 * 1024 * 1024)),
        "/tiny.png": (200, "image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40),
        # A body an image host serves for its own error page, with the type to match and no
        # extension the panel can fall back on.
        "/bg.bin": (200, "text/html", b"<html>not an image</html>" + b" " * 200),
    }
    # The two accounts whose providers answer the stub with a real payload, and therefore
    # the only ones expected to be green.
    OK_IDS = {"zai-main", "ci-main"}
    srv, stub_port = serve(routes)
    stub = stub_env(stub_port)

    proc, base, env, booted = boot(CFG, stub, scratch)
    # A credential must not appear in any served body. The original check asserted a key
    # NAME (`not any("token" in a ...)`), so a credential echoed under any other name passed
    # — it could not detect the leak it was named after. These are the values the config
    # actually carries, plus the shape of a key, tested against every body served.
    secrets = [a.get("token") for a in base_cfg["accounts"] if a.get("token")]
    # ...plus the shape of a real key. The 20+ trailing run keeps account ids and widget
    # keys ("cc-work_error", "ci-main_five_hour") out of the net: they are not secrets.
    shape = re.compile(r"\b(?:sk|user|cmd|ci_live)[-_][A-Za-z0-9._-]{20,}")

    def leak(*bodies):
        text = " ".join(
            b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b) for b in bodies
        )
        for secret in secrets:
            if not isinstance(secret, str) or len(secret) < 6:
                continue
            for view in (secret, secret[:8], secret[-8:]):
                if view in text:
                    return "value %s… appears in a served body" % view[:8]
        found = shape.search(text)
        return "key-shaped substring %s…" % found.group(0)[:16] if found else None

    try:
        check("server boots", booted, "nothing answered %s/api/health within 15s" % base)

        status, ctype, body = get(base + "/")
        check("GET / is HTML", status == 200 and "text/html" in ctype, "%s %s" % (status, ctype))
        check("GET / renders the panel", b"Quota Panel" in body)
        check("GET / references the favicon", b"favicon" in body)
        check("header status line keeps the clock icon and the cadence in one string",
              b"updated" in body and b"auto-refresh ' + cadence" in body and b"checked every" in body
              and b'<span class="ic"' in body
              and b"\xc2\xb7 live \xc2\xb7 data" not in body)
        check("the two header marks are inline icons, not emoji",
              "\u23f0".encode("utf-8") not in body and "\U0001f504".encode("utf-8") not in body)

        status, ctype, body = get(base + "/static/favicon.svg")
        check("GET /static/favicon.svg", status == 200 and "image/svg" in ctype, "%s %s" % (status, ctype))

        status, ctype, quota_body = get(base + "/api/quota")
        payload = json.loads(quota_body)
        check("GET /api/quota shape",
              status == 200 and "accounts" in payload and "poll_seconds" in payload)
        accounts = payload["accounts"]
        check("GET /api/quota account count",
              len(accounts) == len(expected_ids), "%d accounts" % len(accounts))
        # The count alone was satisfied by ten cards in *any* error state, including the
        # `unsupported` the server serves when the balance registry never loaded. The state
        # is what tells a working panel from a six-provider outage.
        check("GET /api/quota account ids", [a["id"] for a in accounts] == expected_ids,
              str([a["id"] for a in accounts]))
        states = sorted({a["state"] for a in accounts})
        check("every account was polled by a registered adapter",
              all(a["state"] in ADAPTER_FAILURE_STATES + ("ok",) for a in accounts),
              "states=%s" % states)
        ok_ids = {a["id"] for a in accounts if a["state"] == "ok"}
        check("exactly the two stubbed providers render a healthy card",
              ok_ids == OK_IDS, "ok=%s expected=%s" % (sorted(ok_ids), sorted(OK_IDS)))
        check("a healthy window card carries a window with a percent",
              all(a["windows"] and any(w.get("percent") is not None or w.get("amount") is not None
                                       for w in a["windows"])
                  for a in accounts if a["state"] == "ok"),
              str([(a["id"], a["windows"]) for a in accounts if a["state"] == "ok"]))
        check("the provider's own HTTP status reaches the payload",
              any(a.get("http_status") == 401 for a in accounts),
              str([(a["id"], a.get("http_status")) for a in accounts]))
        check("GET /api/quota has no credential leak", not leak(quota_body), leak(quota_body) or "")

        # The registry is a published contract (README, accounts.example.json): the card
        # kind and how well each adapter was verified are served, and nothing tested it.
        status, _, providers_body = get(base + "/api/providers")
        providers = json.loads(providers_body)
        registered = providers.get("providers") or []
        ids = sorted(p["id"] for p in registered)
        check("GET /api/providers lists every provider",
              status == 200 and len(registered) == len(ids) and "logo_fallback" in providers,
              "HTTP %s, %d entries" % (status, len(registered)))
        check("every provider the example config uses is registered",
              {a["provider"] for a in base_cfg["accounts"]} <= set(ids),
              "configured=%s served=%s"
              % (sorted({a["provider"] for a in base_cfg["accounts"]}), ids))
        check("every served logo path points at a file on disk",
              all(os.path.exists(os.path.join(ROOT, p["logo"].lstrip("/"))) for p in registered),
              str([p["logo"] for p in registered]))
        check("every provider declares its kind and its contract",
              all(p["kind"] in ("window", "balance") and p["contract"] in ("live", "documented", "third-party")
                  for p in registered),
              str([(p["id"], p.get("kind"), p.get("contract")) for p in registered]))

        # `third-party` exists because z.ai publishes no contract for its route: the registry
        # must not call that "documented", and the claim a reader cannot re-run must be said
        # out loud (the live providers carry that caveat).
        third = [p["id"] for p in registered if p["contract"] == "third-party"]
        check("z.ai is registered as third-party, not documented", third == ["zai"], str(third))
        note_less = [p["id"] for p in registered if p["contract"] == "live" and not p["contract_note"]]
        check("every `live` claim says what backs it", not note_less, str(note_less))
        check("`third-party` names its sources",
              all(p.get("contract_note") for p in registered if p["contract"] == "third-party"),
              str([(p["id"], p.get("contract_note")) for p in registered if p["contract"] == "third-party"]))

        status, _, home_body = get(base + "/api/homepage")
        home = json.loads(home_body)
        check("GET /api/homepage shape",
              status == 200 and "items" in home and "widgets" in home)
        # items keys are an external contract (`items["<id>_<window>"]`, consumed by a
        # gethomepage customapi tile). The old check only asserted the container keys, so
        # collapsing every key to the account id would have shipped every tile blank.
        expected_keys = set()
        per_account = []
        for acc in accounts:
            if acc["state"] != "ok":
                expected_keys.add("%s_error" % acc["id"])
                per_account.append((acc["id"], ["error"]))
                continue
            keys = []
            for win in acc["windows"]:
                expected_keys.add("%s_%s" % (acc["id"], win["key"]))
                keys.append(win["key"])
            per_account.append((acc["id"], keys))
        check("GET /api/homepage items keys are <id>_<window> for every account",
              set(home["items"]) == expected_keys,
              "missing=%s unexpected=%s"
              % (sorted(expected_keys - set(home["items"])), sorted(set(home["items"]) - expected_keys)))
        check("GET /api/homepage widgets cover every account",
              len(home["widgets"]) == sum(len(k) for _, k in per_account),
              "%d widgets for %s" % (len(home["widgets"]), per_account))

        # Every other body the server can hand out, checked for credential material.
        _, _, health_body = get(base + "/api/health")
        _, _, page_body = get(base + "/")
        check("no served body carries a credential value or a key shape",
              not leak(quota_body, home_body, providers_body, health_body, page_body),
              leak(quota_body, home_body, providers_body, health_body, page_body) or "")

        # The history layer is gone, route included: it must 404, not answer an empty set.
        status, _, _ = get(base + "/api/history")
        check("GET /api/history is gone", status == 404, "HTTP %s" % status)
        status, _, _ = get(base + "/api/history?hours=24&max_points=400")
        check("GET /api/history is gone with a query too", status == 404, "HTTP %s" % status)

        status, _, _ = get(base + "/static/vendor/uplot/uPlot.iife.min.js")
        check("the vendored chart library is no longer served", status == 404, "HTTP %s" % status)

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

        # Freshness is the whole point of /api/health, and Docker restarts the container on
        # a 503 (Dockerfile HEALTHCHECK). The old check accepted 200 OR 503, so a
        # permanently-stale panel — restarted forever — passed the suite.
        status, _, raw = get(base + "/api/health")
        health = json.loads(raw)
        check("GET /api/health answers 200 on a panel that just polled",
              status == 200, "HTTP %s %s" % (status, health.get("status")))
        check("health says ok, counts the accounts and reports an age",
              health.get("status") == "ok" and health.get("accounts") == len(expected_ids)
              and isinstance(health.get("last_poll_age_s"), (int, float)),
              str({k: health.get(k) for k in ("status", "accounts", "last_poll_age_s")}))
        check("health counts exactly the healthy accounts",
              health.get("ok_accounts") == len(OK_IDS), str(health.get("ok_accounts")))

        # ---- artwork: bundled by default ----
        with open(BUNDLED, "rb") as fh:
            bundled = fh.read()
        status, ctype, body = get(base + "/background")
        check("GET /background serves the bundled artwork by default",
              status == 200 and ctype == "image/webp" and body == bundled,
              "HTTP %s %s, %d bytes" % (status, ctype, len(body)))
        bg = json.loads(get(base + "/api/health")[2]).get("background", {})
        check("health reports which artwork is live",
              bg.get("configured") is False and bg.get("served") == "bundled", str(bg))

        # ---- artwork: configured URL, fetched once at startup, served from tmpfs ----
        secret = "signature-that-must-not-leak"
        good = write_config(
            os.path.join(scratch, "artwork.json"),
            dict(base_cfg, background_url="http://127.0.0.1:%d/bg.png?%s" % (stub_port, secret)),
        )
        art_env = dict(stub)
        proc2, base2, env2, booted2 = boot(good, art_env, scratch)
        try:
            deadline = time.time() + 15
            status, ctype, body = 0, "", b""
            while time.time() < deadline:
                status, ctype, body = get(base2 + "/background")
                if status == 200 and body != bundled:
                    break
                time.sleep(0.3)
            # With Pillow in the image the panel re-encodes the download as WebP, so the
            # bytes served are the panel's, not the source file's. Both are correct.
            check("a configured background_url is fetched and served",
                  status == 200 and ctype in ("image/png", "image/webp") and body != bundled,
                  "HTTP %s %s, %d bytes" % (status, ctype, len(body)))
            status, _, raw = get(base2 + "/api/health")
            bg = json.loads(raw).get("background", {})
            check("health reports the fetched artwork",
                  bg.get("configured") is True and bg.get("served") == "remote"
                  and bg.get("bytes") == len(body), str(bg))
            check("the artwork URL is never echoed by the API", secret.encode() not in raw,
                  "signature found in /api/health")
            check("the configured artwork is served as its own type, not the bundled one",
                  body != bundled, "served the bundled image instead")
        finally:
            stop(proc2, env2)

        # ---- artwork: an unreachable URL must cost the image, not the panel ----
        dead = write_config(
            os.path.join(scratch, "artwork-dead.json"),
            dict(base_cfg, background_url="http://127.0.0.1:%d/none.png" % free_port()),
        )
        proc3, base3, env3, _ = boot(dead, stub, scratch)
        try:
            status, ctype, body = get(base3 + "/background")
            check("an unreachable background_url falls back to the bundled image",
                  status == 200 and body == bundled,
                  "HTTP %s %s, %d bytes" % (status, ctype, len(body)))
            raw = get(base3 + "/api/health")[2]
            bg = json.loads(raw).get("background", {})
            check("health names the artwork failure",
                  bg.get("configured") is True and bg.get("served") == "bundled" and bg.get("error"),
                  str(bg))
            status, _, _ = get(base3 + "/api/quota")
            check("the panel still serves quotas with a broken artwork URL",
                  status == 200, "HTTP %s" % status)
        finally:
            stop(proc3, env3)

        # ---- artwork: the refusal paths the README advertises ----
        # "DNS, TLS, 404, wrong type, too large — is logged and the bundled image is served
        # instead" was documented and never exercised: each of these is a
        # serve-something-unservable path (a 9 MB wallpaper, a 30-byte error page saved as
        # .png, an HTML error body behind an image Content-Type).
        for label, route, reason in (
            ("an oversized image is refused", "/huge.png", "larger than"),
            ("a body too small to be an image is refused", "/tiny.png", "too small"),
            ("an unsupported content type is refused", "/bg.bin", "unsupported image type"),
        ):
            cfg = write_config(
                os.path.join(scratch, "artwork-%s.json" % route.strip("/").replace(".", "-")),
                dict(base_cfg, background_url="http://127.0.0.1:%d%s" % (stub_port, route)),
            )
            proc4, base4, env4, _ = boot(cfg, stub, scratch)
            try:
                deadline = time.time() + 15
                bg = {}
                while time.time() < deadline:
                    bg = json.loads(get(base4 + "/api/health")[2]).get("background", {})
                    if bg.get("error"):
                        break
                    time.sleep(0.3)
                check(label, reason in (bg.get("error") or ""), str(bg))
                status, ctype, body = get(base4 + "/background")
                check("  -> the bundled image is served instead",
                      status == 200 and body == bundled and ctype == "image/webp",
                      "HTTP %s %s %d bytes" % (status, ctype, len(body)))
            finally:
                stop(proc4, env4)

        # ---- poll_seconds: the file decides, the env var is the fallback ----
        # README: "bounded to 10-3600 (out-of-range values are logged and ignored),
        # overridable per container with QUOTA_POLL_SECONDS". Nothing read either.
        cfg45 = write_config(os.path.join(scratch, "poll45.json"), dict(base_cfg, poll_seconds=45))
        proc5, base5, env5, _ = boot(cfg45, dict(stub, QUOTA_POLL_SECONDS="23"), scratch)
        try:
            served = json.loads(get(base5 + "/api/quota")[2])["poll_seconds"]
            check("a declared poll_seconds is served (and beats the env default)",
                  served == 45, "poll_seconds=%s" % served)
        finally:
            stop(proc5, env5)

        cfg5 = write_config(os.path.join(scratch, "poll5.json"), dict(base_cfg, poll_seconds=5))
        proc6, base6, env6, _ = boot(cfg5, dict(stub, QUOTA_POLL_SECONDS="23"), scratch)
        try:
            served = json.loads(get(base6 + "/api/quota")[2])["poll_seconds"]
            check("an out-of-range poll_seconds is ignored, not honoured",
                  served == 23, "poll_seconds=%s" % served)
            check("the panel still boots with a bad poll_seconds",
                  get(base6 + "/")[0] == 200)
        finally:
            stop(proc6, env6)

        # ---- background_url: the config file wins over the env var ----
        # Both set, and the file's URL is unreachable while the env one is good: if the env
        # var won, the artwork would be served and the workspace's own setting ignored.
        both = write_config(
            os.path.join(scratch, "artwork-both.json"),
            dict(base_cfg, background_url="http://127.0.0.1:%d/none.png" % free_port()),
        )
        proc7, base7, env7, _ = boot(
            both, dict(stub, QUOTA_BACKGROUND_URL="http://127.0.0.1:%d/bg.png" % stub_port), scratch
        )
        try:
            deadline = time.time() + 15
            bg = {}
            while time.time() < deadline:
                bg = json.loads(get(base7 + "/api/health")[2]).get("background", {})
                if bg.get("error"):
                    break
                time.sleep(0.3)
            check("the config file's background_url wins over the env var",
                  bg.get("configured") is True and bg.get("served") == "bundled" and bg.get("error"),
                  str(bg))
        finally:
            stop(proc7, env7)

        # ---- config refusals: one clean line, exit 2, no traceback ----
        one = {"id": "a", "provider": "zai", "label": "A", "token": "t"}
        refuses("duplicate account ids are fatal",
                write_config(os.path.join(scratch, "dup.json"),
                             dict(base_cfg, accounts=[one, dict(one)])),
                "account ids must be unique")
        refuses("an unknown provider is refused",
                write_config(os.path.join(scratch, "prov.json"),
                             dict(base_cfg, accounts=[dict(one, provider="nope")])),
                "must be one of")
        refuses("an empty accounts list is refused",
                write_config(os.path.join(scratch, "empty.json"), dict(base_cfg, accounts=[])),
                "non-empty 'accounts' list")
        bad_json = os.path.join(scratch, "notjson.json")
        with open(bad_json, "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        refuses("invalid JSON is refused", bad_json, "not valid JSON")
        refuses("a missing config file is refused",
                os.path.join(scratch, "absent.json"), "config file not found")
        refuses("a config path that is a directory is refused",
                scratch, "config path is a directory")
        refuses("an unreadable token_file is refused",
                write_config(os.path.join(scratch, "tokfile.json"),
                             dict(base_cfg, accounts=[dict(one, token="",
                                                           token_file="/nonexistent/token.txt")])),
                "token_file unreadable")
        # Not fatal, and the one failure mode that silently produced an account with no
        # credential: the key pasted into `token_env` instead of `token`.
        refuses("a key pasted into token_env is reported, not fatal",
                write_config(os.path.join(scratch, "tokenv.json"),
                             dict(base_cfg, accounts=[dict(one, token="",
                                                           token_env="user_abcdefghij0123456789")])),
                "holds what looks like a KEY", env_extra=stub, expect_code=1, args=("--check",))
    finally:
        stop(proc, env)
        srv.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)

    ui_checks()

    print()
    if failures:
        print("%d check(s) FAILED: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
