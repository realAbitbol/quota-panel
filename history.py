#!/usr/bin/env python3
"""Usage history for quota-panel — an OPTIONAL layer, off unless it is asked for.

The default install keeps no state at all: no volume, no database, and `app.py` never
imports this module. History is switched on explicitly (`history.enabled` in
accounts.json, or QUOTA_HISTORY_ENABLED=1), and only then does anything here run:

  * `record()`  — one row per account/window per `sample_seconds`, then the rollup/prune
                  pass. Serialised by the poller, and never fatal to it.
  * `payload()` — the merged raw+rollup grid served by GET /api/history.

The shape is the one this repository already shipped and then removed (commit 4ae8e04),
kept because each part was measured, with the two defects that killed it addressed:

  * **Growth is bounded, and the rollup IS the record.** Measured on the removed layer: a
    60 s cadence over twelve series is ~6.3 M rows and ~350-400 MB a year. Raw samples are
    therefore kept `raw_days`, and older history survives as pre-aggregated buckets kept
    `rollup_days`. Buckets store n / sum / min / max, so re-aggregating a rollup is exact
    instead of an average of averages.
  * **A raw row is deleted only once its bucket exists in `rollups`.** A broken rollup then
    costs disk, never history, and the rows held back are reported rather than swallowed.
  * **No reading is invented.** A window the provider left unreadable is not stored, and a
    bucket with no data is served as `null`, so an outage breaks the line instead of being
    drawn across it. (The chart that died in 4ae8e04 died of a shared scale, not of data:
    OpenCode Go publishes whole percents while CommandCode reports hundredths, so one
    integer step flattened the other series. The page now scales each series on its own
    axis — this module serves `max` per bucket precisely so that it can.)
  * **A series names itself.** Labels are copied into `series` on every write, so a range
    older than the raw window still reads correctly without cross-referencing live state.

The timestamp format and the epoch helpers are local on purpose: the dependency runs one
way only (app.py imports this module lazily), so a broken history layer can never stop the
panel from serving.
"""
import json
import os
import sqlite3
import urllib.parse
from datetime import datetime, timezone

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

DEFAULT_PATH = os.path.join("/data", "quota.db")
DEFAULTS = {
    "enabled": False,
    "path": DEFAULT_PATH,
    "sample_seconds": 300,
    "raw_days": 90,
    "rollup_days": 365,
    "rollup_seconds": 900,
    # A crossing alert: post once when a window's percent crosses `alert_percent` from below.
    # `alert_url` empty means no webhook, which is the default — the layer stays self-contained.
    "alert_percent": 80,
    "alert_url": "",
    # Delivery is retried `alert_retries` times with an exponential backoff that starts at
    # `alert_backoff_seconds`. A webhook that is down costs a log line, never a poll.
    "alert_retries": 3,
    "alert_backoff_seconds": 1,
}
# sample_seconds bottoms out at 5 rather than 60 so the suite can exercise the sampling
# gate on a real boot in seconds; a value that low is a choice, not a mistake to block.
BOUNDS = {
    "sample_seconds": (5, 86400),
    "raw_days": (1, 3650),
    "rollup_days": (1, 3650),
    "rollup_seconds": (60, 86400),
    "alert_percent": (1, 100),
    "alert_retries": (0, 10),
    "alert_backoff_seconds": (1, 60),
}
ENV = {
    "enabled": "QUOTA_HISTORY_ENABLED",
    "path": "QUOTA_DB",                       # the name the removed layer documented
    "sample_seconds": "QUOTA_HISTORY_SAMPLE_SECONDS",
    "raw_days": "QUOTA_RETENTION_RAW_DAYS",
    "rollup_days": "QUOTA_RETENTION_ROLLUP_DAYS",
    "rollup_seconds": "QUOTA_ROLLUP_SECONDS",
    "alert_percent": "QUOTA_HISTORY_ALERT_PERCENT",
    "alert_url": "QUOTA_HISTORY_ALERT_URL",
    "alert_retries": "QUOTA_HISTORY_ALERT_RETRIES",
    "alert_backoff_seconds": "QUOTA_HISTORY_ALERT_BACKOFF_SECONDS",
}
TRUTHY = ("1", "true", "yes", "on")
FALSY = ("0", "false", "no", "off", "")
# Finest step whose point count fits the requested range. The 15-minute rollup grid is in
# the ladder, so a range that outlives the raw window still lands on stored buckets.
BUCKET_LADDER = (60, 300, 900, 1800, 3600, 10800, 21600, 43200, 86400, 172800, 604800)
# Homes for this file that lose data quietly: sqlite's locking does not hold over
# CIFS/NFS, so a network mount is refused rather than trusted.
NETWORK_MOUNT_PREFIXES = ("/mnt/", "/media/", "/Volumes/", "/net/")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  ts TEXT NOT NULL,          -- 'YYYY-MM-DDTHH:MM:SSZ': lexical order IS time order
  account_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  window_key TEXT NOT NULL,
  percent REAL NOT NULL,
  used REAL,
  cap REAL,
  resets_at TEXT
);
CREATE INDEX IF NOT EXISTS snapshots_ts ON snapshots(ts);
CREATE INDEX IF NOT EXISTS snapshots_series ON snapshots(account_id, window_key, ts);

CREATE TABLE IF NOT EXISTS rollups (
  bucket_ts TEXT NOT NULL,
  account_id TEXT NOT NULL,
  window_key TEXT NOT NULL,
  n INTEGER NOT NULL,
  pct_sum REAL NOT NULL,
  pct_min REAL NOT NULL,
  pct_max REAL NOT NULL,
  PRIMARY KEY (bucket_ts, account_id, window_key)
);
CREATE INDEX IF NOT EXISTS rollups_bucket ON rollups(bucket_ts);

CREATE TABLE IF NOT EXISTS series (
  account_id TEXT NOT NULL,
  window_key TEXT NOT NULL,
  account_label TEXT,
  provider TEXT,
  window_label TEXT,
  kind TEXT,                 -- 'window' (a percentage) or 'balance' (a money amount)
  first_ts TEXT,
  last_ts TEXT,
  PRIMARY KEY (account_id, window_key)
);

-- Money balances are their own record: `snapshots.percent` is NOT NULL and a balance has no
-- cap to divide by, so folding it in would mean either inventing a percentage or rebuilding
-- the table. A balance is stored as the provider reported it — an amount in a currency.
CREATE TABLE IF NOT EXISTS balances (
  ts TEXT NOT NULL,
  account_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  window_key TEXT NOT NULL,
  amount REAL NOT NULL,
  currency TEXT,
  resets_at TEXT
);
CREATE INDEX IF NOT EXISTS balances_ts ON balances(ts);
CREATE INDEX IF NOT EXISTS balances_series ON balances(account_id, window_key, ts);

CREATE TABLE IF NOT EXISTS balance_rollups (
  bucket_ts TEXT NOT NULL,
  account_id TEXT NOT NULL,
  window_key TEXT NOT NULL,
  n INTEGER NOT NULL,
  amt_sum REAL NOT NULL,
  amt_min REAL NOT NULL,
  amt_max REAL NOT NULL,
  currency TEXT,
  PRIMARY KEY (bucket_ts, account_id, window_key)
);
CREATE INDEX IF NOT EXISTS balance_rollups_bucket ON balance_rollups(bucket_ts);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  account_id TEXT NOT NULL,
  window_key TEXT NOT NULL,
  label TEXT,
  previous REAL,
  percent REAL,
  threshold INTEGER,
  status TEXT NOT NULL,          -- pending | delivered | failed | logged
  attempts INTEGER NOT NULL DEFAULT 0,
  delivered_at TEXT,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS alerts_status ON alerts(status);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class HistoryError(Exception):
    """Anything that makes the history layer unusable while it was asked for."""


# ----------------------------------------------------------------------------- config

def load_config(config_path=None, env=None):
    """Resolve the settings: defaults < environment < config file.

    Returns (config, notes). Every refused value lands in `notes` so the caller logs it
    once at startup: a typo must not sit silently in force. `enabled` is the only field
    that defaults to off, and it stays off unless something explicitly says otherwise.
    """
    env = os.environ if env is None else env
    notes = []
    config = dict(DEFAULTS)

    def as_enabled(raw, source):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int):
            return bool(raw)
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text in TRUTHY:
                return True
            if text in FALSY:
                return False
        notes.append("%s must be a boolean (true/false, 1/0) — ignored (%r)" % (source, raw))
        return None

    def as_int(raw, key, source):
        if isinstance(raw, bool) or raw is None:
            notes.append("%s must be an integer — ignored (%r)" % (source, raw))
            return None
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            notes.append("%s must be an integer — ignored (%r)" % (source, raw))
            return None
        low, high = BOUNDS[key]
        if not low <= value <= high:
            notes.append("%s out of range %d..%d — ignored (%d)" % (source, low, high, value))
            return None
        return value

    def apply(raw, source):
        if raw is None:
            return
        if not isinstance(raw, dict):
            notes.append("%s must be an object — ignored (%r)" % (source, raw))
            return
        for key in raw:
            # A leading underscore is this file's documentation convention (`_note`, both at the
            # top level and inside the history block): prose, not a setting. Reporting one as an
            # unknown setting would make the shipped example config log a warning on every start.
            if key.startswith("_"):
                continue
            if key not in DEFAULTS:
                notes.append("%s.%s is not a history setting — ignored" % (source, key))
        for key in DEFAULTS:
            if key not in raw:
                continue
            value = raw[key]
            if key == "enabled":
                parsed = as_enabled(value, "%s.enabled" % source)
            elif key == "path":
                parsed = value if isinstance(value, str) and value.strip() else None
                if parsed is None:
                    notes.append("%s.path must be a non-empty string — ignored" % source)
            elif key == "alert_url":
                # An empty URL is how the webhook is turned off, so "" is a value, not a typo.
                parsed = value.strip() if isinstance(value, str) else None
                if parsed is None:
                    notes.append("%s.alert_url must be a string — ignored" % source)
            else:
                parsed = as_int(value, key, "%s.%s" % (source, key))
            if parsed is not None:
                config[key] = parsed

    apply({key: env[var] for key, var in ENV.items() if env.get(var) not in (None, "")}, "environment")
    if config_path:
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                document = json.load(fh)
        except (OSError, ValueError):
            document = None
        if isinstance(document, dict) and "history" in document:
            apply(document.get("history"), "history")

    if config["rollup_days"] < config["raw_days"]:
        # A rollup window shorter than the raw one would drop the long-term record before
        # the rows it replaces even expire.
        notes.append(
            "rollup_days (%d) is shorter than raw_days (%d) — clamped up to raw_days"
            % (config["rollup_days"], config["raw_days"])
        )
        config["rollup_days"] = config["raw_days"]
    if config["rollup_seconds"] % 60:
        notes.append("rollup_seconds (%d) is not a whole number of minutes — ignored" % config["rollup_seconds"])
        config["rollup_seconds"] = DEFAULTS["rollup_seconds"]
    return config, notes


# ----------------------------------------------------------------------------- time

def epoch_to_iso(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime(TS_FORMAT)


def iso_to_epoch(text):
    if not isinstance(text, str):
        return None
    try:
        return int(datetime.strptime(text, TS_FORMAT).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def bucket_of(epoch, size):
    return (int(epoch) // int(size)) * int(size)


def now_epoch():
    return int(datetime.now(timezone.utc).timestamp())


# ----------------------------------------------------------------------------- store

def _journal_room(path):
    """Explain where a journal could not go, or return "" when the directory is fine.

    A hardened deployment (measured: `read_only: true` plus a single *file* bind-mounted at the
    database path) opens the file fine and then fails, because sqlite must create its journal and
    shared-memory file in the same directory — and in that shape the directory is the read-only
    image layer. sqlite's own wording ("unable to open database file") blames the database file,
    which is the one thing not at fault, so this says what is and what to mount instead.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    probe = os.path.join(directory, ".quota-history-journal-probe")
    try:
        os.unlink(probe)              # a probe left by a crash is not evidence of anything
    except OSError:
        pass
    try:
        handle = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return ""                     # racing another writer's probe means the directory is writable
    except OSError as exc:
        return (" — a write test in %s failed (%s), so sqlite cannot create its journal beside the "
                "database: mount a directory there rather than a single file, and let the "
                "container's own user own it (install -d -o 10001 -g 10001 <host dir>, then "
                "<host dir>:/data)" % (directory, exc))
    os.close(handle)
    try:
        os.unlink(probe)
    except OSError:
        pass
    return ""


def connect(path, read_only=False):
    """Open the database, creating the schema on first write.

    One connection per operation: the poller writes while HTTP handler threads read, and a
    sqlite3 connection is not shareable across threads by default.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if any(directory == prefix.rstrip("/") or directory.startswith(prefix)
           for prefix in NETWORK_MOUNT_PREFIXES):
        raise HistoryError(
            "history path %s sits under a network mount, where sqlite cannot lock safely "
            "(CIFS/NFS) — keep the database on a local volume" % path
        )
    if read_only and not os.path.exists(path):
        raise HistoryError("no history database at %s yet" % path)
    if not read_only:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise HistoryError("cannot create the history directory %s (%s)" % (directory, exc))
    try:
        if read_only:
            # A true read-only handle where the filesystem allows it. A WAL database needs a
            # writable -shm beside it, so this can legitimately fail; the fallback keeps the read
            # working and `query_only` keeps that fallback write-proof too.
            conn = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(os.path.abspath(path)),
                                   uri=True, timeout=5.0)
        else:
            conn = sqlite3.connect(path, timeout=5.0)
    except sqlite3.Error as exc:
        if not read_only:
            raise HistoryError("cannot open the history database at %s (%s)%s"
                               % (path, exc, _journal_room(path)))
        try:
            conn = sqlite3.connect(path, timeout=5.0)
        except sqlite3.Error as exc2:
            raise HistoryError("cannot open the history database at %s (%s)%s"
                               % (path, exc2, _journal_room(path)))
    conn.row_factory = sqlite3.Row
    if read_only:
        # `read_only=True` used to mean only "skip schema creation" — the connection could still
        # write. `mode=ro` above is the real thing when it opens; `query_only` makes the fallback
        # write-proof as well.
        try:
            conn.execute("PRAGMA query_only=ON")
        except sqlite3.Error:
            pass
    if not read_only:
        try:
            # WAL keeps a reader asking for a range off the writer's back.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            # A store created before `kind` existed keeps its old `series` shape: CREATE TABLE
            # IF NOT EXISTS never alters one. Add the column in place so an upgrade needs no
            # rebuild; a missing value reads as 'window'.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(series)")}
            if "kind" not in columns:
                conn.execute("ALTER TABLE series ADD COLUMN kind TEXT")
            balance_columns = {row[1] for row in conn.execute("PRAGMA table_info(balance_rollups)")}
            if "currency" not in balance_columns:
                conn.execute("ALTER TABLE balance_rollups ADD COLUMN currency TEXT")
            conn.commit()
        except sqlite3.Error as exc:
            conn.close()
            raise HistoryError("cannot prepare the history database at %s (%s)%s"
                               % (path, exc, _journal_room(path)))
    return conn


def prepare(config):
    """Create/open the store once at startup. Returns an error string, or None if usable.

    Called before the poller starts so a path the container cannot write is reported at
    boot instead of on the first sample, which would surface as a panel that simply never
    has any history.
    """
    try:
        connect(config["path"]).close()
    except (HistoryError, OSError) as exc:
        return str(exc)
    return None


def _meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# account_id -> epoch of its last stored sample. Process-local, seeded from the database
# the first time an account is seen, so a restart does not write a burst of samples.
_LAST_SAMPLE = {}


def _last_sample_epoch(conn, path, account_id):
    key = (path, account_id)
    if key not in _LAST_SAMPLE:
        # A balance-only account has no snapshots; reading the gate from snapshots alone would
        # re-sample it on every restart.
        newest = None
        for table in ("snapshots", "balances"):
            row = conn.execute("SELECT MAX(ts) FROM %s WHERE account_id = ?" % table,
                               (account_id,)).fetchone()
            epoch = iso_to_epoch(row[0]) if row and row[0] else None
            if epoch is not None and (newest is None or epoch > newest):
                newest = epoch
        _LAST_SAMPLE[key] = newest
    return _LAST_SAMPLE[key]


def record(results, config, now_ts=None):
    """Store one sample per account whose poll succeeded, then maintain the store.

    `results` is app.py's account list. Only `state == "ok"` accounts are stored, so an
    outage reads as a gap in the series instead of as a zero. A `window` is stored as a
    percentage; a `balance` is stored as the money amount the provider reported, in its own
    table, because a balance has no cap to divide by.

    A crossing of `alert_percent` (previous below, current at or above) is reported under
    `alerts`. Posting it is the caller's job, so this module never touches the network.
    """
    if not config.get("enabled"):
        return {"written": 0, "skipped": 0, "reason": "history is disabled"}
    now_ts = now_epoch() if now_ts is None else int(now_ts)
    path = config["path"]
    threshold = config.get("alert_percent")
    written = skipped = 0
    alerts = []
    conn = connect(path)
    try:
        for account in results:
            account_id = account.get("id")
            if account.get("state") != "ok" or not account_id:
                skipped += 1
                continue
            last = _last_sample_epoch(conn, path, account_id)
            if last is not None and now_ts - last < config["sample_seconds"]:
                skipped += 1
                continue
            stamp = epoch_to_iso(now_ts)
            rows = []
            balance_rows = []
            for window in account.get("windows") or []:
                window_key = window.get("key") or "window"
                if (window.get("kind") or "window") == "balance":
                    amount = _number(window.get("amount"))
                    if amount is None:
                        continue                      # unreadable is not zero
                    balance_rows.append(
                        (stamp, account_id, account.get("provider") or "", window_key, amount,
                         window.get("currency"), window.get("resets_at"))
                    )
                    continue
                percent = _number(window.get("percent"))
                if percent is None:
                    continue                          # unreadable is not zero
                if threshold is not None:
                    previous = _previous_percent(conn, account_id, window_key)
                    if previous is not None and previous < threshold <= percent:
                        label = "%s · %s" % (account.get("label") or account_id,
                                             window.get("label") or window_key)
                        cursor = conn.execute(
                            "INSERT INTO alerts (at, account_id, window_key, label, previous, "
                            "percent, threshold, status) VALUES (?,?,?,?,?,?,?, 'pending')",
                            (stamp, account_id, window_key, label, round(previous, 2),
                             round(percent, 2), threshold),
                        )
                        alerts.append({
                            "id": cursor.lastrowid,
                            "account_id": account_id,
                            "window_key": window_key,
                            "label": label,
                            "previous": round(previous, 2),
                            "percent": round(percent, 2),
                            "threshold": threshold,
                            "at": stamp,
                        })
                rows.append(
                    (
                        stamp,
                        account_id,
                        account.get("provider") or "",
                        window_key,
                        percent,
                        _number(window.get("used")),
                        _number(window.get("cap")),
                        window.get("resets_at"),
                    )
                )
            if not rows and not balance_rows:
                skipped += 1
                continue
            if rows:
                conn.executemany(
                    "INSERT INTO snapshots (ts, account_id, provider, window_key, percent, used, cap, "
                    "resets_at) VALUES (?,?,?,?,?,?,?,?)",
                    rows,
                )
            if balance_rows:
                conn.executemany(
                    "INSERT INTO balances (ts, account_id, provider, window_key, amount, currency, "
                    "resets_at) VALUES (?,?,?,?,?,?,?)",
                    balance_rows,
                )
            tracked = [(row, "window") for row in rows] + [(row, "balance") for row in balance_rows]
            for row, kind in tracked:
                conn.execute(
                    "INSERT OR REPLACE INTO series (account_id, window_key, account_label, provider, "
                    "window_label, kind, first_ts, last_ts) VALUES (?,?,?,?,?,?,"
                    "COALESCE((SELECT first_ts FROM series WHERE account_id = ? AND window_key = ?), ?), ?)",
                    (
                        account_id,
                        row[3],
                        account.get("label"),
                        account.get("provider"),
                        _window_label(account, row[3]),
                        kind,
                        account_id,
                        row[3],
                        row[0],
                        row[0],
                    ),
                )
            conn.commit()
            _LAST_SAMPLE[(path, account_id)] = now_ts
            written += 1
        report = maintain(conn, config, now_ts=now_ts)
    finally:
        conn.close()
    report.update({"written": written, "skipped": skipped, "alerts": alerts})
    return report


def _previous_percent(conn, account_id, window_key):
    """The newest stored percent for a series, or None when it has no history yet.

    A crossing is detected against the last reading, and the last reading is not always a raw
    row: once `raw_days` passes, only the rollup survives. Its bucket peak is the strongest
    stand-in — a peak already at or above the threshold means the crossing happened, and a peak
    below it is a last value below it.
    """
    row = conn.execute(
        "SELECT percent FROM snapshots WHERE account_id = ? AND window_key = ? "
        "ORDER BY ts DESC LIMIT 1",
        (account_id, window_key),
    ).fetchone()
    if row is not None:
        return row[0]
    row = conn.execute(
        "SELECT pct_max FROM rollups WHERE account_id = ? AND window_key = ? "
        "ORDER BY bucket_ts DESC LIMIT 1",
        (account_id, window_key),
    ).fetchone()
    return row[0] if row else None


def _window_label(account, window_key):
    for window in account.get("windows") or []:
        if (window.get("key") or "window") == window_key:
            return window.get("label")
    return None


def maintain(conn, config, now_ts=None):
    """Roll complete buckets up, prune what the rollups cover, then report.

    Ordered so no raw row is ever deleted before its bucket exists: the rollup pass runs
    first, and the prune only touches rows whose (account, window, bucket) is present. Money
    balances roll up the same way into their own tables, so a balance outlives the raw window.
    """
    now_ts = now_epoch() if now_ts is None else int(now_ts)
    rollup_seconds = config["rollup_seconds"]
    report = {
        "rollup_rows": 0,
        "raw_deleted": 0,
        "rollup_deleted": 0,
        "raw_kept_unrolled": 0,
        "balance_rollup_rows": 0,
        "balance_raw_deleted": 0,
        "balance_rollup_deleted": 0,
        "malformed": 0,
        "balance_kept_unrolled": 0,
        "alerts_deleted": 0,
    }

    frontier = iso_to_epoch(_meta_get(conn, "rollup_frontier"))
    if frontier is None:
        # The oldest row in either table: an install with only money balances has no snapshots,
        # and a frontier read from snapshots alone would never roll its balances up.
        oldest = []
        for table in ("snapshots", "balances"):
            row = conn.execute("SELECT MIN(ts) FROM %s" % table).fetchone()
            if row and row[0]:
                oldest.append(row[0])
        frontier = min(iso_to_epoch(stamp) for stamp in oldest) if oldest else None
    complete = bucket_of(now_ts, rollup_seconds)     # buckets strictly before this are closed
    if frontier is not None and frontier < complete:
        buckets = {}
        for row in conn.execute(
            "SELECT ts, account_id, window_key, percent FROM snapshots WHERE ts >= ? AND ts < ?",
            (epoch_to_iso(frontier), epoch_to_iso(complete)),
        ):
            epoch = iso_to_epoch(row["ts"])
            if epoch is None:
                report["malformed"] += 1             # a malformed stamp is not rolled up
                continue
            key = (bucket_of(epoch, rollup_seconds), row["account_id"], row["window_key"])
            entry = buckets.setdefault(key, [0, 0.0, None, None])
            entry[0] += 1
            entry[1] += row["percent"]
            entry[2] = row["percent"] if entry[2] is None else min(entry[2], row["percent"])
            entry[3] = row["percent"] if entry[3] is None else max(entry[3], row["percent"])
        for (bucket, account_id, window_key), (n, total, low, high) in buckets.items():
            conn.execute(
                "INSERT OR REPLACE INTO rollups (bucket_ts, account_id, window_key, n, pct_sum, "
                "pct_min, pct_max) VALUES (?,?,?,?,?,?,?)",
                (epoch_to_iso(bucket), account_id, window_key, n, total, low, high),
            )
        report["rollup_rows"] = len(buckets)

        balance_buckets = {}
        for row in conn.execute(
            "SELECT ts, account_id, window_key, amount, currency FROM balances "
            "WHERE ts >= ? AND ts < ?",
            (epoch_to_iso(frontier), epoch_to_iso(complete)),
        ):
            epoch = iso_to_epoch(row["ts"])
            if epoch is None:
                report["malformed"] += 1
                continue
            key = (bucket_of(epoch, rollup_seconds), row["account_id"], row["window_key"])
            entry = balance_buckets.setdefault(key, [0, 0.0, None, None, None])
            entry[0] += 1
            entry[1] += row["amount"]
            entry[2] = row["amount"] if entry[2] is None else min(entry[2], row["amount"])
            entry[3] = row["amount"] if entry[3] is None else max(entry[3], row["amount"])
            if row["currency"]:
                entry[4] = row["currency"]
        for (bucket, account_id, window_key), (n, total, low, high, currency) in balance_buckets.items():
            conn.execute(
                "INSERT OR REPLACE INTO balance_rollups (bucket_ts, account_id, window_key, n, "
                "amt_sum, amt_min, amt_max, currency) VALUES (?,?,?,?,?,?,?,?)",
                (epoch_to_iso(bucket), account_id, window_key, n, total, low, high, currency),
            )
        report["balance_rollup_rows"] = len(balance_buckets)

        _meta_set(conn, "rollup_frontier", epoch_to_iso(complete))
        conn.commit()

    # Pruning walks old rows, so it runs at the rollup cadence at most. The cutoff is the raw
    # window, but the unit of deletion is the ROLLUP BUCKET, not the row: a bucket that straddles
    # the cutoff must lose every raw row at once, or the survivors would shadow the bucket's
    # complete rollup in payload and the cell would undercount. The scan reaches one bucket past
    # the cutoff so it can see a straddling bucket's post-cutoff rows; the guard keeps rows in
    # buckets that have no rollup.
    last_prune = iso_to_epoch(_meta_get(conn, "pruned_at"))
    if last_prune is None or now_ts - last_prune >= rollup_seconds:
        cutoff = now_ts - int(config["raw_days"]) * 86400
        covered = {
            (row["account_id"], row["window_key"], row["bucket_ts"])
            for row in conn.execute(
                "SELECT account_id, window_key, bucket_ts FROM rollups WHERE bucket_ts < ?",
                (epoch_to_iso(cutoff),),
            )
        }
        doomed, kept = [], 0
        for row in conn.execute(
            "SELECT rowid, ts, account_id, window_key FROM snapshots WHERE ts < ?",
            (epoch_to_iso(cutoff + rollup_seconds),),
        ):
            epoch = iso_to_epoch(row["ts"])
            if epoch is None:
                kept += 1
                continue
            key = (row["account_id"], row["window_key"], epoch_to_iso(bucket_of(epoch, rollup_seconds)))
            if key in covered:
                doomed.append((row["rowid"],))
            else:
                kept += 1
        if doomed:
            conn.executemany("DELETE FROM snapshots WHERE rowid = ?", doomed)
        report["raw_deleted"] = len(doomed)
        report["raw_kept_unrolled"] = kept

        balance_covered = {
            (row["account_id"], row["window_key"], row["bucket_ts"])
            for row in conn.execute(
                "SELECT account_id, window_key, bucket_ts FROM balance_rollups WHERE bucket_ts < ?",
                (epoch_to_iso(cutoff),),
            )
        }
        balance_doomed = []
        balance_kept = 0
        for row in conn.execute(
            "SELECT rowid, ts, account_id, window_key FROM balances WHERE ts < ?",
            (epoch_to_iso(cutoff + rollup_seconds),),
        ):
            epoch = iso_to_epoch(row["ts"])
            if epoch is None:
                balance_kept += 1
                continue
            key = (row["account_id"], row["window_key"], epoch_to_iso(bucket_of(epoch, rollup_seconds)))
            if key in balance_covered:
                balance_doomed.append((row["rowid"],))
            else:
                balance_kept += 1
        if balance_doomed:
            conn.executemany("DELETE FROM balances WHERE rowid = ?", balance_doomed)
        report["balance_raw_deleted"] = len(balance_doomed)
        report["balance_kept_unrolled"] = balance_kept

        cursor = conn.execute("DELETE FROM rollups WHERE bucket_ts < ?", (epoch_to_iso(now_ts - int(config["rollup_days"]) * 86400),))
        report["rollup_deleted"] = max(0, cursor.rowcount or 0)
        balance_cursor = conn.execute("DELETE FROM balance_rollups WHERE bucket_ts < ?", (epoch_to_iso(now_ts - int(config["rollup_days"]) * 86400),))
        report["balance_rollup_deleted"] = max(0, balance_cursor.rowcount or 0)
        # The alert log rides the rollup retention: a crossing older than the store's own history
        # is not a record anyone can act on.
        alerts_cursor = conn.execute("DELETE FROM alerts WHERE at < ?", (epoch_to_iso(now_ts - int(config["rollup_days"]) * 86400),))
        report["alerts_deleted"] = max(0, alerts_cursor.rowcount or 0)
        _meta_set(conn, "pruned_at", now_ts)
        conn.commit()
    return report


# ----------------------------------------------------------------------------- read

def pick_bucket(span_seconds, max_points, floor=60):
    """The finest ladder step at or above `floor` whose point count fits the range.

    `floor` is the coarsest grid the store actually holds for this range — the sampling cadence
    while raw rows exist, the rollup cadence once they do not — so an automatic bucket never
    lands between two stored rows and leaves alternating empty cells.
    """
    for step in BUCKET_LADDER:
        if step < floor:
            continue
        if span_seconds <= step * max_points:
            return step
    return BUCKET_LADDER[-1]


def _int_param(params, name):
    """(value, valid). `valid` is False for a value that was given and could not be read.

    A malformed parameter is refused rather than quietly replaced by the default: a view
    that silently shows a different range than the one asked for is a lie about the data.
    """
    raw = (params.get(name) or [None])[0]
    if raw in (None, ""):
        return None, True
    try:
        return int(str(raw)), True
    except (TypeError, ValueError):
        return None, False


def resolve_range(params, now_ts):
    """(since, until) in epoch seconds, or (None, message) when the range is refused."""
    resolved = {}
    for name in ("since", "until"):
        raw = (params.get(name) or [None])[0]
        if raw in (None, ""):
            resolved[name] = None
            continue
        epoch = iso_to_epoch(raw)
        if epoch is None:
            return None, "%s must be an ISO stamp like 2026-09-22T04:00:00Z" % name
        resolved[name] = epoch
    until = resolved["until"] if resolved["until"] is not None else now_ts
    if resolved["since"] is not None:
        since = resolved["since"]
    else:
        hours, ok_hours = _int_param(params, "hours")
        days, ok_days = _int_param(params, "days")
        if not ok_hours or not ok_days:
            return None, "hours and days must be integers"
        if hours is not None and not 1 <= hours <= 24 * 365 * 5:
            return None, "hours must be between 1 and %d" % (24 * 365 * 5)
        if days is not None and not 1 <= days <= 365 * 5:
            return None, "days must be between 1 and %d" % (365 * 5)
        since = until - (days * 86400 if days is not None else (hours * 3600 if hours is not None else 86400))
    if until <= since:
        return None, "until must be later than since"
    return (since, until), None


def payload(params, config, now_ts=None):
    """The merged raw+rollup grid: one aligned time axis, one array per series per metric.

    A bucket with no data is `null` in every metric array, so a gap is drawn as a gap. A money
    balance is served as its own `kind` — `avg`/`min`/`max` are amounts, not percents — so a
    balance account is present in history without being turned into a percentage it never had.
    `resets` names every reset stamp seen in the range, for the chart's reset markers.
    """
    now_ts = now_epoch() if now_ts is None else int(now_ts)
    if not config.get("enabled"):
        return {"error": "history is not enabled"}
    resolved, message = resolve_range(params, now_ts)
    if resolved is None:
        return {"error": message}
    since, until = resolved
    max_points, ok = _int_param(params, "max_points")
    if not ok or (max_points is not None and not 10 <= max_points <= 2000):
        return {"error": "max_points must be between 10 and 2000"}
    max_points = max_points or 240
    bucket, ok = _int_param(params, "bucket_seconds")
    if not ok or (bucket is not None and not 60 <= bucket <= 7 * 86400):
        return {"error": "bucket_seconds must be between 60 and %d" % (7 * 86400)}
    rollup_seconds = config.get("rollup_seconds") or 900
    raw_span = int(config.get("raw_days", 0)) * 86400
    if bucket is None:
        floor = rollup_seconds if (until - since) > raw_span else config.get("sample_seconds", 60)
        bucket = pick_bucket(until - since, max_points, floor)
        # Once rollups are the only source, a ladder step that is not a whole number of rollup
        # buckets would put half a rollup in a cell and leave the rest empty.
        if (until - since) > raw_span and bucket % rollup_seconds:
            bucket = ((bucket // rollup_seconds) + 1) * rollup_seconds
    start = bucket_of(since, bucket)
    end = bucket_of(until, bucket)
    # Count first, build second: an explicit bucket can ask for millions of points (60 s over
    # five years is 2.6 M), and materializing that list before refusing it is a memory spike one
    # request can trigger. The auto ladder cannot land here, so this fires only on a named bucket.
    points = (end - start) // bucket + 1
    if points > max_points + 2:
        return {"error": "bucket_seconds=%d over this range is %d points, more than max_points=%d "
                         "— raise max_points or pick a coarser bucket" % (bucket, points, max_points)}
    grid = list(range(start, end + 1, bucket))
    wanted_accounts = [value for value in (params.get("account_id") or []) if value]
    wanted_series = [value for value in (params.get("series") or []) if value]

    conn = connect(config["path"], read_only=True)
    try:
        labels = {
            (row["account_id"], row["window_key"]): row for row in conn.execute("SELECT * FROM series")
        }
        position = {ts: index for index, ts in enumerate(grid)}
        series = {}
        # Raw buckets are read first, and a fine bucket that raw answered is marked so its
        # rollup is not counted a second time. Both sources still accumulate into the same
        # grid cell: at a coarse bucket, a range can hold recent raw rows AND older rollups,
        # and dropping either half is how a series grows a hole where history exists.
        totals = {}
        answered = set()
        balance_totals = {}
        balance_answered = set()
        currency = {}
        resets = {}

        def ensure(account_id, window_key, kind="window"):
            key = "%s/%s" % (account_id, window_key)
            if key not in series:
                meta = labels.get((account_id, window_key))
                series[key] = {
                    "key": key,
                    "account_id": account_id,
                    "window_key": window_key,
                    "kind": (meta["kind"] if meta and meta["kind"] else kind),
                    "account_label": (meta["account_label"] if meta else None) or account_id,
                    "window_label": (meta["window_label"] if meta else None) or window_key,
                    "provider": (meta["provider"] if meta else None) or "",
                    "currency": None,
                    "avg": [None] * len(grid),
                    "min": [None] * len(grid),
                    "max": [None] * len(grid),
                    "n": [0] * len(grid),
                }
            return series[key]

        def accumulate(key, index, n, total, low, high, store):
            entry = store.get((key, index))
            if entry is None:
                store[(key, index)] = [n, total, low, high]
            else:
                entry[0] += n
                entry[1] += total
                entry[2] = min(entry[2], low)
                entry[3] = max(entry[3], high)

        raw_rows = 0
        for row in conn.execute(
            "SELECT ts, account_id, window_key, percent, resets_at FROM snapshots "
            "WHERE ts >= ? AND ts < ?",
            (epoch_to_iso(start), epoch_to_iso(end + bucket)),
        ):
            epoch = iso_to_epoch(row["ts"])
            if epoch is None:
                continue
            index = position.get(bucket_of(epoch, bucket))
            if index is None:
                continue
            key = "%s/%s" % (row["account_id"], row["window_key"])
            accumulate(key, index, 1, row["percent"], row["percent"], row["percent"], totals)
            answered.add((key, bucket_of(epoch, rollup_seconds)))
            _add_reset(resets, key, row["resets_at"])
            raw_rows += 1

        rollup_rows = 0
        for row in conn.execute(
            "SELECT bucket_ts, account_id, window_key, n, pct_sum, pct_min, pct_max FROM rollups "
            "WHERE bucket_ts >= ? AND bucket_ts < ?",
            (epoch_to_iso(start), epoch_to_iso(end + bucket)),
        ):
            epoch = iso_to_epoch(row["bucket_ts"])
            if epoch is None:
                continue
            index = position.get(bucket_of(epoch, bucket))
            if index is None:
                continue
            key = "%s/%s" % (row["account_id"], row["window_key"])
            if (key, bucket_of(epoch, rollup_seconds)) in answered:
                continue                      # raw is the finer record of that same rollup bucket
            accumulate(key, index, row["n"], row["pct_sum"], row["pct_min"], row["pct_max"], totals)
            rollup_rows += 1

        balance_rows = 0
        for row in conn.execute(
            "SELECT ts, account_id, window_key, amount, currency, resets_at FROM balances "
            "WHERE ts >= ? AND ts < ?",
            (epoch_to_iso(start), epoch_to_iso(end + bucket)),
        ):
            epoch = iso_to_epoch(row["ts"])
            if epoch is None:
                continue
            index = position.get(bucket_of(epoch, bucket))
            if index is None:
                continue
            key = "%s/%s" % (row["account_id"], row["window_key"])
            accumulate(key, index, 1, row["amount"], row["amount"], row["amount"], balance_totals)
            balance_answered.add((key, bucket_of(epoch, rollup_seconds)))
            if row["currency"]:
                currency[key] = row["currency"]
            _add_reset(resets, key, row["resets_at"])
            balance_rows += 1

        balance_rollup_rows = 0
        for row in conn.execute(
            "SELECT bucket_ts, account_id, window_key, n, amt_sum, amt_min, amt_max, currency FROM "
            "balance_rollups WHERE bucket_ts >= ? AND bucket_ts < ?",
            (epoch_to_iso(start), epoch_to_iso(end + bucket)),
        ):
            epoch = iso_to_epoch(row["bucket_ts"])
            if epoch is None:
                continue
            index = position.get(bucket_of(epoch, bucket))
            if index is None:
                continue
            key = "%s/%s" % (row["account_id"], row["window_key"])
            if (key, bucket_of(epoch, rollup_seconds)) in balance_answered:
                continue
            if row["currency"]:
                currency[key] = row["currency"]
            accumulate(key, index, row["n"], row["amt_sum"], row["amt_min"], row["amt_max"], balance_totals)
            balance_rollup_rows += 1

        for (key, index), (n, total, low, high) in totals.items():
            account_id, window_key = key.split("/", 1)
            entry = ensure(account_id, window_key, "window")
            entry["n"][index] = n
            entry["avg"][index] = total / n if n else None
            entry["min"][index] = low
            entry["max"][index] = high

        for (key, index), (n, total, low, high) in balance_totals.items():
            account_id, window_key = key.split("/", 1)
            entry = ensure(account_id, window_key, "balance")
            entry["kind"] = "balance"
            entry["currency"] = currency.get(key)
            entry["n"][index] = n
            entry["avg"][index] = total / n if n else None
            entry["min"][index] = low
            entry["max"][index] = high
    finally:
        conn.close()

    kept = []
    for key in sorted(series):
        entry = series[key]
        if wanted_accounts and entry["account_id"] not in wanted_accounts:
            continue
        if wanted_series and key not in wanted_series:
            continue
        entry["samples"] = sum(entry["n"])
        weighted = [(point, count) for point, count in zip(entry["avg"], entry["n"]) if point is not None and count]
        entry["mean"] = (
            sum(point * count for point, count in weighted) / sum(count for _, count in weighted)
            if weighted else None
        )
        entry["peak"] = max((value for value in entry["max"] if value is not None), default=None)
        entry["latest"] = None
        for index in range(len(grid) - 1, -1, -1):
            if entry["avg"][index] is not None:
                entry["latest"] = {"percent": entry["avg"][index], "at": grid[index]}
                break
        kept.append(entry)

    # The summary describes the percentages: a money balance has no percent and must not pull
    # the average or the peak toward a number it never carried.
    percent_series = [entry for entry in kept if entry["kind"] != "balance"]
    weighted_all = [(entry["mean"], entry["samples"]) for entry in percent_series if entry["mean"] is not None and entry["samples"]]
    return {
        "now": epoch_to_iso(now_ts),
        "since": epoch_to_iso(since),
        "until": epoch_to_iso(until),
        "bucket_seconds": bucket,
        "alert_percent": config.get("alert_percent"),
        "t": grid,
        "series": kept,
        # Resets are read from raw rows only: neither rollup table carries `resets_at`, so a
        # reset older than the raw window is no longer named on the chart. Kept deliberately —
        # storing a reset flag in every rollup would roughly double that table for a marker.
        "resets": [
            {"key": key, "at": at}
            for key in sorted(resets)
            for at in sorted(resets[key])
        ],
        "summary": {
            "series": len(kept),
            "samples": sum(entry["samples"] for entry in kept),
            "mean_percent": (
                sum(mean * count for mean, count in weighted_all) / sum(count for _, count in weighted_all)
                if weighted_all else None
            ),
            "peak": _extreme(percent_series),
            "latest": _latest(percent_series),
            "span_seconds": until - since,
        },
        "sources": {
            "raw_rows": raw_rows,
            "rollup_rows": rollup_rows,
            "balance_rows": balance_rows,
            "balance_rollup_rows": balance_rollup_rows,
            "resolution": (
                "rollup" if (rollup_rows + balance_rollup_rows) and not (raw_rows + balance_rows)
                else ("raw+rollup" if (rollup_rows + balance_rollup_rows) else "raw")
            ),
        },
    }


def _add_reset(resets, key, resets_at):
    """Record a reset stamp under its series, ignoring the empty and malformed ones."""
    epoch = iso_to_epoch(resets_at)
    if epoch is None:
        return
    resets.setdefault(key, set()).add(epoch)


def _extreme(series):
    """The series holding the highest reading in the range, or None when there is none."""
    best = None
    for entry in series:
        if entry["peak"] is None:
            continue
        if best is None or entry["peak"] > best["percent"]:
            best = {"key": entry["key"], "label": _label(entry), "percent": entry["peak"]}
    return best


def _latest(series):
    """The highest of each series' own newest readings — never another window's number."""
    best = None
    for entry in series:
        if not entry["latest"]:
            continue
        latest = entry["latest"]["percent"]
        if best is None or latest > best["percent"]:
            best = {"key": entry["key"], "label": _label(entry), "percent": latest, "at": entry["latest"]["at"]}
    return best


def _label(entry):
    return "%s · %s" % (entry["account_label"], entry["window_label"])


def pending_alerts(config, limit=20):
    """Alerts whose delivery never completed, oldest first, so a restart retries them."""
    if not config.get("enabled"):
        return []
    try:
        conn = connect(config["path"], read_only=True)
    except (HistoryError, sqlite3.Error, OSError):
        return []
    try:
        rows = conn.execute(
            "SELECT id, at, account_id, window_key, label, previous, percent, threshold "
            "FROM alerts WHERE status = 'pending' ORDER BY id LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def mark_alert(config, alert_id, status, attempts=0, error=None, now_ts=None):
    """Record the outcome of one delivery. Best-effort: a failure here is not the caller's."""
    if not config.get("enabled") or alert_id is None:
        return
    now_ts = now_epoch() if now_ts is None else int(now_ts)
    try:
        conn = connect(config["path"])
        try:
            conn.execute(
                "UPDATE alerts SET status = ?, attempts = ?, delivered_at = ?, last_error = ? "
                "WHERE id = ?",
                (status, int(attempts),
                 epoch_to_iso(now_ts) if status == "delivered" else None,
                 (str(error)[:500] if error else None), int(alert_id)),
            )
            conn.commit()
        finally:
            conn.close()
    except (HistoryError, sqlite3.Error, OSError):
        pass


def status(config):
    """What /api/quota reports about history: off, on and usable, or on and broken.

    "Off" and "you asked for this and it broke" are different answers, and the page shows
    them differently — a silent fall-back to no history is how a feature looks absent.
    """
    if not config.get("enabled"):
        return {"enabled": False}
    info = {
        "enabled": True,
        "sample_seconds": config["sample_seconds"],
        "raw_days": config["raw_days"],
        "rollup_days": config["rollup_days"],
        "rollup_seconds": config["rollup_seconds"],
        "series": None,
        "newest": None,
        "error": None,
    }
    try:
        conn = connect(config["path"], read_only=True)
        try:
            info["series"] = conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]
            # ISO stamps sort lexically, so max-of-max across both raw tables is chronological.
            newest = None
            for table in ("snapshots", "balances"):
                row = conn.execute("SELECT MAX(ts) FROM %s" % table).fetchone()
                if row and row[0] and (newest is None or row[0] > newest):
                    newest = row[0]
            info["newest"] = newest
        finally:
            conn.close()
    except (HistoryError, sqlite3.Error, OSError) as exc:
        info["error"] = str(exc)
    return info
