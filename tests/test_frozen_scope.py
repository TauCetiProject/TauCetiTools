"""Frozen admission, immutable path completeness, and authority fences."""
import copy
from email.message import Message
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'tauceti_tools/assets/scripts'
sys.path.insert(0, str(SCRIPTS))
import bors_observation as authority
import bors_scope_approval as policy
from github_api import APIError


def oid(value):
    return hashlib.sha1(value.encode()).hexdigest()


BASE, HEAD, TIP = map(oid, ('base', 'accepted', 'combined'))


class FrozenTests(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.calls = []
        self.admin = True
        self.data = dict(schema='tauceti-bors.observation/v1', repo=policy.REPO, base='main',
            scope_approvals=[], requested_batch=dict(id=37, state='running', base='main',
                base_sha=BASE, head_sha=TIP, members=[dict(pr=1, head_sha=HEAD)]))
        self.files(BASE, {'scripts/x': ('old', '100644'), 'TauCeti/Old.lean': ('old', '100644')})
        self.files(HEAD, {'scripts/x': ('new', '100644'), 'TauCeti/New.lean': ('new', '100644')})
        self.approve()
        self.refresh = lambda: (copy.deepcopy(self.data), authority.FROZEN_ADMISSION)

    def files(self, commit, paths):
        root = {}
        for path, (content, mode) in paths.items():
            parts = path.split('/')
            cursor = root
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = (content, mode)
        def tree(items):
            entries = []
            for name, value in sorted(items.items()):
                if isinstance(value, dict):
                    entry = dict(path=name, type='tree', mode='040000', sha=tree(value))
                else:
                    content, mode = value
                    entry = dict(path=name, type='commit' if mode == '160000' else 'blob', mode=mode, sha=oid(content))
                entries.append(entry)
            sha = oid(json.dumps(entries, sort_keys=True))
            self.objects[f'git/trees/{sha}'] = dict(sha=sha, tree=entries, truncated=False)
            return sha
        self.objects[f'git/commits/{commit}'] = dict(sha=commit, tree=dict(sha=tree(root)))

    def approve(self):
        self.data['scope_approvals'] = [dict(pr=1, actor_id=7, head_sha=HEAD, merge_base_sha=BASE)]

    def api(self, path):
        self.calls.append(path)
        if path.startswith('compare/'):
            # Deliberately misleading/incomplete file list must never be used.
            return dict(merge_base_commit=dict(sha=BASE), files=[])
        if path == '/user/7':
            return dict(id=7, login='admin')
        if path == 'collaborators/admin/permission':
            return dict(user=dict(id=7), permission='admin' if self.admin else 'write')
        if path.startswith('pulls/'):
            raise AssertionError('frozen verification must not read mutable PRs')
        return copy.deepcopy(self.objects[path])

    def verify(self, **kwargs):
        return policy.verify(self.data, TIP, BASE, batch_id=37,
            combined_paths=kwargs.pop('paths', ['scripts/x']), api=kwargs.pop('api', self.api),
            admission=kwargs.pop('admission', authority.FROZEN_ADMISSION), refresh=self.refresh, **kwargs)

    def test_accepted_commits_only_with_effective_admin_and_complete_coverage(self):
        self.assertTrue(self.verify())
        self.assertFalse(self.verify(paths=['scripts/not-approved']))
        self.admin = False
        self.assertFalse(self.verify())

    def test_legacy_and_individual_pr_still_check_live_revision(self):
        with self.assertRaises(AssertionError):
            self.verify(admission=None)
        with self.assertRaises(AssertionError):
            policy.verify(self.data, HEAD, BASE, pr=1, api=self.api,
                admission=authority.FROZEN_ADMISSION)
        self.data['frozen_admission'] = authority.FROZEN_ADMISSION
        with self.assertRaises(AssertionError):
            self.verify(admission=None)

    def test_exact_approval_binding_duplicate_and_merge_base(self):
        for change in [dict(head_sha=TIP), dict(merge_base_sha=TIP), dict(actor_id=True), dict(extra=True)]:
            self.approve(); self.data['scope_approvals'][0].update(change)
            self.assertFalse(self.verify())
        self.approve(); self.data['scope_approvals'] *= 2
        self.assertFalse(self.verify())

    def test_fence_revoked_approval_membership_capability_and_role(self):
        for mutation in [lambda d: d.update(scope_approvals=[]),
                         lambda d: d['requested_batch'].update(head_sha=HEAD),
                         lambda d: d['requested_batch'].update(state='canceled')]:
            after = copy.deepcopy(self.data); mutation(after)
            self.refresh = lambda: (after, authority.FROZEN_ADMISSION)
            self.assertFalse(self.verify())
        self.refresh = lambda: (copy.deepcopy(self.data), None)
        self.assertFalse(self.verify())
        def demote():
            self.admin = False
            return copy.deepcopy(self.data), authority.FROZEN_ADMISSION
        self.refresh = demote
        self.assertFalse(self.verify())

    def test_tree_diff_modes_renames_deletions_symlinks_and_submodules(self):
        cases = [
            ({'scripts/x': ('x', '100644')}, {'TauCeti/X': ('x', '100644')}, {'scripts/x'}),
            ({'.github/x': ('x', '100644')}, {}, {'.github/x'}),
            ({'scripts/x': ('x', '100644')}, {'scripts/x': ('x', '100755')}, {'scripts/x'}),
            ({}, {'root-link': ('TauCeti/X', '120000')}, {'root-link'}),
            ({'scripts/x': ('x', '100644')}, {'scripts/x/child': ('x', '100644')}, {'scripts/x', 'scripts/x/child'}),
            ({}, {'dependency': ('commit', '160000')}, {'dependency'}),
            ({}, {'docbuild/docs/references.bib': ('x', '100644'), 'docbuild/other': ('x', '100644')}, {'docbuild/other'}),
            ({'TauCeti': ('x', '120000')}, {'TauCeti/X': ('x', '100644')}, {'TauCeti'}),
            ({'lean-toolchain': ('x', '100644')}, {'lean-toolchain/child': ('x', '100644')}, {'lean-toolchain/child'}),
        ]
        for before, after, expected in cases:
            self.files(BASE, before); self.files(HEAD, after)
            self.assertEqual(policy.ImmutablePaths(self.api).changed(BASE, HEAD), expected)
            self.assertEqual(policy.ImmutablePaths(self.api).changed(HEAD, BASE), expected)

    def test_unapproved_restricted_member_rejected_even_with_over_300_files(self):
        self.files(HEAD, {**{f'TauCeti/F{i}.lean': ('x', '100644') for i in range(3500)},
                          'scripts/x': ('new', '100644')})
        self.assertTrue(self.verify())
        self.data['scope_approvals'] = []
        self.assertFalse(self.verify())
        self.files(BASE, {}); self.files(HEAD, {f'TauCeti/F{i}': ('x', '100644') for i in range(3500)})
        self.assertTrue(self.verify(paths=['TauCeti/F3499']))

    def test_truncated_invalid_or_duplicate_tree_entries_fail_closed(self):
        root = self.objects[f'git/commits/{HEAD}']['tree']['sha']
        original = copy.deepcopy(self.objects[f'git/trees/{root}'])
        for change in [dict(truncated=True), dict(tree=[dict(path='../x')]),
                       dict(tree=original['tree'] * 2), dict(tree=[None])]:
            self.objects[f'git/trees/{root}'] = dict(original, **change)
            with self.assertRaises(ValueError):
                self.verify()

    def test_unavailable_immutable_object_is_visible_failure_but_quota_propagates(self):
        def unavailable(path):
            raise APIError('GET', path, 404, {})
        with self.assertRaises(ValueError):
            self.verify(api=unavailable)
        def limited(path):
            raise APIError('GET', path, 403, {})
        with self.assertRaises(APIError):
            self.verify(api=limited)

    def test_large_batch_shared_objects_are_cached_and_every_member_checked(self):
        self.data['requested_batch']['members'] = [dict(pr=n, head_sha=HEAD) for n in range(1, 1201)]
        self.files(BASE, {}); self.files(HEAD, {'TauCeti/x': ('x', '100644')})
        self.data['scope_approvals'] = []
        self.assertTrue(self.verify(paths=['TauCeti/x']))
        self.assertEqual(sum(p.startswith('compare/') for p in self.calls), 1200)
        self.assertLess(len(self.calls), 1210)
        self.data['requested_batch']['members'][-1]['head_sha'] = 'invalid'
        self.assertFalse(self.verify(paths=['TauCeti/x']))


class AuthorityTests(unittest.TestCase):
    def test_header_only_capability_redirect_and_duplicates(self):
        data = dict(schema='tauceti-bors.observation/v1', repo=policy.REPO, base='main',
                    frozen_admission=authority.FROZEN_ADMISSION)
        with patch.object(authority.urllib.request, 'urlopen') as opened:
            response = opened.return_value.__enter__.return_value
            response.read.return_value = json.dumps(data).encode()
            response.geturl.return_value = authority.OBSERVATION_ROOT + '?base=main&batch_id=37'
            for values, expected in [([], None), (['unknown'], None), ([authority.FROZEN_ADMISSION], authority.FROZEN_ADMISSION)]:
                headers = Message()
                for value in values: headers[authority.ADMISSION_HEADER] = value
                response.headers = headers
                self.assertEqual(authority.observation_with_admission(37), (data, expected))
            response.headers[authority.ADMISSION_HEADER] = authority.FROZEN_ADMISSION
            with self.assertRaises(ValueError): authority.observation_with_admission(37)
            response.headers = Message()
            response.geturl.return_value = 'https://elsewhere.invalid/'
            with self.assertRaises(ValueError): authority.observation_with_admission(37)


if __name__ == '__main__':
    unittest.main()
