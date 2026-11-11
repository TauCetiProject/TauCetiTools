"""Source adapters normalize native identity/history without sharing credentials."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import Forge, Incomplete, RepositoryRef
from tauceti_tools.review_source import ForgejoReviewSource, GitHubReviewSource, review_comments, source_for


class ReviewSources(unittest.TestCase):
    def setUp(self):
        self.repo = RepositoryRef(Forge("forgejo", "https://git.example.org"), "team/project")
        self.identity = RepositoryIdentity(self.repo, 7)
        self.api = Mock(_token="one-token")
        self.api.repository.return_value = dict(id=7, full_name="team/project")
        self.api.current_user.return_value = dict(id=3, login="author")

    def test_repository_and_principal_are_checked_before_review_operations(self):
        self.api.repository.return_value["id"] = 8
        with self.assertRaises(Incomplete):
            ForgejoReviewSource(self.identity, self.api)
        self.api.repository.return_value["id"] = 7
        with self.assertRaises(Incomplete):
            ForgejoReviewSource(self.identity, self.api, expected_actor=4)
        source = ForgejoReviewSource(self.identity, self.api, expected_actor=3)
        self.assertEqual(source.publisher_key(), "forgejo:https://git.example.org/users/3")

    def test_same_name_on_another_origin_and_new_credentials_have_distinct_caches(self):
        first = ForgejoReviewSource(self.identity, self.api)
        other = RepositoryIdentity(RepositoryRef(Forge("forgejo", "https://other.example.org"), self.repo.name), 7)
        second = ForgejoReviewSource(other, self.api)
        self.assertNotEqual(first.cache_key, second.cache_key)
        old = first.cache_key
        self.api._token = "two-token"
        self.assertNotEqual(first.cache_key, old)

    def test_status_observations_do_not_supply_execution_authority(self):
        self.api.pull.return_value = dict(head=dict(sha="a" * 40))
        source = ForgejoReviewSource(self.identity, self.api)
        self.assertEqual(source.execution_metadata(12), dict(headRefOid="a" * 40, statusCheckRollup=[]))
        self.api.statuses.assert_not_called()

    def test_review_head_comes_from_review_and_replies_keep_native_parent(self):
        self.api.reviews.return_value = [dict(id=2, commit_id="a" * 40)]
        self.api.review_comments.return_value = [
            dict(id=3, body="<!--tauceti-rubric:proof-->", path="A.lean", position=1, commit_id="b" * 40),
            dict(id=4, body="UI reply", path="A.lean", position=1, commit_id="b" * 40),
        ]
        rows = review_comments(self.api, self.repo, 12, conversation=[])
        self.assertEqual(rows[0]["commit_id"], "a" * 40)
        self.assertEqual(rows[0]["line_commit_id"], "b" * 40)
        self.assertEqual(rows[1]["in_reply_to_id"], 3)

    def test_binding_rejects_other_repository_origin_and_unsafe_credential_file(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("secret")
            token.chmod(0o600)
            env = dict(
                TAUCETI_SOURCE_BINDING=json.dumps(self.identity.record()),
                TAUCETI_FORGEJO_ORIGIN=self.repo.forge.url,
                TAUCETI_FORGEJO_TOKEN_FILE=str(token),
            )
            with patch("tauceti_tools.review_source.Forgejo", return_value=self.api):
                self.assertEqual(source_for(self.repo.key, env).key, self.repo.key)
                with self.assertRaises(ValueError):
                    source_for("another/repo", env)
                with self.assertRaises(ValueError):
                    source_for(self.repo.key, dict(env, TAUCETI_FORGEJO_ORIGIN="https://other.example.org"))
                token.chmod(0o644)
                with self.assertRaises(ValueError):
                    source_for(self.repo.key, env)

    def test_github_collection_consumes_every_page_without_new_cli_flags(self):
        source = GitHubReviewSource(RepositoryRef(Forge("github", "https://github.com"), "team/project"))
        with patch(
            "tauceti_tools.review_source.subprocess.run",
            return_value=Mock(returncode=0, stdout='[{"id":1,"body":null}]\n[{"id":2,"body":null}]'),
        ) as run:
            self.assertEqual(source.comments(12), [dict(id=1, body=None), dict(id=2, body=None)])
            self.assertNotIn("--slurp", run.call_args.args[0])
        with patch.dict(os.environ, {"GITHUB_ACTOR": "ci-author"}):
            self.assertEqual(source.publisher_key(), "ci-author")


if __name__ == "__main__":
    unittest.main()
