"""Amp (ampcode.com): Amp Free and subscription usage from `amp usage`, history from local thread files.

`amp usage` prints the balance Amp's server writes for the signed-in account, so Amp handles its
own sign-in; this plugin never reads secrets.json. History comes from <dir>/threads/T-*.json, which
older Amp versions keep on disk (newer ones keep threads on the server, so it may be empty).
"""

import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import model
from ..model import ProviderError

NAME = "Amp"
DEFAULT_DIR = (os.environ.get("XDG_DATA_HOME") or "~/.local/share") + "/amp"
CAPABILITIES = ("limits: Amp Free (daily) and subscription usage from `amp usage`, no token read; "
                "history: local thread files (older Amp versions)")
CLI_TIMEOUT = 20
AMT = r"\$?([0-9][0-9,]*(?:\.[0-9]+)?)"
FREE_PCT = re.compile(r"(?im)^\s*Amp Free:\s*([0-9.]+)\s*%\s+remaining")
FREE_USD = re.compile(rf"(?im)^\s*Amp Free:\s*{AMT}\s*/\s*{AMT}\s+remaining")
SUBSCRIPTION = re.compile(r"(?im)^\s*(?:Subscription\s+(.+?)|Amp\s+(.+?)\s+Subscription):\s*([0-9.]+)\s*%\s+other usage")
TIER = re.compile(rf"(?im)^\s*Amp\s+(.+?)\s+Tier:\s*agent usage\s*{AMT}\s+of\s+{AMT}\s+remaining"
                  r"(?:.*?period\s+(\d{4}-\d\d-\d\d)\s+to\s+(\d{4}-\d\d-\d\d))?")
SIGNED_OUT = re.compile(r"(?i)not (?:logged|signed) in|amp login|auth-required|unauthori[sz]ed")


def detect(directory):
    return os.path.exists(os.path.join(directory, "secrets.json")) or os.path.isdir(os.path.join(directory, "threads"))


def find_cli():
    home = Path.home()
    for candidate in (os.environ.get("AMP_BIN"), shutil.which("amp"), home / ".local/bin/amp", home / ".amp/bin/amp",
                      "/opt/homebrew/bin/amp", "/usr/local/bin/amp"):
        if candidate and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def limits(profile, state_dir):
    cli = find_cli()
    if not cli:
        raise ProviderError("unavailable", "amp CLI not found")
    # Amp keeps its sign-in in $XDG_DATA_HOME/amp, so a profile's dir selects its account.
    env = dict(os.environ, NO_COLOR="1", XDG_DATA_HOME=str(Path(profile["dir"]).parent))
    try:
        done = subprocess.run([cli, "usage"], capture_output=True, text=True, timeout=CLI_TIMEOUT,
                              stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        raise ProviderError("error", "amp usage timed out") from None
    except OSError as exc:
        raise ProviderError("error", f"amp usage: {exc}") from None
    text = done.stdout.strip() or done.stderr.strip()
    if SIGNED_OUT.search(text):
        raise ProviderError("auth", "Not signed in to Amp (run `amp login`)")
    if done.returncode != 0:
        raise ProviderError("error", f"amp usage: {text[:200]}")
    return parse_usage(text)


def parse_usage(text, now=None):
    """The balance text Amp's server writes (plain, or markdown with ANSI when on a terminal)."""
    text = re.sub(r"\x1b\[[0-9;]*m|\*\*|`", "", text)
    now = now or time.time()
    windows, plan = [], None
    if m := FREE_PCT.search(text):
        windows.append(model.window_for(1440, 100 - float(m[1]), next_free_reset(now)))
        plan = "Free"
    elif m := FREE_USD.search(text):  # older Amp Free: a balance that replenishes hourly
        left, total = _amount(m[1]), _amount(m[2])
        if total:
            windows.append(model.window("free", "Free", 100 - 100 * left / total))
            plan = "Free"
    if m := TIER.search(text):
        left, total = _amount(m[2]), _amount(m[3])
        start, end = (model.iso_ts(m[i] + "T00:00:00") if m[i] else None for i in (4, 5))
        minutes = round((end - start) / 86400) * 1440 if start and end else None
        if total:
            windows.insert(0, model.window_for(minutes, 100 - 100 * left / total, end))
            plan = m[1]
    elif m := SUBSCRIPTION.search(text):  # renews "in 1 month": too coarse for a reset time
        windows.insert(0, model.window("subscription", "plan", 100 - float(m[3])))
        plan = m[1] or m[2]
    if not windows:
        raise ProviderError("unavailable", "amp usage showed no Amp Free or subscription usage "
                            "(credits only)" if "credits" in text.lower() else "amp usage showed no usage limits")
    return {"windows": windows, "plan": plan, "source": "amp usage", "observed_at": now}


def next_free_reset(now):
    """Amp Free's daily allowance resets at 8 pm New York time."""
    try:
        here = datetime.fromtimestamp(now, ZoneInfo("America/New_York"))
    except (KeyError, ValueError, OSError):
        return None  # no time zone data on this machine
    reset = here.replace(hour=20, minute=0, second=0, microsecond=0)
    return (reset if reset > here else reset + timedelta(days=1)).timestamp()


def _amount(text):
    return float(text.replace(",", ""))


def history(profile, store):
    for path in sorted(Path(profile["dir"]).glob("threads/T-*.json")):
        store.scan_json(path, thread_events)


def thread_events(thread):
    tid, created = thread.get("id"), model.iso_ts(thread.get("created")) or 0
    trees = ((thread.get("env") or {}).get("initial") or {}).get("trees") or [{}]
    project = str(trees[0].get("uri") or "").removeprefix("file://") or None
    for msg in thread.get("messages") or []:
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if msg.get("role") != "assistant" or not isinstance(usage, dict):
            continue
        # inputTokens excludes the cache; totalInputTokens is the three together.
        yield model.event(f"amp:{tid}:{msg.get('messageId')}",
                          model.iso_ts(usage.get("timestamp")) or created + (msg.get("messageId") or 0),
                          tid, project, usage.get("model"), "amp", input=usage.get("inputTokens"),
                          output=usage.get("outputTokens"), cache_read=usage.get("cacheReadInputTokens"),
                          cache_write=usage.get("cacheCreationInputTokens"))
