import unittest
from unittest.mock import Mock

from tauceti_tools.forge import Forge, Incomplete, RepositoryRef, Unsupported
from tauceti_tools.forge_read import ForgejoReader, pull_record


class ReadContracts(unittest.TestCase):
    def setUp(self):
        self.repo = RepositoryRef(Forge("forgejo", "https://git.example.org"), "one/Project")
        self.api = Mock()
        self.reader = ForgejoReader(self.api)
        self.api.pull.return_value = {
            "flow": 0,
            "changed_files": 1,
            "additions": 1,
            "deletions": 0,
            "id": 21,
            "number": 3,
            "title": "Proof",
            "state": "open",
            "merged": False,
            "draft": False,
            "head": {"sha": "a" * 40, "ref": "proof", "label": "proof", "repo": {}},
            "base": {"sha": "b" * 40, "ref": "main"},
            "user": {"id": 1, "login": "operator"},
        }

    def test_mergeability_false_does_not_prove_conflict(self):
        for native, expected in [(False, "UNKNOWN"), (True, "MERGEABLE")]:
            self.api.pull.return_value["mergeable"] = native
            self.assertEqual(self.reader.pull(self.repo, 3, ["mergeable"]), {"mergeable": expected})
        del self.api.pull.return_value["mergeable"]
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["mergeable"])

    def test_expensive_metadata_is_not_fetched_unless_requested(self):
        self.assertEqual(
            self.reader.pull(self.repo, 3, ["number", "baseRefOid"]), {"number": 3, "baseRefOid": "b" * 40}
        )
        self.api.comments.assert_not_called()
        self.api.reviews.assert_not_called()
        self.api.files.assert_not_called()
        self.api.statuses.assert_not_called()

    def test_native_ids_never_collide_between_forges(self):
        first = pull_record(self.api.pull.return_value, self.repo)
        other = RepositoryRef(Forge("forgejo", "https://other.example.org"), self.repo.name)
        second = pull_record(self.api.pull.return_value, other)
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["author"]["id"], second["author"]["id"])
        self.assertNotIn("is_bot", first["author"])
        case_variant = RepositoryRef(self.repo.forge, "ONE/project")
        self.assertEqual(first["id"], pull_record(self.api.pull.return_value, case_variant)["id"])

    def test_explicit_native_null_slices_are_empty_but_missing_fields_are_unknown(self):
        self.api.pull.return_value.update(labels=None, assignees=None)
        self.assertEqual(self.reader.pull(self.repo, 3, ["labels", "assignees"]), {"labels": [], "assignees": []})
        del self.api.pull.return_value["labels"]
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["labels"])

    def test_comments_are_complete_or_unavailable(self):
        self.api.comments.return_value = [
            {
                "id": 7,
                "body": "Finding",
                "html_url": self.repo.url + "/issues/3#issuecomment-7",
                "user": {"id": 1, "login": "reviewer"},
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
            }
        ]
        value = self.reader.pull(self.repo, 3, ["comments"])
        self.assertEqual(value["comments"][0]["body"], "Finding")
        del self.api.comments.return_value[0]["user"]
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["comments"])

    def test_review_states_include_dismissals_but_not_reviewer_requests(self):
        review = {
            "id": 8,
            "state": "APPROVED",
            "dismissed": True,
            "body": "Review",
            "commit_id": "a" * 40,
            "user": {"login": "reviewer"},
            "submitted_at": "2026-01-01T00:00:00Z",
            "html_url": self.repo.pull_url(3),
        }
        self.api.reviews.return_value = [review, {"state": "REQUEST_REVIEW"}]
        value = self.reader.pull(self.repo, 3, ["reviews"])
        self.assertEqual(len(value["reviews"]), 1)
        self.assertEqual(value["reviews"][0]["state"], "DISMISSED")
        review["dismissed"] = False
        review["state"] = "REQUEST_CHANGES"
        self.assertEqual(self.reader.pull(self.repo, 3, ["reviews"])["reviews"][0]["state"], "CHANGES_REQUESTED")
        review["state"] = "UNRECOGNIZED"
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["reviews"])

    def test_file_metadata_does_not_turn_missing_data_into_zero(self):
        self.api.pull.return_value["changed_files"] = 1
        self.api.files.return_value = [
            {
                "filename": "Proof.lean",
                "additions": 1,
                "deletions": 0,
                "contents_url": "https://git.example.org/api/v1/repos/one/Project/contents/Proof.lean?ref=" + "a" * 40,
            }
        ]
        value = self.reader.pull(self.repo, 3, ["files"])
        self.assertEqual(value["files"], [{"path": "Proof.lean", "additions": 1, "deletions": 0}])
        del self.api.files.return_value[0]["deletions"]
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["files"])

    def test_file_truncation_and_head_changes_are_not_complete_observations(self):
        self.api.pull.return_value["changed_files"] = 2
        self.api.files.return_value = [
            {
                "filename": "Proof.lean",
                "additions": 1,
                "deletions": 0,
                "contents_url": "https://git.example.org/api/v1/repos/one/Project/contents/Proof.lean?ref=" + "a" * 40,
            }
        ]
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["files"])
        self.api.pull.return_value["changed_files"] = 1
        before = self.api.pull.return_value
        self.api.pull.side_effect = [before, {**before, "head": {"sha": "c" * 40}}]
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["files"])

    def test_retained_pull_ref_is_not_a_branch_name(self):
        self.api.pull.return_value["head"]["ref"] = "refs/pull/3/head"
        self.assertEqual(self.reader.pull(self.repo, 3, ["headRefName"]), {"headRefName": "proof"})

    def test_file_ref_must_match_head_even_when_branch_tip_is_stable(self):
        self.api.pull.return_value["changed_files"] = 1
        self.api.files.return_value = [
            {
                "filename": "Proof.lean",
                "additions": 1,
                "deletions": 0,
                "contents_url": "https://git.example.org/contents/Proof.lean?ref=" + "b" * 40,
            }
        ]
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["files"])

    def test_system_actor_and_unrequested_bad_metadata_do_not_break_numbers(self):
        for native_id in [-1, -2, -3, 0]:
            self.api.pull.return_value["user"] = {"id": native_id, "login": "system"}
            actor = self.reader.pull(self.repo, 3, ["author"])["author"]
            self.assertNotIn("id", actor)
            self.assertEqual(actor.get("is_bot"), True if native_id == -2 else None)
        self.api.pull.return_value.update(user="bad", labels="bad", assignees="bad")
        self.assertEqual(self.reader.pull(self.repo, 3, ["number"]), {"number": 3})
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["author"])

    def test_unknown_native_git_flow_and_unavailable_deleted_fork_files_stay_unknown(self):
        self.api.pull.return_value.update(changed_files=0, additions=0, deletions=0, flow=1)
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["changedFiles"])
        self.api.pull.return_value.update(flow=0)
        self.api.pull.return_value["head"]["repo"] = None
        self.api.pull_ref.return_value = "a" * 40
        self.api.files.side_effect = Incomplete("native files unavailable")
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["additions"])

    def test_zero_statistics_are_reconstructed_from_retained_head_files(self):
        self.api.pull.return_value.update(changed_files=0, additions=0, deletions=0)
        self.api.pull_ref.return_value = "a" * 40
        self.api.files.return_value = [
            {
                "filename": "Proof.lean",
                "additions": 2,
                "deletions": 1,
                "contents_url": "https://git.example.org/contents/Proof.lean?ref=" + "a" * 40,
            }
        ]
        self.assertEqual(
            self.reader.pull(self.repo, 3, ["changedFiles", "additions", "deletions"]),
            {"changedFiles": 1, "additions": 2, "deletions": 1},
        )
        self.api.files.return_value = []
        self.assertEqual(
            self.reader.pull(self.repo, 3, ["changedFiles", "additions", "deletions"]),
            {"changedFiles": 0, "additions": 0, "deletions": 0},
        )
        self.api.pull_ref.return_value = "b" * 40
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["changedFiles"])

    def test_closed_unmerged_empty_reconstruction_is_not_proof_of_an_empty_proposal(self):
        self.api.pull.return_value.update(state="closed", merged=False, changed_files=0, additions=0, deletions=0)
        self.api.pull.return_value["head"]["repo"] = None
        self.api.pull_ref.return_value = "a" * 40
        self.api.files.return_value = []
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["changedFiles", "files"])

    def test_missing_head_repository_key_is_unavailable_not_deleted(self):
        del self.api.pull.return_value["head"]["repo"]
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["additions"])

    def test_default_closed_pull_output_cannot_publish_a_moved_branch_head(self):
        self.api.pull.return_value.update(state="closed", merged=True)
        self.api.pull_ref.return_value = "b" * 40
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3)

    def test_same_file_count_with_different_line_totals_is_incomplete(self):
        self.api.files.return_value = [
            {
                "filename": "Proof.lean",
                "additions": 0,
                "deletions": 1,
                "contents_url": "https://git.example.org/contents/Proof.lean?ref=" + "a" * 40,
            }
        ]
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["files"])

    def test_closed_unmerged_files_request_the_exact_stored_base(self):
        self.api.pull.return_value.update(state="closed", merged=False, merge_base="c" * 40)
        self.api.pull_ref.return_value = "a" * 40
        self.api.files.return_value = [
            {
                "filename": "Proof.lean",
                "additions": 1,
                "deletions": 0,
                "contents_url": "https://git.example.org/contents/Proof.lean?ref=" + "a" * 40,
            }
        ]
        self.reader.pull(self.repo, 3, ["files"])
        self.api.files.assert_called_once_with(self.repo, 3, comparison_base="c" * 40, revision="a" * 40)

    def test_deleted_fork_uses_verified_retained_head_diff_metadata(self):
        self.api.pull.return_value.update(state="closed", merged=True)
        self.api.pull.return_value["head"]["repo"] = None
        self.api.pull_ref.return_value = "a" * 40
        self.api.files.return_value = [
            {
                "filename": "Proof.lean",
                "additions": 1,
                "deletions": 0,
                "contents_url": "https://git.example.org/api/v1/repos/one/Project/contents/Proof.lean?ref=" + "a" * 40,
            }
        ]
        self.assertEqual(
            self.reader.pull(self.repo, 3, ["files", "additions"]),
            {"files": [{"path": "Proof.lean", "additions": 1, "deletions": 0}], "additions": 1},
        )
        self.api.pull_ref.return_value = "b" * 40
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["files"])

    def test_closed_branch_statistics_must_describe_the_retained_pull_head(self):
        self.api.pull.return_value.update(state="closed", merged=True, changed_files=9, additions=12, deletions=1)
        self.api.pull_ref.return_value = "b" * 40
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["changedFiles", "additions", "deletions"])
        for field in ["headRefOid", "files", "statusCheckRollup"]:
            with self.subTest(field=field), self.assertRaises(Incomplete):
                self.reader.pull(self.repo, 3, [field])

    def test_pending_reviews_belong_only_to_the_current_viewer(self):
        base = {
            "id": 8,
            "state": "PENDING",
            "dismissed": False,
            "body": "Draft",
            "commit_id": "a" * 40,
            "user": {"id": 2, "login": "reviewer"},
            "submitted_at": "2026-01-01T00:00:00Z",
            "html_url": self.repo.pull_url(3),
        }
        self.api.reviews.return_value = [base]
        self.api.current_user.return_value = {"id": 1, "login": "operator"}
        self.assertEqual(self.reader.pull(self.repo, 3, ["reviews"]), {"reviews": []})
        self.api.current_user.return_value = {"id": 2, "login": "reviewer"}
        self.assertEqual(self.reader.pull(self.repo, 3, ["reviews"])["reviews"][0]["state"], "PENDING")

    def test_global_ids_survive_repository_rename(self):
        before = pull_record(self.api.pull.return_value, self.repo)
        renamed = RepositoryRef(self.repo.forge, "another/NewName")
        self.assertEqual(before["id"], pull_record(self.api.pull.return_value, renamed)["id"])

    def test_latest_status_per_context_is_bound_to_immutable_head(self):
        def status(native_id, context, state):
            return {
                "id": native_id,
                "context": context,
                "status": state,
                "target_url": "",
                "created_at": "2026-01-01T00:00:00Z",
                "description": "Build",
            }

        self.api.statuses.return_value = [
            status(9, "build", "success"),
            status(7, "lint", "pending"),
            status(1, "build", "failure"),
        ]
        value = self.reader.pull(self.repo, 3, ["statusCheckRollup"])
        self.assertEqual(
            [(s["context"], s["state"]) for s in value["statusCheckRollup"]],
            [("build", "SUCCESS"), ("lint", "PENDING")],
        )
        self.api.statuses.assert_called_once_with(self.repo, "a" * 40)
        self.api.statuses.return_value = [status(10, "build", "warning")]
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["statusCheckRollup"])

    def test_deleted_head_repository_is_explicitly_absent(self):
        self.api.pull.return_value["head"]["repo"] = None
        self.assertEqual(
            self.reader.pull(self.repo, 3, ["headRepository", "headRepositoryOwner"]),
            {"headRepository": None, "headRepositoryOwner": None},
        )
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["isCrossRepository"])

    def test_review_decision_is_not_derived_from_historical_approvals(self):
        with self.assertRaises(Unsupported):
            self.reader.pull(self.repo, 3, ["reviewDecision"])
        self.api.pull.assert_not_called()

    def test_issue_read_does_not_accept_pr_or_missing_metadata(self):
        self.api.issue.return_value = {"number": 3, "state": "open", "pull_request": {"merged": False}}
        with self.assertRaises(Incomplete):
            self.reader.issue(self.repo, 3, ["number"])
        self.api.issue.return_value = {"number": 3}
        with self.assertRaises(Incomplete):
            self.reader.issue(self.repo, 3, ["state"])

    def test_repository_effective_permission_and_absent_parent(self):
        self.api.repository.return_value = {
            "full_name": self.repo.name,
            "default_branch": "main",
            "parent": None,
            "permissions": {"admin": False, "push": True, "pull": True},
        }
        self.assertEqual(
            self.reader.repository(self.repo, ["defaultBranchRef", "parent", "viewerPermission"]),
            {
                "defaultBranchRef": {"name": "main"},
                "parent": None,
                "viewerPermission": "WRITE",
            },
        )
        del self.api.repository.return_value["permissions"]
        with self.assertRaises(Incomplete):
            self.reader.repository(self.repo, ["viewerPermission"])


if __name__ == "__main__":
    unittest.main()
