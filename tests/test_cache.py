import argparse
import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import NOW, IsolatedTest
from usage_tracker import cache, collect, config, model
from usage_tracker.model import ProviderError

ROOT = Path(__file__).resolve().parent.parent
GOOD = {"windows": [model.window_for(300, 40, NOW + 3600)], "source": "test", "observed_at": NOW}


class Cache(IsolatedTest):
    def test_atomic_write_and_malformed_reads(self):
        path = self.state / "cache.json"
        cache.write_json(path, {"a": 1})
        self.assertEqual(cache.read_json(path, {}), {"a": 1})
        self.assertEqual([p.name for p in self.state.iterdir()], ["cache.json"])  # no temp files left
        path.write_text('{"profiles": {"x": ')  # partially written by something else
        self.assertEqual(cache.load(self.state), {})

    def test_rewrites_keep_permissions_and_text(self):
        path = self.tmp / "settings.json"
        path.write_text("{}")
        path.chmod(0o644)
        cache.write_json(path, {"name": "café"}, indent=2)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertIn("café", path.read_text())

    def test_lock_is_exclusive(self):
        path = self.state / "refresh.lock"
        with cache.lock(path) as first:
            self.assertTrue(first)
            with cache.lock(path) as second:
                self.assertFalse(second)
            self.assertTrue(cache.locked(path))
        self.assertFalse(cache.locked(path))

    def test_failure_keeps_last_snapshot_and_backs_off(self):
        entry = cache.apply(None, dict(GOOD), NOW, 300)
        self.assertEqual((entry["state"], entry["updated_at"], entry["next_at"]), ("ok", NOW, NOW + 300))
        failed = cache.apply(entry, ProviderError("error", "timed out"), NOW + 300, 300)
        self.assertEqual(failed["windows"], GOOD["windows"])  # last good data kept
        self.assertEqual((failed["updated_at"], failed["state"], failed["error"]), (NOW, "error", "timed out"))
        self.assertEqual(failed["next_at"], NOW + 300 + 600)
        again = cache.apply(failed, ProviderError("error", "timed out"), NOW + 900, 300)
        self.assertEqual(again["next_at"], NOW + 900 + 1200)
        capped = cache.apply(dict(again, failures=10), ProviderError("error", "x"), NOW, 300)
        self.assertEqual(capped["next_at"], NOW + cache.MAX_BACKOFF)
        limited = cache.apply(entry, ProviderError("error", "429", retry_after=900), NOW, 300)
        self.assertEqual(limited["next_at"], NOW + 900)
        recovered = cache.apply(again, dict(GOOD, observed_at=NOW + 2000), NOW + 2000, 300)
        self.assertEqual((recovered["state"], recovered["error"], recovered["failures"]), ("ok", None, 0))

    def test_refresh_is_requested_only_when_due_and_idle(self):
        profiles = [{"id": "p", "enabled": True}]
        later = time.time() + 1000  # well past any lock-file timestamp, so the debounce does not apply
        with mock.patch.object(cache, "spawn") as spawn:
            self.assertFalse(cache.request_refresh(self.state, {"p": {"next_at": later + 60}}, profiles, later))
            self.assertTrue(cache.request_refresh(self.state, {"p": {"next_at": later - 1}}, profiles, later))
            with cache.lock(self.state / "refresh.lock"):  # a collector is running
                self.assertFalse(cache.request_refresh(self.state, {}, profiles, later))
            with cache.lock(self.state / "refresh.lock", mark_run=True):
                pass  # a collector just started, so further starts are debounced
            self.assertFalse(cache.request_refresh(self.state, {}, profiles, time.time()))
            self.assertEqual(spawn.call_count, 1)


class Refresh(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.write_config(f"""
[[profiles]]
id = "good"
provider = "codex"
dir = "{self.tmp}/good"

[[profiles]]
id = "bad"
provider = "grok"
dir = "{self.tmp}/bad"
""")

    def test_one_provider_failing_does_not_affect_others(self):
        def fake_limits(profile, state_dir):
            if profile["id"] == "bad":
                raise RuntimeError("adapter bug with token sk-abcdefghijklmnop")
            return dict(GOOD)

        cfg = config.load()
        with mock.patch.object(collect, "fetch", wraps=collect.fetch), \
                mock.patch("usage_tracker.providers.codex.limits", side_effect=fake_limits), \
                mock.patch("usage_tracker.providers.grok.limits", side_effect=fake_limits):
            self.assertTrue(collect.refresh(cfg, self.state, force=True, with_history=False))
        entries = cache.load(self.state)
        self.assertEqual(entries["good"]["state"], "ok")
        self.assertEqual(entries["bad"]["state"], "error")
        self.assertNotIn("abcdefghijklmnop", entries["bad"]["error"])  # redacted
        self.assertNotIn("abcdefghijklmnop", (self.state / "collector.log").read_text())

    def test_a_finished_turn_refreshes_that_provider_unless_it_is_backing_off(self):
        cfg = config.load()
        cache.save(self.state, {"good": {"next_at": NOW + 3600}, "bad": {"next_at": NOW + 3600, "failures": 2}})
        with mock.patch.object(collect, "fetch", return_value=dict(GOOD)) as fetch:
            collect.refresh(cfg, self.state, providers=["codex"], with_history=False)  # not due, refreshed anyway
            collect.refresh(cfg, self.state, providers=["grok"], with_history=False)  # backing off after errors
        self.assertEqual([call.args[0]["id"] for call in fetch.call_args_list], ["good"])
        with mock.patch.object(cache, "spawn") as spawn:
            self.assertTrue(cache.refresh_after_turn(self.state, "codex", time.time()))
            self.assertFalse(cache.refresh_after_turn(self.state, "codex", time.time()))  # once a minute
            self.assertTrue(cache.refresh_after_turn(self.state, "claude", time.time()))  # per provider
        spawn.assert_any_call("refresh", "--provider", "codex", "--no-history")
        self.assertEqual(spawn.call_count, 2)

    def test_concurrent_collectors_do_not_both_run(self):
        cfg = config.load()
        with cache.lock(self.state / "refresh.lock"):
            self.assertFalse(collect.refresh(cfg, self.state))

    def test_status_survives_a_corrupt_cache(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "cache.json").write_text("{corrupt")
        from usage_tracker import __main__ as cli
        with mock.patch.object(cache, "spawn") as spawn, mock.patch("sys.stdout") as out:
            self.assertEqual(cli.cmd_status(argparse.Namespace(part=None, json=False, check=None, profile=None)), 0)
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(printed, ">_ …   Grok …\n")  # first collection pending, never 0%
        spawn.assert_called_once_with("refresh")

    def test_status_command_is_fast_and_prints_exactly_one_line(self):
        now = time.time()
        live = dict(GOOD, windows=[model.window_for(300, 40, now + 3600)], updated_at=now, next_at=now + 3600)
        cache.save(self.state, {"good": live, "bad": {"state": "auth", "next_at": now + 3600}})
        env = dict(os.environ, HERDR_PLUGIN_STATE_DIR=str(self.state), HERDR_PLUGIN_CONFIG_DIR=str(self.conf))
        started = time.monotonic()
        done = subprocess.run([str(ROOT / "bin/usage-tracker"), "status"], capture_output=True, text=True,
                              env=env, timeout=10)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((done.returncode, done.stdout), (0, ">_ ████░░░░░░ 40% 59m   Grok sign-in needed\n"))
        (self.conf / "config.toml").write_text("[[profiles]\nbroken")
        done = subprocess.run([str(ROOT / "bin/usage-tracker"), "status"], capture_output=True, text=True,
                              env=env, timeout=10)
        self.assertEqual((done.returncode, done.stdout), (0, "Usage: config error (see Usage diagnostics)\n"))

    def test_status_json_and_check_for_scripts(self):
        now = time.time()
        live = dict(GOOD, windows=[model.window_for(300, 85, now + 3600)], updated_at=now, next_at=now + 3600, state="ok")
        cache.save(self.state, {"good": live, "bad": {"state": "auth", "next_at": now + 3600}})
        env = dict(os.environ, HERDR_PLUGIN_STATE_DIR=str(self.state), HERDR_PLUGIN_CONFIG_DIR=str(self.conf))

        def run(*args):
            done = subprocess.run([str(ROOT / "bin/usage-tracker"), "status", *args], capture_output=True,
                                  text=True, env=env, timeout=10)
            return done.returncode, done.stdout

        code, out = run("--json")
        accounts = json.loads(out)["accounts"]
        self.assertEqual(code, 0)
        self.assertEqual([(a["id"], a["state"], a["stale"]) for a in accounts],
                         [("good", "ok", False), ("bad", "auth", True)])
        self.assertEqual([(w["id"], w["used"]) for w in accounts[0]["windows"]], [("session", 85)])
        self.assertEqual(run("--check")[0], 10)  # 85% passes the default 80
        self.assertEqual(run("--check", "90")[0], 0)
        self.assertEqual(run("--check", "90", "--profile", "bad")[0], 20)  # no fresh data for it
        cache.save(self.state, {"good": dict(live, windows=[model.window_for(300, 100, now + 3600)])})
        self.assertEqual(run("--check", "--profile", "good")[0], 11)
        self.assertEqual(run("--check", "0")[0], 2)  # argparse rejects it


if __name__ == "__main__":
    unittest.main()
