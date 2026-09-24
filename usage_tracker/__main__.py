"""Command line: `usage-tracker <command>`; run with --help for the list."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback

from . import PLUGIN_ID, VERSION, agents, alerts, cache, collect, config, fmt, history, integrate
from .providers import PROVIDERS, claude


CHECK_OK, CHECK_NEAR, CHECK_FULL, CHECK_UNKNOWN = 0, 10, 11, 20


def cmd_status(args):
    if args.json or args.check is not None:
        return report(args)
    # Herdr hides a tab-bar entry that fails or times out, so print one line and exit 0. Only a
    # --part without an account (or a later part when something failed) exits 1 to stay hidden.
    first = args.part in (None, "1")
    try:
        cfg, state_dir, now = config.load(), config.state_dir(), time.time()
        entries = collect.entries(cfg, state_dir, now)
        line = fmt.status_line(cfg["profiles"], entries, cfg, now, args.part)
        if line is None:
            return 1
        print(line, flush=True)
        if first:  # one collector request and alert check per refresh cycle, not one per entry
            cache.request_refresh(state_dir, entries, [p for p in cfg["profiles"] if p["enabled"]], now)
            covered = agents.release_waiting(cfg, state_dir, now)
            alerts.check(cfg["profiles"], entries, cfg, now, state_dir, covered=covered)
    except Exception as exc:
        if not first:
            return 1
        print(f"Usage: error ({type(exc).__name__}, see Usage diagnostics)", flush=True)
        traceback.print_exc()
    return 0


def report(args):
    """For scripts: the accounts as JSON (--json) and/or an exit code for the highest limit (--check):
    0 below PCT, 10 at or above it, 11 when a limit is used up, 20 when no account has fresh data.
    Accounts without fresh data are left out of the check."""
    cfg, state_dir, now = config.load(), config.state_dir(), time.time()
    entries = collect.entries(cfg, state_dir, now)
    profiles = [p for p in cfg["profiles"] if p["enabled"] and (not args.profile or p["id"] in args.profile)]
    cache.request_refresh(state_dir, entries, profiles, now)
    accounts = [account(p, entries.get(p["id"]) or {}, cfg, now) for p in profiles]
    if args.json:
        print(json.dumps({"generated_at": int(now), "accounts": accounts}, indent=2))
    else:
        print(fmt.status_line(profiles, entries, cfg, now) or "no accounts")
    if args.check is None:
        return CHECK_OK
    used = [w["used"] for a in accounts if not a["stale"] for w in a["windows"]]
    if not used:
        return CHECK_UNKNOWN
    return CHECK_FULL if max(used) >= 100 else CHECK_NEAR if max(used) >= args.check else CHECK_OK


def account(profile, entry, cfg, now):
    updated = entry.get("updated_at")
    windows = []
    for w in fmt.current(entry.get("windows") or [], now):
        ahead = fmt.forecast(w, now) or (None, None)
        windows.append({"id": w["id"], "label": w["label"], "used": w["used"], "resets_at": w.get("resets_at"),
                        "minutes": w.get("minutes"),
                        "at_reset": None if ahead[0] is None else round(min(ahead[0], 100), 1),
                        "full_in_seconds": None if ahead[1] is None else int(ahead[1])})
    return {"id": profile["id"], "provider": profile["provider"], "label": profile["label"],
            "plan": entry.get("plan"), "state": entry.get("state") or "pending", "error": entry.get("error"),
            "estimated": bool(entry.get("estimated")), "updated_at": updated,
            "stale": not updated or now - updated > cfg["refresh"]["stale_seconds"], "windows": windows}


def check_level(value):
    try:
        level = float(value)
    except ValueError:
        level = -1
    if not 0 < level <= 100:
        raise argparse.ArgumentTypeError("use a percentage from 1 to 100")
    return level


def part(value):
    if not re.fullmatch(r"[1-9][0-9]*-?", value):
        raise argparse.ArgumentTypeError('use an account position such as "2", or "3-" for the third on')
    return value


def cmd_refresh(args):
    if args.background:
        cache.spawn("refresh", *(["--force"] if args.force else []))
        return 0
    ran = collect.refresh(config.load(), config.state_dir(), force=args.force, with_history=not args.no_history,
                          providers=args.provider)
    print("Refreshed." if ran else "Another refresh is already running.")
    return 0


def cmd_agent_event(args):
    """Herdr runs this when an agent's state changes (the manifest's event hook); the printed note
    lands in Herdr's plugin log."""
    try:
        if args.all:
            print(f"context meters on {agents.show_all(config.load(), config.state_dir(), time.time())} agent panes")
            return 0
        event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
        print(agents.on_event(event, config.load(), config.state_dir(), time.time()))
    except Exception:
        traceback.print_exc()
        return 1
    return 0


def cmd_dashboard(args):
    from . import dashboard
    dashboard.run()
    return 0


def cmd_open(args):
    """Ask Herdr to open one of this plugin's popup panes (used by actions and the shortcut)."""
    try:
        code, out, err = integrate.herdr("plugin", "pane", "open", "--plugin", PLUGIN_ID,
                                         "--entrypoint", args.entrypoint, "--focus")
    except integrate.SetupError as exc:
        print(exc, file=sys.stderr)
        return 1
    if code != 0:
        print((err or out).strip(), file=sys.stderr)
    return code


def cmd_claude_statusline(args):
    """Claude Code statusLine bridge: save rate_limits, then run the user's own statusline."""
    data = sys.stdin.buffer.read()
    try:
        payload = json.loads(data or b"{}")
        claude.record_statusline(args.profile, payload, config.state_dir())
        found = agents.from_statusline(payload)
        if found and os.environ.get("HERDR_PANE_ID"):  # running inside a Herdr pane: its context meter
            agents.show(os.environ["HERDR_PANE_ID"], *found, config.state_dir(), config.load()["context"]["icon"],
                        window=(payload.get("context_window") or {}).get("context_window_size"))
    except Exception:
        pass  # never break the user's statusline
    if not args.then:
        return 0
    return subprocess.run(args.then, shell=True, input=data).returncode


def cmd_setup(args):
    try:
        if args.interactive:
            print(GUIDE.format(config=config.config_dir() / "config.toml"))
            _, steps = integrate.setup(apply=False, claude_statusline=True, key=args.key, interval=args.interval)
            if steps and ask("\nApply these changes? [y/N] "):
                integrate.setup(apply=True, claude_statusline=True, key=args.key, interval=args.interval)
                show_meters()
            pause()
            return 0
        key, steps = integrate.setup(apply=args.apply, claude_statusline=args.claude_statusline,
                                     key=args.key, interval=args.interval)
        if steps and not args.apply:
            print(f"\nDry run. Re-run with --apply to make these changes (shortcut: {key}).")
        elif steps:
            show_meters()
        return 0
    except integrate.SetupError as exc:
        print(f"Setup stopped, nothing was changed: {exc}", file=sys.stderr)
        if args.interactive:
            pause()
        return 1


def show_meters():
    try:
        print(f"Context meters on {agents.show_all(config.load(), config.state_dir(), time.time())} agent panes.")
    except (integrate.SetupError, ValueError) as exc:
        print(f"  context meters: {exc}")


def cmd_uninstall(args):
    try:
        integrate.uninstall(apply=args.apply, purge=args.purge)
    except integrate.SetupError as exc:
        print(f"Uninstall stopped, nothing was changed: {exc}", file=sys.stderr)
        return 1
    if not args.apply:
        print("\nDry run. Re-run with --apply to remove.")
    return 0


def cmd_diagnostics(args):
    for line in diagnostics():
        print(line)
    if args.pause:
        pause()
    return 0


def diagnostics():
    """Plain-text health report. Shows paths, states and errors, never credentials."""
    cfg, state_dir, now = config.load(), config.state_dir(), time.time()
    entries = collect.entries(cfg, state_dir, now)
    lock_path = state_dir / "refresh.lock"
    last_run = fmt.duration(now - lock_path.stat().st_mtime) + " ago" if lock_path.exists() else "never"
    out = [f"Usage Tracker {VERSION} diagnostics (credentials are never shown)", "",
           f"python     {sys.executable} ({sys.version.split()[0]})",
           f"plugin     {integrate.ROOT}",
           f"config     {cfg['path']} ({'found' if os.path.exists(cfg['path']) else 'not found; accounts discovered'})",
           f"state      {state_dir}",
           f"collector  {'running' if cache.locked(lock_path) else 'idle'}; last run started {last_run}"]
    if cfg["error"]:
        out.append(f"CONFIG ERROR {cfg['error']}")
    out += ["", "Accounts"]
    coverage = history.coverage(state_dir, cfg["profiles"])
    for p in cfg["profiles"]:
        e = entries.get(p["id"]) or {}
        out.append(f"  {p['id']}: {p['label']} [{p['provider']}] {p['dir']} "
                   f"{'' if os.path.isdir(p['dir']) else '(missing) '}{'enabled' if p['enabled'] else 'disabled'}")
        if e:
            updated = fmt.duration(now - e["updated_at"]) + " ago" if e.get("updated_at") else "never"
            out.append(f"    limits   last attempt {e.get('state')}; data updated {updated}; source {e.get('source', '-')}")
            out.append(f"    windows  " + (", ".join(f"{w['label']} {fmt.pct(w['used'])} (id {w['id']})"
                                                    for w in e.get("windows") or []) or "none"))
            if e.get("error"):
                out.append(f"    error    {e['error']}")
            if e.get("next_at"):
                out.append(f"    next     in {fmt.duration(e['next_at'] - now)}")
        first, last, count = coverage.get(p["id"], (None, None, 0))
        out.append(f"    history  {count:,} requests" + (f", {time.strftime('%Y-%m-%d', time.localtime(first))} to "
                                                           f"{time.strftime('%Y-%m-%d', time.localtime(last))}" if count else ""))
        out.append(f"    supports {PROVIDERS[p['provider']].CAPABILITIES}")
    for warning in cfg["warnings"]:
        out.append(f"  warning: {warning}")
    out += ["", "Herdr integration"] + integration_report(cfg)
    log = state_dir / "collector.log"
    if log.exists():
        out += ["", f"Recent collector log ({log})"] + ["  " + line for line in log.read_text().splitlines()[-12:]]
    return out


def integration_report(cfg):
    path = integrate.herdr_config_path()
    lines = [f"  config    {path}"]
    try:
        herdr_cfg = integrate.parse(path.read_text()) if path.exists() else {}
    except integrate.SetupError as exc:
        return lines + [f"  {exc}"]
    ui = herdr_cfg.get("ui", {})
    ours = [e for e in ui.get("tab_bar_right", []) if str(integrate.LAUNCHER) in str(e.get("command", ""))]
    lines.append(f"  tab bar   {'%d entries every %ss' % (len(ours), ours[0].get('interval_seconds')) if ours else 'MISSING (run setup)'}; "
                 f"position {ui.get('tab_bar_position', 'top (default)')}")
    keys = [k for k in herdr_cfg.get("keys", {}).get("command", []) if str(integrate.LAUNCHER) in str(k.get("command", ""))]
    lines.append(f"  shortcut  {keys[0].get('key') + ' opens the dashboard' if keys else 'MISSING (run setup)'}")
    delivery = ((ui.get("toast") or {}) if isinstance(ui.get("toast"), dict) else {}).get("delivery", "off")
    lines.append(f"  alerts    at {', '.join(f'{t}%' for t in cfg['alerts']['thresholds']) or 'no thresholds (off)'}; "
                 f"Herdr notifications: {delivery}" + (" (alerts will not show)" if delivery == "off" else ""))
    rows = (((ui.get("sidebar") or {}).get("agents") or {}).get("rows") or []) if isinstance(ui.get("sidebar"), dict) else []
    shown = len(cache.read_json(config.state_dir() / "context.json", {}))
    lines.append(f"  meters    {'in the Agents panel' if '$usage_ctx' in str(rows) else 'MISSING (run setup)'}; "
                 f"{shown} panes updated in the last day")
    try:
        reg = integrate.plugin_registration()
        how = "installed from GitHub at " if (reg or {}).get("source", {}).get("kind") == "github" else "linked from "
        lines.append(f"  plugin    {how + reg.get('plugin_root', '?') if reg else 'NOT LINKED (run setup)'}"
                     + ("" if not reg or reg.get("enabled") else " (disabled)"))
    except integrate.SetupError as exc:
        lines.append(f"  plugin    unknown: {exc}")
    for p in cfg["profiles"]:
        if p["provider"] == "claude":
            settings = cache.read_json(os.path.join(p["dir"], "settings.json"), {})
            bridged = "claude-statusline" in str((settings.get("statusLine") or {}).get("command", ""))
            lines.append(f"  claude statusline bridge ({p['id']}): {'installed' if bridged else 'not installed'}")
    return lines


GUIDE = """Usage Tracker setup

Shows AI account usage at the top-right of Herdr's tab bar; the shortcut opens the dashboard.
Accounts and display options live in {config}
(copy examples from config.example.toml in the plugin folder). Setup below is idempotent:
it backs up files it edits, keeps your other settings, and `usage-tracker uninstall` reverts it.
Claude limits need the statusline bridge: it wraps your Claude Code statusLine command so
Claude's own rate_limits are saved, then runs your command unchanged. No credentials are read.
"""


def ask(prompt):
    try:
        return input(prompt).strip().lower().startswith("y")
    except EOFError:
        return False


def pause():
    try:
        input("\nPress Enter to close.")
    except EOFError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="usage-tracker", description="AI account usage for Herdr.")
    parser.add_argument("--version", action="version", version=f"usage-tracker {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("status", help="print the one-line tab-bar summary")
    p.add_argument("--part", type=part, help='only the account at this position ("2"), or from it on ("3-")')
    p.add_argument("--json", action="store_true", help="print every account's limits as JSON")
    p.add_argument("--check", type=check_level, nargs="?", const=80.0, metavar="PCT",
                   help="exit 10 when a limit is at PCT%% or more (default 80), 11 when one is used up, "
                        "20 when no account has fresh data, else 0")
    p.add_argument("--profile", action="append", help="only this account (repeatable; with --json or --check)")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("refresh", help="collect limits and history now")
    p.add_argument("--force", action="store_true", help="refresh every account, not just the due ones")
    p.add_argument("--background", action="store_true", help="start a detached collector and return")
    p.add_argument("--no-history", action="store_true", help="skip the local history scan")
    p.add_argument("--provider", action="append", choices=sorted(PROVIDERS),
                   help="refresh this provider's accounts now (repeatable)")
    p.set_defaults(func=cmd_refresh)
    p = sub.add_parser("agent-event", help="react to an agent state change (Herdr event hook)")
    p.add_argument("--all", action="store_true", help="update every Codex pane's context meter now")
    p.set_defaults(func=cmd_agent_event)
    sub.add_parser("dashboard", help="open the terminal dashboard").set_defaults(func=cmd_dashboard)
    p = sub.add_parser("open", help="open a plugin popup in Herdr")
    p.add_argument("entrypoint", nargs="?", default="dashboard", choices=("dashboard", "diagnostics", "setup"))
    p.set_defaults(func=cmd_open)
    p = sub.add_parser("diagnostics", help="print a health report")
    p.add_argument("--pause", action="store_true", help="wait for Enter (used in the popup)")
    p.set_defaults(func=cmd_diagnostics)
    p = sub.add_parser("setup", help="install the Herdr integration (dry run unless --apply)")
    p.add_argument("--apply", action="store_true", help="make the changes")
    p.add_argument("--claude-statusline", action="store_true", help="also install the Claude statusline bridge")
    p.add_argument("--key", help="dashboard shortcut (default: first free of prefix+u, prefix+alt+u, ...)")
    p.add_argument("--interval", type=int, default=30, help="tab-bar refresh interval in seconds")
    p.add_argument("--interactive", action="store_true", help="show the plan and ask before applying")
    p.set_defaults(func=cmd_setup)
    p = sub.add_parser("uninstall", help="remove this plugin's Herdr entries (dry run unless --apply)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--purge", action="store_true", help="also delete the plugin's config and state")
    p.set_defaults(func=cmd_uninstall)
    p = sub.add_parser("claude-statusline", help="Claude Code statusLine bridge (reads JSON on stdin)")
    p.add_argument("--profile", required=True)
    p.add_argument("--then", help="the statusLine command to run afterwards")
    p.set_defaults(func=cmd_claude_statusline)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
