"""Manual labels replace only PR compilation, with unchanged staging checks."""

import base64
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


BOT = "tauceti-review-bot[bot]"
REPO = "TauCetiProject/TauCeti"


class BumpAPI(API):
    """An automatic Mathlib bump labelled by `labeller`, with Lake files per commit."""

    def __init__(self):
        super().__init__()
        self.pr["head"].update(ref="bump-mathlib/" + "c" * 12, repo={"full_name": REPO})
        self.pr["user"] = {"login": BOT, "type": "Bot"}
        self.pr["changed_files"] = 2
        self.files = [
            {"filename": "lake-manifest.json", "status": "modified", "additions": 2},
            {"filename": "lean-toolchain", "status": "modified", "additions": 1},
        ]
        self.events = [
            {
                "event": "labeled",
                "label": {"name": "self-tested"},
                "actor": {"login": "outsider"},
                "created_at": "2026-11-09T00:00:00Z",
                "id": 1,
            },
            {
                "event": "labeled",
                "label": {"name": "self-tested"},
                "actor": {"login": "worker"},
                "created_at": "2026-11-10T00:00:00Z",
                "id": 2,
            },
        ]
        self.members = {"worker"}
        self.contents = {}
        self.membership_queries = []

    def request(self, method, path, data=None, **kwargs):
        if method == "GET" and "/memberships/" in path:
            login = path.rsplit("/", 1)[1]
            self.membership_queries.append(login)
            return {"state": "active" if login in self.members else "pending"}
        if method == "GET" and path.endswith("/branches/main"):
            return {"commit": {"sha": "m" * 40}}
        if method == "GET" and "/compare/" in path:
            return {"merge_base_commit": {"sha": "f" * 40}}
        if method == "GET" and "/contents/" in path:
            name, ref = path.split("/contents/", 1)[1].split("?ref=")
            if (ref, name) not in self.contents:
                raise route.APIError("GET", path, 404, {"message": "Not Found"})
            text = self.contents[(ref, name)]
            return {"type": "file", "encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        return super().request(method, path, data, **kwargs)

    def items(self, path):
        return self.events if "/events?" in path else self.files


class AutomaticBump(unittest.TestCase):
    def setUp(self):
        staging = patch.object(route, "in_staging", return_value=False)
        self.staging = staging.start()
        self.addCleanup(staging.stop)
        self.guard_patch = patch.object(route, "check_bump", return_value=(True, "BUMP-GUARD: PASS"))
        self.guard = self.guard_patch.start()
        self.addCleanup(self.guard_patch.stop)
        self.api = BumpAPI()
        self.event = {"action": "labeled", "label": {"name": "self-tested"}, "pull_request": self.api.pr}
        env = patch.dict(
            os.environ,
            TAUCETI_SELF_TESTED_TEAM="org/trusted",
            GITHUB_RUN_ID="42",
            SELF_TESTED_MERGE_BACKEND="bors",
            TAUCETI_TEAM_TOKEN="",
            SELF_TESTED_TEAM_TOKEN="",
        )
        env.start()
        self.addCleanup(env.stop)

    def decide(self, **kwargs):
        return route.decide(self.api, REPO, 7, self.event, **kwargs)

    def posted(self):
        return {w[2]["context"]: w[2] for w in self.api.writes if "/statuses/" in w[1]}

    def test_bump_labelled_by_member_gets_the_real_bump_guard(self):
        self.assertTrue(self.decide())
        self.guard.assert_called_once_with(self.api, REPO, "a" * 40)
        self.assertEqual(self.api.membership_queries, ["worker"])
        posted = self.posted()
        self.assertEqual(set(posted), {"bump-guard", "scope", "self-tested/7"})
        self.assertTrue(all(row["state"] == "success" for row in posted.values()))
        self.assertIn("check-bump", posted["bump-guard"]["description"])
        self.assertNotIn("unchanged", posted["bump-guard"]["description"])
        self.assertEqual(posted["scope"]["description"], "self-tested: mathematics plus Lake pins")
        # The guard result is published before the assertion that relies on it.
        self.assertEqual([w[2]["context"] for w in self.api.writes][-1], "self-tested/7")

    def test_bump_with_library_changes_is_eligible(self):
        self.api.files.append({"filename": "TauCeti/A.lean", "status": "modified", "additions": 3})
        self.api.pr["changed_files"] = 3
        self.assertTrue(self.decide())

    def test_bump_labelled_by_nonmember_uses_ordinary_ci(self):
        self.api.members = {"author"}
        self.assertFalse(self.decide())
        self.assertEqual(self.api.membership_queries, ["worker"])
        self.assertEqual([w[0] for w in self.api.writes], ["DELETE"])
        self.guard.assert_not_called()

    def test_unreadable_label_events_use_ordinary_ci(self):
        def unavailable(path):
            raise route.APIError("GET", path, 502, {"message": "unavailable"})

        self.api.items = lambda path: unavailable(path) if "/events?" in path else self.api.files
        self.assertFalse(self.decide())
        self.assertEqual(self.api.membership_queries, [])
        self.assertEqual([w[0] for w in self.api.writes], ["DELETE"])
        self.api.items = lambda path: [] if "/events?" in path else self.api.files
        self.assertFalse(self.decide())

    def test_unreadable_membership_uses_ordinary_ci(self):
        original = self.api.request

        def unavailable(method, path, *args, **kwargs):
            if "/memberships/" in path:
                raise RuntimeError("membership unavailable")
            return original(method, path, *args, **kwargs)

        self.api.request = unavailable
        self.assertFalse(self.decide())
        self.assertEqual([w[0] for w in self.api.writes], ["DELETE"])

    def test_bump_paths_exclude_lakefiles_other_roots_and_pinless_diffs(self):
        for files in (
            [
                {"filename": "lake-manifest.json", "status": "modified"},
                {"filename": "lakefile.toml", "status": "modified"},
            ],
            [{"filename": "lean-toolchain", "status": "modified"}, {"filename": "lakefile.lean", "status": "added"}],
            [
                {"filename": "lean-toolchain", "status": "modified"},
                {"filename": "scripts/tool.py", "status": "modified"},
            ],
            [{"filename": "lean-toolchain", "status": "modified"}, {"filename": "TauCeti.lean", "status": "modified"}],
            [
                {"filename": "TauCeti/A.lean", "status": "modified"},
                {"filename": "TauCeti/B.lean", "status": "modified"},
            ],
            [
                {"filename": "lean-toolchain", "status": "modified"},
                {"filename": "TauCeti/A.lean", "status": "renamed", "previous_filename": "lakefile.lean"},
            ],
        ):
            with self.subTest(files=files):
                self.api.writes.clear()
                self.api.files = files
                self.api.pr["changed_files"] = len(files)
                self.assertFalse(self.decide())
                self.assertEqual([w[0] for w in self.api.writes], ["DELETE"])
        self.guard.assert_not_called()

    def test_bump_has_a_wider_line_budget(self):
        self.api.pr["additions"] = 4585
        self.api.pr["deletions"] = 4276
        self.assertTrue(self.decide())
        self.guard.reset_mock()
        self.api.pr["additions"] = 20001
        self.assertFalse(self.decide())
        self.guard.assert_not_called()

    def test_bump_keeps_the_file_limit(self):
        self.api.files = [{"filename": f"TauCeti/A{i}.lean", "status": "modified"} for i in range(2001)]
        self.api.pr["changed_files"] = 2001
        self.assertFalse(self.decide())
        self.guard.assert_not_called()

    def test_check_bump_failure_posts_no_assertion(self):
        self.guard.return_value = (False, "BUMP-GUARD: FAIL")
        self.assertFalse(self.decide())
        posted = self.posted()
        self.assertEqual(set(posted), {"bump-guard"})
        self.assertEqual(posted["bump-guard"]["state"], "failure")
        self.assertIn("self-tested", posted["bump-guard"]["description"])
        self.assertEqual(self.api.writes[-1][0], "DELETE")

    def test_unreadable_check_bump_inputs_post_no_verdict(self):
        self.guard.return_value = (None, "bump-guard inputs unavailable")
        self.assertFalse(self.decide())
        self.assertEqual([w[0] for w in self.api.writes], ["DELETE"])

    def test_check_bump_failure_withdraws_an_earlier_assertion(self):
        self.api.statuses = [{"context": "self-tested/7", "state": "success", "id": 10}]
        self.guard.return_value = (False, "BUMP-GUARD: FAIL")
        self.assertFalse(self.decide(reconcile=True))
        self.assertEqual(self.posted()["self-tested/7"]["state"], "failure")

    def test_head_change_during_check_bump_does_not_publish(self):
        self.api.move = True
        self.assertFalse(self.decide())
        self.assertEqual(self.api.writes, [])

    def test_bump_branch_needs_the_bot_author_and_the_base_repository(self):
        for change in (
            {"user": {"login": "author", "type": "User"}},
            {"user": {"login": "other-bot[bot]", "type": "Bot"}},
            {"head": {"sha": "a" * 40, "ref": "bump-mathlib/x", "repo": {"full_name": "fork/TauCeti"}}},
        ):
            with self.subTest(change=change):
                self.api.writes.clear()
                saved = copy.deepcopy(self.api.pr)
                self.api.pr.update(copy.deepcopy(change))
                self.event = {"action": "labeled", "label": {"name": "self-tested"}, "pull_request": self.api.pr}
                self.assertFalse(self.decide())
                self.assertEqual([w[0] for w in self.api.writes], ["DELETE"])
                self.api.pr = saved
        self.guard.assert_not_called()

    def test_ordinary_author_on_other_branch_still_uses_author_membership(self):
        self.api.pr["head"]["ref"] = "math"
        self.api.pr["user"] = {"login": "worker"}
        self.api.files = [{"filename": "TauCeti/A.lean", "status": "modified", "additions": 3}]
        self.api.pr["changed_files"] = 1
        self.event["pull_request"] = self.api.pr
        self.assertTrue(self.decide())
        self.assertEqual(self.api.membership_queries, ["worker"])
        self.guard.assert_not_called()
        self.assertEqual(
            self.posted()["bump-guard"]["description"], "self-tested: dependency and toolchain pins unchanged"
        )
        # Lake pins remain excluded from ordinary self-tested PRs.
        self.api.files[0]["filename"] = "lean-toolchain"
        self.assertFalse(self.decide())

    def test_check_bump_reads_only_lake_files_at_main_merge_base_and_head(self):
        self.guard_patch.stop()
        main, merge_base, head = "m" * 40, "f" * 40, "a" * 40
        for sha in (main, merge_base, head):
            self.api.contents[(sha, "lakefile.toml")] = 'name = "TauCeti"\n'
            self.api.contents[(sha, "lean-toolchain")] = f"toolchain-{sha[0]}\n"
        seen = {}

        def run(argv, **kwargs):
            seen.update(argv=argv, env=kwargs["env"])
            seen["files"] = {Path(d).name: sorted(p.name for p in Path(d).iterdir()) for d in argv[2:]}
            seen["toolchain"] = (Path(argv[4]) / "lean-toolchain").read_text()
            return route.subprocess.CompletedProcess(argv, 0, "BUMP-GUARD: PASS", "")

        with patch.dict(os.environ, TAUCETI_TEAM_TOKEN="team-secret"), patch.object(route.subprocess, "run", run):
            self.assertEqual(route.check_bump(self.api, REPO, head), (True, "BUMP-GUARD: PASS"))
        self.assertEqual(Path(seen["argv"][1]), SCRIPTS / "check-bump.sh")
        self.assertEqual([Path(d).name for d in seen["argv"][2:]], ["base", "mergebase", "pr"])
        self.assertEqual(seen["files"]["base"], ["lakefile.toml", "lean-toolchain"])
        self.assertEqual(seen["toolchain"], "toolchain-a\n")
        self.assertNotIn("TAUCETI_TEAM_TOKEN", seen["env"])

    def test_packaged_check_bump_rejects_a_lakefile_moved_on_main(self):
        self.guard_patch.stop()
        main, merge_base, head = "m" * 40, "f" * 40, "a" * 40
        for sha in (merge_base, head):
            self.api.contents[(sha, "lakefile.toml")] = 'name = "TauCeti"\n'
        self.api.contents[(main, "lakefile.toml")] = 'name = "TauCeti"\n# moved\n'
        self.api.contents[(head, "lean-toolchain")] = "leanprover/lean4:v4.99.0\n"
        verdict, output = route.check_bump(self.api, REPO, head)
        self.assertIs(verdict, False)
        self.assertIn("lakefile.toml differs from base", output)

    def test_unreadable_contents_are_not_a_verdict(self):
        self.guard_patch.stop()
        original = self.api.request

        def broken(method, path, *args, **kwargs):
            if "/contents/" in path:
                raise route.APIError("GET", path, 500, {"message": "unavailable"})
            return original(method, path, *args, **kwargs)

        self.api.request = broken
        self.assertIsNone(route.check_bump(self.api, REPO, "a" * 40)[0])


class SharedPredicate(unittest.TestCase):
    def setUp(self):
        self.pr = BumpAPI().pr

    def test_requested_and_accepted_admit_bump_branches(self):
        labels = [{"name": "self-tested"}]
        branch = "bump-mathlib/" + "c" * 12
        self.assertTrue(policy.requested(labels, branch))
        self.assertTrue(policy.accepted(labels, "success", member=True, branch=branch))
        self.assertFalse(policy.accepted(labels, "pending", member=True, branch=branch))
        self.assertFalse(policy.requested(labels + [{"name": "full-lint"}], branch))
        self.assertFalse(policy.requested(labels + [{"name": "lint-repair"}], branch))
        self.assertFalse(policy.requested(labels, "lint-repair/x"))
        self.assertFalse(policy.accepted(labels, "success", member=True, branch="lint-repair/x"))

    def test_automatic_bump_identity(self):
        self.assertTrue(policy.automatic_bump(self.pr, REPO))
        self.assertFalse(policy.automatic_bump(self.pr, "fork/TauCeti"))
        self.assertFalse(policy.automatic_bump(self.pr | {"user": {"login": "author"}}, REPO))
        self.assertFalse(policy.automatic_bump(self.pr | {"head": self.pr["head"] | {"ref": "math"}}, REPO))
        import bump_latest

        self.assertEqual(policy.BUMP_AUTHOR, bump_latest.BOT)

    def test_bump_paths(self):
        self.assertTrue(policy.allowed_bump_paths(["lake-manifest.json", "lean-toolchain", "TauCeti/A.lean"]))
        self.assertTrue(policy.allowed_bump_paths(["lean-toolchain"]))
        self.assertFalse(policy.allowed_bump_paths(["TauCeti/A.lean"]))
        self.assertFalse(policy.allowed_bump_paths(["lake-manifest.json", "lakefile.toml"]))
        self.assertFalse(policy.allowed_bump_paths([]))
        self.assertFalse(policy.allowed_paths(["lean-toolchain"]))

    def test_observer_rejects_bump_branches_outside_the_automatic_bump(self):
        api = BumpAPI()
        api.statuses = [{"context": "self-tested/7", "state": "success", "id": 10}]

        def read(path):
            return api.request("GET", path)

        with patch.dict(os.environ, TAUCETI_SELF_TESTED_TEAM="org/trusted"):
            self.assertTrue(policy.observe(REPO, 7, "a" * 40, read))
            api.pr["user"] = {"login": "author"}
            self.assertFalse(policy.observe(REPO, 7, "a" * 40, read))


if __name__ == "__main__":
    unittest.main()
