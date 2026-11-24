"""The sandboxed build's phases, run one per workflow step, behave as the single invocation did."""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tauceti_tools/assets/scripts/sandbox-build.sh'
WORKFLOW = ROOT / '.github/workflows/pr-build.yml'

STUB = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
with open(os.environ['CALLS'], 'a') as f:
    f.write(json.dumps([name, *sys.argv[1:], 'cache=' + os.environ.get('LAKE_ARTIFACT_CACHE', '')]) + '\\n')
def status(key):
    return int(os.environ.get(key, '0'))
if name == 'lake':
    if sys.argv[1] == 'build':
        if '-o' in sys.argv:
            Path(sys.argv[sys.argv.index('-o') + 1]).write_text('{}\\n')
        sys.exit(status('BUILD_STATUS'))
    if sys.argv[1:3] == ['cache', 'stage']:
        sys.exit(status('STAGE_STATUS'))
    os.execvp(sys.argv[2], sys.argv[2:])
if name == 'audit-driver':
    sys.exit(status('DUPLICATE_STATUS' if '--duplicates-only' in sys.argv else 'FULL_STATUS'))
if name == 'local-axioms':
    sys.exit(status('LOCAL_STATUS'))
if name == 'lean':
    sys.exit(status('MODULE_SYSTEM_STATUS'))
'''


def phases():
    text = SCRIPT.read_text()
    return re.search(r'^PHASES=\(([^)]*)\)$', text, re.M)[1].split()


class SandboxPhases(unittest.TestCase):
    def run_sandbox(self, invocations, *, scoped=True, staging=False, cache=False, batch=False, **statuses):
        """Run sandbox-build.sh once per argument list, stopping at the first failure as the
        workflow's consecutive steps do. Returns the last status, the calls and the phase names."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('bin', 'trusted', 'drivers', 'watchdog/bin', '.lake/tmp'):
                (root / name).mkdir(parents=True)
            scope = root / 'modules.txt'
            scope.write_text('TauCeti.Changed\n')
            (root / 'lakefile.toml').write_text('platformIndependent = true\n')
            for name, key in (('lint-env.sh', 'LINT_ENV_STATUS'), ('lint-style.sh', 'LINT_STYLE_STATUS'),
                              ('declaration-names.sh', None)):
                (root / 'trusted' / name).write_text(
                    "python3 -c 'import json, os, sys; open(os.environ[\"CALLS\"], \"a\")"
                    f".write(json.dumps([\"{name}\", *sys.argv[1:]]) + \"\\n\")' \"$@\"\n"
                    + (f'exit "${{{key}:-0}}"\n' if key else 'exit 0\n'))
            (root / 'trusted/ci_log_filter.py').write_text('import sys\nsys.stdout.write(sys.stdin.read())\n')
            # Exits with the next of the comma-separated OUTPUT_CHECK_STATUSES (0 once they run out).
            (root / 'trusted/lake_output_check.py').write_text(
                'import json, os, sys\nfrom pathlib import Path\n'
                'with open(os.environ["CALLS"], "a") as f: f.write(json.dumps(["lake_output_check", *sys.argv[1:]]) + "\\n")\n'
                'counter = Path(os.environ["CALLS"] + ".checks")\n'
                'n = int(counter.read_text()) if counter.exists() else 0\ncounter.write_text(str(n + 1))\n'
                'statuses = [s for s in os.environ.get("OUTPUT_CHECK_STATUSES", "").split(",") if s]\n'
                'sys.exit(int(statuses[n]) if n < len(statuses) else 0)\n')
            (root / 'trusted/bulk-unpack-cache.py').write_text(
                'import json, os\nwith open(os.environ["CALLS"], "a") as f: '
                'f.write(json.dumps(["bulk-unpack-cache"]) + "\\n")\n')
            for name in ('bin/lake', 'drivers/audit-driver', 'drivers/local-axioms',
                         'drivers/lint-env-driver', 'watchdog/bin/lean'):
                path = root / name
                path.write_text(STUB)
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(root / 'bin') + ':' + os.environ['PATH'],
                       CALLS=str(root / 'calls.jsonl'), TAUCETI_TRUSTED_SCRIPTS=str(root / 'trusted'),
                       WATCHDOG_TOOLCHAIN=str(root / 'watchdog'),
                       LINT_DRIVER_EXE=str(root / 'drivers/lint-env-driver'),
                       AXIOMS_ONLY_MODULES=str(scope) if scoped else '',
                       STAGE_LAKE_CACHE='1' if staging else '',
                       IS_BATCH='true' if batch else '',
                       LAKE_ARTIFACT_CACHE='true' if cache else '',
                       LAKE_RESTORE_ARTIFACTS='true' if cache else '',
                       **{key: str(value) for key, value in statuses.items()})
            status = 0
            for arguments in invocations:
                result = subprocess.run(['bash', str(SCRIPT), *arguments], cwd=root, env=env,
                                        text=True, capture_output=True)
                status = result.returncode
                if status != 0:
                    break
            calls_file = root / 'calls.jsonl'
            calls = [json.loads(line.replace(str(root), '<root>')) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
            phases_file = root / '.lake/telemetry/phases.tsv'
            names = [line.split('\t')[0] for line in phases_file.read_text().splitlines()] \
                if phases_file.exists() else []
            return status, calls, names

    def test_phase_steps_match_the_single_invocation(self):
        scenarios = [
            {}, {'scoped': False}, {'cache': True}, {'staging': True, 'scoped': False},
            {'BUILD_STATUS': 3}, {'DUPLICATE_STATUS': 7}, {'LOCAL_STATUS': 9},
            {'scoped': False, 'FULL_STATUS': 11}, {'MODULE_SYSTEM_STATUS': 4},
            {'LINT_ENV_STATUS': 5}, {'LINT_STYLE_STATUS': 6}, {'staging': True, 'STAGE_STATUS': 8},
        ]
        for scenario in scenarios:
            with self.subTest(**scenario):
                whole = self.run_sandbox([[]], **scenario)
                split = self.run_sandbox([[phase] for phase in phases()], **scenario)
                self.assertEqual(split, whole)
                if not any(key.endswith('_STATUS') for key in scenario):
                    self.assertEqual(whole[0], 0)
                    self.assertEqual(whole[2][-1], 'end')

    def test_only_a_staged_batch_build_fails_fast(self):
        # A landing candidate stops at its first failure: the queue's repair worker builds the
        # whole library itself. Every other build reports every broken module.
        for scenario, expected in (
            ({}, False), ({'staging': True}, False), ({'batch': True}, False),
            ({'staging': True, 'batch': True}, True),
        ):
            with self.subTest(**scenario):
                _, calls, _ = self.run_sandbox([['build']], **scenario)
                builds = [call for call in calls if call[:2] == ['lake', 'build']]
                self.assertEqual(len(builds), 1)
                self.assertEqual('--fail-fast' in builds[0], expected)
                self.assertIn('--iofail', builds[0])

    def test_staged_outputs_are_checked_against_their_traces(self):
        # Batch 506 attempt 339: a truncated restored .olean.private kept its trace, so Lake trusted
        # it. A staging build checks every output and compiles the damaged modules from source.
        def run(statuses="", **scenario):
            status, calls, names = self.run_sandbox([['build']], staging=True, batch=True,
                                                    OUTPUT_CHECK_STATUSES=statuses, **scenario)
            return status, [call[:2] for call in calls if call[0] in ('lake', 'lake_output_check')], names

        build, check = ['lake', 'build'], ['lake_output_check', 'discard']
        self.assertEqual(run(), (0, [build, check], ['cache-unpack', 'build', 'output-check']))
        self.assertEqual(run('3'), (0, [build, check, build, check],
                                    ['cache-unpack', 'build', 'output-check', 'build', 'output-check']))
        # A rebuild that reaches further damage repairs that too, at most twice.
        self.assertEqual(run('3,3')[:2], (0, [build, check, build, check, build, check]))
        self.assertEqual(run('3,3,3')[0], 1)
        # A failed build is checked too (a damaged module can fail its dependents' compilation) and
        # keeps its own status; a check that cannot run blocks only a passing build's publication.
        self.assertEqual(run(BUILD_STATUS=5)[:2], (5, [build, check]))
        self.assertEqual(run('2')[0], 1)
        self.assertEqual(run('2', BUILD_STATUS=5)[0], 5)
        # Ordinary PR builds publish nothing and skip the check.
        _, calls, _ = self.run_sandbox([['build']], cache=True)
        self.assertNotIn('lake_output_check', [call[0] for call in calls])

    def test_each_phase_runs_only_its_own_work(self):
        _, calls, names = self.run_sandbox([['lint-style']])
        self.assertEqual([call[0] for call in calls], ['lint-style.sh'])
        self.assertEqual(names, ['lint-style'])

    def test_unknown_phases_run_nothing(self):
        for arguments in (['lint'], ['build', 'audits'], ['']):
            with self.subTest(arguments=arguments):
                status, calls, names = self.run_sandbox([arguments])
                self.assertEqual((status, calls, names), (2, [], []))


class WorkflowPhaseSteps(unittest.TestCase):
    def setUp(self):
        self.steps = yaml.safe_load(WORKFLOW.read_text())['jobs']['sandboxed-build']['steps']
        self.indices = [i for i, step in enumerate(self.steps) if 'SANDBOX_PHASE' in step.get('env', {})]
        self.phase_steps = [self.steps[i] for i in self.indices]

    def test_one_contiguous_step_per_phase_in_order(self):
        self.assertEqual([step['env']['SANDBOX_PHASE'] for step in self.phase_steps], phases())
        self.assertEqual(self.indices, list(range(self.indices[0], self.indices[0] + len(phases()))))
        names = [step['name'] for step in self.phase_steps]
        self.assertEqual(len(set(names)), len(names))
        self.assertEqual([name for name in names if 'Build exact candidate under bwrap' in name],
                         [names[0]])

    def test_phase_steps_share_one_sandbox_invocation_and_environment(self):
        first = self.phase_steps[0]
        self.assertIn('sandbox-build.sh "$1"\' sandbox-build "$SANDBOX_PHASE"', first['run'])
        for step in self.phase_steps:
            with self.subTest(step=step['name']):
                self.assertEqual(step['run'], first['run'])
                expected = ("${{ env.INFRA != '1' }}" if step is first else
                            "${{ !cancelled() && env.INFRA != '1' && (success() || "
                            "(env.IS_BATCH == 'true' && steps.sandbox_build.outcome == 'success')) }}")
                self.assertEqual(step['if'], expected)
                self.assertEqual({k: v for k, v in step['env'].items() if k != 'SANDBOX_PHASE'},
                                 {k: v for k, v in first['env'].items() if k != 'SANDBOX_PHASE'})
                self.assertNotIn('secrets.', json.dumps(step))
                self.assertNotIn('continue-on-error', step)

    def test_build_step_combines_the_phase_outcomes(self):
        build = self.steps[self.indices[-1] + 1]
        self.assertEqual(build['id'], 'build')
        ids = [step['id'] for step in self.phase_steps]
        self.assertEqual(re.findall(r'\$\{\{ steps\.(\w+)\.outcome \}\}', build['env']['OUTCOMES']), ids)
        condition = build['if'].strip()
        self.assertTrue(condition.startswith('${{ always() && ') and condition.endswith('}}'))
        self.assertEqual(sorted(set(re.findall(r'steps\.(\w+)\.outcome', condition))), sorted(ids))

        def runs(outcomes):
            """Evaluate the step's `if` (GitHub's && || format contains) for the given outcomes."""
            expression = condition[3:-2].replace('&&', ' and ').replace('||', ' or ').replace('always()', 'True')
            expression = re.sub(r'steps\.(\w+)\.outcome', lambda m: repr(outcomes[m[1]]), expression)
            return eval(expression, {'contains': lambda text, item: item in text,  # noqa: S307
                                     'format': lambda text, *values: text.format(*values)})

        def combined(outcomes):
            """The `build` step's outcome: skipped, or its script's result."""
            if not runs(dict(zip(ids, outcomes, strict=True))):
                return 'skipped'
            result = subprocess.run(['bash', '-e', '-c', build['run']], capture_output=True, text=True,
                                    env=dict(os.environ, OUTCOMES=' '.join(outcomes)))
            return 'success' if result.returncode == 0 else 'failure'

        for outcomes, expected in (
            (['success'] * 6, 'success'),
            (['success', 'failure'] + ['skipped'] * 4, 'failure'),
            (['success'] * 5 + ['failure'], 'failure'),
            (['failure'] + ['skipped'] * 5, 'failure'),
            # A cancellation that interrupts a phase, or comes between two phases.
            (['success', 'cancelled'] + ['skipped'] * 4, 'skipped'),
            (['success', 'success'] + ['skipped'] * 4, 'skipped'),
            # The build never started.
            (['skipped'] * 6, 'skipped'),
        ):
            with self.subTest(outcomes=outcomes):
                self.assertEqual(combined(outcomes), expected)

    def test_reporting_attributes_each_phase_step(self):
        import importlib.util
        import sys
        from unittest import mock

        from tauceti_tools.collector import classify

        scripts = ROOT / 'tauceti_tools/assets/scripts'
        spec = importlib.util.spec_from_file_location('ci_daily_graphs', scripts / 'ci_daily_graphs.py')
        graphs = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, 'path', [str(scripts), *sys.path]):
            spec.loader.exec_module(graphs)
        expected = {'build': 'build', 'audits': 'audits', 'module-system': 'audits',
                    'lint-env': 'lints', 'lint-style': 'lints', 'finish': 'build'}
        for step in self.phase_steps:
            with self.subTest(step=step['name']):
                self.assertEqual(graphs.step_phase(step['name']), expected[step['env']['SANDBOX_PHASE']])
                # A failing phase step is classified from its log lines, as the single step was.
                self.assertEqual(classify.classify('failure', step['name'], ['LINT-ENV: FAIL']), 'lint-env')
        legacy = 'Build exact candidate under bwrap with workflow-pinned audits, offline'
        self.assertEqual(graphs.step_phase(legacy), 'build, audits, lints')


if __name__ == '__main__':
    unittest.main()
