"""Qualified CI sources keep GitHub execution credentials out of source fetches."""

import base64
import contextlib
import importlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.forge import GITHUB, Forge, RepositoryRef

SCRIPTS = Path(__file__).parents[1] / "tauceti_tools/assets/scripts"
sys.path.insert(0, str(SCRIPTS))
checkout = importlib.import_module("ci_checkout")
ci_git = importlib.import_module("ci_git")

spec = importlib.util.spec_from_file_location("ci_source_fixture", SCRIPTS / "test_ci_checkout.py")
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)


class Sources(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.Checkouts()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.forge = Forge("forgejo", "https://source.example.org")
        self.repo = RepositoryRef(self.forge, "Owner/Repo")
        self.token = "private-native-source-token"

    def test_qualified_identity_and_materialized_remotes_preserve_source_origin(self):
        transferred = []
        transfer = checkout.SourceGit.transfer

        def local(git, url, references):
            transferred.append(url)
            self.assertIn("http.followRedirects=false", git.command)
            self.assertEqual(git.env["GIT_CONFIG_KEY_0"], "http.https://source.example.org/.extraheader")
            return transfer(git, self.fixture.repo.as_uri(), references)

        with patch.object(checkout.SourceGit, "transfer", local):
            result = checkout.fetch_sources(
                self.repo,
                self.repo,
                self.fixture.head,
                self.fixture.tip,
                self.fixture.base,
                self.fixture.workspace,
                token=self.token,
                credential_origin=self.forge,
                attempts=1,
            )
        self.assertEqual(transferred, [self.repo.url + ".git"])
        store = Path(result["store"])
        identity = json.loads((store / "identity.json").read_text())
        self.assertEqual(identity["schema"], 2)
        self.assertEqual(identity["repository"]["forge"]["url"], self.forge.url)
        for role, commit in [("head", self.fixture.head), ("base", self.fixture.tip), ("mergebase", self.fixture.base)]:
            checkout.materialize(self.fixture.workspace, role, commit)
            path = self.fixture.workspace / checkout.ROLES[role]
            self.assertEqual(self.fixture.git(path, "rev-parse", "HEAD"), commit)
            self.assertEqual(self.fixture.git(path, "remote", "get-url", "origin"), self.repo.url + ".git")
            self.assertNotIn(self.token, (path / ".git/config").read_text())
        self.assertNotIn(self.token, (store / "config").read_text() + (store / "identity.json").read_text())

    def test_source_credentials_cannot_follow_another_origin_or_mixed_identity(self):
        other = RepositoryRef(Forge("forgejo", "https://other.example.org"), "Owner/Repo")
        for repository, head, origin in [
            (self.repo, self.repo, None),
            (self.repo, self.repo, GITHUB),
            (other, other, self.forge),
            (self.repo, other, self.forge),
            (self.repo, "Owner/Repo", self.forge),
            ("Owner/Repo", self.repo, self.forge),
        ]:
            with self.subTest(repository=repository, head=head, origin=origin):
                with patch.object(checkout, "SourceGit") as git:
                    with self.assertRaises(ValueError):
                        checkout.fetch_sources(
                            repository,
                            head,
                            self.fixture.head,
                            self.fixture.tip,
                            self.fixture.base,
                            self.fixture.workspace,
                            token=self.token,
                            credential_origin=origin,
                        )
                    git.assert_not_called()
                self.assertFalse((self.fixture.workspace / checkout.STORE).exists())

    def test_resolver_uses_same_forge_and_verifies_the_exact_candidate_ref(self):
        fetch = ci_git.Git.fetch
        seen = []

        def local(git, url, refs):
            seen.append(url)
            self.assertIn("http.followRedirects=false", git.command)
            return fetch(git, self.fixture.repo.as_uri(), refs)

        with patch.object(ci_git.Git, "fetch", local):
            result = ci_git.resolve(
                self.repo,
                self.repo,
                self.fixture.head,
                self.fixture.base,
                self.fixture.root / "resolved.git",
                token=self.token,
                credential_origin=self.forge,
                staging_ref="feature",
            )
        self.assertEqual(result["base_sha"], self.fixture.base)
        self.assertEqual(result["mergebase"], self.fixture.base)
        self.assertEqual(result["staging_sha"], self.fixture.head)
        self.assertEqual(seen, [self.repo.url + ".git"])
        with patch.object(ci_git.Git, "fetch", local), self.assertRaisesRegex(ValueError, "differs"):
            ci_git.resolve(
                self.repo,
                self.repo,
                self.fixture.base,
                "main",
                self.fixture.root / "wrong.git",
                credential_origin=self.forge,
                staging_ref="feature",
            )

    def test_source_cli_never_borrows_execution_token_environment(self):
        for script, command, target, result in [
            (ci_git, "resolve", "resolve", {"base_sha": self.fixture.base}),
            (checkout, "fetch", "fetch_sources", {"store": "source.git"}),
        ]:
            args = [
                "ci-helper",
                command,
                "--repository",
                "Owner/Repo",
                "--head-repository",
                "Owner/Repo",
                "--head",
                self.fixture.head,
                "--source-kind",
                "forgejo",
                "--source-origin",
                self.forge.url,
            ]
            if command == "resolve":
                args += ["--base-ref", "main", "--store", str(self.fixture.root / "cli.git")]
            else:
                args += ["--base", self.fixture.tip, "--mergebase", self.fixture.base]
            env = {"CI_GIT_TOKEN": "execution-secret", "GH_TOKEN": "another-execution-secret"}
            with (
                self.subTest(command=command),
                patch.dict(os.environ, env, clear=True),
                patch.object(sys, "argv", args),
            ):
                with (
                    patch.object(script, target, return_value=result) as operation,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    script.main()
                self.assertIsNone(operation.call_args.kwargs["token"])
                source = (
                    operation.call_args.args[0]
                    if operation.call_args.args
                    else operation.call_args.kwargs["repository"]
                )
                self.assertEqual(source, self.repo)

    def test_native_credentials_are_scoped_and_never_written_to_arguments_or_config(self):
        with tempfile.TemporaryDirectory() as home:
            git = ci_git.Git(
                self.fixture.root / "auth.git", home, token=self.token, token_origin=self.forge, follow_redirects=False
            )
        self.assertEqual(git.env["GIT_CONFIG_KEY_0"], "http.https://source.example.org/.extraheader")
        self.assertNotIn(self.token, repr(git.command))
        self.assertNotIn(self.token, (git.repository / "config").read_text())
        self.assertNotIn("GH_TOKEN", git.env)

    def test_native_redirect_cannot_forward_a_source_credential(self):
        first, second = [], []

        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):
                second.append(self.headers.get("Authorization"))
                self.send_response(404)
                self.end_headers()

            def log_message(self, *_):
                pass

        sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                first.append(self.headers.get("Authorization"))
                self.send_response(302)
                self.send_header(
                    "Location", f"http://127.0.0.1:{sink.server_port}/foreign.git/info/refs?service=git-upload-pack"
                )
                self.end_headers()

            def log_message(self, *_):
                pass

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threads = []
        for server in (sink, redirect):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        try:
            forge = Forge("forgejo", f"http://127.0.0.1:{redirect.server_port}")
            repo = RepositoryRef(forge, "Owner/Repo")
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors), self.assertRaises(subprocess.CalledProcessError):
                checkout.fetch_sources(
                    repo,
                    repo,
                    self.fixture.head,
                    self.fixture.tip,
                    self.fixture.base,
                    self.fixture.workspace,
                    token=self.token,
                    credential_origin=forge,
                    attempts=1,
                )
            self.assertTrue(first)
            self.assertFalse(second)
            encoded = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            self.assertEqual(first[0], "basic " + encoded)
            self.assertNotIn(self.token, errors.getvalue())
            self.assertNotIn(encoded, errors.getvalue())
            self.assertFalse((self.fixture.workspace / checkout.STORE).exists())
        finally:
            for server in (sink, redirect):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(2)


if __name__ == "__main__":
    unittest.main()
