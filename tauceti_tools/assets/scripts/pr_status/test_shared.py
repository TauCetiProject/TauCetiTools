"""Readiness adapter coherence and invalidation regressions."""

import copy
import unittest
from unittest.mock import Mock

from shared import Evidence, Unavailable, affects

HEADER = dict(number=7, state='OPEN', isDraft=False, headRefOid='a'*40, headRefName='math',
              baseRefName='main', baseRefOid='b'*40, author={'login': 'peer'}, labels=[],
              headRepositoryOwner={'login': 'org'}, headRepository={'name': 'repo'}, mergeable='MERGEABLE')


class EvidenceTests(unittest.TestCase):
    def state(self, revisions=(10, 10, 10)):
        state = Mock()
        state.query.side_effect = [dict(revision=revisions[0], data=copy.deepcopy(HEADER)),
                                   dict(revision=revisions[0], data=[{'ref': 'refs/heads/main', 'object': {'sha': 'b'*40}}]),
                                   dict(revision=revisions[1], data=[{'id': '1', 'body': 'review'}]),
                                   dict(revision=revisions[2], data=[{'context': 'build', 'state': 'SUCCESS'}])]
        state.changes.return_value = 12, []
        return state

    def test_binds_status_query_to_header_head_and_retains_comment_bodies(self):
        state = self.state()
        pr, comments, statuses = Evidence('org/repo', state=state).read(7)
        self.assertEqual(pr['head']['sha'], 'a'*40)
        self.assertEqual(pr['base'], {'ref': 'main', 'sha': 'b'*40})
        self.assertEqual(comments, [{'id': '1', 'body': 'review'}])
        self.assertEqual(statuses, {'build': 'success'})
        state.query.assert_called_with('statuses', sha='a'*40)
        state.changes.assert_not_called()

    def test_deleted_scoreboard_and_head_changes_defer_mixed_evidence(self):
        for row in [dict(kind='issue_comments', id='1', parent='7', tombstone=1),
                    dict(kind='pr', id='7'),
                    dict(kind='collection', id='issue_comments:{"number":7}')]:
            state = self.state((10, 11, 12))
            state.changes.return_value = 12, [row]
            with self.subTest(row=row), self.assertRaisesRegex(Unavailable, 'concurrent_readiness_input'):
                Evidence('org/repo', state=state).read(7)

    def test_final_header_fence_catches_status_change_after_initial_snapshot(self):
        state = self.state((10, 11, 12))
        evidence = Evidence('org/repo', state=state)
        evidence.read(7)
        state.query.side_effect = [dict(revision=14, data=copy.deepcopy(HEADER))]
        state.changes.return_value = 14, [dict(revision=13, kind='status_context', id='a'*40+':build')]
        with self.assertRaisesRegex(Unavailable, 'concurrent_readiness_input'):
            evidence.read(7, comments=False)

    def test_unrelated_pr_activity_does_not_cause_starvation(self):
        state = self.state((10, 11, 12))
        state.changes.return_value = 12, [dict(kind='pr', id='8'),
                                        dict(kind='issue_comments', id='1', parent='8')]
        Evidence('org/repo', state=state).read(7)

    def test_missing_parent_contract_is_unknown_including_retired_rows(self):
        for row in [dict(kind='issue_comments', id='1'), dict(kind='issue_comments', id='1', parent=None)]:
            with self.assertRaisesRegex(Unavailable, 'change_parent_contract'):
                affects(row, 7, 'a'*40)


if __name__ == '__main__':
    unittest.main()
