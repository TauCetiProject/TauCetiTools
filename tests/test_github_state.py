"""Shared evidence contract tests; unavailable reads never call GitHub directly."""

import io
import json
import unittest
from unittest.mock import patch

from tauceti_tools.github_state import State, Unavailable


def page(data, **extra):
    return dict(version=1, repository='org/repo', operation='issue_comments', status='ok',
                complete=True, coverage='complete', revision=10, data=data, **extra)


class SharedStateTests(unittest.TestCase):
    def query(self, responses):
        state = State('org/repo', token='secret')
        stream = [io.BytesIO(json.dumps(row).encode()) for row in responses]
        with patch('urllib.request.urlopen', side_effect=stream):
            return state.query('issue_comments', number=7)

    def test_complete_pages_retain_snapshot_revision(self):
        result = self.query([page([{'id': '1'}], cursor='next'), page([{'id': '2'}])])
        self.assertEqual(result['data'], [{'id': '1'}, {'id': '2'}])
        self.assertEqual(result['revision'], 10)

    def test_incomplete_mixed_or_wrong_identity_never_becomes_empty_history(self):
        for key, value in [('complete', False), ('coverage', 'webhook_observed'),
                           ('repository', 'org/other'), ('operation', 'reviews'), ('status', 'unavailable')]:
            bad = page([])
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(Unavailable):
                self.query([bad])
        bad = page([])
        bad['revision'] = 11
        with self.assertRaisesRegex(Unavailable, 'mixed_pagination_revision'):
            self.query([page([], cursor='next'), bad])

    def test_change_boundary_is_read_completely_and_keeps_later_events_pending(self):
        state = State('org/repo')
        pages = [dict(revision=20, reset=False, changes=[{'revision': 11}, {'revision': 12}]),
                 dict(revision=21, reset=False, changes=[{'revision': 20}, {'revision': 21}])]
        with patch.object(state, '_get', side_effect=pages) as read:
            boundary, changed = state.changes(10)
        self.assertEqual(boundary, 20)
        self.assertEqual([row['revision'] for row in changed], [11, 12, 20])
        self.assertEqual(read.call_args.kwargs, {'after': '12'})

    def test_gap_requires_backfill(self):
        state = State('org/repo')
        with patch.object(state, '_get', return_value=dict(revision=20, reset=True, changes=[])):
            with self.assertRaisesRegex(Unavailable, 'change_log_gap'):
                state.changes(1)

    def test_transport_outage_is_coordinated_without_repeated_requests(self):
        state = State('org/repo')
        with patch('urllib.request.urlopen', side_effect=OSError('secret URL')) as request:
            for _ in range(10):
                with self.assertRaises(Unavailable) as caught:
                    state.query('pr', number=7)
                self.assertNotIn('secret', str(caught.exception))
        request.assert_called_once()


if __name__ == '__main__':
    unittest.main()
