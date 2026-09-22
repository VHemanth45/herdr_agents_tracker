#!/usr/bin/env python3
"""Stand-in for `codex app-server` in tests: answers initialize, account/read, account/rateLimits/read."""
import json, os, sys, time
mode = os.environ.get("FAKE_CODEX_MODE", "ok")
limits = json.load(open(os.path.join(os.path.dirname(__file__), "app-server-ratelimits.json")))
for line in sys.stdin:
    msg = json.loads(line)
    if mode == "hang":
        time.sleep(30)
    if msg.get("id") == 0:
        print(json.dumps({"id": 0, "result": {"userAgent": "fake"}}), flush=True)
        print(json.dumps({"method": "some/notification", "params": {}}), flush=True)
    elif msg.get("method") == "account/read":
        account = {"apikey": {"type": "apiKey"}, "signed-out": None}.get(mode, {"type": "chatgpt", "email": "user@example.invalid", "planType": "prolite"})
        print(json.dumps({"id": msg["id"], "result": {"account": account, "requiresOpenaiAuth": True}}), flush=True)
    elif msg.get("method") == "account/rateLimits/read":
        if mode == "auth-error":
            print(json.dumps({"id": msg["id"], "error": {"code": -32000, "message": "401 Unauthorized: please log in"}}), flush=True)
        else:
            print(json.dumps({"id": msg["id"], "result": limits}), flush=True)
