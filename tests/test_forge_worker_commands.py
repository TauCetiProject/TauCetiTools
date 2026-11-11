"""Agent read commands preserve native routing, complete history and unknown CI."""

import unittest
from unittest.mock import Mock, patch

from tauceti_tools.forge import Forge, Incomplete, RepositoryRef
from tauceti_tools.forge_command import parse_command
from tauceti_tools.forge_commands import execute, exit_status


class WorkerCommands(unittest.TestCase):
    def setUp(self):
        self.repo = RepositoryRef(Forge("forgejo", "https://git.example.org"), "team/project")
        self.api = Mock()

    def run_command(self, args, read):
        command = parse_command(["pr", *args, "-R", "git.example.org/team/project"])
        with patch("tauceti_tools.forge_commands.ForgejoReader") as cls:
            cls.return_value.pull.side_effect = read
            value, json_output = execute(command, self.repo, self.api)
        return command, value, json_output

    def test_patch_and_names_bind_the_same_comparison_on_readback(self):
        comparison = dict(headRefOid="a" * 40, baseRefOid="b" * 40)
        self.api.pull_diff.return_value = "diff --git a/a b/a\n"
        _, value, json_output = self.run_command(["diff", "12"], [comparison, comparison])
        self.assertEqual(value, self.api.pull_diff.return_value)
        self.assertFalse(json_output)
        self.api.pull_diff.assert_called_once_with(self.repo, 12)
        self.api.pull.return_value = dict(merge_base="b" * 40)
        with patch("tauceti_tools.git_inputs.changed_paths", return_value=["A.lean", "B.lean"]):
            _, names, _ = self.run_command(["diff", "12", "--name-only"], [comparison, comparison])
        self.assertEqual(names, "A.lean\nB.lean\n")
        with self.assertRaises(Incomplete):
            self.run_command(["diff", "12"], [comparison, {**comparison, "headRefOid": "c" * 40}])

    def test_view_comments_renders_both_discussion_collections(self):
        row = dict(
            title="Proof",
            url=self.repo.pull_url(12),
            body="Description",
            comments=[dict(author=dict(login="author"), body="Conversation")],
            reviews=[dict(author=dict(login="reviewer"), body="Review")],
        )
        _, value, json_output = self.run_command(["view", "12", "--comments"], [row])
        self.assertFalse(json_output)
        for text in ["Description", "author:", "Conversation", "reviewer:", "Review"]:
            self.assertIn(text, value)

    def test_empty_or_green_native_statuses_cannot_establish_execution_success(self):
        for statuses in [[], [dict(context="build", state="SUCCESS")]]:
            command, rows, _ = self.run_command(["checks", "12"], [{"statusCheckRollup": statuses}])
            self.assertEqual(exit_status(command, rows), 8)
            self.assertEqual(rows[-1]["bucket"], "pending")
            self.assertEqual(rows[-1]["event"], "execution")

    def test_failure_exit_survives_json_projection(self):
        command, rows, _ = self.run_command(
            ["checks", "12", "--json", "name"], [{"statusCheckRollup": [dict(context="build", state="FAILURE")]}]
        )
        self.assertEqual(rows[0], {"name": "build"})
        self.assertEqual(exit_status(command, rows), 1)


if __name__ == "__main__":
    unittest.main()
