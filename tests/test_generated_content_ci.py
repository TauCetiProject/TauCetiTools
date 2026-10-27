"""Content CI must not execute tooling regression suites, including via reusable jobs."""

from pathlib import Path
import re
import tempfile
import unittest

import yaml

from tauceti_tools.project import load
from tauceti_tools.scaffold import initial_configuration
from tauceti_tools.templates import write_project


ROOT = Path(__file__).resolve().parents[1]
REGRESSION_COMMAND = re.compile(
    r'\b(?:python3?|bash)\s+(?:gate/)?scripts/(?:[\w/-]+/)?test_[\w-]+\.(?:py|sh)\b'
    r'|\b(?:python3?\s+-m\s+(?:unittest|pytest)|pytest|bash\s+scripts/run-infrastructure-tests\.sh)\b'
)


class ContentCISeparationTests(unittest.TestCase):
    def assert_no_regression_suites(self, path):
        document = yaml.safe_load(path.read_text())
        for job in document['jobs'].values():
            for step in job.get('steps', []):
                with self.subTest(workflow=path.name, step=step.get('name')):
                    self.assertNotRegex(step.get('run', ''), REGRESSION_COMMAND)

    def test_generated_projects_do_not_install_tooling_regression_suites(self):
        for repo in ('one/Project', 'TauCetiProject/TauCeti'):
            with self.subTest(repo=repo), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / 'tauceti.toml'
                config.write_text(initial_configuration(repo, 'human') +
                                  '\n[tools.tools]\nrepo="TauCetiProject/TauCetiTools"\nref="' + 'a' * 40 +
                                  '"\n[tools.review]\nrepo="TauCetiProject/TauCetiReview"\nref="' + 'b' * 40 + '"\n')
                project = load(str(config))
                # An existing project may own its own independent test workflow.
                owned = root / '.github/workflows/infrastructure-tests.yml'
                owned.parent.mkdir(parents=True)
                expected = '# Project-owned infrastructure checks\n'
                owned.write_text(expected)
                written = write_project(project, root)
                self.assertNotIn('.github/workflows/infrastructure-tests.yml', written)
                self.assertEqual(owned.read_text(), expected)
                for name in ('ci.yml', 'lint-full.yml', 'pr-build.yml'):
                    self.assert_no_regression_suites(root / '.github/workflows' / name)
                ci = yaml.safe_load((root / '.github/workflows/ci.yml').read_text())['jobs']
                self.assertNotIn('readiness-tests', ci)
                self.assertNotIn('duplicate-audit-tests', ci)
                self.assertEqual(ci['publish-lake-cache']['needs'], ['build'])

    def test_reusable_pr_builder_does_not_execute_regression_suites(self):
        self.assert_no_regression_suites(ROOT / '.github/workflows/pr-build.yml')


if __name__ == '__main__':
    unittest.main()
