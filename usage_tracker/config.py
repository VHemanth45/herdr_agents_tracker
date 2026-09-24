"""Plugin directories, user configuration, and account profiles."""

import os
import re
import tomllib
from pathlib import Path

from . import PLUGIN_ID

DEFAULTS = {
    "status": {"order": [], "format": "compact", "window": "max", "max_width": 120, "bar": "blocks", "bar_width": 10},
    "refresh": {"interval_seconds": 300, "stale_seconds": 1800},
    "alerts": {"thresholds": [80, 95], "on_reset": True},
    "context": {"icon": "⛁", "share": True},
    "resume": {"enabled": False, "prompt": "continue"},
}
PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def config_dir():
    # Herdr exports these to plugin actions and panes. Tab-bar commands run without
    # them, so fall back to the locations Herdr assigns to plugins (verified on 0.8.2).
    if os.environ.get("HERDR_PLUGIN_CONFIG_DIR"):
        return Path(os.environ["HERDR_PLUGIN_CONFIG_DIR"])
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "herdr" / "plugins" / "config" / PLUGIN_ID


def state_dir():
    if os.environ.get("HERDR_PLUGIN_STATE_DIR"):
        return Path(os.environ["HERDR_PLUGIN_STATE_DIR"])
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "herdr" / "plugins" / PLUGIN_ID


def expand(path):
    return os.path.expanduser(os.path.expandvars(str(path)))


def load(path=None):
    """Read config.toml over the defaults. A broken file falls back to defaults and is reported."""
    path = Path(path or config_dir() / "config.toml")
    data, error = {}, None
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        error = f"{path}: {exc}"
    cfg = {name: {**defaults, **_table(data, name)} for name, defaults in DEFAULTS.items()}
    cfg["path"], cfg["error"] = str(path), error
    # A broken file shows as a config error rather than silently falling back to discovered accounts.
    cfg["profiles"], cfg["warnings"] = ([], []) if error else resolve_profiles(data.get("profiles"))
    return cfg


def _table(data, name):
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def resolve_profiles(raw):
    """Validate configured profiles, or discover default accounts when none are configured."""
    from .providers import PROVIDERS

    if raw is None:
        raw = [{"id": kind, "provider": kind} for kind, mod in PROVIDERS.items()
               if getattr(mod, "AUTO_ENABLE", True) and mod.detect(expand(mod.DEFAULT_DIR))]
    profiles, warnings, seen_ids, seen_dirs = [], [], set(), {}
    for item in raw if isinstance(raw, list) else []:
        item = item if isinstance(item, dict) else {}
        pid, kind = str(item.get("id", "")), item.get("provider")
        if kind not in PROVIDERS or not PROFILE_ID.match(pid) or pid in seen_ids:
            warnings.append(f"skipped profile id={pid!r} provider={kind!r}: needs a unique id "
                            f"(a-z, 0-9, _ or -) and a provider in {sorted(PROVIDERS)}")
            continue
        seen_ids.add(pid)
        mod = PROVIDERS[kind]
        directory = expand(item.get("dir") or mod.DEFAULT_DIR)
        profile = {"id": pid, "provider": kind, "label": str(item.get("label") or mod.NAME),
                   "dir": directory, "enabled": item.get("enabled", True) is not False,
                   "window": item.get("window"), "format": item.get("format"),
                   "icon": str(item.get("icon", getattr(mod, "ICON", "")))}
        # Two profiles on one directory are one account: keep the first so nothing is counted twice.
        key = (kind, os.path.realpath(directory))
        if key in seen_dirs:
            profile["enabled"] = False
            warnings.append(f"profile {pid} uses the same directory as {seen_dirs[key]}; "
                            "disabled to avoid double counting")
        else:
            seen_dirs[key] = pid
        profiles.append(profile)
    return profiles, warnings
