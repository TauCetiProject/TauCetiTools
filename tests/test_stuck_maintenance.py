"""Exercise installed watchdog and maintenance recovery entrypoints in CI."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MaintenanceScriptTests(unittest.TestCase):
    def test_watchdog_and_recovery_regressions(self):
        for script in ("test_maintenance_builds.py", "pr_status/test_stuck_snapshot.py", "pr_status/test_stuck_alerts.py"):
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, str(ROOT / "tauceti_tools/assets/scripts" / script)],
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dispatched_build_discovery_regressions(self):
        result = subprocess.run([
            sys.executable, str(ROOT / "tauceti_tools/assets/scripts/pr_status/test_batch.py"),
            "ResumeTests.test_dispatched_builds_recheck_prs_without_trusting_the_workflow_head",
            "ResumeTests.test_only_an_exact_dispatched_head_can_be_painted_running",
        ], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
