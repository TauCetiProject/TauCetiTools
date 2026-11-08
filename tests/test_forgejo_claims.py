"""Qualified claim identity, credentials and unavailable native observations."""

import base64
import copy
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.claims import ForgejoGitClaim, GitClaim, qualified_claim_key, script_path
from tauceti_tools.forge import Forge, ForgeError, RepositoryRef
from tauceti_tools.git_credentials import scoped_git_environment


class NativeClaims(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.forge = Forge("forgejo", "https://source.example")
        self.identity = RepositoryIdentity(RepositoryRef(self.forge, "team/claims"), 41)
        self.source = RepositoryIdentity(RepositoryRef(self.forge, "team/content"), 42)
        self.row = {"id": 41, "full_name": "team/claims", "has_actions": False, "archived": False, "mirror": False}
        self.client = SimpleNamespace(forge=self.forge, repository=Mock(side_effect=lambda *_: copy.deepcopy(self.row)))

    def claim(self, **kwargs):
        values = dict(
            source=self.source,
            client=self.client,
            token="native-token",
            credential_origin=self.forge,
            cache_base=self.root / "claims",
        )
        return ForgejoGitClaim(self.identity, "review/7/" + "a" * 40, "worker-nonce", **(values | kwargs))

    def test_source_keys_are_origin_and_native_id_qualified_and_case_stable(self):
        key = qualified_claim_key(self.source, "review/7/" + "a" * 40)
        self.assertRegex(key, r"^v2/[0-9a-f]{64}$")
        aliases = RepositoryIdentity(RepositoryRef(self.forge, "TEAM/CONTENT"), 42)
        self.assertEqual(key, qualified_claim_key(aliases, "review/7/" + "a" * 40))
        for source in [
            RepositoryIdentity(self.source.repository, 43),
            RepositoryIdentity(RepositoryRef(Forge("forgejo", "https://other.example"), "team/content"), 42),
        ]:
            self.assertNotEqual(key, qualified_claim_key(source, "review/7/" + "a" * 40))

    def test_origin_mismatch_fails_before_cache_or_api_work(self):
        with self.assertRaises(ValueError):
            self.claim(credential_origin=Forge("forgejo", "https://other.example"))
        self.client.repository.assert_not_called()
        self.assertFalse((self.root / "claims").exists())

    def test_native_commands_use_the_pinned_helper_and_clean_credential_environment(self):
        with patch.dict(os.environ, TAUCETI_CLAIM_SH="/candidate/helper", GH_TOKEN="other-secret", GIT_TRACE="1"):
            claim = self.claim()
        self.assertEqual(claim.script, str(script_path()))
        self.assertNotIn("GH_TOKEN", claim.env)
        self.assertNotIn("GIT_TRACE", claim.env)
        with patch.object(GitClaim, "command", return_value=0):
            self.assertEqual(claim.acquire(), 0)
        self.assertEqual(self.client.repository.call_count, 2)
        config = (claim.directory / "config").read_text()
        self.assertIn(self.identity.repository.clone_url, config)
        self.assertNotIn("native-token", config)

    def test_source_actions_and_identity_changes_are_unavailable_not_ownership(self):
        claim = self.claim()
        for change in [
            {"id": 99},
            {"id": True},
            {"full_name": "other/claims"},
            {"has_actions": True},
            {"has_actions": 0},
            {"archived": True},
            {"mirror": True},
        ]:
            with self.subTest(change=change), patch.object(GitClaim, "command") as git:
                self.client.repository.side_effect = lambda *_, change=change: self.row | change
                claim.held = True
                self.assertEqual(claim.command("renew"), 2)
                self.assertFalse(claim.held)
                git.assert_not_called()

    def test_post_write_identity_change_never_reports_acquired(self):
        claim = self.claim()
        self.client.repository.side_effect = [self.row, self.row | {"id": 99}]
        with patch.object(GitClaim, "command", return_value=0):
            self.assertEqual(claim.acquire(), 2)
        self.assertFalse(claim.held)

    def test_api_outages_and_forbidden_reads_are_unavailable(self):
        claim = self.claim()
        for status in [401, 403, 404, 503]:
            with self.subTest(status=status), patch.object(GitClaim, "command") as git:
                self.client.repository.side_effect = ForgeError("unavailable", status=status)
                self.assertEqual(claim.acquire(), 2)
                git.assert_not_called()
        self.client.repository.side_effect = lambda *_: copy.deepcopy(self.row)
        with patch.object(GitClaim, "command", return_value=0):
            self.assertEqual(claim.command("holds"), 0)
        self.assertTrue(claim.held)

    def test_git_credentials_are_scoped_without_ambient_headers_helpers_or_traces(self):
        parent = {
            "PATH": os.environ["PATH"],
            "HOME": "/operator",
            "GIT_CONFIG_COUNT": "9",
            "GIT_CONFIG_VALUE_0": "old-secret",
            "GIT_TRACE_CURL": "1",
            "GIT_SSL_NO_VERIFY": "1",
            "GH_TOKEN": "github-secret",
            "HTTPS_PROXY": "other-proxy",
        }
        env = scoped_git_environment(self.forge, "native-token", parent=parent)
        settings = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        self.assertEqual(settings["credential.helper"], "")
        self.assertEqual(settings["http.followRedirects"], "false")
        self.assertEqual(settings["http.sslVerify"], "true")
        self.assertEqual(
            settings["http.https://source.example/.extraheader"],
            "AUTHORIZATION: basic " + base64.b64encode(b"x-access-token:native-token").decode(),
        )
        for value in ["old-secret", "github-secret", "other-proxy"]:
            self.assertNotIn(value, str(env))
        self.assertNotIn("GIT_TRACE_CURL", env)
        self.assertNotIn("GIT_SSL_NO_VERIFY", env)

    def test_redirect_cannot_send_a_native_git_credential_to_another_origin(self):
        received = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(404)
                self.end_headers()

            def log_message(self, *_):
                pass

        target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
        thread = threading.Thread(target=target.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(target.server_close)
        self.addCleanup(target.shutdown)
        authorization = []

        class Redirect(Target):
            def do_GET(self):
                authorization.append(self.headers.get("Authorization"))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/other.git/info/refs")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        forge = Forge("forgejo", f"http://127.0.0.1:{server.server_port}")
        self.identity = RepositoryIdentity(RepositoryRef(forge, "team/claims"), 41)
        self.forge, self.client.forge = forge, forge
        claim = self.claim()
        self.assertEqual(claim.acquire(), 2)
        self.assertTrue(authorization)
        self.assertEqual(received, [])
