"""Run the shared picker/recovery regressions in the package CI suite."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts"))
import test_fleet_runner


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromModule(test_fleet_runner)
