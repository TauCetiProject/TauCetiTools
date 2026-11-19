"""Incremental discovery never substitutes workflow history for status events."""

import copy
import unittest
from unittest.mock import Mock

import discovery
from tauceti_tools.github_state import Unavailable


class DiscoveryTests(unittest.TestCase):
    def checkpoint(self):
        return dict(shared_discovery=dict(revision=10, heads={'7': 'a'*40, '8': 'b'*40},
                                          policy='pin', repair_after=0), decisions={})

    def test_status_and_deleted_scoreboard_changes_coalesce_same_pr(self):
        state, checkpoint = Mock(), self.checkpoint()
        state.changes.return_value = 13, [dict(kind='status_context', id='a'*40+':build'),
                                         dict(kind='issue_comments', id='42', parent='7', tombstone=1)]
        self.assertEqual(discovery.discover(state, checkpoint, 'pin', repair_limit=0), [7])
        self.assertEqual(checkpoint['shared_discovery']['revision'], 13)
        state.query.assert_not_called()

    def test_failed_discovery_cannot_advance_watermark(self):
        state, checkpoint = Mock(), self.checkpoint()
        before = copy.deepcopy(checkpoint)
        state.changes.side_effect = Unavailable('transport_hold')
        with self.assertRaises(Unavailable):
            discovery.discover(state, checkpoint, 'pin')
        self.assertEqual(checkpoint, before)

    def test_lost_journal_rebuilds_from_complete_inventory(self):
        state, checkpoint = Mock(), self.checkpoint()
        state.changes.side_effect = Unavailable('change_log_gap')
        state._get.return_value = dict(revision=100)
        state.query.return_value = dict(data=[dict(number=9, headRefOid='c'*40)])
        self.assertEqual(discovery.discover(state, checkpoint, 'pin'), [9])
        self.assertEqual(checkpoint['shared_discovery']['revision'], 100)
        self.assertEqual(checkpoint['shared_discovery']['heads'], {'9': 'c'*40})

    def test_inventory_grace_cannot_advance_past_unobserved_events(self):
        state, checkpoint = Mock(), {}
        state._get.return_value = dict(revision=100)
        state.query.return_value = dict(revision=95, data=[dict(number=7, headRefOid='a'*40)])
        discovery.discover(state, checkpoint, 'pin', repair_limit=0)
        self.assertEqual(checkpoint['shared_discovery']['revision'], 95)
        state.changes.return_value = 101, [dict(revision=97, kind='pr', id='8')]
        self.assertEqual(discovery.discover(state, checkpoint, 'pin', repair_limit=0), [8])

    def test_main_and_policy_changes_invalidate_all_prs(self):
        state, checkpoint = Mock(), self.checkpoint()
        state.changes.return_value = 11, [dict(kind='refs', id='refs/heads/main')]
        self.assertEqual(discovery.discover(state, checkpoint, 'pin', repair_limit=0), [7, 8])
        state._get.return_value = dict(revision=20)
        state.query.return_value = dict(data=[dict(number=7, headRefOid='a'*40)])
        self.assertEqual(discovery.discover(state, checkpoint, 'new-pin', repair_limit=0), [7])
        self.assertEqual(checkpoint['shared_discovery']['policy'], 'new-pin')

    def test_rotating_repairs_and_time_expiry_retain_coverage(self):
        state, checkpoint = Mock(), self.checkpoint()
        state.changes.return_value = 10, []
        self.assertEqual(discovery.discover(state, checkpoint, 'pin', repair_limit=1), [7])
        self.assertEqual(discovery.discover(state, checkpoint, 'pin', repair_limit=1), [8])
        checkpoint['decisions'] = {'8': {'valid_until': 0}}
        self.assertEqual(discovery.discover(state, checkpoint, 'pin', repair_limit=1), [7, 8])


if __name__ == '__main__':
    unittest.main()
