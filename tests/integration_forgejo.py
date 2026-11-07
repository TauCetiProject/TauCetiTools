"""Disposable real-server smoke: FORGEJO_BINARY=/path/to/forgejo python tests/integration_forgejo.py.

All state/credentials stay in a private temporary directory. The service binds
loopback, disables Actions/registration/SSH, and is stopped even on test failure.
"""

import base64
import contextlib
import getpass
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_tools.forge import Forge, ForgeError, Forgejo, RepositoryRef
from tauceti_tools.forge_cli import run


class Integration(unittest.TestCase):
    def test_real_pr_and_cli(self):
        binary = os.environ.get("FORGEJO_BINARY")
        if not binary:
            self.skipTest("set FORGEJO_BINARY to run a disposable server")
        with tempfile.TemporaryDirectory(prefix="tauceti-forgejo-") as tmp:
            root = Path(tmp)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            config = root / "app.ini"
            config.write_text(f"""RUN_USER = {getpass.getuser()}
WORK_PATH = {root}
[server]
HTTP_ADDR = 127.0.0.1
HTTP_PORT = {port}
ROOT_URL = {url}/
DISABLE_SSH = true
APP_DATA_PATH = {root}/data
[database]
DB_TYPE = sqlite3
PATH = {root}/db.sqlite
[security]
INSTALL_LOCK = true
[repository]
ROOT = {root}/repos
DEFAULT_BRANCH = main
[service]
DISABLE_REGISTRATION = true
[actions]
ENABLED = false
[log]
MODE = file
ROOT_PATH = {root}/log
""")
            base = [binary, "--work-path", tmp, "--config", str(config)]

            def admin(*args):
                result = subprocess.run([*base, *args], capture_output=True, text=True, timeout=60)
                # CLI output can contain randomly generated credentials; never echo it.
                self.assertEqual(
                    result.returncode, 0, "Forgejo setup command failed (credential-bearing output withheld)"
                )
                return result.stdout

            admin("migrate")
            admin(
                "admin",
                "user",
                "create",
                "--username",
                "operator",
                "--email",
                "operator@example.invalid",
                "--admin",
                "--random-password",
                "--must-change-password=false",
            )
            output = admin(
                "admin",
                "user",
                "generate-access-token",
                "--username",
                "operator",
                "--token-name",
                "rehearsal",
                "--scopes",
                "all",
                "--raw",
            )
            tokens = re.findall(r"(?m)^[a-f0-9]{40}$", output)
            self.assertEqual(len(tokens), 1, "expected one local token (output withheld)")
            token = tokens[0]
            token_file = root / "token"
            token_file.write_text(token)
            token_file.chmod(0o600)
            api = Forgejo(Forge("forgejo", url), token=token, timeout=3)
            with (root / "server.log").open("w") as logs:
                process = subprocess.Popen([*base, "web"], stdout=logs, stderr=logs)
                try:
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            api.request("GET", "version")
                            break
                        except ForgeError:
                            if time.monotonic() > deadline or process.poll() is not None:
                                self.fail("local Forgejo did not start")
                            time.sleep(0.1)
                    api.request("POST", "user/repos", {"name": "content", "auto_init": True})
                    api.request(
                        "POST",
                        "repos/operator/content/branches",
                        {"new_branch_name": "proof", "old_branch_name": "main"},
                    )
                    api.request(
                        "POST",
                        "repos/operator/content/contents/Proof.lean",
                        {
                            "branch": "proof",
                            "message": "Add proof",
                            "content": base64.b64encode(b"example : True := trivial\n").decode(),
                        },
                    )
                    pull = api.request(
                        "POST", "repos/operator/content/pulls", {"head": "proof", "base": "main", "title": "Proof"}
                    )
                    repo = RepositoryRef(api.forge, "operator/content")
                    api.post_comment(repo, pull["number"], "Review finding")
                    self.assertEqual(len(api.pulls(repo)), 1)
                    review = api.request(
                        "POST",
                        f"repos/operator/content/pulls/{pull['number']}/reviews",
                        {
                            "event": "COMMENT",
                            "commit_id": pull["head"]["sha"],
                            "body": "Review",
                            "comments": [{"path": "Proof.lean", "new_position": 1, "body": "Original finding"}],
                        },
                    )
                    review_path = f"repos/operator/content/pulls/{pull['number']}/reviews/{review['id']}/comments"
                    code_comment = api.request("GET", review_path)[0]
                    api.edit_comment(repo, code_comment["id"], "Updated finding")
                    self.assertEqual(api.request("GET", review_path)[0]["body"], "Updated finding")
                    with self.assertRaises(ForgeError) as forbidden:
                        Forgejo(api.forge).edit_comment(repo, code_comment["id"], "Unauthorized replacement")
                    self.assertIn(forbidden.exception.status, {401, 403})
                    self.assertEqual(api.request("GET", review_path)[0]["body"], "Updated finding")
                    project = root / "project.toml"
                    project.write_text(f'''schema_version=2
[forges.local]
kind="forgejo"
url="{url}"
[repositories]
content={{forge="local", name="operator/content"}}
roadmap="operator/roadmap"
''')
                    with contextlib.redirect_stdout(io.StringIO()) as rendered:
                        self.assertEqual(
                            run(
                                [
                                    "--conf",
                                    str(project),
                                    "pr",
                                    "view",
                                    str(pull["number"]),
                                    "--json",
                                    "number,title,comments",
                                ],
                                environment={"TAUCETI_FORGEJO_TOKEN_FILE": str(token_file)},
                            ),
                            0,
                        )
                    result = json.loads(rendered.getvalue())
                    self.assertEqual(result["title"], "Proof")
                    self.assertEqual(result["comments"][0]["body"], "Review finding")
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
