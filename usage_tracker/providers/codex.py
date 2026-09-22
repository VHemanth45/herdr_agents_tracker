"""Codex / OpenAI: subscription limits via Codex's own app-server, history from rollout logs.

Limits use `codex app-server` (account/read + account/rateLimits/read), so Codex handles its
own sign-in; this plugin never reads auth.json. If the CLI cannot answer, the newest limit
snapshot recorded in a session log is used and labeled as such.
"""

import json
import os
import re
import shutil
import time
from pathlib import Path

from .. import VERSION, model
from ..jsonlines import JsonLines
from ..model import ProviderError

NAME = "Codex"
ICON = ">_"  # Codex's own mark; a Nerd Font's OpenAI logo is "\uec81"
DEFAULT_DIR = "~/.codex"
CAPABILITIES = ("limits: every window Codex reports (e.g. 5h, 7d, per-model buckets) via "
                "`codex app-server`, falling back to the last session-log snapshot; history: rollout logs")
PLANS = {"plus": "Plus", "pro": "Pro", "prolite": "Pro Lite", "team": "Team", "business": "Business",
         "enterprise": "Enterprise", "edu": "Edu", "free": "Free", "go": "Go",
         "self_serve_business_prolite": "Business Pro Lite"}
RPC_TIMEOUT = 20


def detect(directory):
    return os.path.isdir(os.path.join(directory, "sessions")) or os.path.exists(os.path.join(directory, "auth.json"))


def find_cli():
    home = Path.home()
    for candidate in (os.environ.get("CODEX_BIN"), shutil.which("codex"), home / ".local/bin/codex",
                      "/opt/homebrew/bin/codex", "/usr/local/bin/codex"):
        if candidate and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def plan_label(plan):
    return PLANS.get(plan, str(plan).replace("_", " ").title()) if plan else None


def windows_from(buckets, used, minutes, resets):
    """Rate-limit buckets {limit_id: {primary, secondary}} -> windows; key names differ by source."""
    windows = []
    for limit_id, snap in buckets.items():
        if not isinstance(snap, dict):
            continue
        scope = "" if limit_id in (None, "codex") else (snap.get("limitName") or snap.get("limit_name") or limit_id)
        for slot in ("primary", "secondary"):
            w = snap.get(slot)
            if isinstance(w, dict) and w.get(used) is not None:
                windows.append(model.window_for(w.get(minutes), w[used], w.get(resets), scope))
    return [w for w in windows if w["used"] is not None]


def limits(profile, state_dir):
    home = profile["dir"]
    if not os.path.isdir(home):
        raise ProviderError("unavailable", f"{home} not found")
    try:
        res = rpc(home, [("account/read", {}),
                         ("account/rateLimits/read", {"excludeResetCreditDetails": True})])
    except ProviderError as exc:
        fallback = rollout_limits(home) if exc.state != "auth" else None
        if fallback:
            return fallback
        raise
    account = res["account/read"].get("account")
    if not account:
        raise ProviderError("auth", "Not signed in to Codex (run `codex login`)")
    if account.get("type") != "chatgpt":
        raise ProviderError("unavailable", f"{account.get('type')} account: no subscription limits")
    rl = res["account/rateLimits/read"]
    buckets = rl.get("rateLimitsByLimitId") or {"codex": rl.get("rateLimits") or {}}
    windows = windows_from(buckets, "usedPercent", "windowDurationMins", "resetsAt")
    if not windows:
        raise ProviderError("unavailable", "Codex reported no rate-limit windows for this account")
    return {"windows": windows, "plan": plan_label(account.get("planType")),
            "source": "codex app-server", "observed_at": time.time()}


def rpc(home, calls, timeout=None):
    """Run JSON-RPC calls against a short-lived `codex app-server`; returns {method: result}."""
    cli = find_cli()
    if not cli:
        raise ProviderError("unavailable", "codex CLI not found")
    with JsonLines("codex app-server", [cli, "app-server"], dict(os.environ, CODEX_HOME=home),
                   timeout or RPC_TIMEOUT) as server:
        server.send({"id": 0, "method": "initialize",
                     "params": {"clientInfo": {"name": "usage-tracker", "version": VERSION}}})
        pending, results = {}, {}
        while len(results) < len(calls):
            message = server.read()
            if message.get("error") and (message.get("id") == 0 or message.get("id") in pending):
                raise rpc_error(message["error"])
            if message.get("id") == 0:
                server.send({"method": "initialized"})
                for i, (method, params) in enumerate(calls, 1):
                    pending[i] = method
                    server.send({"id": i, "method": method, "params": params})
            elif message.get("id") in pending:
                results[pending[message["id"]]] = message.get("result") or {}
        return results


def rpc_error(error):
    text = str(error.get("message") if isinstance(error, dict) else error)[:200]
    signed_out = re.search(r"(?i)\b(auth|login|log in|sign in|401|unauthori[sz]ed)\b", text)
    return ProviderError("auth" if signed_out else "error", f"codex: {text}")


def rollout_paths(home):
    root = Path(home)
    return list(root.glob("sessions/*/*/*/rollout-*.jsonl")) + list(root.glob("archived_sessions/*.jsonl"))


def rollout_limits(home, files=10):
    """Newest rate-limit snapshot Codex wrote to a session log (as fresh as that turn)."""
    newest = sorted(rollout_paths(home), key=lambda p: p.stat().st_mtime, reverse=True)[:files]
    for path in newest:
        for line in reversed(tail_lines(path)):
            if b'"rate_limits"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            payload = rec.get("payload") or {}
            rl = payload.get("rate_limits") or (payload.get("info") or {}).get("rate_limits")
            if payload.get("type") != "token_count" or not isinstance(rl, dict):
                continue
            windows = windows_from({rl.get("limit_id") or "codex": rl}, "used_percent", "window_minutes", "resets_at")
            if windows:
                return {"windows": windows, "plan": plan_label(rl.get("plan_type")),
                        "source": "Codex session log (last recorded turn)",
                        "observed_at": model.iso_ts(rec.get("timestamp")) or path.stat().st_mtime}
    return None


def tail_lines(path, size=512 * 1024):
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - size))
        data = f.read()
    return data.splitlines()[1:] if len(data) >= size else data.splitlines()


def history(profile, store):
    for path in sorted(rollout_paths(profile["dir"])):
        store.scan(path, parse_line)


def parse_line(line, ctx):
    if b'"token_' not in line and b'"session_meta"' not in line and b'"turn_context"' not in line:
        return ()
    rec = json.loads(line)
    kind, p = rec.get("type"), rec.get("payload") or {}
    if kind == "session_meta":
        ctx.update(session=p.get("id") or p.get("session_id"), project=p.get("cwd"),
                   backend=p.get("model_provider") or "openai")
        return ()
    if kind == "turn_context":
        ctx.update(model=p.get("model") or ctx.get("model"), project=p.get("cwd") or ctx.get("project"))
        return ()
    ts = model.iso_ts(rec.get("timestamp"))
    if kind == "token_usage_record" and p.get("response_id"):
        return (usage_event("r:" + p["response_id"], ts, p.get("session_id") or ctx.get("session"), ctx, p.get("usage")),)
    if kind == "event_msg" and p.get("type") == "token_count":
        total = (p.get("info") or {}).get("total_token_usage")
        if not isinstance(total, dict):
            return ()
        previous, ctx["total"] = ctx.get("total") or {}, total
        if (total.get("total_tokens") or 0) < (previous.get("total_tokens") or 0):
            previous = {}  # the counter restarted
        delta = {k: (total.get(k) or 0) - (previous.get(k) or 0) for k in total}
        if delta.get("total_tokens", 0) <= 0:
            return ()  # repeated snapshot
        # Older logs only carry cumulative totals. Keying on timestamp+total dedupes forked or
        # resumed logs that replay the same history; the "~" prefix marks a fallback that exact
        # per-response records of the same session supersede (see history.py).
        return (usage_event(f"~{rec.get('timestamp')}:{total.get('total_tokens')}", ts, ctx.get("session"), ctx, delta),)
    return ()


def usage_event(key, ts, session, ctx, usage):
    usage = usage or {}
    cached = usage.get("cached_input_tokens") or 0
    # OpenAI counts cached input inside input_tokens and reasoning inside output_tokens.
    return model.event(key, ts, session, ctx.get("project"), ctx.get("model"), ctx.get("backend") or "openai",
                       input=(usage.get("input_tokens") or 0) - cached, output=usage.get("output_tokens"),
                       cache_read=cached, cache_write=usage.get("cache_write_input_tokens"),
                       reasoning=usage.get("reasoning_output_tokens"))
