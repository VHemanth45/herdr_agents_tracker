import json
import os
import subprocess
import sys
import tomllib
import unittest
from unittest import mock
from pathlib import Path

from helpers import FIXTURES, IsolatedTest
from usage_tracker import integrate
from usage_tracker.integrate import SetupError, plan_herdr, strip_marked

DEFAULTS = {"help": "prefix+?", "toggle_sidebar": "prefix+b", "zoom": "prefix+z", "new_tab": "prefix+c"}
ROOT = Path(__file__).resolve().parent.parent


def fixture(name):
    return (FIXTURES / "herdr" / name).read_text()


def ours(parsed):
    bar = [e for e in parsed.get("ui", {}).get("tab_bar_right", []) if "usage-tracker" in e.get("command", "")]
    keys = [k for k in parsed.get("keys", {}).get("command", []) if "usage-tracker" in k.get("command", "")]
    return bar, keys


class HerdrConfig(unittest.TestCase):
    def plan(self, name, **kwargs):
        return plan_herdr(fixture(name), defaults=DEFAULTS, **kwargs)

    def test_adds_entries_and_preserves_everything_else(self):
        text, key, notes = self.plan("user-style.toml")
        before, after = tomllib.loads(fixture("user-style.toml")), tomllib.loads(text)
        bar, keys = ours(after)
        self.assertEqual([e["command"].split("bin/usage-tracker ")[1] for e in bar],
                         ["status --part 1", "status --part 2", "status --part 3-"])  # one entry per account
        self.assertEqual({(e["interval_seconds"], e["timeout_seconds"]) for e in bar}, {(30, 2)})
        self.assertEqual((key, keys[0]["key"], keys[0]["type"]), ("prefix+u", "prefix+u", "shell"))
        self.assertEqual(after["ui"]["tab_bar_position"], "top")
        self.assertEqual(after["ui"]["tab_bar_right_separator"], "   ")  # only set when the user has none
        self.assertEqual(after["theme"], before["theme"])
        self.assertEqual(after["ui"]["toast"], before["ui"]["toast"])
        self.assertEqual(after["ui"]["status_indicators"], "symbols")
        self.assertTrue(text.startswith(fixture("user-style.toml").splitlines()[0]))

    def test_repeated_setup_is_idempotent(self):
        once, _, _ = self.plan("user-style.toml")
        twice, _, _ = plan_herdr(once, defaults=DEFAULTS)
        self.assertEqual(once, twice)

    def test_existing_status_entries_and_keybindings_are_kept(self):
        text, key, _ = self.plan("existing-entries.toml")
        before, after = tomllib.loads(fixture("existing-entries.toml")), tomllib.loads(text)
        self.assertEqual(after["ui"]["tab_bar_right"][:2], before["ui"]["tab_bar_right"])
        self.assertEqual(len(after["ui"]["tab_bar_right"]), 5)
        self.assertEqual(after["ui"]["tab_bar_right_separator"], before["ui"]["tab_bar_right_separator"])
        self.assertEqual(key, "prefix+alt+u")  # prefix+u is the user's sidebar key; prefix+alt+g is lazygit
        self.assertEqual(after["keys"]["toggle_sidebar"], "prefix+u")
        self.assertEqual(after["keys"]["command"][0], before["keys"]["command"][0])
        self.assertIn("# a comment inside the array", text)
        self.assertEqual(tomllib.loads(strip_marked(text)), before)

    def test_single_line_array_and_missing_ui_table(self):
        for name in ("single-line.toml", "no-ui.toml"):
            text, _, _ = self.plan(name)
            before, after = tomllib.loads(fixture(name)), tomllib.loads(text)
            self.assertEqual(len(ours(after)[0]), 3, name)
            self.assertEqual(tomllib.loads(strip_marked(text)), before, name)

    def test_bottom_tab_bar_is_respected(self):
        text, _, notes = self.plan("bottom.toml")
        self.assertEqual(tomllib.loads(text)["ui"]["tab_bar_position"], "bottom")
        self.assertTrue(any("bottom" in note for note in notes))

    def test_refresh_shortcut_is_added_when_free(self):
        text, key, notes = self.plan("user-style.toml")
        keys = ours(tomllib.loads(text))[1]
        self.assertEqual([k["key"] for k in keys], ["prefix+u", "prefix+shift+u"])
        self.assertTrue(keys[1]["command"].endswith("bin/usage-tracker refresh --force --background"))
        self.assertIn("prefix+shift+u refreshes every account now", notes)
        taken = fixture("user-style.toml").replace("[ui]", '[keys]\nzoom = "prefix+shift+u"\n\n[ui]', 1)
        text, _, _ = plan_herdr(taken, defaults=DEFAULTS)
        self.assertEqual([k["key"] for k in ours(tomllib.loads(text))[1]], ["prefix+u", "prefix+shift+y"])
        taken = taken.replace('zoom = "prefix+shift+u"', 'zoom = "prefix+shift+u"\nhelp = "prefix+shift+y"')
        text, _, notes = plan_herdr(taken, defaults=DEFAULTS)
        self.assertEqual(len(ours(tomllib.loads(text))[1]), 1)
        self.assertTrue(any("no refresh shortcut" in note for note in notes))

    def test_requested_shortcut_must_be_free(self):
        with self.assertRaises(SetupError):
            self.plan("user-style.toml", key="prefix+z")
        text, key, _ = self.plan("user-style.toml", key="prefix+alt+y")
        self.assertEqual(key, "prefix+alt+y")

    def test_invalid_or_hand_edited_configs_are_refused(self):
        with self.assertRaises(SetupError):
            self.plan("invalid.toml")
        edited = self.plan("user-style.toml")[0].replace("  # usage-tracker", "")
        with self.assertRaises(SetupError):
            plan_herdr(edited, defaults=DEFAULTS)

    def test_uninstall_removes_only_our_lines(self):
        for name in ("user-style.toml", "existing-entries.toml", "no-ui.toml", "bottom.toml"):
            original = fixture(name)
            removed = strip_marked(self.plan(name)[0])
            self.assertEqual(tomllib.loads(removed), tomllib.loads(original), name)
            self.assertNotIn("usage-tracker", removed, name)
        # Our own insertion pattern round-trips to the exact original text.
        self.assertEqual(strip_marked(self.plan("user-style.toml")[0]), fixture("user-style.toml"))

    def test_context_meter_joins_the_agent_rows(self):
        text, _, notes = self.plan("user-style.toml")
        rows = tomllib.loads(text)["ui"]["sidebar"]["agents"]["rows"]
        self.assertEqual(rows[0], ["state_icon", "workspace", "tab"])  # Herdr's default rows, meter after the agent
        self.assertEqual(rows[1], ["agent", {"token": "$usage_ctx_ok", "fg": "#928374"},  # gruvbox colors
                                   {"token": "$usage_ctx_warn", "fg": "#fabd2f"}, {"token": "$usage_ctx_hot", "fg": "#fb4934"}])
        self.assertEqual(plan_herdr(text, defaults=DEFAULTS)[0], text)  # idempotent
        base = fixture("user-style.toml") + '\n[ui.sidebar.agents]\nrow_gap = 1\n'
        for extra, first in (("", ["state_icon", "workspace", "tab"]), ('rows = [["agent", "tab"]]\n', ["agent", "tab"])):
            text, _, _ = plan_herdr(base + extra, defaults=DEFAULTS)
            agents = tomllib.loads(text)["ui"]["sidebar"]["agents"]
            self.assertEqual((agents["row_gap"], agents["rows"][0]), (1, first))  # the user's settings stay
            self.assertEqual(agents["rows"][-1][-1]["token"], "$usage_ctx_hot")
            self.assertEqual(tomllib.loads(strip_marked(text)), tomllib.loads(base + extra))
        full = base + "rows = [" + ", ".join(['["agent"]'] * 16) + "]\n"
        text, _, notes = plan_herdr(full, defaults=DEFAULTS)
        self.assertEqual(len(tomllib.loads(text)["ui"]["sidebar"]["agents"]["rows"]), 16)
        self.assertIn("context meters left out", " ".join(notes))

    def test_default_bindings_are_parsed_from_default_config(self):
        sample = '[keys]\n# prefix = "ctrl+b"\n# help = "prefix+?"\n# open_worktree = ""  # optional\n[ui]\n# accent = "cyan"\n'
        original = integrate.herdr
        integrate.herdr = lambda *args, **kw: (0, sample, "")
        try:
            self.assertEqual(integrate.default_bindings(), {"prefix": "ctrl+b", "help": "prefix+?"})
        finally:
            integrate.herdr = original


class Uninstall(IsolatedTest):
    def test_github_installs_are_uninstalled_and_local_links_unlinked(self):
        os.environ["HERDR_CONFIG_PATH"] = str(self.tmp / "herdr.toml")
        self.write_config("profiles = []\n")
        for kind, command in (("github", "uninstall"), ("local", "unlink")):
            lines = []
            with mock.patch.object(integrate, "plugin_registration", return_value={"source": {"kind": kind}}):
                integrate.uninstall(out=lines.append)  # dry run
            self.assertIn(f"herdr plugin {command} herdr_agents_tracker", "\n".join(lines))


class ClaudeBridge(IsolatedTest):
    def settings(self, data):
        directory = self.tmp / "claude"
        directory.mkdir(exist_ok=True)
        if data is not None:
            (directory / "settings.json").write_text(json.dumps(data))
        return {"id": "claude", "provider": "claude", "dir": str(directory)}, directory / "settings.json"

    def test_wraps_and_restores_the_users_statusline(self):
        profile, path = self.settings({"theme": "dark", "statusLine": {"type": "command",
                                                                        "command": "~/.claude/statusline.sh", "padding": 0}})
        self.assertIsNotNone(integrate.install_statusline(profile, apply=True))
        wrapped = json.loads(path.read_text())
        self.assertIn("claude-statusline --profile claude --then", wrapped["statusLine"]["command"])
        self.assertEqual((wrapped["theme"], wrapped["statusLine"]["padding"]), ("dark", 0))
        self.assertIsNone(integrate.install_statusline(profile, apply=True))  # idempotent
        self.assertEqual(len(list(path.parent.glob("settings.json.usage-tracker-*.bak"))), 1)
        integrate.uninstall_statusline(profile, apply=True)
        self.assertEqual(json.loads(path.read_text())["statusLine"],
                         {"type": "command", "command": "~/.claude/statusline.sh", "padding": 0})

    def test_without_a_previous_statusline_uninstall_removes_it(self):
        profile, path = self.settings(None)
        integrate.install_statusline(profile, apply=True)
        self.assertTrue(json.loads(path.read_text())["statusLine"]["command"].endswith("--profile claude"))
        integrate.uninstall_statusline(profile, apply=True)
        self.assertNotIn("statusLine", json.loads(path.read_text()))

    def test_invalid_settings_are_left_alone(self):
        profile, path = self.settings(None)
        path.write_text("{ not json")
        with self.assertRaises(SetupError):
            integrate.install_statusline(profile, apply=True)
        self.assertEqual(path.read_text(), "{ not json")

    def test_bridge_saves_limits_and_passes_input_through(self):
        payload = (FIXTURES / "claude/statusline-input.json").read_bytes()
        env = dict(os.environ, HERDR_PLUGIN_STATE_DIR=str(self.state), PYTHONPATH=str(ROOT))
        done = subprocess.run([sys.executable, "-P", "-m", "usage_tracker", "claude-statusline", "--profile", "claude",
                               "--then", "cat; echo tail"], input=payload, capture_output=True, env=env, timeout=10)
        self.assertEqual(done.stdout, payload + b"tail\n")
        snapshot = json.loads((self.state / "claude/claude.statusline.json").read_text())
        self.assertEqual([w["label"] for w in snapshot["windows"]], ["5h", "7d", "7d Opus"])
        broken = subprocess.run([sys.executable, "-P", "-m", "usage_tracker", "claude-statusline", "--profile", "claude",
                                 "--then", "echo still-works"], input=b"garbage", capture_output=True, env=env, timeout=10)
        self.assertEqual((broken.returncode, broken.stdout), (0, b"still-works\n"))


if __name__ == "__main__":
    unittest.main()
