#!/usr/bin/env python3
"""Regression checks for observational isolation and unchanged command outcomes."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('host_resources', HERE / 'ci_host_resources.py')
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class HostResourcesTests(unittest.TestCase):
    def invoke(self, command, out, snapshot=None):
        with patch.object(sys, 'argv', ['ci_host_resources.py', '--out', str(out), '--', *command]):
            if snapshot:
                with patch.object(host, 'snapshot', snapshot):
                    return host.main()
            return host.main()

    def test_success_failure_and_cpu_are_observed_without_changing_status(self):
        with tempfile.TemporaryDirectory() as directory:
            for code in (0, 7):
                out = Path(directory) / f'{code}.json'
                command = [sys.executable, '-c', f'sum(i*i for i in range(1000000)); raise SystemExit({code})']
                self.assertEqual(self.invoke(command, out), code)
                value = json.loads(out.read_text())
                self.assertEqual(value['exit_code'], code)
                self.assertGreater(value['command_cpu_user_s'], 0)
                self.assertGreater(value['command_max_process_rss_bytes'], 0)
                self.assertLessEqual(value['samples'], host.MAX_SAMPLES)
                self.assertGreaterEqual(value['sampler_cpu_s'], 0)

    def test_record_names_the_sandboxed_phase_it_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            for phase in ('lint-env', None):
                out = Path(directory) / f'{phase}.json'
                environment = {k: v for k, v in os.environ.items() if k != 'SANDBOX_PHASE'}
                if phase:
                    environment['SANDBOX_PHASE'] = phase
                with patch.dict(os.environ, environment, clear=True):
                    self.assertEqual(self.invoke([sys.executable, '-c', 'pass'], out), 0)
                self.assertEqual(json.loads(out.read_text())['sandbox_phase'], phase)

    def test_peak_memory_near_the_host_total_warns_without_changing_status(self):
        gib = 2**30
        for used, warned in ((70 * gib, False), (81 * gib, True)):
            with self.subTest(used=used), tempfile.TemporaryDirectory() as directory:
                out = Path(directory) / 'record.json'
                sample = {'missing': [], 'host_used_bytes': used, 'host_memory_total_bytes': 100 * gib}
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = self.invoke([sys.executable, '-c', 'raise SystemExit(3)'], out, lambda: dict(sample))
                self.assertEqual(code, 3)
                value = json.loads(out.read_text())
                self.assertEqual(value['host_memory_total_bytes'], 100 * gib)
                self.assertAlmostEqual(value['host_used_peak_fraction'], used / (100 * gib))
                self.assertEqual('::warning title=Build memory headroom::' in stdout.getvalue(), warned)

    def test_memory_fraction_is_absent_without_a_total(self):
        sampler = host.Sampler()
        with patch.object(host, 'snapshot', return_value={'missing': ['meminfo']}):
            sampler.sample()
        summary = sampler.summary()
        self.assertIsNone(summary['host_used_peak_fraction'])
        self.assertFalse(host.memory_warning(summary))

    def test_the_crossing_survives_later_samples(self):
        gib = 2**30
        for later in ({'missing': ['meminfo']},
                      {'missing': [], 'host_used_bytes': 10 * gib, 'host_memory_total_bytes': 100 * gib},
                      {'missing': [], 'host_used_bytes': 100 * gib, 'host_memory_total_bytes': 200 * gib}):
            with self.subTest(later=later):
                sampler = host.Sampler()
                samples = iter([{'missing': [], 'host_used_bytes': 85 * gib, 'host_memory_total_bytes': 100 * gib},
                                later])
                with patch.object(host, 'snapshot', lambda: dict(next(samples))):
                    sampler.sample()
                    sampler.sample()
                summary = sampler.summary()
                self.assertAlmostEqual(summary['host_used_peak_fraction'], 0.85)
                self.assertEqual(summary['host_memory_total_bytes'], 100 * gib)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(host.memory_warning(summary))

    def test_the_boundary_warns(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(host.memory_warning({'host_used_peak_fraction': 0.8, 'host_used_peak_fraction_bytes': 80,
                                                 'host_memory_total_bytes': 100}))

    def test_an_unwritable_record_still_warns(self):
        gib = 2**30
        sample = {'missing': [], 'host_used_bytes': 90 * gib, 'host_memory_total_bytes': 100 * gib}
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(stdout):
            out = Path(directory) / 'missing-directory' / 'record.json'
            self.assertEqual(self.invoke([sys.executable, '-c', 'pass'], out, lambda: dict(sample)), 0)
        self.assertIn('telemetry unavailable', stdout.getvalue())
        self.assertIn('::warning title=Build memory headroom::', stdout.getvalue())

    def test_a_capped_record_says_so(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            host.memory_warning({'sample_limit_reached': True})
        self.assertIn('::notice title=Build memory headroom::', stdout.getvalue())

    def test_real_meminfo_reports_the_total(self):
        value = host.snapshot()
        if 'meminfo' not in value['missing']:
            self.assertGreater(value['host_memory_total_bytes'], value['host_used_bytes'])

    def test_missing_proc_and_unwritable_output_are_nonfatal(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'observation.json'
            self.assertEqual(self.invoke([sys.executable, '-c', 'pass'], out,
                             lambda: {'missing': ['stat', 'meminfo', 'pressure/cpu']}), 0)
            self.assertIn('meminfo', json.loads(out.read_text())['missing'])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.invoke([sys.executable, '-c', 'raise SystemExit(1)'], out / 'absent.json'), 1)
            self.assertIn('telemetry unavailable', output.getvalue())

    def test_adopted_descendants_are_counted_and_primary_status_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'resources.json'
            # The command exits before its worker, like bubblewrap's eventfd
            # monitor. The adopted worker reaps CPU/memory-intensive work.
            command = [sys.executable, '-c', textwrap.dedent('''
                import os, time
                if os.fork() == 0:
                    time.sleep(.1)
                    if os.fork() == 0:
                        data = bytearray(32 * 1024 * 1024)
                        sum(i*i for i in range(4000000))
                        os._exit(0)
                    os.wait()
                    os._exit(0)
                os._exit(7)
            ''')]
            self.assertEqual(self.invoke(command, out), 7)
            value = json.loads(out.read_text())
            self.assertEqual(value['command_accounting_scope'], 'reaped-descendants')
            self.assertEqual(value['reaped_processes'], 2)
            self.assertGreater(value['command_cpu_user_s'], .05)
            self.assertGreater(value['command_max_process_rss_bytes'], 32*1024*1024)

    def test_subreaper_failure_is_visible_and_nonfatal(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'resources.json'
            with patch.object(host, 'descendant_accounting', return_value=None):
                self.assertEqual(self.invoke([sys.executable, '-c', 'raise SystemExit(7)'], out), 7)
            value = json.loads(out.read_text())
            self.assertEqual(value['command_accounting_scope'], 'direct-child-only')
            self.assertIn('descendant-accounting-unavailable', value['missing'])

    def test_signal_killed_adopted_worker_marks_accounting_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'resources.json'
            command = [sys.executable, '-c', textwrap.dedent('''
                import os, signal, time
                if os.fork() == 0:
                    time.sleep(.05)
                    sum(i*i for i in range(1000000))
                    os.kill(os.getpid(), signal.SIGKILL)
                os._exit(0)
            ''')]
            self.assertEqual(self.invoke(command, out), 0)
            value = json.loads(out.read_text())
            self.assertEqual(value['exit_code'], 0)
            self.assertEqual(value['cancel_signals'], [])
            self.assertEqual(value['reaped_termination_signals'], [signal.SIGKILL])
            self.assertEqual(value['command_accounting_scope'], 'partial-reaped-descendants')
            self.assertIn('descendant-accounting-interrupted', value['missing'])

    def test_unfinished_adopted_worker_cannot_delay_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            out, ready = Path(directory) / 'resources.json', Path(directory) / 'worker'
            command = [sys.executable, '-c', textwrap.dedent(f'''
                import os, time
                from pathlib import Path
                if os.fork() == 0:
                    Path({str(ready)!r}).write_text(str(os.getpid()))
                    time.sleep(30)
                    os._exit(0)
                os._exit(7)
            ''')]
            try:
                with patch.object(host, 'DESCENDANT_GRACE', .1):
                    start = time.monotonic()
                    self.assertEqual(self.invoke(command, out), 7)
                    self.assertLess(time.monotonic() - start, 2)
                value = json.loads(out.read_text())
                self.assertEqual(value['command_accounting_scope'], 'partial-reaped-descendants')
                self.assertIn('descendant-accounting-unfinished', value['missing'])
            finally:
                if ready.exists():
                    worker = int(ready.read_text())
                    os.kill(worker, signal.SIGKILL)
                    os.waitpid(worker, 0)

    def test_ready_descendant_stream_cannot_bypass_deadline_or_cancellation(self):
        for cancel in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                out = Path(directory) / 'resources.json'
                handlers, primary, calls = {}, [], []
                wait4 = host.os.wait4
                def continuous_reaps(pid, options):
                    if not primary:
                        value = wait4(pid, options)
                        if value[0]:
                            primary.append(value)
                        return value
                    calls.append(1)
                    if len(calls) > 200:
                        raise ChildProcessError()
                    time.sleep(.01)
                    if cancel and len(calls) == 1:
                        handlers[signal.SIGTERM](signal.SIGTERM, None)
                    return primary[0][0] + 1, signal.SIGKILL, primary[0][2]
                with patch.object(host, 'DESCENDANT_GRACE', .05), \
                        patch.object(host.os, 'wait4', side_effect=continuous_reaps), \
                        patch.object(host.signal, 'signal', side_effect=lambda sig, fn: handlers.update({sig: fn})):
                    self.assertEqual(self.invoke([sys.executable, '-c', 'raise SystemExit(7)'], out), 7)
                value = json.loads(out.read_text())
                self.assertLess(value['elapsed_s'], 1)
                self.assertEqual(value['command_accounting_scope'], 'partial-reaped-descendants')
                self.assertIn('descendant-accounting-unfinished', value['missing'])
                self.assertEqual(value['reaped_termination_signals'], [signal.SIGKILL])

    def test_cancellation_after_monitor_exit_flushes_without_waiting_for_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            out, ready = Path(directory) / 'resources.json', Path(directory) / 'worker'
            command = textwrap.dedent(f'''
                import os, time
                from pathlib import Path
                if os.fork() == 0:
                    Path({str(ready)!r}).write_text(str(os.getpid()))
                    time.sleep(30)
                    os._exit(0)
                os._exit(0)
            ''')
            child = subprocess.Popen([sys.executable, str(HERE / 'ci_host_resources.py'),
                '--out', str(out), '--', sys.executable, '-c', command])
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                time.sleep(.05)
                child.send_signal(signal.SIGTERM)
                self.assertEqual(child.wait(timeout=3), 0)
                value = json.loads(out.read_text())
                self.assertEqual(value['cancel_signals'], [signal.SIGTERM])
                self.assertEqual(value['command_accounting_scope'], 'partial-reaped-descendants')
                self.assertIn('descendant-accounting-unfinished', value['missing'])
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                if ready.exists():
                    try:
                        os.kill(int(ready.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_signal_completion_race_preserves_status_when_popen_reaps_first(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'resources.json'
            handlers = {}
            wait4 = host.os.wait4
            def late_signal(pid, options):
                time.sleep(.1)
                handlers[signal.SIGTERM](signal.SIGTERM, None)
                return wait4(pid, options)
            with patch.object(host.signal, 'signal', side_effect=lambda sig, fn: handlers.update({sig: fn})), \
                    patch.object(host.os, 'wait4', side_effect=late_signal):
                self.assertEqual(self.invoke([sys.executable, '-c', 'pass'], out), 0)
            value = json.loads(out.read_text())
            self.assertEqual(value['exit_code'], 0)
            self.assertEqual(value['cancel_signals'], [signal.SIGTERM])
            self.assertEqual(value['command_accounting_scope'], 'partial-reaped-descendants')
            self.assertIn('primary-resource-accounting-unavailable', value['missing'])

    @unittest.skipUnless(shutil.which('bwrap'), 'bubblewrap not installed')
    def test_real_pid_namespace_reports_sandbox_cpu_and_rss(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'resources.json'
            command = ['bwrap', '--ro-bind', '/', '/', '--dev', '/dev', '--unshare-user', '--unshare-pid',
                       '--proc', '/proc', '--die-with-parent', '--', sys.executable, '-c',
                       'data=bytearray(32*1024*1024); sum(i*i for i in range(4000000))']
            probe = subprocess.run(command[:command.index(sys.executable)] + ['true'], capture_output=True)
            if probe.returncode:
                self.skipTest('bubblewrap namespaces unavailable: ' + probe.stderr.decode()[:100])
            self.assertEqual(self.invoke(command, out), 0)
            value = json.loads(out.read_text())
            self.assertGreaterEqual(value['reaped_processes'], 2)
            self.assertGreater(value['command_cpu_user_s'], .05)
            self.assertGreater(value['command_max_process_rss_bytes'], 32*1024*1024)

    def test_cancellation_flushes_partial_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'cancel.json'
            ready = Path(directory) / 'ready'
            child = subprocess.Popen([sys.executable, str(HERE / 'ci_host_resources.py'),
                '--out', str(out), '--', sys.executable, '-c',
                f'from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(30)'])
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.exists())
                child.send_signal(signal.SIGTERM)
                self.assertEqual(child.wait(timeout=10), 128 + signal.SIGTERM)
                value = json.loads(out.read_text())
                self.assertEqual(value['cancel_signals'], [signal.SIGTERM])
                self.assertEqual(value['exit_code'], -signal.SIGTERM)
                self.assertEqual(value['command_accounting_scope'], 'partial-reaped-descendants')
                self.assertIn('descendant-accounting-interrupted', value['missing'])
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()

    def test_sampler_bound_and_missing_counter_coverage(self):
        sampler = host.Sampler()
        with patch.object(host, 'snapshot', return_value={'missing': ['pressure/io']}):
            sampler.sample()
            with patch.object(host, 'MAX_SAMPLES', 1):
                sampler.watch(host.threading.Event())
        self.assertEqual(sampler.samples, 1)
        self.assertIsNone(sampler.summary()['host_cpu_busy_fraction'])
        self.assertIn('pressure/io', sampler.summary()['missing'])

    def test_workflow_source_and_actual_candidate_metadata_remain_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)/'resources.json'
            with patch.dict(os.environ, {'CI_RESOURCE_WORKFLOW_SHA': 'a'*40,
                    'CI_RESOURCE_TESTED_HEAD': 'b'*40, 'CI_RESOURCE_TESTED_BASE': 'c'*40,
                    'CI_RESOURCE_TESTED_COMMIT': 'e'*40, 'CI_RESOURCE_TESTED_TREE': 'f'*40,
                    'CI_RESOURCE_DEPENDENCY_BUMP': '0', 'GITHUB_SHA': 'd'*40}):
                self.assertEqual(self.invoke([sys.executable, '-c', 'pass'], out), 0)
            value = json.loads(out.read_text())
            self.assertEqual(value['workflow_sha'], 'a'*40)
            self.assertEqual(value['tested_head_sha'], 'b'*40)
            self.assertEqual(value['tested_base_sha'], 'c'*40)
            self.assertEqual(value['tested_commit_sha'], 'e'*40)
            self.assertEqual(value['tested_tree_sha'], 'f'*40)
            self.assertEqual(value['dependency_bump'], '0')

    def test_failed_sample_attempts_obey_the_same_cap(self):
        sampler = host.Sampler()
        done = type('Done', (), {'wait': lambda self, interval: False})()
        with patch.object(host, 'snapshot', side_effect=RuntimeError('failed snapshot')), \
                patch.object(host, 'MAX_SAMPLES', 3):
            sampler.watch(done)
            self.assertEqual(sampler.attempts, 3)
            self.assertEqual(sampler.samples, 0)
            self.assertTrue(sampler.summary()['sample_limit_reached'])
            self.assertIn('sampler-error', sampler.summary()['missing'])

    def test_completion_race_cannot_exceed_cap_or_block_final_summary(self):
        sampler = host.Sampler()
        entered, release = host.threading.Event(), host.threading.Event()
        def slow_snapshot():
            entered.set()
            self.assertTrue(release.wait(5))
            return {'missing': []}
        with patch.object(host, 'snapshot', side_effect=slow_snapshot) as snapshot, \
                patch.object(host, 'MAX_SAMPLES', 1):
            watcher = host.threading.Thread(target=sampler.sample)
            watcher.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(sampler.sample())
                self.assertIn('sample-in-progress-at-finish', sampler.summary()['missing'])
            finally:
                release.set()
                watcher.join(5)
            self.assertFalse(watcher.is_alive())
            self.assertFalse(sampler.sample())
            self.assertEqual(sampler.attempts, 1)
            self.assertEqual(sampler.samples, 1)
            self.assertEqual(snapshot.call_count, 1)

    def publication(self, directory):
        spec = importlib.util.spec_from_file_location('publication', HERE / 'ci_publication_observation.py')
        publication = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(publication)
        return publication.measure(directory)

    def test_publication_counts_upload_subset_and_full_map_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # No inherited artifact files exist locally. The trusted planner
            # verified them and froze the actual upload subset outside staging.
            (root / 'outputs.jsonl').write_bytes(b'full map')
            metrics = dict(objects=100, inherited=98, uploaded_objects=2,
                           upload_bytes=40, full_bytes=9000)
            (root / 'metrics.json').write_text(json.dumps(metrics))
            (root / 'junk').write_bytes(b'x' * 1000)
            value = self.publication(root)
            self.assertTrue(value['complete'])
            self.assertEqual(value['staged_objects'], 3)
            self.assertEqual(value['staged_bytes'], 48)
            self.assertEqual(value['inherited_objects'], 98)
            metrics.update(inherited=100, uploaded_objects=0, upload_bytes=0)
            (root / 'metrics.json').write_text(json.dumps(metrics))
            self.assertEqual(self.publication(root)['staged_objects'], 1)
            self.assertEqual(self.publication(root)['staged_bytes'], 8)

    def test_publication_missing_symlink_fifo_oversized_and_invalid_plans_are_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(self.publication(root)['complete'])
            metrics = dict(objects=1, inherited=0, uploaded_objects=1,
                           upload_bytes=40, full_bytes=40)
            (root / 'metrics.json').write_text(json.dumps(metrics))
            (root / 'outputs.jsonl').symlink_to('/etc/passwd')
            self.assertFalse(self.publication(root)['complete'])
            (root / 'outputs.jsonl').unlink()
            os.mkfifo(root / 'outputs.jsonl')
            self.assertFalse(self.publication(root)['complete'])
            (root / 'outputs.jsonl').unlink()
            (root / 'outputs.jsonl').write_bytes(b'full map')
            for field, value in (('objects', 2), ('upload_bytes', 41), ('inherited', True)):
                (root / 'metrics.json').write_text(json.dumps({**metrics, field: value}))
                self.assertFalse(self.publication(root)['complete'])
            (root / 'metrics.json').unlink()
            os.mkfifo(root / 'metrics.json')
            self.assertFalse(self.publication(root)['complete'])
            (root / 'metrics.json').unlink()
            with (root / 'metrics.json').open('wb') as handle:
                handle.truncate(64*1024 + 1)
            self.assertFalse(self.publication(root)['complete'])

    def test_workflow_output_is_outside_candidate_mounts_and_nonfatal(self):
        workflow = (HERE.parent / '.github/workflows/pr-build.yml').read_text()
        # The output is a host path under RUNNER_TEMP, given directly or through `resource_out`.
        self.assertRegex(workflow, r'python3 gate/scripts/ci_host_resources\.py --out '
                                   r'"\$(?:RUNNER_TEMP/ci-host-resources\.json|resource_out)" --')
        self.assertIn('"$RUNNER_TEMP/ci-host-resources', workflow)
        self.assertIn('env -i /usr/bin/bwrap', workflow)
        self.assertNotIn('--bind "$RUNNER_TEMP"', workflow)
        self.assertNotIn('--ro-bind "$RUNNER_TEMP"', workflow)
        step = workflow.split('- name: Upload trusted host resource observations')[1].split('- name:')[0]
        self.assertIn("vars.CI_RESOURCE_EXPERIMENT == '1'", step)
        self.assertIn('continue-on-error: true', step)
        publication = (HERE.parent / '.github/workflows/publish-lake-cache.yml').read_text()
        observe = publication.split('- name: Observe publication volume')[1]
        self.assertNotIn('LAKE_CACHE_KEY', observe)
        self.assertRegex(observe, r'publisher-tools/(?:tauceti_tools/assets/)?scripts/ci_publication_observation\.py')
        self.assertIn('--plan \"$RUNNER_TEMP/lake-publication-plan\"', observe)
        self.assertIn('scripts/ci_publication_observation.py', publication.split('- name: Download')[0])


if __name__ == '__main__':
    unittest.main()
