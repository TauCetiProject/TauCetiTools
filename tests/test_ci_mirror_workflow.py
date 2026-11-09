"""Post-merge source identity, control context, and canonical workflow generation."""

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

from tauceti_tools import ci_candidate as cc
from tauceti_tools import ci_mirror_workflow as mirror
from tests.test_ci_candidate import policy
from tests.test_ci_candidate_workflow import CONFIG as CANDIDATE_CONFIG

CONFIG = CANDIDATE_CONFIG.replace(b'branch="pilot"', b'branch="main"') + b"\n[features]\ndocs=true\n"


class Client:
    def __init__(self, approved, revision):
        self.forge = approved.credential_origin
        self.identity = {"id": approved.source.native_id, "full_name": approved.source.repository.name}
        self.commit = {"sha": revision}
        self.branch = {"name": approved.base_branch, "protected": True, "commit": {"id": revision}}
        self.comparison = {"total_commits": 0, "commits": []}
        self.reverse_comparison = {"total_commits": 1, "commits": [{"sha": "c" * 40}]}
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        if "/git/commits/" in path:
            return self.commit
        if "/branches/" in path:
            return self.branch
        if "/compare/" in path:
            if self.branch["commit"]["id"] != self.commit["sha"] and f"/compare/{self.commit['sha']}..." in path:
                return self.reverse_comparison
            return self.comparison
        return self.identity


class MirrorWorkflow(unittest.TestCase):
    def setUp(self):
        self.policy = cc.ExecutionPolicy.parse(
            replace(policy(), base_branch="main").record(with_config=False), configuration=CONFIG
        )
        self.context = {
            "event_name": "push",
            "repository": "team/Execution",
            "repository_id": "42",
            "ref": "refs/heads/main",
            "sha": "b" * 40,
            "workflow_sha": "b" * 40,
            "checkout_sha": "b" * 40,
            "workflow_ref": "team/Execution/.github/workflows/ci.yml@refs/heads/main",
            "harness_repository": "team/Tools",
            "harness_sha": self.policy.harness_revision,
            "run_id": "101",
            "attempt": "2",
        }
        self.client = Client(self.policy, self.context["sha"])

    def bootstrap(self, context=None):
        with tempfile.TemporaryDirectory() as directory:
            return mirror.bootstrap(
                self.policy, CONFIG, context or self.context, Path(directory) / "scripts", client=self.client
            )

    def test_qualified_source_and_cache_execution_remain_distinct(self):
        env, record = self.bootstrap()
        self.assertEqual(json.loads(env["TAUCETI_SOURCE_REPOSITORY"]), self.policy.source.record())
        self.assertEqual(json.loads(env["TAUCETI_CI_EXECUTION_REPOSITORY"]), self.policy.execution.record())
        self.assertEqual(env["TAUCETI_CACHE_REPOSITORY"], "team/Execution")
        self.assertEqual(env["TAUCETI_REPO"], "qualified-only:unavailable")
        self.assertEqual(env["TAUCETI_SOURCE_CANDIDATE"], "false")
        self.assertEqual(env["TAUCETI_SOURCE_MIRROR"], "true")
        self.assertEqual(env["TAUCETI_GITHUB_STATE_URL"], "")
        self.assertEqual(record["revision"], "b" * 40)
        self.assertEqual(record["schema"], mirror.SCHEMA)
        with self.assertRaises(ValueError):
            cc.CandidateRequest.parse(record, policy=self.policy)
        self.assertEqual(
            self.client.calls,
            [
                ("GET", "repos/team/Math"),
                ("GET", "repos/team/Math/git/commits/" + "b" * 40),
                ("GET", "repos/team/Math/branches/main"),
                ("GET", "repos/team/Math/compare/" + "b" * 40 + "..." + "b" * 40 + "?files=false&verification=false"),
                ("GET", "repos/team/Math"),
            ],
        )

    def test_wrong_host_role_ref_pin_or_event_cannot_bootstrap(self):
        values = [
            ("repository", "team/Math"),
            ("repository_id", "99"),
            ("ref", "refs/heads/proposal"),
            ("workflow_sha", "c" * 40),
            ("checkout_sha", "c" * 40),
            ("sha", "0" * 40),
            ("workflow_ref", "team/Other/.github/workflows/ci.yml@refs/heads/main"),
            ("harness_sha", "main"),
            ("event_name", "repository_dispatch"),
            ("run_id", "0"),
            ("attempt", ""),
        ]
        for key, value in values:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.bootstrap({**self.context, key: value})
        self.assertEqual(self.client.calls, [])
        with patch.dict("os.environ", {"TAUCETI_SOURCE_CANDIDATE": "true"}), self.assertRaises(ValueError):
            self.bootstrap()

    def test_native_repository_name_reuse_and_wrong_revision_fail(self):
        self.client.identity["id"] = 999
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.bootstrap()
        self.client.identity["id"] = self.policy.source.native_id
        self.client.commit["sha"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "revision is unavailable"):
            self.bootstrap()

    def test_existing_unmerged_commit_or_incomplete_ancestry_is_refused(self):
        for comparison in (
            {"total_commits": 1, "commits": [self.client.commit]},
            {"total_commits": 0, "commits": [self.client.commit]},
            {"total_commits": False, "commits": []},
            {"total_commits": 0},
            {},
        ):
            with self.subTest(comparison=comparison), self.assertRaisesRegex(ValueError, "not proven"):
                self.client.comparison = comparison
                self.bootstrap()
        self.client.comparison = {"total_commits": 0, "commits": []}
        self.client.branch["protected"] = False
        with self.assertRaisesRegex(ValueError, "protected branch"):
            self.bootstrap()

    def test_older_mirrored_main_is_valid_when_source_has_advanced(self):
        self.client.branch["commit"]["id"] = "c" * 40
        _, record = self.bootstrap()
        self.assertEqual(record["revision"], "b" * 40)
        self.assertEqual(record["source_main"], "c" * 40)
        self.assertIn(
            ("GET", "repos/team/Math/compare/" + "c" * 40 + "..." + "b" * 40 + "?files=false&verification=false"),
            self.client.calls,
        )

    def test_unrelated_history_cannot_use_the_empty_compare_result(self):
        self.client.branch["commit"]["id"] = "c" * 40
        self.client.reverse_comparison = {"total_commits": 0, "commits": []}
        with self.assertRaisesRegex(ValueError, "no proven common history"):
            self.bootstrap()

    def test_mirror_branch_names_must_agree(self):
        different = replace(self.policy, execution_branch="other")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "same source and execution branch"):
                mirror.bootstrap(different, CONFIG, self.context, Path(directory), client=self.client)
            with self.assertRaisesRegex(ValueError, "same source and execution branch"):
                mirror.write_workflows(different, CONFIG, Path(directory))
        self.assertEqual(self.client.calls, [])

    @staticmethod
    def prepare_layout(directory):
        for name in ("web/lakefile.toml", "web/lean-toolchain", "docbuild/lakefile.toml", "docbuild/sync_manifest.py"):
            target = Path(directory) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture project file\n")

    def test_generator_refuses_missing_flagship_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "existing web/docbuild"):
                mirror.write_workflows(self.policy, CONFIG, Path(directory))
            self.assertFalse((Path(directory) / ".github").exists())

    def test_required_main_cache_publication_cannot_skip_missing_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            self.prepare_layout(directory)
            mirror.write_workflows(self.policy, CONFIG, Path(directory))
            workflow = yaml.safe_load((Path(directory) / ".github/workflows/ci.yml").read_text())
            step = next(step for step in workflow["jobs"]["build"]["steps"] if step.get("id") == "cachecfg")
            self.assertEqual(step["env"]["REQUIRE_PUBLICATION"], "true")
            endpoints = {key: "https://cache.invalid/" for key in step["env"] if key.endswith("ENDPOINT")}
            output = Path(directory) / "output"
            for missing in endpoints:
                with self.subTest(missing=missing):
                    env = {
                        "PATH": os.environ["PATH"],
                        **endpoints,
                        missing: "",
                        "REQUIRE_PUBLICATION": "true",
                        "GITHUB_OUTPUT": str(output),
                    }
                    result = subprocess.run(["bash", "-eu", "-c", step["run"]], env=env, capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
            for required, configured in (("true", endpoints), ("false", {key: "" for key in endpoints})):
                output.unlink(missing_ok=True)
                env = {
                    "PATH": os.environ["PATH"],
                    **configured,
                    "REQUIRE_PUBLICATION": required,
                    "GITHUB_OUTPUT": str(output),
                }
                subprocess.run(["bash", "-eu", "-c", step["run"]], env=env, check=True, capture_output=True)
                self.assertEqual(output.read_text(), "enabled=" + required + "\n")

    def test_composite_action_loads_and_supplies_explicit_mode_inputs(self):
        from tauceti_tools.templates import ASSETS

        action = yaml.safe_load((ASSETS.parent.parent / ".github/actions/setup/action.yml").read_text())
        step = next(item for item in action["runs"]["steps"] if item.get("shell") == "bash")
        self.assertEqual(
            set(step["env"]), {"PROJECT_DIRECTORY", "SOURCE_MIRROR_ENABLED", "TOOLS_REPOSITORY", "TOOLS_REVISION"}
        )
        self.assertEqual(action["inputs"]["source-mirror-enabled"]["default"], "false")
        subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)

    def test_generates_existing_main_and_docs_workflows_with_qualified_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            self.prepare_layout(directory)
            paths = mirror.write_workflows(self.policy, CONFIG, Path(directory))
            self.assertEqual({Path(path).name for path in paths}, {"ci.yml", "pages.yml", "api-docs.yml"})
            for path in paths:
                text = Path(path).read_text()
                self.assertIn("source-mirror-enabled: true", text, path)
                self.assertIn("team/Tools/.github/actions/setup@" + self.policy.harness_revision, text, path)
                self.assertNotIn("__TOOLS_", text)
                self.assertNotIn("__SOURCE_MIRROR", text)
            ci = (Path(directory) / ".github/workflows/ci.yml").read_text()
            self.assertIn("publication-required: true", ci)
            self.assertIn(
                "uses: team/Tools/.github/workflows/publish-lake-cache.yml@" + self.policy.harness_revision, ci
            )
            self.assertNotIn("uses: ./.github/workflows/publish-lake-cache.yml", ci)
            pages = yaml.safe_load((Path(directory) / ".github/workflows/pages.yml").read_text())
            self.assertIn("'true' != 'true'", pages["jobs"]["advance-docgen-ref"]["if"])
            self.assertIn(
                "git clone --quiet https://source.example/team/Roadmap.git",
                (Path(directory) / ".github/workflows/pages.yml").read_text(),
            )
            self.assertIn("branches: [main]", ci)


if __name__ == "__main__":
    unittest.main()
