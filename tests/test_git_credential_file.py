"""Workspace Git authenticates lazily to its origin without exporting bearer data."""

import base64
import subprocess
import tempfile
import unittest
from pathlib import Path

from tauceti_tools.forge import Forge
from tauceti_tools.git_credentials import scoped_git_environment


class CredentialFile(unittest.TestCase):
    def test_real_git_credential_protocol_selects_exact_origin_and_keeps_environment_secret_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "private token"
            token.write_text("fixture-secret\n")
            token.chmod(0o600)
            env = scoped_git_environment(
                Forge("forgejo", "https://git.example.org"), token_file=token, purpose="workspace"
            )
            values = "\n".join(env.values())
            self.assertNotIn("fixture-secret", values)
            self.assertNotIn(base64.b64encode(b"x-access-token:fixture-secret").decode(), values)
            self.assertNotIn(".extraheader", values)
            shadow = root / "tauceti_tools"
            shadow.mkdir()
            (shadow / "__init__.py").write_text("raise RuntimeError('untrusted checkout package executed')")
            for host, success in [
                ("git.example.org", True),
                ("other.example.org", False),
                ("git.example.org.evil", False),
                ("git.example.org:444", False),
            ]:
                result = subprocess.run(
                    ["git", "credential", "fill"],
                    input=f"protocol=https\nhost={host}\npath=team/project.git\n\n",
                    env=env,
                    cwd=root,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode == 0, success, result.stderr)
                if success:
                    self.assertIn("password=fixture-secret", result.stdout)
                else:
                    self.assertNotIn("fixture-secret", result.stdout + result.stderr)
            # Local cached package clones do not even open the credential file.
            token.unlink()
            subprocess.run(["git", "init", "-q", "--bare", str(root / "package")], check=True)
            local = subprocess.run(
                ["git", "clone", str(root / "package"), str(root / "clone")], env=env, capture_output=True, text=True
            )
            self.assertEqual(local.returncode, 0, local.stderr)

    def test_private_owned_regular_file_required_on_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("private-secret")
            token = root / "token"
            for unsafe in ("public", "symlink"):
                if unsafe == "public":
                    token.write_text("private-secret")
                    token.chmod(0o644)
                else:
                    token.unlink()
                    target.chmod(0o600)
                    token.symlink_to(target)
                env = scoped_git_environment(
                    Forge("forgejo", "https://git.example.org"), token_file=token, purpose="workspace"
                )
                result = subprocess.run(
                    ["git", "credential", "fill"],
                    input="protocol=https\nhost=git.example.org\n\n",
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("private-secret", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
