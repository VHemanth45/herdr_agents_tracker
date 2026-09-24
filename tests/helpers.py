"""Shared test setup: isolated plugin dirs so tests never touch real accounts or Herdr config."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from anywhere: python3 -m unittest discover -s tests

FIXTURES = Path(__file__).parent / "fixtures"
NOW = 1790060000.0  # 2026-09-22, a fixed clock for deterministic output


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-tracker-test-"))
        self.state = self.tmp / "state"
        self.conf = self.tmp / "config"
        self.env = {k: os.environ.get(k) for k in ("HERDR_PLUGIN_STATE_DIR", "HERDR_PLUGIN_CONFIG_DIR",
                                                   "HERDR_CONFIG_PATH", "CODEX_BIN", "FAKE_CODEX_MODE",
                                                   "CLAUDE_BIN", "FAKE_CLAUDE_MODE", "HERDR_SOCKET_PATH",
                                                   "GH_BIN", "FAKE_GH_MODE", "AMP_BIN", "FAKE_AMP_MODE")}
        os.environ["HERDR_PLUGIN_STATE_DIR"] = str(self.state)
        os.environ["HERDR_SOCKET_PATH"] = str(self.tmp / "no-herdr.sock")  # never reach a live Herdr
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = str(self.conf)
        os.environ["CLAUDE_BIN"] = str(FIXTURES / "claude/fake-claude.py")  # never the real Claude Code
        os.environ["GH_BIN"] = str(FIXTURES / "copilot/fake-gh.py")  # never the real GitHub account
        os.environ["AMP_BIN"] = str(FIXTURES / "amp/fake-amp.py")

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def copy_fixture(self, name):
        target = self.tmp / name
        shutil.copytree(FIXTURES / name, target) if (FIXTURES / name).is_dir() else shutil.copy(FIXTURES / name, target)
        return target

    def write_config(self, text):
        self.conf.mkdir(parents=True, exist_ok=True)
        (self.conf / "config.toml").write_text(text)


def cfg(**status):
    """A minimal loaded-config dict for formatter tests."""
    return {"status": {"order": [], "format": "compact", "window": "max", "max_width": 200, "bar": "none",
                       "bar_width": 10, **status},
            "refresh": {"interval_seconds": 300, "stale_seconds": 1800}, "alerts": {"thresholds": [80, 95]},
            "context": {"icon": "⛁"}, "resume": {"enabled": False, "prompt": "continue"},
            "error": None}


def profile(pid, provider="claude", label=None, window=None, enabled=True, format=None, icon=""):
    return {"id": pid, "provider": provider, "label": label or pid.title(), "dir": "/nonexistent",
            "enabled": enabled, "window": window, "format": format, "icon": icon}
