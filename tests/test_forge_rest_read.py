import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from tauceti_tools.forge import Forge, ForgeError, Incomplete, RepositoryRef, Unsupported
from tauceti_tools.forge_api import execute, plan
from tauceti_tools.forge_cli import run
from tauceti_tools.forge_command import parse_command


class SourceRestReads(unittest.TestCase):
    def setUp(self):
        self.forge = Forge("forgejo", "https://git.example.org")
        self.repo = RepositoryRef(self.forge, "one/project")
        self.api = Mock(forge=self.forge, max_pages=10, max_bytes=1024 * 1024)
        self.user = {"id": 4, "login": "operator"}
        self.api.current_user.return_value = self.user
        self.pull = {
            "id": 5, "number": 12, "html_url": self.repo.pull_url(12), "title": "Proof", "body": "Proposal",
            "created_at": "2026-11-07T00:00:00Z", "updated_at": "2026-11-07T00:00:00Z",
            "user": self.user, "labels": [{"id": 4, "name": "ready"}], "assignees": [self.user],
            "state": "open", "merged": False, "draft": False, "mergeable": True, "is_locked": False, "flow": 0,
            "head": {"sha": "a" * 40, "ref": "refs/pull/12/head", "label": "proof", "repo": {"id": 3, "full_name": self.repo.name}},
            "base": {"sha": "b" * 40, "ref": "main", "label": "main", "repo": {"id": 3, "full_name": self.repo.name}},
            "changed_files": 1, "additions": 1, "deletions": 0, "merge_commit_sha": None,
            "merged_at": None, "closed_at": None, "merged_by": None, "requested_reviewers": None,
        }
        self.api.pull.return_value = self.pull
        self.api.pull_ref.return_value = "a" * 40
        self.issue = {**self.pull, "html_url": self.repo.url + "/issues/12", "pull_request": None}
        self.api.issue.return_value = self.issue
        self.comment = {"id": 7, "body": "Finding", "user": self.user,
                        "created_at": "2026-11-07T00:00:00Z", "updated_at": "2026-11-07T00:00:00Z",
                        "html_url": self.repo.pull_url(12) + "#issuecomment-7"}
        self.api.comments.return_value = [self.comment]
        self.review = {"id": 8, "body": "Checked", "user": self.user, "commit_id": "a" * 40,
                       "state": "APPROVED", "dismissed": False, "official": True, "stale": False,
                       "submitted_at": "2026-11-07T00:00:00Z", "updated_at": "2026-11-07T00:00:00Z",
                       "html_url": self.repo.pull_url(12) + "#issuecomment-8"}
        self.api.reviews.return_value = [self.review]

    def request(self, suffix, *options):
        return plan(parse_command(["api", "repos/one/project/" + suffix, *options]))

    def read(self, suffix, *options):
        output, diagnostic, status = execute(self.request(suffix, *options), self.api)
        self.assertEqual((diagnostic, status), ("", 0))
        return json.loads(output)

    def test_pull_rest_fields_keep_verified_head_and_native_numeric_ids(self):
        before = copy.deepcopy(self.pull)
        row = self.read("pulls/12")
        self.assertEqual(row["id"], 5)
        self.assertEqual(row["node_id"], self.forge.key + "/pulls/5")
        self.assertEqual(row["head"]["sha"], "a" * 40)
        self.assertEqual(row["head"]["ref"], "proof")
        self.assertEqual(row["head"]["forgejo_ref"], "refs/pull/12/head")
        self.assertEqual(row["requested_reviewers"], [])
        self.assertEqual(row["changed_files"], 1)
        self.assertFalse(row["locked"])
        self.assertNotIn("is_locked", row)
        self.assertEqual(row["labels"][0]["node_id"], self.forge.key + "/labels/4")
        self.assertEqual(row["assignees"][0]["node_id"], self.forge.key + "/users/4")
        self.assertEqual(row["head"]["repo"]["node_id"], self.forge.key + "/repositories/3")
        self.assertNotIn("author_association", row)
        self.assertEqual(self.pull, before)

    def test_source_pinned_request_shapes_and_required_fields(self):
        cases = json.loads((Path(__file__).parent / "fixtures/forge_source_rest_commands.json").read_text())["cases"]
        for case in cases:
            with self.subTest(case=case["id"]):
                request = plan(parse_command(case["argv"]))
                self.assertEqual(request.kind, case["kind"])
                # Inspect the adapter's JSON before the existing jq renderer.
                # Projection syntax is still carried by the accepted plan.
                output, _, _ = execute(replace(request, options={k: v for k, v in request.options.items() if k != "jq"}), self.api)
                row = json.loads(output)
                if isinstance(row, list):
                    row = row[0]
                self.assertTrue(set(case["fields"]) <= row.keys())
                self.assertTrue(set(case.get("unavailable_fields", [])).isdisjoint(row))

    def test_closed_moved_branch_cannot_leak_current_head_or_diff_stats(self):
        self.pull["state"] = "closed"
        self.api.pull_ref.return_value = "c" * 40
        with self.assertRaisesRegex(Incomplete, "moved closed-PR"):
            self.read("pulls/12")

    def test_false_mergeability_is_unknown_and_missing_lifecycle_is_unavailable(self):
        self.pull["mergeable"] = False
        self.assertIsNone(self.read("pulls/12")["mergeable"])
        self.pull["merged"] = "false"
        with self.assertRaises(Incomplete):
            self.read("pulls/12")

    def test_closed_retained_head_allows_bound_nonzero_stats(self):
        self.pull["state"] = "closed"
        self.assertEqual(self.read("pulls/12")["head"]["sha"], "a" * 40)
        self.api.pull_ref.assert_called_once_with(self.repo, 12)

    def test_zero_stats_reuse_verified_reconstruction_and_propagate_missing_proof(self):
        self.pull.update(changed_files=0, additions=0, deletions=0)
        with patch("tauceti_tools.forge_rest_read.ForgejoReader._hydrate", side_effect=Incomplete("unbound diff")):
            with self.assertRaisesRegex(Incomplete, "unbound diff"):
                self.read("pulls/12")
        with patch("tauceti_tools.forge_rest_read.ForgejoReader._hydrate", return_value={
            "headRefOid": "a" * 40, "changedFiles": 1, "additions": 2, "deletions": 0,
        }):
            row = self.read("pulls/12")
        self.assertEqual((row["changed_files"], row["additions"]), (1, 2))

    def test_issue_uses_distinct_identity_namespace_and_native_lifecycle(self):
        row = self.read("issues/12")
        self.assertEqual(row["node_id"], self.forge.key + "/issues/5")
        self.assertEqual(row["url"], self.forge.api_url + "/repos/one/project/issues/12")
        self.assertEqual(row["state"], "open")
        self.assertNotIn("pull_request", row)
        self.api.issue.side_effect = Incomplete("not an issue")
        with self.assertRaises(Incomplete):
            self.read("issues/12")

    def test_native_null_label_and_assignee_slices_are_explicit_empty_arrays(self):
        self.pull.update(labels=None, assignees=None)
        row = self.read("pulls/12")
        self.assertEqual((row["labels"], row["assignees"]), ([], []))
        self.pull.pop("assignees")
        with self.assertRaises(Incomplete):
            self.read("pulls/12")

    def test_comment_pages_slice_one_unpaginated_native_history(self):
        self.api.comments.return_value = [{**self.comment, "id": n} for n in range(1, 6)]
        pages = self.read("issues/12/comments?page=2&per_page=2", "--paginate", "--slurp")
        self.assertEqual([[r["id"] for r in page] for page in pages], [[3, 4], [5]])
        self.api.comments.assert_called_once_with(self.repo, 12)
        self.assertEqual(pages[0][0]["node_id"], self.forge.key + "/comments/3")
        self.assertEqual(pages[0][0]["user"]["node_id"], self.forge.key + "/users/4")
        self.assertEqual(pages[0][0]["issue_url"], self.forge.api_url + "/repos/one/project/issues/12")

    def test_default_page_does_not_repeat_the_native_history(self):
        self.api.comments.return_value = [{**self.comment, "id": n} for n in range(1, 6)]
        self.assertEqual([r["id"] for r in self.read("issues/12/comments?page=2&per_page=2")], [3, 4])
        self.assertEqual(self.read("issues/12/comments?page=10&per_page=2"), [])

    def test_inaccessible_history_does_not_become_empty_success(self):
        self.api.comments.side_effect = ForgeError("forbidden", status=403)
        with self.assertRaises(ForgeError):
            self.read("issues/12/comments", "--paginate")

    def test_wrong_origin_repo_or_number_is_unavailable(self):
        for url in ["https://evil.example/one/project/pulls/12", "https://git.example.org/other/project/pulls/12",
                    self.repo.pull_url(13), "https://operator@git.example.org/one/project/pulls/12"]:
            with self.subTest(url=url):
                self.pull["html_url"] = url
                with self.assertRaises(Incomplete):
                    self.read("pulls/12")
                self.comment["html_url"] = url
                with self.assertRaises(Incomplete):
                    self.read("issues/12/comments")

    def test_missing_or_duplicate_discussion_fields_are_errors(self):
        for key in ["id", "user", "body", "created_at", "html_url"]:
            row = {k: v for k, v in self.comment.items() if k != key}
            self.api.comments.return_value = [row]
            with self.subTest(key=key), self.assertRaises(Incomplete):
                self.read("issues/12/comments")
        self.api.comments.return_value = [self.comment, self.comment]
        with self.assertRaisesRegex(Incomplete, "repeated"):
            self.read("issues/12/comments")

    def test_deleted_author_remains_deleted_without_an_invented_type(self):
        self.comment["user"] = None
        self.assertIsNone(self.read("issues/12/comments")[0]["user"])
        self.review["user"] = {"id": -1, "login": "Ghost"}
        actor = self.read("pulls/12/reviews")[0]["user"]
        self.assertEqual(actor, {"id": -1, "login": "Ghost", "node_id": None})

    def test_other_actors_drafts_and_reviewer_requests_never_escape(self):
        self.api.reviews.return_value = [
            {**self.review, "id": 1, "state": "PENDING", "user": {"id": 9, "login": "other"}},
            {**self.review, "id": 2, "state": "REQUEST_REVIEW"},
            {**self.review, "id": 3, "state": "PENDING"},
            {**self.review, "id": 4, "state": "COMMENT", "stale": True},
        ]
        rows = self.read("pulls/12/reviews?per_page=1", "--paginate", "--slurp")
        self.assertEqual([[r["id"] for r in page] for page in rows], [[3], [4]])
        self.assertIsNone(rows[0][0]["submitted_at"])
        self.assertEqual(rows[1][0]["state"], "COMMENTED")
        self.assertTrue(rows[1][0]["stale"])
        self.assertTrue(rows[1][0]["official"])

    def test_review_states_normalize_without_losing_native_authority(self):
        self.review.update(state="REQUEST_CHANGES", official=False, stale=True)
        row = self.read("pulls/12/reviews")[0]
        self.assertEqual(row["state"], "CHANGES_REQUESTED")
        self.assertFalse(row["official"])
        self.assertTrue(row["stale"])
        self.review["dismissed"] = True
        self.assertEqual(self.read("pulls/12/reviews")[0]["state"], "DISMISSED")
        self.review.pop("stale")
        with self.assertRaises(Incomplete):
            self.read("pulls/12/reviews")

    def test_own_web_started_draft_may_have_no_summary_url(self):
        self.review.update(state="PENDING", html_url="")
        self.assertEqual(self.read("pulls/12/reviews")[0]["html_url"], "")
        self.review["state"] = "APPROVED"
        with self.assertRaises(Incomplete):
            self.read("pulls/12/reviews")

    def test_agit_and_missing_or_inconsistent_lifecycle_are_unavailable(self):
        self.pull["flow"] = 1
        with self.assertRaisesRegex(Unsupported, "AGit"):
            self.read("pulls/12")
        self.pull["flow"] = 0
        for key in ("merged_at", "closed_at", "merge_commit_sha", "merged_by", "requested_reviewers"):
            original = self.pull.pop(key)
            with self.subTest(key=key), self.assertRaises(Incomplete):
                self.read("pulls/12")
            self.pull[key] = original
        self.pull["merged_at"] = "2026-11-07T00:00:00Z"
        with self.assertRaises(Incomplete):
            self.read("pulls/12")

    def test_aggregate_byte_and_page_bounds_apply_to_normalized_collections(self):
        self.api.max_bytes = 10
        for suffix in ["pulls/12", "issues/12", "issues/12/comments", "pulls/12/reviews"]:
            with self.subTest(suffix=suffix), self.assertRaises(Incomplete):
                self.read(suffix)
        self.api.max_bytes = 1024 * 1024
        self.api.max_pages = 1
        self.api.comments.return_value = [{**self.comment, "id": n} for n in range(1, 4)]
        with self.assertRaises(Incomplete):
            self.read("issues/12/comments?per_page=1", "--paginate")

    def test_unsupported_paths_headers_and_queries_reject_before_io(self):
        for suffix, options in [
            ("pulls/0", []), ("pulls/12/files", []), ("pulls/12/comments", []),
            ("pulls/12/reviews/8", []), ("issues/12/comments?since=2026-01-01", []),
            ("pulls/12?state=closed", []), ("issues/12/comments", ["--include"]),
            ("pulls/12", ["--include"]), ("issues/12/comments", ["-X", "POST"]),
            ("pulls/12/reviews", ["-H", "Accept: application/vnd.github.raw"]),
        ]:
            with self.subTest(suffix=suffix, options=options), self.assertRaises(Unsupported):
                self.request(suffix, *options)
        self.api.request.assert_not_called()

    def test_cli_rejects_unsupported_source_forms_before_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "project.toml"
            config.write_text('''schema_version=2
[forges.source]
kind="forgejo"
url="https://git.example.org"
[repositories]
content={forge="source",name="one/project"}
roadmap={forge="source",name="one/roadmap"}
''')
            for args in [["repos/one/project/pulls/12", "--include"], ["repos/one/project/issues/12/comments?since=2026-01-01"],
                         ["repos/one/project/pulls/12/reviews", "-X", "POST"]]:
                with (self.subTest(args=args), patch("tauceti_tools.forge_cli.Forgejo") as client,
                      patch("tauceti_tools.forge_cli._delegate") as delegate, self.assertRaises(Unsupported)):
                    run(["--conf", str(config), "api", "--hostname", "git.example.org", *args], environment={
                        "TAUCETI_FORGEJO_TOKEN_FILE": str(Path(root) / "must-not-open-token"),
                        "TAUCETI_FORGEJO_ORIGIN": "https://git.example.org",
                    })
                client.assert_not_called()
                delegate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
