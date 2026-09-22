import json
import os
import unittest
from unittest import mock

from helpers import NOW, IsolatedTest, cfg, profile
from usage_tracker import agents, cache, integrate
from usage_tracker.providers import claude


def rollout(path, source="cli", counts=((40000, 258400), (72341, 258400))):
    """A Codex session file: session_meta, then token_count events (input tokens, context window)."""
    lines = [{"type": "session_meta", "payload": {"id": "s1", "source": source}}]
    for tokens, window in counts:
        lines += [{"type": "response_item", "payload": {"type": "message"}},
                  {"type": "event_msg", "payload": {"type": "token_count", "info": {
                      "last_token_usage": {"input_tokens": tokens, "cached_input_tokens": tokens - 900},
                      "model_context_window": window}}}]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return str(path)


class Meters(IsolatedTest):
    def test_meter_text(self):
        self.assertEqual(agents.meter(57.4, 565000, "⛁"), "⛁ 57% 565k")
        self.assertEqual(agents.meter(84, 1_240_000, "\ueace"), "\ueace  84% 1.2M")  # room for a two-cell Nerd Font icon
        self.assertEqual(agents.meter(1, 512, ""), "ctx 1% 512")
        self.assertEqual(agents.meter(None, 118483, "⛁"), "⛁ 118k")  # window size not known yet

    def test_claude_statusline_input(self):
        payload = {"context_window": {"used_percentage": 8, "total_input_tokens": 15500, "context_window_size": 200000}}
        self.assertEqual(agents.from_statusline(payload), (8.0, 15500))
        self.assertIsNone(agents.from_statusline({"context_window": {"used_percentage": None}}))  # before a response
        self.assertIsNone(agents.from_statusline({}))

    def test_show_sends_only_changes_with_one_token_per_level(self):
        with mock.patch.object(integrate, "herdr_bin", return_value="herdr"), \
                mock.patch("subprocess.Popen") as popen:
            self.assertEqual(agents.show("w1:p1", 84, 216138, self.state, "⛁", NOW), "⛁ 84% 216k")
            self.assertIsNone(agents.show("w1:p1", 84, 216138, self.state, "⛁", NOW + 60))  # unchanged
            agents.show("w1:p1", 84, 216138, self.state, "⛁", NOW + agents.RESEND + 1)  # re-sent after a while
            agents.show("w1:p1", 95, 245000, self.state, "⛁", NOW + agents.RESEND + 2)
        self.assertEqual(popen.call_count, 3)
        self.assertEqual(popen.call_args_list[0].args[0],
                         ["herdr", "pane", "report-metadata", "w1:p1", "--source", "herdr_agents_tracker",
                          "--clear-token", "usage_ctx_ok", "--token", "usage_ctx_warn=⛁ 84% 216k",
                          "--clear-token", "usage_ctx_hot"])
        self.assertIn("usage_ctx_hot=⛁ 95% 245k", popen.call_args_list[2].args[0])


class Claude(IsolatedTest):
    SESSION = "1da57f49-943b-489d-a0a6-b005c1ce3346"

    def setUp(self):
        super().setUp()
        home = self.tmp / "claude"
        (home / "sessions").mkdir(parents=True)
        (home / "sessions" / "64019.json").write_text(json.dumps({"pid": 64019, "sessionId": self.SESSION}))
        project = home / "projects" / "-Users-me-app"
        project.mkdir(parents=True)
        self.transcript = project / f"{self.SESSION}.jsonl"
        self.config = cfg() | {"profiles": [profile("claude") | {"dir": str(home)}]}

    def write(self, tokens):
        reply = lambda n, **extra: {"type": "assistant", "message": {"id": "m", "model": "claude-opus-5", "usage": {
            "input_tokens": 3, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": n - 1003}}, **extra}
        lines = [reply(50000), reply(tokens), reply(900000, isSidechain=True),  # a subagent's reply is not this context
                 {"type": "assistant", "message": {"model": "<synthetic>", "usage": {"input_tokens": 0}}}]
        self.transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))

    def context(self):
        with mock.patch.object(agents, "pids", return_value=[64019]):
            return agents.claude_context("w1:p1", self.config, self.state)

    def test_the_transcript_of_the_panes_session(self):
        self.write(268565)
        self.assertEqual(self.context(), (100 * 268565 / 1_000_000, 268565))  # past 200k: the 1M window
        self.write(118483)
        self.assertEqual(self.context(), (None, 118483))  # 200k or 1M: only the tokens for now
        with mock.patch.object(integrate, "herdr_bin", return_value="herdr"), mock.patch("subprocess.Popen"):
            agents.show("w1:p1", 59.2, 118483, self.state, "⛁", NOW, window=200_000)  # seen by the statusline bridge
        self.assertEqual(self.context(), (100 * 118483 / 200_000, 118483))

    def test_a_finished_turn_skips_the_claude_check_while_the_statusline_is_fresh(self):
        """The bridge already reported the 5h and weekly windows, so only Fable would be new."""
        event = {"agent": "claude", "pane_id": "w1:p1", "agent_status": "done"}
        cache.write_json(claude.snapshot_path(self.state, "claude"),
                         {"observed_at": NOW, "windows": [{"id": "session", "used": 40.0}]})
        with mock.patch.object(agents, "context", return_value=None), \
                mock.patch("usage_tracker.cache.refresh_after_turn", return_value=True) as refresh:
            self.assertEqual(agents.on_event(event, self.config, self.state, NOW),
                             "claude w1:p1 done: no context found; limits current from the statusline")
            refresh.assert_not_called()
            later = agents.on_event(event, self.config, self.state, NOW + 10 * 60)  # stale reading: ask Claude again
        self.assertEqual(later, "claude w1:p1 done: no context found; refresh started")


class Codex(IsolatedTest):
    def test_latest_token_count_of_the_main_session(self):
        sessions = self.tmp / "sessions"
        sessions.mkdir()
        main = rollout(sessions / "rollout-2026-09-22T10-00-00-main.jsonl")
        sub = rollout(sessions / "rollout-2026-09-22T10-00-01-sub.jsonl", source={"subagent": {"parent": "s1"}},
                      counts=((245000, 258400),))
        self.assertEqual(agents.last_context(main), (100 * 72341 / 258400, 72341))
        self.assertEqual((agents.subagent(main), agents.subagent(sub)), (False, True))
        info = {"result": {"process_info": {"foreground_processes": [
            {"pid": 11, "cmdline": "node /usr/local/bin/codex"}, {"pid": 12, "cmdline": "vim notes.md"}]}}}
        with mock.patch.object(integrate, "herdr", return_value=(0, json.dumps(info), "")), \
                mock.patch.object(agents, "open_files", return_value=[main, sub, "/dev/ttys001"]) as opened:
            self.assertEqual(agents.codex_context("w2:p1"), (100 * 72341 / 258400, 72341))  # not the subagent's
        opened.assert_called_once_with(11)  # only the codex process is inspected

    def test_open_files_finds_a_file_this_process_holds_open(self):
        """The real lookup behind it: /proc/<pid>/fd on Linux, lsof on macOS."""
        path = self.tmp / "rollout-2026-09-22T09-00-00-open.jsonl"
        path.write_text("{}\n")
        with open(path):
            found = {os.path.realpath(f) for f in agents.open_files(os.getpid())}
        self.assertIn(os.path.realpath(path), found)

    def test_a_finished_turn_updates_the_meter_and_refreshes_its_provider(self):
        config = cfg() | {"profiles": [profile("codex", "codex")]}
        with mock.patch.object(agents, "context", side_effect=lambda pane, kind, *_: (28.0, 72341) if kind == "codex" else None), \
                mock.patch.object(agents, "show", return_value="⛁ 28% 72k") as show, \
                mock.patch("usage_tracker.cache.refresh_after_turn", return_value=True) as refresh:
            note = agents.on_event({"event": "pane.agent_status_changed", "data": {
                "agent": "codex", "pane_id": "w2:p1", "agent_status": "done", "workspace_id": "w2"}},
                config, self.state, NOW)
            self.assertEqual(note, "codex w2:p1 done: context ⛁ 28% 72k; refresh started")
            show.assert_called_once_with("w2:p1", 28.0, 72341, self.state, "⛁", NOW)
            refresh.assert_called_once_with(self.state, "codex", NOW)
            working = {"agent": "codex", "pane_id": "w2:p1", "agent_status": "working"}  # also without "data"
            self.assertEqual(agents.on_event(working, config, self.state, NOW), "codex w2:p1 working: nothing to do")
            claude = {"agent": "claude", "pane_id": "w1:p1", "agent_status": "idle"}  # no Claude account set up
            self.assertEqual(agents.on_event(claude, config, self.state, NOW), "claude w1:p1 idle: no context found")
        self.assertEqual(refresh.call_count, 1)


if __name__ == "__main__":
    unittest.main()
