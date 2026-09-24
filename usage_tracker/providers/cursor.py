"""Cursor: the included usage of the current billing cycle, from Cursor's own dashboard service.

Uses the sign-in Cursor already stored in <dir>/state.vscdb (the editor), or the cursor-agent CLI's
auth.json when it keeps its sign-in in a file. The token is held in memory for one HTTPS request
and never refreshed, logged or written; the macOS Keychain is never read. No local history:
Cursor keeps no reliable token records on disk.
"""

import base64
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

from .. import model, web
from ..model import ProviderError

NAME = "Cursor"
DEFAULT_DIR = ("~/Library/Application Support/Cursor/User/globalStorage" if sys.platform == "darwin"
               else "~/.config/Cursor/User/globalStorage")
AUTO_ENABLE = False  # reads a stored sign-in token, so it runs only when a profile is configured explicitly
CAPABILITIES = ("limits: included usage of the billing cycle (total, Auto and API) from Cursor's dashboard "
                "service (uses Cursor's stored sign-in, never refreshed or persisted); history: none")
USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
CLI_AUTH = ("~/.config/cursor/auth.json", "~/.cursor/auth.json")  # cursor-agent with a file credential store


def detect(directory):
    return os.path.exists(os.path.join(directory, "state.vscdb"))


def stored(directory, key):
    """A value from Cursor's state.vscdb, read-only (immutable when Cursor's WAL files are gone)."""
    path = Path(directory) / "state.vscdb"
    if not path.exists():
        return None
    for mode in ("ro", "ro&immutable=1"):
        try:
            con = sqlite3.connect(f"file:{path}?mode={mode}", uri=True, timeout=5)
            try:
                row = con.execute("SELECT value FROM ItemTable WHERE key = ? LIMIT 1", (key,)).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        value = row[0] if row else None
        if isinstance(value, bytes):
            value = value.decode("utf-16-le" if b"\x00" in value else "utf-8", "ignore")
        return value.strip().strip('"') if isinstance(value, str) else None
    return None


def access_token(directory):
    token = stored(directory, "cursorAuth/accessToken")
    for path in CLI_AUTH if not token else ():
        try:
            token = json.loads(Path(os.path.expanduser(path)).read_text()).get("accessToken")
        except (OSError, ValueError, AttributeError):
            continue
        if token:
            break
    return token or None


def expiry(token):
    """The `exp` claim of a JWT (read, not verified), or None."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("exp")
    except (IndexError, ValueError, AttributeError):
        return None


def limits(profile, state_dir, opener=urllib.request.urlopen):
    token = access_token(profile["dir"])
    if not token:
        raise ProviderError("unavailable", "Not signed in to Cursor")
    exp = expiry(token)
    if isinstance(exp, (int, float)) and exp <= time.time() + 60:
        raise ProviderError("auth", "Cursor sign-in expired: open Cursor to refresh it")
    data = web.fetch_json("Cursor", USAGE_URL, {"Authorization": f"Bearer {token}", "Connect-Protocol-Version": "1"},
                          body={}, opener=opener)
    snap = parse_usage(data)
    plan = stored(profile["dir"], "cursorAuth/stripeMembershipType")
    return snap | {"plan": plan.replace("_", " ").title() if plan else None}


def parse_usage(data):
    start, end = _date(data.get("billingCycleStart")), _date(data.get("billingCycleEnd"))
    minutes = round((end - start) / 86400) * 1440 if start and end and end > start else None
    usage = data.get("planUsage") or {}
    total = _num(usage.get("totalPercentUsed"))
    if total is None and _num(usage.get("limit")):
        total = 100 * (_num(usage.get("totalSpend")) or 0) / _num(usage["limit"])
    if total is None:
        raise ProviderError("unavailable", "Cursor reported no included usage for this account")
    windows = [model.window_for(minutes, total, end)]
    for key, scope in (("autoPercentUsed", "Auto"), ("apiPercentUsed", "API")):
        if _num(usage.get(key)) is not None:
            windows.append(model.window_for(minutes, _num(usage[key]), end, scope))
    return {"windows": windows, "source": "Cursor dashboard service", "observed_at": time.time()}


def _num(value):
    """Numbers arrive as numbers or strings."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value):
    """Epoch-ms (as a number or a string) or ISO."""
    return model.iso_ts(_num(value) if _num(value) is not None else value)
