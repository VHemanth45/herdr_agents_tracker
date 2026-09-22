"""Normalized usage data shared by every provider adapter.

Limit snapshot (one per account profile), as returned by an adapter's limits():
    {"windows": [window, ...], "plan": "Max 5x", "source": "codex app-server",
     "observed_at": 1790000000.0, "estimated": False}

Limit window (plain dict so snapshots serialize straight into the JSON cache):
    {"id": "weekly", "label": "7d", "used": 63.0, "resets_at": 1790574924, "minutes": 10080}
`used` is percent used (0-100) or None when unknown. Unknown is never shown as 0%.

Usage event (one API response recorded in local session history), see history.py:
    {"key", "ts", "session", "project", "model", "backend", <TOKEN_FIELDS>}
Token fields are normalized so they never overlap: `input` excludes cached input,
`cache_write_1h` is the 1-hour share of `cache_write`, and `reasoning` is the share of
`output` spent on reasoning.
"""

import math
import re
from datetime import datetime

TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write", "cache_write_1h", "reasoning")

# Well-known window lengths get stable ids so users can pick them in config.
NAMED_WINDOWS = {300: ("session", "5h"), 10080: ("weekly", "7d"),
                 43200: ("monthly", "30d"), 44640: ("monthly", "30d")}


class ProviderError(Exception):
    """A collection attempt failed.

    state: "auth" (sign-in needed), "unavailable" (no data source for this account),
    or "error" (transient failure). retry_after: seconds to wait before retrying.
    """

    def __init__(self, state, message, retry_after=None):
        super().__init__(message)
        self.state, self.message, self.retry_after = state, message, retry_after


def window(id, label, used, resets_at=None, minutes=None):
    used = float(used) if isinstance(used, (int, float)) and math.isfinite(used) else None
    if used is not None:
        used = min(100.0, max(0.0, used))
    resets_at = int(resets_at) if isinstance(resets_at, (int, float)) and resets_at > 0 else None
    return {"id": id, "label": label, "used": used, "resets_at": resets_at, "minutes": minutes}


def window_for(minutes, used, resets_at=None, scope=""):
    """Build a window named after its length, e.g. 10080 minutes -> id "weekly", label "7d"."""
    if minutes in NAMED_WINDOWS:
        id, label = NAMED_WINDOWS[minutes]
    elif minutes and minutes % 1440 == 0:
        id = label = f"{minutes // 1440}d"
    elif minutes and minutes % 60 == 0:
        id = label = f"{minutes // 60}h"
    else:
        id, label = "window", "limit"
    if scope:
        id, label = f"{id}_{slug(scope)}", f"{label} {scope}"
    return window(id, label, used, resets_at, minutes)


def higher(old, new, jitter=120):
    """Usage only grows within a window: of two readings for the same reset time (give or take
    `jitter` seconds), keep the higher one. Returns `new`, raised to `old` when that is higher."""
    if (old and old.get("used") is not None and new["used"] is not None and old.get("resets_at")
            and new.get("resets_at") and abs(old["resets_at"] - new["resets_at"]) <= jitter and old["used"] > new["used"]):
        return new | {"used": old["used"]}
    return new


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


def event(key, ts, session, project, model, backend, **tokens):
    ev = {"key": key, "ts": int(ts or 0), "session": session, "project": project,
          "model": model or "unknown", "backend": backend}
    for field in TOKEN_FIELDS:
        value = tokens.get(field) or 0
        ev[field] = int(value) if isinstance(value, (int, float)) and value > 0 else 0
    return ev


def iso_ts(value):
    """ISO-8601 or epoch (s/ms) timestamp -> epoch seconds (float), or None."""
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None
