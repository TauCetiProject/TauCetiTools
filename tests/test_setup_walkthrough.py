"""Execute setup commands and workflow guards in temporary repositories."""

import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import yaml
import test_ci_workflow as workflow
import test_doctor_setup as setup
from tauceti_tools.project import parse
from tauceti_tools.scaffold import initial_configuration
from tauceti_tools.templates import guard_app_jobs, materialize, write_project


class SetupWalkthrough(unittest.TestCase):
    def project(self, bors=False):
        project = parse(initial_configuration("one/Project", "human").encode())
        project.raw["tools"] = {
            "tools": {"repo": "TauCetiProject/TauCetiTools", "ref": "a" * 40},
            "review": {"repo": "TauCetiProject/TauCetiReview", "ref": "b" * 40},
        }
        if bors:
            project.raw["merge"]["backend"] = "bors"
            project.raw["services"]["bors"].update(mode="custom", url="https://bors.example.org", repository_id=7)
        return project

    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout

    def test_printed_starter_commands_survive_a_commit_and_fresh_clone(self):
        fixture = setup.DoctorSetupTests()
        fixture.setUp()
        fixture.publish_package("one/Project")
        fixture.responses["repos/one/Project/contents/.github/workflows/pr-build.yml?ref=main"] = {
            "size": 20}
        rows = fixture.findings()
        for role, directories in (("content", ["Project"]), ("roadmap", ["ProjectRoadmap", "Completed"])):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "original"
                root.mkdir()
                self.git(root, "init", "-q")
                (root / ".gitignore").write_text("existing-rule\n")
                for step in rows["lean." + role].steps:
                    for line in step.splitlines():
                        if line.startswith(("$ mkdir ", "$ printf ", "$ touch ", "$ git check-ignore ")):
                            subprocess.run(["sh", "-c", line[2:]], cwd=root, check=True)
                (root / ".lake/packages").mkdir(parents=True)
                (root / ".lake/packages/artifact").touch()
                self.git(root, "add", ".")
                tracked = self.git(root, "ls-files").splitlines()
                self.assertFalse(any(name.startswith(".lake/") for name in tracked))
                self.git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com",
                         "-c", "commit.gpgsign=false", "commit", "-qm", "Starter")
                clone = Path(tmp) / "clone"
                subprocess.run(["git", "clone", "-q", str(root), str(clone)], check=True)
                for directory in directories:
                    self.assertTrue((clone / directory).is_dir())
                if role == "content":
                    text = (clone / "Project/Basic.lean").read_text()
                    self.assertTrue(text.startswith("/-\nCopyright"))
                    self.assertTrue(text.endswith("-/\nmodule\n"), repr(text))
                else:
                    self.assertTrue((clone / "Completed/.gitkeep").is_file())

    def test_both_merge_setups_render_with_the_right_bors_observation(self):
        for bors in (False, True):
            with self.subTest(bors=bors), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = self.project(bors)
                write_project(project, root)
                workflows = {p.name: yaml.safe_load(p.read_text()) for p in (root / ".github/workflows").glob("*.yml")}
                self.assertNotIn("app-credentials", workflows["pr-build.yml"]["jobs"])
                self.assertNotIn("app-credentials", workflows["retry-ci-status.yml"]["jobs"])
                for name in ("housekeeping.yml", "merge-sweep.yml", "auto-merge.yml"):
                    self.assertIn("app-credentials", workflows[name]["jobs"])
                self.assertNotIn("roadmap-label.yml", workflows)
                merge = workflows["auto-merge.yml"]["jobs"]["merge"]
                self.assertIn("resolve", merge["needs"])
                self.assertIn("app-credentials", merge["needs"])
                self.assertIn("needs.resolve.outputs.pr", merge["if"])
                self.assertNotIn("# Pinned", merge["if"])
                normalized = " ".join(workflows["auto-merge.yml"]["jobs"]["resolve"]["if"].split())
                self.assertEqual(workflows["auto-merge.yml"]["jobs"]["app-credentials"]["if"].strip(),
                                 "((" + normalized + "))")
                lint = workflows["lint-full.yml"]["jobs"]
                self.assertNotIn("needs", lint["lint"])
                self.assertIn("app-credentials", lint["repair"]["needs"])
                if bors:
                    materialize(project, root / "shared")
                    text = (root / "shared/bors_observation.py").read_text()
                    self.assertIn("https://bors.example.org/repositories/7/active-batches", text)
                    self.assertIn('REPO = "one/Project"', text)

    def test_app_guard_checks_presence_without_logging_values_or_validating_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_project(self.project(), root)
            text = (root / ".github/workflows/housekeeping.yml").read_text()
            data = yaml.safe_load(text)
            self.assertEqual(guard_app_jobs(text), text)
            script = data["jobs"]["app-credentials"]["steps"][0]["run"]
            for app_id, key, ready in (("", "", False), ("123", "", False), ("", "secret-PEM", False),
                                       ("123", "invalid-but-present-PEM", True)):
                with self.subTest(app_id=app_id, ready=ready):
                    output, summary = root / "output", root / "summary"
                    output.unlink(missing_ok=True)
                    summary.unlink(missing_ok=True)
                    result = subprocess.run(["bash", "-eu", "-c", script], capture_output=True, text=True,
                                            env={**os.environ, "APP_ID": app_id, "APP_PRIVATE_KEY": key,
                                                 "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output.read_text().strip(), "ready=" + str(ready).lower())
                    public = result.stdout + result.stderr + (summary.read_text() if summary.exists() else "")
                    if key:
                        self.assertNotIn(key, public)
                    if not ready:
                        self.assertIn("Finish App credential setup", public)

    def test_app_guard_preserves_fork_filters_comments_and_multiline_needs(self):
        source = """name: example
on: issue_comment
jobs:
  first:
    runs-on: ubuntu-latest
    steps:
      - run: echo first
  second:
    runs-on: ubuntu-latest
    steps:
      - run: echo second
  publish:
    needs:
      - first
      - second
    if: ${{ github.repository == 'one/Project' }} # Never in a fork
    runs-on: ubuntu-latest
    env:
      APP_ID: ${{ secrets.APP_ID }}
      APP_PRIVATE_KEY: ${{ secrets.APP_PRIVATE_KEY }}
    steps:
      - run: echo publish
"""
        text = guard_app_jobs(source)
        jobs = yaml.safe_load(text)["jobs"]
        self.assertEqual(jobs["publish"]["needs"], ["app-credentials", "first", "second"])
        self.assertEqual(jobs["app-credentials"]["if"].strip(), "((github.repository == 'one/Project'))")
        self.assertNotIn("# Never", jobs["publish"]["if"])
        self.assertEqual(text.count("    needs:"), 1)

    def test_scope_report_routes_only_bors_overrides_to_bors_verification(self):
        script = workflow.shell_for("Report build and bump-guard statuses to the PR head commit")
        values = {"env.SCOPE_OK": "1", "env.OUT_OF_SCOPE": "1", "env.PINS_KNOWN": "1",
                  "env.IS_BATCH": "false", "steps.build.outcome": "success",
                  "steps.pr.outputs.status_sha": "a" * 40, "steps.pr.outputs.num": "7",
                  "steps.pr.outputs.base_ref": "main", "inputs.lean_root": "Project"}
        for backend in ("queue", "bors"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scripts = root / "gate/scripts"
                scripts.mkdir(parents=True)
                (scripts / "prepare_pr_integration.py").write_text("")
                (scripts / "ci_status.py").write_text(
                    "import json,sys\nfrom pathlib import Path\n"
                    "with Path('statuses.jsonl').open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n")
                command = re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda m: values.get(m[1], ""), script)
                result = subprocess.run(["bash", "-eu", "-c", command], cwd=root, capture_output=True, text=True,
                                        env={**os.environ, "MERGE_BACKEND": backend, "IS_BATCH": "false",
                                             "OUT_OF_SCOPE": "1", "RAW_HEAD": "a" * 40, "CAPTURED_BASE": "b" * 40,
                                             "CANDIDATE_MODE": "pr-integration",
                                             "TESTED_SHA": "c" * 40, "TESTED_TREE": "d" * 40,
                                             "TESTED_SCOPE_BASE": "e" * 40, "SCOPE_PR": "7", "RUNNER_TEMP": tmp,
                                             "GITHUB_OUTPUT": str(root / "output")})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                calls = [json.loads(line) for line in (root / "statuses.jsonl").read_text().splitlines()]
                scope, = [args for args in calls if "--context" in args and args[args.index("--context") + 1] == "scope"]
                self.assertEqual(scope[scope.index("--state") + 1], "success" if backend == "bors" else "failure")
                self.assertEqual("--scope-approval-pr" in scope, backend == "bors")
                if backend == "queue":
                    self.assertIn("admin merge", scope[scope.index("--description") + 1])

    def test_empty_roadmap_succeeds_without_api_calls_but_missing_directory_fails(self):
        with patch.object(sys, "path", [str(workflow.ROOT / "tauceti_tools/assets/scripts"), *sys.path]):
            import roadmap_label
        with tempfile.TemporaryDirectory() as tmp, patch.object(roadmap_label, "GitHub") as api:
            output = io.StringIO()
            with redirect_stdout(output):
                result = roadmap_label.main(["--roadmap-dir", tmp, "--pr", "7", "--apply", "--sync-labels"])
            self.assertEqual(result, 0)
            self.assertIn("No roadmap areas yet", output.getvalue())
            api.assert_not_called()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(roadmap_label.main(["--roadmap-dir", tmp + "/missing", "--pr", "7"]), 2)
                with patch.object(roadmap_label, "canonical_areas", side_effect=PermissionError(13, "denied")):
                    self.assertEqual(roadmap_label.main(["--roadmap-dir", tmp, "--pr", "7"]), 2)
            api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
