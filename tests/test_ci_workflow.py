"""Exercise the reusable workflow's identity handoff without GitHub or Git traffic."""

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/pr-build.yml"


def shell_for(name, workflow=WORKFLOW):
    lines = workflow.read_text().splitlines()
    start = lines.index("      - name: " + name)
    run = next(i for i in range(start, len(lines)) if lines[i] == "        run: |")
    end = run + 1
    while end < len(lines) and (not lines[end] or lines[end].startswith("          ")):
        end += 1
    return "\n".join(line[10:] for line in lines[run + 1 : end]) + "\n"


class WorkflowIdentityTests(unittest.TestCase):
    def resolve(self, event, *, git_fails=False, requested_pr="123"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        scripts = root / "gate/scripts"
        scripts.mkdir(parents=True)
        stub = """import json, os, sys
from pathlib import Path
with Path("calls.jsonl").open("a") as f:
 f.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + "\\n")
name = Path(sys.argv[0]).name
if name == "github_api.py":
 print(json.dumps({"head":{"sha":"a"*40,"repo":{"full_name":"fork/Project"}},"base":{"ref":"main"},"user":{"login":"author"}}))
elif name == "bors_train_identity.py":
 print("bors/staging/main/123")
else:
 if os.environ.get("CI_GIT_FAIL"): sys.exit(7)
 print(json.dumps({"base_sha":"b"*40,"mergebase":"c"*40}))
"""
        for name in ("ci_git.py", "github_api.py", "bors_train_identity.py"):
            (scripts / name).write_text(stub)
        values = {
            "github.event_name": event,
            "github.event.action": "tauceti-bors-staging" if event == "repository_dispatch" else "",
            "github.repository": "owner/Project",
            "github.event.merge_group.head_sha": "a" * 40,
            "github.event.merge_group.base_sha": "b" * 40,
            "github.event.pull_request.number": "123",
            "github.event.pull_request.head.sha": "a" * 40,
            "github.event.pull_request.head.repo.full_name": "fork/Project",
            "github.event.pull_request.base.ref": "main",
            "github.event.pull_request.user.login": "author",
            "inputs.lean_root": "Project",
        }
        shell = re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda m: values[m[1]], shell_for("Resolve PR head"))
        output, environment = root / "output", root / "environment"
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", shell],
            cwd=root,
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "GITHUB_OUTPUT": str(output),
                "GITHUB_ENV": str(environment),
                "RUNNER_TEMP": str(root),
                "REQUESTED_PR": requested_pr,
                "BORS_HEAD_SHA": "a" * 40,
                "BORS_BASE_SHA": "b" * 40,
                "BORS_BATCH_ID": "123",
                "BORS_BASE_REF": "main",
                "CI_GIT_FAIL": "1" if git_fails else "",
            },
        )
        calls = (
            [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
            if (root / "calls.jsonl").exists()
            else []
        )
        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
        return result, calls, outputs

    def test_manual_dispatch_fetches_pr_once_and_passes_git_identity(self):
        result, calls, outputs = self.resolve("workflow_dispatch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0], ["github_api.py", "repos/owner/Project/pulls/123"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0:2], ["ci_git.py", "resolve"])
        self.assertIn("fork/Project", calls[1])
        self.assertEqual(outputs["base_sha"], "b" * 40)
        self.assertEqual(outputs["mergebase"], "c" * 40)
        self.assertEqual(outputs["status_sha"], "a" * 40)

    def test_event_payloads_avoid_pr_metadata_read(self):
        for event in ("pull_request_target", "merge_group", "repository_dispatch"):
            with self.subTest(event=event):
                result, calls, outputs = self.resolve(event)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("github_api.py", [c[0] for c in calls])
                self.assertEqual(outputs["sha"], "a" * 40)
                if event == "repository_dispatch":
                    self.assertEqual(calls[0][0], "bors_train_identity.py")
                    self.assertEqual(calls[-1][-2:], ["--staging-ref", "bors/staging/main/123"])

    def test_failed_git_identity_cannot_publish_head_or_status_outputs(self):
        result, _, outputs = self.resolve("workflow_dispatch", git_fails=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outputs, {})

    def test_invalid_manual_pr_stops_before_any_request(self):
        result, calls, outputs = self.resolve("workflow_dispatch", requested_pr="123;bad")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertEqual(outputs, {})

    def test_retry_discovery_accepts_only_small_unique_original_run_intents(self):
        workflow = ROOT / ".github/workflows/retry-ci-status.yml"
        shell = shell_for("Discover retained intents from this exact original run", workflow)
        for rows, count, expected in (
            ([{"id": 41, "name": "ci-status-intents-sandbox", "size_in_bytes": 100}], 1, True),
            ([{"id": 41, "name": "unrelated", "size_in_bytes": 100}], 1, True),
            ([{"id": 41, "name": "ci-status-intents-sandbox", "size_in_bytes": 2**20 + 1}], 1, False),
            ([{"id": 41, "name": "ci-status-intents-sandbox", "expired": True}], 1, False),
            (
                [{"id": 41, "name": "ci-status-intents-sandbox"}, {"id": 42, "name": "ci-status-intents-sandbox"}],
                2,
                False,
            ),
            ([], 101, False),
        ):
            with self.subTest(rows=rows, count=count), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                (directory / "scripts").mkdir()
                data = json.dumps({"artifacts": rows, "total_count": count})
                (directory / "scripts/github_api.py").write_text("print(" + repr(data) + ")\n")
                output = directory / "output"
                result = subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", shell],
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "CI_STATUS_SOURCE_RUN_ID": "99",
                        "CI_STATUS_SOURCE_ATTEMPT": "1",
                        "GITHUB_REPOSITORY": "owner/Project",
                        "RUNNER_TEMP": temp,
                        "GITHUB_OUTPUT": str(output),
                    },
                )
                self.assertEqual(result.returncode == 0, expected, result.stderr)
                if expected:
                    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
                    self.assertEqual(outputs["ids"], "41" if rows[0]["name"] == "ci-status-intents-sandbox" else "")

    def test_generated_wrapper_exposes_optional_credential_configuration(self):
        wrapper = (ROOT / "tauceti_tools/assets/project/.github/workflows/pr-build.yml").read_text()
        source = WORKFLOW.read_text()
        for name in ("CI_READ_APP_ID", "CI_READ_APP_PRIVATE_KEY", "CI_STATUS_APP_ID", "CI_STATUS_APP_PRIVATE_KEY"):
            self.assertIn(name + ": {required: false}", source)
            self.assertIn(name + ": ${{ secrets." + name + " }}", wrapper)
        self.assertNotIn("TOOLS_CI_REVISION", source)
        self.assertIn("uses: ./.tauceti-tools/.github/actions/ci-credentials", source)
        self.assertNotIn("gh api", source)
        self.assertIn("runs-on: ubuntu-24.04", source)
        retry = (ROOT / "tauceti_tools/assets/project/.github/workflows/retry-ci-status.yml").read_text()
        self.assertIn("workflows: [pr-build]", retry)
        self.assertIn("__TOOLS_REPO__/.github/workflows/retry-ci-status.yml@__TOOLS_REF__", retry)
        self.assertNotIn("now=$(python3 gate/scripts/github_api.py", source)


if __name__ == "__main__":
    unittest.main()
