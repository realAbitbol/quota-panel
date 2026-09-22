#!/usr/bin/env python3
"""History tests: the optional layer's storage contract, and the contract that it is absent
until someone asks for it.

Two halves, both hermetic and deterministic:

  * storage — history.py is imported directly and driven on a fixed clock, so the
    assertions pin bucketing, exact rollup aggregates, the prune guard, the merged
    raw+rollup view and every refusal, instead of the providers' current state.
  * the optional contract — app.py is booted twice against a local stub. With the layer
    off (the default) `/api/history` must 404 and no database file may appear anywhere;
    with it on, samples land per account/window, a balance is not turned into a
    percentage, and the merged view reads back. `--check` must not create the database
    either: a probe that writes is not a probe.

Runs in about half a minute and needs nothing but the standard library.
"""
import importlib.util
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
WORK = tempfile.mkdtemp(prefix="quota-history-")
FAILURES = []


def check(name, ok, detail=""):
    # `detail` explains a failure; printing it on PASS reads like one.
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("" if ok else " — " + detail)))
    if not ok:
        FAILURES.append(name)


_spec = importlib.util.spec_from_file_location("quota_history", os.path.join(ROOT, "history.py"))
history = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(history)

DAY = 86400
NOW = 1800000123                      # fixed clock, deliberately off a bucket boundary so the
BUCKET = 900                          # frontier falls strictly before "now"


def config(**overrides):
    base = {"enabled": True, "path": os.path.join(WORK, "store.db"), "sample_seconds": 300,
            "raw_days": 90, "rollup_days": 365, "rollup_seconds": BUCKET}
    base.update(overrides)
    return base


def fresh(path):
    if os.path.exists(path):
        os.unlink(path)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.unlink(path + suffix)
    return path


def insert(path, rows):
    """Seed raw rows the way the writer does, `series` included.

    Bypassing the writer is what makes these assertions deterministic — but it must not
    bypass the schema: a fixture that leaves `series` empty would let a broken writer look
    correct.
    """
    conn = history.connect(path)
    try:
        conn.executemany(
            "INSERT INTO snapshots (ts, account_id, provider, window_key, percent, used, cap, resets_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(history.epoch_to_iso(ts), acc, "synthetic", win, pct, None, None, None)
             for ts, acc, win, pct in rows],
        )
        for ts, acc, win, _ in rows:
            conn.execute(
                "INSERT OR REPLACE INTO series (account_id, window_key, account_label, provider, "
                "window_label, first_ts, last_ts) VALUES (?,?,?,?,?,"
                "COALESCE((SELECT first_ts FROM series WHERE account_id = ? AND window_key = ?), ?), ?)",
                (acc, win, acc, "synthetic", win, acc, win, history.epoch_to_iso(ts),
                 history.epoch_to_iso(ts)),
            )
        conn.commit()
    finally:
        conn.close()


def query(path, sql, args=()):
    conn = history.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def scalar(path, sql, args=()):
    rows = query(path, sql, args)
    return rows[0][0] if rows else None


def storage_checks():
    print("--- config")
    defaults, notes = history.load_config(None, env={})
    check("history is off unless it is asked for",
          defaults["enabled"] is False and defaults["path"] == history.DEFAULT_PATH and not notes,
          "%s %s" % (defaults["enabled"], notes))

    env_only, notes = history.load_config(None, env={"QUOTA_HISTORY_ENABLED": "1",
                                                     "QUOTA_DB": os.path.join(WORK, "a.db")})
    check("the environment can turn it on",
          env_only["enabled"] is True and env_only["path"] == os.path.join(WORK, "a.db") and not notes,
          "%s %s" % (env_only["enabled"], notes))

    cfg = os.path.join(WORK, "cfg.json")
    with open(cfg, "w", encoding="utf-8") as fh:
        json.dump({"history": {"_note": "prose, not a setting", "enabled": True, "sample_seconds": 60,
                               "raw_days": 30, "rollup_days": 10, "rollup_seconds": 1800,
                               "nonsense": 3}}, fh)
    mixed, notes = history.load_config(cfg, env={"QUOTA_HISTORY_ENABLED": "0",
                                                 "QUOTA_HISTORY_SAMPLE_SECONDS": "900"})
    check("the config file wins over the environment",
          mixed["enabled"] is True and mixed["sample_seconds"] == 60, "%s" % mixed)
    check("rollup_days below raw_days is clamped up, and said so",
          mixed["rollup_days"] == 30 and any("clamped" in note for note in notes), "%s" % notes)
    check("an unknown history key is reported", any("nonsense" in note for note in notes), "%s" % notes)
    # The example config documents itself with `_note` blocks. Prose is not a setting, and a
    # warning on every start of a shipped example is a warning nobody reads.
    check("an underscore note is documentation, not an unknown key",
          not any("_note" in note for note in notes), "%s" % notes)

    _, notes = history.load_config(None, env={"QUOTA_HISTORY_ENABLED": "maybe",
                                              "QUOTA_HISTORY_SAMPLE_SECONDS": "0",
                                              "QUOTA_ROLLUP_SECONDS": "900x"})
    check("a value that cannot be read is refused, not silently used",
          len(notes) == 3 and all("ignored" in note for note in notes), "%s" % notes)
    _, notes = history.load_config(None, env={"QUOTA_HISTORY_ENABLED": "1", "QUOTA_ROLLUP_SECONDS": "905"})
    check("a rollup that is not a whole number of minutes falls back to the default", len(notes) == 1
          and "whole number of minutes" in notes[0], "%s" % notes)

    print("--- the store refuses what it cannot keep")
    refused = history.load_config(None, env={"QUOTA_HISTORY_ENABLED": "1",
                                             "QUOTA_DB": "/mnt/nas/quota.db"})[0]
    check("a network mount is refused rather than trusted", "network mount" in (history.prepare(refused) or ""),
          history.prepare(refused))
    unwritable = history.load_config(None, env={"QUOTA_HISTORY_ENABLED": "1",
                                                "QUOTA_DB": "/dev/null/quota.db"})[0]
    check("a path that cannot be created reports why", bool(history.prepare(unwritable)))
    # Measured in production: a hardened compose with `read_only: true` and a single *file* bind
    # mounted at the database path. sqlite opens the file and then cannot put its journal beside
    # it, and its own message ("unable to open database file") blames the file. The store has to
    # name the real cause and the fix, because that message is the only clue the operator gets.
    locked_dir = os.path.join(WORK, "read-only-dir")
    os.makedirs(locked_dir, exist_ok=True)
    os.chmod(locked_dir, 0o500)
    try:
        boxed = history.load_config(None, env={"QUOTA_HISTORY_ENABLED": "1",
                                               "QUOTA_DB": os.path.join(locked_dir, "quota.db")})[0]
        reason = history.prepare(boxed) or ""
        check("a database whose directory cannot take a journal says so, and says what to mount",
              "write test in" in reason and "mount a directory" in reason, reason)
    finally:
        os.chmod(locked_dir, 0o700)

    print("--- sampling")
    path = fresh(os.path.join(WORK, "sampling.db"))
    cfg = config(path=path, sample_seconds=300)
    history._LAST_SAMPLE.clear()
    results = [{"id": "syn-main", "provider": "synthetic", "label": "Synthetic", "state": "ok",
                "windows": [
                    {"kind": "window", "key": "subscription", "label": "Window", "percent": 30.4,
                     "used": 41, "cap": 135, "resets_at": "2025-09-21T14:36:14Z"},
                    {"kind": "balance", "key": "credits", "label": "Credits", "amount": 12.4},
                ]}]
    first = history.record(results, cfg, now_ts=NOW)
    check("a first poll is stored", first["written"] == 1 and scalar(path, "SELECT COUNT(*) FROM snapshots") == 1,
          "%s" % first)
    check("a balance window is not turned into a percentage",
          scalar(path, "SELECT COUNT(*) FROM snapshots WHERE window_key = 'credits'") == 0,
          "%s" % scalar(path, "SELECT window_key FROM snapshots"))
    second = history.record(results, cfg, now_ts=NOW + 120)
    check("a sample inside sample_seconds is skipped", second["written"] == 0 and second["skipped"] == 1,
          "%s" % second)
    third = history.record(results, cfg, now_ts=NOW + 301)
    check("a sample past sample_seconds is written",
          third["written"] == 1 and scalar(path, "SELECT COUNT(*) FROM snapshots") == 2, "%s" % third)

    dead = [dict(results[0], state="provider_error", error="boom", windows=[])]
    fourth = history.record(dead, cfg, now_ts=NOW + 1000)
    check("a failing account is a gap, never a zero",
          fourth["written"] == 0 and scalar(path, "SELECT COUNT(*) FROM snapshots") == 2, "%s" % fourth)

    unreadable = [dict(results[0], windows=[{"kind": "window", "key": "subscription", "label": "Window",
                                             "percent": None, "note": "no reading"}])]
    fifth = history.record(unreadable, cfg, now_ts=NOW + 2000)
    check("an unreadable window is not stored as 0",
          fifth["written"] == 0 and scalar(path, "SELECT COUNT(*) FROM snapshots") == 2, "%s" % fifth)

    print("--- retention: rollups are the record")
    path = fresh(os.path.join(WORK, "retention.db"))
    cfg = config(path=path)
    old_bucket = history.bucket_of(NOW - 100 * DAY, BUCKET)
    recent_bucket = history.bucket_of(NOW - 3600, BUCKET)
    insert(path, [(recent_bucket + 60 * i, "cmd-perso", "monthly", 10 + i) for i in range(1, 9)])
    insert(path, [(old_bucket + 60 * i, "cmd-perso", "monthly", 10 * (i + 1)) for i in range(4)])
    insert(path, [(old_bucket + 60 * i, "og-perso", "weekly", 90 + 5 * i) for i in range(2)])
    insert(path, [(NOW - 400 * DAY + 60 * i, "cmd-perso", "monthly", 5.0) for i in range(4)])
    conn = history.connect(path)
    try:
        report = history.maintain(conn, cfg, now_ts=NOW)
    finally:
        conn.close()
    check("maintenance rolls up", report["rollup_rows"] > 0, "%s" % report)
    check("maintenance prunes raw", report["raw_deleted"] > 0, "%s" % report)
    check("maintenance prunes expired rollups", report["rollup_deleted"] > 0, "%s" % report)
    check("nothing past retention is left unrolled", report["raw_kept_unrolled"] == 0, "%s" % report)
    check("recent raw rows are kept", scalar(path, "SELECT COUNT(*) FROM snapshots") == 8,
          "%s" % scalar(path, "SELECT COUNT(*) FROM snapshots"))
    check("the old bucket is aggregated exactly",
          query(path, "SELECT n, pct_sum, pct_min, pct_max FROM rollups WHERE bucket_ts = ? "
                      "AND account_id = 'cmd-perso'", (history.epoch_to_iso(old_bucket),))[0][:] == (4, 100.0, 10.0, 40.0),
          "%s" % query(path, "SELECT n, pct_sum, pct_min, pct_max FROM rollups WHERE account_id = 'cmd-perso'"))
    check("a second series is aggregated independently",
          query(path, "SELECT n, pct_sum FROM rollups WHERE account_id = 'og-perso'")[0][:] == (2, 185.0),
          "%s" % query(path, "SELECT n, pct_sum FROM rollups WHERE account_id = 'og-perso'"))
    check("rollups past the rollup window are gone",
          scalar(path, "SELECT COUNT(*) FROM rollups WHERE pct_sum = 20.0") == 0,
          "%s" % scalar(path, "SELECT COUNT(*) FROM rollups"))

    conn = history.connect(path)
    try:
        history.maintain(conn, cfg, now_ts=NOW)
    finally:
        conn.close()
    check("re-running maintenance does not double-count",
          query(path, "SELECT n, pct_sum FROM rollups WHERE bucket_ts = ? AND account_id = 'cmd-perso'",
                (history.epoch_to_iso(old_bucket),))[0][:] == (4, 100.0),
          "%s" % query(path, "SELECT n, pct_sum FROM rollups WHERE account_id = 'cmd-perso'"))

    orphan = NOW - 200 * DAY
    insert(path, [(orphan, "cmd-perso", "monthly", 42.0)])
    conn = history.connect(path)
    try:
        report = history.maintain(conn, cfg, now_ts=NOW)
    finally:
        conn.close()
    check("a raw row whose bucket has no rollup is KEPT",
          scalar(path, "SELECT COUNT(*) FROM snapshots WHERE ts = ?", (history.epoch_to_iso(orphan),)) == 1
          and report["raw_kept_unrolled"] == 1,
          "%s" % report)
    conn = history.connect(path)
    try:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'rollup_frontier'",
                     (history.epoch_to_iso(orphan - 30 * DAY),))
        conn.commit()
        report = history.maintain(conn, cfg, now_ts=NOW)
    finally:
        conn.close()
    check("once its bucket is rolled up, the orphan is pruned",
          scalar(path, "SELECT COUNT(*) FROM snapshots WHERE ts = ?", (history.epoch_to_iso(orphan),)) == 0
          and report["raw_kept_unrolled"] == 0,
          "%s" % report)

    print("--- /api/history payload")
    result = history.payload({"since": [history.epoch_to_iso(NOW - 120 * DAY)],
                              "until": [history.epoch_to_iso(NOW)], "max_points": ["200"]},
                             cfg, now_ts=NOW)
    grid = result["t"]
    check("the grid is capped by max_points", len(grid) <= 202, "%d points" % len(grid))
    check("the grid is aligned and regular",
          grid[0] % result["bucket_seconds"] == 0
          and all(grid[i + 1] - grid[i] == result["bucket_seconds"] for i in range(len(grid) - 1)),
          "bucket %ss, %d points" % (result["bucket_seconds"], len(grid)))
    check("both sources are reported", result["sources"]["resolution"] == "raw+rollup",
          "%s" % result["sources"])
    series = {entry["key"]: entry for entry in result["series"]}
    check("series are keyed per account/window", set(series) == {"cmd-perso/monthly", "og-perso/weekly"},
          "%s" % sorted(series))
    cm = series["cmd-perso/monthly"]
    recent_index = grid.index(history.bucket_of(recent_bucket, result["bucket_seconds"]))
    old_index = grid.index(history.bucket_of(old_bucket, result["bucket_seconds"]))
    check("a recent bucket averages every sample", cm["avg"][recent_index] == 14.5, "%s" % cm["avg"][recent_index])
    check("an old bucket averages the rollup", cm["avg"][old_index] == 25.0, "%s" % cm["avg"][old_index])
    check("rollup min/max survive the merge",
          cm["min"][old_index] == 10.0 and cm["max"][old_index] == 40.0,
          "%s..%s" % (cm["min"][old_index], cm["max"][old_index]))
    check("sample counts survive the merge",
          cm["n"][recent_index] == 8 and cm["n"][old_index] == 4,
          "%s / %s" % (cm["n"][recent_index], cm["n"][old_index]))
    check("a bucket with no data is a null, not a zero", cm["avg"][recent_index - 5] is None,
          "%s" % cm["avg"][recent_index - 5])
    check("the other series is independent", series["og-perso/weekly"]["avg"][old_index] == 92.5,
          "%s" % series["og-perso/weekly"]["avg"][old_index])
    check("a series reports its own mean, peak and latest",
          cm["mean"] == 18.0 and cm["peak"] == 40.0 and cm["latest"]["percent"] == 14.5,
          "mean=%s peak=%s latest=%s" % (cm["mean"], cm["peak"], cm["latest"]))
    check("the summary names the peak series",
          result["summary"]["peak"]["key"] == "og-perso/weekly" and result["summary"]["peak"]["percent"] == 95.0,
          "%s" % result["summary"]["peak"])

    filtered = history.payload({"hours": ["1"], "account_id": ["og-perso"]}, cfg, now_ts=NOW)
    check("the account filter applies", [entry["key"] for entry in filtered["series"]] == [],
          "%s" % [entry["key"] for entry in filtered["series"]])
    filtered = history.payload({"hours": ["120"], "account_id": ["cmd-perso"]}, cfg, now_ts=NOW)
    check("the account filter keeps the matching series",
          [entry["key"] for entry in filtered["series"]] == ["cmd-perso/monthly"],
          "%s" % [entry["key"] for entry in filtered["series"]])
    check("a raw-only range says so", filtered["sources"]["resolution"] == "raw",
          "%s" % filtered["sources"])

    for params, expected in (
        ({"since": ["2026-01-02T00:00:00Z"], "until": ["2026-01-01T00:00:00Z"]}, "later than since"),
        ({"since": ["yesterday"]}, "ISO stamp"),
        ({"hours": ["abc"]}, "integers"),
        ({"max_points": ["5"]}, "max_points"),
        ({"bucket_seconds": ["30"]}, "bucket_seconds"),
        # An explicit bucket over a long range is a point count the caller chose: refused rather
        # than built, because 60 s over five years is 2.6 M points of grid.
        ({"days": ["1800"], "bucket_seconds": ["60"]}, "points, more than max_points"),
    ):
        refused = history.payload(params, cfg, now_ts=NOW)
        check("refused: %s" % json.dumps(params),
              expected in (refused.get("error") or ""), "%s" % refused.get("error"))

    off = history.payload({"hours": ["24"]}, config(enabled=False, path=path), now_ts=NOW)
    check("a disabled layer answers with a reason, not data",
          off.get("error") == "history is not enabled", "%s" % off)

    disabled_status = history.status(config(enabled=False, path=path))
    check("status says off without touching the store", disabled_status == {"enabled": False},
          "%s" % disabled_status)
    on_status = history.status(cfg)
    check("status reports what the store holds",
          on_status["enabled"] is True and on_status["error"] is None and on_status["series"] == 2
          and on_status["newest"] is not None,
          "%s" % on_status)


# ----------------------------------------------------------------------------- HTTP contract

PROVIDER_BASE_ENV = (
    "COMMANDCODE_API_BASE", "CHEAPERINFERENCE_API_BASE", "OPENROUTER_API_BASE", "DEEPSEEK_API_BASE",
    "MOONSHOT_API_BASE", "Z_AI_API_BASE", "SYNTHETIC_API_BASE",
)


class StubHandler(BaseHTTPRequestHandler):
    """Serves the two shapes this suite needs, and 401 to anything else.

    The 401 default is what keeps the suite hermetic: a base pointed here never reaches a
    vendor, whatever an adapter decides to call.
    """

    routes = {}

    def do_GET(self):
        path = self.path.split("?")[0]
        route = self.routes.get(path)
        if callable(route):
            status, ctype, body = route(self)
        elif route:
            status, ctype, body = route
        else:
            status, ctype = 401, "application/json"
            body = json.dumps({"error": "invalid api key: %s" % (self.headers.get("Authorization") or "")}).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def serve(routes):
    handler = type("Stub", (StubHandler,), {"routes": routes})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def boot(config_path, stub_port, extra_env=None):
    port = free_port()
    env = dict(os.environ)
    env.update({name: "http://127.0.0.1:%d" % stub_port for name in PROVIDER_BASE_ENV})
    env.update({
        "PORT": str(port),
        "QUOTA_CONFIG": config_path,
        "QUOTA_POLL_SECONDS": "1",             # the sampling gate is what is under test here
        "QUOTA_HTTP_TIMEOUT": "2",
        "QUOTA_BACKGROUND_DIR": os.path.join(WORK, "bg-%d" % port),
        "PYTHONUNBUFFERED": "1",
    })
    env.update(extra_env or {})
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py")], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = "http://127.0.0.1:%d" % port
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/api/health", timeout=2)
            return proc, base, True
        except urllib.error.HTTPError:
            return proc, base, True              # 503 while the first poll is pending
        except Exception:
            time.sleep(0.3)
    return proc, base, False


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def http_checks():
    print("--- the layer is absent until it is asked for")
    accounts = [
        {"id": "syn-main", "provider": "synthetic", "label": "Synthetic", "token": "syn_xxx"},
        # A balance provider in the same config: its card has no percentage, and the history
        # must not invent one for it.
        {"id": "ci-main", "provider": "cheaperinference", "label": "CheaperInference", "token": "ci_xxx"},
    ]
    disabled_path = os.path.join(WORK, "disabled", "quota.db")
    disabled_cfg = os.path.join(WORK, "disabled.json")
    with open(disabled_cfg, "w", encoding="utf-8") as fh:
        json.dump({"poll_seconds": 1, "background_url": "none", "accounts": accounts}, fh)

    calls = {"n": 0}

    def synthetic(request):
        calls["n"] += 1
        requests_used = 41 if calls["n"] == 1 else 82
        return (200, "application/json", json.dumps({
            "subscription": {"limit": 135, "requests": requests_used, "renewsAt": "2025-09-21T14:36:14.288Z"}
        }).encode())

    routes = {
        "/v2/quotas": synthetic,
        # Bytes, not a str: a route that hands the socket a string closes the connection, and
        # the account merely reads as failed — which is how "a balance contributes no series"
        # passes for the wrong reason.
        "/v1/account/balance": (200, "application/json", json.dumps({
            "object": "account.balance", "balance_usd": 40.0, "available_usd": 37.5,
            "reserved_usd": 2.5, "currency": "USD"}).encode()),
    }
    server, stub_port = serve(routes)
    try:
        proc, base, booted = boot(disabled_cfg, stub_port, {"QUOTA_DB": disabled_path,
                                                           "QUOTA_HISTORY_ENABLED": "0"})
        try:
            check("the panel boots with history off", booted, "nothing answered /api/health")
            status, body = get(base + "/api/quota")
            payload = json.loads(body)
            check("the page is told history is off", payload.get("history") == {"enabled": False},
                  "%s" % payload.get("history"))
            status, body = get(base + "/api/history?hours=1")
            check("the history route is absent when the layer is off", status == 404, "HTTP %s" % status)
            check("…and it says how to turn it on", "history.enabled" in body.decode(),
                  body.decode()[:120])
            check("no database is created anywhere while it is off", not os.path.exists(disabled_path),
                  disabled_path)
        finally:
            stop(proc)

        print("--- and it works once it is")
        enabled_path = fresh(os.path.join(WORK, "enabled", "quota.db"))
        enabled_cfg = os.path.join(WORK, "enabled.json")
        with open(enabled_cfg, "w", encoding="utf-8") as fh:
            json.dump({"poll_seconds": 1, "background_url": "none", "accounts": accounts,
                       "history": {"enabled": True, "path": enabled_path, "sample_seconds": 5,
                                   "raw_days": 90, "rollup_days": 365, "rollup_seconds": 900}}, fh)
        proc, base, booted = boot(enabled_cfg, stub_port)
        try:
            check("the panel boots with history on", booted, "nothing answered /api/health")
            status, body = get(base + "/api/quota")
            history_block = json.loads(body).get("history") or {}
            check("the page is told history is on, and how it samples",
                  history_block.get("enabled") is True and history_block.get("sample_seconds") == 5
                  and history_block.get("error") is None, "%s" % history_block)

            # Two samples: the stub answers 30.37% then 60.74%, so the stored series has to
            # show both, and the peak has to be the second one.
            deadline = time.time() + 20
            series = {}
            while time.time() < deadline:
                status, body = get(base + "/api/history?hours=1")
                if status == 200:
                    series = {entry["key"]: entry for entry in json.loads(body)["series"]}
                    if series.get("syn-main/subscription", {}).get("samples", 0) >= 2:
                        break
                time.sleep(1)
            check("a sample is stored per account and window",
                  set(series) == {"syn-main/subscription"}, "%s" % sorted(series))
            entry = series.get("syn-main/subscription") or {}
            check("both readings are in the series", (entry.get("samples") or 0) >= 2 and entry.get("peak") == 60.74,
                  "samples=%s peak=%s" % (entry.get("samples"), entry.get("peak")))
            check("the window's own label travels with the series",
                  entry.get("window_label") == "Window" and entry.get("account_label") == "Synthetic",
                  "%s / %s" % (entry.get("account_label"), entry.get("window_label")))
            check("a balance account contributes no series", "ci-main/credits" not in series
                  and not any(key.startswith("ci-main") for key in series), "%s" % sorted(series))
            check("the database exists where it was asked for", os.path.exists(enabled_path), enabled_path)

            status, body = get(base + "/api/history?hours=1&max_points=5")
            check("a bad parameter is refused with 400, not 500", status == 400 and "max_points" in body.decode(),
                  "HTTP %s %s" % (status, body.decode()[:80]))
        finally:
            stop(proc)

        print("--- a probe does not write history")
        probe_path = os.path.join(WORK, "probe", "quota.db")
        probe_env = {name: "http://127.0.0.1:%d" % stub_port for name in PROVIDER_BASE_ENV}
        probe_env.update({"QUOTA_CONFIG": enabled_cfg, "QUOTA_DB": probe_path, "PYTHONUNBUFFERED": "1"})
        with open(enabled_cfg, "r", encoding="utf-8") as fh:
            document = json.load(fh)
        document["history"]["path"] = probe_path
        with open(enabled_cfg, "w", encoding="utf-8") as fh:
            json.dump(document, fh)
        fresh(probe_path)
        result = subprocess.run([sys.executable, os.path.join(ROOT, "app.py"), "--check"], env=probe_env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
        check("--check exits 0 with the stub answering", result.returncode == 0,
              "exit=%s tail=%s" % (result.returncode, (result.stdout or "").strip().splitlines()[-1:]))
        check("--check does not create the database", not os.path.exists(probe_path), probe_path)
        check("--check prints the accounts, not history", '"accounts"' in (result.stdout or ""),
              (result.stdout or "")[:80])
    finally:
        server.shutdown()


def page_checks():
    """What the /history page must do, checked in its source.

    This repository has no JS test runner, so these are source assertions — the same kind the
    smoke suite already makes against index.html. Each one stands for a way the page was wrong
    once and would be wrong again silently:
      * every series stretched to its own peak, so a row's best day was always full opacity and
        the map said nothing about any other day;
      * twelve series on screen at once (every window of every account), including the five-hour
        window whose daily average describes nothing;
      * a chart drawn across the whole requested range, which for a store ten minutes old is an
        empty box with a speck in the corner.
    """
    with open(os.path.join(ROOT, "static", "history.html"), encoding="utf-8") as fh:
        page = fh.read()
    check("the page has one honest scale: the real percent, no scale selector",
          "scale-select" not in page and "Own scale" not in page,
          "a second scale is a second story about the same numbers")

    heat = page.split("async function renderHeatmap()")[1].split("// ---")[0] if \
        "async function renderHeatmap()" in page else ""
    # Comments in that function discuss the mistake on purpose, so the assertion reads the code
    # without its comments: what must never come back is a shade computed from a row's own peak.
    heat_code = "\n".join(line.split("//")[0] for line in heat.splitlines())
    check("the heatmap shades a cell by the real percentage",
          "Math.max(0.08, Math.min(1, value / 100))" in heat_code, heat_code[:60])
    check("the heatmap never scales a row to its own best day",
          bool(heat_code.strip()) and "peak" not in heat_code,
          "row-relative shading paints every row's peak the same and hides the rest")
    check("a day is a cell, not a full-width bar",
          ".heat-cell{height:18px" in page and "minmax(" in heat and ", 1fr)" in heat,
          "a single day stretched to the row width is what a full bar looked like")

    check("the page shows one window at a time, chosen by its own period",
          "function windowSeconds" in page and "s.window_key === state.window" in page)
    check("the default window is the widest the account has",
          "function defaultWindow" in page and "ranked[ranked.length - 1]" in page)
    check("a window whose label names no period is never the default",
          "w.seconds !== null" in page and "Infinity" in page)

    check("auto never asks for buckets finer than the store's own cadence",
          "Math.max(sampleSeconds(), Math.ceil(span / MAX_POINTS))" in page,
          "60-second buckets over a 300-second store is four empty buckets out of five")
    check("the stored span is what gets drawn, and the page says so",
          "function drawnSpan" in page and "range starts before this store does" in page,
          "a young store would draw an empty day-long axis with a speck at the right edge")


def main():
    try:
        storage_checks()
        http_checks()
        page_checks()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    print()
    if FAILURES:
        print("%d check(s) FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("history checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
