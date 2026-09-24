"""Local token history: one SQLite database per account profile, filled incrementally.

Log files are read from the last byte offset, and only complete lines are consumed, so a line
still being written is picked up on the next pass. Events are keyed by the provider's own
response/message id and merged by keeping the largest counts, so streamed duplicates, re-read
files (truncated or replaced), and responses copied into resumed sessions never double count.

Keys starting with "~" are fallback estimates (derived from cumulative totals); they are
ignored for any session that also has exact per-response records.

The limits table keeps each limit reading the collector takes, one row per unchanged run
(first and last time it was seen), for the recent-rate forecast and the dashboard's trend line.
"""

import json
import os
import sqlite3
from pathlib import Path

from .model import TOKEN_FIELDS

COLUMNS = ("key", "ts", "session", "project", "model", "backend", *TOKEN_FIELDS)
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, ts INTEGER NOT NULL, session TEXT,
  project TEXT, model TEXT, backend TEXT, {", ".join(f"{f} INTEGER" for f in TOKEN_FIELDS)});
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_session ON events(session);
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, inode INTEGER, offset INTEGER, ctx TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS limits (window TEXT NOT NULL, resets_at INTEGER NOT NULL, first INTEGER NOT NULL,
  last INTEGER NOT NULL, used REAL NOT NULL, PRIMARY KEY (window, first));
"""
SAME_WINDOW = 3600  # reset times this close apart are one window (providers round them differently)
UPSERT = (f"INSERT INTO events ({', '.join(COLUMNS)}) VALUES ({', '.join(':' + c for c in COLUMNS)}) "
          "ON CONFLICT(key) DO UPDATE SET ts = MIN(events.ts, excluded.ts), "
          + ", ".join(f"{f} = MAX(events.{f}, excluded.{f})" for f in TOKEN_FIELDS)
          + ", model = CASE WHEN events.model = 'unknown' THEN excluded.model ELSE events.model END")
EXACT_ONLY = ("NOT (key LIKE '~%' AND IFNULL(session, '') IN "
              "(SELECT IFNULL(session, '') FROM events WHERE key NOT LIKE '~%'))")


def db_path(state_dir, profile_id):
    return Path(state_dir) / "history" / f"{profile_id}.sqlite"


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")  # the dashboard reads while the collector writes
        self.db.executescript(SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.db.close()

    def add(self, events):
        with self.db:
            self.db.executemany(UPSERT, [e for e in events if e["ts"] > 0])

    def meta(self, key):
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

    def record_limits(self, windows, ts):
        """Save one reading of each limit; an unchanged reading extends the previous row."""
        ts = int(ts)
        with self.db:
            for w in windows:
                if w.get("used") is None or not w.get("resets_at"):
                    continue
                row = self.db.execute("SELECT first, resets_at, used, last FROM limits WHERE window = ? "
                                      "ORDER BY first DESC LIMIT 1", (w["id"],)).fetchone()
                if row and ts <= row[3]:
                    continue  # an older reading than the one already saved
                if row and abs(row[1] - w["resets_at"]) <= SAME_WINDOW and row[2] == w["used"]:
                    self.db.execute("UPDATE limits SET last = ? WHERE window = ? AND first = ?", (ts, w["id"], row[0]))
                else:
                    self.db.execute("INSERT INTO limits VALUES (?, ?, ?, ?, ?)",
                                    (w["id"], int(w["resets_at"]), ts, ts, w["used"]))

    def scan(self, path, parse):
        """Ingest the complete new lines of a JSONL file; parse(line_bytes, ctx) -> events.

        ctx is per-file parser state (e.g. current model) persisted with the byte offset.
        """
        try:
            st = os.stat(path)
        except OSError:
            return 0
        row = self.db.execute("SELECT inode, offset, ctx FROM files WHERE path = ?", (str(path),)).fetchone()
        if row and row[0] == st.st_ino and row[1] <= st.st_size:
            offset, ctx = row[1], json.loads(row[2] or "{}")
            if offset == st.st_size:
                return 0
        else:
            offset, ctx = 0, {}  # new, replaced or truncated file: re-read; keys prevent double counting
        events = []
        with open(path, "rb") as f:
            f.seek(offset)
            for line in f:
                if not line.endswith(b"\n"):
                    break  # still being written
                offset += len(line)
                try:
                    events.extend(parse(line, ctx))
                except (ValueError, TypeError, AttributeError, KeyError):
                    continue  # malformed or unexpected record
        with self.db:
            self.db.executemany(UPSERT, [e for e in events if e["ts"] > 0])  # undated records are unusable
            self.db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                            (str(path), st.st_ino, offset, json.dumps(ctx)))
        return len(events)


def rows(state_dir, profiles, since=0):
    """Aggregated usage per (day, session, project, model, backend) for each profile."""
    out = []
    sums = ", ".join(f"SUM({f})" for f in TOKEN_FIELDS)
    for profile in profiles:
        path = db_path(state_dir, profile["id"])
        if not path.exists():
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            query = (f"SELECT date(ts, 'unixepoch', 'localtime'), session, project, model, backend, "
                     f"{sums}, COUNT(*), MIN(ts), MAX(ts) FROM events "
                     f"WHERE ts >= ? AND {EXACT_ONLY} GROUP BY 1, 2, 3, 4, 5")
            for r in con.execute(query, (int(since),)):
                row = dict(zip(("day", "session", "project", "model", "backend"), r[:5]))
                row.update(zip(TOKEN_FIELDS, r[5:5 + len(TOKEN_FIELDS)]))
                row.update(zip(("requests", "first", "last"), r[5 + len(TOKEN_FIELDS):]))
                row.update(profile=profile["id"], provider=profile["provider"], label=profile["label"])
                out.append(row)
        except sqlite3.Error:
            continue  # being created by the first collector run
        finally:
            con.close()
    return out


def coverage(state_dir, profiles):
    """First/last recorded event and event count per profile (to label incomplete history)."""
    out = {}
    for profile in profiles:
        path = db_path(state_dir, profile["id"])
        if path.exists():
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                out[profile["id"]] = con.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM events").fetchone()
            except sqlite3.Error:
                pass
            finally:
                con.close()
    return out


def _query(state_dir, profile_id, query, args):
    path = db_path(state_dir, profile_id)
    if not path.exists():
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute(query, args).fetchall()
    except sqlite3.Error:
        return []  # created before the limits table existed, or being created by the first collector run
    finally:
        con.close()


def with_baselines(state_dir, profile_id, windows, now):
    """Windows with "base": [time, % used] a fifth of the window ago, when a saved reading from this
    window covers that time; fmt.forecast then uses the recent rate instead of the average."""
    out = []
    for w in windows:
        minutes, reset = w.get("minutes"), w.get("resets_at")
        if w.get("used") is not None and minutes and reset and reset > now:
            before = now - minutes * 12  # a fifth of the window, in seconds
            row = next(iter(_query(state_dir, profile_id, "SELECT last, used, resets_at FROM limits WHERE window = ? "
                                    "AND first <= ? ORDER BY first DESC LIMIT 1", (w["id"], int(before)))), None)
            if row and abs(row[2] - reset) <= SAME_WINDOW:
                w = w | {"base": [min(before, row[0]), row[1]]}
        out.append(w)
    return out


def limit_readings(state_dir, profile_id, window):
    """Saved readings of this window, oldest first: [(first, last, % used)]."""
    reset = window.get("resets_at") or 0
    return _query(state_dir, profile_id, "SELECT first, last, used FROM limits WHERE window = ? "
                   "AND ABS(resets_at - ?) <= ? ORDER BY first", (window["id"], int(reset), SAME_WINDOW))


def session_share(state_dir, profile_id, sessions, since):
    """(tokens of these sessions, tokens of the whole account) since `since`, cache reads left out."""
    marks = ", ".join("?" * len(sessions)) or "NULL"
    rows = _query(state_dir, profile_id,
                  f"SELECT SUM(CASE WHEN session IN ({marks}) THEN n END), SUM(n) FROM (SELECT session, "
                  f"IFNULL(input, 0) + IFNULL(cache_write, 0) + IFNULL(output, 0) AS n FROM events "
                  f"WHERE ts >= ? AND {EXACT_ONLY})", (*sessions, int(since)))
    return tuple(v or 0 for v in rows[0]) if rows else (0, 0)
