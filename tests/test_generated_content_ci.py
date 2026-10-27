"""Generated content CI and publication do not depend on tooling regression suites."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from tauceti_tools.project import load
from tauceti_tools.scaffold import initial_configuration
from tauceti_tools.templates import materialize, write_project


class ContentCISeparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = self.root / 'tauceti.toml'
        config.write_text(initial_configuration('one/Project', 'human') +
                          '\n[tools.tools]\nrepo="TauCetiProject/TauCetiTools"\nref="' + 'a' * 40 +
                          '"\n[tools.review]\nrepo="TauCetiProject/TauCetiReview"\nref="' + 'b' * 40 + '"\n')
        self.project = load(str(config))
        write_project(self.project, self.root)

    def workflow(self, name):
        return yaml.safe_load((self.root / '.github/workflows' / name).read_text())

    def test_only_infrastructure_changes_run_the_additional_suites(self):
        infra = self.workflow('infrastructure-tests.yml')
        events = infra.get('on', infra.get(True))
        self.assertEqual(set(events), {'push', 'pull_request'})
        for value in events.values():
            self.assertEqual(set(value['paths']), {'scripts/**', '.github/**'})
        self.assertEqual(infra['permissions'], {'contents': 'read'})
        ci = self.workflow('ci.yml')['jobs']
        self.assertEqual(set(ci), {'build', 'publish-lake-cache'})
        self.assertEqual(ci['publish-lake-cache']['needs'], ['build'])
        for filename in ('ci.yml', 'lint-full.yml'):
            for job in self.workflow(filename)['jobs'].values():
                for step in job.get('steps', []):
                    self.assertNotIn('python3 scripts/test_', step.get('run', ''))

    def test_setup_restores_the_reviewed_workflow_test_after_materialization(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        test = self.root / 'scripts/test_bors_scope_approval.py'
        expected = '# Corrected workflow contract from the candidate revision.\n'
        test.write_text(expected)
        subprocess.run(['git', 'add', '.'], cwd=self.root, check=True)
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '-qm', 'Fixture'], cwd=self.root, check=True)
        shared = self.root / 'shared'
        materialize(self.project, shared, policy_dir=self.root / 'scripts')
        shutil.copytree(shared, self.root / 'scripts', dirs_exist_ok=True)
        self.assertNotEqual(test.read_text(), expected)
        steps = self.workflow('infrastructure-tests.yml')['jobs']['python-tests']['steps']
        restore = next(s['run'] for s in steps if s.get('name') ==
                       'Restore workflow contract tests after shared setup')
        subprocess.run(['bash', '-euo', 'pipefail', '-c', restore], cwd=self.root, check=True)
        self.assertEqual(test.read_text(), expected)


if __name__ == '__main__':
    unittest.main()
