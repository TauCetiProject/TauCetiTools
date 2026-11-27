"""Exercise nightly cache verification and reporting without builds or network access."""

import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.parse

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/nightly-verify.yml').read_text())
JOBS = WORKFLOW['jobs']
# The triggers, the main-only admission and the concurrency group live in the caller.
SCHEDULE = yaml.safe_load((ROOT / '.github/workflows/nightly-verify-schedule.yml').read_text())


def step(job, name):
    return next(s for s in JOBS[job]['steps'] if name in (s.get('id'), s.get('name')))


def condition(source, **values):
    source = source.removeprefix('${{').removesuffix('}}').strip()
    source = re.sub(r'!(?!=)', 'not ', source).replace('&&', ' and ').replace('||', ' or ')
    return eval(source, {'__builtins__': {}, 'always': lambda: True,
                         'cancelled': lambda: False, 'contains': lambda values, value: value in values, **values})


class NightlyVerification(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.temp = self.root / 'temp'
        self.temp.mkdir()
        self.helper = self.root / 'fresh/scripts'
        self.helper.mkdir(parents=True)
        (self.root / 'reference/scripts').mkdir(parents=True)
        shutil.copyfile(ROOT / 'scripts/compare-build-outputs.py',
                        self.helper / 'compare-build-outputs.py')
        self.env = {**os.environ, 'GITHUB_OUTPUT': str(self.temp / 'outputs'),
                    'GITHUB_ENV': str(self.temp / 'environment'), 'RUNNER_TEMP': str(self.temp),
                    'LAKE_ARTIFACT_CACHE': 'true', 'LAKE_RESTORE_ARTIFACTS': 'true'}

    def shell(self, body, substitutions=None):
        for key, value in (substitutions or {}).items():
            body = body.replace('${{ ' + key + ' }}', value)
        self.assertNotIn('${{', body)
        return subprocess.run(['bash', '-euo', 'pipefail', '-c', body], cwd=self.root,
                              env=self.env, text=True, capture_output=True)

    def outputs(self):
        output = Path(self.env['GITHUB_OUTPUT'])
        return dict(line.split('=', 1) for line in output.read_text().splitlines())

    def assertNightlyRunsOneAtATimeOnlyOnMain(self):
        # The callee has no trigger of its own, so every run goes through the caller's admission.
        self.assertEqual(list(WORKFLOW[True]), ['workflow_call'])
        self.assertNotIn('concurrency', WORKFLOW)
        # No workflow-level group in the caller, which could cancel or displace a running check.
        self.assertNotIn('concurrency', SCHEDULE)
        caller = SCHEDULE['jobs']['run']
        # TauCeti's own copy, or TauCetiTools' at main once TauCeti's caller is a stub.
        self.assertIn(caller['uses'], ('./.github/workflows/nightly-verify.yml',
                                       'TauCetiProject/TauCetiTools/.github/workflows/nightly-verify.yml@main'))
        self.assertEqual(caller['needs'], 'admit')
        self.assertEqual(caller['if'], "needs.admit.outputs.main == 'true'")
        self.assertFalse(caller['concurrency']['cancel-in-progress'])
        # Admitted runs share one group; any other run gets a group of its own.
        group = caller['concurrency']['group']
        self.assertEqual(group, "${{ needs.admit.outputs.main == 'true' && 'nightly-verify' || "
                                "format('nightly-verify-not-admitted-{0}', github.run_id) }}")
        admit = SCHEDULE['jobs']['admit']
        self.assertEqual(admit['outputs']['main'], '${{ steps.ref.outputs.main }}')
        script = next(s for s in admit['steps'] if s.get('id') == 'ref')['run']
        for ref, admitted in (('refs/heads/main', 'true'), ('refs/heads/Main', 'false'),
                              ('refs/heads/feature', 'false'), ('refs/tags/main', 'false')):
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / 'output'
                subprocess.run(['bash', '-euo', 'pipefail', '-c', script], check=True,
                               capture_output=True, text=True,
                               env={**os.environ, 'GITHUB_REF': ref, 'GITHUB_OUTPUT': str(output)})
                self.assertEqual(output.read_text(), f'main={admitted}\n', ref)

    def test_jobs_have_separate_budgets_and_transfer_only_the_clean_manifest(self):
        # Full source builds retain their budget on general CI capacity.
        self.assertEqual(JOBS['verify']['runs-on'], {
            'group': 'TauCetiFleet',
            'labels': ['self-hosted', 'linux', 'x64', 'tauceti-fleet-pr'],
        })
        self.assertGreater(JOBS['verify']['timeout-minutes'], 360)
        self.assertNotIn('needs', JOBS['verify'])
        self.assertNightlyRunsOneAtATimeOnlyOnMain()
        self.assertEqual(JOBS['reference']['timeout-minutes'], 240)
        self.assertIn('verify', JOBS['reference']['needs'])
        for s in JOBS['reference']['steps']:
            if s.get('uses', '').startswith('actions/checkout@'):
                # The project at the commit under test; in TauCetiTools' copy also the shared tools,
                # at that workflow's own revision.
                tools = s['with'].get('repository') == '${{ job.workflow_repository }}'
                self.assertEqual(s['with']['ref'], '${{ job.workflow_sha }}' if tools else '${{ github.sha }}')
                self.assertFalse(s['with']['persist-credentials'])
        steps = JOBS['verify']['steps']
        snapshot = step('verify', 'Record what the clean build produced')
        upload = step('verify', 'Retain the clean manifest for cache verification')
        self.assertLess(steps.index(snapshot), steps.index(upload))
        self.assertLess(steps.index(snapshot), steps.index(step('verify', 'fresh_audit')))
        self.assertLess(steps.index(step('verify', 'fresh_audit')), steps.index(upload))
        self.assertTrue(upload['continue-on-error'])
        self.assertEqual(upload['with']['path'], '${{ runner.temp }}/clean-manifest.txt')
        self.assertEqual(upload['with']['if-no-files-found'], 'error')
        download = step('reference', 'Download the clean manifest from the source-build job')
        self.assertEqual(download['with']['name'], '${{ needs.verify.outputs.manifest }}')
        self.assertIn('github.run_attempt', step('verify', 'rev')['run'])
        self.assertEqual(step('verify', 'fresh_build')['env']['LAKE_RESTORE_ARTIFACTS'], 'false')

    def test_audit_failure_allows_comparison_but_missing_clean_evidence_blocks_it(self):
        for build, upload, expected in (
            ('success', 'success', True), ('failure', 'skipped', False),
            ('cancelled', 'skipped', False), ('success', 'failure', False),
        ):
            # A lint failure makes verify.result=failure; it must not suppress the
            # independent comparison when the source build and evidence transfer succeeded.
            needs = SimpleNamespace(verify=SimpleNamespace(result='failure',
                outputs=SimpleNamespace(build=build, manifest_upload=upload)))
            self.assertEqual(condition(JOBS['reference']['if'], needs=needs,
                github=SimpleNamespace(repository='TauCetiProject/TauCeti')), expected)
            self.assertEqual(condition(step('verify', 'fresh_manifest')['if'],
                steps=SimpleNamespace(fresh_build=SimpleNamespace(outcome=build))), build == 'success')

    def test_reference_waits_for_the_fleet_pr_class_and_never_cloud_capacity(self):
        self.assertNotIn('pick-reference-runner', JOBS)
        self.assertEqual(JOBS['reference']['needs'], 'verify')
        self.assertNotIn('concurrency', JOBS['reference'])
        for job in ('verify', 'reference'):
            self.assertEqual(JOBS[job]['runs-on'], {
                'group': 'TauCetiFleet', 'labels': ['self-hosted', 'linux', 'x64', 'tauceti-fleet-pr']})
        text = (ROOT / '.github/workflows/nightly-verify.yml').read_text()
        for label in ('nscloud-', 'namespace-profile-', 'namespace-features:'):
            self.assertNotIn(label, text)

    def test_restore_uses_the_materialized_helper_with_the_reference_as_target(self):
        # Model the reference checkout's uvx wrapper failing on a runner without uvx.
        (self.root / 'reference/scripts/lake-cache-get.sh').write_text('exit 127\n')
        (self.helper / 'lake-cache-get.sh').write_text(
            'set -eu\n[ "$1" = reference ]\nmkdir -p "$1/.lake/cache"\n'
            'echo restored > "$1/.lake/cache/artifact"\n')
        result = self.shell(step('reference', 'restore')['run'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'reference/.lake/cache/artifact').exists())

    def test_a_failed_disabled_or_empty_restore_never_reaches_reference_build(self):
        for enabled, outcome, cache_enabled, populated, expected in (
            ('false', 'skipped', 'true', True, False),
            ('true', 'failure', 'true', True, False),
            ('true', 'success', 'false', True, False),
            ('true', 'success', 'true', False, False),
            ('true', 'success', 'true', True, True),
        ):
            with self.subTest(enabled=enabled, outcome=outcome,
                              cache_enabled=cache_enabled, populated=populated):
                cache = self.root / 'reference/.lake/cache'
                shutil.rmtree(cache, ignore_errors=True)
                cache.mkdir(parents=True)
                if populated:
                    (cache / 'artifact').write_text('cached')
                self.env['LAKE_ARTIFACT_CACHE'] = cache_enabled
                result = self.shell(step('reference', 'restored')['run'], {
                    'steps.refcfg.outputs.enabled': enabled, 'steps.restore.outcome': outcome})
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs = self.outputs()
                builds = condition(step('reference', 'reference_build')['if'],
                                   steps=SimpleNamespace(restored=SimpleNamespace(
                                       outputs=SimpleNamespace(**outputs))))
                self.assertEqual(builds, expected)
                if not expected:
                    self.assertTrue(outputs['why'])

    def test_real_manifests_agree_diverge_or_fail_closed_when_missing(self):
        for changed, expected in ((False, 'agree'), (True, 'disagree')):
            for tree, content in (('fresh', 'clean'), ('reference', 'changed' if changed else 'clean')):
                build = self.root / tree / '.lake/build/lib/lean'
                build.mkdir(parents=True, exist_ok=True)
                (build / 'Example.olean').write_text(content)
            for job, name in (
                ('verify', 'Record what the clean build produced'),
                ('reference', 'Record what the cache-restored build produced'),
            ):
                result = self.shell(step(job, name)['run'])
                self.assertEqual(result.returncode, 0, result.stderr)
            substitutions = {'steps.restored.outputs.restored': 'true',
                             'steps.restored.outputs.why': '',
                             'steps.reference_build.outcome': 'success'}
            result = self.shell(step('reference', 'compare')['run'], substitutions)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.outputs()['status'], expected)
        (self.temp / 'clean-manifest.txt').unlink()
        result = self.shell(step('reference', 'compare')['run'], substitutions)
        self.assertNotEqual(result.returncode, 0, 'missing evidence must not count as agreement')

    def test_incomplete_reference_reports_inconclusive_without_a_comparison(self):
        for restored, build in (('false', 'skipped'), ('true', 'failure')):
            result = self.shell(step('reference', 'compare')['run'], {
                'steps.restored.outputs.restored': restored,
                'steps.restored.outputs.why': 'cache fetch failed',
                'steps.reference_build.outcome': build})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.outputs()['status'], 'inconclusive')

    def test_reporting_alerts_on_any_incomplete_or_divergent_job(self):
        for source in ('success', 'failure', 'cancelled', 'skipped'):
            for reference in ('success', 'failure', 'cancelled', 'skipped'):
                for compare in ('agree', 'disagree', 'inconclusive', ''):
                    needs = SimpleNamespace(
                        verify=SimpleNamespace(result=source),
                        reference=SimpleNamespace(result=reference,
                                                  outputs=SimpleNamespace(compare=compare)))
                    for repo in ('TauCetiProject/TauCeti', 'someone/fork'):
                        actual = condition(JOBS['report']['if'], needs=needs,
                                           github=SimpleNamespace(repository=repo))
                        expected = repo == 'TauCetiProject/TauCeti' and (
                            source != 'success' or reference != 'success' or compare != 'agree')
                        self.assertEqual(actual, expected)

    def test_report_distinguishes_proof_failures_timeouts_and_cache_failures(self):
        body = JOBS['report']['steps'][0]['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        cases = (
            ('failure', 'failure', 'skipped', 'skipped', '', 'from-source build FAILED'),
            ('failure', 'success', 'failure', 'skipped', '', 'audit or lint FAILED'),
            ('cancelled', 'cancelled', 'skipped', 'skipped', '', 'did not complete'),
            ('success', 'success', 'success', 'cancelled', '', 'cache verification did not complete'),
            ('success', 'success', 'success', 'success', 'inconclusive', 'comparison could not be made'),
            ('success', 'success', 'success', 'success', 'disagree', 'DIFFERENT oleans'),
        )
        for result, build, audit, reference, compare, expected in cases:
            with self.subTest(result=result, build=build, reference=reference, compare=compare):
                env = {'RESULT': result, 'BUILD': build, 'AUDIT': audit, 'REFERENCE_RESULT': reference,
                       'COMPARE': compare, 'DETAIL': 'example', 'SHA': 'a' * 40,
                       'ZULIP_EMAIL': 'test@example.com', 'ZULIP_API_KEY': 'fixture',
                       'ZULIP_SITE': 'https://zulip.invalid', 'ZULIP_CHANNEL': 'Tau Ceti',
                       'ZULIP_TOPIC': 'Nightly verification', 'RUN_URL': 'https://example.com/run'}
                response = MagicMock()
                response.__enter__.return_value.read.return_value = b'{"result":"success"}'
                with patch.dict(os.environ, env, clear=True), \
                        patch('urllib.request.urlopen', return_value=response) as send, \
                        contextlib.redirect_stdout(io.StringIO()):
                    exec(compile(body, 'nightly-report', 'exec'), {})
                content = urllib.parse.parse_qs(send.call_args.args[0].data.decode())['content'][0]
                self.assertIn(expected, content)
                if build == 'cancelled':
                    self.assertNotIn('FAILED', content)
                    self.assertNotIn('suspect', content)

    def run_report(self, env, jobs=None):
        body = JOBS['report']['steps'][0]['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        env = {'SHA': 'a' * 40, 'ZULIP_EMAIL': 'test@example.com', 'ZULIP_API_KEY': 'fixture',
               'ZULIP_SITE': 'https://zulip.invalid', 'ZULIP_CHANNEL': 'Tau Ceti',
               'ZULIP_TOPIC': 'Nightly verification', 'RUN_URL': 'https://example.com/run',
               'GH_TOKEN': 'fixture', 'GITHUB_API_URL': 'https://api.invalid',
               'GITHUB_REPOSITORY': 'TauCetiProject/TauCeti', 'GITHUB_RUN_ID': '7',
               'GITHUB_RUN_ATTEMPT': '1', **env}
        posted = MagicMock()
        posted.__enter__.return_value.read.return_value = b'{"result":"success"}'
        listed = MagicMock()
        listed.__enter__.return_value.read.return_value = json.dumps({'jobs': jobs or []}).encode()
        def urlopen(request, timeout=None):
            return listed if '/actions/runs/' in request.full_url else posted
        with patch.dict(os.environ, env, clear=True), \
                patch('urllib.request.urlopen', side_effect=urlopen) as send, \
                contextlib.redirect_stdout(io.StringIO()):
            exec(compile(body, 'nightly-report', 'exec'), {})
        sent = [c.args[0] for c in send.call_args_list if '/api/v1/messages' in c.args[0].full_url]
        self.assertEqual(len(sent), 1)
        return urllib.parse.parse_qs(sent[0].data.decode())['content'][0]

    def test_capacity_check_runs_before_the_build_and_fails_short_runners(self):
        steps = JOBS['verify']['steps']
        check = step('verify', 'capacity')
        self.assertLess(steps.index(step('verify', 'Fetch Mathlib oleans for the clean build')), steps.index(check))
        self.assertEqual(steps.index(check) + 1, steps.index(step('verify', 'fresh_build')))
        self.assertEqual(JOBS['verify']['outputs']['capacity'], '${{ steps.capacity.outputs.status }}')
        self.assertEqual(JOBS['report']['permissions'], {'actions': 'read', 'contents': 'read'})
        self.env['RUNNER_NAME'] = 'fleet-example'
        for need, code, status in (('1', 0, 'ok'), ('100000000', 1, 'short')):
            with self.subTest(need=need):
                Path(self.env['GITHUB_OUTPUT']).write_text('')
                self.env['NEED_GIB'] = need
                result = self.shell(check['run'])
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(self.outputs()['status'], status)
                self.assertIn('fleet-example', self.outputs()['detail'])

    def test_report_names_capacity_shortfalls_and_lost_runners(self):
        content = self.run_report({'RESULT': 'failure', 'BUILD': 'skipped', 'CAPACITY': 'short',
                                   'CAPACITY_DETAIL': 'runner r had 81 GiB free'})
        self.assertIn('not attempted: runner r had 81 GiB free', content)
        self.assertNotIn('FAILED', content)
        jobs = [{'name': 'run / verify', 'runner_name': 'fleet-0fb9966f7c40-1',
                 'started_at': '2026-11-27T00:04:32Z', 'completed_at': '2026-11-27T02:17:33Z',
                 'steps': [{'name': 'Set up job', 'started_at': '2026-11-27T00:04:33Z', 'conclusion': 'success'},
                           {'name': 'Build main from source', 'started_at': '2026-11-27T00:12:23Z',
                            'conclusion': None},
                           {'name': 'Record what the clean build produced', 'started_at': None,
                            'conclusion': None}]}]
        content = self.run_report({'RESULT': 'failure'}, jobs)
        self.assertIn('runner stopped reporting', content)
        self.assertIn('`fleet-0fb9966f7c40-1` for 2.2 hours and stopped during "Build main from source"', content)
        self.assertNotIn('FAILED', content)
        # An unreadable jobs listing still posts the report.
        content = self.run_report({'RESULT': 'failure'}, [])
        self.assertIn('recorded no build outcome', content)

    def test_all_workflow_shell_steps_parse(self):
        for job in JOBS.values():
            for s in job.get('steps', []):
                if 'run' in s:
                    body = re.sub(r'\$\{\{.*?\}\}', 'placeholder', s['run'])
                    result = subprocess.run(['bash', '-n'], input=body, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, f"{s.get('name')}: {result.stderr}")


if __name__ == '__main__':
    unittest.main()
