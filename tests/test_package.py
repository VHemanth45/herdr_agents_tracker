"""The shipped package: the manifest matches the code, and the launcher starts on this machine."""

import subprocess
import tomllib
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts the repo root on sys.path)
from usage_tracker import PLUGIN_ID, VERSION

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINTS = ("build", "startup", "events", "actions", "panes")


class Manifest(unittest.TestCase):
    def setUp(self):
        self.manifest = tomllib.loads((ROOT / "herdr-plugin.toml").read_text())

    def test_id_and_version_match_the_code(self):
        self.assertEqual(self.manifest["id"], PLUGIN_ID)
        self.assertEqual(self.manifest["version"], VERSION)

    def test_every_entrypoint_runs_the_launcher(self):
        for key in ENTRYPOINTS:
            self.assertTrue(self.manifest.get(key), f"no [[{key}]] in the manifest")
            for entry in self.manifest[key]:
                self.assertIn("bin/usage-tracker", " ".join(entry["command"]))

    def test_the_build_step_reports_the_version(self):
        """What `herdr plugin install` runs before registering the plugin (Python 3.11+ check)."""
        command = self.manifest["build"][0]["command"]
        done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, f"usage-tracker {VERSION}"))


if __name__ == "__main__":
    unittest.main()
