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
import sqlite3
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


def pending_row(path, alert_id):
    return scalar(path, "SELECT status FROM alerts WHERE id = ?", (alert_id,))


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
    check("a balance is stored as a money amount, in its own table",
          scalar(path, "SELECT COUNT(*) FROM balances WHERE window_key = 'credits'") == 1
          and scalar(path, "SELECT amount FROM balances WHERE window_key = 'credits'") == 12.4,
          "%s" % scalar(path, "SELECT amount FROM balances"))
    check("the series records whether it is a window or a balance",
          scalar(path, "SELECT kind FROM series WHERE window_key = 'credits'") == "balance"
          and scalar(path, "SELECT kind FROM series WHERE window_key = 'subscription'") == "window",
          "%s" % query(path, "SELECT window_key, kind FROM series"))
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

    print("--- crossing alerts")
    alert_path = fresh(os.path.join(WORK, "alerts.db"))
    alert_cfg = config(path=alert_path, sample_seconds=1, alert_percent=50)
    history._LAST_SAMPLE.clear()

    def reading(percent):
        return [{"id": "syn-main", "provider": "synthetic", "label": "Synthetic", "state": "ok",
                 "windows": [{"kind": "window", "key": "subscription", "label": "Window",
                              "percent": percent}]}]

    history.record(reading(40), alert_cfg, now_ts=NOW)
    below = history.record(reading(45), alert_cfg, now_ts=NOW + 10)
    cross = history.record(reading(55), alert_cfg, now_ts=NOW + 20)
    again = history.record(reading(60), alert_cfg, now_ts=NOW + 30)
    check("no alert while the reading stays under the line", below["alerts"] == [], "%s" % below["alerts"])
    check("a crossing is reported once, with both readings",
          len(cross["alerts"]) == 1 and cross["alerts"][0]["previous"] == 45
          and cross["alerts"][0]["percent"] == 55 and cross["alerts"][0]["threshold"] == 50,
          "%s" % cross["alerts"])
    check("staying above the line does not re-alert", again["alerts"] == [], "%s" % again["alerts"])
    cross_id = cross["alerts"][0]["id"] if cross["alerts"] else None
    check("a crossing is written to the event log as pending",
          pending_row(alert_path, cross_id) == "pending", "id=%s" % cross_id)
    check("an undelivered alert is pending for a restart to retry",
          [row["id"] for row in history.pending_alerts(alert_cfg)] == [cross_id],
          "%s" % history.pending_alerts(alert_cfg))
    history.mark_alert(alert_cfg, cross_id, "failed", attempts=4, error="boom", now_ts=NOW + 40)
    check("a failed delivery is recorded with its attempts and error",
          pending_row(alert_path, cross_id) == "failed"
          and scalar(alert_path, "SELECT attempts FROM alerts WHERE id = ?", (cross_id,)) == 4
          and scalar(alert_path, "SELECT last_error FROM alerts WHERE id = ?", (cross_id,)) == "boom",
          "%s" % query(alert_path, "SELECT status, attempts, last_error FROM alerts"))
    check("a failed alert is not pending again",
          history.pending_alerts(alert_cfg) == [], "%s" % history.pending_alerts(alert_cfg))

    # The prior reading can be a rollup: once raw_days has passed, the raw row is gone and the
    # crossing has to be detected against what the rollup still holds.
    rolled_path = fresh(os.path.join(WORK, "rolled-alert.db"))
    rolled_cfg = config(path=rolled_path, sample_seconds=60, alert_percent=50)
    conn = history.connect(rolled_path)
    try:
        conn.execute(
            "INSERT INTO rollups (bucket_ts, account_id, window_key, n, pct_sum, pct_min, pct_max) "
            "VALUES (?,?,?,?,?,?,?)",
            (history.epoch_to_iso(NOW - 3600), "syn-main", "subscription", 2, 50.0, 20.0, 30.0),
        )
        conn.execute(
            "INSERT OR REPLACE INTO series (account_id, window_key, account_label, provider, "
            "window_label, kind, first_ts, last_ts) VALUES (?,?,?,?,?,?,?,?)",
            ("syn-main", "subscription", "Synthetic", "synthetic", "Window", "window",
             history.epoch_to_iso(NOW - 3600), history.epoch_to_iso(NOW - 3600)),
        )
        conn.commit()
    finally:
        conn.close()
    history._LAST_SAMPLE.clear()
    rolled_alert = history.record(reading(60), rolled_cfg, now_ts=NOW)
    check("a crossing is detected against a rollup-only prior reading",
          len(rolled_alert["alerts"]) == 1 and rolled_alert["alerts"][0]["previous"] == 30.0,
          "%s" % rolled_alert["alerts"])

    # The gate used to be `now - last >= sample_seconds`, which drifts with the poll cadence: each
    # cycle is a little longer than poll_seconds, the write slides forward inside its window, and
    # eventually a whole window is skipped -- which the trend draws as a gap. Poll every 80 s and
    # require every 300 s window to hold a reading.
    drift_path = fresh(os.path.join(WORK, "drift.db"))
    drift_cfg = config(path=drift_path, sample_seconds=300)
    stamps = [0]
    for _ in range(120):
        stamps.append(stamps[-1] + 80)
    drift_written = sum(history.record(reading(50), drift_cfg, now_ts=stamp)["written"]
                        for stamp in stamps)
    drift = history.payload({"since": [history.epoch_to_iso(0)],
                             "until": [history.epoch_to_iso(stamps[-1])],
                             "bucket_seconds": ["300"], "max_points": ["500"]},
                            drift_cfg, now_ts=stamps[-1])
    drift_entry = {e["key"]: e for e in drift["series"]}.get("syn-main/subscription") or {}
    holes = [i for i, value in enumerate(drift_entry.get("avg") or []) if value is None]
    check("a drifting poll cadence never leaves a sample window empty",
          not holes and drift_written >= 30,
          "holes=%s written=%s" % (holes[:6], drift_written))

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

    print("--- raw and rollup halves of one display bucket are both counted")
    merge_path = fresh(os.path.join(WORK, "merge.db"))
    merge_cfg = config(path=merge_path)                 # rollup_seconds = BUCKET (900)
    display = history.bucket_of(NOW - 3600, 1800)
    older, newer = display, display + 900
    conn = history.connect(merge_path)
    try:
        conn.execute(
            "INSERT INTO rollups (bucket_ts, account_id, window_key, n, pct_sum, pct_min, pct_max) "
            "VALUES (?,?,?,?,?,?,?)",
            (history.epoch_to_iso(older), "cmd-perso", "monthly", 2, 40.0, 20.0, 20.0),
        )
        conn.execute(
            "INSERT INTO rollups (bucket_ts, account_id, window_key, n, pct_sum, pct_min, pct_max) "
            "VALUES (?,?,?,?,?,?,?)",
            (history.epoch_to_iso(newer), "cmd-perso", "monthly", 2, 160.0, 80.0, 80.0),
        )
        conn.executemany(
            "INSERT INTO snapshots (ts, account_id, provider, window_key, percent) VALUES (?,?,?,?,?)",
            [(history.epoch_to_iso(newer + 60), "cmd-perso", "synthetic", "monthly", 80.0),
             (history.epoch_to_iso(newer + 120), "cmd-perso", "synthetic", "monthly", 80.0)],
        )
        conn.execute(
            "INSERT OR REPLACE INTO series (account_id, window_key, account_label, provider, "
            "window_label, kind, first_ts, last_ts) VALUES (?,?,?,?,?,?,?,?)",
            ("cmd-perso", "monthly", "CommandCode", "synthetic", "Month", "window",
             history.epoch_to_iso(older), history.epoch_to_iso(newer + 120)),
        )
        conn.commit()
    finally:
        conn.close()
    merged = history.payload({"since": [history.epoch_to_iso(display)],
                              "until": [history.epoch_to_iso(display + 1800)],
                              "bucket_seconds": ["1800"], "max_points": ["10"]},
                             merge_cfg, now_ts=NOW)
    merge_entry = {e["key"]: e for e in merged["series"]}.get("cmd-perso/monthly") or {}
    check("a display bucket keeps both its pruned-raw rollup half and its raw half",
          merge_entry.get("avg", [None])[0] == 50.0 and merged["sources"]["rollup_rows"] == 1,
          "avg=%s sources=%s" % (merge_entry.get("avg"), merged["sources"]))

    # A bucket straddling the raw cutoff: its older raw row is inside the raw window's edge, its
    # newer rows are past it. Without a whole-bucket prune the survivors shadow the complete
    # rollup, and the cell undercounts. The rollup pass first computes R's rollup (50,80,80), then
    # the prune must remove every raw row of R, not only the one below the cutoff.
    shadow_path = fresh(os.path.join(WORK, "shadow.db"))
    shadow_cfg = config(path=shadow_path)                 # rollup_seconds = BUCKET, raw_days = 90
    R = history.bucket_of(NOW - 100 * DAY, BUCKET)
    shadow_now = R + 300 + 90 * DAY                       # cutoff = R + 300, mid-bucket
    conn = history.connect(shadow_path)
    try:
        conn.executemany(
            "INSERT INTO snapshots (ts, account_id, provider, window_key, percent) VALUES (?,?,?,?,?)",
            [(history.epoch_to_iso(R + 100), "cmd-perso", "synthetic", "monthly", 50.0),
             (history.epoch_to_iso(R + 400), "cmd-perso", "synthetic", "monthly", 80.0),
             (history.epoch_to_iso(R + 700), "cmd-perso", "synthetic", "monthly", 80.0)],
        )
        conn.execute(
            "INSERT OR REPLACE INTO series (account_id, window_key, account_label, provider, "
            "window_label, kind, first_ts, last_ts) VALUES (?,?,?,?,?,?,?,?)",
            ("cmd-perso", "monthly", "CommandCode", "synthetic", "Month", "window",
             history.epoch_to_iso(R + 100), history.epoch_to_iso(R + 700)),
        )
        conn.commit()
    finally:
        conn.close()
    conn = history.connect(shadow_path)
    try:
        history.maintain(conn, shadow_cfg, now_ts=shadow_now)
    finally:
        conn.close()
    shadow = history.payload({"since": [history.epoch_to_iso(R)],
                              "until": [history.epoch_to_iso(R + BUCKET)],
                              "bucket_seconds": [str(BUCKET)], "max_points": ["10"]},
                             shadow_cfg, now_ts=shadow_now)
    shadow_entry = {e["key"]: e for e in shadow["series"]}.get("cmd-perso/monthly") or {}
    check("a bucket whose raw rows were pruned does not shadow its complete rollup",
          shadow_entry.get("avg", [None])[0] == 70.0 and shadow_entry.get("n", [0])[0] == 3,
          "avg=%s n=%s" % (shadow_entry.get("avg"), shadow_entry.get("n")))

    # H7: the read path is write-proof. mode=ro when it opens, query_only on the fallback; either
    # way an INSERT must be refused.
    ro_conn = history.connect(os.path.join(WORK, "sampling.db"), read_only=True)
    try:
        refused = False
        try:
            ro_conn.execute("INSERT INTO meta (key, value) VALUES ('probe', '1')")
        except sqlite3.Error:
            refused = True
    finally:
        ro_conn.close()
    check("a read-only connection refuses a write", refused, "the read-only handle accepted an INSERT")

    print("--- balances roll up and prune like percentages")
    bpath = fresh(os.path.join(WORK, "balance-retention.db"))
    bcfg = config(path=bpath)
    bold = history.bucket_of(NOW - 100 * DAY, BUCKET)
    conn = history.connect(bpath)
    try:
        conn.executemany(
            "INSERT INTO balances (ts, account_id, provider, window_key, amount, currency, resets_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(history.epoch_to_iso(bold + 60 * i), "ci-main", "cheaperinference", "balance",
              40.0 - i, "USD", None) for i in range(4)],
        )
        conn.commit()
    finally:
        conn.close()
    conn = history.connect(bpath)
    try:
        breport = history.maintain(conn, bcfg, now_ts=NOW)
    finally:
        conn.close()
    check("a balance rolls up into its own table, exactly",
          query(bpath, "SELECT n, amt_sum, amt_min, amt_max FROM balance_rollups")[0][:] == (4, 154.0, 37.0, 40.0),
          "%s" % query(bpath, "SELECT n, amt_sum FROM balance_rollups"))
    check("a balance raw row is pruned once its bucket exists",
          scalar(bpath, "SELECT COUNT(*) FROM balances") == 0 and breport["balance_raw_deleted"] == 4,
          "%s" % breport)
    rolled = history.payload({"since": [history.epoch_to_iso(bold - 3600)],
                              "until": [history.epoch_to_iso(bold + 3600)]}, bcfg, now_ts=NOW)
    rolled_balance = {e["key"]: e for e in rolled["series"]}.get("ci-main/balance") or {}
    check("a balance served from its rollup keeps its currency",
          rolled_balance.get("kind") == "balance" and rolled_balance.get("currency") == "USD",
          "%s" % rolled_balance)

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

    # A balance is served as its own kind and must not pull the percentage summary toward a
    # number it never carried.
    balance_payload = history.payload({"hours": ["1"]}, config(path=os.path.join(WORK, "sampling.db")),
                                      now_ts=NOW + 400)
    balance_series = {entry["key"]: entry for entry in balance_payload["series"]}
    check("a balance is served as a balance series",
          balance_series.get("syn-main/credits", {}).get("kind") == "balance"
          and (balance_series.get("syn-main/credits", {}).get("samples") or 0) >= 1,
          "%s" % sorted(balance_series))
    check("a balance does not pull the percent summary",
          balance_payload["summary"]["peak"]["key"] == "syn-main/subscription",
          "%s" % balance_payload["summary"])
    filtered = history.payload({"hours": ["1"], "series": ["syn-main/credits"]},
                               config(path=os.path.join(WORK, "sampling.db")), now_ts=NOW + 400)
    check("a balance key in the series filter is honoured",
          [entry["key"] for entry in filtered["series"]] == ["syn-main/credits"],
          "%s" % [entry["key"] for entry in filtered["series"]])

    print("--- resets and the threshold the chart draws")
    reset_path = fresh(os.path.join(WORK, "resets.db"))
    reset_cfg = config(path=reset_path, alert_percent=80)
    conn = history.connect(reset_path)
    try:
        conn.execute(
            "INSERT INTO snapshots (ts, account_id, provider, window_key, percent, used, cap, resets_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (history.epoch_to_iso(NOW - 3600), "cmd-perso", "synthetic", "monthly", 10.0, None, None,
             history.epoch_to_iso(NOW - 1800)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO series (account_id, window_key, account_label, provider, "
            "window_label, kind, first_ts, last_ts) VALUES (?,?,?,?,?,?,?,?)",
            ("cmd-perso", "monthly", "CommandCode", "synthetic", "Month", "window",
             history.epoch_to_iso(NOW - 3600), history.epoch_to_iso(NOW - 3600)),
        )
        conn.commit()
    finally:
        conn.close()
    reset_payload = history.payload({"hours": ["2"], "bucket_seconds": ["900"]}, reset_cfg, now_ts=NOW)
    check("a reset in the range is named, with its series",
          reset_payload["resets"] == [{"key": "cmd-perso/monthly", "at": NOW - 1800}],
          "%s" % reset_payload["resets"])
    check("the payload carries the threshold the chart draws",
          reset_payload["alert_percent"] == 80, "%s" % reset_payload["alert_percent"])

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
    posted = []
    post_statuses = []            # scripted HTTP statuses for the next POSTs; empty -> 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            StubHandler.posted.append(json.loads(body.decode("utf-8") or "{}"))
        except ValueError:
            StubHandler.posted.append({"raw": body.decode("utf-8", "replace")})
        self.send_response(StubHandler.post_statuses.pop(0) if StubHandler.post_statuses else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

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
        # The crossing webhook points at this suite's own stub, so a real POST is observed
        # rather than the call merely not raising. The stub's counter is reset because the
        # history-off boot above already polled it once, and the crossing needs a real
        # below-then-above pair in THIS app's samples.
        StubHandler.posted.clear()
        StubHandler.post_statuses[:] = [500, 500]      # two failures, then a success
        calls["n"] = 0
        with open(enabled_cfg, "w", encoding="utf-8") as fh:
            json.dump({"poll_seconds": 1, "background_url": "none", "accounts": accounts,
                       "history": {"enabled": True, "path": enabled_path, "sample_seconds": 5,
                                   "raw_days": 90, "rollup_days": 365, "rollup_seconds": 900,
                                   "alert_percent": 50,
                                   "alert_url": "http://127.0.0.1:%d/hook" % stub_port}}, fh)
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
                  {"syn-main/subscription", "ci-main/balance"} <= set(series), "%s" % sorted(series))
            entry = series.get("syn-main/subscription") or {}
            check("both readings are in the series", (entry.get("samples") or 0) >= 2 and entry.get("peak") == 60.74,
                  "samples=%s peak=%s" % (entry.get("samples"), entry.get("peak")))
            check("the window's own label travels with the series",
                  entry.get("window_label") == "Window" and entry.get("account_label") == "Synthetic",
                  "%s / %s" % (entry.get("account_label"), entry.get("window_label")))
            balance = series.get("ci-main/balance") or {}
            check("a balance is stored as a balance, never a percentage",
                  balance.get("kind") == "balance" and balance.get("currency") == "USD"
                  and (balance.get("samples") or 0) >= 1
                  and not any(series[key].get("kind") != "balance"
                              for key in series if key.startswith("ci-main")),
                  "%s" % balance)
            check("the database exists where it was asked for", os.path.exists(enabled_path), enabled_path)

            # The crossing webhook runs on a worker thread with retry/backoff; the stub fails
            # twice, so the third attempt lands after ~3 s. Wait on the outcome the worker
            # records, not on the POST count: the third POST only says the stub answered, and
            # the row is written a moment later — on a slow runner the gap between the two is
            # what a single immediate read would measure instead of the result.
            hook_deadline = time.time() + 20
            while time.time() < hook_deadline and len(StubHandler.posted) < 3:
                time.sleep(0.3)
            alert = StubHandler.posted[0] if StubHandler.posted else {}
            check("a crossing is posted to the configured webhook",
                  alert.get("threshold") == 50 and alert.get("account_id") == "syn-main"
                  and alert.get("previous") == 30.37 and alert.get("percent") == 60.74,
                  "%s" % (alert or StubHandler.posted))
            check("a failing webhook is retried before it is given up on",
                  len(StubHandler.posted) == 3, "%d POST(s)" % len(StubHandler.posted))
            outcome = []
            hook_deadline = time.time() + 20
            while time.time() < hook_deadline:
                outcome = query(enabled_path, "SELECT status, attempts FROM alerts ORDER BY id DESC LIMIT 1")
                if outcome and outcome[0][0] in ("delivered", "failed"):
                    break
                time.sleep(0.2)
            check("the delivery outcome is recorded in the event log",
                  bool(outcome) and outcome[0][0] == "delivered" and outcome[0][1] == 3,
                  "%s" % [(row[0], row[1]) for row in outcome])

            status, body = get(base + "/api/history?hours=1&max_points=5")
            check("a bad parameter is refused with 400, not 500", status == 400 and "max_points" in body.decode(),
                  "HTTP %s %s" % (status, body.decode()[:80]))
        finally:
            stop(proc)

        print("--- a webhook that never answers ends failed, and the poller keeps polling")
        fail_cfg = os.path.join(WORK, "fail.json")
        fail_path = os.path.join(WORK, "fail.db")
        StubHandler.posted.clear()
        StubHandler.post_statuses[:] = [500] * 50
        calls["n"] = 0
        with open(fail_cfg, "w", encoding="utf-8") as fh:
            json.dump({"poll_seconds": 1, "background_url": "none", "accounts": accounts,
                       "history": {"enabled": True, "path": fail_path, "sample_seconds": 5,
                                   "raw_days": 90, "rollup_days": 365, "rollup_seconds": 900,
                                   "alert_percent": 50,
                                   "alert_url": "http://127.0.0.1:%d/hook" % stub_port,
                                   "alert_retries": 1, "alert_backoff_seconds": 1}}, fh)
        proc, base, booted = boot(fail_cfg, stub_port)
        try:
            check("a second store boots for the failure path", booted, "%s" % base)
            deadline = time.time() + 25
            while time.time() < deadline:
                rows = query(fail_path, "SELECT status, attempts FROM alerts")
                if rows and rows[0][0] == "failed":
                    break
                time.sleep(0.3)
            rows = query(fail_path, "SELECT status, attempts, last_error FROM alerts")
            check("a webhook that always fails ends failed after retries+1 attempts",
                  rows and rows[0][0] == "failed" and rows[0][1] == 2, "%s" % rows)
            status, body = get(base + "/api/quota")
            parsed = json.loads(body.decode()) if status == 200 else {}
            check("the poller keeps polling after a failed webhook",
                  any(a.get("state") == "ok" for a in parsed.get("accounts", [])),
                  "HTTP %s" % status)
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
      * a colour per account in the map, which made a shade mean "which account" instead of "how
        much" and left two rows impossible to compare down a column;
      * twelve series on screen at once (every window of every account), including the five-hour
        window whose daily average describes nothing — and a Window selector asking the reader to
        answer the question the page had just failed to answer for them;
      * a chart drawn across the whole requested range, which for a store ten minutes old is an
        empty box with a speck in the corner;
      * a refresh that failed by erasing what was already drawn.
    """
    with open(os.path.join(ROOT, "static", "history.html"), encoding="utf-8") as fh:
        page = fh.read()
    check("the page has one honest scale: the real percent, no scale selector",
          "scale-select" not in page and "Own scale" not in page,
          "a second scale is a second story about the same numbers")
    check("the history panels carry the calmer chrome",
          ".panel{background:rgba(var(--card),var(--card-alpha));border:1px solid var(--line);border-radius:16px;" in page
          and ".stat{background:var(--card-2);border:1px solid var(--line);border-radius:12px;" in page,
          "the panels and stat tiles must share the refined radii")
    check("the page remembers the three controls client-side",
          "localStorage" in page and "quota-panel:history" in page
          and "function applyStoredSettings" in page and "function saveSettings" in page,
          "a reload must keep the account, range and interval the reader chose")
    check("the stored settings are read defensively",
          "function applyStoredSettings" in page and "try{" in page
          and "JSON.parse" in page and "catch(e)" in page,
          "a disabled or corrupt store must fall back to defaults, not throw")
    check("a stored account is re-validated against the catalogue",
          "state.account = ids.indexOf(state.account) >= 0 ? state.account : ''" in page,
          "a stored account that no longer exists must fall back to All accounts")
    check("a restored interval the range cannot afford falls back to Auto",
          "if(state.bucket && state.bucket < floor) state.bucket = 0;" in page,
          "an over-fine bucket would ask the server for more points than it allows")
    check("the square charts draw into one square box",
          "const SQUARE = 236" in page and "function scatterLegendHtml" in page
          and 'id="radar-values"' in page,
          "a radar or donut floating in a wide tile is unreadable")

    heat = page.split("async function renderHeatmap()")[1].split("// ---")[0] if \
        "async function renderHeatmap()" in page else ""
    # Comments in that function discuss the mistake on purpose, so the assertion reads the code
    # without its comments: what must never come back is a shade computed from a row's own peak.
    heat_code = "\n".join(line.split("//")[0] for line in heat.splitlines())
    check("the heatmap shades a cell by the real percentage",
          "function heatPaint" in page and "Math.max(0.08, Math.min(1, value / 100))" in page,
          "the shade has to come from the reading, not from the row")
    check("the heatmap never scales a row to its own best day",
          bool(heat_code.strip()) and "peak" not in heat_code,
          "row-relative shading paints every row's peak the same and hides the rest")
    check("the heatmap paints every row with the same hue, mixed into the card",
          "const HEAT_HUE" in page and "HEAT_CARD" in page
          and "lineColor" not in heat_code and "accountColor" not in heat_code
          and "opacity" not in heat_code,
          "a colour per account makes a shade say which account, and a translucent cell lets the "
          "artwork behind the panel change the shade of the same number")
    check("a day is a cell, not a full-width bar",
          ".heat-cell{height:18px" in page and "minmax(" in heat and ", 1fr)" in heat,
          "a single day stretched to the row width is what a full bar looked like")

    check("the page offers the account, the range and the interval, and no window selector",
          '"window-select"' not in page and 'id="account-select"' in page
          and 'id="range-select"' in page and 'id="bucket-select"' in page,
          "a window selector asks the reader to answer the question the page exists to answer")
    check("the window that stands for an account is the widest its label names",
          "function choosePrimary" in page
          and "position > ranked[entry.account_id].position" in page and "Infinity" in page,
          "a label that names no period cannot be ranked, so it must never stand for an account")
    check("one account selected draws every window that account publishes",
          "s.account_id === state.account" in page,
          "with an account chosen, identity is the window; with all of them, it is the account")
    check("the cap is drawn on the chart and named",
          "(cap ? ' cap' : '')" in page and "stroke-dasharray=\"4 3\"" in page,
          "a percentage chart the reader cannot measure against its own cap is a shape, not a reading")
    check("each line is filled under, the way the page it was modelled on is",
          "linearGradient" in page and 'stop-opacity="0.28"' in page,
          "a hairline on a dark background reads as less than it is")
    check("a refresh that fails keeps the readings already on screen",
          "showing the previous range" in page and "function renderNote" in page,
          "a transient stall must not empty a chart the reader was reading")

    check("auto never asks for buckets finer than the store's own cadence",
          "Math.max(sampleSeconds(), Math.ceil(span / MAX_POINTS))" in page,
          "60-second buckets over a 300-second store is four empty buckets out of five")
    check("the stored span is what gets drawn, and the page says so",
          "function drawnSpan" in page and "range starts before this store does" in page,
          "a young store would draw an empty day-long axis with a speck at the right edge")

    check("the page carries a provider palette and marks",
          "const PROVIDER_COLORS" in page and "const PROVIDER_LOGOS" in page
          and "function providerLogo" in page,
          "a line's colour and mark have to say which provider it belongs to")
    check("the page draws the panels the reference page draws",
          all(('id="%s-panel"' % name) in page for name in ("weekly", "donut", "scatter", "radar", "insights"))
          and all(("function render%s" % name) in page
                  for name in ("Weekly", "Donut", "Scatter", "Radar", "Insights")),
          "the multi-chart grid is the body of the reference page")
    check("the summary names the peak account and the current pressure",
          "Peak account" in page and "Current pressure" in page and "volatility" in page,
          "the summary has to name who is under pressure, not just a number")
    check("a day-or-longer range is snapped to calendar days",
          "function rangeParams" in page and "setHours(0, 0, 0, 0)" in page,
          "a daily bar has to line up with a day")
    check("the alert threshold is drawn and named",
          "% alert" in page and "alert_percent" in page,
          "a threshold the reader cannot see is not a threshold")
    check("reset markers are drawn from the store's own resets",
          "state.data.resets" in page and "resetIndexes" in page,
          "a sawtooth that reads as a fall is a sawtooth the page failed to name")
    check("a projection names the soonest cap crossing",
          "function projection" in page and "On pace for the cap" in page,
          "the slope is the one forward-looking number the range actually supports")
    note_code = (page.split("function renderNote")[1].split("function ")[0]
                 if "function renderNote" in page else "")
    check("the projection note respects an isolated line",
          "state.isolate" in note_code,
          "an isolated line must not be projected from windows that are not drawn")
    check("a balance is kept out of the percent chart and shown as money",
          "filter(isWindow)" in page and "fmtMoney" in page and "balanceOnly" in page,
          "a balance has no percentage, and a money axis cannot share a percent chart")

    check("a balance-only store is told why the percent panels are empty",
          "function balanceOnlyStore" in page and "function noPercentMessage" in page
          and "money balance, not a percentage" in page,
          "the empty state must not blame sampling when the data is money")

    check("the page carries the support link to Ko-fi",
          "https://ko-fi.com/realAbitbol" in page and 'rel="noopener noreferrer"' in page
          and 'aria-label="Support this project on Ko-fi"' in page,
          "the funding link is part of the page")
    check("the page surfaces the store's retention and this range's resolution",
          "function renderStore" in page and 'id="store-line"' in page
          and "balance_rollup_rows" in page,
          "a reader has to know whether a flat line is the plan or the retention policy")
    check("a per-window breakdown appears when one account is chosen",
          'id="breakdown-panel"' in page and "function renderBreakdown" in page
          and "Next reset" in page,
          "the chosen account's windows get a panel of their own")
    check("the request asks for the viewed accounts' balances too",
          "function requestKeys" in page and "entry.kind === 'balance'" in page
          and "accounts[state.account] = true" in page and "paramsFor(requestKeys()" in page,
          "a balance-only account has no window in trendKeys(), so the selection seeds the set")
    check("an empty selection never becomes a wildcard request",
          "__none__" in page,
          "no series= at all is the server's no-filter, which draws every account")
    check("a superseded response cannot overwrite a newer one",
          "state.requestId" in page and "token !== state.requestId" in page,
          "an out-of-order response must not render a range the controls no longer show")
    check("the chart legends are keyboard- and AT-legible",
          "aria-pressed" in page and "tabindex" in page and "ArrowRight" in page,
          "a value only a mouse can read is a value half the readers cannot read")

    check("the trend legend carries each series' provider mark",
          "badgeHtml(s.provider, seriesLabel(s))" in page,
          "the legend must show the provider mark, not only a colour dot")

    check("the live-panel link reads as a button and lights up on hover",
          ".nav{font-size:12.5px;color:var(--fg)" in page
          and "background:rgba(var(--accent-rgb),.14)" in page
          and ".nav:hover{color:#fff;border-color:var(--accent)}" in page,
          "the header link must read as a pill and light up on hover")
    check("the sparkline breaks on gaps and the donut ink follows the slice",
          "previous + 1" in page and "function labelInk" in page,
          "a gap drawn as a line invents data, and fixed ink vanishes on dark slices")


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
