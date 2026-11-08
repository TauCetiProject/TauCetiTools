"""Manual labels replace only PR compilation, with unchanged staging checks."""

import copy
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_tools import self_tested as policy

SCRIPTS = Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("local_route", SCRIPTS / "self_tested.py")
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)


class API:
    def __init__(self):
        self.pr = {
            "number": 7,
            "state": "open",
            "head": {"sha": "a" * 40, "ref": "math"},
            "base": {"ref": "main"},
            "user": {"login": "author"},
            "labels": [{"name": "self-tested"}],
            "changed_files": 1,
            "additions": 3,
            "deletions": 2,
            "html_url": "https://github.com/o/r/pull/7",
        }
        self.statuses = []
        self.files = [{"filename": "TauCeti/A.lean", "status": "modified", "additions": 3}]
        self.member = True
        self.writes = []
        self.move = False
        self.reads = 0
        self.runs = []

    def request(self, method, path, data=None, **kwargs):
        if method != "GET":
            self.writes.append((method, path, data))
            return {}
        if "/runs?" in path:
            return {"workflow_runs": self.runs}
        if "/memberships/" in path:
            return {"state": "active" if self.member else "pending"}
        if "/status?" in path:
            return {"statuses": self.statuses}
        self.reads += 1
        pr = copy.deepcopy(self.pr)
        if self.move and self.reads > 1:
            pr["head"]["sha"] = "b" * 40
        return pr

    def items(self, path):
        return [] if "/events?" in path else self.files


class SelfTested(unittest.TestCase):
    def setUp(self):
        staging = patch.object(route, "in_staging", return_value=False)
        self.staging = staging.start()
        self.addCleanup(staging.stop)
        self.api = API()
        self.event = {"action": "labeled", "label": {"name": "self-tested"}, "pull_request": self.api.pr}
        self.env = patch.dict(
            os.environ,
            TAUCETI_SELF_TESTED_TEAM="org/trusted",
            GITHUB_RUN_ID="42",
            SELF_TESTED_MERGE_BACKEND="bors",
            TAUCETI_TEAM_TOKEN="",
            SELF_TESTED_TEAM_TOKEN="",
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def decide(self, **kwargs):
        return route.decide(self.api, "org/repo", 7, self.event, **kwargs)

    def test_allowed_author_gets_explicit_skip_without_a_build_success(self):
        self.assertTrue(self.decide())
        contexts = {data["context"] for _, _, data in self.api.writes}
        self.assertEqual(contexts, {"self-tested/7", "scope", "bump-guard"})
        self.assertEqual(self.api.writes[0][2]["description"], policy.DESCRIPTION)

    def test_accepted_staging_membership_survives_label_removal(self):
        self.api.statuses = [{"context": "self-tested/7", "state": "success", "id": 10}]
        self.api.pr["labels"] = []
        self.event = {"action": "unlabeled", "label": {"name": "self-tested"}}
        self.staging.return_value = True
        self.assertTrue(self.decide())
        self.assertEqual(self.api.writes, [])

    def test_nonmember_infrastructure_and_full_lint_use_ordinary_ci(self):
        self.api.member = False
        self.assertFalse(self.decide())
        self.assertEqual([w[0] for w in self.api.writes], ["DELETE"])
        self.api.member = True
        self.api.files[0]["filename"] = "lakefile.toml"
        self.assertFalse(self.decide())
        self.api.files[0]["filename"] = "TauCeti/A.lean"
        self.api.pr["labels"].append({"name": "full-lint"})
        self.assertFalse(self.decide())

    def test_push_without_relabel_clears_assertion_and_reconciliation_requests_ci(self):
        self.event = {"action": "synchronize", "pull_request": self.api.pr}
        self.assertFalse(self.decide(reconcile=True))
        self.assertIn("/dispatches", self.api.writes[0][1])
        self.assertEqual(self.api.writes[1][0], "DELETE")

    def test_late_push_event_keeps_a_new_head_assertion_and_main_advance_is_irrelevant(self):
        self.api.statuses = [{"context": "self-tested/7", "state": "success", "id": 10}]
        self.event = {"action": "synchronize", "pull_request": self.api.pr}
        self.assertTrue(self.decide())
        self.assertEqual({w[2]["context"] for w in self.api.writes}, {"scope", "bump-guard"})

    def test_label_for_an_old_head_cannot_assert_the_new_head(self):
        self.event = copy.deepcopy(self.event)
        self.event["pull_request"]["head"]["sha"] = "b" * 40
        self.assertFalse(self.decide())
        self.assertEqual(self.api.writes[0][0], "DELETE")

    def test_membership_revocation_returns_to_ordinary_ci(self):
        self.api.statuses = [{"context": "self-tested/7", "state": "success", "id": 10}]
        self.api.member = False
        self.assertFalse(self.decide(reconcile=True))
        self.assertIn("/dispatches", self.api.writes[0][1])
        self.assertEqual(self.api.writes[1][2]["state"], "failure")

    def test_unavailable_membership_returns_to_ordinary_ci(self):
        self.api.statuses = [{"context": "self-tested/7", "state": "success", "id": 10}]
        original = self.api.request

        def unavailable(method, path, *args, **kwargs):
            if "/memberships/" in path:
                raise RuntimeError("membership unavailable")
            return original(method, path, *args, **kwargs)

        self.api.request = unavailable
        self.assertFalse(self.decide(reconcile=True))
        self.assertIn("/dispatches", self.api.writes[0][1])

    def test_head_change_during_read_does_not_publish(self):
        self.api.move = True
        self.assertFalse(self.decide())
        self.assertEqual(self.api.writes, [])

    def test_renaming_human_owned_files_into_library_remains_ineligible(self):
        self.api.files[0].update(status="renamed", previous_filename="scripts/tool.lean")
        self.assertFalse(self.decide())
        self.assertEqual([w[0] for w in self.api.writes], ["DELETE"])

    def test_partial_diff_and_size_guards_still_use_ordinary_ci(self):
        self.api.pr["changed_files"] = 2
        self.assertFalse(self.decide())
        self.api.pr["changed_files"] = 1
        self.api.pr["additions"] = 2001
        self.assertFalse(self.decide())

    def test_fresh_label_reconciliation_waits_for_picker_and_dispatch_failure_keeps_retry_evidence(self):
        self.event = {}
        with patch.object(route, "recent_label", return_value=True):
            self.assertFalse(self.decide(reconcile=True))
        self.assertEqual(self.api.writes, [])
        original = self.api.request

        def rejected(method, path, *args, **kwargs):
            if "/dispatches" in path:
                raise RuntimeError("dispatch unavailable")
            return original(method, path, *args, **kwargs)

        self.api.request = rejected
        with patch.object(route, "recent_label", return_value=False), self.assertRaises(RuntimeError):
            self.decide(reconcile=True)
        self.assertEqual(self.api.writes, [])

    def test_queued_picker_is_not_limited_by_the_five_minute_grace(self):
        self.event = {}
        self.api.runs = [{"pull_requests": [{"number": 7}]}]
        with patch.object(route, "recent_label", return_value=False):
            self.assertFalse(self.decide(reconcile=True))
        self.assertEqual(self.api.writes, [])

    def test_stale_label_with_existing_build_does_not_dispatch_again(self):
        self.event = {}
        self.api.statuses = [{"context": "build", "state": "failure", "id": 10}]
        self.assertFalse(self.decide(reconcile=True))
        self.assertEqual([w[0] for w in self.api.writes], ["DELETE"])

    def test_unrelated_label_change_during_read_does_not_lose_the_assertion(self):
        original = self.api.request

        def relabel(method, path, *args, **kwargs):
            result = original(method, path, *args, **kwargs)
            if method == "GET" and "/pulls/" in path and self.api.reads > 1:
                result["labels"].append({"name": "awaiting-review"})
            return result

        self.api.request = relabel
        self.assertTrue(self.decide())

    def test_observer_needs_current_head_label_and_the_pr_bound_policy_status(self):
        self.api.statuses = [{"context": "self-tested/7", "state": "success", "id": 10}]

        def read(path):
            return self.api.request("GET", path)

        self.assertTrue(policy.observe("org/repo", 7, "a" * 40, read))
        self.api.statuses[0]["context"] = "self-tested/8"
        self.assertFalse(policy.observe("org/repo", 7, "a" * 40, read))
        self.api.statuses[0]["context"] = "self-tested/7"
        self.assertFalse(policy.observe("org/repo", 7, "b" * 40, read))


if __name__ == "__main__":
    unittest.main()
