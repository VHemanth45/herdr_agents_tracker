"""Low-limit alerts: a Herdr notification when a limit passes one of alerts.thresholds (% used).

The status command checks after each tab-bar refresh. alerts.json keeps the highest threshold
announced for each limit until that limit resets, so each threshold is announced once per window.
When an announced limit reaches its reset time, a second notification says it has reset
(alerts.on_reset).
"""

import json
import subprocess
from pathlib import Path

from . import cache, fmt, integrate


def check(profiles, entries, cfg, now, state_dir, send=None, covered=()):
    """Announce limits that passed a threshold not yet announced this window; returns the
    (title, body) pairs sent. `send` returns False when the alert should be tried again later.
    `covered` (profile, window) resets were already announced with the agents waiting on them."""
    raw = cfg["alerts"]["thresholds"]
    thresholds = sorted(t for t in (raw if isinstance(raw, list) else []) if isinstance(t, (int, float)) and 0 < t <= 100)
    if not thresholds:
        return []
    state_dir, sent = Path(state_dir), []
    with cache.lock(state_dir / "alerts.lock") as held:  # one checker at a time across Herdr sessions
        if not held:
            return []
        path = state_dir / "alerts.json"
        seen = cache.read_json(path, {})
        before = dict(seen)
        enabled = {p["id"]: p for p in profiles if p["enabled"]}
        for key, last in list(seen.items()):
            pid, _, wid = key.partition("/")
            if not last.get("resets_at") or last["resets_at"] > now:
                continue
            if pid in enabled and (pid, wid) not in covered and cfg["alerts"].get("on_reset", True) is not False:
                note = reset_message(enabled[pid], wid, last, entries.get(pid) or {}, now)
                if not (send or notify)(*note):
                    continue  # tried again at the next check
                sent.append(note)
            del seen[key]
        for p in profiles:
            entry = entries.get(p["id"]) or {}
            if not p["enabled"] or now - (entry.get("updated_at") or 0) > cfg["refresh"]["stale_seconds"]:
                continue
            for w in fmt.current(entry.get("windows") or [], now):
                key, last = f"{p['id']}/{w['id']}", seen.get(f"{p['id']}/{w['id']}")
                if last and last.get("resets_at") and last["resets_at"] <= now:
                    continue  # its reset notification is still to be delivered
                if last and (w["used"] < last["level"] or abs((w.get("resets_at") or 0) - (last.get("resets_at") or 0)) > 3600):
                    last = None  # the limit has reset since
                level = max((t for t in thresholds if w["used"] >= t), default=None)
                if level and (not last or level > last["level"]):
                    note = message(p, w, now)
                    if (send or notify)(*note):
                        sent.append(note)
                        seen[key] = {"level": level, "resets_at": w.get("resets_at"), "label": w["label"]}
                elif not last:
                    seen.pop(key, None)
        if seen != before:
            cache.write_json(path, seen)
    return sent


def message(profile, w, now):
    """("Codex 7d limit at 88%", "Resets in 5d18h (Mon 11:25). At this rate 100% in 4h05m.")"""
    base, _, scope = w["label"].partition(" ")
    title = f"{profile['label']} {base} limit{f' ({scope})' if scope else ''} at {fmt.pct(w['used'])}"
    body = f"Resets in {fmt.duration(w['resets_at'] - now)} ({fmt.clock(w['resets_at'], now)})." if w.get("resets_at") else ""
    ahead = fmt.forecast(w, now)
    if ahead and ahead[1] is not None:
        body += f" At this rate 100% in {fmt.duration(ahead[1])}."
    return title, body.strip()


def reset_message(profile, wid, last, entry, now):
    """("Claude 5h limit has reset", "7d 41% used · Fable 3% used.")"""
    base, _, scope = str(last.get("label") or wid).partition(" ")
    title = f"{profile['label']} {base} limit{f' ({scope})' if scope else ''} has reset"
    others = [f"{w['label']} {fmt.pct(w['used'])} used" for w in fmt.current(entry.get("windows") or [], now)
              if w["id"] != wid]
    return title, (" · ".join(others) + ".") if others else ""


def notify(title, body):
    """Show a Herdr notification, delivered as Herdr's [ui.toast] setting says. Returns False when
    it should be tried again (Herdr busy or unreachable); when notifications are off it counts as done."""
    try:
        done = subprocess.run([integrate.herdr_bin(), "notification", "show", title, "--body", body,
                               "--sound", "request"], capture_output=True, text=True, timeout=3)
        result = json.loads(done.stdout or "{}").get("result") or {}
    except (OSError, ValueError, subprocess.TimeoutExpired, integrate.SetupError):
        return False
    return bool(result) and (result.get("shown") or result.get("reason") != "busy")
