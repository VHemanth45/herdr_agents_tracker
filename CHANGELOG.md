# Changelog

Notable changes per release. Versions follow [semantic versioning](https://semver.org):
before 1.0, a minor bump may change config keys or state layout, and the release notes say so.

## Unreleased

- **Four more providers**: GitHub Copilot CLI (monthly premium requests via `gh api`, token
  history from its session records), Amp (Amp Free and subscription usage via `amp usage`, history
  from thread files), and opt-in Gemini CLI (daily quota per model, history from session files)
  and Cursor (included usage of the billing cycle). Copilot and Amp are found automatically; Gemini
  and Cursor read the tool's stored sign-in, so they need a `[[profiles]]` entry.
- **Fix**: a finished agent turn no longer fails when Herdr can't report the pane's processes
  (missing, hung or restarting). This also made the tests fail on CI.

- **Each agent's part of the limit**: after every turn the context meter adds the agent's share
  of its account's shortest limit window, e.g. `⛁ 28% 72k · ~12% of 5h` (its sessions' tokens in
  the window, subagents included and cache reads left out, over the account's, times the window's
  %). `context.share = false` turns it off.
- **Agents stopped at a limit**: when an agent's last reply is its provider's usage-limit error,
  its meter shows when the limit resets, and a minute after the reset one notification names the
  agents that were waiting (in place of the plain reset notice). With `[resume] enabled = true`,
  each one still idle at that error is sent `resume.prompt` ("continue"). Off by default.
- **Reset notifications**: when a limit you were alerted about reaches its reset time, a Herdr
  notification says so (`Claude 5h limit has reset`, with the account's other limits).
  `alerts.on_reset = false` turns them off.
- **Pace from the recent rate**: each limit reading is saved (a new `limits` table in the
  per-account history database), and the forecast uses the rate over the last fifth of the
  window instead of the average since it started, so a recent burst or a quiet spell shows up.
  Until such a reading exists, the average is used as before. The dashboard draws a trend line of
  each limit across its window.
- **`status --json` and `status --check [PCT]`** for scripts: every account's limits and forecast
  as JSON, and exit codes `0` / `10` (at PCT% or more, default 80) / `11` (used up) / `20` (no
  fresh data); `--profile` narrows them to one account. Plain `status` is unchanged.
- **Refresh shortcut**: setup adds `prefix+shift+u` (or `prefix+shift+y` if that is taken) to
  re-read every account now. Re-run setup to get it.
- **Context meter after a compact**: it no longer keeps the pre-compact reading. Claude Code
  panes update as soon as the compact finishes, with an estimate (the kept conversation plus the
  session's fixed prompt and tools) until the next reply; Codex panes show an empty context until
  the next request, as Codex itself reports.

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
