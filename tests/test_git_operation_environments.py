"""Real Git distinguishes workspace dependency work from immutable source reads."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tauceti_tools.forge import Forge
from tauceti_tools.git_credentials import scoped_git_environment


class OperationEnvironments(unittest.TestCase):
    def test_workspace_can_clone_trusted_local_package_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", "--bare", str(root / "package")], check=True)
            for purpose, success in [("source", False), ("workspace", True)]:
                env = scoped_git_environment(
                    Forge("forgejo", "https://git.example.org"), "private-token", parent=os.environ, purpose=purpose
                )
                result = subprocess.run(
                    ["git", "clone", str(root / "package"), str(root / purpose)], env=env, capture_output=True
                )
                self.assertEqual(result.returncode == 0, success, result.stderr)

    def test_workspace_materializes_partial_dependency_while_source_stays_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "origin"

            def git(*args, env=None):
                return subprocess.run(["git", *map(str, args)], env=env, capture_output=True, text=True)

            self.assertEqual(git("init", "-q", repo).returncode, 0)
            (repo / "dependency.txt").write_text("needed by Lake\n")
            git("-C", repo, "add", ".")
            result = git(
                "-C", repo, "-c", "user.name=test", "-c", "user.email=test@example.org", "commit", "-qm", "dependency"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            git("-C", repo, "config", "uploadpack.allowFilter", "true")
            partial = root / "partial"
            result = git("clone", "--no-checkout", "--no-local", "--filter=tree:0", repo, partial)
            self.assertEqual(result.returncode, 0, result.stderr)
            for purpose, success in [("source", False), ("workspace", True)]:
                env = scoped_git_environment(
                    Forge("forgejo", "https://git.example.org"), "private-token", parent=os.environ, purpose=purpose
                )
                result = git("-C", partial, "-c", "protocol.file.allow=always", "checkout", "--force", "HEAD", env=env)
                self.assertEqual(result.returncode == 0, success, result.stderr)
            self.assertEqual((partial / "dependency.txt").read_text(), "needed by Lake\n")


if __name__ == "__main__":
    unittest.main()
