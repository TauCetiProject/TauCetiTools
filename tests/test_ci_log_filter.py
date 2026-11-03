import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts/ci_log_filter.py"
spec = importlib.util.spec_from_file_location("log_filter", SCRIPT)
log_filter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(log_filter)


class LogFilterTests(unittest.TestCase):
    def filtered(self, text, mode):
        out = io.StringIO()
        log_filter.filter_log(io.StringIO(text), out, mode)
        return out.getvalue()

    def test_cache_success_noise_collapses_but_errors_and_misses_survive(self):
        item = ("info: TauCetiProject/TauCeti: downloaded artifact 1234567890abcdef\n"
                "  local path: /cache/archive.ltar\n  remote URL: https://cache.test/archive\n")
        errors = "warning: slow transfer\nerror: hash mismatch\nerror: no outputs found\n"
        out = self.filtered(item * 24000 + errors, "cache")
        self.assertIn(errors, out)
        self.assertIn("downloaded artifacts=24000", out)
        self.assertNotIn("local path", out)
        self.assertLess(len(out.splitlines()), 10)

    def test_build_cascade_is_sampled_without_hiding_primary_or_unknown_output(self):
        primary = "error: TauCeti/Analysis/Matrix/OrthogonalRows.lean:44:34: unsolved goals\n  x = y\n"
        imports = "error: TauCeti/Other.lean: bad import 'Mathlib.Other'\n" * 43000
        targets = log_filter.HEADER + "\n" + "- TauCeti.Other\n" * 24000 + "error: build failed\n"
        out = self.filtered(primary + imports + targets + "unfamiliar finding\n", "build")
        self.assertIn(primary, out)
        self.assertIn("bad imports=43000", out)
        self.assertIn("TAUCETI-DIAGNOSTICS-SAMPLED failed-targets total=24000", out)
        self.assertIn("error: build failed\nunfamiliar finding\n", out)
        self.assertLess(len(out.splitlines()), 25)

    def test_paired_progress_and_canceled_cascade_are_summarized(self):
        text = ("✖ [12/100] Running TauCeti.Other\nerror: TauCeti/Other.lean: bad import 'TauCeti.Before'\n") * 1000
        text += "⊘ [13/100] Canceled TauCeti.Later\n" * 1000
        text += "✖ [14/100] Running TauCeti.Real\nerror: TauCeti/Real.lean:1:1: actual error\n"
        text += log_filter.HEADER + "\n- TauCeti.A\n\ufeff2026-11-02T20:01:25.19Z - TauCeti.B\n" + "- TauCeti.C\n" * 100
        out = self.filtered(text, "build")
        self.assertLess(len(out.splitlines()), 30)
        self.assertIn("Running TauCeti.Real\nerror: TauCeti/Real.lean:1:1: actual error", out)
        self.assertIn("bad imports=1000", out)
        self.assertIn("canceled targets: total=1000", out)
        self.assertIn("failed-targets total=102", out)

    def test_unrecognized_and_long_records_are_unchanged(self):
        text = "error: another kind of bad import\n" + "x" * 200000 + "\nwarning: useful\n"
        self.assertEqual(self.filtered(text, "build"), text)

    def test_small_target_list_remains_complete(self):
        text = log_filter.HEADER + "\n- TauCeti.A\nerror: build failed\n"
        self.assertEqual(self.filtered(text, "build"), text)

    def test_log_filter_failure_cannot_change_lake_exit_status(self):
        scripts = SCRIPT.parent
        for status in (0, 7):
            for missing_filter in (False, True):
                with self.subTest(status=status, missing_filter=missing_filter), tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    trusted = root / 'trusted'
                    trusted.mkdir()
                    (root / 'bin').mkdir()
                    (root / '.lake/tmp').mkdir(parents=True)
                    (root / 'watchdog/bin').mkdir(parents=True)
                    for name in ['lint-env-driver', 'audit-driver', 'watchdog/bin/lean']:
                        p = root / name
                        p.write_text('#!/bin/sh\nexit 0\n')
                        p.chmod(0o755)
                    for name in ['lint-env.sh', 'lint-style.sh']:
                        (trusted / name).write_text('exit 0\n')
                    if not missing_filter:
                        (trusted / 'ci_log_filter.py').write_text(SCRIPT.read_text())
                    lake = root / 'bin/lake'
                    lake.write_text('#!/usr/bin/env bash\n'
                        f'if [ "$1" = build ]; then echo "error: TauCeti/A.lean:1:1: example"; exit {status}; fi\n'
                        'shift\nexec "$@"\n')
                    lake.chmod(0o755)
                    result = subprocess.run(['bash', str(scripts / 'sandbox-build.sh')], cwd=root,
                        env={**os.environ, 'PATH': f'{root}/bin:'+os.environ['PATH'],
                            'TAUCETI_TRUSTED_SCRIPTS': str(trusted), 'WATCHDOG_TOOLCHAIN': str(root/'watchdog'),
                            'LINT_DRIVER_EXE': str(root/'lint-env-driver'), 'IS_BATCH': 'true', 'STAGE_LAKE_CACHE': ''},
                        capture_output=True, text=True)
                    self.assertEqual(result.returncode, status, result.stdout+result.stderr)

    def test_cache_retries_reuse_verified_artifacts_and_preserve_fallback(self):
        for scenario, count in [("success", 1), ("miss", 1), ("retry", 2), ("failure", 3)]:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / 'pr/.lake/cache').mkdir(parents=True)
                (root / 'bin').mkdir()
                (root / 'pr/.lake/cache/verified').write_text('valid artifact')
                lake = root / 'bin/lake'
                lake.write_text("#!/usr/bin/env bash\n"
                    "n=$(cat \"$RUNNER_TEMP/calls\" 2>/dev/null || echo 0); n=$((n+1)); echo $n > \"$RUNNER_TEMP/calls\"\n"
                    "test -f .lake/cache/verified || exit 20\n"
                    "echo 'info: TauCetiProject/TauCeti: downloaded artifact 1234567890abcdef'\n"
                    "echo '  local path: /cache/archive'; echo '  remote URL: https://cache.test/a'\n"
                    "case $SCENARIO in\n"
                    "success) exit 0;; miss) echo 'error: no outputs found'; exit 1;;\n"
                    "retry) if [ $n -gt 1 ]; then exit 0; fi;; esac\n"
                    "echo 'error: hash mismatch'; exit 1\n")
                lake.chmod(0o755)
                sleep = root / 'bin/sleep'
                sleep.write_text('#!/bin/sh\nexit 0\n'); sleep.chmod(0o755)
                result = subprocess.run(['bash', str(SCRIPT.with_name('lake-cache-get.sh')), 'pr'], cwd=root,
                    env={**os.environ, 'PATH': str(root/'bin')+':'+os.environ['PATH'], 'RUNNER_TEMP': str(root),
                         'GITHUB_ENV': str(root/'github_env'), 'SCENARIO': scenario}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(int((root/'calls').read_text()), count)
                self.assertNotIn('downloaded artifact 1234', result.stdout)
                self.assertNotIn('remote URL:', result.stdout)
                if scenario in ('miss', 'failure'):
                    self.assertIn('LAKE_ARTIFACT_CACHE=false', (root/'github_env').read_text())
                    self.assertFalse((root/'pr/.lake/cache').exists())
                else:
                    self.assertTrue((root/'pr/.lake/cache/verified').exists())
                    self.assertIn('incremental build enabled', result.stdout)
                if scenario in ('retry', 'failure'):
                    self.assertIn('hash mismatch', result.stdout)
                    self.assertIn('retaining verified artifacts', result.stdout)

    def test_raw_artifact_copy_rejects_symlinks_and_special_files(self):
        spec = importlib.util.spec_from_file_location('failure_logs', SCRIPT.with_name('ci_failure_logs.py'))
        failure_logs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(failure_logs)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root/'outside').write_text('private')
            (root/'link').symlink_to(root/'outside')
            os.mkfifo(root/'fifo')
            for name in ['link', 'fifo']:
                with self.subTest(name=name), self.assertRaises((OSError, ValueError)):
                    failure_logs.copy_log(root, [name], root/'output.gz')


if __name__ == "__main__":
    unittest.main()
