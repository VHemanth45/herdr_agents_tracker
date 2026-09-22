"""Claude Code / Anthropic: subscription limits and local transcript history.

Limits come from Claude Code itself; this plugin never reads credentials:
  1. `get_usage`, the request Claude Code's SDK uses, sent to a short-lived `claude -p`, which
     signs in on its own (5h, weekly and per-model weekly windows such as Fable);
  2. the statusline bridge: Claude Code passes `rate_limits` to statusLine commands, and
     `usage-tracker claude-statusline` saves them per profile before running the user's own
     statusline command (5h and weekly, updated while Claude Code is in use);
  3. `cachedUsageUtilization`, which older Claude Code versions stored in .claude.json.
History comes from transcripts in <dir>/projects/**/*.jsonl.
"""

import json
import os
import re
import shutil
import time
from pathlib import Path

from .. import model
from ..cache import read_json, write_json
from ..jsonlines import JsonLines
from ..model import ProviderError

NAME = "Claude"
ICON = "✻"  # Claude Code's own mark; a Nerd Font's Claude logo is "\uec82"
DEFAULT_DIR = "~/.claude"
CAPABILITIES = ("limits: 5h, weekly and per-model weekly windows (e.g. Fable) from Claude Code itself, "
                "or the statusline bridge; no credentials read; history: local transcripts")
USAGE_TIMEOUT = 20
# Weekly limits Claude Code's own usage view shows; others in the reply are internal buckets.
SHOWN_LIMITS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
PLANS = {"claude_pro": "Pro", "claude_max": "Max", "claude_team": "Team", "claude_enterprise": "Enterprise"}


def detect(directory):
    return os.path.isdir(os.path.join(directory, "projects"))


def claude_json(directory):
    """Claude Code keeps .claude.json inside CLAUDE_CONFIG_DIR; the default ~/.claude uses ~/.claude.json."""
    inside = Path(directory) / ".claude.json"
    if inside.exists() or Path(directory) != Path.home() / ".claude":
        return inside
    return Path.home() / ".claude.json"


def find_cli():
    home = Path.home()
    for candidate in (os.environ.get("CLAUDE_BIN"), shutil.which("claude"), home / ".local/bin/claude",
                      home / ".claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if candidate and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def snapshot_path(state_dir, profile_id):
    return Path(state_dir) / "claude" / f"{profile_id}.statusline.json"


def windows_from(raw):
    """Map Claude's {five_hour: {...}, seven_day: {...}, seven_day_opus: {...}} to windows."""
    windows = []
    for key, data in (raw or {}).items():
        if not isinstance(data, dict):
            continue
        if key == "five_hour":
            minutes, scope = 300, ""
        elif key.startswith("seven_day"):
            minutes, scope = 10080, key[len("seven_day_"):].replace("_", " ").title()
        else:
            continue
        # statusline input says used_percentage (epoch resets_at); the usage cache says
        # utilization (ISO resets_at). Both are percentages.
        used = data.get("used_percentage", data.get("utilization"))
        windows.append(model.window_for(minutes, used, model.iso_ts(data.get("resets_at")), scope))
    return [w for w in windows if w["used"] is not None]


def quick(profile, state_dir):
    snap = read_json(snapshot_path(state_dir, profile["id"]), {})
    if not snap.get("windows"):
        return None
    return {"windows": snap["windows"], "observed_at": snap.get("observed_at", 0),
            "source": "Claude Code statusline"}


def limits(profile, state_dir):
    account = read_json(claude_json(profile["dir"]), {})
    oauth = account.get("oauthAccount") or {}
    try:
        best = live_limits(profile, state_dir)
    except ProviderError as exc:
        # Claude Code could not answer (not installed, offline, ...): use what it last reported.
        found = [s for s in (quick(profile, state_dir), from_usage_cache(account)) if s]
        if not found:
            raise exc
        best = max(found, key=lambda s: s["observed_at"])
    best["plan"] = plan_label(oauth) or best.get("plan")
    return best


def live_limits(profile, state_dir):
    """Ask Claude Code for its plan limits with `get_usage`, the request its SDK sends.

    --safe-mode skips hooks, MCP servers and plugins, --no-session-persistence saves no session,
    and no prompt is sent, so no model tokens are used. Claude Code signs in on its own.
    Update checks and error reports are turned off; telemetry is not, because Claude Code then
    omits the per-model rows (e.g. Fable) from its answer (verified on 2.1.278).
    """
    cli = find_cli()
    if not cli:
        raise ProviderError("unavailable", "claude CLI not found")
    env = dict(os.environ, DISABLE_AUTOUPDATER="1", DISABLE_ERROR_REPORTING="1")
    env.pop("CLAUDE_CONFIG_DIR", None)
    if Path(profile["dir"]) != Path.home() / ".claude":
        env["CLAUDE_CONFIG_DIR"] = profile["dir"]
    cmd = [cli, "-p", "--safe-mode", "--no-session-persistence", "--input-format", "stream-json",
           "--output-format", "stream-json", "--verbose"]
    os.makedirs(state_dir, exist_ok=True)
    with JsonLines("claude", cmd, env, USAGE_TIMEOUT, cwd=state_dir) as claude:
        claude.send({"type": "control_request", "request_id": "usage",
                     "request": {"subtype": "get_usage", "skip_behaviors": True}})
        reply = {}
        while reply.get("request_id") != "usage":
            message = claude.read()
            reply = (message.get("response") or {}) if message.get("type") == "control_response" else {}
    if reply.get("subtype") != "success":
        text = str(reply.get("error"))[:200]
        signed_out = re.search(r"(?i)\b(auth|login|log in|sign in|401|unauthori[sz]ed)\b", text)
        raise ProviderError("auth" if signed_out else "error", f"claude: {text}")
    body = reply.get("response") or {}
    if not body.get("rate_limits_available"):
        raise ProviderError("unavailable", "No plan limits for this sign-in (API-key or cloud-provider "
                            "accounts have no subscription limits)")
    rate = body.get("rate_limits") or {}
    windows = windows_from({k: rate[k] for k in SHOWN_LIMITS if isinstance(rate.get(k), dict)})
    windows += [w for w in (model.window_for(10080, s.get("utilization"), model.iso_ts(s.get("resets_at")),
                                             s["display_name"])
                            for s in rate.get("model_scoped") or [] if isinstance(s, dict) and s.get("display_name"))
                if w["used"] is not None]
    if not windows:
        raise ProviderError("unavailable", "Claude Code reported no plan limits")
    plan = body.get("subscription_type")
    return {"windows": windows, "observed_at": time.time(), "source": "Claude Code",
            "plan": PLANS.get(f"claude_{plan}", str(plan).title()) if plan else None}


def from_usage_cache(account):
    cached = account.get("cachedUsageUtilization") or {}
    # Claude Code discards this cache when it belongs to another account; do the same.
    owner = cached.get("accountUuid")
    if not cached or (owner and owner != (account.get("oauthAccount") or {}).get("accountUuid")):
        return None
    windows = windows_from(cached.get("utilization"))
    if not windows:
        return None
    return {"windows": windows, "observed_at": (cached.get("fetchedAtMs") or 0) / 1000,
            "source": "Claude Code usage cache"}


def plan_label(oauth):
    kind = oauth.get("organizationType")
    if not kind:
        return None
    plan = PLANS.get(kind, kind)
    tier = re.search(r"(\d+x)$", oauth.get("userRateLimitTier") or oauth.get("organizationRateLimitTier") or "")
    return f"{plan} {tier.group(1)}" if tier else plan


def record_statusline(profile_id, payload, state_dir, now=None):
    """Save the rate limits from one statusline render; returns whether anything was saved.

    Usage within a window only grows until it resets, so a lower reading for the same reset
    time (e.g. an idle Claude process re-rendering an older value) never replaces a higher one.
    """
    windows = windows_from((payload or {}).get("rate_limits"))
    if not windows:
        return False
    path = snapshot_path(state_dir, profile_id)
    previous = {w["id"]: w for w in read_json(path, {}).get("windows", [])}
    windows = [model.higher(previous.get(w["id"]), w) for w in windows]
    write_json(path, {"observed_at": now or time.time(), "windows": windows})
    return True


def history(profile, store):
    for path in sorted((Path(profile["dir"]) / "projects").rglob("*.jsonl")):
        store.scan(path, parse_line)


def parse_line(line, ctx):
    if b'"usage"' not in line or b'"assistant"' not in line:
        return ()
    rec = json.loads(line)
    msg = rec.get("message") or {}
    usage = msg.get("usage") or {}
    if rec.get("type") != "assistant" or not usage or not msg.get("id") or msg.get("model") == "<synthetic>":
        return ()
    written = usage.get("cache_creation") or {}
    # One response is logged on several lines as it streams; the key merges them (history keeps
    # the largest counts), and it also dedupes responses copied into resumed sessions.
    return (model.event(
        f"{msg['id']}:{rec.get('requestId') or ''}", model.iso_ts(rec.get("timestamp")),
        rec.get("sessionId"), rec.get("cwd"), msg.get("model"), "anthropic",
        input=usage.get("input_tokens"), output=usage.get("output_tokens"),
        cache_read=usage.get("cache_read_input_tokens"),
        cache_write=usage.get("cache_creation_input_tokens"),
        cache_write_1h=written.get("ephemeral_1h_input_tokens"),
        reasoning=(usage.get("output_tokens_details") or {}).get("thinking_tokens")),)
