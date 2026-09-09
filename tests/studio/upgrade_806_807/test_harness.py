# SPDX-License-Identifier: AGPL-3.0-only
"""Local checks for evidence handling; never installs or launches Studio."""
import json
from pathlib import Path
import tempfile
import unittest

from run_upgrade import Run


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parent / ".test-state"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.run = Run(self.root)
        self.run.studio = self.root / "fake-studio"

    def tearDown(self):
        self.temp.cleanup()

    def test_collect_only_preserves_timing_evidence(self):
        self.run.timings = [{"phase": "native-upgrade", "seconds": 123.4, "exit_code": 0}]
        self.run.save_timings()
        resumed = Run(self.root)
        resumed.studio = self.run.studio
        resumed.collect()
        self.assertEqual(json.loads((resumed.public / "timings.json").read_text()), self.run.timings)

    def test_redacts_bootstrap_secret_and_retains_native_phase_timestamps(self):
        auth = self.run.studio / "auth"
        auth.mkdir(parents=True)
        (auth / ".bootstrap_password").write_text("fixture-secret-not-real")
        (self.run.raw / "bootstrap.log").write_text(
            "ordinary progress\nfixture-secret-not-real\nAuthorization: Bearer fixture-token\n"
        )
        logs = self.run.studio / "logs"
        logs.mkdir()
        (logs / "update-test-s00.log").write_text(
            "[1700000000000][stdout] [TAURI:STEP] clone\n"
            "[1700000012500][stdout] [TAURI:STEP] update\n"
        )
        self.run.collect()
        text = (self.run.public / "bootstrap.log").read_text()
        self.assertIn("ordinary progress", text)
        self.assertNotIn("fixture-secret-not-real", text)
        self.assertNotIn("fixture-token", text)
        phases = json.loads((self.run.public / "native-phase-timings.json").read_text())
        self.assertEqual(phases[0]["seconds_to_next_marker"], 12.5)


if __name__ == "__main__":
    unittest.main()
