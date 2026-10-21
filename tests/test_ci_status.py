import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).parents[1] / 'tauceti_tools/assets/scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('ci_status', SCRIPTS / 'ci_status.py')
ci_status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_status)


class FakeGitHub:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.reported = False

    def request(self, method, path, data=None, **kwargs):
        self.calls.append((method, path, data, kwargs))
        # Existing fixtures default to an empty current status history. Tests
        # covering cross-run fencing supply an explicit list for this endpoint.
        if '/commits/' in path and '/statuses?' in path and (not self.replies or not isinstance(self.replies[0], list)):
            return []
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def report(self):
        self.reported = True


class StatusIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'RUNNER_TEMP': str(self.directory), 'GITHUB_REPOSITORY': 'Owner/Repo',
                                          'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sha = 'a' * 40
        self.run = {'id': 123, 'run_attempt': 2, 'conclusion': None, 'run_started_at': '2026-10-21T02:00:00Z'}
        self.pull = {'head': {'sha': self.sha}, 'base': {'ref': 'main'}}

    def record(self, context='build', **kwargs):
        return ci_status.record_intent(self.sha, context, 'success', 'trusted checks passed',
                                       pr=7, base_ref='main', **kwargs)

    def publish(self, records, replies, **kwargs):
        fake = FakeGitHub(replies)
        with patch.object(ci_status, 'GitHub', return_value=fake):
            result = ci_status.publish(records, **kwargs)
        self.assertTrue(fake.reported)
        return result, fake

    def test_intent_saved_before_any_request_and_invalid_sha_does_not_write(self):
        with patch.object(ci_status, 'GitHub') as api:
            for value in ('', 'main', self.sha + '\n', '0' * 40):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    ci_status.record_intent(value, 'build', 'success', 'pass')
            api.assert_not_called()
            self.assertFalse((self.directory / 'tauceti-status-intents').exists())
            path, record = self.record()
            self.assertEqual(json.loads(path.read_text()), record)
            api.assert_not_called()

    def test_healthy_three_status_group_reuses_identity_and_status_snapshot(self):
        records = [self.record(context) for context in ('bump-guard', 'build', 'scope')]
        result, fake = self.publish(records, [self.run, self.pull, {}, {}, {}])
        self.assertTrue(result)
        self.assertEqual([call[0] for call in fake.calls], ['GET', 'GET', 'GET', 'POST', 'POST', 'POST'])
        self.assertTrue(all(call[3] == {'idempotent': True} for call in fake.calls[3:]))
        self.assertTrue(all(not path.exists() for path, _ in records))

    def test_failed_post_retains_intent_without_repeating_other_success(self):
        first, second = self.record('build'), self.record('scope')
        failure = ci_status.APIError('POST', 'path', 422, {'message': 'invalid'})
        result, fake = self.publish([first, second], [self.run, self.pull, failure, {}])
        self.assertFalse(result)
        self.assertTrue(first[0].exists())
        self.assertFalse(second[0].exists())
        self.assertEqual(len(fake.calls), 5)

    def test_quota_without_wait_budget_retains_intent(self):
        record = self.record()
        result, fake = self.publish([record], [self.run, self.pull, ci_status.Deferred('write', ci_status.time.time() + 3600)], max_wait=0)
        self.assertFalse(result)
        self.assertTrue(record[0].exists())
        self.assertEqual(len(fake.calls), 4)

    def test_after_quota_wait_rechecks_pr_and_skips_moved_head(self):
        record = self.record()
        moved = {'head': {'sha': 'b' * 40}, 'base': {'ref': 'main'}}
        with patch.object(ci_status.time, 'sleep') as sleep:
            result, fake = self.publish([record], [self.run, self.pull,
                                       ci_status.Deferred('write', ci_status.time.time() + 2), self.run, moved], max_wait=10)
        self.assertTrue(result)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual([call[0] for call in fake.calls], ['GET', 'GET', 'GET', 'POST', 'GET', 'GET'])
        self.assertFalse(record[0].exists())

    def test_obsolete_attempt_cancelled_or_pr_retargeted_never_posts(self):
        for index, replies in enumerate(([dict(self.run, run_attempt=3)],
                                         [dict(self.run, conclusion='cancelled')],
                                         [self.run, {'head': {'sha': self.sha}, 'base': {'ref': 'other'}}])):
            with self.subTest(index=index):
                record = self.record()
                result, fake = self.publish([record], replies)
                self.assertTrue(result)
                self.assertTrue(all(call[0] == 'GET' for call in fake.calls))
                self.assertFalse(record[0].exists())

    def test_batch_context_without_pr_uses_only_attempt_fence(self):
        record = ci_status.record_intent(self.sha, 'bump-guard', 'success', 'pass')
        result, fake = self.publish([record], [self.run, {}])
        self.assertTrue(result)
        self.assertEqual([call[0] for call in fake.calls], ['GET', 'GET', 'POST'])

    def test_strict_schema_rejects_wrong_repo_run_context_payload_and_url(self):
        _, original = self.record()
        mutations = [dict(repo='Other/Repo'), dict(run_id=124), dict(attempt=3), dict(sha='main'),
                     dict(extra=True), dict(pr=None), dict(pr=True),
                     dict(payload=dict(original['payload'], context='admin')),
                     dict(payload=dict(original['payload'], target_url='https://evil.invalid/')),
                     dict(payload=dict(original['payload'], description='x' * 141)),
                     dict(payload=dict(original['payload'], extra=True))]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                ci_status.validate(dict(original, **mutation))

    def test_resume_bound_to_original_source_not_retry_run(self):
        _, record = self.record()
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '999', 'GITHUB_RUN_ATTEMPT': '1',
                                     'CI_STATUS_SOURCE_RUN_ID': '123', 'CI_STATUS_SOURCE_ATTEMPT': '2'}):
            ci_status.validate(record)
        with patch.dict(os.environ, {'CI_STATUS_SOURCE_RUN_ID': '124'}), self.assertRaises(ValueError):
            ci_status.validate(record)

    def test_validate_entire_bounded_directory_before_any_publish(self):
        path, _ = self.record()
        directory = path.parent
        (directory / 'scope.json').write_text('{}')
        with patch.object(ci_status, 'GitHub') as api, self.assertRaises(ValueError):
            ci_status.load_intents(directory)
        api.assert_not_called()
        (directory / 'scope.json').unlink()
        (directory / 'scope.json').symlink_to(path)
        with self.assertRaises(ValueError):
            ci_status.load_intents(directory)
        (directory / 'scope.json').unlink()
        for index in range(17):
            (directory / f'{index}.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'too many'):
            ci_status.load_intents(directory)

    def test_exact_named_files_and_no_work(self):
        self.assertEqual(ci_status.load_intents(self.directory / 'absent'), [])
        path, record = self.record()
        self.assertEqual(ci_status.load_intents(path.parent), [(path, record)])
        path.rename(path.with_name('other.json'))
        with self.assertRaises(ValueError):
            ci_status.load_intents(path.parent)

    def test_creation_truncates_description_but_replay_remains_strict(self):
        _, record = ci_status.record_intent(self.sha, 'build', 'success', 'x' * 300)
        self.assertEqual(record['payload']['description'], 'x' * 140)
        record['payload']['description'] += 'x'
        with self.assertRaises(ValueError):
            ci_status.validate(record)

    def approved_scope(self):
        return ci_status.record_intent(self.sha, 'scope', 'success', 'restricted paths approved', pr=7,
                                       base_ref='main', approval={'head': self.sha, 'base': 'b' * 40, 'pr': 7})

    def test_scope_approval_is_validated_and_bound_to_exact_status(self):
        path, record = self.approved_scope()
        self.assertEqual(ci_status.load_intents(path.parent), [(path, record)])
        for change in ({'head': 'c' * 40}, {'base': 'main'}, {'pr': 8}, {'pr': True}, {'extra': True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ci_status.validate(dict(record, approval=dict(record['approval'], **change)))
        with self.assertRaises(ValueError):
            ci_status.record_intent(self.sha, 'build', 'success', 'pass', pr=7, base_ref='main',
                                    approval=record['approval'])

    def test_revoked_scope_posts_red_instead_of_leaving_old_green(self):
        record = self.approved_scope()
        with patch.object(ci_status, 'scope_approved', return_value=False) as approval:
            result, fake = self.publish([record], [self.run, self.pull, {}])
        self.assertFalse(result)
        self.assertEqual(approval.call_count, 1)
        self.assertEqual(fake.calls[-1][2]['state'], 'failure')
        self.assertEqual(fake.calls[-1][2]['context'], 'scope')
        self.assertFalse(record[0].exists())

    def test_quota_retry_rechecks_scope_approval_before_posting(self):
        record = self.approved_scope()
        with patch.object(ci_status, 'scope_approved', side_effect=[True, False]) as approval, patch.object(ci_status.time, 'sleep'):
            result, fake = self.publish([record], [self.run, self.pull,
                                       ci_status.Deferred('write', ci_status.time.time() + 1), self.run, self.pull, {}], max_wait=10)
        self.assertFalse(result)
        self.assertEqual(approval.call_count, 2)
        self.assertEqual(fake.calls[3][2]['state'], 'success')
        self.assertEqual(fake.calls[-1][2]['state'], 'failure')

    def test_scope_verification_uses_shared_api_and_quota_is_not_revocation(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'TauCetiProject/TauCeti'}):
            _, record = self.approved_scope()
        fake = FakeGitHub([{'number': 7}, {'id': 123}])
        def verify(data, head, base, **kwargs):
            self.assertEqual(kwargs['api']('pulls/7'), {'number': 7})
            self.assertEqual(kwargs['api']('/user/123'), {'id': 123})
            return True
        with patch.object(ci_status.bors_scope_approval, 'observation', return_value={}), patch.object(ci_status.bors_scope_approval, 'verify', side_effect=verify):
            self.assertTrue(ci_status.scope_approved(fake, record))
        self.assertEqual([call[1] for call in fake.calls], ['repos/TauCetiProject/TauCeti/pulls/7', '/user/123'])
        limited = ci_status.Deferred('read', ci_status.time.time() + 3600)
        with patch.object(ci_status.bors_scope_approval, 'observation', return_value={}), patch.object(ci_status.bors_scope_approval, 'verify', side_effect=limited), self.assertRaises(ci_status.Deferred):
            ci_status.scope_approved(fake, record)

    def test_batch_paths_are_relative_unique_sorted_and_bounded(self):
        approval = {'head': self.sha, 'base': 'b' * 40, 'batch_id': 5, 'paths': ['TauCeti/X.lean', 'scripts/x']}
        _, record = ci_status.record_intent(self.sha, 'scope', 'success', 'approved', approval=approval)
        for paths in (['/etc/passwd'], ['../foo'], ['x/../foo'], ['x\nother'], ['x\\other'], ['x', 'x'], ['z', 'a'], []):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                ci_status.validate(dict(record, approval=dict(approval, paths=paths)))
        approval['paths'] = [f'TauCeti/LongModuleName{i:04d}.lean' for i in range(2000)]
        path, record = ci_status.record_intent(self.sha, 'scope', 'success', 'approved', approval=approval)
        self.assertGreater(path.stat().st_size, 16384)
        self.assertEqual(ci_status.load_intents(path.parent), [(path, record)])

    def test_bors_network_failure_retains_intent_and_never_posts_red(self):
        record = self.approved_scope()
        with patch.object(ci_status, 'scope_approved', side_effect=OSError('temporary network failure')):
            result, fake = self.publish([record], [self.run, self.pull], max_wait=0)
        self.assertFalse(result)
        self.assertTrue(record[0].exists())
        self.assertTrue(all(call[0] == 'GET' for call in fake.calls))

    def test_bors_network_retry_recovers_without_false_revocation(self):
        record = self.approved_scope()
        with patch.object(ci_status, 'scope_approved', side_effect=[OSError('temporary'), True]), patch.object(ci_status.time, 'sleep'):
            result, fake = self.publish([record], [self.run, self.pull, self.run, self.pull, {}], max_wait=10)
        self.assertTrue(result)
        self.assertFalse(record[0].exists())
        self.assertEqual(fake.calls[-1][2]['state'], 'success')

    def test_scope_observation_network_failure_is_not_a_valid_revocation(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'TauCetiProject/TauCeti'}):
            _, record = self.approved_scope()
        with patch.object(ci_status.bors_scope_approval, 'observation', side_effect=OSError('network')), self.assertRaises(OSError):
            ci_status.scope_approved(FakeGitHub([]), record)

    def test_newer_run_skips_only_its_exact_context(self):
        records = [self.record(context) for context in ('build', 'scope', 'bump-guard')]
        rows = [{'context': 'build', 'target_url': 'https://github.com/Owner/Repo/actions/runs/124', 'created_at': '2026-10-21T02:10:00Z'},
                {'context': 'scope', 'target_url': 'https://github.com/Owner/Repo/actions/runs/122'}]
        result, fake = self.publish(records, [self.run, self.pull, rows, {}, {}])
        self.assertTrue(result)
        posts = [call for call in fake.calls if call[0] == 'POST']
        self.assertEqual([call[2]['context'] for call in posts], ['scope', 'bump-guard'])
        self.assertEqual(len([call for call in fake.calls if '/statuses?' in call[1]]), 1)
        self.assertEqual(fake.calls[2][3], {'conditional': True, 'read': False})
        self.assertTrue(all(not path.exists() for path, _ in records))

    def test_status_target_must_be_exact_same_repository_run_url(self):
        for target in ('https://github.com/Other/Repo/actions/runs/999',
                       'https://github.com/Owner/Repo/actions/runs/999?evil=1',
                       'https://github.com/Owner/Repo/actions/runs/999/extra',
                       'https://evil.invalid/Owner/Repo/actions/runs/999',
                       'https://github.com/Owner/Repo/actions/runs/not-numeric', None):
            with self.subTest(target=target):
                record = self.record()
                result, fake = self.publish([record], [self.run, self.pull, [{'context': 'build', 'target_url': target}], {}])
                self.assertTrue(result)
                self.assertEqual(fake.calls[-1][0], 'POST')

    def test_only_first_matching_context_status_is_current(self):
        record = self.record()
        rows = [{'context': 'build', 'target_url': 'https://github.com/Owner/Repo/actions/runs/122'},
                {'context': 'build', 'target_url': 'https://github.com/Owner/Repo/actions/runs/124', 'created_at': '2026-10-21T02:10:00Z'}]
        result, fake = self.publish([record], [self.run, self.pull, rows, {}])
        self.assertTrue(result)
        self.assertEqual(fake.calls[-1][0], 'POST')

    def test_status_snapshot_refreshed_after_quota_wait(self):
        record = self.record()
        newer = [{'context': 'build', 'target_url': 'https://github.com/Owner/Repo/actions/runs/124', 'created_at': '2026-10-21T02:10:00Z'}]
        with patch.object(ci_status.time, 'sleep'):
            result, fake = self.publish([record], [self.run, self.pull, [],
                                       ci_status.Deferred('write', ci_status.time.time() + 1), self.run, self.pull, newer], max_wait=10)
        self.assertTrue(result)
        self.assertEqual(len([call for call in fake.calls if call[0] == 'POST']), 1)
        self.assertEqual(len([call for call in fake.calls if '/statuses?' in call[1]]), 2)
        self.assertFalse(record[0].exists())

    def test_truncated_history_without_context_retains_intent_without_pagination(self):
        record = self.record()
        rows = [{'context': 'other', 'target_url': None}] * 100
        result, fake = self.publish([record], [self.run, self.pull, rows])
        self.assertFalse(result)
        self.assertTrue(record[0].exists())
        self.assertEqual(len(fake.calls), 3)
        self.assertTrue(all(call[0] == 'GET' for call in fake.calls))

    def test_credential_renewal_error_retains_context_and_does_not_skip_others(self):
        first, second = self.record('build'), self.record('scope')
        result, fake = self.publish([first, second], [self.run, self.pull, RuntimeError('renewal temporarily unavailable'), {}], max_wait=0)
        self.assertFalse(result)
        self.assertTrue(first[0].exists())
        self.assertFalse(second[0].exists())
        self.assertEqual(fake.calls[-1][2]['context'], 'scope')

    def test_later_attempt_of_older_run_can_replace_prior_newer_numbered_run(self):
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '100'}):
            record = self.record()
        rerun = dict(self.run, id=100, run_started_at='2026-10-21T03:00:00Z')
        rows = [{'context': 'build', 'target_url': 'https://github.com/Owner/Repo/actions/runs/101',
                 'created_at': '2026-10-21T02:30:00Z'}]
        result, fake = self.publish([record], [rerun, self.pull, rows, {}])
        self.assertTrue(result)
        self.assertEqual(fake.calls[-1][0], 'POST')
        self.assertFalse(record[0].exists())

    def test_older_rerun_does_not_replace_status_posted_after_attempt_start(self):
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '100'}):
            record = self.record()
        rerun = dict(self.run, id=100, run_started_at='2026-10-21T03:00:00Z')
        rows = [{'context': 'build', 'target_url': 'https://github.com/Owner/Repo/actions/runs/101',
                 'created_at': '2026-10-21T03:00:01Z'}]
        result, fake = self.publish([record], [rerun, self.pull, rows])
        self.assertTrue(result)
        self.assertTrue(all(call[0] == 'GET' for call in fake.calls))
        self.assertFalse(record[0].exists())

    def test_newer_run_with_unreadable_timestamp_retains_intent(self):
        for created in (None, 'not-a-time', '2026-10-21T03:00:00'):
            with self.subTest(created_at=created):
                record = self.record()
                rows = [{'context': 'build', 'target_url': 'https://github.com/Owner/Repo/actions/runs/124', 'created_at': created}]
                result, fake = self.publish([record], [self.run, self.pull, rows])
                self.assertFalse(result)
                self.assertTrue(record[0].exists())
                self.assertTrue(all(call[0] == 'GET' for call in fake.calls))

    def test_asset_copies_match(self):
        self.assertEqual((SCRIPTS / 'ci_status.py').read_bytes(),
                         (SCRIPTS.parent / 'project/scripts/ci_status.py').read_bytes())


if __name__ == '__main__':
    unittest.main()
