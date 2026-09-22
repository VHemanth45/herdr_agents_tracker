import time
import unittest
from unittest import mock

from helpers import IsolatedTest
from usage_tracker import cache, collect, config, dashboard, model
from usage_tracker.providers import claude


class FakeScreen:
    def __init__(self, height, width):
        self.size, self.cells = (height, width), {}

    def getmaxyx(self):
        return self.size

    def addstr(self, y, x, text, attr=0):
        assert x + len(text) < self.size[1], "drew past the right edge"
        self.cells[y] = self.cells.get(y, "").ljust(x) + text

    def erase(self):
        self.cells = {}

    def refresh(self):
        pass

    def text(self):
        return "\n".join(self.cells.get(y, "") for y in range(self.size[0]))


class Dashboard(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.copy_fixture("claude")
        self.copy_fixture("codex")
        self.write_config(f"""
[[profiles]]
id = "claude"
provider = "claude"
dir = "{self.tmp}/claude"

[[profiles]]
id = "codex"
provider = "codex"
label = "Codex Work"
dir = "{self.tmp}/codex"
""")
        now = time.time()
        claude.record_statusline("claude", {"rate_limits": {"five_hour": {"used_percentage": 92, "resets_at": now + 4000},
                                                            "seven_day": {"used_percentage": 30, "resets_at": now + 90000}}},
                                 self.state, now)
        cache.save(self.state, {"codex": {"state": "error", "error": "codex app-server timed out", "updated_at": now - 7200,
                                          "windows": [model.window_for(10080, 63, now + 400000)], "source": "codex app-server"}})
        collect.update_history(config.load()["profiles"], self.state)

    def render(self, height, width, keys=()):
        screen = FakeScreen(height, width)
        board = dashboard.Dashboard(screen)
        board.range = 3  # all history: the fixtures are from September 2026
        for key in keys:
            board.handle(ord(key))
        board.draw()
        return board, screen

    def test_renders_limits_activity_and_honest_states(self):
        board, screen = self.render(60, 140)
        body = "\n".join("".join(text for text, _ in line) if isinstance(line, list) else line for line in board.body(140))
        for expected in ("Claude", "Session limit (5h) · resets in", "Weekly limit (7d)", "92% used · 8% left",
                         "Codex Work", "STALE", "LAST REFRESH FAILED", "Requests", "BY MODEL", "claude-opus-5",
                         "RECENT SESSIONS", "HISTORY COVERAGE", "not the subscription allowance above"):
            self.assertIn(expected, body)
        self.assertIn("Usage", screen.text())

    def test_limit_bars_are_smooth_and_colored_by_level(self):
        board, _ = self.render(60, 140)
        line = next(l for l in board.body(70) if isinstance(l, list) and " 92% used" in "".join(t for t, _ in l))
        self.assertEqual(line[1:5], [("█" * 38, "bad"), ("▋", "bad_edge"), ("░░░", "track"),  # 92% of 42 cells
                                     ("  92% used", "bad_value")])
        fakes = dict(curs_set=mock.DEFAULT, use_default_colors=mock.DEFAULT, init_pair=mock.DEFAULT)
        with mock.patch.multiple(dashboard.curses, create=True, COLORS=256, has_colors=lambda: True,
                                 color_pair=lambda n: n << 8, **fakes) as patched:
            board.setup_colors()
        patched["init_pair"].assert_any_call(5, dashboard.TRACK, -1)  # solid dark track on 256-color terminals
        self.assertEqual(board.track, "█")

    def test_narrow_terminals_do_not_overflow(self):
        for width in (40, 60, 80):
            self.render(24, width)  # FakeScreen asserts nothing is drawn past the edge

    def test_tiny_terminal_shows_a_message(self):
        _, screen = self.render(6, 30)
        self.assertIn("too small", screen.text())

    def test_filters_scrolling_and_help(self):
        board, _ = self.render(20, 100, keys="f")
        self.assertEqual([p["id"] for p in board.profiles()], ["claude"])
        board.handle(ord("p"))
        self.assertEqual(board.provider, "claude")
        board.handle(ord("G"))
        board.draw()
        self.assertGreater(board.scroll, 0)
        board.handle(ord("?"))
        self.assertTrue(board.help)
        self.assertFalse(board.handle(ord("q")))

    def test_refresh_key_starts_one_background_collector(self):
        board, _ = self.render(20, 100)
        with mock.patch.object(dashboard.cache, "spawn") as spawn:
            board.handle(ord("r"))
            board.handle(ord("r"))
        spawn.assert_called_once_with("refresh", "--force")


if __name__ == "__main__":
    unittest.main()
