#!/usr/bin/env python3
"""History tests: bucketing, exact rollup aggregates, ordered pruning, merged view.

Fully offline and deterministic — the database is built by the test itself and the
clock is a constant, so these assertions pin the storage contract (raw retention,
rollup retention, the raw+rollup merge served by /api/history) instead of the
providers' current state.
"""
import importlib.util
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp(prefix="quota-history-")
os.environ["QUOTA_DB"] = os.path.join(WORK, "quota.db")
os.environ["QUOTA_CONFIG"] = os.path.join(ROOT, "accounts.example.json")

_spec = importlib.util.spec_from_file_location("quota_app", os.path.join(ROOT, "app.py"))
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

DAY = 86400
NOW = 1800000123                  # fixed clock, deliberately off a bucket boundary so
BUCKET = 900                      # the frontier falls strictly before "now" and the
FRONTIER = (NOW // BUCKET) * BUCKET   # merged view is exercised inside one bucket

failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    if not ok:
        failures.append(name)


def bucket_of(epoch, size=BUCKET):
    return (epoch // size) * size


def insert(rows):
    conn = app.db_connect()
    try:
        conn.executemany(
            "INSERT INTO snapshots (ts, account_id, window_key, percent, used, cap, resets_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(app.epoch_to_iso(ts), acc, win, pct, None, None, None) for ts, acc, win, pct in rows],
        )
        conn.commit()
    finally:
        conn.close()


def query(sql, args=()):
    conn = app.db_connect()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def scalar(sql, args=()):
    rows = query(sql, args)
    return rows[0][0] if rows else None


def main():
    app.RETENTION = {"raw_days": 90, "rollup_days": 365, "rollup_seconds": BUCKET}

    recent = [(NOW - 60 * i, "cmd-perso", "monthly", 50 + (30 - i)) for i in range(30, 0, -1)]
    old = [(NOW - 100 * DAY + 60 * i, "cmd-perso", "monthly", 10 + 10 * i) for i in range(4)]
    old_other = [(NOW - 100 * DAY + 60 * i, "og-perso", "weekly", 90 + 5 * i) for i in range(2)]
    expired = [(NOW - 400 * DAY + 60 * i, "cmd-perso", "monthly", 5) for i in range(4)]
    insert(recent + old + old_other + expired)

    print("--- seeded %d raw rows (recent %d, old %d, expired %d)"
          % (len(recent) + len(old) + len(old_other) + len(expired),
             len(recent), len(old) + len(old_other), len(expired)))

    conn = app.db_connect()
    try:
        report = app.maintain_history(conn, now_ts=NOW)
    finally:
        conn.close()

    check("maintenance rolls up", report["rollup_rows"] > 0, "%d row(s)" % report["rollup_rows"])
    check("maintenance prunes raw", report["raw_deleted"] > 0, "%d row(s)" % report["raw_deleted"])
    check("maintenance prunes expired rollups", report["rollup_deleted"] > 0,
          "%d row(s)" % report["rollup_deleted"])
    check("no raw row past retention left unrolled", report["raw_kept_unrolled"] == 0,
          "%d" % report["raw_kept_unrolled"])

    check("recent raw rows kept", scalar("SELECT COUNT(*) FROM snapshots") == len(recent),
          "%s" % scalar("SELECT COUNT(*) FROM snapshots"))
    check("nothing raw past the 90d cutoff",
          scalar("SELECT COUNT(*) FROM snapshots WHERE ts < ?", (app.epoch_to_iso(NOW - 90 * DAY),)) == 0)

    row = query("SELECT n, pct_sum, pct_min, pct_max FROM rollups WHERE bucket_ts = ? "
                "AND account_id = 'cmd-perso' AND window_key = 'monthly'", (bucket_of(NOW - 100 * DAY),))
    check("old bucket rolled up exactly", row == [(4, 100.0, 10.0, 40.0)], "%s" % (row,))
    row = query("SELECT n, pct_sum FROM rollups WHERE bucket_ts = ? AND account_id = 'og-perso'",
                (bucket_of(NOW - 100 * DAY),))
    check("per-series rollup is independent", row == [(2, 185.0)], "%s" % (row,))
    check("expired rollups gone",
          scalar("SELECT COUNT(*) FROM rollups WHERE account_id = 'cmd-perso' AND pct_sum = 20.0") == 0)
    check("rollups inside retention kept",
          scalar("SELECT COUNT(*) FROM rollups WHERE bucket_ts >= ? AND bucket_ts < ?",
                 (NOW - 365 * DAY, NOW - 90 * DAY)) == 2,
          "%s" % scalar("SELECT COUNT(*) FROM rollups WHERE bucket_ts >= ? AND bucket_ts < ?",
                        (NOW - 365 * DAY, NOW - 90 * DAY)))

    # Idempotence: a second pass must not double-count the buckets it rewrites.
    conn = app.db_connect()
    try:
        app.maintain_history(conn, now_ts=NOW)
    finally:
        conn.close()
    row = query("SELECT n, pct_sum, pct_min, pct_max FROM rollups WHERE bucket_ts = ? "
                "AND account_id = 'cmd-perso' AND window_key = 'monthly'", (bucket_of(NOW - 100 * DAY),))
    check("re-running maintenance does not double-count", row == [(4, 100.0, 10.0, 40.0)], "%s" % (row,))

    # The guard: a raw row past the cutoff whose bucket has no rollup must be KEPT.
    orphan = NOW - 200 * DAY
    insert([(orphan, "cmd-perso", "monthly", 42)])
    conn = app.db_connect()
    try:
        report = app.maintain_history(conn, now_ts=NOW)
    finally:
        conn.close()
    check("orphan raw row is kept, not deleted",
          scalar("SELECT COUNT(*) FROM snapshots WHERE ts = ?", (app.epoch_to_iso(orphan),)) == 1)
    check("report flags the unrolled row", report["raw_kept_unrolled"] == 1,
          "%s" % report["raw_kept_unrolled"])

    # Rewind the frontier so that bucket gets rolled up: the row is then deletable.
    conn = app.db_connect()
    try:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'rollup_frontier'",
                     (str(orphan - 30 * DAY),))
        conn.commit()
        report = app.maintain_history(conn, now_ts=NOW)
    finally:
        conn.close()
    check("once rolled up, the orphan is pruned",
          scalar("SELECT COUNT(*) FROM snapshots WHERE ts = ?", (app.epoch_to_iso(orphan),)) == 0)
    check("report is clean afterwards", report["raw_kept_unrolled"] == 0)

    # --- /api/history: the merged view -----------------------------------------------
    since, until = NOW - 120 * DAY, NOW
    payload = app.history_payload(
        {"since": [app.epoch_to_iso(since)], "until": [app.epoch_to_iso(until)], "max_points": ["200"]},
        now_ts=NOW,
    )
    grid = payload["t"]
    check("payload reports both sources", payload["sources"]["resolution"] == "raw+rollup",
          payload["sources"]["resolution"])
    check("grid is capped by max_points", len(grid) <= 202, "%d points" % len(grid))
    check("grid is aligned and regular",
          grid[0] % payload["bucket_seconds"] == 0
          and all(grid[i + 1] - grid[i] == payload["bucket_seconds"] for i in range(len(grid) - 1)),
          "bucket %ss, %d points" % (payload["bucket_seconds"], len(grid)))

    series = {s["key"]: s for s in payload["series"]}
    check("series are keyed per account/window", set(series) == {"cmd-perso/monthly", "og-perso/weekly"},
          "%s" % sorted(series))
    cm = series["cmd-perso/monthly"]
    pos_recent = grid.index(bucket_of(NOW - 3600, payload["bucket_seconds"]))
    pos_old = grid.index(bucket_of(NOW - 100 * DAY, payload["bucket_seconds"]))
    check("recent day averages every sample", cm["avg"][pos_recent] == 64.5, "%s" % cm["avg"][pos_recent])
    check("old day averages the rollup", cm["avg"][pos_old] == 25.0, "%s" % cm["avg"][pos_old])
    check("rollup min/max survive the merge",
          cm["min"][pos_old] == 10.0 and cm["max"][pos_old] == 40.0,
          "%s..%s" % (cm["min"][pos_old], cm["max"][pos_old]))
    check("sample counts survive the merge",
          cm["n"][pos_recent] == 30 and cm["n"][pos_old] == 4,
          "%s / %s" % (cm["n"][pos_recent], cm["n"][pos_old]))
    check("no data is a null, not a zero", cm["avg"][pos_recent - 5] is None,
          "%s" % cm["avg"][pos_recent - 5])
    check("gaps are dense nulls", sum(1 for v in cm["avg"] if v is None) >= len(grid) - 5,
          "%d null(s) of %d" % (sum(1 for v in cm["avg"] if v is None), len(grid)))
    check("second series is independent", series["og-perso/weekly"]["avg"][pos_old] == 92.5,
          "%s" % series["og-perso/weekly"]["avg"][pos_old])

    filtered = app.history_payload({"hours": ["24"], "account_id": ["og-perso"]}, now_ts=NOW)
    check("account filter applies", [s["key"] for s in filtered["series"]] == [],
          "%s" % [s["key"] for s in filtered["series"]])
    filtered = app.history_payload({"hours": ["24"], "account_id": ["cmd-perso"]}, now_ts=NOW)
    check("account filter keeps the matching series",
          [s["key"] for s in filtered["series"]] == ["cmd-perso/monthly"],
          "%s" % [s["key"] for s in filtered["series"]])
    check("24h view is raw only", filtered["sources"]["resolution"] == "raw", filtered["sources"]["resolution"])

    bad = app.history_payload({"since": ["2026-01-02T00:00:00Z"], "until": ["2026-01-01T00:00:00Z"]},
                              now_ts=NOW)
    check("inverted range is refused", bad.get("error") == "until must be later than since", "%s" % bad)

    print()
    if failures:
        print("%d check(s) FAILED: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("all history checks passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(code)
