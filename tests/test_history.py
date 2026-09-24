import os
import shutil
import unittest

from helpers import NOW, IsolatedTest
from usage_tracker import config, history, model
from usage_tracker.providers import claude, codex


class History(IsolatedTest):
    def scan(self, adapter, profile):
        with history.Store(history.db_path(self.state, profile["id"])) as store:
            adapter.history(profile, store)
        return history.rows(self.state, [profile])

    def claude_profile(self, pid="claude"):
        return {"id": pid, "provider": "claude", "label": pid, "dir": str(self.tmp / "claude")}

    def test_claude_streamed_duplicates_keep_final_counts(self):
        self.copy_fixture("claude")
        rows = self.scan(claude, self.claude_profile())
        # msg_A is logged twice while streaming (3 then 286 output tokens); msg_B appears again in a
        # resumed session; the synthetic, usage-less and malformed lines are skipped.
        by_model = {}
        for r in rows:
            by_model[r["model"]] = by_model.get(r["model"], 0) + r["output"]
        self.assertEqual(by_model, {"claude-opus-5": 286 + 70, "claude-sonnet-4-5-20250929": 50})
        self.assertEqual(sum(r["requests"] for r in rows), 3)
        opus = next(r for r in rows if r["session"] == "sess-a" and r["model"] == "claude-opus-5")
        self.assertEqual((opus["input"], opus["cache_read"], opus["cache_write"], opus["cache_write_1h"],
                          opus["reasoning"]), (10, 1000, 200, 200, 40))

    def test_rescans_are_incremental_and_never_double_count(self):
        self.copy_fixture("claude")
        profile = self.claude_profile()
        self.scan(claude, profile)
        again = self.scan(claude, profile)
        self.assertEqual(sum(r["requests"] for r in again), 3)
        # A new line still being written (no newline yet) is left for the next pass.
        path = self.tmp / "claude/projects/-tmp-demo/session-b-resumed.jsonl"
        line = ('{"type":"assistant","sessionId":"sess-b","cwd":"/tmp/demo","timestamp":"2026-09-21T10:00:00Z",'
                '"requestId":"req_D","message":{"id":"msg_D","model":"claude-opus-5","usage":{"input_tokens":1,'
                '"output_tokens":9}}}')
        with open(path, "a") as f:
            f.write(line)
        self.assertEqual(sum(r["requests"] for r in self.scan(claude, profile)), 3)
        with open(path, "a") as f:
            f.write("\n")
        self.assertEqual(sum(r["requests"] for r in self.scan(claude, profile)), 4)
        # A replaced (rotated/rewritten) file is re-read from the start without duplicates.
        copy = path.with_suffix(".tmp")
        shutil.copy(path, copy)
        os.replace(copy, path)
        self.assertEqual(sum(r["requests"] for r in self.scan(claude, profile)), 4)

    def test_codex_exact_records_supersede_totals_and_forks_dedupe(self):
        self.copy_fixture("codex")
        profile = {"id": "codex", "provider": "codex", "label": "Codex", "dir": str(self.tmp / "codex")}
        rows = self.scan(codex, profile)
        new = [r for r in rows if r["session"] == "cx-new"]
        # Two per-response records; the session's cumulative token_count events are ignored.
        self.assertEqual(sum(r["requests"] for r in new), 2)
        self.assertEqual((sum(r["input"] for r in new), sum(r["cache_read"] for r in new),
                          sum(r["output"] for r in new)), (700, 2300, 400))
        self.assertEqual({r["model"] for r in new}, {"gpt-5-codex"})
        legacy = [r for r in rows if r["session"] in ("cx-old", "cx-fork")]
        # Old format: deltas of cumulative totals (110, repeat ignored, +220); the fork replays them.
        self.assertEqual(sum(r["requests"] for r in legacy), 2)
        self.assertEqual((sum(r["input"] for r in legacy), sum(r["cache_read"] for r in legacy),
                          sum(r["output"] for r in legacy), sum(r["reasoning"] for r in legacy)), (200, 100, 30, 5))

    def test_accounts_stay_separate(self):
        self.copy_fixture("claude")
        work = dict(self.claude_profile("work"), dir=str(self.tmp / "work"))
        shutil.copytree(self.tmp / "claude", self.tmp / "work")
        self.scan(claude, self.claude_profile())
        self.scan(claude, work)
        rows = history.rows(self.state, [self.claude_profile(), work])
        self.assertEqual({r["profile"] for r in rows}, {"claude", "work"})
        self.assertTrue(history.db_path(self.state, "work").exists())
        per_account = {p: sum(r["requests"] for r in rows if r["profile"] == p) for p in ("claude", "work")}
        self.assertEqual(per_account, {"claude": 3, "work": 3})

    def test_limit_readings_feed_the_forecast(self):
        week = lambda used, reset=NOW + 86400: model.window_for(10080, used, reset)
        with history.Store(history.db_path(self.state, "claude")) as store:
            store.record_limits([week(20)], NOW - 3 * 86400)
            store.record_limits([week(20)], NOW - 2 * 86400)  # unchanged: extends the same row
            store.record_limits([week(50)], NOW - 3600)
            store.record_limits([week(40)], NOW - 7200)  # older than the saved one: ignored
            store.record_limits([week(5, NOW + 8 * 86400)], NOW - 60)  # a later window has its own rows
        rows = history.limit_readings(self.state, "claude", week(50))
        self.assertEqual(rows, [(NOW - 3 * 86400, NOW - 2 * 86400, 20), (NOW - 3600, NOW - 3600, 50)])
        # A fifth of the week (33.6 h) ago the limit stood at 20%, last seen 2 days ago.
        [w] = history.with_baselines(self.state, "claude", [week(50)], NOW)
        self.assertEqual(w["base"], [NOW - 2 * 86400, 20])
        # No saved reading that far back in this window: the average since the start is used.
        [w] = history.with_baselines(self.state, "claude", [week(5, NOW + 8 * 86400)], NOW)
        self.assertNotIn("base", w)
        self.assertEqual(history.with_baselines(self.state, "none", [week(1)], NOW), [week(1)])

    def test_same_directory_twice_is_not_counted_twice(self):
        profiles, warnings = config.resolve_profiles([
            {"id": "a", "provider": "claude", "dir": str(self.tmp)},
            {"id": "b", "provider": "claude", "dir": str(self.tmp) + "/."},
            {"id": "c", "provider": "codex", "dir": str(self.tmp)},
            {"id": "a", "provider": "codex"}, {"id": "Bad Id", "provider": "claude"}, {"id": "x", "provider": "nope"}])
        self.assertEqual([(p["id"], p["enabled"]) for p in profiles], [("a", True), ("b", False), ("c", True)])
        self.assertEqual(len(warnings), 4)


if __name__ == "__main__":
    unittest.main()
