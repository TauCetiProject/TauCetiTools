"""Exercise the shared lease protocol against a real local Git remote."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.claims import GitClaim


class GitLeaseTest(unittest.TestCase):
    def test_ref_changed_between_observation_and_fetch_is_contention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            git = shutil.which("git")
            subprocess.run([git, "init", "-q", "--bare", str(remote)], check=True)
            env = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url." + str(remote) + ".insteadOf",
                "GIT_CONFIG_VALUE_0": "https://github.com/test/claims",
                "CLAIM_GITDIR_BASE": str(root / "holder"),
            }
            with patch.dict(os.environ, env):
                holder = GitClaim("test/claims", "review/race", "holder")
                self.assertEqual(holder.acquire(), 0)
                ref = "refs/tauceti-claims/" + holder.key
                old = subprocess.check_output([git, "-C", str(remote), "rev-parse", ref]).decode().strip()
                holder.ttl += 1  # A different payload gives the renewal a distinct commit.
                self.assertEqual(holder.command("renew"), 0)
                renewed = subprocess.check_output([git, "-C", str(remote), "rev-parse", ref]).decode().strip()
                self.assertNotEqual(old, renewed)
                wrapper = root / "bin"
                wrapper.mkdir()
                executable = wrapper / "git"
                executable.write_text(
                    f"#!{sys.executable}\n"
                    "import os, subprocess, sys\n"
                    "if 'fetch' in sys.argv[1:]:\n"
                    f"    subprocess.run({[git, '-C', str(remote), 'update-ref', ref, renewed]!r}, check=True)\n"
                    f"os.execv({git!r}, [{git!r}] + sys.argv[1:])\n"
                )
                executable.chmod(0o700)
                for verb in ("acquire", "renew", "holds"):
                    subprocess.run([git, "-C", str(remote), "update-ref", ref, old], check=True)
                    with patch.dict(
                        os.environ,
                        PATH=str(wrapper) + os.pathsep + os.environ["PATH"],
                        CLAIM_GITDIR_BASE=str(root / verb),
                    ):
                        reader = GitClaim("test/claims", holder.key, "holder")
                        self.assertEqual(reader.command(verb), 1, verb)

    def test_gc_preserves_malformed_leases_but_collects_other_expired_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            env = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url." + str(remote) + ".insteadOf",
                "GIT_CONFIG_VALUE_0": "https://github.com/test/claims",
                "CLAIM_GITDIR_BASE": str(root / "cache"),
            }
            author = dict(
                os.environ,
                GIT_AUTHOR_NAME="fixture",
                GIT_AUTHOR_EMAIL="test@example.invalid",
                GIT_COMMITTER_NAME="fixture",
                GIT_COMMITTER_EMAIL="test@example.invalid",
            )
            tree = subprocess.check_output(["git", "-C", str(remote), "mktree"], input=b"").decode().strip()
            malformed = "a-bad"
            expired = "z-expired"
            for key, expiry in ((malformed, 10**30), (expired, 0)):
                payload = dict(schema="tauceti-claim/v1", resource=key, owner="other", expires_at=expiry)
                oid = (
                    subprocess.check_output(
                        ["git", "-C", str(remote), "commit-tree", tree], env=author, input=json.dumps(payload).encode()
                    )
                    .decode()
                    .strip()
                )
                subprocess.run(["git", "-C", str(remote), "update-ref", "refs/tauceti-claims/" + key, oid], check=True)
            with patch.dict(os.environ, env):
                claim = GitClaim("test/claims", malformed, "owner")
                self.assertEqual(claim.acquire(), 2)
                self.assertEqual(claim.command("gc"), 2)
            references = subprocess.check_output(
                ["git", "-C", str(remote), "for-each-ref", "--format=%(refname)"]
            ).decode()
            self.assertIn("refs/tauceti-claims/" + malformed, references)
            self.assertNotIn("refs/tauceti-claims/" + expired, references)

    def test_unavailable_or_malformed_history_cannot_become_an_expired_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote = Path(tmp) / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            env = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url." + str(remote) + ".insteadOf",
                "GIT_CONFIG_VALUE_0": "https://github.com/test/claims",
                "CLAIM_GITDIR_BASE": str(Path(tmp) / "scratch"),
            }
            with patch.dict(os.environ, env):
                claim = GitClaim("test/claims", "review/test", "owner")
                self.assertEqual(claim.acquire(), 0)
                hidden = remote.with_name("unavailable.git")
                remote.rename(hidden)
                for verb in ["acquire", "renew", "holds", "release"]:
                    self.assertEqual(claim.command(verb), 2, verb)
                hidden.rename(remote)
                author = dict(
                    os.environ,
                    GIT_AUTHOR_NAME="fixture",
                    GIT_AUTHOR_EMAIL="test@example.invalid",
                    GIT_COMMITTER_NAME="fixture",
                    GIT_COMMITTER_EMAIL="test@example.invalid",
                )
                tree = subprocess.check_output(["git", "-C", str(remote), "mktree"], input=b"").decode().strip()
                oid = (
                    subprocess.check_output(
                        ["git", "-C", str(remote), "commit-tree", tree],
                        env=author,
                        input=json.dumps({"owner": "other", "expires_at": 0}).encode(),
                    )
                    .decode()
                    .strip()
                )
                ref = "refs/tauceti-claims/" + claim.key
                subprocess.run(["git", "-C", str(remote), "update-ref", ref, oid], check=True)
                self.assertEqual(claim.acquire(), 2)
                self.assertEqual(claim.command("holds"), 2)
                self.assertEqual(claim.command("gc"), 2)
                self.assertEqual(
                    subprocess.check_output(["git", "-C", str(remote), "rev-parse", ref]).decode().strip(), oid
                )

    def test_real_cas(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote = Path(tmp) / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            env = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url." + str(remote) + ".insteadOf",
                "GIT_CONFIG_VALUE_0": "https://github.com/test/claims",
                "CLAIM_GITDIR_BASE": str(Path(tmp) / "scratch"),
            }
            with patch.dict(os.environ, env):
                first = GitClaim("test/claims", "review/org/repo/7/" + "a" * 40, "first")
                second = GitClaim("test/claims", first.key, "second")
                assert first.acquire() == 0
                assert second.acquire() == 1
                assert first.command("renew") == 0
                second.release()
                assert second.acquire() == 1
                first.release()
                assert second.acquire() == 0
                second.release()
                expired = GitClaim("test/claims", first.key, "expired", ttl=-1)
                assert expired.acquire() == 0
                assert first.acquire() == 0
                expired.release()
                assert second.acquire() == 1
                first.release()

                hook = remote / "hooks/pre-receive"
                hook.write_text("#!/bin/sh\necho 'permission denied' >&2\nexit 1\n")
                hook.chmod(0o700)
                assert first.acquire() == 2, "a policy/auth push rejection is unavailable, not contention"
