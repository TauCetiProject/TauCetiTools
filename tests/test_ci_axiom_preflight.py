"""Exercise the actual offline build script's scoped/full axiom dispatch and exit statuses."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tauceti_tools/assets/scripts/sandbox-build.sh'


class CIAxiomPreflightTests(unittest.TestCase):
    def sandbox(self, *, scoped=True, empty=False, duplicate_status=0, local_status=0, full_status=0):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('bin', 'trusted', 'drivers', 'watchdog/bin', '.lake/tmp'):
                (root / name).mkdir(parents=True)
            scope = root / 'modules.txt'
            scope.write_text('' if empty else 'TauCeti.Changed\n')
            for name in ('declaration-names.sh', 'lint-env.sh', 'lint-style.sh'):
                (root / 'trusted' / name).write_text('exit 0\n')
            (root / 'trusted/ci_log_filter.py').write_text('import sys\nsys.stdout.write(sys.stdin.read())\n')
            stub = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
with open(os.environ['CALLS'], 'a') as f:
    f.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
if name == 'lake':
    if sys.argv[1] == 'build': sys.exit(0)
    os.execvp(sys.argv[2], sys.argv[2:])
if name == 'audit-driver':
    sys.exit(int(os.environ['DUPLICATE_STATUS'] if '--duplicates-only' in sys.argv else os.environ['FULL_STATUS']))
if name == 'local-axioms': sys.exit(int(os.environ['LOCAL_STATUS']))
'''
            for name in ('bin/lake', 'drivers/audit-driver', 'drivers/local-axioms',
                         'drivers/lint-env-driver', 'watchdog/bin/lean'):
                path = root / name
                path.write_text(stub)
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(root / 'bin') + ':' + os.environ['PATH'],
                       CALLS=str(root / 'calls.jsonl'), TAUCETI_TRUSTED_SCRIPTS=str(root / 'trusted'),
                       WATCHDOG_TOOLCHAIN=str(root / 'watchdog'), LINT_DRIVER_EXE=str(root / 'drivers/lint-env-driver'),
                       AXIOMS_ONLY_MODULES=str(scope) if scoped else '', STAGE_LAKE_CACHE='',
                       DUPLICATE_STATUS=str(duplicate_status), LOCAL_STATUS=str(local_status), FULL_STATUS=str(full_status))
            result = subprocess.run(['bash', str(SCRIPT)], cwd=root, env=env, text=True, capture_output=True)
            calls = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()]
            return result, calls, str(scope), str(root / '.lake/build/lib/lean')

    def test_scoped_pr_checks_global_duplicates_then_own_compiled_declarations(self):
        result, calls, scope, artifacts = self.sandbox()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        audit = [c for c in calls if c[0] == 'audit-driver']
        self.assertEqual(audit, [['audit-driver', '--duplicates-only']])
        self.assertEqual([c for c in calls if c[0] == 'local-axioms'],
                         [['local-axioms', scope, 'TauCeti', artifacts]])
        self.assertLess(next(i for i, c in enumerate(calls) if c[0] == 'audit-driver'),
                        next(i for i, c in enumerate(calls) if c[0] == 'local-axioms'))

    def test_empty_scope_stays_scoped(self):
        result, calls, _, _ = self.sandbox(empty=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(['audit-driver', '--duplicates-only'], calls)
        self.assertEqual(sum(c[0] == 'local-axioms' for c in calls), 1)

    def test_full_audit_is_unchanged(self):
        result, calls, _, _ = self.sandbox(scoped=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(['audit-driver'], calls)
        self.assertFalse(any(c[0] == 'local-axioms' for c in calls))

    def test_findings_keep_the_audit_exit_status(self):
        for arguments, expected in (({'duplicate_status': 7}, 7), ({'local_status': 9}, 9),
                                    ({'scoped': False, 'full_status': 11}, 11)):
            with self.subTest(arguments=arguments):
                result, calls, _, _ = self.sandbox(**arguments)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                if 'duplicate_status' in arguments:
                    self.assertFalse(any(c[0] == 'local-axioms' for c in calls))
                self.assertFalse(any(c[0] == 'lean' for c in calls))

    def test_existing_workflow_selects_full_audits_for_batches_and_pin_changes(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/pr-build.yml').read_text())
        step = next(s for job in workflow['jobs'].values() for s in job.get('steps', [])
                    if s.get('name') == 'Decide the audit scope')
        for batch, bump, unmatched, expect_scoped in (
            ('false', '0', '', True), ('true', '0', '', False),
            ('false', '1', '', False), ('false', '0', 'unknown/file\n', False),
        ):
            with self.subTest(batch=batch, bump=bump, unmatched=unmatched), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'modules.txt').write_text('TauCeti.Changed\n')
                (root / 'unmatched.txt').write_text(unmatched)
                output = root / 'environment'
                env = dict(os.environ, LINT_ONLY_MODULES=str(root / 'modules.txt'), LINT_SCOPE_DIR=str(root),
                           GITHUB_ENV=str(output), BUMP=bump)
                result = subprocess.run(['bash', '-euo', 'pipefail', '-c',
                                         step['run'].replace('${{ env.IS_BATCH }}', batch)],
                                        env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                values = dict(line.split('=', 1) for line in output.read_text().splitlines())
                self.assertEqual(bool(values['AXIOMS_ONLY_MODULES']), expect_scoped)


if __name__ == '__main__':
    unittest.main()
