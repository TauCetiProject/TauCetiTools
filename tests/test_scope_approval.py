"""Run the shipped Bors approval tests against Tools' reusable workflow."""

import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "tauceti_tools/assets/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("shipped_scope_tests", SCRIPTS / "test_bors_scope_approval.py")
scope_tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope_tests)
scope_tests.ROOT = ROOT


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromModule(scope_tests)
