#!/usr/bin/env python3
"""Stands in for `amp usage`; FAKE_AMP_MODE picks the reply (a usage-<mode>.txt fixture)."""
import os
import sys
from pathlib import Path

if sys.argv[1:] != ["usage"]:
    sys.exit("unexpected arguments")
mode = os.environ.get("FAKE_AMP_MODE", "subscription")
if mode == "signed-out":
    sys.exit(print("Error: not logged in. Run `amp login` first.", file=sys.stderr) or 1)
if mode == "hang":
    import time
    time.sleep(30)
print((Path(__file__).parent / f"usage-{mode}.txt").read_text(), end="")
