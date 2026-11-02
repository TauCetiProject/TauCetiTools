"""Exercise installed watchdog and maintenance recovery entrypoints in CI."""
import subprocess
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MaintenanceScriptTests(unittest.TestCase):
    def test_watchdog_and_recovery_regressions(self):
        for script in ("test_maintenance_builds.py", "pr_status/test_stuck_snapshot.py", "pr_status/test_stuck_alerts.py"):
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, str(ROOT / "tauceti_tools/assets/scripts" / script)],
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
