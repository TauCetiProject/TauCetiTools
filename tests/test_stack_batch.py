"""Checkpoint coverage and real checker integration, without network or policy writes."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'tauceti_tools/assets/scripts'
sys.path.insert(0, str(SCRIPTS))
import stack_policy as sp
import test_stack_policy as fixtures


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        self.rows = [dict(number=n, head={'sha': f'{n:040x}'}, base={'ref': 'main'},
                          updated_at='2026-10-21T20:00:00Z') for n in range(1, 9)]
        self.quiet = redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def batch(self, targets=(), sweep=False):
        return sp.BatchProgress(self.path, self.rows, set(targets), sweep)

    def drain(self, b):
        for n in list(b.state['pending']):
            b.finish(n, True)
        b.save(self.path)

    def test_more_than_100_replaced_wakes_cover_all_latest_prs_once(self):
        b = self.batch([8])  # 150 replaced events are recoverable from current metadata.
        for n in [n % 8 + 1 for n in range(150)]:
            self.rows[n - 1]['head']['sha'] = f'{n + 100:040x}'
            self.rows[n - 1]['updated_at'] = b.started
        self.assertEqual(set(b.state['pending']), set(range(1, 9)))
        self.drain(b)
        b = self.batch([8])
        self.assertEqual(set(b.state['pending']), set(range(1, 9)))
        self.drain(b)
        self.assertEqual(self.batch().state['pending'], [])

    def test_update_during_batch_and_same_second_update_are_covered(self):
        b = self.batch()
        self.rows[0]['updated_at'] = b.started
        self.rows[0]['head']['sha'] = 'f' * 40
        self.drain(b)
        self.assertEqual(self.batch().state['pending'], [1])

    def test_budget_and_failed_children_rotate_behind_unvisited_work(self):
        b = self.batch()
        b.finish(1, False, retry_soon=True)
        b.finish(2, True)
        b.save(self.path)
        self.assertEqual(self.batch().state['pending'], [3, 4, 5, 6, 7, 8, 1])

    def test_unpublished_checkpoint_repeats_work_instead_of_losing_it(self):
        b = self.batch(); b.save(self.path)
        b.finish(1, True)  # Failed upload leaves the previous artifact authoritative.
        self.assertIn(1, self.batch().state['pending'])

    def test_invalid_missing_and_expired_state_recover_all_current_prs(self):
        for data in (None, '', '{}', '{"version":99}', '{"version":1,"pending":"bad"}'):
            if data is None:
                self.path.unlink(missing_ok=True)
            else:
                self.path.write_text(data)
            self.assertEqual(self.batch().state['pending'], list(range(1, 9)))

    def test_closed_targets_retired_and_sweep_rechecks_old_unchanged_prs(self):
        self.drain(self.batch())
        self.rows.pop()
        self.assertEqual(self.batch([8]).state['pending'], [])
        self.assertEqual(self.batch(sweep=True).state['pending'], list(range(1, 8)))

    def test_previously_deferred_prs_survive_advancing_discovery_boundary(self):
        b = self.batch(); b.finish(1, True); b.save(self.path)
        self.rows.append(dict(number=9, head={'sha': 'a' * 40}, base={'ref': 'parent'},
                              updated_at=b.started))
        self.assertEqual(self.batch([9]).state['pending'], [9, 2, 3, 4, 5, 6, 7, 8])

    def test_pagination_skip_returns_even_when_update_is_older_than_watermark(self):
        b = self.batch()
        b.save(self.path)
        missing = self.rows.pop(0)
        self.drain(self.batch())
        self.rows.insert(0, missing)
        self.assertEqual(self.batch().state['pending'], [1])

    def test_unchanged_unknown_is_retained_and_retried_on_change_or_sweep(self):
        b = self.batch(); b.finish(1, False)
        self.drain(b)
        self.assertEqual(self.batch().state['pending'], [])
        self.assertIn(1, self.batch(sweep=True).state['pending'])
        self.rows[0]['head']['sha'] = 'e' * 40
        self.assertEqual(self.batch().state['pending'], [1])

    def test_dry_checkpoint_and_policy_change_request_recovery(self):
        self.drain(self.batch())
        self.assertEqual(sp.BatchProgress(self.path, self.rows, set(), apply=True).state['pending'],
                         list(range(1, 9)))
        state = json.loads(self.path.read_text()); state['policy'] = 'changed'
        self.path.write_text(json.dumps(state))
        self.assertEqual(self.batch().state['pending'], list(range(1, 9)))


    def test_repeated_small_sweeps_eventually_visit_entire_backlog(self):
        seen = set()
        for _ in range(8):
            b = self.batch(sweep=True)
            for n in b.state['pending'][:2]:
                seen.add(n)
                b.finish(n, True)
            b.save(self.path)
        self.assertEqual(seen, set(range(1, 9)))

    def test_new_events_do_not_reorder_unfinished_tail(self):
        b = self.batch(); b.finish(1, True); b.save(self.path)
        for _ in range(8):
            b = self.batch([1])
            self.assertEqual(b.state['pending'][0], 1)
            for n in b.state['pending'][:2]:
                b.finish(n, True)
            b.save(self.path)
        self.assertEqual(self.batch().state['pending'], [])


class BatchIntegration(fixtures.MainTests):
    # Re-run the legacy main-interface cases alongside new batch integration cases.
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state.json'
        self.api.deadline = sp.time.monotonic() + 1200
        for r in self.raw:
            r['updated_at'] = '2026-10-21T20:00:00Z'

    def main(self, *args):
        # Existing focused tests stay on the legacy interface during rollout.
        return super().main(*args)

    def batch(self, *args):
        return self.main('--state-in', str(self.state), '--state-out', str(self.state), *args)

    def test_batch_shared_preparation_and_full_parent_index(self):
        visited = []
        self.detector.detect = lambda p: (visited.append(p.number) or None, [])
        self.assertFalse(self.batch())
        self.assertEqual(visited, [1, 2])
        self.last_source.fetch.assert_called_once()
        self.assertEqual({p.number for p in self.last_constructor.call_args.args[2]}, {1, 2})
        self.assertEqual(json.loads(self.state.read_text())['pending'], [])

    def test_budget_saves_unvisited_then_failed_child_and_recovers(self):
        def fail(p):
            raise sp.BudgetGap('quota exhausted')
        self.detector.detect = fail
        self.assertTrue(self.batch())  # No progress must remain visible as a failure.
        self.assertEqual(json.loads(self.state.read_text())['pending'], [2, 1])
        visited = []
        self.detector.detect = lambda p: (visited.append(p.number) or None, [])
        self.assertFalse(self.batch())
        self.assertEqual(visited, [2, 1])

    def test_preparation_budget_preserves_discovered_work(self):
        original = self.api.call
        def stop(path, *args, **kwargs):
            if path.startswith('pulls?state=closed'):
                raise sp.BudgetGap('preparation limit')
            return original(path, *args, **kwargs)
        with patch.object(self.api, 'call', side_effect=stop):
            with self.assertRaises(sp.BudgetGap):
                self.batch()
        self.assertEqual(json.loads(self.state.read_text())['pending'], [1, 2])

    def test_per_pr_gap_does_not_starve_unrelated_child(self):
        def detect(p):
            if p.number == 1:
                raise sp.ChildGap('expensive child')
            return None, []
        self.detector.detect = detect
        self.assertFalse(self.batch())
        state = json.loads(self.state.read_text())
        self.assertEqual(state['pending'], [])
        self.assertIn('1', state['failed'])

    def test_multiple_explicit_prs_reuse_one_preparation(self):
        visited = []
        self.detector.detect = lambda p: (visited.append(p.number) or None, [])
        self.assertFalse(self.main('--prs', '1', '2'))
        self.assertEqual(visited, [2, 1])
        self.last_source.fetch.assert_called_once()

    def test_stale_fork_wake_still_discovers_current_changes(self):
        self.assertFalse(self.event({'workflow_run': {'head_sha': 'f' * 40,
            'head_branch': 'gone', 'head_repository': {'full_name': 'fork/TauCeti'}}},
            '--state-in', str(self.state), '--state-out', str(self.state)))
        self.assertEqual(json.loads(self.state.read_text())['pending'], [])

    def test_dry_batch_never_writes_policy(self):
        self.detector.detect = lambda p: (self.evidence if p.number == 2 else None, [])
        self.assertFalse(self.batch())
        self.assertEqual(self.api.calls, [])

    def test_schedule_rechecks_all_unchanged_children(self):
        self.assertFalse(self.batch())
        visited = []
        self.detector.detect = lambda p: (visited.append(p.number) or None, [])
        self.assertFalse(self.batch())
        self.assertEqual(visited, [1, 2])

    def test_deleted_fork_only_batch_reports_gap_without_failing(self):
        self.raw = [self.raw[1]]
        self.raw[0]['head']['repo'] = None
        self.assertFalse(self.batch())
        self.assertIn('2', json.loads(self.state.read_text())['failed'])

    def test_apply_batch_closes_only_after_existing_checks(self):
        self.detector.detect = lambda p: (self.evidence if p.number == 2 else None, [])
        self.assertFalse(self.batch('--apply'))
        self.assertTrue(any(path == 'pulls/2' and method == 'PATCH'
                            for path, method, _ in self.api.calls))

    def test_mutation_budget_remains_failure_and_pending(self):
        self.detector.detect = lambda p: (self.evidence if p.number == 2 else None, [])
        original = self.api.call
        def stop(path, method='GET', data=None):
            if path == 'issues/2/labels' and method == 'POST':
                raise sp.BudgetGap('quota during mutation')
            return original(path, method, data)
        with patch.object(self.api, 'call', side_effect=stop):
            self.assertTrue(self.batch('--apply'))
        self.assertEqual(json.loads(self.state.read_text())['pending'], [2])

    def test_progress_before_budget_defers_successfully(self):
        def detect(p):
            if p.number == 2:
                raise sp.BudgetGap('quota')
            return None, []
        self.detector.detect = detect
        self.assertFalse(self.batch())
        self.assertEqual(json.loads(self.state.read_text())['pending'], [2])

    def test_identity_race_remains_pending_instead_of_waiting_for_hourly_sweep(self):
        self.detector.detect = lambda p: (_ for _ in ()).throw(sp.RaceGap('main advanced'))
        self.assertFalse(self.batch())
        state = json.loads(self.state.read_text())
        self.assertEqual(set(state['pending']), {1, 2})
        self.assertEqual(state['failed'], {})

    def test_single_pr_canary_rejects_batch_scope(self):
        with self.assertRaisesRegex(sp.Gap, 'focused canary'):
            self.batch('--pr', '2', '--apply')

    def test_network_failure_is_pending_for_next_wake(self):
        from urllib.error import HTTPError
        self.detector.detect = lambda p: (_ for _ in ()).throw(
            HTTPError('https://api.github.com', 503, 'unavailable', {}, None))
        self.assertFalse(self.batch())
        state = json.loads(self.state.read_text())
        self.assertEqual(state['pending'], [1, 2])
        self.assertEqual(state['failed'], {})

    def test_preexisting_output_does_not_hide_zero_progress_failure(self):
        self.state.write_text('{"old":true}')
        with patch.object(self.api, 'pages', side_effect=sp.BudgetGap('no quota')):
            with self.assertRaises(sp.BudgetGap):
                self.batch()
        self.assertEqual(json.loads(self.state.read_text()), {'old': True})

    def test_duplicate_page_entry_is_only_inspected_once(self):
        self.raw.append(dict(self.raw[0]))
        visited = []
        self.detector.detect = lambda p: (visited.append(p.number) or None, [])
        self.assertFalse(self.batch())
        self.assertEqual(visited, [1, 2])

    def test_explicit_sweep_flag_rechecks_unchanged_identities(self):
        self.assertFalse(self.batch())
        visited = []
        self.detector.detect = lambda p: (visited.append(p.number) or None, [])
        self.assertFalse(self.batch('--sweep'))
        self.assertEqual(visited, [1, 2])

    def test_interrupted_child_stays_ahead_of_unvisited_tail_after_progress(self):
        extra = dict(self.raw[1], number=3)
        self.raw.append(extra)
        def detect(p):
            if p.number == 2:
                raise sp.BudgetGap('quota')
            return None, []
        self.detector.detect = detect
        self.assertFalse(self.batch())
        self.assertEqual(json.loads(self.state.read_text())['pending'], [2, 3])

    def test_failed_child_fetch_remains_pending(self):
        self.fetch_failures = {1}
        def detect(p):
            if p.number == 1:
                raise sp.Gap('head changed while fetching')
            return None, []
        self.detector.detect = detect
        self.assertFalse(self.batch())
        state = json.loads(self.state.read_text())
        self.assertEqual(state['pending'], [1])
        self.assertEqual(state['failed'], {})

    def test_rate_limited_discovery_passes_and_preserves_previous_checkpoint(self):
        self.assertFalse(self.batch())
        previous = self.state.read_text()
        with patch.object(self.api, 'pages', side_effect=sp.RateLimitGap('GitHub quota exhausted')):
            self.assertFalse(self.batch())
        self.assertEqual(self.state.read_text(), previous)
        self.assertIn('"deferred": true', self.last_output)
        self.assertNotIn('no-confirmed-stack', self.last_output)

    def test_rate_limit_before_any_progress_keeps_all_work_pending(self):
        self.detector.detect = lambda p: (_ for _ in ()).throw(sp.RateLimitGap('GitHub quota exhausted'))
        self.assertFalse(self.batch('--apply'))
        state = json.loads(self.state.read_text())
        self.assertEqual(set(state['pending']), {1, 2})
        self.assertEqual(state['checked'], {})
        self.assertEqual(state['failed'], {})
        self.assertFalse(any(method != 'GET' for _, method, _ in self.api.calls))

    def test_rate_limit_during_preparation_retains_discovered_work(self):
        original = self.api.call
        def stop(path, *args, **kwargs):
            if path.startswith('pulls?state=closed'):
                raise sp.RateLimitGap('GitHub quota exhausted')
            return original(path, *args, **kwargs)
        with patch.object(self.api, 'call', side_effect=stop):
            self.assertFalse(self.batch())
        self.assertEqual(json.loads(self.state.read_text())['pending'], [1, 2])

    def test_rate_limit_after_mutation_starts_still_fails(self):
        for args in (('--pr', '2'), ('--state-in', str(self.state), '--state-out', str(self.state))):
            with self.subTest(args=args):
                self.detector.detect = lambda p: (self.evidence if p.number == 2 else None, [])
                original = self.api.call
                def stop(path, method='GET', data=None):
                    if path == 'issues/2/comments' and method == 'POST':
                        raise sp.RateLimitGap('GitHub quota during explanation')
                    return original(path, method, data)
                with patch.object(self.api, 'call', side_effect=stop):
                    self.assertTrue(self.main('--apply', *args))
                self.assertFalse(any(path == 'pulls/2' and method == 'PATCH'
                                     for path, method, _ in self.api.calls))

    def test_focused_rate_limit_passes_without_mutation(self):
        self.detector.detect = lambda p: (_ for _ in ()).throw(sp.RateLimitGap('GitHub quota exhausted'))
        self.assertFalse(self.main('--pr', '2', '--apply'))
        self.assertIn('"deferred": true', self.last_output)

    def test_report_and_executing_step_both_show_rate_limit(self):
        report = Path(self.tmp.name) / 'audit.jsonl'
        with patch.object(self.api, 'pages', side_effect=sp.RateLimitGap('GitHub quota exhausted')):
            self.assertFalse(self.main('--report', str(report)))
        self.assertEqual(report.read_text(), self.last_output)
        self.assertIn('"deferred": true', report.read_text())

    def test_report_and_executing_step_both_show_other_failures(self):
        report = Path(self.tmp.name) / 'audit.jsonl'
        with patch.object(self.api, 'pages', side_effect=sp.Gap('permission denied')):
            with self.assertRaises(sp.Gap):
                self.main('--report', str(report))
        self.assertEqual(report.read_text(), self.last_output)
        self.assertIn('permission denied', report.read_text())

    def test_idle_batch_avoids_main_and_source_reads(self):
        # Eventless calls without --sweep still request a full audit. Supply a
        # superseded event to exercise the automatic consumer's idle path.
        self.assertFalse(self.batch())
        with patch.object(self.api, 'main') as main_read:
            self.assertFalse(self.event({'pull_request': {'number': 999}},
                '--state-in', str(self.state), '--state-out', str(self.state)))
        main_read.assert_not_called()
        self.last_source.fetch.assert_not_called()


class RateLimitTests(unittest.TestCase):
    def test_primary_limit_and_429_are_distinct_from_permission_denial(self):
        from urllib.error import HTTPError
        for status, headers in ((403, {'x-ratelimit-remaining': '0'}),
                                (403, {'retry-after': '60'}), (429, {})):
            with self.subTest(status=status, headers=headers):
                api = object.__new__(sp.API)
                api.token, api.calls, api.bytes = 'fixture', 0, 0
                api.deadline = sp.time.monotonic() + 1200
                error = HTTPError('https://api.github.com', status, 'throttled', headers, io.BytesIO(b'{}'))
                with patch.object(sp, 'urlopen', side_effect=error):
                    with self.assertRaises(sp.RateLimitGap):
                        api.request('https://api.github.com/repos/owner/repo/pulls')

    def test_headerless_secondary_limit_stops_instead_of_failing_each_child(self):
        from urllib.error import HTTPError
        for message in ('secondary rate limit', 'abuse detection', 'creating content too quickly'):
            api = object.__new__(sp.API)
            api.token, api.calls, api.bytes = 'fixture', 0, 0
            api.deadline = sp.time.monotonic() + 1200
            error = HTTPError('https://api.github.com', 403, 'Forbidden', {},
                              io.BytesIO(json.dumps({'message': message}).encode()))
            with patch.object(sp, 'urlopen', side_effect=error):
                with self.assertRaises(sp.RateLimitGap):
                    api.request('https://api.github.com/repos/owner/repo/pulls')

    def test_permission_denial_is_not_successful_quota_deferral(self):
        from urllib.error import HTTPError
        api = object.__new__(sp.API)
        api.token, api.calls, api.bytes = 'fixture', 0, 0
        api.deadline = sp.time.monotonic() + 1200
        error = HTTPError('https://api.github.com', 403, 'Forbidden', {},
                          io.BytesIO(b'{"message":"Resource not accessible by integration"}'))
        with patch.object(sp, 'urlopen', side_effect=error):
            with self.assertRaises(HTTPError):
                api.request('https://api.github.com/repos/owner/repo/pulls')
