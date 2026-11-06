"""Exercise installed lint scope with real Git graphs and canned label policy."""

import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = tempfile.TemporaryDirectory(prefix="installed-lint-scope-")
project = Path(FIXTURE.name)
shutil.copytree(ROOT / "tauceti_tools/assets/scripts", project / "scripts")
(project / ".github/workflows").mkdir(parents=True)
shutil.copyfile(ROOT / ".github/workflows/pr-build.yml",
                project / ".github/workflows/pr-build.yml")
sys.path.insert(0, str(project / "scripts"))
spec = importlib.util.spec_from_file_location("installed_lint_scope_tests",
                                             project / "scripts/test_lint_scope.py")
scope_tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope_tests)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([
        loader.loadTestsFromTestCase(scope_tests.LintScopeTest),
        loader.loadTestsFromTestCase(scope_tests.PolicyLookupTest),
    ])
