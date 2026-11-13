"""Exercise installed lint scope with real Git graphs and canned label policy."""

import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

import yaml

from tauceti_tools.project import parse
from tauceti_tools.scaffold import initial_configuration
from tauceti_tools.templates import render


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


class GeneratedRepairLoginTest(unittest.TestCase):
    """A generated project's caller gives pr-build the login of its automation App's repair PRs."""

    def test_dotted_repository_name(self):
        # GitHub's slug of the App name `Example.Ceti-automation`, as for `deepsource.io`.
        app = "example-ceti-automation[bot]"
        config = parse(initial_configuration("o/Example.Ceti", "human").encode())
        caller = yaml.safe_load(render(
            (ROOT / "tauceti_tools/assets/project/.github/workflows/pr-build.yml").read_text(), config))
        login = caller["jobs"]["validate"]["with"]["repair_bot_login"]
        self.assertEqual(login, app)
        guard = scope_tests.RepairExemptionTest("repair_exemption")
        env = {"REPAIR_BOT_LOGIN": login, "PR_HEAD_REF": "lint-repair/main", "PR_HEAD_REPO": "o/r",
               "PR_USER": app}
        self.assertEqual(guard.repair_exemption("pull_request_target", "", env=env), (True, False))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([
        loader.loadTestsFromTestCase(scope_tests.LintScopeTest),
        loader.loadTestsFromTestCase(scope_tests.PolicyLookupTest),
        loader.loadTestsFromTestCase(scope_tests.RepairExemptionTest),
        loader.loadTestsFromTestCase(GeneratedRepairLoginTest),
    ])
