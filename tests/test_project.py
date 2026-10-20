"""Project boundaries, pinned configuration inheritance and read-only repair previews."""

import json
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.doctor import diagnose, repair
from tauceti_tools.project import ConfigError, bootstrap, configuration_source, parse
from tauceti_tools.scaffold import initial_configuration


class ProjectTests(unittest.TestCase):
    def test_configuration_discovery_and_explicit_overrides(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            config = root / "tauceti.toml"
            config.touch()
            with patch("tauceti_tools.project.Path.cwd", return_value=root), patch("subprocess.run") as run:
                self.assertEqual(configuration_source(), str(config))
                os.environ["TAUCETI_CONF"] = "environment.toml"
                self.assertEqual(configuration_source(), "environment.toml")
                self.assertEqual(configuration_source("explicit.toml"), "explicit.toml")
                run.assert_not_called()

    def test_git_discovery_respects_checkout_and_worktree_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ):
            for name in ("TAUCETI_CONF", "GIT_DIR", "GIT_WORK_TREE"):
                os.environ.pop(name, None)
            root = Path(tmp) / "project"
            root.mkdir()
            (Path(tmp) / "tauceti.toml").touch()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                            "commit", "-qm", "Initial", "--allow-empty"], check=True)
            subdirectory = root / "src"
            subdirectory.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(subdirectory)
                self.assertIsNone(configuration_source())
                (root / "tauceti.toml").touch()
                self.assertEqual(configuration_source(), str(root / "tauceti.toml"))
                nested = subdirectory / "tauceti.toml"
                nested.touch()
                self.assertEqual(configuration_source(), str(nested))
                nested.unlink()
                subprocess.run(["git", "init", "-q", str(subdirectory)], check=True)
                self.assertIsNone(configuration_source())
                os.chdir(root)
                worktree = Path(tmp) / "worktree"
                subprocess.run(["git", "worktree", "add", "-qb", "other", str(worktree)], check=True)
                (worktree / "tauceti.toml").touch()
                (worktree / "src").mkdir()
                os.chdir(worktree / "src")
                self.assertEqual(configuration_source(), str(worktree / "tauceti.toml"))
            finally:
                os.chdir(previous)

    def test_downstream_docs_use_parent_pins_and_need_no_website(self):
        from tauceti_tools.templates import ASSETS, write_project

        text = initial_configuration("one/Project", "human").replace("docs = false", "docs = true")
        text += '\n[tools.docgen]\nrepo="leanprover/doc-gen4"\nref="' + "a" * 40 + '"\n'
        project = parse(text.encode())
        spec = importlib.util.spec_from_file_location("prepare_docs", ASSETS / "scripts/prepare_docs.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lakefile.toml").write_text('name = "Project"\n')
            (root / "lean-toolchain").write_text("leanprover/lean4:v4.36.0-rc1\n")
            (root / "Project").mkdir()
            (root / "Project/Basic.lean").write_text("module\n")
            (root / "lake-manifest.json").write_text(json.dumps({"packages": [
                {"name": "mathlib", "rev": "1" * 40}, {"name": "Cli", "rev": "2" * 40},
            ]}))
            upstream = {"version": "1.3.0", "packages": [
                {"name": "Cli", "rev": "0" * 40}, {"name": "UnicodeBasic", "rev": "3" * 40},
            ]}
            with patch.object(helper, "bootstrap", return_value=project), \
                    patch.object(helper.Path, "cwd", return_value=root), \
                    patch.object(helper.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(upstream).encode())):
                helper.main()
            packages = {p["name"]: p for p in json.loads((root / "docbuild/lake-manifest.json").read_text())["packages"]}
            self.assertEqual(packages["mathlib"]["rev"], "1" * 40)
            self.assertEqual(packages["Cli"]["rev"], "2" * 40)
            self.assertEqual(packages["UnicodeBasic"]["rev"], "3" * 40)
            self.assertIn("import Project.Basic", (root / "docbuild/ProjectDocs.lean").read_text())
            write_project(project, root)
            workflow = (root / ".github/workflows/api-docs.yml").read_text()
            self.assertNotIn("web/", workflow)
            self.assertNotIn("sync_manifest.py", workflow)
            self.assertIn("Deploy to GitHub Pages", workflow)
            self.assertFalse((root / ".github/workflows/pages.yml").exists())

    def test_project_and_worker_state_are_separate(self):
        a = parse(initial_configuration("one/Project", "human").encode())
        b = parse(initial_configuration("two/Project", "human").encode())
        self.assertNotEqual(a.state_root, b.state_root)
        self.assertNotEqual(a.key, b.key)
        self.assertEqual(a.environment()["TAUCETI_DATA_REPO"], "")
        self.assertEqual(a.environment()["TAUCETI_GITHUB_STATE_URL"], "")
        self.assertEqual(a.environment()["TAUCETI_CACHE_SERVICE"], "")

    def test_root_rendering_handles_shell_words_without_changing_shared_repositories(self):
        from tauceti_tools.templates import render
        project = parse(initial_configuration("kim-em/Arcturus", "human").encode())
        source = "find TauCeti -name '*.lean'\nTauCetiProject/TauCetiTools\nTauCetiProject/TauCetiReview\n"
        self.assertEqual(render(source, project),
                         "find Arcturus -name '*.lean'\nTauCetiProject/TauCetiTools\nTauCetiProject/TauCetiReview\n")

    def test_configuration_snapshot_does_not_follow_file_edits(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_STATE_HOME": tmp}, clear=True):
            config = Path(tmp) / "project.toml"
            config.write_text(initial_configuration("one/Project", "human"))
            bootstrap(["--conf", str(config)])
            saved = Path(os.environ["TAUCETI_CONF_SNAPSHOT"])
            config.write_text(initial_configuration("two/Project", "human"))
            child = subprocess.check_output(
                [sys.executable, "-c", "from tauceti_tools.project import bootstrap; print(bootstrap([]).content)"],
                text=True,
            )
            self.assertEqual(child.strip(), "one/Project")
            self.assertIn('content = "one/Project"', saved.read_text())

    def test_paths_and_mutable_tool_refs_are_rejected(self):
        text = initial_configuration("one/Project", "human")
        for bad in (
            text.replace('active_dir = "ProjectRoadmap"', 'active_dir = "../other"'),
            text + '\n[tools.tools]\nrepo="one/Tools"\nref="main"\n',
            text.replace('roots = ["Project"]', 'roots = ["$(env)"]'),
        ):
            with self.assertRaises(ConfigError):
                parse(bad.encode())

    def test_inaccessible_repository_has_no_false_absence_or_implicit_creation(self):
        class Unavailable:
            def read(self, path):
                return None, "GitHub HTTP 404"

        project = parse(initial_configuration("one/Project", "human").encode(), "/tmp/project.toml")
        findings = diagnose(project, extras=False, github=Unavailable())
        self.assertTrue(findings)
        for item in findings:
            self.assertNotEqual(item.status, "ready")
            self.assertTrue(item.commands)
            self.assertTrue(item.verify)
            self.assertTrue(item.steps)
            json.dumps(item.json())
        with patch("subprocess.run") as run:
            repair(project, "actions", apply=False)
            run.assert_not_called()

    def test_failed_docs_build_is_not_hidden_by_skipped_deployment(self):
        from tauceti_tools.doctor import extra_findings

        class GitHub:
            def read(self, path, **kwargs):
                if "/runs?" in path:
                    return {"workflow_runs": [{"id": 2, "conclusion": "failure"},
                                              {"id": 1, "conclusion": "success"}]}, ""
                if path.endswith("/2/jobs"):
                    return {"jobs": [{"steps": [{"name": "Generate API documentation", "conclusion": "failure"}]}]}, ""
                return {}, ""

        project = parse(initial_configuration("one/Project", "human").replace("docs = false", "docs = true").encode())
        finding = next(f for f in extra_findings(project, GitHub(), "doctor") if f.id == "features.docs")
        self.assertEqual(finding.status, "failed")
        self.assertIn("Run 2 failed", finding.summary)

    def test_recorded_neon_grant_does_not_claim_runtime_is_ready(self):
        from tauceti_tools.doctor import extra_findings

        project = parse(initial_configuration("one/Project", "human").encode())
        project.raw["services"]["bors"]["mode"] = "hosted"
        with tempfile.TemporaryDirectory() as tmp, patch("tauceti_tools.doctor.Path.home", return_value=Path(tmp)):
            registry = Path(tmp) / ".config/tauceti/neon-projects.json"
            registry.parent.mkdir(parents=True)
            registry.write_text(json.dumps({"one/Project": {"approved": True, "project_id": "project-123"}}))
            rows = {f.id: f for f in extra_findings(project, object(), "doctor")}
            self.assertEqual(rows["services.bors.enrollment"].status, "ready")
            self.assertEqual(rows["services.bors"].status, "pending")
            registry.write_text(json.dumps({"other/Project": {"approved": True, "project_id": "project-456"}}))
            rows = {f.id: f for f in extra_findings(project, object(), "doctor")}
            self.assertEqual(rows["services.bors.enrollment"].status, "pending")


if __name__ == "__main__":
    unittest.main()
