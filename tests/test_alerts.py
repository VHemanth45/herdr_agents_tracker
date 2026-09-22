import unittest

from helpers import NOW, IsolatedTest, cfg, profile
from usage_tracker import alerts, model


def five(used, reset=NOW + 3600):
    return model.window_for(300, used, reset)


class Alerts(IsolatedTest):
    def check(self, *windows, updated=NOW - 60, thresholds=(80, 95), delivered=True):
        self.calls = []

        def send(title, body):
            self.calls.append(title)
            return delivered

        entries = {"claude": {"windows": list(windows), "updated_at": updated, "state": "ok"}}
        return [title for title, _ in alerts.check([profile("claude")], entries,
                                                   cfg() | {"alerts": {"thresholds": list(thresholds)}}, NOW,
                                                   self.state, send)]

    def test_each_threshold_is_announced_once_per_window(self):
        self.assertEqual(self.check(five(50)), [])
        self.assertEqual(self.check(five(82)), ["Claude 5h limit at 82%"])
        self.assertEqual(self.check(five(85)), [])  # 80% was already announced for this window
        self.assertEqual(self.check(five(96)), ["Claude 5h limit at 96%"])
        self.assertEqual(self.check(five(97)), [])
        self.assertEqual(self.check(five(81, NOW + 5 * 3600)), ["Claude 5h limit at 81%"])  # the next window

    def test_busy_herdr_is_retried_and_old_or_switched_off_alerts_stay_quiet(self):
        self.assertEqual(self.check(five(90), delivered=False), [])
        self.assertEqual(self.check(five(90)), ["Claude 5h limit at 90%"])  # tried again
        self.assertEqual(self.check(five(99), updated=NOW - 3 * 3600), [])  # stale data
        self.assertEqual(self.check(five(99), thresholds=()), [])
        self.assertEqual(self.calls, [])

    def test_message_names_the_limit_and_says_when_it_runs_out(self):
        fast = model.window_for(10080, 88, NOW + 138 * 3600)
        title, body = alerts.message(profile("codex", "codex"), fast, NOW)
        self.assertEqual(title, "Codex 7d limit at 88%")
        self.assertTrue(body.startswith("Resets in 5d18h ("))
        self.assertTrue(body.endswith("At this rate 100% in 4h05m."))
        fable = model.window_for(10080, 81, NOW + 86400, "Fable")
        self.assertEqual(alerts.message(profile("claude"), fable, NOW)[0], "Claude 7d limit (Fable) at 81%")


if __name__ == "__main__":
    unittest.main()
