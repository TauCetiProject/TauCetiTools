"""Exercise the shipped manual-dispatch workflow's retry and identity boundaries."""

import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from tests.test_ci_candidate import policy, request

WORKFLOW = Path(__file__).resolve().parents[1] / "examples/forgejo-stage1/stage1-dispatch.yml"
SEND = "Dispatch with this repository's native job token"


class Handoff(unittest.TestCase):
    def setUp(self):
        content = yaml.safe_load(WORKFLOW.read_text())
        self.validation = (
            content["jobs"]["dispatch"]["steps"][2]["run"]
            .split("python3 - <<'PY_VALIDATE'\n", 1)[1]
            .rsplit("\nPY_VALIDATE", 1)[0]
        )
        self.policy = policy()
        self.request = request(self.policy)
        self.calls = []
        self.skipped = {
            "total_count": 1,
            "jobs": [{"status": "completed", "steps": [{"name": SEND, "conclusion": "skipped"}]}],
        }

    @contextmanager
    def workspace(self, attempt=1):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gate/.tauceti").mkdir(parents=True)
            (root / "gate/tauceti.toml").write_bytes(b"trusted configuration")
            (root / "gate/.tauceti/ci-source.json").write_text(json.dumps(self.policy.record(with_config=False)))
            env = {
                "REQUEST": json.dumps(self.request.record()),
                "GITHUB_REPOSITORY": "team/Execution",
                "GITHUB_REPOSITORY_ID": "42",
                "CALLER_REF": "refs/heads/main",
                "CALLER_SHA": self.request.workflow_revision,
                "GITHUB_RUN_ATTEMPT": str(attempt),
                "GITHUB_RUN_ID": "202",
                "RUNNER_TEMP": str(root),
                "EXECUTION_JOB_TOKEN": "fixture-only",
            }
            old = Path.cwd()
            os.chdir(root)
            try:
                with patch.dict(os.environ, env):
                    yield root
            finally:
                os.chdir(old)

    def execute(self, evidence=None):
        def read(query, timeout):
            self.calls.append((query.get_method(), query.full_url))
            raw = json.dumps(self.skipped if evidence is None else evidence).encode()
            response = io.BytesIO(raw)
            response.status = 200
            return response

        with patch("urllib.request.build_opener", return_value=SimpleNamespace(open=read)):
            exec(compile(self.validation, str(WORKFLOW), "exec"), {})

    def test_first_send_needs_no_retry_evidence_and_freezes_exact_payload(self):
        with self.workspace() as root:
            self.execute()
            self.assertEqual(json.loads((root / "dispatch-intent.json").read_text()), self.request.dispatch())
        self.assertEqual(self.calls, [])

    def test_same_parent_rerun_only_after_all_earlier_sends_were_skipped(self):
        with self.workspace(attempt=3):
            self.execute()
        self.assertEqual(
            self.calls,
            [
                ("GET", "https://api.github.com/repos/team/Execution/actions/runs/202/attempts/1/jobs?per_page=100"),
                ("GET", "https://api.github.com/repos/team/Execution/actions/runs/202/attempts/2/jobs?per_page=100"),
            ],
        )

    def test_uncertain_successful_or_missing_send_evidence_cannot_repeat(self):
        for outcome in ["success", "failure", "cancelled", None]:
            evidence = {
                "total_count": 1,
                "jobs": [{"status": "completed", "steps": [{"name": SEND, "conclusion": outcome}]}],
            }
            with self.subTest(outcome=outcome), self.workspace(attempt=2) as root:
                with self.assertRaises(SystemExit):
                    self.execute(evidence)
                self.assertFalse((root / "dispatch-intent.json").exists())
        for evidence in [
            {"total_count": 0, "jobs": []},
            {"total_count": 2, "jobs": [self.skipped["jobs"][0]]},
            {"total_count": 1, "jobs": [{"status": "completed", "steps": []}]},
        ]:
            with self.workspace(attempt=2), self.assertRaises(SystemExit):
                self.execute(evidence)

    def test_wrong_repository_branch_and_caller_fail_even_with_python_optimization(self):
        for key, value in [
            ("GITHUB_REPOSITORY", "other/Repo"),
            ("GITHUB_REPOSITORY_ID", "77"),
            ("CALLER_REF", "refs/heads/untrusted"),
            ("CALLER_SHA", "f" * 40),
        ]:
            with self.subTest(key=key), self.workspace(), patch.dict(os.environ, {key: value}):
                with self.assertRaises(SystemExit):
                    exec(compile(self.validation, str(WORKFLOW), "exec", optimize=2), {})


if __name__ == "__main__":
    unittest.main()
