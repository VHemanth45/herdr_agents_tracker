import unittest

from helpers import NOW, cfg, profile
from usage_tracker import fmt, model


def entry(*windows, updated=NOW - 60, **extra):
    return {"windows": list(windows), "updated_at": updated, "state": "ok", **extra}


FIVE = model.window_for(300, 17, NOW + 4 * 3600 + 41 * 60)
WEEK = model.window_for(10080, 59, NOW + 2 * 86400 + 5 * 3600)
FABLE = model.window_for(10080, 0, NOW + 2 * 86400 + 5 * 3600, "Fable")


class Formatting(unittest.TestCase):
    def test_missing_percentage_is_never_zero(self):
        self.assertEqual(fmt.pct(None), "--")
        self.assertEqual(fmt.pct(0), "0%")
        self.assertIsNone(model.window("x", "x", float("nan"))["used"])
        self.assertIsNone(model.window("x", "x", "12")["used"])

    def test_percentages_are_clamped(self):
        self.assertEqual(model.window("x", "x", 140)["used"], 100)
        self.assertEqual(model.window("x", "x", -3)["used"], 0)

    def test_durations(self):
        self.assertEqual(fmt.duration(4 * 3600 + 41 * 60), "4h41m")
        self.assertEqual(fmt.duration(4 * 3600 + 5 * 60), "4h05m")
        self.assertEqual(fmt.duration(5 * 86400 + 23 * 3600 + 59), "5d23h")
        self.assertEqual(fmt.duration(61), "1m")
        self.assertEqual(fmt.duration(30), "<1m")
        self.assertEqual(fmt.duration(-100), "<1m")

    def test_window_names_follow_length(self):
        self.assertEqual((FIVE["id"], FIVE["label"]), ("session", "5h"))
        self.assertEqual((WEEK["id"], WEEK["label"]), ("weekly", "7d"))
        scoped = model.window_for(10080, 5, scope="GPT-5 mini")
        self.assertEqual((scoped["id"], scoped["label"]), ("weekly_gpt_5_mini", "7d GPT-5 mini"))
        self.assertEqual(model.window_for(720, 1)["label"], "12h")


class StatusLine(unittest.TestCase):
    def line(self, profiles, entries, part=None, **status):
        return fmt.status_line(profiles, entries, cfg(**status), NOW, part)

    def test_several_accounts_in_configured_order(self):
        profiles = [profile("claude"), profile("work", label="Claude Work"), profile("codex", "codex")]
        entries = {"claude": entry(FIVE), "work": entry(model.window_for(300, 80, NOW + 600)), "codex": entry(WEEK)}
        self.assertEqual(self.line(profiles, entries, order=["codex", "claude", "work"]),
                         "Codex 59% 2d5h   Claude 17% 4h41m   Claude Work 80% 10m")

    def test_order_selects_which_accounts_appear(self):
        profiles = [profile("claude"), profile("codex", "codex")]
        entries = {"claude": entry(FIVE), "codex": entry(WEEK)}
        self.assertEqual(self.line(profiles, entries, order=["codex"]), "Codex 59% 2d5h")

    def test_default_window_is_highest_used_and_setting_overrides(self):
        profiles = [profile("claude")]
        entries = {"claude": entry(FIVE, WEEK)}
        self.assertEqual(self.line(profiles, entries), "Claude 59% 2d5h")
        self.assertEqual(self.line(profiles, entries, window="session"), "Claude 17% 4h41m")
        # A requested window the account does not have falls back to the highest one.
        self.assertEqual(self.line(profiles, entries, window="monthly"), "Claude 59% 2d5h")
        per_profile = [profile("claude", window="session")]
        self.assertEqual(self.line(per_profile, entries), "Claude 17% 4h41m")

    def test_detailed_format_lists_every_window(self):
        entries = {"claude": entry(FIVE, WEEK, FABLE)}
        self.assertEqual(self.line([profile("claude")], entries, format="detailed"),
                         "Claude 17% 4h41m | 59% 2d5h | 0% Fable")
        # One bar, for the first (5-hour) window; the others already show their percentage.
        self.assertEqual(self.line([profile("claude")], entries, format="detailed", bar="blocks", bar_width=8),
                         "Claude █░░░░░░░ 17% 4h41m | 59% 2d5h | 0% Fable")

    def test_pace_warns_when_a_limit_runs_out_before_its_reset(self):
        # 88% of the week used in 30 hours: at that rate 100% comes in about 4 hours, days before the reset.
        fast = model.window_for(10080, 88, NOW + 138 * 3600)
        codex = [profile("codex", "codex")]
        self.assertEqual(self.line(codex, {"codex": entry(fast)}), "Codex 88% 5d18h (100% in 4h05m)")
        # On track: 59% after 4d19h, with 2d5h to go, ends the week near 86%.
        self.assertEqual(round(fmt.forecast(WEEK, NOW)[0]), 86)
        self.assertIsNone(fmt.forecast(WEEK, NOW)[1])
        # Too early to tell while less than a tenth of the window has passed.
        self.assertIsNone(fmt.forecast(FIVE, NOW))
        # No forecast from stale data, or once reset times are left out to fit.
        self.assertEqual(self.line(codex, {"codex": entry(fast, updated=NOW - 3 * 3600)}), "Codex 88% 5d18h (3h00m old)")
        self.assertEqual(self.line(codex, {"codex": entry(fast)}, max_width=20), "Codex 88%")

    def test_states_are_explicit(self):
        profiles = [profile(p) for p in ("a", "b", "c", "d")]
        entries = {"a": {"state": "auth", "error": "expired"}, "b": {"state": "unavailable"},
                   "c": {"state": "error"}}
        self.assertEqual(self.line(profiles, entries), "A sign-in needed   B n/a   C error   D …")

    def test_stale_data_shows_its_age(self):
        entries = {"claude": entry(FIVE, updated=NOW - 3 * 3600)}
        self.assertEqual(self.line([profile("claude")], entries), "Claude 17% 4h41m (3h00m old)")

    def test_failed_refresh_keeps_last_snapshot(self):
        entries = {"codex": entry(WEEK, state="error", error="timed out")}
        self.assertEqual(self.line([profile("codex", "codex")], entries), "Codex 59% 2d5h")

    def test_windows_past_their_reset_are_not_shown_as_current(self):
        expired = model.window_for(300, 95, NOW - 60)
        self.assertEqual(self.line([profile("claude")], {"claude": entry(expired, WEEK)}), "Claude 59% 2d5h")
        self.assertEqual(self.line([profile("claude")], {"claude": entry(expired)}), "Claude -- (reset)")

    def test_estimates_are_marked(self):
        entries = {"oc": entry(model.window_for(300, 21), estimated=True)}
        self.assertEqual(self.line([profile("oc", "opencode", "OpenCode")], entries), "OpenCode ~21% (5h)")

    def test_long_output_is_truncated_to_width(self):
        profiles = [profile(f"p{i}") for i in range(6)]
        entries = {f"p{i}": entry(FIVE) for i in range(6)}
        line = self.line(profiles, entries, max_width=40)
        self.assertLessEqual(fmt.width(line), 40)  # each account cut to its share
        self.assertTrue(line.endswith("…"))

    def test_accounts_drop_detail_to_fit_together(self):
        # Herdr hides the whole summary when it does not fit, so detail goes instead of accounts.
        profiles = [profile("a", format="split", label="A"), profile("b", "codex", label="B")]
        entries = {"a": entry(FIVE, WEEK, FABLE), "b": entry(WEEK)}
        steps = {66: "A █▄▄▄▄░░░ 17% 4h41m | 59% 2d5h | 0% Fable   B █████░░░ 59% 2d5h",
                 60: "A █▄▄▄▄░░░ 17% 4h41m | 59% 2d5h   B █████░░░ 59% 2d5h",  # no model limits
                 40: "A █▄▄▄▄░░░ 17% | 59%   B █████░░░ 59%",  # no reset times
                 20: "A 17% | 59%   B 59%"}  # no bars
        for budget, expected in steps.items():
            self.assertEqual(self.line(profiles, entries, bar="blocks", bar_width=8, max_width=budget), expected)
        # Every tab-bar entry makes the same choice, so the parts always match the whole line.
        self.assertEqual(self.line(profiles, entries, "2", bar="blocks", bar_width=8, max_width=40), "B █████░░░ 59%")

    def test_icons_replace_names(self):
        profiles = [profile("claude", icon="✻"), profile("codex", "codex", icon=">_")]
        entries = {"claude": entry(FIVE), "codex": entry(WEEK)}
        self.assertEqual(self.line(profiles, entries), "✻ 17% 4h41m   >_ 59% 2d5h")
        # Accounts sharing an icon keep their names so they stay distinguishable.
        profiles.append(profile("work", label="Work", icon="✻"))
        entries["work"] = entry(FIVE)
        self.assertEqual(self.line(profiles, entries),
                         "✻ Claude 17% 4h41m   >_ 59% 2d5h   ✻ Work 17% 4h41m")
        # Nerd Font logos get a second space, room for terminals that draw them two cells wide.
        self.assertEqual(self.line([profile("claude", icon="\uec82")], entries), "\uec82  17% 4h41m")

    def test_parts_give_each_account_its_own_entry(self):
        profiles = [profile("claude"), profile("codex", "codex"), profile("oc", "opencode", "OpenCode")]
        entries = {"claude": entry(FIVE), "codex": entry(WEEK)}
        self.assertEqual(self.line(profiles, entries, "1"), "Claude 17% 4h41m")
        self.assertEqual(self.line(profiles, entries, "2"), "Codex 59% 2d5h")
        self.assertEqual(self.line(profiles, entries, "2-"), "Codex 59% 2d5h   OpenCode …")
        self.assertIsNone(self.line(profiles, entries, "4-"))  # no account there: the entry stays hidden
        broken = cfg()
        broken["error"] = "bad toml"
        self.assertEqual(fmt.status_line(profiles[:1], {"claude": entry(FIVE)}, broken, NOW, "2"), "config error")

    def test_bars_fill_whole_cells(self):
        # A partly filled cell would show the tab bar's background instead of the track.
        self.assertEqual(fmt.bar(18, 10, "blocks"), "██░░░░░░░░")
        self.assertEqual(fmt.bar(66, 10, "blocks"), "███████░░░")
        self.assertEqual(fmt.bar(66, 5, "blocks"), "███░░")
        self.assertEqual(fmt.bar(99, 5, "blocks"), "████░")  # only a used-up limit looks full
        self.assertEqual(fmt.bar(100, 5, "blocks"), "█████")
        self.assertEqual(fmt.bar(0, 5, "blocks"), "░░░░░")
        self.assertEqual(fmt.bar(None, 5, "blocks"), "")  # unknown usage gets no bar, never an empty one
        self.assertEqual(fmt.bar(50, 5, "none"), "")

    def test_color_bars_follow_usage_level(self):
        # Width is in columns and emoji squares are two columns wide.
        self.assertEqual(fmt.bar(18, 10, "color"), "🟩⬛⬛⬛⬛")
        self.assertEqual(fmt.bar(66, 10, "color"), "🟩🟩🟩⬛⬛")
        self.assertEqual(fmt.bar(82, 10, "color"), "🟨🟨🟨🟨⬛")
        self.assertEqual(fmt.bar(95, 11, "color"), "🟥🟥🟥🟥⬛")
        self.assertEqual(fmt.bar(100, 11, "color"), "🟥🟥🟥🟥🟥")
        self.assertEqual(fmt.bar(0, 10, "color"), "⬛⬛⬛⬛⬛")

    def test_status_line_with_bars(self):
        entries = {"claude": entry(FIVE), "codex": entry(model.window_for(10080, 66, NOW + 60))}
        profiles = [profile("claude"), profile("codex", "codex")]
        self.assertEqual(self.line(profiles, entries, bar="blocks"),
                         "Claude ██░░░░░░░░ 17% 4h41m   Codex ███████░░░ 66% 1m")
        self.assertEqual(self.line(profiles, entries, bar="color"),
                         "Claude 🟩⬛⬛⬛⬛ 17% 4h41m   Codex 🟩🟩🟩⬛⬛ 66% 1m")
        self.assertEqual(self.line(profiles, entries, bar="blocks", bar_width=4),
                         "Claude █░░░ 17% 4h41m   Codex ███░ 66% 1m")
        self.assertEqual(self.line(profiles, entries, bar="color", bar_width="wide"),  # bad value: default width
                         "Claude 🟩⬛⬛⬛⬛ 17% 4h41m   Codex 🟩🟩🟩⬛⬛ 66% 1m")

    def test_split_bar_shows_two_windows_in_one_row(self):
        self.assertEqual(fmt.split_bar(14, 18, 10), "█▄░░░░░░░░")
        self.assertEqual(fmt.split_bar(60, 20, 10), "██▀▀▀▀░░░░")  # top half 60%, bottom half 20%
        self.assertEqual(fmt.split_bar(95, 40, 10), "████▀▀▀▀▀░")
        self.assertEqual(fmt.split_bar(0, 100, 4), "▄▄▄▄")

    def test_split_format_is_per_account(self):
        profiles = [profile("claude", format="split"), profile("codex", "codex")]
        entries = {"claude": entry(FIVE, WEEK, FABLE), "codex": entry(WEEK)}
        self.assertEqual(self.line(profiles, entries, "1", bar="blocks", bar_width=8),
                         "Claude █▄▄▄▄░░░ 17% 4h41m | 59% 2d5h | 0% Fable")
        self.assertEqual(self.line(profiles, entries, "2", bar="blocks", bar_width=8), "Codex █████░░░ 59% 2d5h")
        # Herdr shows at most 80 columns of an entry, so longer lines end in "…" instead of being cut.
        line = self.line(profiles, entries, bar="blocks", bar_width=12, max_width=120)
        self.assertEqual((fmt.width(line), line[-1]), (80, "…"))
        self.assertEqual(self.line(profiles[:1], entries), "Claude 17% 4h41m | 59% 2d5h | 0% Fable")
        # Without both windows it falls back to the compact form.
        self.assertEqual(self.line(profiles[:1], {"claude": entry(WEEK)}), "Claude 59% 2d5h")

    def test_truncation_counts_emoji_as_two_columns(self):
        self.assertEqual(fmt.clip("Claude 🟩⬛⬛⬛⬛ 17%", 12), "Claude 🟩⬛…")
        self.assertEqual(fmt.width("🟩⬛a"), 5)
        # Too narrow for the bar: it is left out rather than cut.
        self.assertEqual(self.line([profile("claude")], {"claude": entry(FIVE)}, bar="color", max_width=20),
                         "Claude 17%")

    def test_disabled_and_config_errors(self):
        profiles = [profile("claude", enabled=False)]
        self.assertEqual(self.line(profiles, {}), "Usage: no accounts found (open Usage setup)")
        broken = cfg()
        broken["error"] = "bad toml"
        self.assertEqual(fmt.status_line([profile("claude")], {"claude": entry(FIVE)}, broken, NOW),
                         "Claude 17% 4h41m   config error")


if __name__ == "__main__":
    unittest.main()
