"""Grok (xAI): subscription usage from the Grok CLI's billing endpoint.

Uses the sign-in the Grok CLI already stored in <dir>/auth.json. The token is only held in
memory for one HTTPS request; it is never refreshed, logged, or written anywhere. When it has
expired the account shows "sign-in needed" until `grok` refreshes it. No local history.
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import model
from ..model import ProviderError

NAME = "Grok"
DEFAULT_DIR = "~/.grok"
AUTO_ENABLE = False  # reads a stored CLI token, so it runs only when a profile is configured explicitly
CAPABILITIES = ("limits: weekly credit % and monthly usage from the Grok CLI billing endpoint "
                "(uses the CLI's stored sign-in, never refreshed or persisted); history: none")
BILLING_URL = (os.environ.get("GROK_CLI_CHAT_PROXY_BASE_URL") or "https://cli-chat-proxy.grok.com/v1").rstrip("/") \
    + "/billing?format=credits"
TIMEOUT = 10


def detect(directory):
    return os.path.exists(os.path.join(directory, "auth.json"))


def read_auth(directory):
    """The Grok CLI sign-in entry: {key, user_id, expires_at, ...}, keyed by issuer in auth.json."""
    try:
        data = json.loads((Path(directory) / "auth.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    entries = [data] + [v for k, v in sorted(data.items(), key=lambda kv: not str(kv[0]).startswith("https://auth.x.ai"))
                        if isinstance(v, dict)]
    return next((e for e in entries if isinstance(e.get("key"), str) and e["key"]), None)


def limits(profile, state_dir, opener=urllib.request.urlopen):
    entry = read_auth(profile["dir"])
    if not entry:
        raise ProviderError("unavailable", "Not signed in to the Grok CLI (run `grok login`)")
    expires = model.iso_ts(entry.get("expires_at"))
    if expires and expires <= time.time():
        raise ProviderError("auth", "Grok sign-in expired: run `grok` to refresh it")
    headers = {"Authorization": f"Bearer {entry['key']}", "X-XAI-Token-Auth": "xai-grok-cli",
               "Accept": "application/json"}
    if entry.get("user_id"):
        headers["x-userid"] = str(entry["user_id"])
    try:
        with opener(urllib.request.Request(BILLING_URL, headers=headers), timeout=TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        exc.close()  # release the error response
        if exc.code in (401, 403):
            raise ProviderError("auth", f"Grok rejected the stored sign-in (HTTP {exc.code}): run `grok`") from None
        retry = exc.headers.get("Retry-After") if exc.headers else None
        raise ProviderError("error", f"Grok billing request failed (HTTP {exc.code})",
                            retry_after=int(retry) if retry and retry.isdigit() else None) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ProviderError("error", f"Grok billing request failed: {type(exc).__name__}") from None
    return parse_billing(data.get("config") if isinstance(data.get("config"), dict) else data)


def parse_billing(cfg):
    period = cfg.get("currentPeriod") or {}
    end = model.iso_ts(period.get("end") or cfg.get("billingPeriodEnd"))
    windows = []
    if isinstance(cfg.get("creditUsagePercent"), (int, float)):
        weekly = period.get("type") == "USAGE_PERIOD_TYPE_WEEKLY"
        windows.append(model.window("weekly" if weekly else "period", "7d" if weekly else "period",
                                    cfg["creditUsagePercent"], end, 10080 if weekly else None))
    limit, used = _val(cfg.get("monthlyLimit")), _val(cfg.get("used"))
    if limit and used is not None:
        windows.append(model.window("monthly", "30d", 100 * used / limit,
                                    model.iso_ts(cfg.get("billingPeriodEnd")), 43200))
    if not windows:
        raise ProviderError("unavailable", "Grok did not report a usage percentage for this account")
    return {"windows": windows, "plan": cfg.get("subscriptionTier"), "source": "Grok CLI billing endpoint",
            "observed_at": time.time()}


def _val(field):
    value = field.get("val") if isinstance(field, dict) else field
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
