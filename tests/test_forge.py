"""Real HTTP contract checks for qualified identities and direct Forgejo reads."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from tauceti_tools.forge import (
    GITHUB,
    AmbiguousWrite,
    Forge,
    ForgeError,
    Forgejo,
    Incomplete,
    PrincipalRef,
    RepositoryRef,
)


class IdentityTests(unittest.TestCase):
    def test_same_names_and_ids_do_not_cross_servers(self):
        forge = Forge("forgejo", "https://git.example.org")
        self.assertNotEqual(RepositoryRef(GITHUB, "one/project").key, RepositoryRef(forge, "one/project").key)
        self.assertNotEqual(PrincipalRef(GITHUB, "1"), PrincipalRef(forge, "1"))
        self.assertEqual(Forge("forgejo", "https://GIT.example.org:443/"), forge)
        self.assertEqual(
            RepositoryRef(forge, "one/project").pull_url(12), "https://git.example.org/one/project/pulls/12"
        )
        self.assertIn("/src/commit/" + "a" * 40, RepositoryRef(forge, "one/project").source_url("a" * 40, "A B.lean"))

    def test_reject_credential_urls_paths_and_nonlocal_plaintext(self):
        for url in [
            "https://user:secret@git.example.org",
            "https://git.example.org/path",
            "http://example.org",
            "https://git.example.org?q=1",
            "https://git.example.org/#fragment",
            "https://bad host/",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                Forge("forgejo", url)
        for name in ["one/..", "../two", "one/two.git", "one/two/three", "one/two?token=secret"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                RepositoryRef(GITHUB, name)


class DirectTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.reply = lambda path: (200, [], {})
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.calls.append((self.command, self.path, self.headers.get("Authorization")))
                status, data, headers = owner.reply(self.path)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data if isinstance(data, bytes) else json.dumps(data).encode())

            do_POST = do_GET

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.forge = Forge("forgejo", f"http://127.0.0.1:{self.server.server_port}")
        self.api = Forgejo(self.forge, token="private-test-token", max_pages=3)
        self.repo = RepositoryRef(self.forge, "one/project")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_response_metadata_comes_from_the_real_http_response(self):
        self.reply = lambda path: (200, {"id": 2, "login": "operator"}, {"ETag": '"version"'})
        headers, meta = {}, {}
        value = self.api.request("GET", "user", response_headers=headers, response_meta=meta)
        self.assertEqual(value["id"], 2)
        self.assertEqual(headers["etag"], '"version"')
        self.assertEqual(meta, {"status": 200, "reason": "OK", "version": 10})

    def test_complete_pagination_despite_server_cap(self):
        self.reply = lambda path: (200, [] if "page=3" in path else [{"number": 1 if "page=2" in path else 2}], {})
        self.assertEqual(len(self.api.pulls(self.repo)), 2)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(auth == "token private-test-token" for _, _, auth in self.calls))

    def test_prefix_stops_without_scanning_history(self):
        self.reply = lambda path: (200, [{"number": n} for n in [10000, 9999, 9998]], {})
        self.assertEqual([r["number"] for r in self.api.pulls(self.repo, limit=2)], [10000, 9999])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("sort=oldest", self.calls[0][1])

    def test_merged_and_label_filters_precede_prefix_with_overlapping_pages(self):
        pages = {
            "1": [{"number": 6, "merged": False}, {"number": 5, "merged": True, "labels": [{"name": "Bug"}]}],
            "2": [
                {"number": 5, "merged": True},
                {"number": 4, "merged": True, "labels": [{"name": "BUG"}, {"name": "ready"}]},
            ],
        }
        self.reply = lambda path: (200, pages.get(path.rsplit("page=", 1)[1], []), {})
        rows = self.api.pulls(self.repo, state="merged", labels=["bug", "ready"], limit=1)
        self.assertEqual([r["number"] for r in rows], [4])
        self.assertEqual(len(self.calls), 2)
        self.assertIn("state=closed", self.calls[0][1])

    def test_filtered_prefix_cap_raises_rather_than_partial_success(self):
        self.reply = lambda path: (200, [{"number": int(path.rsplit("page=", 1)[1]), "merged": False}], {})
        with self.assertRaises(Incomplete):
            self.api.pulls(self.repo, state="merged", limit=1)

    def test_closed_pr_state_excludes_merged_prs(self):
        self.reply = lambda path: (200, [{"number": 4, "merged": True}, {"number": 3, "merged": False}], {})
        self.assertEqual([r["number"] for r in self.api.pulls(self.repo, state="closed", limit=1)], [3])

    def test_issue_filters_keep_prs_out_and_require_all_labels(self):
        self.reply = lambda path: (
            200,
            [
                {"number": 8, "labels": [{"name": "bug"}], "user": {"login": "a"}},
                {"number": 7, "labels": [{"name": "bug"}, {"name": "ready"}], "user": {"login": "b"}},
                {"number": 6, "labels": [{"name": "bug"}, {"name": "ready"}], "user": {"login": "A"}},
            ],
            {},
        )
        self.assertEqual(
            [r["number"] for r in self.api.issues(self.repo, labels=["bug", "ready"], author="a", limit=1)], [6]
        )
        self.assertIn("type=issues", self.calls[0][1])
        self.assertIn("created_by=a", self.calls[0][1])

    def test_no_numeric_order_assumption_for_imported_timestamps(self):
        self.reply = lambda path: (200, [{"number": 1}, {"number": 3}], {})
        self.assertEqual([r["number"] for r in self.api.pulls(self.repo, limit=2)], [1, 3])

    def test_review_comments_are_unpaginated_and_cross_host_checks_apply(self):
        self.reply = lambda path: (200, [{"id": 3}], {})
        self.assertEqual(self.api.review_comments(self.repo, 1, 2), [{"id": 3}])
        self.assertEqual(self.calls[0][1], "/api/v1/repos/one/project/pulls/1/reviews/2/comments")
        for call in [
            lambda: self.api.files(RepositoryRef(GITHUB, "one/project"), 1),
            lambda: self.api.statuses(self.repo, "main"),
            lambda: self.api.review_comments(self.repo, 1, -1),
        ]:
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(len(self.calls), 1)

    def test_hidden_draft_review_page_does_not_hide_later_submitted_reviews(self):
        def reply(path):
            if path.endswith("settings/api"):
                return 200, {"max_response_items": 1}, {}
            return 200, ([{"id": 2}] if "page=2" in path else []), {"X-Total-Count": "2"}

        self.reply = reply
        self.assertEqual(self.api.reviews(self.repo, 1), [{"id": 2}])
        self.assertEqual(len(self.calls), 3)
        self.assertIn("limit=1", self.calls[1][1])

    def test_review_pages_require_count_metadata(self):
        self.reply = lambda path: (200, {"max_response_items": 1} if path.endswith("settings/api") else [], {})
        with self.assertRaises(Incomplete):
            self.api.reviews(self.repo, 1)

    def test_unknown_author_is_empty_only_after_repository_access_is_verified(self):
        def reply(path):
            if path == "/api/v1/repos/one/project":
                return 200, {"full_name": "one/project"}, {}
            return 404, {}, {}

        self.reply = reply
        self.assertEqual(self.api.issues(self.repo, author="unknown"), [])
        self.reply = lambda path: (404, {}, {})
        with self.assertRaises(ForgeError):
            self.api.issues(self.repo, author="unknown")

    def test_branch_filters_are_checked_against_native_labels(self):
        self.reply = lambda path: (
            200,
            [
                {"number": 3, "head": {"label": "wrong"}, "base": {"label": "main"}},
                {"number": 2, "head": {"label": "proof", "ref": "refs/pull/2/head"}, "base": {"label": "main"}},
            ],
            {},
        )
        self.assertEqual([r["number"] for r in self.api.pulls(self.repo, head="proof", base="main", limit=1)], [2])

    def test_repository_and_account_responses_must_identify_target(self):
        for data, call in [
            ({"full_name": "other/project"}, lambda: self.api.repository(self.repo)),
            ({"number": 2}, lambda: self.api.issue(self.repo, 1)),
            ({"number": 1, "pull_request": {}}, lambda: self.api.issue(self.repo, 1)),
            ({"login": "a"}, self.api.current_user),
        ]:
            self.reply = lambda path, data=data: (200, data, {})
            with self.assertRaises(Incomplete):
                call()

    def test_pull_ref_requires_exact_native_ref_and_immutable_object(self):
        self.reply = lambda path: (
            200,
            [
                {"ref": "refs/pull/1/headExtra", "object": {"sha": "b" * 40}},
                {"ref": "refs/pull/1/head", "object": {"sha": "a" * 40}},
            ],
            {},
        )
        self.assertEqual(self.api.pull_ref(self.repo, 1), "a" * 40)
        self.reply = lambda path: (200, [{"ref": "refs/pull/1/headExtra", "object": {"sha": "a" * 40}}], {})
        with self.assertRaises(Incomplete):
            self.api.pull_ref(self.repo, 1)

    def test_later_page_failure_is_not_partial_success(self):
        self.reply = lambda path: (503, {}, {}) if "page=2" in path else (200, [1], {})
        with self.assertRaises(ForgeError):
            self.api.pulls(self.repo)

    def test_pagination_exhaustion_is_not_complete(self):
        self.reply = lambda path: (200, [1], {})
        with self.assertRaises(Incomplete):
            self.api.pulls(self.repo)

    def test_comment_history_uses_unpaginated_native_endpoint(self):
        self.reply = lambda path: (200, [{"id": 1}], {})
        self.assertEqual(self.api.comments(self.repo, 1), [{"id": 1}])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], "/api/v1/repos/one/project/issues/1/comments")

    def test_native_null_file_end_is_not_a_generic_successful_empty_response(self):
        self.reply = lambda path: (200, None, {})
        self.assertEqual(self.api.files(self.repo, 1), [])
        with self.assertRaises(Incomplete):
            self.api.collection("user/repos")
        self.reply = lambda path: (200, b"", {})
        with self.assertRaises(Incomplete):
            self.api.files(self.repo, 1)

    def test_redirect_is_not_followed(self):
        self.reply = lambda path: (302, {}, {"Location": self.forge.url + "/stolen"})
        with self.assertRaises(ForgeError) as caught:
            self.api.request("GET", "user")
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("private-test-token", str(caught.exception))

    def test_mutation_is_not_retried_after_server_failure(self):
        self.reply = lambda path: (503, {}, {})
        with self.assertRaises(AmbiguousWrite):
            self.api.post_comment(self.repo, 1, "body")
        self.assertEqual(len(self.calls), 1)

    def test_cross_server_and_absolute_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            self.api.pull(RepositoryRef(GITHUB, "one/project"), 1)
        for path in ["https://other.org/user", "//other.org/user", "../user", "%2e%2e/user", "user#fragment"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.api.request("GET", path)
        with self.assertRaises(ValueError):
            self.api.collection("https://other.org/user")
        self.assertEqual(self.calls, [])

    def test_response_byte_limit(self):
        self.api.max_bytes = 1
        with self.assertRaises(Incomplete):
            self.api.request("GET", "user")
        with self.assertRaises(AmbiguousWrite):
            self.api.request("POST", "user", {})

    def test_loopback_credentials_bypass_environment_proxy(self):
        with patch.dict(
            "os.environ",
            {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": ""},
        ):
            api = Forgejo(self.forge, token="local-token")
            self.assertEqual(api.request("GET", "user"), [])
        self.assertEqual(self.calls[0][1], "/api/v1/user")


if __name__ == "__main__":
    unittest.main()
