import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import NOW, IsolatedTest, cfg, profile
from usage_tracker import agents, alerts, cache, collect, integrate, model
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

    def test_compact_shows_the_size_it_left_until_the_next_reply(self):
        self.write(268565)
        boundary = {"type": "system", "subtype": "compact_boundary",
                    "compactMetadata": {"trigger": "manual", "preTokens": 268565, "postTokens": 15719}}
        summary = {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "summary"}}
        with self.transcript.open("a") as f:
            f.write(json.dumps(boundary) + "\n" + json.dumps(summary) + "\n")
        self.assertEqual(agents.claude_tokens(self.transcript), 50000 + 15719)  # first reply: prompt, tools, memory
        after = {"transcript_path": str(self.transcript),  # what Claude Code sends the statusline after a /compact
                 "context_window": {"used_percentage": None, "current_usage": None, "context_window_size": 1_000_000}}
        self.assertEqual(agents.from_statusline(after), (100 * 65719 / 1_000_000, 65719))
        reply = {"type": "assistant", "message": {"model": "claude-opus-5", "usage": {
            "input_tokens": 5, "cache_creation_input_tokens": 20000, "cache_read_input_tokens": 0}}}
        with self.transcript.open("a") as f:
            f.write(json.dumps(reply) + "\n")
        self.assertEqual(agents.claude_tokens(self.transcript), 20005)

    def reply(self, session, n, error=None, ago=600):
        """A dated transcript reply of n fresh input and n output tokens."""
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(NOW - ago))
        message = {"id": f"m{n}{ago}", "model": "<synthetic>" if error else "claude-opus-5",
                   "usage": {"input_tokens": 0 if error else n, "output_tokens": 0 if error else n}}
        return {"type": "assistant", "sessionId": session, "timestamp": stamp, "message": message,
                **({"error": error, "isApiErrorMessage": True} if error else {})}

    def located(self):
        with mock.patch.object(agents, "pids", return_value=[64019]):
            return agents.locate("w1:p1", "claude", self.config)

    def test_the_panes_part_of_the_accounts_5h_limit(self):
        other = self.transcript.with_name("0e0e0e0e-0000-4000-8000-000000000000.jsonl")
        other.write_text(json.dumps(self.reply("other", 600)) + "\n")
        collect.update_history(self.config["profiles"], self.state)  # the collector has the other session
        self.transcript.write_text("".join(json.dumps(self.reply(self.SESSION, n, ago=a)) + "\n"
                                          for n, a in ((100, 900), (100, 300))))
        self.transcript.with_suffix("").joinpath("subagents").mkdir(parents=True)
        self.transcript.with_suffix("").joinpath("subagents/agent-1.jsonl").write_text(
            json.dumps(self.reply(self.SESSION, 100, ago=200) | {"isSidechain": True}) + "\n")
        old = json.dumps(self.reply(self.SESSION, 5000, ago=6 * 3600))  # before this window opened
        with self.transcript.open("a") as f:
            f.write(old + "\n")
        cache.save(self.state, {"claude": {"windows": [model.window_for(300, 40, NOW + 3600)], "updated_at": NOW,
                                           "state": "ok"}})
        found = self.located()
        self.assertEqual(len(found["files"]), 2)  # the transcript and its subagent's
        # 600 of this session's 1800 tokens (its subagent included) since the window opened: a third of 40%.
        self.assertEqual(agents.share(found, self.config, self.state, NOW), ["~13% of 5h", NOW + 3600])
        self.assertIsNone(agents.share(found, self.config | {"context": {"share": False}}, self.state, NOW))

    def test_an_agent_stopped_at_its_limit_waits_for_the_reset(self):
        self.transcript.write_text(json.dumps(self.reply(self.SESSION, 100)) + "\n"
                                   + json.dumps(self.reply(self.SESSION, 1, error="rate_limit")) + "\n")
        cache.save(self.state, {"claude": {"windows": [model.window_for(300, 100, NOW + 3600),
                                                       model.window_for(10080, 41, NOW + 86400)],
                                           "updated_at": NOW, "state": "ok"}})
        idle = {"agent": "claude", "agent_status": "idle", "terminal_title_stripped": "api server"}
        sent = []
        send = lambda title, body: sent.append((title, body)) or True
        with mock.patch.object(agents, "agent_info", return_value=idle), \
                mock.patch.object(agents, "pids", return_value=[64019]), \
                mock.patch.object(integrate, "herdr_bin", return_value="herdr"), mock.patch("subprocess.Popen") as popen:
            found = agents.locate("w1:p1", "claude", self.config)
            self.assertTrue(agents.stopped_at_limit(found))
            note = agents.note_waiting(found, "w1:p1", self.config, self.state, NOW)
            self.assertTrue(note[0].startswith("limit · resets "))
            self.assertEqual(agents.release_waiting(self.config, self.state, NOW + 3630, send), set())  # a minute's grace
            self.assertEqual(agents.release_waiting(self.config, self.state, NOW + 3661, send), {("claude", "session")})
            self.assertEqual(sent, [("Claude 5h limit has reset", "Waiting on it: api server. It can go on.")])
            popen.assert_not_called()  # resume is off by default
            self.assertEqual(cache.read_json(self.state / "waiting.json", {}), {})
            # With resume on, the agent is prompted, but only while it is idle at the limit error.
            resuming = self.config | {"resume": {"enabled": True, "prompt": "continue"}}
            for status, prompted in (("working", False), ("idle", True)):
                agents.note_waiting(found, "w1:p1", self.config, self.state, NOW)
                with mock.patch.object(agents, "agent_info", return_value=idle | {"agent_status": status}):
                    agents.release_waiting(resuming, self.state, NOW + 3661, send)
                prompts = [c.args[0] for c in popen.call_args_list if c.args[0][1:3] == ["agent", "prompt"]]
                self.assertEqual(prompts, [["herdr", "agent", "prompt", "w1:p1", "continue"]] if prompted else [])
            self.assertEqual(sent[-1], ("Claude 5h limit has reset", "Waiting on it: api server. Resumed."))
        # Its reset was announced with the waiting agents: the plain reset alert is left out.
        seen = {"claude/session": {"level": 95, "resets_at": NOW + 3600, "label": "5h"}}
        cache.write_json(self.state / "alerts.json", seen)
        self.assertEqual(alerts.check(self.config["profiles"], {}, self.config, NOW + 3661, self.state, send,
                                      covered={("claude", "session")}), [])

    def test_meter_notes_are_remembered_until_their_time(self):
        with mock.patch.object(integrate, "herdr_bin", return_value="herdr"), mock.patch("subprocess.Popen"):
            self.assertEqual(agents.show("w1:p1", 20, 40000, self.state, "⛁", NOW, extra=["~12% of 5h", NOW + 60]),
                             "⛁ 20% 40k · ~12% of 5h")
            self.assertEqual(agents.show("w1:p1", 21, 42000, self.state, "⛁", NOW + 1), "⛁ 21% 42k · ~12% of 5h")
            self.assertEqual(agents.clear_expired(self.state, NOW + 30), [])
            self.assertEqual(agents.clear_expired(self.state, NOW + 61), ["w1:p1"])
            self.assertEqual(cache.read_json(self.state / "context.json", {})["w1:p1"][0], "⛁ 21% 42k")

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
    def test_compaction_empties_the_meter_until_the_next_request(self):
        path = rollout(self.tmp / "rollout-2026-09-22T10-00-00-main.jsonl")
        after = [{"type": "compacted", "payload": {"message": ""}},
                 {"type": "event_msg", "payload": {"type": "token_count", "info": {
                     "last_token_usage": {"input_tokens": 0}, "model_context_window": 258400}}}]
        with open(path, "a") as f:
            f.write("".join(json.dumps(line) + "\n" for line in after))
        self.assertEqual(agents.last_context(path), (0.0, 0))
        rollout(self.tmp / "later.jsonl", counts=((12000, 258400),))
        with open(path, "a") as f:
            f.write(open(self.tmp / "later.jsonl").read().split("\n", 1)[1])
        self.assertEqual(agents.last_context(path), (100 * 12000 / 258400, 12000))

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

    def test_codex_names_the_limit_it_reached(self):
        path = Path(rollout(self.tmp / "rollout-limit.jsonl"))
        self.assertFalse(agents.stopped_at_limit({"kind": "codex", "main": path}))
        hit = {"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": {
            "primary": {"used_percent": 100.0, "window_minutes": 300}, "rate_limit_reached_type": "primary"}}}
        with path.open("a") as f:
            f.write(json.dumps(hit) + "\n")
        self.assertTrue(agents.stopped_at_limit({"kind": "codex", "main": path}))
        self.assertEqual(agents.codex_session(path), "s1")

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
            show.assert_called_once_with("w2:p1", 28.0, 72341, self.state, "⛁", NOW, extra=None)
            refresh.assert_called_once_with(self.state, "codex", NOW)
            working = {"agent": "codex", "pane_id": "w2:p1", "agent_status": "working"}  # also without "data"
            self.assertEqual(agents.on_event(working, config, self.state, NOW), "codex w2:p1 working: nothing to do")
            claude = {"agent": "claude", "pane_id": "w1:p1", "agent_status": "idle"}  # no Claude account set up
            self.assertEqual(agents.on_event(claude, config, self.state, NOW), "claude w1:p1 idle: no context found")
        self.assertEqual(refresh.call_count, 1)


if __name__ == "__main__":
    unittest.main()
