#!/usr/bin/env python3
"""Stands in for `gh api /copilot_internal/user`; FAKE_GH_MODE picks the reply."""
import os
import sys
from pathlib import Path

mode = os.environ.get("FAKE_GH_MODE", "ok")
if sys.argv[1:3] != ["api", "/copilot_internal/user"]:
    sys.exit("unexpected arguments")
if mode == "signed-out":
    sys.exit(print("To get started with GitHub CLI, please run:  gh auth login", file=sys.stderr) or 4)
if mode == "no-copilot":
    sys.exit(print("gh: Not Found (HTTP 404)", file=sys.stderr) or 1)
print((Path(__file__).parent / ("user-free.json" if mode == "free" else "user.json")).read_text())
