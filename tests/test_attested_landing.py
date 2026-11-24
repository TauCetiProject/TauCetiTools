"""pr-build.yml's attested landing (`tauceti-bors-attested`, decision 200): a staging run without a build."""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_TEXT = (ROOT / '.github/workflows/pr-build.yml').read_text()
PUBLISHER_TEXT = (ROOT / '.github/workflows/publish-lake-cache.yml').read_text()
WORKFLOW = yaml.safe_load(WORKFLOW_TEXT)
CALLER = (ROOT / 'tauceti_tools/assets/project/.github/workflows/pr-build.yml').read_text()
STEPS = WORKFLOW['jobs']['sandboxed-build']['steps']


def step(**match):
    [found] = [s for s in STEPS if all(s.get(k) == v for k, v in match.items())]
    return found


class AttestedLanding(unittest.TestCase):
    def test_every_staging_expression_also_takes_the_attested_action(self):
        """Routing, identity, the repair-cache fetch, the finalizer and the run title treat it as staging."""
        staging = re.findall(r"github\.event\.action == 'tauceti-bors-staging'[^)]*\)", WORKFLOW_TEXT + PUBLISHER_TEXT)
        self.assertTrue(staging)
        for expression in staging:
            self.assertIn("github.event.action == 'tauceti-bors-attested'", expression)
        self.assertIn("ATTESTED: ${{ github.event_name == 'repository_dispatch' && github.event.action == "
                      "'tauceti-bors-attested' }}", WORKFLOW_TEXT)

    def test_identity_requires_the_queues_attestation_of_the_exact_commit_and_tree(self):
        run = step(id='pr')['run']
        confirmation = run.index('gate/scripts/bors_attested.py')
        self.assertLess(run.index('ci_git.py resolve'), confirmation, 'after the commit identity is resolved')
        self.assertIn('--tree "$TREE"', run)
        self.assertIn("echo 'INFRA=1' >> \"$GITHUB_ENV\"", run[confirmation:])

    def test_no_audit_or_lint_phase_runs_and_the_build_restores_without_building(self):
        for sid in ('sandbox_audits', 'sandbox_module_system', 'sandbox_lint_env', 'sandbox_lint_style'):
            self.assertIn("env.ATTESTED != 'true'", step(id=sid)['if'])
        for sid in ('sandbox_build', 'sandbox_finish'):
            self.assertNotIn('ATTESTED', step(id=sid)['if'])
        self.assertIn('--setenv ATTESTED_BUILD "${{ env.ATTESTED == \'true\' && \'1\' || \'\' }}"',
                      step(id='sandbox_build')['run'])

    def test_the_caller_template_subscribes_and_titles_it_as_bors_staging(self):
        self.assertIn('tauceti-bors-attested', re.search(r'repository_dispatch:\n(?:\s*#.*\n)*\s*types: \[([^]]*)\]',
                                                          CALLER)[1])
        title = CALLER.split('\n', 3)[2]
        self.assertIn("github.event.action == 'tauceti-bors-attested'", title)
        self.assertIn("format('bors branch=", title)


if __name__ == '__main__':
    unittest.main()
