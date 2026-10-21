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
        self.run = {'id': 123, 'run_attempt': 2, 'conclusion': None}
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

    def test_healthy_three_status_group_reuses_two_fences(self):
        records = [self.record(context) for context in ('bump-guard', 'build', 'scope')]
        result, fake = self.publish(records, [self.run, self.pull, {}, {}, {}])
        self.assertTrue(result)
        self.assertEqual([call[0] for call in fake.calls], ['GET', 'GET', 'POST', 'POST', 'POST'])
        self.assertTrue(all(call[3] == {'idempotent': True} for call in fake.calls[2:]))
        self.assertTrue(all(not path.exists() for path, _ in records))

    def test_failed_post_retains_intent_without_repeating_other_success(self):
        first, second = self.record('build'), self.record('scope')
        failure = ci_status.APIError('POST', 'path', 422, {'message': 'invalid'})
        result, fake = self.publish([first, second], [self.run, self.pull, failure, {}])
        self.assertFalse(result)
        self.assertTrue(first[0].exists())
        self.assertFalse(second[0].exists())
        self.assertEqual(len(fake.calls), 4)

    def test_quota_without_wait_budget_retains_intent(self):
        record = self.record()
        result, fake = self.publish([record], [self.run, self.pull, ci_status.Deferred('write', ci_status.time.time() + 3600)], max_wait=0)
        self.assertFalse(result)
        self.assertTrue(record[0].exists())
        self.assertEqual(len(fake.calls), 3)

    def test_after_quota_wait_rechecks_pr_and_skips_moved_head(self):
        record = self.record()
        moved = {'head': {'sha': 'b' * 40}, 'base': {'ref': 'main'}}
        with patch.object(ci_status.time, 'sleep') as sleep:
            result, fake = self.publish([record], [self.run, self.pull,
                                       ci_status.Deferred('write', ci_status.time.time() + 2), self.run, moved], max_wait=10)
        self.assertTrue(result)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual([call[0] for call in fake.calls], ['GET', 'GET', 'POST', 'GET', 'GET'])
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
        self.assertEqual([call[0] for call in fake.calls], ['GET', 'POST'])

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

    def test_asset_copies_match(self):
        self.assertEqual((SCRIPTS / 'ci_status.py').read_bytes(),
                         (SCRIPTS.parent / 'project/scripts/ci_status.py').read_bytes())


if __name__ == '__main__':
    unittest.main()
