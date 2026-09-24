# Internals

Notes for anyone changing the plugin. The [README](../README.md) covers installing and using it.

## Flow

```
Herdr tab bar ──(every 30s)──> usage-tracker status ──reads──> state/cache.json (+ Claude snapshot)
                                     │ due & no collector running (flock + 60s debounce)
                                     └──spawns──> usage-tracker refresh ──> providers.limits()  (4 workers, bounded timeouts)
                                                                        └─> providers.history() (incremental, per-profile SQLite)
Claude Code statusLine ──> usage-tracker claude-statusline ──> state/claude/<profile>.statusline.json ──> your statusline script
                                     └──> herdr pane report-metadata (context meter of that pane)
Herdr event pane.agent_status_changed ──> usage-tracker agent-event ──> context meter; refresh --provider (≤ 1/min,
                                                                        skipped while Claude's bridge is current)
```

Limits come from each tool itself where possible: `codex app-server` for Codex and a short-lived
`claude -p` answering `get_usage` for Claude, so both handle their own sign-in.

| Module | Role |
|---|---|
| `providers/` | Adapters: provider/account data → normalized snapshots and usage events |
| `model.py` | Normalized windows, events, provenance, `ProviderError` states |
| `cache.py` | Atomic JSON writes, lock, backoff (`2^n × interval`, capped at 1 h, honors `Retry-After`), collector spawning |
| `collect.py` | Collection runs; failures keep the last good snapshot; secrets redacted from logs |
| `web.py` | The one HTTPS request of the opt-in token providers (Grok, Gemini, Cursor); 401/403 → sign-in needed |
| `history.py` | Per-profile SQLite; byte-offset incremental reads; partial and malformed lines are safe |
| `fmt.py` | Status line, pace forecast and shared formatting |
| `alerts.py` | Low-limit notifications, once per threshold and window |
| `agents.py` | Context meters and the reaction to finished agent turns |
| `dashboard.py` | curses dashboard |
| `integrate.py` | Herdr config and Claude statusline setup/uninstall |

State lives in Herdr's plugin state dir (`~/.local/state/herdr/plugins/herdr_agents_tracker`):
`cache.json`, `history/<profile>.sqlite`, `claude/<profile>.statusline.json`, `alerts.json`,
`context.json`, `collector.log`. The `[[startup]]` hooks are one-shot: start a detached refresh,
and put the context meters back after a Herdr restart.

## Adding a provider

Add `usage_tracker/providers/<name>.py` with `NAME`, `DEFAULT_DIR`, `CAPABILITIES`, `detect()`,
`limits()` and optionally `history()` / `quick()`, then register it in `providers/__init__.py`.
The status line and the dashboard need no changes. `quick()` is a cheap local read (like Claude's
statusline snapshot) that the status command overlays on the cache without a collector run.

## Tab bar

- Each account gets its own `tab_bar_right` entry, because Herdr shows at most 80 columns of one
  entry. An entry without an account exits 1 and stays hidden; the last one shows any remaining
  accounts.
- `status.max_width` is the budget for all of them together. When it is tight, `fmt.fit` drops
  model limits, then reset times, then bars, and only then cuts text with `…`, so every account
  stays visible.
- `format = "split"` puts two windows in one bar: the top half of each cell is the 5-hour window,
  the bottom half the weekly one (`██▀▀▀▀░░` is 81% of 5 hours and 24% of the week). `detailed`
  draws one bar, for the first window, since the others print their percentage anyway.
- Accounts that share a logo also print their `label`. A font's "Mono" variant draws Nerd Font
  logos in one narrow cell; mapping just those two code points to the regular variant makes them
  bigger, e.g. in Ghostty `font-codepoint-map = U+EC81-U+EC82=JetBrainsMono Nerd Font`. The plugin
  puts two spaces after a Nerd Font logo to leave room.

## Context meters

- Claude Code panes: the statusline bridge reports `context_window` at each reply. Between replies
  (after setup, when Herdr starts, after a turn) the meter comes from the transcript of the session
  that pane's `claude` process records in `<dir>/sessions/<pid>.json`.
- A transcript does not say how large the window is, so until the bridge has reported it the meter
  shows tokens only (`⛁ 118k`); past 200k tokens the 1M window is assumed.
- Codex panes: the last `token_count` in the session file that pane's `codex` process holds open
  (its main thread, not subagents), found with `lsof` (macOS) or `/proc/<pid>/fd` (Linux).
- One of `$usage_ctx_ok`, `$usage_ctx_warn`, `$usage_ctx_hot` is set per pane, by level, so the
  meter takes that colour. Setup appends them to the agent row; agents with their own
  `rows_by_agent` need those three tokens added there by hand.

## Verified Herdr 0.8.2 behavior

- Plugins cannot declare tab-bar entries; the entry is a `ui.tab_bar_right` command in `config.toml`.
- Status commands run through `sh -c` with a minimal `PATH` and without plugin env vars. The
  launcher therefore finds Python 3.11+ explicitly, and the plugin dirs default to Herdr's locations.
- Output is one plain-text line: ANSI escapes are shown literally, entries are not clickable,
  leading spaces are trimmed, and a non-zero exit or a timeout hides the entry. Entries have no
  colour or style setting, which is why the bars are one colour and `bar = "color"` uses emoji
  squares the terminal colours itself. Wide characters are measured correctly.
- Pane tokens (`herdr pane report-metadata`) appear in `[ui.sidebar.agents] rows` as `$name`, each
  styled by its own `fg`; tokens in one row are joined with ` · `, and Herdr forgets them when its
  server restarts (a startup hook puts the Codex meters back; Claude's return at its next reply).
- `herdr notification show` delivers as `[ui.toast] delivery` says, and reports `disabled` when
  that is off or `busy` right after a config reload (alerts then retry at the next check).
- `[[events]] on = "pane.agent_status_changed"` runs for every state change of every agent
  (`agent_status` idle, working, blocked or done).
- `[[panes]]` with `placement = "popup"` open session-modal, leave the layout unchanged and close
  when the command exits. `keys.command` supports `shell`, `pane` and `popup`, not plugin actions,
  so the shortcut runs `usage-tracker open dashboard`, which calls `herdr plugin pane open`.
- `[[build]]` commands run only for GitHub installs, in the checkout, without plugin env vars;
  a failure aborts the install. Ours checks for Python 3.11+.
