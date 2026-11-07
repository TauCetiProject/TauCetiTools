import unittest
from unittest.mock import Mock

from tauceti_tools.forge import AmbiguousWrite, Forge, ForgeError, Incomplete, RepositoryRef, Unsupported
from tauceti_tools.forge_command import parse_command
from tauceti_tools.forge_write import ForgejoWriter


class Parsing(unittest.TestCase):
    def test_payload_flags_are_not_routing_flags(self):
        for payload in ["-R", "--repo=github.com/attacker/repo", "--conf=other", "--approve", "--json"]:
            for option in ["--body", "-b"]:
                with self.subTest(payload=payload, option=option):
                    cmd = parse_command(["pr", "comment", "12", option, payload, "-R", "git.example/one/project"])
                    self.assertEqual(cmd.options, {"body": payload, "repo": "git.example/one/project"})

    def test_short_values_and_boolean_values(self):
        cmd = parse_command(["-Rgit.example/one/project", "repo", "fork", "--clone=false", "--remote=false"])
        self.assertFalse(cmd.options["clone"])
        self.assertFalse(cmd.options["remote"])
        cmd = parse_command(["pr", "create", "-tTitle", "-b--repo=literal", "-Hproof", "-d"])
        self.assertEqual(cmd.options["body"], "--repo=literal")
        self.assertTrue(cmd.options["draft"])

    def test_review_action_and_body_are_distinct(self):
        cmd = parse_command(["pr", "review", "12", "-c", "-b", "--approve"])
        self.assertTrue(cmd.options["comment"])
        self.assertNotIn("approve", cmd.options)
        self.assertEqual(cmd.options["body"], "--approve")

    def test_invalid_input_is_rejected_before_execution(self):
        for args in [
            ["pr", "merge", "12"],
            ["pr", "create", "-t", "Title"],
            ["pr", "review", "12", "--approve", "--comment"],
            ["pr", "comment", "1_2", "-b", "text"],
            ["issue", "edit", "12", "-b", "text", "-F", "file"],
            ["issue", "list", "--label", "bug,"],
            ["repo", "fork", "--clone=true"],
            ["label", "list", "--limit", "0"],
            ["pr", "create", "-R", "a/b", "-R", "other/repo", "-t", "Title", "-H", "branch"],
            ["--json", "number", "pr", "comment", "12", "-b", "text"],
        ]:
            with self.subTest(args=args), self.assertRaises(Unsupported):
                parse_command(args)


class Writes(unittest.TestCase):
    def setUp(self):
        self.repo = RepositoryRef(Forge("forgejo", "https://git.example.org"), "one/project")
        self.api = Mock()
        self.api._repo.return_value = "repos/one/project"
        self.api.current_user.return_value = {"id": 2, "login": "operator"}
        self.api.pull.return_value = {"number": 12, "state": "open", "head": {"sha": "a" * 40}}
        self.api.collection.return_value = []
        self.api.reviews.return_value = []
        self.api.pull_ref.return_value = "a" * 40
        self.api.repository.return_value = {"permissions": {"push": True}}
        self.writer = ForgejoWriter(self.api, expected_user_id="2")

    def test_wrong_account_never_mutates(self):
        self.api.current_user.return_value["id"] = 3
        with self.assertRaises(ForgeError):
            self.writer.comment(self.repo, 12, "body")
        self.api.request.assert_not_called()

    def test_review_binds_to_synchronized_immutable_head(self):
        self.api.request.return_value = {"commit_id": "a" * 40}
        self.writer.review(self.repo, 12, event="APPROVED")
        self.api.request.assert_called_once_with(
            "POST",
            "repos/one/project/pulls/12/reviews",
            {
                "event": "APPROVED",
                "body": "",
                "commit_id": "a" * 40,
            },
        )

    def test_unsynchronized_head_prevents_review(self):
        self.api.pull_ref.return_value = "b" * 40
        with self.assertRaises(Incomplete):
            self.writer.review(self.repo, 12, event="APPROVED")
        self.api.request.assert_not_called()

    def test_unknown_review_response_is_ambiguous_not_retried(self):
        self.api.request.return_value = {}
        with self.assertRaises(AmbiguousWrite):
            self.writer.review(self.repo, 12, event="APPROVED")
        self.assertEqual(self.api.request.call_count, 1)

    def test_all_label_names_are_checked_before_any_edit(self):
        self.api.collection.return_value = [{"id": 3, "name": "ready"}]
        with self.assertRaises(ForgeError):
            self.writer.edit(self.repo, 12, pull=True, changes={"body": "new"}, add_labels=["missing"])
        self.api.request.assert_not_called()

    def test_label_edits_are_additive_and_partial_failure_requires_reconciliation(self):
        self.api.collection.return_value = [{"id": 3, "name": "ready"}]
        self.api.request.side_effect = [{"number": 12}, ForgeError("forbidden", status=403)]
        with self.assertRaises(AmbiguousWrite):
            self.writer.edit(self.repo, 12, pull=True, changes={"body": "new"}, add_labels=["ready"])
        self.assertEqual(self.api.request.call_args.args, ("PATCH", "repos/one/project/pulls/12", {"body": "new"}))
        self.assertEqual(self.api.request.call_count, 2)

    def test_already_closed_is_a_noop_even_with_comment(self):
        self.api.pull.return_value["state"] = "closed"
        self.writer.lifecycle(self.repo, 12, pull=True, state="closed", comment="close finding")
        self.api.request.assert_not_called()

    def test_merged_pr_cannot_be_reopened_or_commented_as_part_of_reopen(self):
        self.api.pull.return_value.update(state="closed", merged=True)
        with self.assertRaises(ForgeError):
            self.writer.lifecycle(self.repo, 12, pull=True, state="open", comment="reopen finding")
        self.api.request.assert_not_called()

    def test_wip_creation_verifies_native_draft_state(self):
        self.api.request.return_value = {"number": 12}
        self.api.pull.return_value["draft"] = True
        self.assertEqual(
            self.writer.create(self.repo, pull=True, title="Proof", head="proof", base="main", draft=True), 12
        )
        self.assertEqual(self.api.request.call_args.args[2]["title"], "WIP: Proof")

    def test_unsupported_wip_prefix_is_an_ambiguous_creation_outcome(self):
        self.api.request.return_value = {"number": 12}
        self.api.pull.return_value["draft"] = False
        with self.assertRaises(AmbiguousWrite):
            self.writer.create(self.repo, pull=True, title="Proof", head="proof", base="main", draft=True)
        self.assertEqual(self.api.request.call_count, 1)

    def test_review_does_not_publish_an_existing_pending_draft(self):
        self.api.reviews.return_value = [{"user": None}, {"state": "PENDING", "user": {"id": 2}}]
        with self.assertRaises(Unsupported):
            self.writer.review(self.repo, 12, event="APPROVED")
        self.api.request.assert_not_called()

    def test_repo_org_case_variants_cannot_remove_the_wrong_label(self):
        self.api.collection.side_effect = lambda path: (
            [{"id": 3, "name": "Ready"}] if path.startswith("repos/") else [{"id": 4, "name": "ready"}]
        )
        with self.assertRaises(Incomplete):
            self.writer.edit(self.repo, 12, pull=True, remove_labels=["ready"])
        self.api.request.assert_not_called()

    def test_invalid_base_is_checked_before_any_label_mutation(self):
        self.api.request.side_effect = ForgeError("missing branch", status=404)
        with self.assertRaises(ForgeError):
            self.writer.edit(self.repo, 12, pull=True, changes={"base": "missing/branch"}, add_labels=["ready"])
        self.api.request.assert_called_once_with("GET", "repos/one/project/branches/missing%2Fbranch")
        self.api.collection.assert_not_called()

    def test_repo_labels_work_without_organization_scope(self):
        self.api.collection.side_effect = lambda path: (
            [{"id": 3, "name": "ready"}]
            if path.startswith("repos/")
            else (_ for _ in ()).throw(ForgeError("scope", status=403))
        )
        self.assertEqual(self.writer.label_ids(self.repo, ["ready"]), [3])
        self.api.collection.assert_called_once_with("repos/one/project/labels")
        with self.assertRaisesRegex(ForgeError, "organization read scope"):
            self.writer.label_ids(self.repo, ["inherited"])
        self.api.collection.side_effect = lambda path: (
            [] if path.startswith("repos/") else (_ for _ in ()).throw(ForgeError("not org", status=404))
        )
        with self.assertRaisesRegex(ForgeError, "does not exist"):
            self.writer.label_ids(self.repo, ["missing"])

    def test_issue_edit_rejects_a_pr_before_writing(self):
        self.api.issue.side_effect = Incomplete("not an issue")
        with self.assertRaises(Incomplete):
            self.writer.edit(self.repo, 12, pull=False, changes={"title": "Ready"})
        self.api.request.assert_not_called()

    def test_custom_prefix_draft_transition_is_an_ambiguous_edit(self):
        self.api.pull.side_effect = [{"title": "Old", "draft": False}, {"title": "Draft: New", "draft": True}]
        with self.assertRaises(AmbiguousWrite):
            self.writer.edit(self.repo, 12, pull=True, changes={"title": "Draft: New"})
        self.api.request.assert_called_once()

    def test_label_create_rejects_duplicate_and_rename_collision(self):
        self.api.collection.return_value = [{"id": 3, "name": "ready"}, {"id": 4, "name": "other"}]
        for kwargs in [{}, {"edit": True, "rename": "other"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ForgeError):
                self.writer.label(self.repo, "ready", **kwargs)
        self.api.request.assert_not_called()

    def test_unrelated_duplicate_labels_do_not_block_requested_label(self):
        self.api.collection.return_value = [
            {"id": 1, "name": "dup"},
            {"id": 2, "name": "dup"},
            {"id": 3, "name": "Ready"},
            {"id": 4, "name": "ready"},
        ]
        self.assertEqual(self.writer.label_ids(self.repo, ["Ready"]), [3])
        with self.assertRaises(Incomplete):
            self.writer.label_ids(self.repo, ["READY"])

    def test_numeric_label_name_cannot_redirect_removal(self):
        self.api.collection.return_value = [{"id": 3, "name": "ready"}, {"id": 4, "name": "3"}]
        self.writer.edit(self.repo, 12, pull=True, remove_labels=["ready"])
        self.api.request.assert_called_once_with("DELETE", "repos/one/project/issues/12/labels/ready", None)

    def test_denied_label_edit_does_not_partially_edit_body(self):
        self.api.collection.return_value = [{"id": 3, "name": "ready"}]
        self.api.request.side_effect = ForgeError("forbidden", status=403)
        with self.assertRaises(ForgeError):
            self.writer.edit(self.repo, 12, pull=True, changes={"body": "new"}, add_labels=["ready"])
        self.api.request.assert_called_once_with("POST", "repos/one/project/issues/12/labels", {"labels": [3]})

    def test_draft_title_edit_preserves_state_and_ready_title_cannot_draft(self):
        self.api.pull.return_value.update(draft=True, title="[wip] Old")
        self.writer.edit(self.repo, 12, pull=True, changes={"title": "New"})
        self.assertEqual(self.api.request.call_args.args[2], {"title": "[wip] New"})
        self.api.request.reset_mock()
        self.api.pull.return_value.update(draft=False, title="Old")
        with self.assertRaises(Unsupported):
            self.writer.edit(self.repo, 12, pull=True, changes={"title": "WIP: New"})
        self.api.request.assert_not_called()

    def test_draft_edit_preserves_the_prefix_known_to_be_enabled(self):
        self.api.pull.return_value.update(draft=True, title="WIP: Old")
        self.writer.edit(self.repo, 12, pull=True, changes={"title": "[WIP] Literal title"})
        self.assertEqual(self.api.request.call_args.args[2], {"title": "WIP: [WIP] Literal title"})

    def test_wrong_type_pr_comments_and_label_edits_do_not_mutate_issues(self):
        self.api.pull.side_effect = ForgeError("not a pull request", status=404)
        for operation in [
            lambda: self.writer.comment(self.repo, 12, "body", pull=True),
            lambda: self.writer.edit(self.repo, 12, pull=True, add_labels=["ready"]),
        ]:
            with self.assertRaises(ForgeError):
                operation()
        self.api.request.assert_not_called()

    def test_fork_reuse_checks_parent_and_never_overwrites_a_clash(self):
        self.api.repository.return_value = {"fork": True, "parent": {"full_name": self.repo.name}}
        self.assertEqual(self.writer.fork(self.repo).name, "operator/project")
        self.api.request.assert_not_called()
        self.api.repository.return_value = {"fork": False}
        with self.assertRaises(ForgeError):
            self.writer.fork(self.repo)
        self.api.request.assert_not_called()

    def test_ambiguous_post_is_never_retried(self):
        self.api.request.side_effect = AmbiguousWrite("transport failed")
        with self.assertRaises(AmbiguousWrite):
            self.writer.comment(self.repo, 12, "body")
        self.assertEqual(self.api.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
