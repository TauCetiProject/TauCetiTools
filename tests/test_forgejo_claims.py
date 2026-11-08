"""Qualified claim identity, credentials and unavailable native observations."""

import base64
import copy
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.claims import ForgejoGitClaim, qualified_claim_key, script_path
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
        renamed = RepositoryIdentity(RepositoryRef(self.forge, "another/transferred"), 42)
        self.assertEqual(key, qualified_claim_key(renamed, "review/7/" + "a" * 40))
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
        with patch.dict(
            os.environ,
            TAUCETI_CLAIM_SH="/candidate/helper",
            GH_TOKEN="other-secret",
            GIT_TRACE="1",
            TAUCETI_QUALIFIED_ONLY="true",
        ):
            claim = self.claim()
        self.assertEqual(claim.script, str(script_path()))
        self.assertNotIn("GH_TOKEN", claim.env)
        self.assertNotIn("GIT_TRACE", claim.env)
        self.assertNotIn("TAUCETI_QUALIFIED_ONLY", claim.env)
        with patch.object(ForgejoGitClaim, "_run", return_value=0):
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
            with self.subTest(change=change), patch.object(ForgejoGitClaim, "_run") as git:
                self.client.repository.side_effect = lambda *_, change=change: self.row | change
                claim.held = True
                self.assertEqual(claim.command("renew"), 2)
                self.assertFalse(claim.held)
                git.assert_not_called()

    def test_post_write_identity_change_never_reports_acquired(self):
        claim = self.claim()
        self.client.repository.side_effect = [self.row, self.row | {"id": 99}]
        with patch.object(ForgejoGitClaim, "_run", return_value=0):
            self.assertEqual(claim.acquire(), 2)
        self.assertFalse(claim.held)

    def test_api_outages_and_forbidden_reads_are_unavailable(self):
        claim = self.claim()
        for status in [401, 403, 404, 503]:
            with self.subTest(status=status), patch.object(ForgejoGitClaim, "_run") as git:
                self.client.repository.side_effect = ForgeError("unavailable", status=status)
                self.assertEqual(claim.acquire(), 2)
                git.assert_not_called()
        self.client.repository.side_effect = lambda *_: copy.deepcopy(self.row)
        with patch.object(ForgejoGitClaim, "_run", return_value=0):
            self.assertEqual(claim.command("holds"), 0)
        self.assertTrue(claim.held)

    def test_uncertain_acquisition_still_releases_its_remote_lease(self):
        claim = self.claim()
        self.client.repository.side_effect = [self.row, ForgeError("lost response", status=503)]
        with patch.object(ForgejoGitClaim, "_run", return_value=0):
            self.assertEqual(claim.acquire(), 2)
        self.assertFalse(claim.held)
        self.client.repository.side_effect = lambda *_: copy.deepcopy(self.row)
        with patch.object(ForgejoGitClaim, "_run", return_value=0) as run:
            claim.release()
            run.assert_called_once_with("release")

    def test_native_ttl_must_be_positive_and_bounded(self):
        for ttl in (0, -1, 86401, True, 1.5):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.claim(ttl=ttl)

    def test_empty_xdg_cache_uses_the_home_cache_and_relative_paths_are_rejected(self):
        with patch.dict(os.environ, XDG_CACHE_HOME=""):
            claim = self.claim(cache_base=None)
        self.assertEqual(claim.directory.parent, Path.home() / ".cache/tauceti-claims")
        with self.assertRaises(ValueError):
            self.claim(cache_base=Path("candidate-cache"))

    def test_release_reports_an_outage_for_retry_without_skipping_identity_checks(self):
        claim = self.claim()
        self.client.repository.side_effect = ForgeError("unavailable", status=503)
        with patch.object(ForgejoGitClaim, "_run") as run:
            self.assertEqual(claim.release(), 2)
            run.assert_not_called()
        self.assertEqual(claim.last_error, "native claim repository binding unavailable")
        self.client.repository.side_effect = lambda *_: copy.deepcopy(self.row)
        with patch.object(ForgejoGitClaim, "_run", return_value=0):
            self.assertEqual(claim.release(), 0)

    def test_release_waits_for_an_inflight_renew_and_blocks_late_heartbeats(self):
        claim = self.claim()
        entered, proceed = threading.Event(), threading.Event()
        calls = []

        def run(verb):
            if verb == "renew":
                entered.set()
                self.assertTrue(proceed.wait(5))
            calls.append(verb)
            return 0

        with patch.object(ForgejoGitClaim, "_run", side_effect=run):
            claim.thread = threading.Thread(target=lambda: claim.command("renew"))
            claim.thread.start()
            self.assertTrue(entered.wait(5))
            with ThreadPoolExecutor(max_workers=1) as pool:
                released = pool.submit(claim.release)
                try:
                    self.assertTrue(claim.stop.wait(5))
                    self.assertFalse(released.done())
                    self.assertEqual(calls, [])
                finally:
                    proceed.set()
                self.assertEqual(released.result(timeout=5), 0)
            self.assertEqual(claim.command("renew"), 1)
        self.assertEqual(calls, ["renew", "release"])
        self.assertFalse(claim.held)

    def test_existing_cache_cannot_redirect_authenticated_git_operations(self):
        claim = self.claim()
        claim._prepare()
        config = claim.directory / "config"
        original = config.read_bytes()
        for key, value in (
            ("http.https://source.example/.sslVerify", "false"),
            ("http.https://source.example/.followRedirects", "true"),
            ("http.https://source.example/.proxy", "https://other.example"),
            ("remote.origin.pushurl", "https://other.example/team/claims.git"),
            ("include.path", str(self.root / "other-config")),
        ):
            with self.subTest(key=key):
                config.write_bytes(original)
                subprocess.run([claim.git, "-C", str(claim.directory), "config", "--local", key, value], check=True)
                with patch.object(ForgejoGitClaim, "_run") as run:
                    self.assertEqual(claim.acquire(), 2)
                    self.assertEqual(claim.last_error, "native claim cache setup unavailable")
                    run.assert_not_called()

    def test_timeout_stops_children_even_after_their_pipes_are_closed(self):
        claim = self.claim()
        claim.command_timeout = 0.2
        ready = self.root / "ready"
        child = self.root / "child.py"
        child.write_text(
            "import os, signal, time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "os.close(1); os.close(2)\n"
            f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
            "while True: time.sleep(1)\n"
        )
        helper = self.root / "timeout.sh"
        # Only this test replaces the pinned helper with a controlled process tree.
        helper.write_text(f"{shlex.quote(sys.executable)} {shlex.quote(str(child))} &\nwait\n")
        claim.script = str(helper)
        popen = subprocess.Popen

        def started(*args, **kwargs):
            process = popen(*args, **kwargs)
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "child must start before the communication timeout begins")
            return process

        try:
            with patch("tauceti_tools.claims.subprocess.Popen", side_effect=started):
                self.assertEqual(claim._run("acquire"), 2)
            status = Path(f"/proc/{ready.read_text()}/stat")
            deadline = time.monotonic() + 5
            while status.exists():
                try:
                    if status.read_text().split()[2] == "Z":
                        break
                except FileNotFoundError:
                    break
                self.assertLess(time.monotonic(), deadline, "a timed-out child must stop executing")
                time.sleep(0.01)
        finally:
            if ready.exists():
                try:
                    os.kill(int(ready.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

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
