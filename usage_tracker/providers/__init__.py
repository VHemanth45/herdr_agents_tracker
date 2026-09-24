"""Provider registry. Every adapter module implements the same small interface:

NAME            display name used as the default account label
DEFAULT_DIR     account/configuration directory used for discovery and as the profile default
CAPABILITIES    one-line summary of what the adapter supports (shown in diagnostics)
detect(dir)     -> bool: an account exists in this directory
limits(profile, state_dir) -> limit snapshot (see model.py); raises model.ProviderError
history(profile, store)    optional: ingest local usage records incrementally (see history.py)
quick(profile, state_dir)  optional: cheap local limits read used directly by the status line
AUTO_ENABLE     optional, default True; False keeps a detected account out of automatic discovery
ICON            optional: short mark shown instead of the account name in the tab bar

To add a provider (Kiro, Qwen, ...), add a module and register it here.
The status line and dashboard only use this interface, so they need no changes.
"""

from . import amp, claude, codex, copilot, cursor, gemini, grok, opencode

PROVIDERS = {"claude": claude, "codex": codex, "opencode": opencode, "copilot": copilot, "amp": amp,
             "gemini": gemini, "cursor": cursor, "grok": grok}
