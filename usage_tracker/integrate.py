"""Herdr and Claude Code integration: idempotent setup and a matching uninstall.

Every line this plugin writes into Herdr's config.toml ends with MARK. Setup first drops its own
marked lines and then adds fresh ones, so running it again never duplicates entries; uninstall
removes exactly those lines. Each write is preceded by a timestamped backup, and a new config
is only written after re-parsing proves it equals the original plus our entries.
"""

import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

from . import PLUGIN_ID, cache, config, fmt

MARK = "# usage-tracker"
PREFERRED_KEYS = ("prefix+u", "prefix+alt+u", "prefix+y")
REFRESH_KEYS = ("prefix+shift+u", "prefix+shift+y")  # refresh every account now
ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "usage-tracker"
HEADER = re.compile(r"^\s*\[\[?[^\[\]]+\]\]?\s*(#.*)?$")


class SetupError(Exception):
    pass


# ---- Herdr CLI -------------------------------------------------------------------------------

def herdr_config_path():
    if os.environ.get("HERDR_CONFIG_PATH"):
        return Path(os.environ["HERDR_CONFIG_PATH"])
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "herdr" / "config.toml"


def herdr_bin():
    for candidate in (os.environ.get("HERDR_BIN_PATH"), shutil.which("herdr"), Path.home() / ".local/bin/herdr"):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return str(candidate)
    raise SetupError("herdr binary not found")


def herdr(*args, timeout=15):
    try:
        done = subprocess.run([herdr_bin(), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"herdr {' '.join(args)}: {exc}") from None
    return done.returncode, done.stdout, done.stderr


def herdr_json(*args):
    code, out, err = herdr(*args)
    try:
        return json.loads(out or err)
    except ValueError:
        raise SetupError(f"herdr {' '.join(args)} failed: {(err or out).strip()[:300]}") from None


def plugin_registration():
    """Our entry in Herdr's plugin registry, or None."""
    plugins = herdr_json("plugin", "list", "--json").get("result", {}).get("plugins", [])
    return next((p for p in plugins if p.get("plugin_id") == PLUGIN_ID), None)


def reload_herdr():
    result = herdr_json("server", "reload-config").get("result", {})
    return result.get("status"), result.get("diagnostics") or []


def default_bindings():
    """Herdr's built-in keybindings, read from `herdr --default-config` (commented [keys] lines)."""
    code, out, err = herdr("--default-config")
    if code != 0:
        raise SetupError(f"herdr --default-config failed: {err.strip()[:200]}")
    section, found = None, {}
    for line in out.splitlines():
        header = re.match(r"^\s*\[+([^\]]+)\]+", line)
        if header:
            section = header.group(1).strip()
            continue
        binding = re.match(r'^\s*#\s*([a-z_]+)\s*=\s*"([^"]+)"', line)
        if section == "keys" and binding:
            found[binding.group(1)] = binding.group(2)
    return found


# ---- config.toml editing ---------------------------------------------------------------------

def command(*args):
    return " ".join([shlex.quote(str(LAUNCHER)), *args])


# One tab-bar entry per account: Herdr shows at most 80 columns of each entry. An entry without an
# account exits 1 and stays hidden; the last one also shows any accounts after it.
PARTS = ("1", "2", "3-")
SEPARATOR = fmt.SEPARATOR


def status_entries(interval):
    return [{"type": "command", "command": command("status", "--part", part), "interval_seconds": interval,
             "timeout_seconds": 2} for part in PARTS]


def key_entry(key):
    return {"key": key, "type": "shell", "command": command("open", "dashboard")}


def refresh_key_entry(key):
    return {"key": key, "type": "shell", "command": command("refresh", "--force", "--background")}


def toml_value(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(map(toml_value, value)) + "]"
    return json.dumps(value) if isinstance(value, (str, bool)) else str(value)  # valid TOML too


inline = toml_value


# Context meters in the Agents panel: one of these pane tokens is set per agent, by level, so the
# meter takes that color. Colors (calm, getting full, nearly full) suit Herdr's built-in themes.
CONTEXT_TOKENS = ("usage_ctx_ok", "usage_ctx_warn", "usage_ctx_hot")
CONTEXT_COLORS = {
    "catppuccin": ("#7f849c", "#f9e2af", "#f38ba8"), "gruvbox": ("#928374", "#fabd2f", "#fb4934"),
    "tokyo-night": ("#565f89", "#e0af68", "#f7768e"), "dracula": ("#6272a4", "#f1fa8c", "#ff5555"),
    "nord": ("#616e88", "#ebcb8b", "#bf616a"), "one-dark": ("#5c6370", "#e5c07b", "#e06c75"),
    "kanagawa": ("#727169", "#e6c384", "#ff5d62"), "rose-pine": ("#6e6a86", "#f6c177", "#eb6f92"),
}
AGENT_ROWS = [["state_icon", "workspace", "tab"], ["agent"]]  # Herdr's default agent rows
MAX_ROWS = 16  # Herdr refuses a sidebar layout with more rows


def context_tokens(herdr_cfg):
    theme = herdr_cfg.get("theme") if isinstance(herdr_cfg.get("theme"), dict) else {}
    name = theme.get("dark_name") if theme.get("auto_switch") else theme.get("name")
    colors = CONTEXT_COLORS.get(name, CONTEXT_COLORS["catppuccin"])
    return [{"token": f"${token}", "fg": color} for token, color in zip(CONTEXT_TOKENS, colors)]


def is_marked(line):
    return line.rstrip().endswith(MARK)


def strip_marked(text):
    """Remove every line this plugin added. A marked table header whose table has since gained
    other lines is kept (unmarked) so those lines stay in their table."""
    lines = text.splitlines(keepends=True)
    out = []
    for i, line in enumerate(lines):
        if not is_marked(line):
            out.append(line)
        elif HEADER.match(line) and _table_has_user_lines(lines, i):
            out.append(line.rstrip()[:-len(MARK)].rstrip() + "\n")
        elif HEADER.match(line) and out and not out[-1].strip():
            out.pop()  # the blank line setup put before its own table
    return "".join(out)


def _table_has_user_lines(lines, header):
    for line in lines[header + 1:]:
        if HEADER.match(line):
            return False
        if line.strip() and not line.strip().startswith("#") and not is_marked(line):
            return True
    return False


def _find_header(lines, name):
    pattern = re.compile(rf"^\s*\[\s*{re.escape(name)}\s*\]\s*(#.*)?$")
    return next((i for i, line in enumerate(lines) if pattern.match(line)), None)


def _array_close(text, start):
    """For the '[' at text[start], return (index of its matching ']', index of the last
    significant character inside it), skipping strings and comments."""
    depth, i, last = 0, start, start
    while i < len(text):
        c = text[i]
        if c == "#":
            i = text.find("\n", i)
            if i < 0:
                break
            continue
        if c in "\"'":
            quote = c * 3 if text.startswith(c * 3, i) else c
            j = i + len(quote)
            while j < len(text) and not text.startswith(quote, j):
                j += 2 if c == '"' and text[j] == "\\" else 1
            i = last = j + len(quote) - 1
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return i, last
        if not c.isspace() and c not in "\"'":
            last = i
        i += 1
    raise SetupError("could not find the end of ui.tab_bar_right")


def _append_to_array(text, lines, start, entry_line, key="tab_bar_right"):
    """Insert entry_line (one or more lines) at the end of the existing `key` array in the table
    whose header is lines[start]."""
    offset = sum(len(line) for line in lines[:start + 1])
    for line in lines[start + 1:]:
        if HEADER.match(line):
            break
        found = re.match(rf"^\s*{key}\s*=\s*\[", line)
        if found:
            close, last = _array_close(text, offset + found.end() - 1)
            if text[last] not in "[,":
                text = text[:last + 1] + "," + text[last + 1:]
                close += 1
            line_start = text.rfind("\n", 0, close) + 1
            if text[line_start:close].strip():
                return text[:close] + "\n" + entry_line + text[close:]
            return text[:line_start] + entry_line + text[line_start:]
        offset += len(line)
    raise SetupError(f"{key} is set in a form this setup cannot edit; add the entry manually")


def _add_context_meter(text, agents, meter):
    """Show the meter in the Agents panel: after the agent name in Herdr's default rows, or as a
    row of its own after rows the user set."""
    lines = text.splitlines(keepends=True)
    start = _find_header(lines, "ui.sidebar.agents")
    if "rows" in agents:
        if start is None:
            raise SetupError("ui.sidebar.agents.rows is set in a form this setup cannot edit; add the meter manually")
        return _append_to_array(text, lines, start, f"  {toml_value(meter)},  {MARK}\n", key="rows"), \
            agents["rows"] + [meter]
    rows = AGENT_ROWS[:-1] + [AGENT_ROWS[-1] + meter]
    body = f"rows = [  {MARK}\n" + "".join(f"  {toml_value(row)},  {MARK}\n" for row in rows) + f"]  {MARK}\n"
    if start is not None:
        return "".join(lines[:start + 1]) + body + "".join(lines[start + 1:]), rows
    return text.rstrip("\n") + "\n\n" + f"[ui.sidebar.agents]  {MARK}\n" + body, rows


def plan_herdr(text, interval=30, key=None, defaults=None):
    """Return (new_text, key, notes). Raises SetupError when the config cannot be edited safely."""
    base_text = strip_marked(text)
    base = parse(base_text)
    if str(LAUNCHER) in base_text:
        raise SetupError("config.toml has hand-edited usage-tracker entries; remove them and run setup again")
    ui = base.get("ui") if isinstance(base.get("ui"), dict) else {}
    notes, ui_lines = [], []
    if "tab_bar_position" not in ui:
        ui_lines.append(f'tab_bar_position = "top"  {MARK}\n')
    elif ui["tab_bar_position"] != "top":
        notes.append(f'ui.tab_bar_position is "{ui["tab_bar_position"]}"; left unchanged, so usage shows on that side')
    if "tab_bar_right_separator" not in ui:  # a wider gap between accounts (Herdr trims our own spaces)
        ui_lines.append(f'tab_bar_right_separator = "{SEPARATOR}"  {MARK}\n')
    entries = status_entries(interval)
    entry_line = "".join(f"  {inline(entry)},  {MARK}\n" for entry in entries)
    lines = base_text.splitlines(keepends=True)
    ui_start = _find_header(lines, "ui")
    new = base_text
    if "tab_bar_right" in ui:
        if ui_start is None:
            raise SetupError("ui.tab_bar_right is set outside a [ui] table; add the entry manually")
        new = _append_to_array(new, lines, ui_start, entry_line)
    else:
        ui_lines += [f"tab_bar_right = [  {MARK}\n", entry_line, f"]  {MARK}\n"]
    if ui_lines:
        lines = new.splitlines(keepends=True)
        if ui_start is None:
            new = new.rstrip("\n") + ("\n\n" if new.strip() else "") + f"[ui]  {MARK}\n" + "".join(ui_lines)
        else:
            new = "".join(lines[:ui_start + 1] + ui_lines + lines[ui_start + 1:])
    defaults = defaults if defaults is not None else default_bindings()
    key = check_key(base, key, defaults)
    bindings = [key_entry(key)]
    used = used_keys(base, defaults)
    refresh = next((k for k in REFRESH_KEYS if k not in used and k != key.strip().lower()), None)
    if refresh:
        bindings.append(refresh_key_entry(refresh))
        notes.append(f"{refresh} refreshes every account now")
    else:
        notes.append(f"{', '.join(REFRESH_KEYS)} are bound, so there is no refresh shortcut; "
                     "use the Usage: refresh now action")
    for binding in bindings:
        new = (new.rstrip("\n") + "\n\n" + f"[[keys.command]]  {MARK}\n" + f"key = {toml_value(binding['key'])}  {MARK}\n"
               + f'type = "shell"  {MARK}\n' + f"command = {toml_value(binding['command'])}  {MARK}\n")
    expected = copy.deepcopy(base)
    expected.setdefault("ui", {}).setdefault("tab_bar_position", "top")
    expected["ui"].setdefault("tab_bar_right_separator", SEPARATOR)
    expected["ui"]["tab_bar_right"] = list(ui.get("tab_bar_right", [])) + entries
    expected.setdefault("keys", {}).setdefault("command", []).extend(bindings)
    sidebar = ui.get("sidebar", {})
    agents = sidebar.get("agents", {}) if isinstance(sidebar, dict) else None
    rows = agents.get("rows", AGENT_ROWS) if isinstance(agents, dict) else None
    if not isinstance(rows, list):
        notes.append("ui.sidebar.agents is set in a form this setup cannot edit; context meters left out")
    elif len(rows) >= MAX_ROWS:
        notes.append(f"ui.sidebar.agents already has {MAX_ROWS} rows; context meters left out")
    else:
        new, rows = _add_context_meter(new, agents, context_tokens(base))
        expected["ui"].setdefault("sidebar", {}).setdefault("agents", {})["rows"] = rows
    if parse(new) != expected:
        raise SetupError("could not add the entries without changing other settings; add them manually")
    toast = ui.get("toast") if isinstance(ui.get("toast"), dict) else {}
    if toast.get("delivery", "off") == "off":
        notes.append('Herdr notifications are off, so low-limit alerts will not show; set [ui.toast] '
                     'delivery = "herdr" (or "system") to see them')
    return new, key, notes


def parse(text):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"Herdr config.toml is not valid TOML ({exc}); fix it first") from None


def used_keys(cfg, defaults):
    keys = cfg.get("keys") if isinstance(cfg.get("keys"), dict) else {}
    bindings = {**defaults, **{k: v for k, v in keys.items() if isinstance(v, str)}}
    used = {v.strip().lower(): k for k, v in bindings.items() if v}
    for custom in keys.get("command", []) if isinstance(keys.get("command"), list) else []:
        if isinstance(custom, dict) and custom.get("key"):
            used[str(custom["key"]).strip().lower()] = f"custom command {custom.get('command', '')!r}"
    return used


def check_key(cfg, key, defaults):
    """The requested shortcut, or the first free preferred one; never one that is already bound."""
    used = used_keys(cfg, defaults)
    if key:
        if key.strip().lower() in used:
            raise SetupError(f"{key} is already bound to {used[key.strip().lower()]}; choose another with --key")
        return key
    free = next((k for k in PREFERRED_KEYS if k not in used), None)
    if not free:
        raise SetupError(f"{', '.join(PREFERRED_KEYS)} are all bound; choose a shortcut with --key")
    return free


def backup(path):
    target = path.with_name(f"{path.name}.usage-tracker-{time.strftime('%Y%m%d-%H%M%S')}.bak")
    shutil.copy2(path, target)
    return target


def write_text(path, text):
    tmp = path.with_name(f".{path.name}.usage-tracker.tmp")
    tmp.write_text(text)
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


# ---- Claude Code statusline bridge -----------------------------------------------------------

def bridge_command(profile_id, original):
    base = command("claude-statusline", "--profile", profile_id)
    return base + (f" --then {shlex.quote(original)}" if original else "")


def _is_bridge(cmd):
    return " claude-statusline --profile " in (cmd or "")


def _read_settings(path):
    text = path.read_text() if path.exists() else ""
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise SetupError(f"{path} is not a JSON object; left untouched")
    return data


def install_statusline(profile, apply):
    """Wrap the profile's statusLine command so Claude's rate_limits are saved first."""
    path = Path(profile["dir"]) / "settings.json"
    data = _read_settings(path)
    line = data.get("statusLine") if isinstance(data.get("statusLine"), dict) else {}
    original = line.get("command") or ""
    if _is_bridge(original):
        return None
    if apply:
        if path.exists():
            backup(path)
        data["statusLine"] = {**line, "type": "command", "command": bridge_command(profile["id"], original)}
        cache.write_json(path, data, indent=2)
    return f"{path}: wrap statusLine command ({'currently ' + repr(original) if original else 'none set'})"


def uninstall_statusline(profile, apply):
    path = Path(profile["dir"]) / "settings.json"
    data = _read_settings(path)
    line = data.get("statusLine") if isinstance(data.get("statusLine"), dict) else {}
    if not _is_bridge(line.get("command")):
        return None
    args = shlex.split(line["command"])
    original = args[args.index("--then") + 1] if "--then" in args[:-1] else None
    if apply:
        backup(path)
        if original:
            data["statusLine"] = {**line, "command": original}
        else:
            data.pop("statusLine")
        cache.write_json(path, data, indent=2)
    return f"{path}: restore statusLine command ({repr(original) if original else 'remove'})"


# ---- setup / uninstall -----------------------------------------------------------------------

def starter_config(profiles):
    """A commented plugin config listing the discovered accounts."""
    from .providers import PROVIDERS

    lines = ["# Usage Tracker settings. Every setting is optional.", "",
             "[status]",
             "# Accounts shown in the tab bar, in order (profile ids). Empty = every enabled profile.",
             "order = []",
             '# "compact" = one window per account, "detailed" = every window, "split" = the 5-hour and',
             '# weekly windows in one bar (top half 5h, bottom half weekly). Profiles can override it.',
             'format = "compact"',
             '# Window for compact mode: "max" (highest % used), "session", "weekly", "monthly",',
             '# or a window id such as "weekly_opus".',
             'window = "max"',
             '# Progress bar before each percentage: "blocks" (███░░ bar in the tab bar text color),',
             '# "color" (🟩 🟨 🟥 squares; the tab bar cannot color text) or "none". Width in columns.',
             'bar = "blocks"',
             "bar_width = 10",
             "# Room for all accounts together, in columns; with less, details are left out.",
             "max_width = 120", "",
             "[alerts]",
             "# A Herdr notification when a limit passes one of these (% used), once per window; [] = off.",
             "thresholds = [80, 95]", "",
             "[context]",
             '# Symbol before each agent\'s context meter in the Agents panel (⛁, as in Claude Code\'s /context).',
             'icon = "⛁"', "",
             "[refresh]",
             "# How often live limits are re-read, and when data counts as stale (seconds).",
             "interval_seconds = 300",
             "stale_seconds = 1800", "",
             "# Accounts. Add one [[profiles]] per account; `dir` is that account's config directory.",
             "# Set enabled = false to hide one, and icon = \"...\" for its tab-bar logo (with a Nerd Font,",
             '# "\\uEC82" is the Claude logo and "\\uEC81" the OpenAI one). See config.example.toml for more.']
    for p in profiles:
        lines += ["", "[[profiles]]", f"id = {toml_value(p['id'])}", f"provider = {toml_value(p['provider'])}",
                  f"label = {toml_value(p['label'])}", f"dir = {toml_value(p['dir'])}"]
    for kind, mod in PROVIDERS.items():
        directory = config.expand(mod.DEFAULT_DIR)
        if not getattr(mod, "AUTO_ENABLE", True) and mod.detect(directory):
            lines += ["", f"# Found {directory}. {mod.__doc__.splitlines()[0]}",
                      "# Uncomment to enable (it reads that CLI's stored sign-in):",
                      "# [[profiles]]", f'# id = "{kind}"', f'# provider = "{kind}"', f'# dir = "{directory}"']
    lines.append("")
    return "\n".join(lines)


def setup(apply=False, claude_statusline=False, key=None, interval=30, out=print):
    cfg = config.load()
    path = herdr_config_path()
    text = path.read_text() if path.exists() else ""
    new_text, key, notes = plan_herdr(text, interval, key)
    steps = []
    if new_text != text:
        steps.append(f"Herdr config {path}: top-right usage entries (one per account, every {interval}s), "
                     f"context meters in the Agents panel, dashboard shortcut {key} and a refresh shortcut")
    registration = plugin_registration()
    if not registration or Path(registration.get("plugin_root", "")) != ROOT:
        steps.append(f"register the plugin: herdr plugin link {ROOT}")
    starter = config.config_dir() / "config.toml"
    if not starter.exists():
        steps.append(f"write starter plugin config {starter}")
    bridges = []
    if claude_statusline:
        bridges = [(p, install_statusline(p, apply=False)) for p in cfg["profiles"]
                   if p["provider"] == "claude" and p["enabled"]]
        steps += [change for _, change in bridges if change]
    out("Planned changes:" if steps else "Already set up; nothing to change.")
    for step in steps:
        out(f"  - {step}")
    for note in notes:
        out(f"  note: {note}")
    if not apply or not steps:
        return key, steps
    if new_text != text:
        if path.exists():
            out(f"Backed up Herdr config to {backup(path)}")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, new_text)
    if not registration or Path(registration.get("plugin_root", "")) != ROOT:
        code, _, err = herdr("plugin", "link", str(ROOT))
        if code != 0:
            raise SetupError(f"herdr plugin link failed: {err.strip()[:300]}")
    if not starter.exists():
        starter.parent.mkdir(parents=True, exist_ok=True)
        starter.write_text(starter_config(cfg["profiles"]))
    for profile, change in bridges:
        if change:
            install_statusline(profile, apply=True)
    status, diagnostics = reload_herdr()
    out(f"Herdr config reload: {status}")
    for diagnostic in diagnostics:
        out(f"  herdr: {diagnostic}")
    return key, steps


def uninstall(apply=False, purge=False, out=print):
    cfg = config.load()
    path = herdr_config_path()
    text = path.read_text() if path.exists() else ""
    new_text = strip_marked(text)
    parse(new_text)
    steps = []
    if new_text != text:
        steps.append(f"remove usage-tracker lines from {path}")
    if str(LAUNCHER) in new_text:
        out(f"  note: {path} still mentions {LAUNCHER} in lines without the {MARK} marker; remove them by hand")
    restores = [(p, uninstall_statusline(p, apply=False)) for p in cfg["profiles"] if p["provider"] == "claude"]
    steps += [change for _, change in restores if change]
    registration = plugin_registration()
    # A GitHub install is removed with `uninstall` (which deletes Herdr's checkout); a local link with `unlink`.
    remove = "uninstall" if (registration or {}).get("source", {}).get("kind") == "github" else "unlink"
    if registration:
        steps.append(f"unregister the plugin: herdr plugin {remove} {PLUGIN_ID}")
    if purge:
        steps.append(f"delete {config.config_dir()} and {config.state_dir()}")
    out("Planned removal:" if steps else "Nothing to remove.")
    for step in steps:
        out(f"  - {step}")
    if not apply or not steps:
        return
    if new_text != text:
        out(f"Backed up Herdr config to {backup(path)}")
        write_text(path, new_text)
    for profile, change in restores:
        if change:
            uninstall_statusline(profile, apply=True)
    if registration:
        herdr("plugin", remove, PLUGIN_ID)
    if purge:
        for directory in (config.config_dir(), config.state_dir()):
            shutil.rmtree(directory, ignore_errors=True)
    status, diagnostics = reload_herdr()
    out(f"Herdr config reload: {status}")
    for diagnostic in diagnostics:
        out(f"  herdr: {diagnostic}")
