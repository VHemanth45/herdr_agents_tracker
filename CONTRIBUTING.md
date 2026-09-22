# Contributing

Bug reports, provider requests and pull requests are welcome. Please open an issue first for
anything larger than a small fix, so we can agree on the approach.

## Setup

Python 3.11+ and the standard library only — no dependencies to install.

```sh
git clone https://github.com/VHemanth45/herdr_agents_tracker.git
cd herdr_agents_tracker
python3 -m unittest discover -s tests
```

Tests use synthetic fixtures and temporary directories; they never touch your real accounts or
Herdr config. To try your changes in Herdr, `herdr plugin link "$PWD"`.

## Adding a provider

A provider is one module. The tab bar and dashboard need no changes.

1. Add `usage_tracker/providers/<name>.py` with:
   - `NAME` — display name, e.g. `"Gemini"`
   - `DEFAULT_DIR` — the tool's config directory, e.g. `"~/.gemini"`
   - `CAPABILITIES` — one line describing what it supports (shown in diagnostics)
   - `detect(dir)` — `True` when an account exists in that directory
   - `limits(profile, state_dir)` — a limit snapshot (see `model.py`); raise `model.ProviderError`
     for sign-in, missing-data and other failures instead of returning zeros
   - optional: `history(profile, store)` for token history, `quick(profile, state_dir)` for a
     cheap local read, `AUTO_ENABLE = False` for opt-in providers, `ICON`
2. Register it in `usage_tracker/providers/__init__.py`.
3. Add fixtures under `tests/fixtures/<name>/` and tests in `tests/test_providers.py`, including
   the failure states (signed out, CLI missing or hung, malformed data).

[`grok.py`](usage_tracker/providers/grok.py) is a small example; [`codex.py`](usage_tracker/providers/codex.py)
shows history and a CLI-backed source. [docs/internals.md](docs/internals.md) covers the overall
flow.

### Ground rules for data sources

- Prefer asking the tool itself (its CLI or status line) so it handles its own sign-in.
- Never read browser cookies, the macOS Keychain, or conversation text.
- If a stored token must be used, keep it in memory for one request and never log or write it;
  make the provider opt-in (`AUTO_ENABLE = False`).
- Estimated numbers must be marked as estimates.

## Pull requests

- Keep changes focused, and add or update tests.
- Update the README (and `CHANGELOG.md`) when behavior users can see changes.
