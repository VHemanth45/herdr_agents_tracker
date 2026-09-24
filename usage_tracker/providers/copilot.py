"""GitHub Copilot CLI: monthly premium-request quota via the GitHub CLI, history from local session records.

Limits ask `gh api /copilot_internal/user`, so the GitHub CLI signs itself in and this plugin never
reads a token (the account is the one `gh` is signed in to). History comes from Copilot CLI's
<dir>/session-store.db, or for older versions the totals in <dir>/session-state/*/events.jsonl.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from .. import model
from ..model import ProviderError

NAME = "Copilot"
DEFAULT_DIR = "~/.copilot"
CAPABILITIES = ("limits: monthly premium requests (and chat/completions on Copilot Free) via "
                "`gh api /copilot_internal/user`, no token read; history: local session records")
MONTH = 43200
GH_TIMEOUT = 20
QUOTAS = (("premium_interactions", ""), ("chat", "Chat"), ("completions", "Completions"))


def detect(directory):
    return os.path.exists(os.path.join(directory, "session-store.db")) or \
        os.path.isdir(os.path.join(directory, "session-state"))


def find_gh():
    for candidate in (os.environ.get("GH_BIN"), shutil.which("gh"), "/opt/homebrew/bin/gh", "/usr/local/bin/gh",
                      "/usr/bin/gh"):
        if candidate and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def limits(profile, state_dir):
    gh = find_gh()
    if not gh:
        raise ProviderError("unavailable", "GitHub CLI (gh) not found: Copilot limits come from `gh api`")
    try:
        done = subprocess.run([gh, "api", "/copilot_internal/user", "-H", "X-Github-Api-Version: 2025-04-01"],
                              capture_output=True, text=True, timeout=GH_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ProviderError("error", "gh api timed out") from None
    except OSError as exc:
        raise ProviderError("error", f"gh api: {exc}") from None
    if done.returncode != 0:
        text = (done.stderr or done.stdout).strip()[:200]
        if re.search(r"(?i)auth login|not logged|401|bad credentials", text):
            raise ProviderError("auth", "GitHub CLI is not signed in (run `gh auth login`)")
        if "404" in text:
            raise ProviderError("unavailable", "No Copilot subscription on the account gh is signed in to")
        raise ProviderError("error", f"gh api: {text}")
    try:
        data = json.loads(done.stdout)
    except ValueError:
        raise ProviderError("error", "gh api returned no JSON") from None
    return parse_user(data)


def parse_user(data):
    reset = model.iso_ts(data.get("quota_reset_date_utc") or data.get("quota_reset_date")
                         or data.get("limited_user_reset_date"))
    snaps = data.get("quota_snapshots") or {}
    windows = []
    for key, scope in QUOTAS:
        snap = snaps.get(key) if isinstance(snaps.get(key), dict) else {}
        if snap.get("unlimited"):
            continue
        left = _num(snap.get("percent_remaining"))
        total, remaining = _num(snap.get("entitlement")), _num(snap.get("remaining"))
        if not snap:  # Copilot Free reports its monthly allowance separately
            total, remaining = _num((data.get("monthly_quotas") or {}).get(key)), \
                _num((data.get("limited_user_quotas") or {}).get(key))
        if left is None and total and remaining is not None:
            left = 100 * remaining / total
        if left is not None and (total or snap):
            windows.append(model.window_for(MONTH, round(100 - left, 2), reset, scope))
    if not windows:
        raise ProviderError("unavailable", "Copilot reported no limited quota for this account")
    plan = data.get("copilot_plan")
    return {"windows": windows, "plan": str(plan).replace("_", " ").title() if plan else None,
            "source": "gh api /copilot_internal/user", "observed_at": time.time()}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def history(profile, store):
    home = Path(profile["dir"])
    db = home / "session-store.db"
    if db.exists():
        session_store(db, store)
    for path in sorted(home.glob("session-state/*/events.jsonl")):
        store.scan(path, lambda line, ctx, session=path.parent.name: parse_line(line, ctx, session))


def session_store(path, store):
    cursor = int(store.meta("copilot_cursor") or 0)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            """SELECT e.id, e.created_at, e.session_id, s.cwd, COALESCE(e.copilot_usage_model, e.model),
                      e.input_tokens, e.output_tokens, e.cache_read_tokens, e.cache_write_tokens, e.reasoning_tokens
               FROM assistant_usage_events e LEFT JOIN sessions s ON s.id = e.session_id
               WHERE e.id > ? ORDER BY e.id""", (cursor,)).fetchall()
    except sqlite3.Error as exc:
        raise ProviderError("error", f"session-store.db: {exc}") from None
    finally:
        con.close()
    events = []
    for rid, created, session, cwd, name, inp, out, read, write, reasoning in rows:
        cursor = max(cursor, rid)
        read, write = read or 0, write or 0
        # Copilot counts cache reads and writes inside input_tokens.
        events.append(model.event(f"cp:{session}:{rid}", utc_ts(created), session, cwd, name, "github", input=max(0, (inp or 0) - read - write), output=out,
                                  cache_read=read, cache_write=write, reasoning=reasoning))
    store.add(events)
    store.set_meta("copilot_cursor", cursor)


def utc_ts(value):
    """session-store.db writes "2026-07-01 12:34:56" in UTC, without saying so."""
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\d[ T][\d:.]+", value):
        value += "+00:00"
    return model.iso_ts(value)


def parse_line(line, ctx, session):
    if b'"session.start"' not in line and b'"session.shutdown"' not in line:
        return ()
    rec = json.loads(line)
    data = rec.get("data") or {}
    if rec.get("type") == "session.start":
        ctx["project"] = (data.get("context") or {}).get("cwd")
        return ()
    if rec.get("type") != "session.shutdown":
        return ()
    # Totals so far for the whole session (they carry on across resumes), per model: one "~" event per
    # model, raised to the latest totals, and ignored when session-store.db has this session's turns.
    events = []
    for name, metrics in (data.get("modelMetrics") or {}).items():
        usage = (metrics or {}).get("usage") or {}
        read, write = usage.get("cacheReadTokens") or 0, usage.get("cacheWriteTokens") or 0
        events.append(model.event(f"~cp:{session}:{name}", model.iso_ts(rec.get("timestamp")), session,
                                  ctx.get("project"), name, "github",
                                  input=max(0, (usage.get("inputTokens") or 0) - read - write),
                                  output=usage.get("outputTokens"), cache_read=read, cache_write=write,
                                  reasoning=usage.get("reasoningTokens")))
    return events
