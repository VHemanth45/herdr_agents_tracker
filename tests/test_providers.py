import base64
import io
import json
import os
import sqlite3
import time
import unittest
import urllib.error
from unittest import mock

from helpers import FIXTURES, NOW, IsolatedTest
from usage_tracker import cache, history
from usage_tracker.model import ProviderError
from usage_tracker.providers import PROVIDERS
from usage_tracker.providers import amp, claude, codex, copilot, cursor, gemini, grok, opencode


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


def replies(*bodies, status=200):
    """A fake urlopen answering each request with the next body; the requests are kept on it."""
    def fake(request, timeout):
        fake.requests.append(request)
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "err", {}, io.BytesIO(b"{}"))
        return io.BytesIO(json.dumps(bodies[len(fake.requests) - 1]).encode())
    fake.requests = []
    return fake


def token_rows(profile, state):
    with history.Store(history.db_path(state, profile["id"])) as store:
        PROVIDERS[profile["provider"]].history(profile, store)
    return sorted((r["session"], r["project"], r["model"], r["input"], r["output"], r["cache_read"], r["cache_write"])
                  for r in history.rows(state, [profile]))


class Gemini(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.home = self.copy_fixture("gemini")
        self.profile = {"id": "gemini", "provider": "gemini", "label": "Gemini", "dir": str(self.home)}

    def test_daily_quota_per_model_pro_first(self):
        opener = replies(load("gemini/load-code-assist.json"), load("gemini/quota.json"))
        snap = gemini.limits(self.profile, self.state, opener=opener)
        self.assertEqual([(w["label"], round(w["used"])) for w in snap["windows"]],
                         [("1d 2.5 Pro", 55), ("1d 2.5 Flash", 30), ("1d 2.5 Flash Lite", 0)])
        self.assertEqual(snap["plan"], "Gemini Code Assist Standard")
        self.assertEqual(json.loads(opener.requests[1].data), {"project": "demo-project-123"})
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer ya29.test-token")

    def test_accounts_google_no_longer_serves_are_unavailable(self):
        setup = {"ineligibleTiers": [{"reasonCode": "UNSUPPORTED_CLIENT"}]}
        with self.assertRaises(ProviderError) as err:
            gemini.limits(self.profile, self.state, opener=replies(setup))
        self.assertEqual(err.exception.state, "unavailable")
        with self.assertRaises(ProviderError) as err:
            gemini.limits(self.profile, self.state, opener=replies(load("gemini/load-code-assist.json"), status=403))
        self.assertEqual(err.exception.state, "auth")  # 403 on the first request: the sign-in itself

    def test_expired_sign_in_and_api_key_accounts(self):
        creds = load("gemini/oauth_creds.json") | {"expiry_date": 1_600_000_000_000}
        (self.home / "oauth_creds.json").write_text(json.dumps(creds))
        with self.assertRaises(ProviderError) as err:
            gemini.limits(self.profile, self.state, opener=replies())
        self.assertEqual(err.exception.state, "auth")
        self.assertNotIn("ya29", err.exception.message)
        (self.home / "settings.json").write_text('{"security": {"auth": {"selectedType": "gemini-api-key"}}}')
        with self.assertRaises(ProviderError) as err:
            gemini.limits(self.profile, self.state, opener=replies())
        self.assertEqual(err.exception.state, "unavailable")

    def test_history_from_jsonl_subagent_and_legacy_sessions(self):
        rows = token_rows(self.profile, self.state)
        app = "/Users/demo/code/app"
        self.assertEqual(rows, [
            ("3f2a9c1e-8b7d-4e21-9a55-0c6d2b1f7e44", app, "gemini-2.5-flash", 2050, 100, 12000, 0),
            ("3f2a9c1e-8b7d-4e21-9a55-0c6d2b1f7e44", app, "gemini-2.5-pro", 4648, 1067, 8192, 0),
            ("7c9e1f00-0000-4000-8000-000000000001", app, "gemini-2.5-flash", 3000, 60, 0, 0),
            ("aaaa1111-0000-4000-8000-000000000000", app, "gemini-2.5-pro", 1000, 20, 0, 0)])
        self.assertEqual(token_rows(self.profile, self.state), rows)  # a second pass adds nothing

    def test_not_auto_enabled(self):
        self.assertFalse(gemini.AUTO_ENABLE)


class Copilot(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.home = self.copy_fixture("copilot")
        self.profile = {"id": "copilot", "provider": "copilot", "label": "Copilot", "dir": str(self.home)}

    def test_premium_requests_from_gh(self):
        snap = copilot.limits(self.profile, self.state)
        self.assertEqual([(w["id"], w["label"], round(w["used"], 1)) for w in snap["windows"]],
                         [("monthly", "30d", 21.9)])  # chat and completions are unlimited
        self.assertEqual((snap["plan"], snap["windows"][0]["resets_at"]), ("Individual", 1790812800))

    def test_copilot_free_monthly_allowances(self):
        os.environ["FAKE_GH_MODE"] = "free"
        snap = copilot.limits(self.profile, self.state)
        self.assertEqual([(w["label"], w["used"]) for w in snap["windows"]], [("30d Chat", 80.0), ("30d Completions", 25.0)])

    def test_gh_signed_out_missing_or_without_copilot(self):
        for mode, state in (("signed-out", "auth"), ("no-copilot", "unavailable")):
            os.environ["FAKE_GH_MODE"] = mode
            with self.assertRaises(ProviderError) as err:
                copilot.limits(self.profile, self.state)
            self.assertEqual(err.exception.state, state)
        os.environ["GH_BIN"] = str(self.tmp / "missing")
        with mock.patch("shutil.which", return_value=None), mock.patch("os.access", return_value=False):
            with self.assertRaises(ProviderError) as err:
                copilot.limits(self.profile, self.state)
        self.assertEqual(err.exception.state, "unavailable")

    def test_history_prefers_the_session_store_over_shutdown_totals(self):
        sid = "5b1d0c2e-7a44-4f0e-9d11-2c3b4a5d6e7f"
        api = "/Users/demo/code/api"
        # Only the session log: the latest cumulative totals, once.
        self.assertEqual(token_rows(self.profile, self.state), [(sid, api, "claude-sonnet-4.5", 350, 180, 100, 50)])
        con = sqlite3.connect(self.home / "session-store.db")
        con.executescript("""CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, host_type TEXT,
            branch TEXT, summary TEXT, created_at TEXT, updated_at TEXT);
          CREATE TABLE assistant_usage_events (id INTEGER PRIMARY KEY, session_id TEXT, turn_index INTEGER, model TEXT,
            copilot_usage_model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, reasoning_tokens INTEGER, total_nano_aiu INTEGER, duration_ms INTEGER,
            created_at TEXT);""")
        con.execute("INSERT INTO sessions (id, cwd) VALUES (?, ?)", (sid, api))
        con.executemany("INSERT INTO assistant_usage_events (id, session_id, model, copilot_usage_model, input_tokens, "
                        "output_tokens, cache_read_tokens, cache_write_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [(1, sid, "claude-sonnet-4.5", None, 300, 100, 60, 40, "2026-09-20 09:05:00"),
                         (2, sid, "gpt-5", "gpt-5-mini", 150, 20, 0, 0, "2026-09-20 09:06:00")])
        con.commit()
        con.close()
        self.assertEqual(token_rows(self.profile, self.state), [(sid, api, "claude-sonnet-4.5", 200, 100, 60, 40),
                                                                (sid, api, "gpt-5-mini", 150, 20, 0, 0)])


def make_vscdb(path, items):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    con.executemany("INSERT INTO ItemTable VALUES (?, ?)", items.items())
    con.commit()
    con.close()


def jwt(claims):
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{part}.sig"


class Cursor(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "cursor"
        self.home.mkdir()
        self.profile = {"id": "cursor", "provider": "cursor", "label": "Cursor", "dir": str(self.home)}
        self.token = jwt({"sub": "auth0|user_abc123", "exp": 4102444800})

    def test_included_usage_of_the_billing_cycle(self):
        make_vscdb(self.home / "state.vscdb", {"cursorAuth/accessToken": self.token,
                                               "cursorAuth/stripeMembershipType": "pro"})
        opener = replies(load("cursor/usage.json"))
        snap = cursor.limits(self.profile, self.state, opener=opener)
        self.assertEqual([(w["id"], w["label"], w["used"]) for w in snap["windows"]],
                         [("monthly", "30d", 20.0), ("monthly_auto", "30d Auto", 12.5), ("monthly_api", "30d API", 7.5)])
        self.assertEqual((snap["plan"], snap["windows"][0]["resets_at"]), ("Pro", 1792022400))
        self.assertEqual(opener.requests[0].get_header("Authorization"), f"Bearer {self.token}")

    def test_token_stored_as_utf16_blob_and_percent_from_spend(self):
        make_vscdb(self.home / "state.vscdb", {"cursorAuth/accessToken": self.token.encode("utf-16-le")})
        usage = load("cursor/usage.json")
        usage["planUsage"] = {"totalSpend": "10000", "limit": "40000"}
        snap = cursor.limits(self.profile, self.state, opener=replies(usage))
        self.assertEqual([(w["label"], w["used"]) for w in snap["windows"]], [("30d", 25.0)])

    def test_signed_out_expired_and_rejected(self):
        with self.assertRaises(ProviderError) as err:
            cursor.limits(self.profile, self.state, opener=replies())
        self.assertEqual(err.exception.state, "unavailable")
        make_vscdb(self.home / "state.vscdb", {"cursorAuth/accessToken": jwt({"exp": 1_600_000_000})})
        with self.assertRaises(ProviderError) as err:
            cursor.limits(self.profile, self.state, opener=replies())
        self.assertEqual(err.exception.state, "auth")
        make_vscdb(self.home / "state.vscdb", {"cursorAuth/accessToken": self.token})
        with self.assertRaises(ProviderError) as err:
            cursor.limits(self.profile, self.state, opener=replies(status=401))
        self.assertEqual(err.exception.state, "auth")
        self.assertNotIn(self.token, err.exception.message)

    def test_not_auto_enabled(self):
        self.assertFalse(cursor.AUTO_ENABLE)


class Amp(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.home = self.copy_fixture("amp") / "amp"
        self.profile = {"id": "amp", "provider": "amp", "label": "Amp", "dir": str(self.home)}

    def test_subscription_and_amp_free(self):
        snap = amp.limits(self.profile, self.state)
        self.assertEqual([(w["id"], w["label"], w["used"]) for w in snap["windows"]],
                         [("subscription", "plan", 27.0), ("1d", "1d", 39.0)])
        self.assertEqual(snap["plan"], "Gigawatt")

    def test_amp_free_resets_at_8pm_new_york(self):
        snap = amp.parse_usage("Amp Free: 61% remaining today (resets daily)", now=NOW)  # 2026-09-22 03:33 EDT
        self.assertEqual(snap["windows"][0]["resets_at"], 1790121600)  # 2026-09-22 20:00 EDT

    def test_tier_with_a_billing_period_and_legacy_free(self):
        os.environ["FAKE_AMP_MODE"] = "tier"
        snap = amp.limits(self.profile, self.state)
        self.assertEqual([(w["label"], round(w["used"], 2)) for w in snap["windows"]], [("30d", 7.15)])
        self.assertEqual(snap["plan"], "Megawatt")
        os.environ["FAKE_AMP_MODE"] = "legacy"
        snap = amp.limits(self.profile, self.state)
        self.assertEqual([(w["id"], w["used"]) for w in snap["windows"]], [("free", 20.0)])

    def test_signed_out_credits_only_and_hung(self):
        for mode, state in (("signed-out", "auth"), ("credits", "unavailable")):
            os.environ["FAKE_AMP_MODE"] = mode
            with self.assertRaises(ProviderError) as err:
                amp.limits(self.profile, self.state)
            self.assertEqual(err.exception.state, state)
        os.environ["FAKE_AMP_MODE"] = "hang"
        with mock.patch.object(amp, "CLI_TIMEOUT", 0.5), self.assertRaises(ProviderError) as err:
            amp.limits(self.profile, self.state)
        self.assertEqual(err.exception.state, "error")

    def test_history_from_thread_files(self):
        tid, app = "T-3f1c2a9e-8b7d-4e21-9c0a-5d6e7f809a1b", "/Users/demo/code/app"
        rows = token_rows(self.profile, self.state)
        self.assertEqual(rows, [(tid, app, "claude-haiku-4-5-20251001", 10, 178, 11372, 986),
                                (tid, app, "claude-sonnet-4-5", 5, 40, 12000, 0)])
        self.assertEqual(token_rows(self.profile, self.state), rows)


if __name__ == "__main__":
    unittest.main()
