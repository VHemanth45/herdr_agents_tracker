"""Agents: a context meter per agent in Herdr's Agents panel, and fresh limits after each turn.

The meter ("⛁ 28% 72k": share of the context window in use, and its tokens) is a display-only
pane token that setup adds to [ui.sidebar.agents] rows; one of integrate.CONTEXT_TOKENS is set per
pane, picked by level so the meter takes that color. Claude Code panes are updated by the
statusline bridge, which runs inside the pane (HERDR_PANE_ID) and receives context_window, and
otherwise from the transcript of the session their claude process records in sessions/<pid>.json.
Codex panes are updated from the session file their codex process holds open (its main thread;
subagents have files of their own). Both also when their agent finishes a turn.

After a turn the meter also names the pane's part of its account's shortest limit ("~12% of 5h":
the session's share of the account's tokens in that window, cache reads left out, times the
window's % used). When the session's last reply is its provider's usage-limit error, the pane
is noted in waiting.json and the meter says when the limit resets instead; a minute after the
reset, one notification names the agents that were waiting, and with [resume] enabled they
are sent the resume prompt, if they are still idle at that error.
"""

import re

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import PLUGIN_ID, alerts, cache, collect, fmt, history, integrate
from .providers import PROVIDERS, claude

BRIDGE_FRESH = 120  # seconds: a statusline reading this recent belongs to the turn that just ended
RESEND = 600  # re-send an unchanged meter after this long: Herdr drops pane tokens when it restarts
CLAUDE_WINDOW = 200_000  # Claude's usual context window; a session past it has the 1M window
SESSION_ID = re.compile(r"^[0-9a-f-]{36}$")
TAIL = 256 * 1024  # bytes read from the end of a Codex session file to find its latest token count
GRACE = 60  # seconds after a reset before waiting agents are released: provider clocks differ a little
FORGET_WAITING = 86400  # a waiting note this long past its reset is dropped (Herdr was not running)


def meter(pct, tokens, icon):
    """"⛁ 28% 72k"; without a known window size just the tokens, "⛁ 118k"."""
    size = f"{tokens / 1e6:.1f}M" if tokens >= 1e6 else f"{tokens / 1e3:.0f}k" if tokens >= 1e3 else str(tokens)
    return f"{fmt.icon_text(icon) or 'ctx'} " + (f"{pct:.0f}% " if pct is not None else "") + size


def show(pane_id, pct, tokens, state_dir, icon, now=None, window=None, extra=None):
    """Put the meter on the pane when it changed, or was last sent long ago; returns the text sent.
    `window` (a context size seen by the statusline bridge) is remembered for the pane, and so is
    `extra`, [note, until]: a note after the meter ("~12% of 5h") shown until that time."""
    now = now or time.time()
    path = Path(state_dir) / "context.json"
    shown = {pane: v for pane, v in cache.read_json(path, {}).items() if now - v[1] < 86400}  # forget closed panes
    last = shown.get(pane_id) or ["", 0, None]
    window = window or (last[2] if len(last) > 2 else None)
    extra = extra if extra is not None else (last[3] if len(last) > 3 else None)
    level = fmt.level(pct) if pct is not None else 0
    return send(pane_id, meter(pct, tokens, icon), level, window, extra, shown, path, now)


def send(pane_id, base, level, window, extra, shown, path, now):
    extra = extra if extra and extra[1] > now else None
    text = base + (f" · {extra[0]}" if extra else "")
    last = shown.get(pane_id) or ["", 0]
    if last[0] == text and now - last[1] < RESEND:
        return None
    shown[pane_id] = [text, now, window, extra, level, base]
    cache.write_json(path, shown)
    args = [a for i, token in enumerate(integrate.CONTEXT_TOKENS)
            for a in (("--token", f"{token}={text}") if i == level else ("--clear-token", token))]
    subprocess.Popen([integrate.herdr_bin(), "pane", "report-metadata", pane_id, "--source", PLUGIN_ID, *args],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)  # never keep Claude's statusline waiting
    return text


def clear_expired(state_dir, now):
    """Take notes past their time off the meters (e.g. a pane idle since its limit reset)."""
    path = Path(state_dir) / "context.json"
    shown = cache.read_json(path, {})
    cleared = [pane for pane, v in shown.items() if len(v) > 5 and v[3] and v[3][1] <= now and now - v[1] < 86400]
    for pane in cleared:
        send(pane, shown[pane][5], shown[pane][4], shown[pane][2], None, shown, path, now)
    return cleared


def from_statusline(payload):
    """(% of the context window used, tokens) from Claude Code's statusline input. Right after a
    /compact Claude Code sends no usage, so the transcript it names is read instead; None before
    the session's first response."""
    window = (payload or {}).get("context_window") or {}
    pct = window.get("used_percentage")
    if isinstance(pct, (int, float)):
        return float(pct), int(window.get("total_input_tokens") or 0)
    tokens = claude_tokens(payload["transcript_path"]) if (payload or {}).get("transcript_path") else None
    size = window.get("context_window_size")
    return (100 * tokens / size if size else None, tokens) if tokens else None


def pids(pane_id, name):
    """Process ids of the pane's foreground processes whose command line mentions `name`."""
    code, out, _ = integrate.herdr("pane", "process-info", "--pane", pane_id)
    info = json.loads(out or "{}").get("result", {}).get("process_info", {}) if code == 0 else {}
    return [p["pid"] for p in info.get("foreground_processes", []) if name in (p.get("cmdline") or "")]


def claude_context(pane_id, cfg, state_dir):
    """(% of the context window used, or None while its size is unknown, tokens) of the Claude Code
    session in the pane, from the transcript of the session its claude process records."""
    found = locate(pane_id, "claude", cfg)
    return measure(found, pane_id, state_dir) if found else None


def locate(pane_id, kind, cfg):
    """The agent session running in the pane: {"kind", "profile" (None when no enabled account
    holds it), "sessions" (ids, subagents included), "files" (its session files), "main" (the
    file of its main thread)}, or None."""
    if kind == "claude":
        for pid in pids(pane_id, "claude"):
            for profile in (p for p in cfg["profiles"] if p["provider"] == "claude" and p["enabled"]):
                session = cache.read_json(Path(profile["dir"]) / "sessions" / f"{pid}.json", {}).get("sessionId")
                transcripts = sorted((Path(profile["dir"]) / "projects").glob(f"*/{session}.jsonl")) \
                    if SESSION_ID.match(str(session)) else []
                if transcripts:
                    subagents = sorted(transcripts[0].parent.glob(f"{session}/**/*.jsonl"))
                    return {"kind": kind, "profile": profile, "sessions": [session],
                            "files": [transcripts[0], *subagents], "main": transcripts[0]}
    elif kind == "codex":
        files = {f for pid in pids(pane_id, "codex") for f in open_files(pid)
                 if "/sessions/" in f and os.path.basename(f).startswith("rollout-") and f.endswith(".jsonl")}
        mains = sorted((f for f in files if not subagent(f)), key=os.path.getmtime, reverse=True)
        main = next((f for f in mains if last_context(f)), mains[0] if mains else None)
        if main:
            real = os.path.realpath(main)
            profile = next((p for p in cfg["profiles"] if p["provider"] == "codex" and p["enabled"]
                            and real.startswith(os.path.realpath(p["dir"]) + os.sep)), None)
            return {"kind": kind, "profile": profile, "files": sorted(files), "main": main,
                    "sessions": [s for s in map(codex_session, sorted(files)) if s]}
    return None


def measure(found, pane_id, state_dir):
    """(% of the context window used or None, tokens) of a located session, or None."""
    if found["kind"] == "codex":
        return last_context(found["main"])
    tokens = claude_tokens(found["main"])
    if not tokens:
        return None
    seen = cache.read_json(Path(state_dir) / "context.json", {}).get(pane_id) or []
    window = (seen[2] if len(seen) > 2 else None) or (1_000_000 if tokens > CLAUDE_WINDOW else None)
    return (100 * tokens / window if window else None), tokens


def codex_session(path):
    try:
        with open(path) as f:
            first = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    payload = first.get("payload") or {}
    return (payload.get("id") or payload.get("session_id")) if first.get("type") == "session_meta" else None


def shortest(entry, now):
    """The account's shortest limit window shared by all models (the 5-hour one, where there is one)."""
    live = [w for w in fmt.current(entry.get("windows") or [], now)
            if w.get("minutes") and w.get("resets_at") and " " not in w["label"]]
    return min(live, key=lambda w: w["minutes"], default=None)


def share(found, cfg, state_dir, now):
    """[note, until] naming the pane's part of its account's shortest limit window, e.g.
    ["~12% of 5h", reset time]: its sessions' share of the account's tokens since the window
    opened (cache reads left out: they weigh little against the limits), times the window's %."""
    profile = found.get("profile")
    if not profile or cfg["context"].get("share", True) is False:
        return None
    w = shortest(collect.entries(cfg, state_dir, now).get(profile["id"]) or {}, now)
    if not w:
        return None
    with history.Store(history.db_path(state_dir, profile["id"])) as store:
        for path in found["files"]:  # this session's latest replies; the collector brings in the rest
            store.scan(path, PROVIDERS[profile["provider"]].parse_line)
    mine, total = history.session_share(state_dir, profile["id"], found["sessions"], w["resets_at"] - w["minutes"] * 60)
    if not total:
        return None
    part = w["used"] * mine / total
    return [f"{'~' + fmt.pct(part) if part >= 1 else '<1%'} of {w['label']}", w["resets_at"]]


def stopped_at_limit(found):
    """Whether the session's latest reply is its provider's usage-limit error: Claude Code's
    "You've hit your session limit · resets 10:20pm" (a synthetic reply with error "rate_limit"),
    or a Codex token count naming the limit it reached."""
    for line in reversed(tail(found["main"])):
        if found["kind"] == "claude" and (b'"user"' in line or b'"assistant"' in line):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") in ("user", "assistant") and not record.get("isSidechain"):
                return record["type"] == "assistant" and record.get("error") == "rate_limit"
        elif found["kind"] == "codex" and b'"token_count"' in line:
            try:
                payload = json.loads(line).get("payload") or {}
            except ValueError:
                continue
            if payload.get("type") == "token_count":
                return bool((payload.get("rate_limits") or {}).get("rate_limit_reached_type"))
    return False


def note_waiting(found, pane_id, cfg, state_dir, now):
    """When the session stopped at its usage limit: note the pane as waiting for the limit it is at
    (the fullest; the later reset of a tie) and return the meter's [note, until]; else None."""
    if not found.get("profile") or not stopped_at_limit(found):
        return None
    entry = collect.entries(cfg, state_dir, now).get(found["profile"]["id"]) or {}
    w = max((w for w in fmt.current(entry.get("windows") or [], now) if w.get("resets_at")),
            key=lambda w: (w["used"], w["resets_at"]), default=None)
    if not w:
        return None
    agent = agent_info(pane_id) or {}
    with cache.lock(Path(state_dir) / "waiting.lock"):
        path = Path(state_dir) / "waiting.json"
        waiting = cache.read_json(path, {})
        waiting[pane_id] = {"profile": found["profile"]["id"], "window": w["id"], "label": w["label"],
                            "resets_at": w["resets_at"], "agent": found["kind"], "since": now,
                            "name": agent.get("terminal_title_stripped") or pane_id}
        cache.write_json(path, waiting)
    return [f"limit · resets {fmt.clock(w['resets_at'], now)}", w["resets_at"] + GRACE]


def agent_info(pane_id):
    try:
        code, out, _ = integrate.herdr("agent", "get", pane_id, timeout=3)
        return json.loads(out or "{}").get("result", {}).get("agent") if code == 0 else None
    except (ValueError, integrate.SetupError):
        return None


def release_waiting(cfg, state_dir, now, send=None):
    """At each status refresh: for agents waiting on a limit that reset over a minute ago, one
    notification per limit, and with [resume] enabled the resume prompt to each agent still idle at
    its limit error. Also clears meter notes past their time. Returns the (profile, window) pairs
    announced, so alerts leave out their plain reset notice."""
    path, announced = Path(state_dir) / "waiting.json", set()
    if any(w.get("resets_at", 0) + GRACE <= now for w in cache.read_json(path, {}).values()):
        with cache.lock(Path(state_dir) / "waiting.lock") as held:
            if held:
                announced = release(cfg, path, now, send)
    clear_expired(state_dir, now)
    return announced


def release(cfg, path, now, send):
    waiting, announced = cache.read_json(path, {}), set()
    profiles = {p["id"]: p for p in cfg["profiles"] if p["enabled"]}
    groups = {}
    for pane, w in waiting.items():
        if w.get("resets_at", 0) + GRACE <= now:
            groups.setdefault((w["profile"], w["window"]), []).append(pane)
    for (pid, wid), panes in groups.items():
        if pid not in profiles or now - waiting[panes[0]]["resets_at"] > FORGET_WAITING:
            for pane in panes:
                del waiting[pane]
            continue
        for pane in panes:
            if cfg["resume"].get("enabled") is True and not waiting[pane].get("resumed"):
                waiting[pane]["resumed"] = resume(pane, waiting[pane], cfg)
        note = waiting_message(profiles[pid], [waiting[pane] for pane in panes])
        if cfg["alerts"].get("on_reset", True) is False or (send or alerts.notify)(*note):
            for pane in panes:
                del waiting[pane]
            announced.add((pid, wid))
    cache.write_json(path, waiting)
    return announced


def resume(pane_id, wait, cfg):
    """Send the resume prompt when the agent is still idle at its limit error (nobody went on by hand)."""
    agent = agent_info(pane_id) or {}
    if agent.get("agent") != wait["agent"] or agent.get("agent_status") not in ("idle", "done"):
        return False
    found = locate(pane_id, wait["agent"], cfg)
    if not found or not stopped_at_limit(found):
        return False
    try:
        subprocess.Popen([integrate.herdr_bin(), "agent", "prompt", pane_id, str(cfg["resume"].get("prompt") or "continue")],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except (OSError, integrate.SetupError):
        return False
    return True


def waiting_message(profile, waits):
    """("Claude 5h limit has reset", "Waiting on it: api, web. Resumed both.")"""
    base, _, scope = waits[0]["label"].partition(" ")
    title = f"{profile['label']} {base} limit{f' ({scope})' if scope else ''} has reset"
    names = ", ".join(str(w["name"])[:30] for w in waits)
    resumed = sum(1 for w in waits if w.get("resumed"))
    tail_text = (" Resumed." if resumed == len(waits) == 1 else f" Resumed {resumed} of {len(waits)}." if resumed
                 else " It can go on." if len(waits) == 1 else " They can go on.")
    return title, f"Waiting on it: {names}.{tail_text}"


def claude_tokens(path):
    """Tokens in context at the transcript's latest reply: its input, cache writes and cache reads,
    as Claude Code's context_window counts them.

    After a /compact with no reply since, an estimate: the compact_boundary's postTokens counts only
    the kept conversation, so the session's first reply is added for the system prompt, tools and
    memory it always carries (within about 10% of the next reply in real sessions)."""
    for line in reversed(tail(path)):
        compacted = b'"compact_boundary"' in line
        if not compacted and (b'"usage"' not in line or b'"assistant"' not in line):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if compacted and record.get("subtype") == "compact_boundary" and not record.get("isSidechain"):
            post, base = (record.get("compactMetadata") or {}).get("postTokens"), first_reply(path)
            return post + base if isinstance(post, int) and post > 0 and base else None
        if reply_tokens(record):
            return reply_tokens(record)
    return None


def reply_tokens(record):
    message = record.get("message") or {}
    usage = message.get("usage") or {}
    if record.get("type") == "assistant" and usage and not record.get("isSidechain") and message.get("model") != "<synthetic>":
        return sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return None


def first_reply(path):
    """Tokens in context at the session's first reply, from the start of its transcript."""
    try:
        with open(path, "rb") as f:
            lines = f.read(TAIL).splitlines()
    except OSError:
        return None
    for line in lines:
        if b'"usage"' in line and b'"assistant"' in line:
            try:
                tokens = reply_tokens(json.loads(line))
            except ValueError:
                continue
            if tokens:
                return tokens
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
    found = locate(pane_id, "codex", {"profiles": []})
    return last_context(found["main"]) if found else None


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
    (cached ones included) and their share of model_context_window. After a compaction with no
    request since, Codex reports no tokens in use, and so does the meter."""
    for line in reversed(tail(path)):
        if b'"compacted"' in line:
            try:
                if json.loads(line).get("type") == "compacted":
                    return 0.0, 0
            except ValueError:
                pass
            continue
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


def context(pane_id, kind, cfg, state_dir, found=None):
    found = found or (locate(pane_id, kind, cfg) if kind in ("claude", "codex") else None)
    return measure(found, pane_id, state_dir) if found else None


def show_all(cfg, state_dir, now):
    """Meters for every Claude Code and Codex agent pane now (after setup, and when Herdr starts).
    Returns how many panes have one."""
    code, out, _ = integrate.herdr("agent", "list")
    listed = json.loads(out or "{}").get("result", {}).get("agents", []) if code == 0 else []
    shown = 0
    for agent in listed:
        found = locate(agent["pane_id"], agent.get("agent"), cfg) if agent.get("agent") in ("claude", "codex") else None
        measured = context(agent["pane_id"], agent.get("agent"), cfg, state_dir, found)
        if measured:
            show(agent["pane_id"], *measured, state_dir, cfg["context"]["icon"], now, extra=usage_note(found, agent["pane_id"], cfg, state_dir, now))
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


def usage_note(found, pane_id, cfg, state_dir, now):
    """The meter's note after a turn: when the limit resets if the agent stopped at it, else its share."""
    if not found:
        return None
    try:
        return note_waiting(found, pane_id, cfg, state_dir, now) or share(found, cfg, state_dir, now)
    except Exception:  # the meter itself matters more than its note
        return None


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
        found = locate(pane, kind, cfg)
        measured = context(pane, kind, cfg, state_dir, found)
        extra = usage_note(found, pane, cfg, state_dir, now) if measured else None
        notes.append(f"context {show(pane, *measured, state_dir, cfg['context']['icon'], now, extra=extra) or 'unchanged'}"
                     if measured else "no context found")
    if any(p["enabled"] and p["provider"] == kind for p in cfg["profiles"]):
        if bridged(kind, cfg, state_dir, now):
            notes.append("limits current from the statusline")
        else:
            notes.append("refresh started" if cache.refresh_after_turn(state_dir, kind, now) else "refreshed recently")
    return f"{kind} {pane} {status}: " + ("; ".join(notes) or "nothing to do")
