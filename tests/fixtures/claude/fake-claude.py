#!/usr/bin/env python3
"""Stand-in for `claude -p --input-format stream-json` in tests: answers the get_usage control request."""
import json, os, sys, time
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
assert "--safe-mode" in sys.argv and "--no-session-persistence" in sys.argv, sys.argv
for line in sys.stdin:
    msg = json.loads(line)
    if mode == "hang":
        time.sleep(30)
    request = msg.get("request") or {}
    if msg.get("type") != "control_request" or request.get("subtype") != "get_usage":
        continue
    print(json.dumps({"type": "system", "subtype": "status"}), flush=True)
    if mode == "signed-out":
        reply = {"subtype": "error", "request_id": msg["request_id"], "error": "Not logged in · Please run /login"}
    else:
        limits = {"five_hour": {"utilization": 18, "resets_at": "2026-09-22T11:30:00+00:00"},
                  "seven_day": {"utilization": 19, "resets_at": "2026-09-23T21:00:00+00:00"},
                  "seven_day_opus": None, "seven_day_omelette": {"utilization": 3, "resets_at": None},
                  "seven_day_breakdown": {"rows": []},
                  "model_scoped": [{"display_name": "Fable", "utilization": 0, "resets_at": "2026-09-23T21:00:00+00:00"}]}
        reply = {"subtype": "success", "request_id": msg["request_id"],
                 "response": {"subscription_type": "max", "rate_limits_available": mode != "apikey",
                              "rate_limits": None if mode == "apikey" else limits}}
    print(json.dumps({"type": "control_response", "response": reply}), flush=True)
