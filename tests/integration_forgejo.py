"""Disposable real-server smoke: FORGEJO_BINARY=/path/to/forgejo python tests/integration_forgejo.py.

All state/credentials stay in a private temporary directory. The service binds
loopback, disables Actions/registration/SSH, and is stopped even on test failure.
"""

import base64
import contextlib
import getpass
import hashlib
import http.cookiejar
import io
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_tools.forge import Forge, ForgeError, Forgejo, Incomplete, RepositoryRef
from tauceti_tools.forge_cli import run
from tauceti_tools.forge_read import ForgejoReader
from tauceti_tools import ci_candidate, ci_candidate_workflow
from tauceti_tools.forge import GITHUB


class Integration(unittest.TestCase):
    def test_real_pr_and_cli(self):
        binary = os.environ.get("FORGEJO_BINARY")
        retained_metadata = os.environ.get("FORGEJO_RETAINED_METADATA") == "1"
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
[repository.pull-request]
WORK_IN_PROGRESS_PREFIXES = WIP:
[service]
DISABLE_REGISTRATION = true
[actions]
ENABLED = false
[api]
MAX_RESPONSE_ITEMS = 1
DEFAULT_PAGING_NUM = 1
DEFAULT_GIT_TREES_PER_PAGE = 1
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
            reviewer_password = "local-fixture-only-" + secrets.token_urlsafe(24)
            admin(
                "admin",
                "user",
                "create",
                "--username",
                "reviewer",
                "--email",
                "reviewer@example.invalid",
                "--password",
                reviewer_password,
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
            limited_output = admin(
                "admin",
                "user",
                "generate-access-token",
                "--username",
                "operator",
                "--token-name",
                "cli-limited",
                "--scopes",
                "read:user,write:repository,write:issue",
                "--raw",
            )
            limited_tokens = re.findall(r"(?m)^[a-f0-9]{40}$", limited_output)
            self.assertEqual(len(limited_tokens), 1, "expected one scoped token (output withheld)")
            token_file.write_text(limited_tokens[0])
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
                    # Trusted CI helpers fetch complete histories directly from a
                    # private source, with a source-only read token. No GitHub twin
                    # or candidate-controlled Git metadata supplies these objects.
                    ci_scripts = Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts"
                    with patch.object(sys, "path", [str(ci_scripts), *sys.path]):
                        import ci_checkout
                        import ci_git
                    api.request("POST", "user/repos", {"name": "ci-source", "private": True, "auto_init": True})
                    api.request(
                        "POST",
                        "repos/operator/ci-source/branches",
                        {
                            "new_branch_name": "candidate",
                            "old_branch_name": "main",
                        },
                    )
                    api.request(
                        "POST",
                        "repos/operator/ci-source/contents/Source.lean",
                        {
                            "branch": "candidate",
                            "message": "CI source candidate",
                            "content": base64.b64encode(b"example : True := trivial\n").decode(),
                        },
                    )
                    api.request("POST", "repos/operator/ci-source/contents/lean-toolchain", {
                        "branch": "candidate", "message": "Pin the candidate toolchain",
                        "content": base64.b64encode(b"leanprover/lean4:v4.35.0\n").decode(),
                    })
                    ci_pull = api.request(
                        "POST",
                        "repos/operator/ci-source/pulls",
                        {
                            "head": "candidate",
                            "base": "main",
                            "title": "CI source candidate",
                        },
                    )
                    native_repo = RepositoryRef(api.forge, "operator/ci-source")
                    native_token_output = admin(
                        "admin",
                        "user",
                        "generate-access-token",
                        "--username",
                        "operator",
                        "--token-name",
                        "ci-source-reader",
                        "--scopes",
                        "read:repository",
                        "--raw",
                    )
                    native_tokens = re.findall(r"(?m)^[a-f0-9]{40}$", native_token_output)
                    self.assertEqual(len(native_tokens), 1, "expected one source-only read token (output withheld)")
                    ci_head, ci_base = ci_pull["head"]["sha"], ci_pull["base"]["sha"]
                    resolved = ci_git.resolve(
                        native_repo,
                        native_repo,
                        ci_head,
                        "main",
                        root / "ci-resolver.git",
                        staging_ref="candidate",
                        token=native_tokens[0],
                        credential_origin=api.forge,
                    )
                    self.assertEqual(resolved["base_sha"], ci_base)
                    self.assertEqual(resolved["mergebase"], ci_base)
                    # Advance main after resolution: complete fetch/materialization
                    # must retain B, rather than silently reading a newer main.
                    api.request(
                        "POST",
                        "repos/operator/ci-source/contents/Later.lean",
                        {
                            "branch": "main",
                            "message": "Main advanced",
                            "content": base64.b64encode(b"example : True := trivial\n").decode(),
                        },
                    )
                    ci_job = root / "ci-job"
                    ci_job.mkdir()
                    fetched = ci_checkout.fetch_sources(
                        native_repo,
                        native_repo,
                        ci_head,
                        ci_base,
                        ci_base,
                        ci_job,
                        token=native_tokens[0],
                        credential_origin=api.forge,
                        attempts=1,
                    )
                    for role, commit in [("head", ci_head), ("base", ci_base), ("mergebase", ci_base)]:
                        ci_checkout.materialize(ci_job, role, commit)
                    self.assertEqual((ci_job / "pr/Source.lean").read_text(), "example : True := trivial\n")
                    self.assertFalse((ci_job / "base/Source.lean").exists())
                    self.assertFalse((ci_job / "base/Later.lean").exists())
                    identity = json.loads((Path(fetched["store"]) / "identity.json").read_text())
                    self.assertEqual(identity["schema"], 2)
                    self.assertEqual(identity["repository"]["forge"]["url"], api.forge.url)
                    self.assertNotIn(native_tokens[0], (Path(fetched["store"]) / "config").read_text())
                    # Match the reusable workflow bootstrap: the helpers run
                    # without installed packages, using only the pinned trusted
                    # Tools checkout, even from an unrelated working directory.
                    cli_env = dict(os.environ, PYTHONPATH=str(ci_scripts.parents[2]),
                                   CI_SOURCE_GIT_TOKEN=native_tokens[0],
                                   CI_GIT_TOKEN="execution-token-must-not-be-used", GH_TOKEN="another-execution-token")
                    cli_job = root / "ci-cli-job"
                    cli_job.mkdir()
                    source_args = ["--repository", native_repo.name, "--head-repository", native_repo.name,
                                   "--head", ci_head, "--source-kind", "forgejo", "--source-origin", api.forge.url,
                                   "--credential-origin", api.forge.url]
                    commands = [
                        [str(ci_scripts / "ci_git.py"), "resolve", *source_args, "--base-ref", ci_base,
                         "--staging-ref", "candidate", "--store", str(root / "ci-cli-resolver.git")],
                        [str(ci_scripts / "ci_checkout.py"), "fetch", *source_args, "--base", ci_base,
                         "--mergebase", ci_base, "--workspace", str(cli_job)],
                        [str(ci_scripts / "ci_checkout.py"), "materialize", "--role", "head",
                         "--commit", ci_head, "--workspace", str(cli_job)],
                    ]
                    for command in commands:
                        child = subprocess.run([sys.executable, "-B", "-S", *command], cwd=root, env=cli_env,
                                               capture_output=True, text=True, timeout=30)
                        self.assertEqual(child.returncode, 0, "qualified CLI failed; credential diagnostics withheld")
                        self.assertIsInstance(json.loads(child.stdout), dict)
                        self.assertNotIn(native_tokens[0], child.stdout + child.stderr)
                        self.assertNotIn(base64.b64encode(f"x-access-token:{native_tokens[0]}".encode()).decode(),
                                         child.stdout + child.stderr)
                    self.assertEqual((cli_job / "pr/Source.lean").read_text(), "example : True := trivial\n")

                    source_metadata = api.request("GET", "repos/operator/ci-source")
                    with ci_git.object_reader(ci_job / "pr") as ci_reader:
                        ci_tree = ci_reader.run("show", "-s", "--format=%T", ci_head).stdout.decode().strip()
                    candidate_policy = ci_candidate.ExecutionPolicy(
                        ci_candidate.RepositoryIdentity(native_repo, source_metadata["id"]),
                        ci_candidate.RepositoryIdentity(RepositoryRef(GITHUB, "example/Execution"), 42),
                        api.forge, ".github/workflows/pr-build.yml",
                        ci_candidate.RepositoryIdentity(RepositoryRef(GITHUB, "example/Tools"), 43),
                        "a" * 40, "main", "main", True, "1" * 64,
                    )
                    candidate_configuration = root / "ci-source-project.toml"
                    candidate_configuration.write_text(
                        'schema_version=2\n[forges.native]\nkind="forgejo"\nurl=' + json.dumps(api.forge.url)
                        + '\n[repositories]\ncontent={forge="native",name="operator/ci-source"}\n'
                        + 'roadmap={forge="native",name="operator/ci-source"}\n'
                        + '[lean]\nroots=["Source"]\n[ci]\nbackend="github-actions"\nrepository="example/Execution"\n'
                    )
                    candidate_policy = ci_candidate.ExecutionPolicy.parse(
                        candidate_policy.record(with_config=False), configuration=candidate_configuration.read_bytes(),
                    )
                    candidate_request = ci_candidate.CandidateRequest(
                        "1" * 32, "2" * 32, candidate_policy.digest, candidate_policy.source,
                        candidate_policy.execution, "b" * 40, candidate_policy.harness_revision,
                        candidate_policy.config_digest, 1, 1, ci_head, ci_base, ci_tree,
                        (ci_candidate.Member(ci_pull["number"], ci_head),),
                    )
                    candidate_context = {
                        "event_name": "repository_dispatch", "action": ci_candidate.EVENT,
                        "repository": "example/Execution", "repository_id": "42", "workflow_sha": "b" * 40,
                        "workflow_ref": "example/Execution/.github/workflows/pr-build.yml@refs/heads/main",
                        "harness_repository": "example/Tools", "harness_sha": "a" * 40,
                        "harness_file": ".github/workflows/pr-build.yml", "run_id": 101, "attempt": 1,
                    }
                    for retained in (candidate_request.retained_ref, candidate_request.member_ref(candidate_request.members[0])):
                        api.request("POST", "repos/operator/ci-source/branches", {
                            "new_branch_name": retained.removeprefix("refs/heads/"), "old_branch_name": "candidate",
                        })
                    resolved_candidate = ci_candidate_workflow.resolve(
                        candidate_request, candidate_policy, candidate_context, root / "ci-qualified-request.git",
                        token=native_tokens[0],
                    )
                    self.assertEqual(resolved_candidate["request_digest"], candidate_request.digest)
                    self.assertEqual(resolved_candidate["tree"], ci_tree)
                    request_job = root / "ci-qualified-request-job"
                    request_job.mkdir()
                    ci_candidate_workflow.fetch(
                        candidate_request, candidate_policy, candidate_context, request_job, token=native_tokens[0],
                    )
                    ci_checkout.materialize(request_job, "head", ci_head)
                    self.assertEqual((request_job / "pr/Source.lean").read_text(), "example : True := trivial\n")
                    self.assertFalse((request_job / "pr/Later.lean").exists())
                    publisher_context = dict(candidate_context, harness_file=".github/workflows/publish-lake-cache.yml")
                    pin = ci_candidate_workflow.toolchain_pin(
                        candidate_request, candidate_policy, publisher_context, root / "ci-publisher-pin.git",
                        token=native_tokens[0],
                    )
                    self.assertEqual(pin, b"leanprover/lean4:v4.35.0\n")
                    candidate_policy_path, candidate_event = root / "ci-source-policy.json", root / "ci-source-event.json"
                    candidate_policy_path.write_bytes(ci_candidate.canonical(candidate_policy.record(with_config=False)))
                    candidate_event.write_bytes(ci_candidate.canonical({"action": ci_candidate.EVENT, "client_payload": candidate_request.dispatch()["client_payload"]}))
                    ingress_env = dict(os.environ, PYTHONPATH=str(ci_scripts.parents[2]),
                        CI_SOURCE_READ_TOKEN=native_tokens[0], GH_TOKEN="github-token-must-not-be-used",
                        GITHUB_EVENT_NAME="repository_dispatch", CI_SOURCE_EVENT_ACTION=ci_candidate.EVENT,
                        GITHUB_REPOSITORY="example/Execution", GITHUB_REPOSITORY_ID="42",
                        GITHUB_WORKFLOW_SHA="b" * 40,
                        GITHUB_WORKFLOW_REF="example/Execution/.github/workflows/pr-build.yml@refs/heads/main",
                        GITHUB_RUN_ID="101", GITHUB_RUN_ATTEMPT="1",
                        CI_SOURCE_HARNESS_REPOSITORY="example/Tools", CI_SOURCE_HARNESS_SHA="a" * 40,
                        CI_SOURCE_HARNESS_FILE=".github/workflows/pr-build.yml")
                    child = subprocess.run([
                        sys.executable, "-B", "-S", "-m", "tauceti_tools.ci_candidate_workflow", "resolve",
                        "--policy", str(candidate_policy_path), "--configuration", str(candidate_configuration),
                        "--event", str(candidate_event), "--store", str(root / "ci-source-module-cli.git"),
                        "--output", str(root / "ci-source-module-output.json"),
                    ], cwd=root, env=ingress_env, capture_output=True, text=True, timeout=30)
                    self.assertEqual(child.returncode, 0, "source workflow module failed; credential diagnostics withheld")
                    self.assertEqual(json.loads((root / "ci-source-module-output.json").read_text())["request_digest"], candidate_request.digest)
                    self.assertNotIn(native_tokens[0], child.stdout + child.stderr)
                    lint_scope = ci_candidate_workflow.lint_scope(
                        candidate_request, candidate_policy, candidate_context, request_job / "pr",
                        root / "ci-native-lint-scope", candidate_configuration, observation=resolved_candidate,
                    )
                    self.assertEqual(lint_scope["LINT_ONLY_MODULES"], str(root / "ci-native-lint-scope/modules.txt"))
                    self.assertEqual((root / "ci-native-lint-scope/unmatched.txt").read_text(), "Source.lean\n")
                    native_full_lint = api.request("POST", "repos/operator/ci-source/labels", {"name": "full-lint", "color": "aabbcc"})
                    api.request("PUT", f"repos/operator/ci-source/issues/{ci_pull['number']}/labels", {"labels": [native_full_lint["id"]]})
                    lint_observation = dict(request_digest=candidate_request.digest,
                        lint_scoped=ci_candidate_workflow.native_lint_policy(candidate_request, candidate_policy, candidate_context,
                            client=Forgejo(api.forge, token=native_tokens[0])))
                    lint_scope = ci_candidate_workflow.lint_scope(
                        candidate_request, candidate_policy, candidate_context, request_job / "pr",
                        root / "ci-native-full-lint", candidate_configuration, observation=lint_observation,
                    )
                    self.assertEqual(lint_scope["LINT_ONLY_MODULES"], "")
                    self.assertEqual(ci_candidate_workflow.member_size_policy(candidate_request, candidate_policy,
                        candidate_context, request_job), {"SOURCE_SIZE_POLICY_COMPLETE": "1"})
                    from dataclasses import replace
                    # Membership can live in a retained data-only Git commit.
                    # Only resolve reads it from the native host; later steps
                    # verify the saved bytes with no native credential.
                    manifest_count = int(os.environ.get("FORGEJO_MANIFEST_MEMBERS", "1"))
                    self.assertTrue(1 <= manifest_count <= ci_candidate.MAX_RETAINED_MEMBERS)
                    manifest_members = tuple(ci_candidate.Member(number, ci_head)
                        for number in range(1, manifest_count + 1))
                    writer = ci_git.Git(root / "manifest-writer.git", root,
                        token=token, token_origin=api.forge, follow_redirects=False)
                    writer.env.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                        GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")

                    def write_git(*args, data=None):
                        try:
                            child = subprocess.run(writer.command + list(args), input=data,
                                env=writer.env, capture_output=True, timeout=30)
                        except subprocess.TimeoutExpired:
                            self.fail("manifest Git operation exceeded30seconds; credential diagnostics and arguments withheld")
                        self.assertEqual(child.returncode, 0, "manifest write failed; credential diagnostics withheld")
                        return child.stdout.decode().strip()

                    write_git("fetch", "--quiet", native_repo.clone_url, "candidate")
                    if os.environ.get("FORGEJO_MANIFEST_DISTINCT") == "1":
                        manifest_members = tuple(ci_candidate.Member(number,
                            write_git("commit-tree", ci_tree, "-p", ci_base,
                                data=f"Synthetic transport member {number}\n".encode()))
                            for number in range(1, manifest_count + 1))
                    member_raw = ci_candidate.member_manifest_bytes(candidate_request.epoch, "3" * 32, manifest_members)
                    member_blob = write_git("hash-object", "-w", "--stdin", data=member_raw)
                    member_tree = write_git("mktree", data=f"100644 blob {member_blob}\t{ci_candidate.MEMBER_MANIFEST_FILE}\n".encode())
                    parents = tuple(dict.fromkeys(member.head for member in manifest_members))
                    member_commit = write_git("commit-tree", member_tree,
                        *(arg for parent in parents for arg in ("-p", parent)), data=b"Retain accepted source heads\n")
                    retained_request = replace(candidate_request, request_id="3" * 32, members=manifest_members,
                        member_manifest=ci_candidate.MemberManifest(member_commit, hashlib.sha256(member_raw).hexdigest(),
                            len(manifest_members), len(member_raw)))
                    write_git("push", "--quiet", "--atomic", native_repo.clone_url,
                        f"{member_commit}:{retained_request.manifest_ref}", f"{ci_head}:{retained_request.retained_ref}")
                    unbound = ci_candidate.CandidateRequest.parse(retained_request.record(), policy=candidate_policy)
                    bound, actual_raw = ci_candidate_workflow.retained_members(unbound, candidate_policy,
                        candidate_context, token=native_tokens[0])
                    self.assertEqual(bound.accepted_members, retained_request.members)
                    self.assertEqual(actual_raw, member_raw)
                    bad_manifest = replace(unbound, member_manifest=replace(unbound.member_manifest, commit="9" * 40))
                    with self.assertRaisesRegex(ValueError, "commit differs"):
                        ci_candidate_workflow.retained_members(bad_manifest, candidate_policy,
                            candidate_context, token=native_tokens[0])
                    with self.assertRaises(subprocess.CalledProcessError):
                        ci_candidate_workflow.retained_members(replace(unbound, request_id="4" * 32),
                            candidate_policy, candidate_context, token=native_tokens[0])
                    candidate_event.write_bytes(ci_candidate.canonical({"action": ci_candidate.EVENT,
                        "client_payload": retained_request.dispatch()["client_payload"]}))
                    manifest_path = root / "verified-members.json"
                    manifest_job = root / "manifest-job"
                    manifest_job.mkdir()
                    common = ["--policy", str(candidate_policy_path), "--configuration", str(candidate_configuration),
                        "--event", str(candidate_event)]
                    commands = [
                        ["resolve", *common, "--store", str(root / "manifest-cli-resolve.git"),
                         "--members-output", str(manifest_path), "--output", str(root / "manifest-resolved.json")],
                        ["fetch", *common, "--workspace", str(manifest_job), "--members-file", str(manifest_path),
                         "--output", str(root / "manifest-fetched.json")],
                        ["member-policy", *common, "--workspace", str(manifest_job), "--members-file", str(manifest_path),
                         "--output", str(root / "manifest-policy.json")],
                    ]
                    manifest_metrics = {"members": manifest_count, "manifest_bytes": len(member_raw),
                        "dispatch_bytes": len(ci_candidate.canonical(retained_request.dispatch())), "seconds": {}}
                    for command in commands:
                        env = ingress_env if command[0] != "member-policy" else dict(ingress_env,
                            CI_SOURCE_READ_TOKEN="", GH_TOKEN="", GH_READ_TOKEN="")
                        started = time.monotonic()
                        child = subprocess.run([sys.executable, "-B", "-S", "-m", "tauceti_tools.ci_candidate_workflow", *command],
                            cwd=root, env=env, capture_output=True, text=True, timeout=30)
                        manifest_metrics["seconds"][command[0]] = time.monotonic() - started
                        self.assertEqual(child.returncode, 0, "retained membership CLI failed; credential diagnostics withheld")
                        self.assertNotIn(native_tokens[0], child.stdout + child.stderr)
                    self.assertEqual(manifest_path.read_bytes(), member_raw)
                    self.assertEqual(json.loads((root / "manifest-policy.json").read_text()), {"SOURCE_SIZE_POLICY_COMPLETE": "1"})
                    manifest_path.write_bytes(member_raw + b"\n")
                    child = subprocess.run([sys.executable, "-B", "-S", "-m", "tauceti_tools.ci_candidate_workflow", *commands[-1]],
                        cwd=root, env=dict(ingress_env, CI_SOURCE_READ_TOKEN="", GH_TOKEN="", GH_READ_TOKEN=""),
                        capture_output=True, text=True, timeout=30)
                    self.assertNotEqual(child.returncode, 0)
                    self.assertNotIn(native_tokens[0], child.stdout + child.stderr)
                    if os.environ.get("FORGEJO_MANIFEST_METRICS"):
                        Path(os.environ["FORGEJO_MANIFEST_METRICS"]).write_text(json.dumps(manifest_metrics, indent=2) + "\n")
                    wrong_tree = replace(candidate_request, tree="3" * 40)
                    with self.assertRaisesRegex(ValueError, "tree"):
                        ci_candidate_workflow.resolve(
                            wrong_tree, candidate_policy, candidate_context, root / "ci-qualified-wrong-tree.git",
                            token=native_tokens[0],
                        )


                    def await_mergeability(candidate):
                        # Native checking is asynchronous. Observe readiness
                        # before the one mutation, never retry a failed merge.
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            observed = api.pull(repo, candidate["number"])
                            self.assertEqual(observed["head"]["sha"], candidate["head"]["sha"])
                            self.assertEqual(observed["state"], "open")
                            if observed["mergeable"] is True:
                                return
                            time.sleep(0.05)
                        self.fail("local candidate did not become mergeable")

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

                    self.assertEqual(native("api", "user", "--jq", ".login").strip(), "operator")
                    repository_json = json.loads(native("api", "repos/{owner}/{repo}"))
                    self.assertEqual(repository_json["full_name"], "operator/content")
                    self.assertTrue(repository_json["node_id"].startswith(api.forge.key + "/repositories/"))
                    included = native("api", "repos/operator/content", "--include")
                    self.assertTrue(included.startswith("HTTP/1.1 200 OK\n"))
                    self.assertEqual(json.loads(included.split("\n\n", 1)[1])["full_name"], "operator/content")
                    self.assertEqual(
                        native(
                            "api", "repos/operator/content/collaborators/operator/permission", "--jq", ".permission"
                        ).strip(),
                        "admin",
                    )
                    api.request("PUT", "repos/operator/content/collaborators/reviewer", {"permission": "read"})
                    collaborator_pages = json.loads(
                        native("api", "repos/operator/content/collaborators", "--paginate", "--slurp")
                    )
                    self.assertIn("reviewer", {row["login"] for page in collaborator_pages for row in page})
                    git_ref = json.loads(native("api", "repos/operator/content/git/refs/heads/proof"))
                    self.assertEqual(git_ref["object"]["sha"], pull["head"]["sha"])
                    git_commit = json.loads(native("api", "repos/operator/content/git/commits/" + pull["head"]["sha"]))
                    self.assertEqual(git_commit["sha"], pull["head"]["sha"])
                    self.assertEqual(git_commit["message"].strip(), "Add proof")
                    self.assertIn("sha", git_commit["tree"])
                    self.assertNotEqual(git_commit["tree"]["sha"], git_commit["sha"])
                    self.assertEqual(
                        native("api", "repos/operator/content/commits/proof", "--jq", ".sha").strip(),
                        pull["head"]["sha"],
                    )
                    self.assertEqual(
                        json.loads(native("api", "repos/operator/content/git/matching-refs/heads/no-such-prefix/")), []
                    )
                    full_tree = json.loads(
                        native("api", "repos/operator/content/git/trees/" + pull["head"]["sha"] + "?recursive=1")
                    )
                    self.assertEqual(full_tree["sha"], git_commit["tree"]["sha"])
                    same_tree = json.loads(
                        native("api", "repos/operator/content/git/trees/" + full_tree["sha"] + "?recursive=1")
                    )
                    self.assertEqual(same_tree["sha"], full_tree["sha"])
                    self.assertFalse(full_tree["truncated"])
                    self.assertEqual({row["path"] for row in full_tree["tree"]}, {"README.md", "Proof.lean"})
                    api.request(
                        "POST",
                        "repos/operator/content/contents/nested/Leaf.lean",
                        {
                            "branch": "main",
                            "new_branch": "tree-proof",
                            "message": "Nested tree",
                            "content": base64.b64encode(b"nested\n").decode(),
                        },
                    )
                    api.request(
                        "POST",
                        "repos/operator/content/contents/%CE%94.lean",
                        {
                            "branch": "tree-proof",
                            "message": "Unicode path",
                            "content": base64.b64encode("-- λ\n".encode()).decode(),
                        },
                    )
                    self.assertEqual(
                        native(
                            "api",
                            "repos/operator/content/contents/Δ.lean?ref=tree-proof",
                            "-H",
                            "Accept: application/vnd.github.raw",
                        ),
                        "-- λ\n",
                    )
                    recursive_false = json.loads(
                        native("api", "repos/operator/content/git/trees/tree-proof?recursive=false")
                    )
                    subtree_sha = next(row["sha"] for row in recursive_false["tree"] if row["path"] == "nested")
                    subtree = json.loads(native("api", "repos/operator/content/git/trees/" + subtree_sha))
                    self.assertEqual(subtree["sha"], subtree_sha)
                    self.assertEqual([row["path"] for row in subtree["tree"]], ["Leaf.lean"])
                    self.assertIn("nested/Leaf.lean", {row["path"] for row in recursive_false["tree"]})
                    blob_sha = next(row["sha"] for row in full_tree["tree"] if row["path"] == "Proof.lean")
                    self.assertEqual(
                        native(
                            "api",
                            "repos/operator/content/git/blobs/" + blob_sha,
                            "-H",
                            "Accept: application/vnd.github.raw",
                        ),
                        "example : True := trivial\n",
                    )
                    file_text = native(
                        "api",
                        "repos/operator/content/contents/Proof.lean",
                        "-X",
                        "GET",
                        "-f",
                        "ref=proof",
                        "-H",
                        "Accept: application/vnd.github.raw",
                    )
                    self.assertEqual(file_text, "example : True := trivial\n")
                    label_pages = json.loads(
                        native("api", "repos/operator/content/labels?per_page=1", "--paginate", "--slurp")
                    )
                    self.assertTrue(all(len(page) == 1 for page in label_pages))
                    self.assertEqual({row["name"] for page in label_pages for row in page}, {"bug", "ready"})
                    rest_native = native
                    rest_pull = json.loads(rest_native("api", f"repos/operator/content/pulls/{pull['number']}"))
                    self.assertEqual(rest_pull["head"]["sha"], pull["head"]["sha"])
                    self.assertEqual(rest_pull["changed_files"], 1)
                    self.assertEqual(rest_pull["head"]["ref"], "proof")
                    self.assertEqual(rest_pull["user"]["node_id"], api.forge.key + "/users/" + str(rest_pull["user"]["id"]))
                    rest_issue = json.loads(native("api", f"repos/operator/content/issues/{second_issue['number']}"))
                    self.assertEqual(rest_issue["title"], "Both labels")
                    self.assertEqual(rest_issue["node_id"], api.forge.key + "/issues/" + str(rest_issue["id"]))
                    api.post_comment(repo, pull["number"], "Follow-up finding")
                    rest_comments = json.loads(native(
                        "api", f"repos/operator/content/issues/{pull['number']}/comments?per_page=1", "--paginate", "--slurp"
                    ))
                    self.assertEqual([[c["body"] for c in page] for page in rest_comments], [["Review finding"], ["Follow-up finding"]])
                    self.assertNotEqual(rest_comments[0][0]["id"], rest_comments[1][0]["id"])
                    with self.assertRaises(Incomplete):
                        native("api", f"repos/operator/content/issues/{pull['number']}")
                    rest_reviews = json.loads(native(
                        "api", f"repos/operator/content/pulls/{pull['number']}/reviews?per_page=1", "--paginate", "--slurp", review=True
                    ))
                    visible_reviews = [row for page in rest_reviews for row in page]
                    self.assertTrue(any(row["body"] == "Later submitted review" for row in visible_reviews))
                    self.assertTrue(all(row["state"] != "PENDING" for row in visible_reviews))
                    self.assertTrue(all(type(row["official"]) is bool and type(row["stale"]) is bool for row in visible_reviews))
                    ref_pages = json.loads(
                        native(
                            "api", "repos/operator/content/git/matching-refs/heads/?per_page=1", "--paginate", "--slurp"
                        )
                    )
                    self.assertTrue(all(len(page) == 1 for page in ref_pages))
                    self.assertTrue(any(row["ref"] == "refs/heads/proof" for page in ref_pages for row in page))
                    pending_review = reviewer.request(
                        "POST",
                        f"repos/operator/content/pulls/{pull['number']}/reviews",
                        {"event": "PENDING", "body": "Reviewer draft must stay private"},
                    )
                    with self.assertRaises(ForgeError):
                        native("pr", "review", str(pull["number"]), "--approve", review=True)
                    self.assertTrue(
                        any(
                            row["id"] == pending_review["id"] and row["state"] == "PENDING"
                            for row in reviewer.reviews(repo, pull["number"])
                        )
                    )
                    own_rest_reviews = json.loads(native("api", f"repos/operator/content/pulls/{pull['number']}/reviews", review=True))
                    self.assertTrue(any(row["id"] == pending_review["id"] and row["state"] == "PENDING" and row["submitted_at"] is None for row in own_rest_reviews))
                    other_rest_reviews = json.loads(native("api", f"repos/operator/content/pulls/{pull['number']}/reviews"))
                    self.assertTrue(all(row["id"] != pending_review["id"] for row in other_rest_reviews))
                    reviewer.request(
                        "DELETE", f"repos/operator/content/pulls/{pull['number']}/reviews/{pending_review['id']}"
                    )
                    # A web-started review creates only the pending review and
                    # code comment, without the API's summary issue comment.
                    browser = urllib.request.build_opener(
                        urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
                    )

                    with browser.open(url + "/user/login", timeout=5) as response:
                        self.assertEqual(response.status, 200)
                    login = urllib.parse.urlencode({"user_name": "reviewer", "password": reviewer_password}).encode()
                    with browser.open(urllib.request.Request(url + "/user/login", login, {"Origin": url}), timeout=5) as response:
                        self.assertNotIn("/user/login", response.url)
                    web_path = f"/operator/content/pulls/{pull['number']}/files"
                    form = urllib.parse.urlencode({
                        "origin": "diff", "content": "Web draft finding", "side": "proposed",
                        "line": "1", "path": "Proof.lean", "before_commit_id": pull["base"]["sha"],
                        "latest_commit_id": pull["head"]["sha"],
                    }).encode()
                    with browser.open(urllib.request.Request(url + web_path + "/reviews/comments", form, {"Origin": url}), timeout=5) as response:
                        self.assertEqual(response.status, 200)
                    web_pending = [row for row in reviewer.reviews(repo, pull["number"]) if row["state"] == "PENDING"]
                    self.assertEqual(len(web_pending), 1)
                    self.assertEqual(web_pending[0]["html_url"], "")
                    own_web_reviews = json.loads(native("api", f"repos/operator/content/pulls/{pull['number']}/reviews", review=True))
                    self.assertTrue(any(row["id"] == web_pending[0]["id"] and row["html_url"] == "" for row in own_web_reviews))
                    self.assertTrue(all(row["id"] != web_pending[0]["id"] for row in json.loads(native("api", f"repos/operator/content/pulls/{pull['number']}/reviews"))))
                    reviewer.request("DELETE", f"repos/operator/content/pulls/{pull['number']}/reviews/{web_pending[0]['id']}")
                    native("label", "create", "cli", "--color", "123456", "--description", "--repo=literal")
                    with self.assertRaises(ForgeError):
                        native("label", "create", "cli")
                    native("label", "create", "cli", "--force", "--color", "654321")
                    native("label", "edit", "cli", "--name", "cli-renamed", "--description", "Updated")
                    listed_labels = json.loads(native("label", "list", "--json", "name,color,description"))
                    self.assertIn({"name": "cli-renamed", "color": "654321", "description": "Updated"}, listed_labels)
                    bug_id = next(
                        row["id"] for row in api.collection("repos/operator/content/labels") if row["name"] == "bug"
                    )
                    numeric_name = str(bug_id)
                    native("label", "create", numeric_name, "--color", "123456")
                    issue_url = native(
                        "issue", "create", "-t", "CLI issue", "-b", "-R", "-l", "bug," + numeric_name
                    ).strip()
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
                    self.assertEqual(
                        {label["name"] for label in api.issue(repo, issue_number)["labels"]}, {"ready", numeric_name}
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
                    self.assertTrue(api.pull(repo, pr_number)["draft"])
                    with self.assertRaises(ForgeError):
                        native("issue", "edit", str(pr_number), "-t", "Cannot clear WIP through issues")
                    self.assertTrue(api.pull(repo, pr_number)["draft"])
                    native("pr", "edit", str(pr_number), "-t", "[WIP] Literal title")
                    self.assertTrue(api.pull(repo, pr_number)["draft"])
                    self.assertEqual(api.pull(repo, pr_number)["title"], "WIP: [WIP] Literal title")
                    # Only an explicit native draft transition readies it; a gh
                    # title edit preserves draft state.
                    api.request("PATCH", f"repos/operator/content/pulls/{pr_number}", {"title": "CLI proof"})
                    with self.assertRaises(ForgeError):
                        native("pr", "comment", str(issue_number), "-b", "wrong type")
                    with self.assertRaises(ForgeError):
                        native("pr", "edit", str(issue_number), "--add-label", "bug")
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
                    readme = api.request("GET", "repos/operator/content/contents/README.md?ref=main")
                    api.request(
                        "PUT",
                        "repos/operator/content/contents/README-renamed.md",
                        {
                            "branch": "main",
                            "new_branch": "rename-proof",
                            "from_path": "README.md",
                            "sha": readme["sha"],
                            "message": "Rename readme",
                            "content": readme["content"],
                        },
                    )
                    rename_pull = api.request(
                        "POST",
                        "repos/operator/content/pulls",
                        {"head": "rename-proof", "base": "main", "title": "Rename proof"},
                    )
                    if retained_metadata:
                        renamed = reader.pull(repo, rename_pull["number"], ["files", "additions", "deletions"])
                        self.assertEqual(
                            renamed["files"], [{"path": "README-renamed.md", "additions": 0, "deletions": 0}]
                        )
                        self.assertEqual((renamed["additions"], renamed["deletions"]), (0, 0))
                    else:
                        # Stock slices the renamed destination without its old
                        # path, counting it as a new file instead of a rename.
                        with self.assertRaises(Incomplete):
                            reader.pull(repo, rename_pull["number"], ["files"])
                        self.assertGreater(sum(row["additions"] for row in api.files(repo, rename_pull["number"])), 0)
                    many = api.request(
                        "POST",
                        "repos/operator/content/pulls",
                        {"head": "many-files", "base": "main", "title": "Many files"},
                    )
                    self.assertEqual(len(reader.pull(repo, many["number"], ["files"])["files"]), 65)
                    api.request("PATCH", f"repos/operator/content/pulls/{second_pull['number']}", {"state": "closed"})
                    await_mergeability(pull)
                    api.request(
                        "POST",
                        f"repos/operator/content/pulls/{pull['number']}/merge",
                        {"Do": "merge", "head_commit_id": pull["head"]["sha"]},
                    )
                    rest_merged = json.loads(rest_native("api", f"repos/operator/content/pulls/{pull['number']}"))
                    self.assertTrue(rest_merged["merged"])
                    self.assertIsInstance(rest_merged["merged_at"], str)
                    self.assertIsInstance(rest_merged["merge_commit_sha"], str)
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
                    await_mergeability(fork_pull)
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
                        {"changed_files": 1, "additions": 1, "deletions": 0}
                        if retained_metadata
                        else {"changed_files": 0, "additions": 0, "deletions": 0},
                    )
                    if retained_metadata:
                        self.assertEqual(
                            reader.pull(repo, fork_pull["number"], ["changedFiles", "additions", "deletions"]),
                            {"changedFiles": 1, "additions": 1, "deletions": 0},
                        )
                    else:
                        # Missing comparison/format headers prevent reconstruction;
                        # on stock, rename pages cannot prove correct line totals.
                        with self.assertRaisesRegex(Incomplete, "did not bind the requested comparison"):
                            reader.pull(repo, fork_pull["number"], ["changedFiles", "additions", "deletions"])
                    reviewer.request(
                        "POST",
                        "repos/reviewer/content/contents/ClosedFork.lean",
                        {
                            "branch": "main",
                            "new_branch": "closed-fork",
                            "message": "Closed fork proof",
                            "content": base64.b64encode(b"example : True := trivial\n").decode(),
                        },
                    )
                    closed_fork = reviewer.request(
                        "POST",
                        "repos/operator/content/pulls",
                        {"head": "reviewer:closed-fork", "base": "main", "title": "Closed proposal"},
                    )
                    reviewer.request(
                        "PATCH", f"repos/operator/content/pulls/{closed_fork['number']}", {"state": "closed"}
                    )
                    superseding = reviewer.request(
                        "POST",
                        "repos/operator/content/pulls",
                        {"head": "reviewer:closed-fork", "base": "main", "title": "Superseding proposal"},
                    )
                    await_mergeability(superseding)
                    api.request(
                        "POST",
                        f"repos/operator/content/pulls/{superseding['number']}/merge",
                        {"Do": "merge", "head_commit_id": superseding["head"]["sha"]},
                    )
                    api.request("DELETE", "repos/reviewer/content")
                    deleted_fork = api.pull(repo, fork_pull["number"])
                    self.assertIsNone(deleted_fork["head"]["repo"])
                    self.assertEqual(
                        {key: deleted_fork[key] for key in ["changed_files", "additions", "deletions"]},
                        {"changed_files": 1, "additions": 1, "deletions": 0}
                        if retained_metadata
                        else {"changed_files": 0, "additions": 0, "deletions": 0},
                    )
                    self.assertIsNone(api.pull(repo, closed_fork["number"])["head"]["repo"])
                    if retained_metadata:
                        self.assertEqual(
                            {
                                key: api.pull(repo, closed_fork["number"])[key]
                                for key in ["changed_files", "additions", "deletions"]
                            },
                            {"changed_files": 1, "additions": 1, "deletions": 0},
                        )
                        proposal = reader.pull(repo, closed_fork["number"], ["changedFiles", "files"])
                        self.assertEqual(proposal["changedFiles"], 1)
                        self.assertEqual(
                            proposal["files"], [{"path": "ClosedFork.lean", "additions": 1, "deletions": 0}]
                        )
                    else:
                        self.assertEqual(api.files(repo, closed_fork["number"]), [])
                        with self.assertRaises(Incomplete):
                            reader.pull(repo, closed_fork["number"], ["files"])
                        for proposal in [fork_pull, closed_fork]:
                            requests = []
                            original_request = Forgejo.request

                            def recorded_request(client, method, path, *args, requests=requests, original_request=original_request, **kwargs):
                                requests.append((method, path))
                                return original_request(client, method, path, *args, **kwargs)

                            with (self.subTest(stock_rest_deleted_fork=proposal["number"]),
                                  patch.object(Forgejo, "request", recorded_request), self.assertRaises(ForgeError) as unavailable):
                                rest_native("api", f"repos/operator/content/pulls/{proposal['number']}")
                            self.assertIn(unavailable.exception.status, {0, 500})
                            self.assertEqual(requests[-1][0], "GET")
                            self.assertEqual(urllib.parse.urlsplit(requests[-1][1]).path, f"repos/operator/content/pulls/{proposal['number']}/files")
                            for field in ["changedFiles", "additions", "deletions", "files"]:
                                with (
                                    self.subTest(deleted_source_proposal=proposal["number"], field=field),
                                    self.assertRaises(ForgeError),
                                ):
                                    reader.pull(repo, proposal["number"], [field])

                    if retained_metadata:
                        historical = reader.pull(
                            repo, fork_pull["number"], ["headRefOid", "changedFiles", "additions", "deletions", "files"]
                        )
                        self.assertEqual(historical["headRefOid"], fork_pull["head"]["sha"])
                        raw_historical = json.loads(rest_native("api", f"repos/operator/content/pulls/{fork_pull['number']}"))
                        self.assertEqual(raw_historical["head"]["sha"], fork_pull["head"]["sha"])
                        self.assertIsNone(raw_historical["head"]["repo"])
                        self.assertEqual(raw_historical["changed_files"], 1)
                        self.assertEqual(historical["changedFiles"], 1)
                        self.assertEqual(historical["files"], [{"path": "Fork.lean", "additions": 1, "deletions": 0}])

                    if retained_metadata:
                        # A source can be reset onto its target while the PR stays
                        # open. Real zero scalars must traverse bound empty pages.
                        api.request(
                            "POST",
                            "repos/operator/content/contents/EmptyProof.lean",
                            {
                                "branch": "main",
                                "new_branch": "empty-proof",
                                "message": "Empty proof setup",
                                "content": base64.b64encode(b"example : True := trivial\n").decode(),
                            },
                        )
                        empty = api.request(
                            "POST",
                            "repos/operator/content/pulls",
                            {
                                "head": "empty-proof",
                                "base": "main",
                                "title": "Empty comparison proof",
                            },
                        )
                        zero = empty["base"]["sha"]
                        subprocess.run(
                            ["git", "--git-dir", str(root / "repos/operator/content.git"), "update-ref", "--stdin"],
                            input=f"update refs/heads/empty-proof {zero}\nupdate refs/pull/{empty['number']}/head {zero}\n",
                            text=True,
                            check=True,
                            capture_output=True,
                        )
                        native_empty = api.pull(repo, empty["number"])
                        self.assertEqual(
                            [native_empty[k] for k in ["changed_files", "additions", "deletions"]], [0, 0, 0]
                        )
                        self.assertEqual(
                            reader.pull(repo, empty["number"], ["files", "changedFiles", "additions", "deletions"]),
                            {"files": [], "changedFiles": 0, "additions": 0, "deletions": 0},
                        )

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
                    if retained_metadata:
                        self.assertEqual(api.pull(repo, pull["number"])["head"]["sha"], pull["head"]["sha"])
                        self.assertEqual(json.loads(rest_native("api", f"repos/operator/content/pulls/{pull['number']}"))["head"]["sha"], pull["head"]["sha"])
                        historical = reader.pull(
                            repo,
                            pull["number"],
                            ["headRefOid", "changedFiles", "additions", "files", "statusCheckRollup"],
                        )
                        self.assertEqual(historical["headRefOid"], pull["head"]["sha"])
                        self.assertEqual(historical["changedFiles"], 1)
                        self.assertEqual(historical["files"], [{"path": "Proof.lean", "additions": 1, "deletions": 0}])
                        self.assertEqual(historical["statusCheckRollup"][0]["state"], "SUCCESS")
                        return
                    self.assertNotEqual(api.pull(repo, pull["number"])["head"]["sha"], pull["head"]["sha"])
                    with self.assertRaisesRegex(Incomplete, "moved closed-PR"):
                        rest_native("api", f"repos/operator/content/pulls/{pull['number']}")
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
