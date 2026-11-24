"""The attested-landing confirmation (bors_attested.py) against the queue's observation, without network I/O."""
import copy
import unittest

from bors_attested import attested

TREE = 'c' * 40
BATCH = dict(id=7, base='main', state='running', head_sha='a' * 40, base_sha='b' * 40,
             attestation=dict(tip='a' * 40, tree=TREE, task_id=12))
OBSERVATION = dict(schema='tauceti-bors.observation/v1', repo='TauCetiProject/TauCeti', base='main',
                   requested_batch=BATCH)


def confirm(data, tree=TREE):
    return attested(data, 'a' * 40, 'b' * 40, tree, batch_id=7)


class AttestedTests(unittest.TestCase):
    def test_the_queue_attests_exactly_this_commit_and_tree(self):
        self.assertEqual(confirm(OBSERVATION)['task_id'], 12)

    def test_no_attestation_or_another_commit_or_tree_is_refused(self):
        for change in (dict(attestation=None), dict(attestation='yes'),
                       dict(attestation=dict(tip='d' * 40, tree=TREE)),
                       dict(attestation=dict(tip='a' * 40, tree='e' * 40))):
            with self.subTest(change=change):
                data = copy.deepcopy(OBSERVATION)
                data['requested_batch'].update(change)
                with self.assertRaises(ValueError):
                    confirm(data)
        with self.assertRaises(ValueError):
            confirm(OBSERVATION, tree='')

    def test_a_batch_that_is_not_running_with_this_head_and_base_is_refused(self):
        for field, value in [('state', 'waiting'), ('head_sha', 'd' * 40), ('base_sha', 'e' * 40), ('id', 8)]:
            with self.subTest(field=field):
                data = copy.deepcopy(OBSERVATION)
                data['requested_batch'][field] = value
                with self.assertRaises(ValueError):
                    confirm(data)


if __name__ == '__main__':
    unittest.main()
