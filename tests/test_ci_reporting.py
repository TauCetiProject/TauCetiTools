"""Run the shared CI chart regression suite in package CI."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts"))
import test_ci_graphs


def load_tests(loader, tests, pattern):
    tests.addTests(loader.loadTestsFromModule(test_ci_graphs))
    return tests


class RunnerIdentityTests(unittest.TestCase):
    def test_older_runner_names_and_conflicting_evidence(self):
        from tauceti_tools.assets.scripts.ci_runners import reporting_kind
        name = "fleet-11e2756fc110-5b75de8785749239"
        self.assertEqual(reporting_kind("self-hosted", ["tauceti-fleet"], name), "leanfro")
        self.assertEqual(reporting_kind("self-hosted", ["fleet-host-chris-server"], name), "self-hosted")
        self.assertEqual(reporting_kind("github", [], name), "github")
        self.assertEqual(reporting_kind("self-hosted", [], "not-a-fleet-runner"), "self-hosted")
