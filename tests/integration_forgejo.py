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
from tauceti_tools.forge_read import ForgejoReader


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
[api]
MAX_RESPONSE_ITEMS = 1
DEFAULT_PAGING_NUM = 1
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
            admin(
                "admin",
                "user",
                "create",
                "--username",
                "reviewer",
                "--email",
                "reviewer@example.invalid",
                "--random-password",
                "--must-change-password=false",
            )
            output = admin(
                "admin",
                "user",
                "generate-access-token",
                "--username",
                "reviewer",
                "--token-name",
                "review",
                "--scopes",
                "all",
                "--raw",
            )
            reviewer_tokens = re.findall(r"(?m)^[a-f0-9]{40}$", output)
            self.assertEqual(len(reviewer_tokens), 1, "expected one reviewer token (output withheld)")
            token_file = root / "token"
            token_file.write_text(token)
            token_file.chmod(0o600)
            reviewer_token_file = root / "reviewer-token"
            reviewer_token_file.write_text(reviewer_tokens[0])
            reviewer_token_file.chmod(0o600)
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
                    self.assertEqual(api.current_user()["login"], "operator")
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
                    reader = ForgejoReader(api)
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
                    code_comment = api.review_comments(repo, pull["number"], review["id"])[0]
                    api.edit_comment(repo, code_comment["id"], "Updated finding")
                    self.assertEqual(api.request("GET", review_path)[0]["body"], "Updated finding")
                    api.request(
                        "POST",
                        f"repos/operator/content/pulls/{pull['number']}/requested_reviewers",
                        {"reviewers": ["reviewer"]},
                    )
                    reviews = reader.pull(repo, pull["number"], ["reviews"])["reviews"]
                    self.assertEqual([r["state"] for r in reviews], ["COMMENTED"])
                    reviewer = Forgejo(api.forge, token=reviewer_tokens[0])
                    self.assertEqual(reviewer.current_user()["login"], "reviewer")
                    reviewer.request(
                        "POST",
                        f"repos/operator/content/pulls/{pull['number']}/reviews",
                        {"event": "APPROVED", "commit_id": pull["head"]["sha"], "body": "Checked"},
                    )
                    self.assertEqual(
                        {r["state"] for r in reader.pull(repo, pull["number"], ["reviews"])["reviews"]},
                        {"COMMENTED", "APPROVED"},
                    )
                    # The hidden draft occupies a whole native page; its empty
                    # visible page must not hide a later submitted review.
                    api.request(
                        "POST",
                        f"repos/operator/content/pulls/{pull['number']}/reviews",
                        {"event": "PENDING", "body": "Operator draft"},
                    )
                    reviewer.request(
                        "POST",
                        f"repos/operator/content/pulls/{pull['number']}/reviews",
                        {"event": "COMMENT", "body": "Later submitted review"},
                    )
                    public_reviews = ForgejoReader(reviewer).pull(repo, pull["number"], ["reviews"])["reviews"]
                    self.assertTrue(any(r["body"] == "Later submitted review" for r in public_reviews))
                    self.assertTrue(all(r["state"] != "PENDING" for r in public_reviews))
                    for state in ["pending", "success"]:
                        api.request(
                            "POST",
                            f"repos/operator/content/statuses/{pull['head']['sha']}",
                            {"state": state, "context": "build", "description": "Exact candidate"},
                        )
                    details = reader.pull(
                        repo, pull["number"], ["files", "statusCheckRollup", "headRepositoryOwner", "baseRefOid"]
                    )
                    self.assertEqual(details["files"], [{"path": "Proof.lean", "additions": 1, "deletions": 0}])
                    self.assertEqual(
                        [(s["context"], s["state"]) for s in details["statusCheckRollup"]], [("build", "SUCCESS")]
                    )
                    self.assertEqual(details["headRepositoryOwner"]["login"], "operator")
                    labels = [
                        api.request("POST", "repos/operator/content/labels", {"name": name, "color": "123456"})
                        for name in ["bug", "ready"]
                    ]
                    first_issue = api.request(
                        "POST", "repos/operator/content/issues", {"title": "One label", "labels": [labels[0]["id"]]}
                    )
                    second_issue = api.request(
                        "POST",
                        "repos/operator/content/issues",
                        {"title": "Both labels", "labels": [label["id"] for label in labels]},
                    )
                    api.post_comment(repo, second_issue["number"], "Issue finding")
                    # Issue APIs must not include PRs; native label filtering is OR,
                    # while our gh contract requires every requested label.
                    issues = reader.issues(repo, ["number", "title"], labels=["bug", "ready"], limit=1)
                    self.assertEqual(issues, [{"number": second_issue["number"], "title": "Both labels"}])
                    self.assertEqual(
                        [r["number"] for r in api.issues(repo)], [second_issue["number"], first_issue["number"]]
                    )
                    # A second base branch permits another PR from the same head.
                    api.request(
                        "POST",
                        "repos/operator/content/branches",
                        {"new_branch_name": "other-base", "old_branch_name": "main"},
                    )
                    second_pull = api.request(
                        "POST",
                        "repos/operator/content/pulls",
                        {"head": "proof", "base": "other-base", "title": "Another proof", "draft": True},
                    )
                    prefix = Forgejo(api.forge, token=token, max_pages=1).pulls(repo, limit=1)
                    self.assertEqual([r["number"] for r in prefix], [second_pull["number"]])
                    self.assertEqual(api.pulls(repo, head="missing-branch", limit=1), [])
                    self.assertEqual(
                        [r["number"] for r in api.pulls(repo, head="proof", base="main", limit=1)], [pull["number"]]
                    )
                    reviewer.request(
                        "POST",
                        f"repos/operator/content/pulls/{second_pull['number']}/reviews",
                        {"event": "PENDING", "body": "Private draft"},
                    )
                    self.assertEqual(reader.pull(repo, second_pull["number"], ["reviews"]), {"reviews": []})
                    self.assertEqual(
                        ForgejoReader(reviewer).pull(repo, second_pull["number"], ["reviews"])["reviews"][0]["state"],
                        "PENDING",
                    )
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
                                environment={
                                    "TAUCETI_FORGEJO_TOKEN_FILE": str(token_file),
                                    "TAUCETI_FORGEJO_ORIGIN": url,
                                    "GH_REPO": "operator/content",
                                },
                            ),
                            0,
                        )
                    result = json.loads(rendered.getvalue())
                    self.assertEqual(result["title"], "Proof")
                    self.assertEqual(result["comments"][0]["body"], "Review finding")

                    def cli(*args):
                        with contextlib.redirect_stdout(io.StringIO()) as rendered:
                            self.assertEqual(
                                run(
                                    ["--conf", str(project), *args],
                                    environment={
                                        "TAUCETI_FORGEJO_TOKEN_FILE": str(token_file),
                                        "TAUCETI_FORGEJO_ORIGIN": url,
                                        "GH_REPO": "operator/content",
                                    },
                                ),
                                0,
                            )
                        return json.loads(rendered.getvalue())

                    self.assertEqual(
                        cli("pr", "list", "-B", "main", "-A", "operator", "-L", "1", "--json", "number"),
                        [{"number": pull["number"]}],
                    )
                    self.assertEqual(
                        cli("issue", "list", "-l", "bug", "-l", "ready", "--json", "number"),
                        [{"number": second_issue["number"]}],
                    )
                    self.assertEqual(
                        cli("issue", "view", str(second_issue["number"]), "--json", "comments")["comments"][0]["body"],
                        "Issue finding",
                    )
                    self.assertEqual(
                        cli("repo", "view", "--json", "defaultBranchRef,viewerPermission"),
                        {"defaultBranchRef": {"name": "main"}, "viewerPermission": "ADMIN"},
                    )

                    def native(*args, review=False):
                        with contextlib.redirect_stdout(io.StringIO()) as rendered:
                            self.assertEqual(
                                run(
                                    ["--conf", str(project), *args],
                                    environment={
                                        "TAUCETI_FORGEJO_TOKEN_FILE": str(
                                            reviewer_token_file if review else token_file
                                        ),
                                        "TAUCETI_FORGEJO_ORIGIN": url,
                                        "GH_REPO": "operator/content",
                                    },
                                ),
                                0,
                            )
                        return rendered.getvalue()

                    native("label", "create", "cli", "--color", "123456", "--description", "--repo=literal")
                    native("label", "create", "cli", "--force", "--color", "654321")
                    native("label", "edit", "cli", "--name", "cli-renamed", "--description", "Updated")
                    listed_labels = json.loads(native("label", "list", "--json", "name,color,description"))
                    self.assertIn({"name": "cli-renamed", "color": "654321", "description": "Updated"}, listed_labels)
                    issue_url = native("issue", "create", "-t", "CLI issue", "-b", "-R", "-l", "bug").strip()
                    issue_number = int(issue_url.rsplit("/", 1)[1])
                    self.assertEqual(api.issue(repo, issue_number)["body"], "-R")
                    body_file = root / "body.txt"
                    body_file.write_text("Literal --repo and --conf\nSecond line: λ\n")
                    native(
                        "issue",
                        "edit",
                        str(issue_number),
                        "-F",
                        str(body_file),
                        "--add-label",
                        "ready",
                        "--remove-label",
                        "bug",
                    )
                    native("issue", "comment", str(issue_number), "--body", "--conf=literal")
                    native("issue", "close", str(issue_number), "--comment", "Close finding")
                    self.assertEqual(api.issue(repo, issue_number)["state"], "closed")
                    native("issue", "reopen", str(issue_number))
                    api.request(
                        "POST",
                        "repos/operator/content/branches",
                        {"new_branch_name": "cli-base", "old_branch_name": "main"},
                    )
                    api.request(
                        "POST",
                        "repos/operator/content/contents/CLI.lean",
                        {
                            "branch": "main",
                            "new_branch": "cli-proof",
                            "message": "CLI proof",
                            "content": base64.b64encode(b"example : True := trivial\n").decode(),
                        },
                    )
                    pr_url = native(
                        "pr",
                        "create",
                        "--head",
                        "cli-proof",
                        "--base",
                        "cli-base",
                        "-t",
                        "CLI proof",
                        "-F",
                        str(body_file),
                        "-l",
                        "bug",
                        "--draft",
                    ).strip()
                    pr_number = int(pr_url.rsplit("/", 1)[1])
                    self.assertTrue(api.pull(repo, pr_number)["draft"])
                    native(
                        "pr", "edit", str(pr_number), "-t", "CLI proof", "--add-label", "ready", "--remove-label", "bug"
                    )
                    self.assertFalse(api.pull(repo, pr_number)["draft"])
                    native("pr", "review", str(pr_number), "--approve", review=True)
                    native("pr", "review", str(pr_number), "--request-changes", "-b", "--repo=literal", review=True)
                    native("pr", "comment", str(pr_number), "-b", "-R")
                    native("pr", "close", str(pr_number), "--comment", "Close finding")
                    self.assertEqual(api.pull(repo, pr_number)["state"], "closed")
                    native("pr", "reopen", str(pr_number))
                    state = cli("pr", "view", str(pr_number), "--json", "body,labels,reviews")
                    self.assertEqual(state["body"], body_file.read_text())
                    self.assertEqual([label["name"] for label in state["labels"]], ["ready"])
                    self.assertIn("CHANGES_REQUESTED", {row["state"] for row in state["reviews"]})
                    fork_url = native(
                        "repo", "fork", "--clone=false", "--remote=false", "--fork-name", "cli-fork", review=True
                    ).strip()
                    self.assertEqual(fork_url, url + "/reviewer/cli-fork")
                    self.assertEqual(
                        native("repo", "fork", "--clone=false", "--fork-name", "cli-fork", review=True).strip(),
                        fork_url,
                    )
                    forks = json.loads(native("repo", "list", "--fork", "--json", "nameWithOwner,parent", review=True))
                    self.assertEqual(forks[0]["nameWithOwner"], "reviewer/cli-fork")
                    self.assertEqual(forks[0]["parent"]["owner"]["login"], "operator")
                    self.assertIn("as reviewer", native("auth", "status", review=True))
                    api.request("DELETE", "repos/reviewer/cli-fork")
                    api.request(
                        "POST",
                        "repos/operator/content/contents",
                        {
                            "branch": "main",
                            "new_branch": "many-files",
                            "message": "Exercise file pagination",
                            "files": [
                                {
                                    "operation": "create",
                                    "path": f"proofs/Proof{i:03}.lean",
                                    "content": base64.b64encode(b"example : True := trivial\n").decode(),
                                }
                                for i in range(65)
                            ],
                        },
                    )
                    many = api.request(
                        "POST",
                        "repos/operator/content/pulls",
                        {"head": "many-files", "base": "main", "title": "Many files"},
                    )
                    self.assertEqual(len(reader.pull(repo, many["number"], ["files"])["files"]), 65)
                    api.request("PATCH", f"repos/operator/content/pulls/{second_pull['number']}", {"state": "closed"})
                    api.request(
                        "POST",
                        f"repos/operator/content/pulls/{pull['number']}/merge",
                        {"Do": "merge", "head_commit_id": pull["head"]["sha"]},
                    )
                    self.assertEqual([r["number"] for r in api.pulls(repo, state="closed")], [second_pull["number"]])
                    self.assertEqual([r["number"] for r in api.pulls(repo, state="merged")], [pull["number"]])
                    api.request("DELETE", "repos/operator/content/branches/proof")
                    self.assertEqual(reader.pull(repo, pull["number"], ["headRefName"]), {"headRefName": "proof"})
                    reviewer.request("POST", "repos/operator/content/forks", {})
                    reviewer.request(
                        "POST",
                        "repos/reviewer/content/branches",
                        {"new_branch_name": "fork-proof", "old_branch_name": "main"},
                    )
                    reviewer.request(
                        "POST",
                        "repos/reviewer/content/contents/Fork.lean",
                        {
                            "branch": "fork-proof",
                            "message": "Fork proof",
                            "content": base64.b64encode(b"example : True := trivial\n").decode(),
                        },
                    )
                    fork_pull = reviewer.request(
                        "POST",
                        "repos/operator/content/pulls",
                        {"head": "reviewer:fork-proof", "base": "main", "title": "Fork proof"},
                    )
                    api.request(
                        "POST",
                        f"repos/operator/content/pulls/{fork_pull['number']}/merge",
                        {"Do": "merge", "head_commit_id": fork_pull["head"]["sha"]},
                    )
                    reviewer.request("DELETE", "repos/reviewer/content/branches/fork-proof")
                    # Forgejo's converter now serializes failed GetShortStat as
                    # zeros. The reader must recover the retained PR's real diff.
                    native = api.pull(repo, fork_pull["number"])
                    self.assertEqual(
                        {key: native[key] for key in ("changed_files", "additions", "deletions")},
                        {"changed_files": 0, "additions": 0, "deletions": 0},
                    )
                    self.assertEqual(
                        reader.pull(repo, fork_pull["number"], ["changedFiles", "additions", "deletions"]),
                        {"changedFiles": 1, "additions": 1, "deletions": 0},
                    )
                    api.request("DELETE", "repos/reviewer/content")
                    reviewer.post_comment(repo, pull["number"], "Preserved after account deletion")
                    api.request("DELETE", "admin/users/reviewer")
                    comments = reader.pull(repo, pull["number"], ["comments"])["comments"]
                    self.assertEqual(comments[-1]["author"], {"login": "Ghost"})
                    self.assertEqual(api.issues(repo, author="definitely-unknown-user"), [])
                    self.assertEqual(api.pulls(repo, author="definitely-unknown-user"), [])
                    api.request(
                        "POST",
                        "repos/operator/content/branches",
                        {"new_branch_name": "proof", "old_branch_name": "main"},
                    )
                    self.assertNotEqual(api.pull(repo, pull["number"])["head"]["sha"], pull["head"]["sha"])
                    for field in ["changedFiles", "additions", "headRefOid", "files", "statusCheckRollup"]:
                        with self.subTest(moved_closed_field=field), self.assertRaises(ForgeError):
                            reader.pull(repo, pull["number"], [field])
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
