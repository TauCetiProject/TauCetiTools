"""First-run diagnostics distinguish missing setup from access failures."""

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.doctor import GitHub, diagnose, render
from tauceti_tools.project import parse
from tauceti_tools.scaffold import initial_configuration


class DoctorSetupTests(unittest.TestCase):
    def test_public_tau_ceti_mirror_needs_no_token_or_enrollment(self):
        from tauceti_tools.doctor import extra_findings
        project = parse(initial_configuration("TauCetiProject/TauCeti", "human").encode(), "/tmp/tauceti.toml")
        for absent in (False, True):
            if absent:
                project.raw["services"].pop("github_state")
            with patch("tauceti_tools.github_state_setup.client_token_file", side_effect=AssertionError("no token needed")):
                findings = extra_findings(project, GitHub(), "again")
            finding = next(f for f in findings if f.id == "services.github_state")
            self.assertEqual(finding.status, "ready")
            self.assertIn("no client token", finding.summary)

    def setUp(self):
        self.project = parse(initial_configuration("one/Project", "human").encode(), "/tmp/tauceti.toml")
        self.responses = {}
        for repo in ("one/Project", "one/ProjectRoadmap"):
            self.responses[f"repos/{repo}"] = {
                "owner": {"type": "Organization"}, "default_branch": "main",
                "permissions": {"admin": False, "push": True},
            }
            self.responses[f"repos/{repo}/commits/main"] = {"sha": "a" * 40}

    def api(self, command, **kwargs):
        response = self.responses.get(command[-1], 404)
        if isinstance(response, int):
            return subprocess.CompletedProcess(command, 1, "", f"gh: private response body (HTTP {response})")
        return subprocess.CompletedProcess(command, 0, json.dumps([response] if "--slurp" in command else response), "")

    def findings(self):
        with patch("tauceti_tools.doctor.subprocess.run", side_effect=self.api):
            return {f.id: f for f in diagnose(self.project, extras=False, github=GitHub())}

    def publish_package(self, repo, *, lakefile="lakefile.toml"):
        for name in ("lean-toolchain", "lake-manifest.json", lakefile):
            self.responses[f"repos/{repo}/contents/{name}?ref=main"] = {"size": 20}
        directories = ["Project"] if repo == "one/Project" else ["ProjectRoadmap", "Completed"]
        for directory in directories:
            self.responses[f"repos/{repo}/contents/{directory}?ref=main"] = [{"name": ".gitkeep", "type": "file"}]

    def test_missing_files_are_failed_and_identify_the_published_branch(self):
        rows = self.findings()
        for name in ("lean.content", "lean.roadmap", "files.tauceti.toml", "roadmap.workflows"):
            with self.subTest(name=name):
                self.assertEqual(rows[name].status, "failed")
                self.assertIn("Missing file:", rows[name].summary)
                self.assertIn("at main", rows[name].summary)
                self.assertNotIn("inaccessible does not establish absence", rows[name].summary)
                self.assertNotIn("private response body", rows[name].summary)

    def test_repository_revision_and_file_access_failures_stay_unknown(self):
        for endpoint, code in (
            ("repos/one/Project", 404),
            ("repos/one/Project/commits/main", 404),
            ("repos/one/Project/commits/main", 403),
            ("repos/one/Project/contents/lean-toolchain?ref=main", 403),
        ):
            with self.subTest(endpoint=endpoint, code=code):
                previous = self.responses.get(endpoint)
                self.responses[endpoint] = code
                with patch("tauceti_tools.doctor.subprocess.run", side_effect=self.api):
                    _, error = GitHub().read("repos/one/Project/contents/lean-toolchain?ref=main")
                self.assertIn("inaccessible does not establish absence", error)
                if previous is None:
                    del self.responses[endpoint]
                else:
                    self.responses[endpoint] = previous

    def test_revision_access_is_cached_across_missing_files(self):
        with patch("tauceti_tools.doctor.subprocess.run", side_effect=self.api) as run:
            gh = GitHub()
            gh.read("repos/one/Project/contents/lean-toolchain?ref=main")
            gh.read("repos/one/Project/contents/lake-manifest.json?ref=main")
        endpoints = [call.args[0][-1] for call in run.call_args_list]
        self.assertEqual(endpoints.count("repos/one/Project"), 1)
        self.assertEqual(endpoints.count("repos/one/Project/commits/main"), 1)

    def test_actions_403_explains_account_and_token_permissions(self):
        self.responses["repos/one/Project/actions/permissions"] = 403
        permissions = self.responses["repos/one/Project"]["permissions"]
        for admin, expected in ((False, "ask a repository administrator or organization owner"),
                                (True, "your account has repository admin access"),
                                (None, "requires repository admin access")):
            with self.subTest(admin=admin):
                permissions["admin"] = admin
                finding = self.findings()["actions.enabled"]
                self.assertEqual(finding.status, "unknown")
                self.assertIn(expected, finding.summary)
                self.assertIn("viewerPermission", finding.commands[0])
        self.responses["repos/one/Project/actions/permissions"] = {"enabled": True}
        self.assertEqual(self.findings()["actions.enabled"].status, "ready")

    def test_local_build_guidance_and_roadmap_prerequisite(self):
        rows = self.findings()
        self.assertIn("Finish the content package", rows["lean.roadmap"].summary)
        self.assertNotIn("$ gh api", "\n".join(rows["lean.roadmap"].steps))
        self.publish_package("one/Project")
        rows = self.findings()
        for role in ("content", "roadmap"):
            steps = "\n".join(rows["lean." + role].steps)
            self.assertIn("reads GitHub at one/Project", steps)
            self.assertIn("not your local checkout", steps)
            self.assertIn("merge the PR into main", steps)
            self.assertLess(steps.index(".gitignore"), steps.index("$ lake update"))
        roadmap = "\n".join(rows["lean.roadmap"].steps)
        self.assertLess(roadmap.index("First finish the Content Lean package"), roadmap.index("$ gh api"))
        self.publish_package("one/Project")
        rows = self.findings()
        self.assertEqual(rows["lean.content"].status, "ready")
        self.assertNotIn("Finish the content package", rows["lean.roadmap"].summary)
        self.publish_package("one/ProjectRoadmap", lakefile="lakefile.lean")
        self.assertEqual(self.findings()["lean.roadmap"].status, "ready")

    def test_optional_roadmap_lakefile_missing_does_not_prove_other_format_absent(self):
        self.publish_package("one/ProjectRoadmap")
        self.responses["repos/one/ProjectRoadmap/contents/lakefile.toml?ref=main"] = 404
        self.responses["repos/one/ProjectRoadmap/contents/lakefile.lean?ref=main"] = 403
        self.assertEqual(self.findings()["lean.roadmap"].status, "unknown")

    def test_build_and_merge_have_distinct_triggers_in_rendered_output(self):
        rows = self.findings()
        build, merge = rows["evidence.pr-build"], rows["evidence.auto-merge"]
        output = io.StringIO()
        with redirect_stdout(output):
            render([build, merge])
        build_text, merge_text = output.getvalue().split("\nAutomatic merging\n")
        self.assertIn("pr-build runs on normal content changes", build_text)
        self.assertNotIn("gh pr list", build_text)
        self.assertNotIn("no manual-dispatch trigger", build_text)
        self.assertIn("no manual-dispatch trigger", merge_text)
        self.assertNotIn("gh workflow run", merge_text)

    @unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
    def test_toolchain_fetch_commands_run_under_zsh(self):
        self.publish_package("one/Project")
        rows = self.findings()
        for role in ("content", "roadmap"):
            steps = rows["lean." + role].steps
            command, = [line[2:] for step in steps for line in step.splitlines() if line.startswith("$ gh api")]
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run(
                    ["zsh", "-f", "-c", "gh() { printf 'leanprover/lean4:v4.test\\n'; }; " + command],
                    cwd=tmp, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((Path(tmp) / "lean-toolchain").read_text(), "leanprover/lean4:v4.test\n")

    def test_gitignore_instruction_preserves_rules_and_excludes_lake_artifacts(self):
        self.publish_package("one/Project")
        steps = self.findings()["lean.content"].steps
        command, = [line[2:] for step in steps for line in step.splitlines() if line.startswith("$ git check-ignore")]
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            root = Path(tmp)
            (root / ".gitignore").write_text("existing-rule\n")
            for _ in range(2):
                subprocess.run(["sh", "-c", command], cwd=tmp, check=True)
            self.assertEqual((root / ".gitignore").read_text(), "existing-rule\n\n/.lake/\n")
            (root / ".lake/packages").mkdir(parents=True)
            (root / ".lake/packages/artifact").touch()
            result = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                                    cwd=tmp, check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout, ".gitignore\n")


if __name__ == "__main__":
    unittest.main()
