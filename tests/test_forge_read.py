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
            "id": 21,
            "number": 3,
            "title": "Proof",
            "state": "open",
            "merged": False,
            "draft": False,
            "head": {"sha": "a" * 40, "ref": "proof"},
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
        self.api.files.return_value = [{"filename": "Proof.lean", "additions": 1, "deletions": 0}]
        value = self.reader.pull(self.repo, 3, ["files"])
        self.assertEqual(value["files"], [{"path": "Proof.lean", "additions": 1, "deletions": 0}])
        del self.api.files.return_value[0]["deletions"]
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["files"])

    def test_file_truncation_and_head_changes_are_not_complete_observations(self):
        self.api.pull.return_value["changed_files"] = 2
        self.api.files.return_value = [{"filename": "Proof.lean", "additions": 1, "deletions": 0}]
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["files"])
        self.api.pull.return_value["changed_files"] = 1
        before = self.api.pull.return_value
        self.api.pull.side_effect = [before, {**before, "head": {"sha": "c" * 40}}]
        with self.assertRaises(Incomplete):
            self.reader.pull(self.repo, 3, ["files"])

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
