"""Persistent declarations use inline author/scope eligibility, with no Actions receipts."""

import base64
import copy
import importlib.util
import os
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


if __name__ == "__main__":
    unittest.main()
