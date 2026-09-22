"""Snapshot cache and refresh coordination.

cache.json holds one entry per account profile. Freshness (`updated_at`: when the data was
observed) and the last attempt (`state`, `error`, `checked_at`) are recorded separately, so a
failed refresh keeps the last good snapshot visible, marked stale once it ages.
Only the collector writes, while holding refresh.lock; readers never block.
"""

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .model import ProviderError

MAX_BACKOFF = 3600
SPAWN_DEBOUNCE = 60  # at most one collector start per minute, across all Herdr sessions
TURN_DEBOUNCE = 60  # at most one refresh per provider and minute after agents finish turns


def read_json(path, default):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def write_json(path, data, indent=1):
    """Atomic write: readers see the old file or the new one, never a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)  # keep the file's permissions (mkstemp uses 0600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def load(state_dir):
    return read_json(Path(state_dir) / "cache.json", {}).get("profiles", {})


def save(state_dir, entries):
    write_json(Path(state_dir) / "cache.json", {"version": 1, "profiles": entries})


@contextlib.contextmanager
def lock(path, mark_run=False):
    """Non-blocking exclusive lock; yields False when another process holds it.

    The kernel drops a flock when its holder exits, so a crashed collector never wedges refreshes.
    mark_run stamps the lock file so request_refresh can debounce collector starts.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        if mark_run:
            os.utime(path)
        yield True


def locked(path):
    with lock(path) as held:
        return not held


def apply(entry, result, now, interval):
    """Merge one collection attempt into a profile's entry; failures keep the last good windows."""
    entry = dict(entry or {})
    entry["checked_at"] = now
    if isinstance(result, ProviderError):
        failures = entry.get("failures", 0) + 1
        delay = result.retry_after or min(interval * 2 ** failures, MAX_BACKOFF)
        entry.update(state=result.state, error=result.message, failures=failures, next_at=now + delay)
    else:
        entry.update(result)
        entry.update(state="ok", error=None, failures=0, next_at=now + interval,
                     updated_at=result.get("observed_at") or now)
    return entry


def due(entry, now):
    return not entry or entry.get("next_at", 0) <= now


def request_refresh(state_dir, entries, profiles, now):
    """Start a background collector if any profile is due and none is running or just started."""
    lock_path = Path(state_dir) / "refresh.lock"
    if not any(due(entries.get(p["id"]), now) for p in profiles):
        return False
    with contextlib.suppress(FileNotFoundError):
        if now - lock_path.stat().st_mtime < SPAWN_DEBOUNCE:
            return False
    if locked(lock_path):
        return False
    spawn("refresh")
    return True


def refresh_after_turn(state_dir, provider, now):
    """Start a background refresh of one provider after its agent finished a turn; returns whether
    one started (not when that provider was refreshed this way within TURN_DEBOUNCE, or a
    collector is running)."""
    stamp = Path(state_dir) / f"turn-{provider}.stamp"
    with contextlib.suppress(FileNotFoundError):
        if now - stamp.stat().st_mtime < TURN_DEBOUNCE:
            return False
    if locked(Path(state_dir) / "refresh.lock"):
        return False
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    spawn("refresh", "--provider", provider, "--no-history")
    return True


def spawn(*args):
    """Run `usage-tracker <args>` detached, so it outlives a short-lived caller."""
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ, PYTHONPATH=str(root))
    return subprocess.Popen([sys.executable, "-P", "-m", "usage_tracker", *args], cwd=root, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def age(entry, now):
    return now - (entry.get("updated_at") or 0) if entry else None
