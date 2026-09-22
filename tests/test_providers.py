import io
import json
import os
import sqlite3
import time
import unittest
import urllib.error
from unittest import mock

from helpers import FIXTURES, NOW, IsolatedTest
from usage_tracker import cache
from usage_tracker.model import ProviderError
from usage_tracker.providers import claude, codex, grok, opencode


def load(name):
    return json.loads((FIXTURES / name).read_text())


class Claude(IsolatedTest):
    def profile(self, directory=None):
        return {"id": "claude", "provider": "claude", "label": "Claude", "dir": str(directory or self.tmp / "claude")}

    def test_statusline_rate_limits_become_windows(self):
        windows = claude.windows_from(load("claude/statusline-input.json")["rate_limits"])
        self.assertEqual([(w["id"], w["label"], w["used"], w["resets_at"]) for w in windows],
                         [("session", "5h", 17.0, 1790080000), ("weekly", "7d", 41.6, 1790500000),
                          ("weekly_opus", "7d Opus", 8.0, 1790500000)])

    def test_bridge_keeps_the_higher_reading_within_one_window(self):
        payload = load("claude/statusline-input.json")
        self.assertTrue(claude.record_statusline("claude", payload, self.state, now=NOW))
        older = json.loads(json.dumps(payload))
        older["rate_limits"]["five_hour"]["used_percentage"] = 9  # idle process re-rendering an old value
        claude.record_statusline("claude", older, self.state, now=NOW + 5)
        snap = claude.quick(self.profile(), self.state)
        self.assertEqual(snap["windows"][0]["used"], 17)
        # After the window resets (new reset time) the lower value is the truth.
        older["rate_limits"]["five_hour"]["resets_at"] = 1790080000 + 5 * 3600
        claude.record_statusline("claude", older, self.state, now=NOW + 10)
        self.assertEqual(claude.quick(self.profile(), self.state)["windows"][0]["used"], 9)

    def test_bridge_ignores_payloads_without_limits(self):
        self.assertFalse(claude.record_statusline("claude", {"model": {}}, self.state))
        self.assertFalse(claude.snapshot_path(self.state, "claude").exists())

    def test_limits_come_from_claude_code_itself(self):
        snap = claude.limits(self.profile(), self.state)  # the fake `claude -p` answers get_usage
        self.assertEqual((snap["source"], snap["plan"]), ("Claude Code", "Max"))
        self.assertEqual([(w["id"], w["label"], w["used"]) for w in snap["windows"]],
                         [("session", "5h", 18.0), ("weekly", "7d", 19.0), ("weekly_fable", "7d Fable", 0.0)])

    def test_without_claude_code_the_last_reported_limits_are_used(self):
        os.environ["FAKE_CLAUDE_MODE"] = "signed-out"
        with self.assertRaises(ProviderError) as err:
            claude.limits(self.profile(), self.state)
        self.assertEqual(err.exception.state, "auth")
        directory = self.tmp / "claude"
        directory.mkdir()
        (directory / ".claude.json").write_text((FIXTURES / "claude/claude.json").read_text())
        snap = claude.limits(self.profile(directory), self.state)  # only Claude's own cache exists
        self.assertEqual((snap["source"], snap["plan"]), ("Claude Code usage cache", "Max 5x"))
        self.assertEqual([w["label"] for w in snap["windows"]], ["5h", "7d"])  # null sonnet window dropped
        claude.record_statusline("claude", load("claude/statusline-input.json"), self.state, now=time.time())
        self.assertEqual(claude.limits(self.profile(directory), self.state)["source"], "Claude Code statusline")

    def test_api_key_sign_in_and_hung_cli_are_reported(self):
        os.environ["FAKE_CLAUDE_MODE"] = "apikey"
        with self.assertRaises(ProviderError) as err:
            claude.limits(self.profile(), self.state)
        self.assertEqual(err.exception.state, "unavailable")  # no invented subscription limits
        os.environ["FAKE_CLAUDE_MODE"] = "hang"
        started = time.monotonic()
        with mock.patch.object(claude, "USAGE_TIMEOUT", 0.5), self.assertRaises(ProviderError) as err:
            claude.limits(self.profile(), self.state)
        self.assertEqual(err.exception.message, "claude timed out")
        self.assertLess(time.monotonic() - started, 5)

    def test_cache_from_another_account_is_ignored(self):
        data = load("claude/claude.json")
        data["cachedUsageUtilization"]["accountUuid"] = "someone-else"
        self.assertIsNone(claude.from_usage_cache(data))

    def test_no_data_is_reported_not_zero(self):
        with mock.patch.object(claude, "find_cli", return_value=None), self.assertRaises(ProviderError) as err:
            claude.limits(self.profile(self.tmp / "missing"), self.state)
        self.assertEqual(err.exception.state, "unavailable")


class Codex(IsolatedTest):
    def setUp(self):
        super().setUp()
        os.environ["CODEX_BIN"] = str(FIXTURES / "codex/fake-app-server.py")
        self.home = self.copy_fixture("codex")
        self.profile = {"id": "codex", "provider": "codex", "label": "Codex", "dir": str(self.home)}

    def test_app_server_limits_include_every_bucket(self):
        snap = codex.limits(self.profile, self.state)
        self.assertEqual(snap["source"], "codex app-server")
        self.assertEqual(snap["plan"], "Pro Lite")
        self.assertEqual([(w["label"], w["used"]) for w in snap["windows"]],
                         [("5h", 20.0), ("7d", 63.0), ("7d GPT-5 mini", 5.0)])

    def test_api_key_accounts_get_no_invented_limits(self):
        os.environ["FAKE_CODEX_MODE"] = "apikey"
        with self.assertRaises(ProviderError) as err:
            codex.limits(self.profile, self.state)
        self.assertEqual(err.exception.state, "unavailable")

    def test_signed_out_and_auth_errors(self):
        for mode in ("signed-out", "auth-error"):
            os.environ["FAKE_CODEX_MODE"] = mode
            with self.assertRaises(ProviderError) as err:
                codex.limits(self.profile, self.state)
            self.assertEqual(err.exception.state, "auth", mode)

    def test_hung_cli_times_out_and_falls_back_to_the_session_log(self):
        os.environ["FAKE_CODEX_MODE"] = "hang"
        started = time.monotonic()
        with mock.patch.object(codex, "RPC_TIMEOUT", 1):
            snap = codex.limits(self.profile, self.state)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(snap["source"], "Codex session log (last recorded turn)")
        self.assertEqual([(w["label"], w["used"]) for w in snap["windows"]], [("5h", 12.0), ("7d", 55.0)])
        self.assertEqual(snap["plan"], "Plus")

    def test_missing_cli_without_logs_is_unavailable(self):
        with mock.patch.object(codex, "find_cli", return_value=None), \
                mock.patch.object(codex, "rollout_limits", return_value=None):
            with self.assertRaises(ProviderError) as err:
                codex.limits(self.profile, self.state)
        self.assertEqual(err.exception.state, "unavailable")


class Grok(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.home = self.copy_fixture("grok")
        self.profile = {"id": "grok", "provider": "grok", "label": "Grok", "dir": str(self.home)}

    def opener(self, status=200, body=None, headers=None):
        def fake(request, timeout):
            self.request = request
            if status != 200:
                raise urllib.error.HTTPError(request.full_url, status, "err", headers or {}, None)
            return io.BytesIO(json.dumps(body or load("grok/billing.json")).encode())
        return fake

    def test_billing_windows(self):
        snap = grok.limits(self.profile, self.state, opener=self.opener())
        self.assertEqual([(w["id"], w["used"]) for w in snap["windows"]], [("weekly", 12.5), ("monthly", 25.0)])
        self.assertEqual(snap["plan"], "SuperGrok")
        self.assertEqual(self.request.get_header("X-xai-token-auth"), "xai-grok-cli")

    def test_rejected_token_is_auth_and_never_echoed(self):
        with self.assertRaises(ProviderError) as err:
            grok.limits(self.profile, self.state, opener=self.opener(401))
        self.assertEqual(err.exception.state, "auth")
        self.assertNotIn("test-token", err.exception.message)

    def test_rate_limit_honors_retry_after(self):
        with self.assertRaises(ProviderError) as err:
            grok.limits(self.profile, self.state, opener=self.opener(429, headers={"Retry-After": "900"}))
        self.assertEqual((err.exception.state, err.exception.retry_after), ("error", 900))

    def test_expired_and_missing_sign_in(self):
        auth = load("grok/auth.json")
        next(iter(auth.values()))["expires_at"] = "2020-01-01T00:00:00Z"
        (self.home / "auth.json").write_text(json.dumps(auth))
        with self.assertRaises(ProviderError) as err:
            grok.limits(self.profile, self.state, opener=self.opener())
        self.assertEqual(err.exception.state, "auth")
        (self.home / "auth.json").unlink()
        with self.assertRaises(ProviderError) as err:
            grok.limits(self.profile, self.state, opener=self.opener())
        self.assertEqual(err.exception.state, "unavailable")

    def test_not_auto_enabled(self):
        self.assertFalse(grok.AUTO_ENABLE)


def make_opencode_db(path, messages):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, "
                "time_updated INTEGER, data TEXT)")
    for mid, created, updated, data in messages:
        con.execute("INSERT OR REPLACE INTO message VALUES (?, 's', ?, ?, ?)", (mid, created, updated, json.dumps(data)))
    con.commit()
    con.close()


class OpenCode(IsolatedTest):
    def message(self, cost, minutes_ago, provider="opencode-go", tokens=None):
        created = int((time.time() - minutes_ago * 60) * 1000)
        return {"role": "assistant", "providerID": provider, "modelID": "kimi-k2", "cost": cost,
                "sessionID": "ses_1", "path": {"cwd": "/tmp/demo"}, "time": {"created": created},
                "tokens": tokens or {"input": 100, "output": 20, "reasoning": 5, "cache": {"read": 50, "write": 0}}}

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "opencode"
        self.home.mkdir()
        self.profile = {"id": "opencode", "provider": "opencode", "label": "OpenCode", "dir": str(self.home)}

    def test_go_allowance_is_an_estimate_from_recorded_costs(self):
        rows = [("m1", self.message(2.4, 60)), ("m2", self.message(3.0, 3 * 1440)), ("m3", self.message(9.0, 20 * 1440)),
                ("m4", self.message(50.0, 60, provider="anthropic"))]
        make_opencode_db(self.home / "opencode.db", [(m, d["time"]["created"], d["time"]["created"], d) for m, d in rows])
        snap = opencode.limits(self.profile, self.state)
        self.assertTrue(snap["estimated"])
        self.assertEqual([(w["label"], round(w["used"], 1)) for w in snap["windows"]],
                         [("5h", 20.0), ("7d", 18.0), ("30d", 24.0)])

    def test_without_go_usage_it_is_unavailable(self):
        make_opencode_db(self.home / "opencode.db", [("m1", 1, 1, self.message(1.0, 10, provider="openai"))])
        with self.assertRaises(ProviderError) as err:
            opencode.limits(self.profile, self.state)
        self.assertEqual(err.exception.state, "unavailable")

    def test_history_is_incremental_and_follows_updates(self):
        from usage_tracker import history
        first = self.message(0.5, 5)
        make_opencode_db(self.home / "opencode.db", [("m1", 1000, 1000, first)])
        with history.Store(history.db_path(self.state, "opencode")) as store:
            opencode.history(self.profile, store)
        grown = self.message(0.9, 5, tokens={"input": 100, "output": 60, "reasoning": 5, "cache": {"read": 50}})
        make_opencode_db(self.home / "opencode.db", [("m1", 1000, 2000, grown)])
        with history.Store(history.db_path(self.state, "opencode")) as store:
            opencode.history(self.profile, store)
        rows = history.rows(self.state, [self.profile])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["output"], rows[0]["reasoning"], rows[0]["backend"]),
                         (65, 5, "opencode-go"))


class CacheRoundTrip(IsolatedTest):
    def test_quick_overlay_does_not_postpone_the_real_refresh(self):
        from usage_tracker import collect, config
        self.write_config(f'[[profiles]]\nid = "claude"\nprovider = "claude"\ndir = "{self.tmp}/c"\n')
        cfg = config.load()
        cache.save(self.state, {"claude": {"state": "unavailable", "next_at": 5, "checked_at": 1}})
        claude.record_statusline("claude", load("claude/statusline-input.json"), self.state, now=NOW)
        entries = collect.entries(cfg, self.state, NOW)
        self.assertEqual((entries["claude"]["state"], entries["claude"]["next_at"]), ("ok", 5))

    def test_quick_overlay_keeps_windows_it_does_not_have(self):
        from usage_tracker import collect, config
        from usage_tracker.model import window_for
        self.write_config(f'[[profiles]]\nid = "claude"\nprovider = "claude"\ndir = "{self.tmp}/c"\n')
        cached = [window_for(300, 5, NOW + 60), window_for(10080, 9, NOW + 600), window_for(10080, 2, NOW + 600, "Fable"),
                  window_for(10080, 50, 1790500000, "Opus")]  # a higher earlier reading of the same window
        cache.save(self.state, {"claude": {"state": "ok", "windows": cached, "updated_at": NOW - 100, "next_at": NOW + 99}})
        claude.record_statusline("claude", load("claude/statusline-input.json"), self.state, now=NOW)
        windows = collect.entries(config.load(), self.state, NOW)["claude"]["windows"]
        self.assertEqual([(w["id"], w["used"]) for w in windows],  # newer 5h / 7d from the statusline, Fable kept
                         [("session", 17.0), ("weekly", 41.6), ("weekly_opus", 50.0), ("weekly_fable", 2.0)])


if __name__ == "__main__":
    unittest.main()
