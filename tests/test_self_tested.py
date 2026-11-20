"""Persistent declarations use inline author/scope eligibility, with no Actions receipts."""

import base64
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_tools import self_tested as policy

SCRIPTS = Path(__file__).parents[1] / "tauceti_tools/assets/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("local_route", SCRIPTS / "self_tested.py")
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)

REPO = "TauCetiProject/TauCeti"
HEAD = "a" * 40


class API:
    def __init__(self):
        self.pr = dict(
            number=7,
            state="open",
            user=dict(login="author"),
            labels=[dict(name="self-tested")],
            base=dict(ref="main"),
            head=dict(ref="fix/math", sha=HEAD, repo=dict(full_name=REPO)),
            changed_files=1,
        )
        self.files = [dict(filename="TauCeti/A.lean", status="modified")]
        self.member = True
        self.calls = []
        self.events = [dict(id=1, event="labeled", label=dict(name="self-tested"), actor=dict(login="trusted"))]

    def request(self, method, path, *args, **kwargs):
        self.calls.append((method, path))
        if method != "GET":
            raise AssertionError("eligibility must never write statuses, labels or workflow requests")
        if path.startswith("orgs/"):
            return dict(state="active" if self.member else "inactive")
        if "/files?" in path:
            return copy.deepcopy(self.files)
        if "/events?" in path:
            return copy.deepcopy(self.events)
        if path.endswith("/pulls/7"):
            return copy.deepcopy(self.pr)
        raise AssertionError("unexpected observation: " + path)


class PersistentRouteTests(unittest.TestCase):
    def setUp(self):
        policy._MEMBERS.clear()
        self.api = API()

    def decide(self, **kwargs):
        return route.decide(self.api, REPO, 7, **kwargs)

    def test_label_is_enough_without_any_status_or_timeline_history(self):
        self.assertTrue(self.decide())
        self.assertFalse(any("/status" in p or "/events" in p or "graphql" in p for _, p in self.api.calls))
        self.assertTrue(all(m == "GET" for m, _ in self.api.calls))

    def test_pushes_fix_rounds_and_main_advances_preserve_the_declaration(self):
        for head in ["a" * 40, "b" * 40, "c" * 40]:
            self.api.pr["head"]["sha"] = head
            self.assertTrue(self.decide())
            self.assertEqual(self.api.pr["labels"], [dict(name="self-tested")])
        self.api.pr["base"]["sha"] = "d" * 40
        self.assertTrue(self.decide())

    def test_member_and_source_scope_still_control_admission(self):
        for change in [
            "member",
            "infrastructure",
            "pins",
            "empty",
            "old-rename-path",
            "base",
            "closed",
            "full-lint",
            "lint-repair",
        ]:
            with self.subTest(change=change):
                self.setUp()
                if change == "member":
                    self.api.member = False
                elif change == "infrastructure":
                    self.api.files = [dict(filename="scripts/tool.py")]
                elif change == "pins":
                    self.api.files = [dict(filename="lean-toolchain")]
                elif change == "empty":
                    self.api.files = []
                    self.api.pr["changed_files"] = 0
                elif change == "old-rename-path":
                    self.api.files = [
                        dict(filename="TauCeti/A.lean", status="renamed", previous_filename="scripts/tool.py")
                    ]
                elif change == "base":
                    self.api.pr["base"]["ref"] = "other"
                elif change == "closed":
                    self.api.pr["state"] = "closed"
                else:
                    self.api.pr["labels"].append(dict(name=change))
                self.assertFalse(self.decide())
                self.assertEqual(self.api.pr["labels"][0]["name"], "self-tested")
                self.assertTrue(all(m == "GET" for m, _ in self.api.calls))

    def test_removed_declaration_returns_to_ci_without_inspecting_old_receipts(self):
        self.api.pr["labels"] = []
        self.assertFalse(self.decide(rows=[dict(context="self-tested/7", state="success")]))

    def test_failed_old_receipt_cannot_override_current_declaration(self):
        self.assertTrue(self.decide(rows=[dict(context="self-tested/7", state="failure")]))

    def test_existing_diff_is_reused_by_inline_admission(self):
        self.assertTrue(self.decide(pr=self.api.pr, paths=["TauCeti/A.lean"]))
        self.assertFalse(any("/files" in p or "/pulls/" in p for _, p in self.api.calls))

    def test_incomplete_files_and_unknown_membership_never_grant_admission(self):
        self.api.pr["changed_files"] = 2
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.decide()
        policy._MEMBERS.clear()
        original = self.api.request

        def unavailable(method, path):
            if path.startswith("orgs/"):
                raise RuntimeError("membership unavailable")
            return original(method, path)

        with patch.object(self.api, "request", side_effect=unavailable):
            with self.assertRaisesRegex(RuntimeError, "membership unavailable"):
                self.decide()

    def test_membership_cache_is_bounded_and_shared_between_inline_consumers(self):
        self.assertTrue(self.decide())
        self.assertTrue(self.decide())
        self.assertEqual(sum(p.startswith("orgs/") for _, p in self.api.calls), 1)
        for key, (at, value) in policy._MEMBERS.copy().items():
            policy._MEMBERS[key] = (at - 61, value)
        self.api.member = False
        self.assertFalse(self.decide())
        self.assertEqual(sum(p.startswith("orgs/") for _, p in self.api.calls), 2)

    def test_a_404_is_cached_like_any_other_answer(self):
        original = self.api.request

        def missing(method, path):
            if path.startswith("orgs/"):
                self.api.calls.append((method, path))
                error = RuntimeError("not found")
                error.status = 404
                raise error
            return original(method, path)

        with patch.object(self.api, "request", side_effect=missing):
            self.assertFalse(self.decide())
            self.assertFalse(self.decide())
        self.assertEqual(sum(p.startswith("orgs/") for _, p in self.api.calls), 1)

    def test_a_report_can_extend_the_cache_to_its_whole_run(self):
        self.assertTrue(self.decide())
        for key, (at, value) in policy._MEMBERS.copy().items():
            policy._MEMBERS[key] = (at - 3600, value)
        with patch.object(policy, "MEMBERSHIP_CACHE_SECONDS", float("inf")):
            self.assertTrue(self.decide())
        self.assertEqual(sum(p.startswith("orgs/") for _, p in self.api.calls), 1)

    def test_observe_uses_live_label_and_scope_not_a_status(self):
        self.assertTrue(policy.observe(REPO, 7, HEAD, lambda p: self.api.request("GET", p)))
        self.assertFalse(policy.observe(REPO, 7, "b" * 40, lambda p: self.api.request("GET", p)))
        self.api.pr["labels"] = []
        self.assertFalse(policy.observe(REPO, 7, HEAD, lambda p: self.api.request("GET", p)))

    def test_team_lookup_can_use_existing_org_read_credential(self):
        with patch.dict(os.environ, SELF_TESTED_TEAM_TOKEN="fixture-org-token"):
            with patch.object(policy, "gh_read", return_value={"state": "active"}) as org_read:
                self.assertTrue(self.decide())
        org_read.assert_called_once_with(
            "orgs/TauCetiProject/teams/self-tested/memberships/author", token="fixture-org-token"
        )
        self.assertFalse(any(p.startswith("orgs/") for _, p in self.api.calls))

    def test_unconfigured_repository_cannot_use_the_team(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(
                policy.eligible(
                    self.api.pr, "Other/Project", paths=["TauCeti/A.lean"], read=lambda p: self.api.request("GET", p)
                )
            )

    def test_requested_status_parameter_is_no_longer_a_gate(self):
        labels = [dict(name="self-tested")]
        for status in [None, "", "pending", "failure", "success"]:
            self.assertTrue(policy.accepted(labels, status, member=True))
            self.assertFalse(policy.accepted(labels, status, member=False))
        self.assertFalse(policy.accepted(labels + [dict(name="full-lint")], member=True))
        self.assertFalse(policy.accepted(labels, member=True, branch="lint-repair/x"))

    def test_inline_bump_reads_allow_missing_alternative_lakefile_from_queue_client(self):
        class Missing(RuntimeError):
            status = 404

        class QueueRead:
            def request(self, method, path):
                if "/lakefile.lean?" in path:
                    raise Missing("no alternative Lake file")
                return dict(type="file", encoding="base64", content=base64.b64encode(b"pins").decode())

        with tempfile.TemporaryDirectory() as directory:
            route.fetch_guard_files(QueueRead(), REPO, HEAD, Path(directory))
            self.assertFalse((Path(directory) / "lakefile.lean").exists())
            self.assertTrue((Path(directory) / "lakefile.toml").exists())

    def test_bumps_keep_their_trusted_labeller_and_real_pin_guard(self):
        self.api.pr["user"]["login"] = policy.BUMP_AUTHOR
        self.api.pr["head"]["ref"] = "bump-mathlib/latest"
        self.api.files = [dict(filename="lean-toolchain")]
        guard = unittest.mock.Mock()
        guard.check_bump.return_value = (True, "passed")
        with patch.object(policy, "_bump_policy", return_value=guard):
            self.assertTrue(self.decide())
            guard.check_bump.assert_called_once()
            guard.check_bump.return_value = (False, "backwards pins")
            self.assertFalse(self.decide())
            guard.check_bump.return_value = (None, "unavailable")
            with self.assertRaisesRegex(RuntimeError, "bump-guard"):
                self.decide()
        self.api.pr["user"]["login"] = "ordinary-author"
        self.assertFalse(self.decide())


class BumpTransportTests(unittest.TestCase):
    def setUp(self):
        from test_bump_manifest import fixture
        pr, mathlib, base = fixture()
        self.pr, self.mathlib, self.base = pr, mathlib, base
        self.calls = []
        self.forward = True
        self.error = None
        self.malformed = False
        self.no_change = False
        self.environment = patch.dict(os.environ, TAUCETI_CONF_SNAPSHOT='', TAUCETI_SOURCE_CANDIDATE='',
                                      TAUCETI_REPO=REPO, GH_READ_TOKEN='', GH_TOKEN='fixture-token',
                                      TAUCETI_GITHUB_WRITE_ROLE='test-bump-read')
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def request(self, method, path):
        self.assertEqual(method, 'GET')
        self.calls.append(path)
        if path.startswith('repos/leanprover-community/'):
            if self.error:
                raise self.error
            if '/compare/' in path:
                if self.malformed:
                    return {}
                return {'status': 'ahead' if self.forward else 'behind'}
            if '/actions/' in path:
                return {'workflow_runs': [{'head_branch': 'master', 'status': 'completed', 'conclusion': 'success'}]}
            content = json.dumps(self.mathlib) if '/lake-manifest.json?' in path else 'leanprover/lean4:v4.31.0\n'
        else:
            if path.endswith('/branches/main'):
                return {'commit': {'sha': 'c'*40}}
            if '/compare/' in path:
                return {'merge_base_commit': {'sha': 'd'*40}}
            filename = path.split('/contents/', 1)[1].split('?', 1)[0]
            if filename == 'lakefile.lean':
                raise route.APIError(method, path, 404, {})
            manifest = self.pr if path.endswith(HEAD) and not self.no_change else self.base
            content = {'lakefile.toml': 'name = "TauCeti"\n', 'lake-manifest.json': json.dumps(manifest),
                       'lean-toolchain': 'leanprover/lean4:v4.31.0\n'}[filename]
        return {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(content.encode()).decode()}

    def check(self, api=None):
        return route.check_bump(api or self, REPO, HEAD)

    def upstream(self):
        return [path for path in self.calls if path.startswith('repos/leanprover-community/')]

    def test_real_guard_uses_each_structured_observation_once_and_keeps_its_verdict(self):
        passed, output = self.check()
        self.assertTrue(passed, output)
        self.assertIn('forward-only bump validated', output)
        self.assertEqual(len(self.upstream()), 5)
        self.assertEqual(len(set(self.upstream())), 5)

    def test_actual_backward_bump_still_fails_without_later_reads(self):
        self.forward = False
        passed, output = self.check()
        self.assertFalse(passed)
        self.assertIn('not a forward move', output)
        self.assertEqual(len(self.upstream()), 1)

    def test_no_pin_change_does_not_read_upstream(self):
        self.no_change = True
        passed, output = self.check()
        self.assertTrue(passed, output)
        self.assertEqual(self.upstream(), [])

    def test_unreadable_upstream_is_unknown_rather_than_a_failed_guard(self):
        self.error = route.APIError('GET', 'repos/upstream/compare', 403, {'message': 'not authorized'})
        passed, output = self.check()
        self.assertIsNone(passed)
        self.assertIn('inputs unavailable', output)

    def test_caller_transport_deferral_propagates_instead_of_unknown(self):
        class CallerDeferred(RuntimeError):
            def __init__(self):
                super().__init__('read deferred')
                self.retry_at = 1234.0

        self.error = CallerDeferred()
        with self.assertRaises(CallerDeferred):
            self.check()

        class Early:
            def request(api, method, path):
                raise CallerDeferred()

        with self.assertRaises(CallerDeferred):
            self.check(Early())

    def test_missing_upstream_fields_remain_unknown(self):
        self.malformed = True
        passed, output = self.check()
        self.assertIsNone(passed)
        self.assertIn('inputs unavailable', output)

    def test_real_guard_cached_quota_refusal_updates_shared_hold_and_stops_upstream_dispatch(self):
        self.environment.stop()
        self.environment = patch.dict(os.environ, TAUCETI_CONF_SNAPSHOT='', TAUCETI_SOURCE_CANDIDATE='',
                                      TAUCETI_REPO=REPO, GH_READ_TOKEN='fixture-read-token',
                                      GH_TOKEN='fixture-write-token', TAUCETI_GITHUB_READ_ROLE='test-bump-read')
        self.environment.start()
        self.addCleanup(self.environment.stop)
        client = route.GitHub(max_wait=0)
        real_run = subprocess.run
        upstream = []

        def transport(argv, *args, **kwargs):
            if argv[:2] != ['gh', 'api']:
                return real_run(argv, *args, **kwargs)
            self.assertEqual(kwargs['env']['GH_TOKEN'], 'fixture-read-token')
            path = argv[argv.index('--method')+2]
            if path.startswith('repos/leanprover-community/'):
                upstream.append(path)
                return subprocess.CompletedProcess(argv, 1, '',
                    'gh: API rate limit already exceeded for installation ID 143500674.')
            try:
                body, status = self.request('GET', path), 200
            except route.APIError as error:
                body, status = error.data, error.status
            import time
            headers = ('HTTP/2 '+str(status)+'\nX-RateLimit-Resource: core\n'
                       'X-RateLimit-Remaining: 100\nX-RateLimit-Reset: '+str(int(time.time())+3600)+'\n\n')
            return subprocess.CompletedProcess(argv, 0, headers+json.dumps(body), '')

        with patch('github_api.subprocess.run', side_effect=transport):
            with self.assertRaises(route.Deferred):
                self.check(client)
            self.assertEqual(len(upstream), 1)
            self.assertIn(('test-bump-read', 'core'), client.quota_state.blocked)
            other = route.GitHub(max_wait=0, quota_state=client.quota_state)
            with self.assertRaises(route.Deferred):
                other.request('GET', 'repos/other/repo/pulls/1')
            self.assertEqual(other.stats, {})
        self.assertEqual(client.stats['test-bump-read']['resources']['core']['requests'], len(self.calls)+1)


if __name__ == "__main__":
    unittest.main()
