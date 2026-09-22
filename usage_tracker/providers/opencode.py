"""OpenCode: token history for every backend OpenCode records, and an estimated OpenCode Go allowance.

OpenCode Go limits are dollar amounts per model plan: 5-hour = 20%, weekly = 50%, monthly = 100%
of the monthly limit ($12 / $30 / $60 on the $60 plan, per opencode.ai/docs/go). The official
meter sits behind an opencode.ai web session, which this plugin does not read, so the allowance
here is an ESTIMATE: costs OpenCode recorded in the local opencode.db over trailing windows.
It misses usage from other machines and does not know OpenCode's exact window anchors.
"""

import json
import os
import sqlite3
import time
from pathlib import Path

from .. import model
from ..model import ProviderError

NAME = "OpenCode"
DEFAULT_DIR = "~/.local/share/opencode"
CAPABILITIES = ("limits: ESTIMATED OpenCode Go 5h/7d/30d spend vs published caps from local "
                "opencode.db (no official meter); history: token counts for every backend in opencode.db")
GO_CAPS = ((300, 12.0), (10080, 30.0), (43200, 60.0))  # (window minutes, USD cap)


def detect(directory):
    return os.path.exists(os.path.join(directory, "opencode.db"))


def connect(directory):
    path = Path(directory) / "opencode.db"
    if not path.exists():
        raise ProviderError("unavailable", f"{path} not found")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def limits(profile, state_dir):
    now = time.time()
    con = connect(profile["dir"])
    try:
        rows = con.execute(
            """SELECT COALESCE(json_extract(data, '$.time.created'), time_created), json_extract(data, '$.cost')
               FROM message WHERE time_created >= ? AND json_valid(data)
                 AND json_extract(data, '$.providerID') = 'opencode-go'
                 AND json_extract(data, '$.role') = 'assistant'""",
            ((now - 30 * 86400) * 1000,)).fetchall()
    except sqlite3.Error as exc:
        raise ProviderError("error", f"opencode.db: {exc}") from None
    finally:
        con.close()
    spend = [(created / 1000, cost) for created, cost in rows if isinstance(cost, (int, float)) and cost >= 0]
    if not spend:
        raise ProviderError("unavailable", "No OpenCode Go usage recorded locally in the last 30 days")
    windows = [model.window_for(minutes, 100 * sum(c for t, c in spend if t >= now - minutes * 60) / cap)
               for minutes, cap in GO_CAPS]
    return {"windows": windows, "plan": "Go", "estimated": True, "observed_at": now,
            "source": "estimate: local opencode.db costs over trailing windows vs Go caps"}


def history(profile, store):
    con = connect(profile["dir"])
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(message)")}
        stamp = "time_updated" if "time_updated" in columns else "time_created"
        cursor = int(store.meta("opencode_cursor") or 0)
        # >= re-reads the last millisecond; upserts make that harmless.
        rows = con.execute(f"SELECT id, {stamp}, data FROM message WHERE {stamp} >= ? ORDER BY {stamp}",
                           (cursor,)).fetchall()
    except sqlite3.Error as exc:
        raise ProviderError("error", f"opencode.db: {exc}") from None
    finally:
        con.close()
    events = []
    for message_id, updated, data in rows:
        cursor = max(cursor, updated or 0)
        try:
            d = json.loads(data)
        except (TypeError, ValueError):
            continue
        if not isinstance(d, dict) or d.get("role") != "assistant":
            continue
        tokens, where = d.get("tokens") or {}, d.get("path") or {}
        cache = tokens.get("cache") or {}
        reasoning = tokens.get("reasoning") or 0
        events.append(model.event(
            f"oc:{message_id}", model.iso_ts((d.get("time") or {}).get("created")), d.get("sessionID"),
            where.get("cwd") or where.get("root"), d.get("modelID"), d.get("providerID"),
            input=tokens.get("input"), output=(tokens.get("output") or 0) + reasoning, reasoning=reasoning,
            cache_read=cache.get("read"), cache_write=cache.get("write")))
    store.add(events)
    store.set_meta("opencode_cursor", cursor)
