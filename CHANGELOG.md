# Changelog

Notable changes per release. Versions follow [semantic versioning](https://semver.org):
before 1.0, a minor bump may change config keys or state layout, and the release notes say so.

## Unreleased

- **Refresh shortcut**: setup adds `prefix+shift+u` (or `prefix+shift+y` if that is taken) to
  re-read every account now. Re-run setup to get it.
- **Context meter after a compact**: a compacted Claude Code session shows the size the compact
  left, and a compacted Codex session shows an empty context, instead of the old reading.

## 0.1.0 — 2026-09-22

First release.

- **Tab bar**: one entry per account at the top right, `<logo> <bar> <% used> <time until reset>`
  for every limit window, refreshed every 30 s from a local cache. `compact`, `detailed` and
  `split` formats, `blocks` / `color` / `none` bars, and a width budget that sheds detail
  (model limits, then reset times, then bars) so every account stays visible.
- **Multi-account**: one `[[profiles]]` entry per account, never merged, with per-account cache,
  history and refresh state. Two profiles on one directory are detected and the second disabled.
- **Honest states** instead of a misleading 0%: `…` first refresh, `n/a`, `sign-in needed`,
  `error`, `(3h00m old)` stale, `~21%` estimate, `-- (reset)`.
- **Pace**: once a tenth of a window has passed, a projection of its average rate, e.g.
  `88% 5d18h (100% in 4h05m)`; nothing is projected from stale data.
- **Alerts**: a Herdr notification when a limit passes `alerts.thresholds` (80% and 95% by
  default), once per threshold and window, delivered as Herdr's `[ui.toast] delivery` says.
- **Context meters**: a `⛁ 28% 72k` meter per Claude Code and Codex agent in Herdr's Agents
  panel, grey / yellow / red by fullness, from the statusline bridge, the session transcript
  or the Codex session file the pane's process has open.
- **Event-driven refresh**: when an agent finishes a turn, its provider's limits are re-read
  right away (at most once a minute per provider) instead of waiting for the 5-minute cycle;
  skipped for Claude accounts whose statusline bridge just reported those windows.
- **Dashboard** in a popup: every window, token history (today / 7 d / 30 d / all), a heatmap,
  per-model and per-project breakdowns, account and provider filters. Token counts only: no
  prices are applied and no spend is estimated.
- **Providers**: Claude Code (live), Codex (live), OpenCode Go and Grok (fixtures only).
  Claude and Codex limits come from those tools themselves, so no credentials are read; Grok is
  opt-in and the only provider whose stored sign-in is used.
- **Setup and uninstall**: idempotent, backs up every file it edits, writes only lines marked
  `# usage-tracker`, re-parses the result before saving, and reverts cleanly.
- Herdr 0.8.2+, Python 3.11+ (standard library only), macOS and Linux; 98 tests.
