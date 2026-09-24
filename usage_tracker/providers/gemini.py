"""Gemini CLI (Google): daily per-model quota from Code Assist, history from local session files.

Limits use the sign-in Gemini CLI stored in <dir>/oauth_creds.json (Google sign-in accounts only):
the access token is held in memory for two HTTPS requests and never refreshed, logged or written;
once it has expired the account shows "sign-in needed" until `gemini` refreshes it. Google stopped
serving personal (free, AI Pro, Ultra) accounts through Gemini CLI in 2026; those show "n/a".
History comes from <dir>/tmp/<project>/chats/**/session-*.jsonl (and older session-*.json files).
"""

import json
import os
import time
import urllib.request
from pathlib import Path

from .. import model, web
from ..model import ProviderError

NAME = "Gemini"
DEFAULT_DIR = "~/.gemini"
AUTO_ENABLE = False  # limits read a stored CLI token, so it runs only when a profile is configured explicitly
CAPABILITIES = ("limits: daily quota per model from Code Assist (uses the Gemini CLI's stored Google "
                "sign-in, never refreshed or persisted); history: local session files")
API = (os.environ.get("CODE_ASSIST_ENDPOINT") or "https://cloudcode-pa.googleapis.com").rstrip("/") + "/v1internal:"
DAY = 1440  # Code Assist quotas reset daily


def detect(directory):
    return os.path.isdir(os.path.join(directory, "tmp")) or os.path.exists(os.path.join(directory, "oauth_creds.json"))


def auth_type(directory):
    try:
        settings = json.loads((Path(directory) / "settings.json").read_text())
        return ((settings.get("security") or {}).get("auth") or {}).get("selectedType")
    except (OSError, ValueError, AttributeError):
        return None


def limits(profile, state_dir, opener=urllib.request.urlopen):
    kind = auth_type(profile["dir"])
    if kind not in (None, "oauth-personal"):
        raise ProviderError("unavailable", f"{kind} sign-in: no Gemini CLI quota to show")
    try:
        creds = json.loads((Path(profile["dir"]) / "oauth_creds.json").read_text())
    except (OSError, ValueError):
        creds = None
    token = creds.get("access_token") if isinstance(creds, dict) else None
    if not token:
        raise ProviderError("unavailable", "Not signed in to Gemini CLI with Google (run `gemini`)")
    expires = model.iso_ts(creds.get("expiry_date"))
    if expires and expires <= time.time() + 60:
        raise ProviderError("auth", "Gemini sign-in expired: run `gemini` to refresh it")
    headers = {"Authorization": f"Bearer {token}"}
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT_ID")
    setup = web.fetch_json("Gemini", API + "loadCodeAssist", headers, opener=opener, body={
        "cloudaicompanionProject": project,
        "metadata": {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI",
                     "duetProject": project}})
    tier = setup.get("paidTier") or setup.get("currentTier")
    if not tier:
        reasons = {t.get("reasonCode") for t in setup.get("ineligibleTiers") or [] if isinstance(t, dict)}
        raise ProviderError("unavailable", "Google no longer serves this account through Gemini CLI"
                            if "UNSUPPORTED_CLIENT" in reasons else "Gemini CLI is not set up for this account")
    found = setup.get("cloudaicompanionProject")
    project = (found.get("id") if isinstance(found, dict) else found) or project
    try:
        quota = web.fetch_json("Gemini", API + "retrieveUserQuota", headers, opener=opener,
                               body={"project": project} if project else {})
    except ProviderError as exc:
        if getattr(exc, "status", None) == 403:  # signed in, but the plan has no Gemini CLI quota
            raise ProviderError("unavailable", "Gemini CLI quota is not available for this plan") from None
        raise
    windows = windows_from(quota.get("buckets"))
    if not windows:
        raise ProviderError("unavailable", "Gemini reported no quota for this account")
    return {"windows": windows, "plan": tier.get("name") or tier.get("id"), "source": "Code Assist quota",
            "observed_at": time.time()}


def windows_from(buckets):
    """Quota buckets -> one daily window per model (its lowest remaining fraction), Pro first."""
    lowest = {}
    for b in buckets if isinstance(buckets, list) else []:
        if isinstance(b, dict) and b.get("modelId") and isinstance(b.get("remainingFraction"), (int, float)):
            old = lowest.get(b["modelId"])
            if not old or b["remainingFraction"] < old["remainingFraction"]:
                lowest[b["modelId"]] = b
    order = sorted(lowest, key=lambda m: ("pro" not in m, "lite" in m, m))
    return [model.window_for(DAY, 100 * (1 - lowest[m]["remainingFraction"]), model.iso_ts(lowest[m].get("resetTime")),
                             short_model(m)) for m in order]


def short_model(model_id):
    """gemini-2.5-flash-lite -> 2.5 Flash Lite"""
    return model_id.removeprefix("gemini-").replace("-", " ").title()


def history(profile, store):
    for path in sorted(Path(profile["dir"]).glob("tmp/*/chats/**/*.jsonl")):
        store.scan(path, lambda line, ctx, project=project_of(path): parse_line(line, ctx, project))
    for path in sorted(Path(profile["dir"]).glob("tmp/*/chats/session-*.json")):
        # Before Gemini CLI 0.39 a session was one JSON file, rewritten on every save.
        store.scan_json(path, lambda data, project=project_of(path): [
            e for m in data.get("messages") or [] if isinstance(m, dict)
            for e in message_event(m, data.get("sessionId"), project)])


def project_of(path):
    """The project directory Gemini CLI wrote next to its chats (tmp/<project>/.project_root)."""
    for parent in path.parents:
        if parent.parent.name == "tmp":
            try:
                return (parent / ".project_root").read_text().strip() or None
            except OSError:
                return None
    return None


def parse_line(line, ctx, project=None):
    if b'"tokens"' not in line and b'"sessionId"' not in line:
        return ()
    rec = json.loads(line)
    if rec.get("sessionId") and not rec.get("id"):  # the session's first line
        ctx["session"] = rec["sessionId"]
        return ()
    return message_event(rec, ctx.get("session"), project)


def message_event(rec, session, project):
    tokens = rec.get("tokens")
    if rec.get("type") != "gemini" or not isinstance(tokens, dict) or not rec.get("id"):
        return ()
    cached, thoughts = tokens.get("cached") or 0, tokens.get("thoughts") or 0
    # Gemini counts cached tokens inside `input`; `thoughts` are billed as output.
    return (model.event("gm:" + rec["id"], model.iso_ts(rec.get("timestamp")), session, project, rec.get("model"),
                        "google", input=(tokens.get("input") or 0) - cached + (tokens.get("tool") or 0),
                        output=(tokens.get("output") or 0) + thoughts, reasoning=thoughts, cache_read=cached),)
