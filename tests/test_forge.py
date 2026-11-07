"""Real HTTP contract checks for qualified identities and direct Forgejo reads."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
                self.wfile.write(json.dumps(data).encode())

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

    def test_complete_pagination_despite_server_cap(self):
        self.reply = lambda path: (200, [] if "page=3" in path else [{"number": 1}], {})
        self.assertEqual(len(self.api.pulls(self.repo)), 2)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(auth == "token private-test-token" for _, _, auth in self.calls))

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


if __name__ == "__main__":
    unittest.main()
