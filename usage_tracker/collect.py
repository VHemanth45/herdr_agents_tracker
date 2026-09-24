"""Collection runs: refresh limits for due profiles in parallel, then update local history.

A run holds refresh.lock, so concurrent status calls from several Herdr sessions never start
duplicate collectors. Each provider fails independently; its error is recorded next to the
last good snapshot instead of replacing it.
"""

import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import cache, history, model
from .model import ProviderError
from .providers import PROVIDERS

WORKERS = 4
LOG_LIMIT = 256 * 1024
SECRET = re.compile(r"(?i)(bearer\s+|token[\"'=:\s]+|key[\"'=:\s]+|sk-|xai-)[A-Za-z0-9._~+/=-]{8,}")


def redact(text):
    return SECRET.sub(lambda m: m.group(1) + "<redacted>", str(text))


def log(state_dir, message):
    path = Path(state_dir) / "collector.log"
    try:
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            path.replace(path.with_name("collector.log.1"))
        with open(path, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {redact(message)}\n")
    except OSError:
        pass


def fetch(profile, state_dir):
    try:
        return PROVIDERS[profile["provider"]].limits(profile, state_dir)
    except ProviderError as exc:
        return ProviderError(exc.state, redact(exc.message), exc.retry_after)
    except Exception as exc:  # an adapter bug must not take the other providers down
        log(state_dir, f"{profile['id']}: unexpected error\n{traceback.format_exc()}")
        return ProviderError("error", redact(f"{type(exc).__name__}: {exc}"))


def refresh(cfg, state_dir, force=False, with_history=True, providers=None):
    """Returns False when another collector already holds the lock. With providers, refreshes
    those providers' accounts now (an agent finished a turn), unless one is backing off after errors."""
    state_dir = Path(state_dir)
    with cache.lock(state_dir / "refresh.lock", mark_run=True) as held:
        if not held:
            return False
        now = time.time()
        profiles = [p for p in cfg["profiles"] if p["enabled"]]
        entries = cache.load(state_dir)
        if providers:
            due = [p for p in profiles if p["provider"] in providers and not (entries.get(p["id"]) or {}).get("failures")]
        else:
            due = [p for p in profiles if force or cache.due(entries.get(p["id"]), now)]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(lambda p: fetch(p, state_dir), due))
        for profile, result in zip(due, results):
            entry = cache.apply(entries.get(profile["id"]), result, now, cfg["refresh"]["interval_seconds"])
            entries[profile["id"]] = entry | {"provider": profile["provider"], "label": profile["label"]}
            if isinstance(result, ProviderError):
                log(state_dir, f"{profile['id']}: {result.state}: {result.message}")
            else:
                record_limits(profile, result, state_dir, now)
        known = {p["id"] for p in cfg["profiles"]}
        cache.save(state_dir, {k: v for k, v in entries.items() if k in known})
        if with_history:
            update_history(profiles, state_dir)
        return True


def record_limits(profile, result, state_dir, now):
    try:
        with history.Store(history.db_path(state_dir, profile["id"])) as store:
            store.record_limits(result.get("windows") or [], result.get("observed_at") or now)
    except Exception:
        log(state_dir, f"{profile['id']}: saving limits failed\n{traceback.format_exc()}")


def update_history(profiles, state_dir):
    for profile in profiles:
        adapter = PROVIDERS[profile["provider"]]
        if not hasattr(adapter, "history"):
            continue
        started = time.time()
        try:
            with history.Store(history.db_path(state_dir, profile["id"])) as store:
                adapter.history(profile, store)
                store.set_meta("scanned_at", time.time())
        except ProviderError as exc:
            log(state_dir, f"{profile['id']}: history: {exc.message}")
        except Exception:
            log(state_dir, f"{profile['id']}: history failed\n{traceback.format_exc()}")
        if time.time() - started > 10:
            log(state_dir, f"{profile['id']}: history scan took {time.time() - started:.0f}s")


def entries(cfg, state_dir, now):
    """Cached entries overlaid with cheap local reads (e.g. Claude's statusline snapshot),
    so the status line reflects them immediately instead of at the next collector run; each
    window gets its saved reading from a fifth of a window ago, for the pace forecast."""
    data = cache.load(state_dir)
    for profile in cfg["profiles"]:
        quick = getattr(PROVIDERS[profile["provider"]], "quick", None)
        if not profile["enabled"] or not quick:
            continue
        try:
            snap = quick(profile, state_dir)
        except Exception:
            snap = None
        entry = data.get(profile["id"]) or {}
        if snap and snap["observed_at"] > (entry.get("updated_at") or 0):
            # The snapshot updates the windows it has (never lowering a reading within one window);
            # others (e.g. Claude's Fable limit) stay.
            cached = {w["id"]: w for w in entry.get("windows") or []}
            windows = [model.higher(cached.pop(w["id"], None), w) for w in snap["windows"]]
            snap = snap | {"windows": windows + list(cached.values())}
            data[profile["id"]] = cache.apply(entry, snap, now, cfg["refresh"]["interval_seconds"]) | {
                "next_at": entry.get("next_at", 0)}  # an in-memory overlay must not postpone the real refresh
    for profile in cfg["profiles"]:
        entry = data.get(profile["id"])
        if profile["enabled"] and entry and entry.get("windows"):
            entry["windows"] = history.with_baselines(state_dir, profile["id"], entry["windows"], now)
    return data
