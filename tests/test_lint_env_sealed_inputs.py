"""lint-env.sh's module lists and docstring-scan source are sealed before candidate code runs.

Candidate code runs during the docstring scan. In the sandbox $TMPDIR is under the candidate's
`.lake`, so lists generated there could be swapped for a smaller set before the `#lint` driver
reads them, and the lint would pass with reduced coverage while reporting full scope.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / 'tauceti_tools/assets/scripts'
WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/pr-build.yml'
MODULES = ['A', 'B', 'C']

# `lake env lean <docscan source>` stands for the docstring scan, the first candidate code that
# runs. Like an initializer, it rewrites every module list and scan source it can find (under
# $TMPDIR, or the sealed inputs if writable) to name one module, then prints a passing scan.
LAKE = textwrap.dedent('''\
    import os, pathlib, re, sys
    if not sys.argv[2].endswith('lean'):
        os.execv(sys.argv[2], sys.argv[2:])
    source = pathlib.Path(sys.argv[-1]).read_text()
    pathlib.Path(os.environ['SCANNED']).write_text(source)
    for top in (os.environ['TMPDIR'], os.environ.get('LINT_ENV_INPUTS') or os.environ['TMPDIR']):
        for path in pathlib.Path(top).rglob('*'):
            try:
                if path.name in ('modules.txt', 'lint-modules.txt'):
                    path.write_text('TauCeti.A\\n')
                elif path.suffix == '.lean':
                    path.write_text('-- emptied\\n')
            except OSError:
                pass
    marker = re.search(r'DOCSCAN-COMPLETE-[a-f0-9]+', source)[0]
    linter = re.search(r'LINTERSET-COMPLETE-[a-f0-9]+', source)[0]
    print('LINTERSET base ' + linter)
    print('DOCSCAN-SUMMARY scanned=0 documented=0 undocumented=0 nolints=0 ' + marker)
    ''')

# The compiled `#lint` driver: records the modules it was asked to lint and passes.
DRIVER = textwrap.dedent('''\
    import os, pathlib, sys
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    tag, marker, modules, *linters = args
    pathlib.Path(os.environ['LINTED']).write_text(pathlib.Path(modules).read_text())
    print(f'-- Found 0 errors in 1 declarations (plus 0 automatically generated ones) in TauCeti with {len(linters)} linters')
    print(pathlib.Path(marker).read_text().strip())
    ''')


class SealedInputs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = root = Path(temporary.name)
        (root / 'pr/TauCeti').mkdir(parents=True)
        (root / 'pr/.lake/tmp').mkdir(parents=True)
        for module in MODULES:
            (root / f'pr/TauCeti/{module}.lean').write_text('theorem t : True := trivial\n')
        trusted = root / 'trusted'
        trusted.mkdir()
        for name in ('lint-env.sh', 'source-modules.sh', 'lint_env_locations.py'):
            (trusted / name).symlink_to(SCRIPTS / name)
        (trusted / 'lint-baseline.txt').write_text('')
        (trusted / 'lint-nolints-allowlist.txt').write_text('')
        for name, text in (('bin/lake', LAKE), ('drivers/lint-env-driver', DRIVER),
                           ('watchdog/bin/lean', '')):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'#!{sys.executable}\n' + text)
            path.chmod(0o755)
        self.env = {**os.environ, 'PATH': f"{root / 'bin'}:{os.environ['PATH']}",
                    'TAUCETI_PROJECT_ROOT': str(root / 'pr'), 'TAUCETI_TRUSTED_SCRIPTS': str(trusted),
                    'TMPDIR': str(root / 'pr/.lake/tmp'), 'LINT_ONLY_MODULES': '',
                    'LINT_CHANGED_ONLY': '0', 'LINT_ENV_INPUTS': '', 'WATCHDOG_TOOLCHAIN': '',
                    'LINT_DRIVER_EXE': str(root / 'drivers/lint-env-driver'),
                    'SCANNED': str(root / 'scanned.lean'), 'LINTED': str(root / 'linted.txt')}

    def lint(self, *args, **env):
        return subprocess.run(['bash', str(self.root / 'trusted/lint-env.sh'), *args], text=True,
                              capture_output=True, env={**self.env, **env})

    def prepare(self, **env):
        inputs = self.root / 'host/lint-env-inputs'
        result = self.lint('--prepare', str(inputs), **env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.addCleanup(lambda: subprocess.run(['chmod', '-R', 'u+w', str(inputs)]))
        return inputs

    def linted(self):
        return (self.root / 'linted.txt').read_text().split()

    def test_unsealed_lists_are_the_hole(self):
        # Generated under the candidate-writable $TMPDIR, the scan's rewrite shrinks the lint.
        result = self.lint()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.linted(), ['TauCeti.A'])

    def test_a_candidate_rewrite_cannot_shrink_sealed_coverage(self):
        inputs = self.prepare()
        source = (inputs / 'DocScanDriver.lean').read_text()
        watchdog = str(self.root / 'watchdog')
        result = self.lint(LINT_ENV_INPUTS=str(inputs), WATCHDOG_TOOLCHAIN=watchdog)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('LINT-ENV: PASS', result.stdout)
        self.assertEqual(self.linted(), [f'TauCeti.{m}' for m in MODULES])
        self.assertEqual((inputs / 'DocScanDriver.lean').read_text(), source)
        self.assertEqual((self.root / 'scanned.lean').read_text(), source)
        self.assertIn('linted 3 modules', result.stdout)

    def test_scoped_lists_are_sealed_too(self):
        scope = self.root / 'host/scope.txt'
        scope.parent.mkdir()
        scope.write_text('TauCeti.B\nTauCeti.C\n')
        env = {'LINT_ONLY_MODULES': str(scope), 'LINT_CHANGED_ONLY': '1'}
        inputs = self.prepare(**env)
        result = self.lint(LINT_ENV_INPUTS=str(inputs), WATCHDOG_TOOLCHAIN=str(self.root / 'watchdog'), **env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.linted(), ['TauCeti.B', 'TauCeti.C'])

    def test_the_sandbox_requires_sealed_inputs(self):
        result = self.lint(WATCHDOG_TOOLCHAIN=str(self.root / 'watchdog'))
        self.assertEqual(result.returncode, 1)
        self.assertIn('LINT_ENV_INPUTS', result.stdout)
        self.assertFalse((self.root / 'scanned.lean').exists())

    def test_unusable_sealed_inputs_fail_before_candidate_code(self):
        inputs = self.prepare()
        scope = self.root / 'host/scope.txt'
        scope.write_text('TauCeti.B\n')
        writable = self.root / 'pr/.lake/tmp/inputs'
        subprocess.run(['cp', '-r', str(inputs), str(writable)], check=True)
        subprocess.run(['chmod', '-R', 'u+w', str(writable)], check=True)
        cases = [
            ({'LINT_ENV_INPUTS': str(writable)}, 'must be read-only'),
            ({'LINT_ENV_INPUTS': str(inputs), 'LINT_ONLY_MODULES': str(scope)}, 'another scope'),
            ({'LINT_ENV_INPUTS': str(inputs), 'LINT_CHANGED_ONLY': '1',
              'LINT_ONLY_MODULES': str(scope)}, 'another scope'),
        ]
        for env, message in cases:
            with self.subTest(message=message, env=env):
                result = self.lint(**env)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(message, result.stdout)
                self.assertFalse((self.root / 'scanned.lean').exists())
        (self.root / 'pr/TauCeti/D.lean').write_text('theorem d : True := trivial\n')
        result = self.lint(LINT_ENV_INPUTS=str(inputs))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('lists other modules', result.stdout)

    def test_pr_build_seals_the_inputs_before_the_sandbox_and_mounts_them_read_only(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        steps = workflow['jobs']['sandboxed-build']['steps']
        names = [step.get('name', '') for step in steps]
        prepare = next(i for i, name in enumerate(names) if 'Build the trusted environment-lint driver' in name)
        self.assertLess(names.index('Decide the audit scope'), prepare)
        self.assertIn('lint-env.sh --prepare "$inputs"', steps[prepare]['run'])
        phases = [step for step in steps[prepare:] if step.get('env', {}).get('SANDBOX_PHASE')]
        for step in phases:
            self.assertIn('--ro-bind "$LINT_ENV_INPUTS" "$LINT_ENV_INPUTS"', step['run'])
            self.assertNotIn('--bind "$LINT_ENV_INPUTS"', step['run'])
            self.assertIn('--setenv LINT_ENV_INPUTS "$LINT_ENV_INPUTS"', step['run'])
        self.assertIn('lint-env', [step['env']['SANDBOX_PHASE'] for step in phases])

    def test_prepare_needs_an_empty_directory(self):
        inputs = self.root / 'host/used'
        inputs.mkdir(parents=True)
        (inputs / 'stale').write_text('')
        result = self.lint('--prepare', str(inputs))
        self.assertEqual(result.returncode, 1)
        self.assertIn('empty directory', result.stdout)


if __name__ == '__main__':
    unittest.main()
