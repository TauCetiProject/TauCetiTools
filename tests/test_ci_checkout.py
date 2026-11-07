"""Run the shipped checkout fixtures against the same implementation CI receives."""

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "tauceti_tools/assets/scripts/test_ci_checkout.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("ci_checkout_tests", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AssetCopies(unittest.TestCase):
    def test_shipped_copies_match(self):
        scripts = SCRIPT.parent
        for name in ("ci_checkout.py", "test_ci_checkout.py"):
            self.assertEqual((scripts / name).read_bytes(), (scripts.parent / "project/scripts" / name).read_bytes())


def load_tests(loader, tests, pattern):
    tests.addTests(loader.loadTestsFromModule(module))
    return tests
