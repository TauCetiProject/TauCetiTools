"""Exercise the existing custom-ref lease protocol on disposable Forgejo HTTPS."""

import json
import os
import signal
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.forgejo_https import source_fixture

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.claims import ForgejoGitClaim
from tauceti_tools.forge import Forgejo, RepositoryRef


@unittest.skipUnless(os.environ.get("FORGEJO_BINARY"), "set FORGEJO_BINARY for disposable HTTPS claims")
class NativeGitClaims(unittest.TestCase):
    def test_contributor_custom_refs_competing_claims_expiry_restart_and_permissions(self):
        with source_fixture() as (root, admin, content, source_row, tokens):
            row = admin.request("POST", "user/repos", {"name": "claims", "private": True, "auto_init": True})
            repository = RepositoryRef(content.forge, "operator/claims")
            admin.request("PATCH", f"repos/{repository.name}", {"has_actions": False})
            admin.request("PUT", f"repos/{repository.name}/collaborators/contributor", {"permission": "write"})
            client = Forgejo(content.forge, token=tokens["contributor"], timeout=5)
            self.assertFalse(client.current_user()["is_admin"])
            self.assertIs(client.repository(repository)["has_actions"], False)
            identity = RepositoryIdentity(repository, row["id"])
            source = RepositoryIdentity(content, source_row["id"])

            def claim(owner, *, resource="review/7/" + "a" * 40, ttl=60, cache=None):
                return ForgejoGitClaim(
                    identity,
                    resource,
                    owner,
                    ttl,
                    source=source,
                    client=client,
                    token=tokens["contributor"],
                    credential_origin=content.forge,
                    cache_base=cache or root / "cache",
                )

            first, second = claim("first"), claim("second")
            self.assertEqual(first.acquire(), 0)
            self.assertEqual(second.acquire(), 1)
            self.assertEqual(first.command("holds"), 0)
            self.assertEqual(first.command("renew"), 0)
            restarted = claim("first")  # No local ownership flag survives a process restart.
            self.assertEqual(restarted.acquire(), 0)
            first.held = False
            restarted.release()
            self.assertEqual(second.acquire(), 0)
            second.release()

            expired = claim("dead-holder", ttl=-1)
            self.assertEqual(expired.acquire(), 0)
            replacement = claim("after-crash")
            self.assertEqual(replacement.acquire(), 0)
            expired.release()
            self.assertEqual(replacement.command("holds"), 0)
            replacement.release()

            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import json, os, signal, sys
from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.claims import ForgejoGitClaim
from tauceti_tools.forge import Forgejo
value = json.load(sys.stdin)
repository = RepositoryIdentity.parse(value['repository'])
source = RepositoryIdentity.parse(value['source'])
client = Forgejo(repository.repository.forge, token=value['token'], timeout=5)
lease = ForgejoGitClaim(repository, value['resource'], 'crashed-process', -1,
    source=source, client=client, token=value['token'], credential_origin=repository.repository.forge,
    cache_base=value['cache'])
if lease.acquire() != 0:
    raise SystemExit(10)
os.kill(os.getpid(), signal.SIGKILL)
""",
                ],
                input=json.dumps(
                    {
                        "repository": identity.record(),
                        "source": source.record(),
                        "token": tokens["contributor"],
                        "resource": "review/crash/" + "d" * 40,
                        "cache": str(root / "crashed-process"),
                    }
                ),
                capture_output=True,
                text=True,
                timeout=30,
                cwd=Path(__file__).resolve().parents[1],
            )
            self.assertEqual(child.returncode, -signal.SIGKILL, "fixture process did not acquire then crash")
            after_crash = claim("takeover", resource="review/crash/" + "d" * 40)
            self.assertEqual(after_crash.acquire(), 0)
            after_crash.release()

            contenders = [
                claim("racer-" + str(n), resource="review/8/" + "b" * 40, cache=root / f"racer-{n}") for n in range(2)
            ]
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda c: c.acquire(), contenders))
            self.assertEqual(sorted(results), [0, 1])
            for contender in contenders:
                contender.release()

            revoked = claim("revoked", resource="review/9/" + "c" * 40)
            admin.request("PUT", f"repos/{repository.name}/collaborators/contributor", {"permission": "read"})
            self.assertEqual(revoked.acquire(), 2)
            self.assertFalse(revoked.held)


if __name__ == "__main__":
    unittest.main()
