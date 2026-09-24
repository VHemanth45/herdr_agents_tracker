"""One HTTPS request for the opt-in providers that use a tool's stored sign-in (Grok, Gemini, Cursor).

The token is only passed in `headers`; nothing here logs, stores or echoes it.
"""

import json
import urllib.error
import urllib.request

from .model import ProviderError

TIMEOUT = 10


def fetch_json(name, url, headers, body=None, opener=urllib.request.urlopen):
    """GET (or POST `body` as JSON) and parse the reply; failures raise ProviderError with `status`
    set to the HTTP code: 401/403 are "auth", others "error" (honouring Retry-After)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", **({"Content-Type": "application/json"} if data else {}), **headers}
    try:
        with opener(urllib.request.Request(url, data=data, headers=headers), timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        exc.close()  # release the error response
        if exc.code in (401, 403):
            err = ProviderError("auth", f"{name} rejected the stored sign-in (HTTP {exc.code})")
        else:
            retry = exc.headers.get("Retry-After") if exc.headers else None
            err = ProviderError("error", f"{name} request failed (HTTP {exc.code})",
                                retry_after=int(retry) if retry and retry.isdigit() else None)
        err.status = exc.code
        raise err from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ProviderError("error", f"{name} request failed: {type(exc).__name__}") from None
