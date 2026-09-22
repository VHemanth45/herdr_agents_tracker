"""Agents: a context meter per agent in Herdr's Agents panel, and fresh limits after each turn.

The meter ("⛁ 28% 72k": share of the context window in use, and its tokens) is a display-only
pane token that setup adds to [ui.sidebar.agents] rows; one of integrate.CONTEXT_TOKENS is set per
pane, picked by level so the meter takes that color. Claude Code panes are updated by the
statusline bridge, which runs inside the pane (HERDR_PANE_ID) and receives context_window, and
otherwise from the transcript of the session their claude process records in sessions/<pid>.json.
Codex panes are updated from the session file their codex process holds open (its main thread;
subagents have files of their own). Both also when their agent finishes a turn.
"""

import re

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import PLUGIN_ID, cache, fmt, integrate
from .providers import claude

BRIDGE_FRESH = 120  # seconds: a statusline reading this recent belongs to the turn that just ended
RESEND = 600  # re-send an unchanged meter after this long: Herdr drops pane tokens when it restarts
CLAUDE_WINDOW = 200_000  # Claude's usual context window; a session past it has the 1M window
SESSION_ID = re.compile(r"^[0-9a-f-]{36}$")
TAIL = 256 * 1024  # bytes read from the end of a Codex session file to find its latest token count


def meter(pct, tokens, icon):
    """"⛁ 28% 72k"; without a known window size just the tokens, "⛁ 118k"."""
    size = f"{tokens / 1e6:.1f}M" if tokens >= 1e6 else f"{tokens / 1e3:.0f}k" if tokens >= 1e3 else str(tokens)
    return f"{fmt.icon_text(icon) or 'ctx'} " + (f"{pct:.0f}% " if pct is not None else "") + size


def show(pane_id, pct, tokens, state_dir, icon, now=None, window=None):
    """Put the meter on the pane when it changed, or was last sent long ago; returns the text sent.
    `window` (a context size seen by the statusline bridge) is remembered for the pane."""
    now, text = now or time.time(), meter(pct, tokens, icon)
    path = Path(state_dir) / "context.json"
    shown = {pane: v for pane, v in cache.read_json(path, {}).items() if now - v[1] < 86400}  # forget closed panes
    last = shown.get(pane_id) or ["", 0, None]
    window = window or (last[2] if len(last) > 2 else None)
    if last[0] == text and now - last[1] < RESEND:
        return None
    shown[pane_id] = [text, now, window]
    cache.write_json(path, shown)
    level = fmt.level(pct) if pct is not None else 0
    args = [a for i, token in enumerate(integrate.CONTEXT_TOKENS)
            for a in (("--token", f"{token}={text}") if i == level else ("--clear-token", token))]
    subprocess.Popen([integrate.herdr_bin(), "pane", "report-metadata", pane_id, "--source", PLUGIN_ID, *args],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)  # never keep Claude's statusline waiting
    return text


def from_statusline(payload):
    """(% of the context window used, tokens) from Claude Code's statusline input, or None
    before the session's first response."""
    window = (payload or {}).get("context_window") or {}
    pct = window.get("used_percentage")
    return (float(pct), int(window.get("total_input_tokens") or 0)) if isinstance(pct, (int, float)) else None


def pids(pane_id, name):
    """Process ids of the pane's foreground processes whose command line mentions `name`."""
    code, out, _ = integrate.herdr("pane", "process-info", "--pane", pane_id)
    info = json.loads(out or "{}").get("result", {}).get("process_info", {}) if code == 0 else {}
    return [p["pid"] for p in info.get("foreground_processes", []) if name in (p.get("cmdline") or "")]


def claude_context(pane_id, cfg, state_dir):
    """(% of the context window used, or None while its size is unknown, tokens) of the Claude Code
    session in the pane, from the transcript of the session its claude process records."""
    for pid in pids(pane_id, "claude"):
        for profile in (p for p in cfg["profiles"] if p["provider"] == "claude" and p["enabled"]):
            session = cache.read_json(Path(profile["dir"]) / "sessions" / f"{pid}.json", {}).get("sessionId")
            transcripts = sorted((Path(profile["dir"]) / "projects").glob(f"*/{session}.jsonl")) \
                if SESSION_ID.match(str(session)) else []
            tokens = claude_tokens(transcripts[0]) if transcripts else None
            if tokens:
                seen = cache.read_json(Path(state_dir) / "context.json", {}).get(pane_id) or []
                window = (seen[2] if len(seen) > 2 else None) or (1_000_000 if tokens > CLAUDE_WINDOW else None)
                return (100 * tokens / window if window else None), tokens
    return None


def claude_tokens(path):
    """Tokens in context at the transcript's latest reply: its input, cache writes and cache reads,
    as Claude Code's context_window counts them."""
    for line in reversed(tail(path)):
        if b'"usage"' not in line or b'"assistant"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message") or {}
        usage = message.get("usage") or {}
        if record.get("type") == "assistant" and usage and not record.get("isSidechain") and message.get("model") != "<synthetic>":
            return sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return None


def tail(path):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - TAIL))
            return f.read().splitlines()
    except OSError:
        return []


def codex_context(pane_id):
    """(% of the context window used, tokens) of the Codex session running in the pane, or None."""
    files = {f for pid in pids(pane_id, "codex") for f in open_files(pid)
             if "/sessions/" in f and os.path.basename(f).startswith("rollout-") and f.endswith(".jsonl")}
    for path in sorted((f for f in files if not subagent(f)), key=os.path.getmtime, reverse=True):
        found = last_context(path)
        if found:
            return found
    return None


def open_files(pid):
    fds = Path(f"/proc/{pid}/fd")
    if fds.is_dir():  # Linux
        paths = []
        for fd in fds.iterdir():
            try:
                paths.append(os.readlink(fd))
            except OSError:
                pass
        return paths
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        out = subprocess.run([lsof, "-a", "-p", str(pid), "-Fn"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line[1:] for line in out.splitlines() if line.startswith("n/")]


def subagent(path):
    """A subagent's session file (it names its parent in session_meta.source)."""
    try:
        with open(path) as f:
            source = (json.loads(f.readline()).get("payload") or {}).get("source")
    except (OSError, ValueError):
        return False
    return isinstance(source, dict) and "subagent" in source


def last_context(path):
    """The context in use at the session's latest token count: the last request's input tokens
    (cached ones included) and their share of model_context_window."""
    for line in reversed(tail(path)):
        if b'"token_count"' not in line:
            continue
        try:
            payload = json.loads(line).get("payload") or {}
        except ValueError:
            continue
        info = payload.get("info") or {}
        tokens, window = (info.get("last_token_usage") or {}).get("input_tokens"), info.get("model_context_window")
        if payload.get("type") == "token_count" and tokens and window:
            return 100 * tokens / window, tokens
    return None


def context(pane_id, kind, cfg, state_dir):
    if kind == "claude":
        return claude_context(pane_id, cfg, state_dir)
    return codex_context(pane_id) if kind == "codex" else None


def show_all(cfg, state_dir, now):
    """Meters for every Claude Code and Codex agent pane now (after setup, and when Herdr starts).
    Returns how many panes have one."""
    code, out, _ = integrate.herdr("agent", "list")
    listed = json.loads(out or "{}").get("result", {}).get("agents", []) if code == 0 else []
    shown = 0
    for agent in listed:
        found = context(agent["pane_id"], agent.get("agent"), cfg, state_dir)
        if found:
            show(agent["pane_id"], *found, state_dir, cfg["context"]["icon"], now)
            shown += 1
    return shown


def bridged(kind, cfg, state_dir, now):
    """Whether this provider's limits are already current without asking it again.

    Claude Code hands its 5-hour and weekly limits to the statusline bridge with every reply, so
    starting a `claude -p` check after the turn would only add the slow-moving model limits (Fable),
    which the regular refresh brings in anyway. True when every enabled Claude account is that fresh.
    """
    if kind != "claude":
        return False
    profiles = [p for p in cfg["profiles"] if p["provider"] == "claude" and p["enabled"]]
    return bool(profiles) and all(
        now - (cache.read_json(claude.snapshot_path(state_dir, p["id"]), {}).get("observed_at") or 0) <= BRIDGE_FRESH
        for p in profiles)


def on_event(event, cfg, state_dir, now):
    """Herdr's pane.agent_status_changed: when an agent finishes a turn, re-read its provider's
    limits (at most once a minute, and not when the statusline bridge already has them) and the
    pane's context meter. Returns a note."""
    data = event.get("data") if isinstance(event.get("data"), dict) else event
    kind, pane, status = data.get("agent"), data.get("pane_id"), data.get("agent_status")
    if status not in ("done", "idle") or not pane:
        return f"{kind} {pane} {status}: nothing to do"
    notes = []
    if kind in ("claude", "codex"):
        found = context(pane, kind, cfg, state_dir)
        notes.append(f"context {show(pane, *found, state_dir, cfg['context']['icon'], now) or 'unchanged'}"
                     if found else "no context found")
    if any(p["enabled"] and p["provider"] == kind for p in cfg["profiles"]):
        if bridged(kind, cfg, state_dir, now):
            notes.append("limits current from the statusline")
        else:
            notes.append("refresh started" if cache.refresh_after_turn(state_dir, kind, now) else "refreshed recently")
    return f"{kind} {pane} {status}: " + ("; ".join(notes) or "nothing to do")
