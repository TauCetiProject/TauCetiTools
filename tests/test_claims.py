"""Exercise the shared lease protocol against a real local Git remote."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.claims import GitClaim


class GitLeaseTest(unittest.TestCase):
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
