"""Exercise setup previews and PR creation against local Git repositories."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.cli import main
from tauceti_tools.credentials import app_setup
from tauceti_tools.doctor import Finding
from tauceti_tools.project import CANONICAL, ConfigError, load
from tauceti_tools.templates import ASSETS, configuration_pr, materialize, write_project


class SetupFixture(unittest.TestCase):
    def setUp(self):
        from tauceti_tools.scaffold import initial_configuration

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "tauceti.toml"
        self.config.write_text(initial_configuration("one/Project", "human") +
                               '\n[tools.tools]\nrepo="TauCetiProject/TauCetiTools"\nref="' + "a" * 40 +
                               '"\n[tools.review]\nrepo="TauCetiProject/TauCetiReview"\nref="' + "b" * 40 + '"\n')
        self.project = load(str(self.config))


class SetupTemplateTests(SetupFixture):
    def test_generated_content_and_roadmap_ignore_lake_without_replacing_rules(self):
        for role in ("content", "roadmap"):
            with self.subTest(role=role):
                checkout = self.root / role
                checkout.mkdir()
                subprocess.run(["git", "init", "-q", str(checkout)], check=True)
                ignore = checkout / ".gitignore"
                ignore.write_text("existing-rule")
                self.assertIn(".gitignore", write_project(self.project, checkout, role))
                self.assertEqual(ignore.read_text(), "existing-rule\n/.lake/\n")
                self.assertNotIn(".gitignore", write_project(self.project, checkout, role))
                (checkout / ".lake").mkdir()
                (checkout / ".lake/artifact").touch()
                subprocess.run(["git", "-C", str(checkout), "check-ignore", "-q", ".lake/artifact"], check=True)

    def test_minimal_setup_keeps_lean_and_policy_and_loads_scripts_at_runtime(self):
        checkout = self.root / "checkout"
        checkout.mkdir()
        (checkout / "scripts").mkdir()
        (checkout / "scripts/fleet_runner.py").write_text("existing project customization\n")
        (checkout / "scripts/lint-baseline.txt").write_text("existing baseline\n")
        written = write_project(self.project, checkout)
        self.assertLess(len(written), 35)
        for name in ("Axioms.lean", "ModuleSystem.lean", "lint-nolints-allowlist.txt"):
            self.assertTrue((checkout / "scripts" / name).is_file())
        for name in ("bors_observation.py", "claims_access.py", "fleet_recovery.py", "stack_policy_fixtures.json"):
            self.assertFalse((checkout / "scripts" / name).exists())
        self.assertEqual((checkout / "scripts/fleet_runner.py").read_text(), "existing project customization\n")
        self.assertEqual((checkout / "scripts/lint-baseline.txt").read_text(), "existing baseline\n")
        for name in ("claims-access.yml", "pick-runner.yml", "roadmap-label.yml"):
            self.assertFalse((checkout / ".github/workflows" / name).exists())
        shared = self.root / "shared"
        materialize(self.project, shared, policy_dir=checkout / "scripts")
        for name in ("ci_status.py", "housekeeping.py", "prepare_docs.py", "lint-env.sh"):
            self.assertTrue((shared / name).is_file())
        self.assertEqual((shared / "lint-baseline.txt").read_text(), "existing baseline\n")
        self.assertIn("Prepare pinned shared tools", (checkout / ".github/workflows/ci.yml").read_text())

    def test_enabled_templates_and_shared_entrypoints_pin_external_actions(self):
        self.project.raw["features"].update(docs=True, progress=True, claims=True, zulip=True,
                                            fleet=True, security_audit=True)
        self.project.raw["tools"]["progress"] = {"repo": "TauCetiProject/TauCetiProgress", "ref": "c" * 40}
        for role in ("content", "roadmap", "ci"):
            checkout = self.root / role
            checkout.mkdir()
            write_project(self.project, checkout, role)
            for path in checkout.rglob("*.yml"):
                for action, ref in re.findall(r"uses:\s+((?:actions/|leanprover/)[^@\s]+)@([^\s#]+)", path.read_text()):
                    self.assertRegex(ref, r"^[0-9a-f]{40}$", f"{path}: {action}")
        for path in (".github/actions/setup/action.yml", ".github/workflows/pr-build.yml",
                     ".github/workflows/publish-lake-cache.yml"):
            text = (ASSETS.parent.parent / path).read_text()
            self.assertNotRegex(text, r"uses:\s+actions/[^@\s]+@v\d")
        self.assertTrue((self.root / "content/.github/workflows/claims-access.yml").is_file())
        self.assertTrue((self.root / "content/.github/workflows/pick-runner.yml").is_file())

    def test_app_permission_prerequisite_precedes_registration(self):
        _, steps = app_setup(self.project, "automation", organization=True)
        self.assertIn("organization owner or an authorized GitHub App manager", steps[0])
        self.assertIn("404", steps[0])
        self.assertIn("does not itself grant permission to install", steps[0])

    def test_successful_repair_exits_zero_without_running_an_unrelated_report(self):
        for apply in (False, True):
            with self.subTest(apply=apply), patch("tauceti_tools.doctor.repair") as repair, \
                    patch("tauceti_tools.doctor.diagnose") as diagnose, redirect_stdout(io.StringIO()):
                args = ["doctor", "--conf", str(self.config), "--fix", "workflows"]
                self.assertEqual(main(args + (["--apply"] if apply else [])), 0)
                repair.assert_called_once()
                diagnose.assert_not_called()
        with patch("tauceti_tools.doctor.repair", side_effect=ConfigError("push failed")), \
                patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(["doctor", "--conf", str(self.config), "--fix", "workflows", "--apply"]), 2)
        with patch("tauceti_tools.doctor.diagnose", return_value=[Finding("test", "failed", "missing", "present")]), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["doctor", "--conf", str(self.config)]), 1)



class BumpGuardDispatchTests(SetupFixture):
    def setUp(self):
        super().setUp()
        self.base = self.root / "base"
        self.candidate = self.root / "candidate with spaces"
        manifest = {"packages": [
            {"name": "cslib", "type": "git", "url": "https://github.com/leanprover/cslib",
             "rev": "a" * 40, "inputRev": "main", "inherited": False},
            {"name": "mathlib", "type": "git",
             "url": "https://github.com/leanprover-community/mathlib4",
             "rev": "b" * 40, "inputRev": "master", "inherited": True},
        ]}
        for directory in (self.base, self.candidate):
            directory.mkdir()
            (directory / "lakefile.toml").write_text('name = "Project"\n')
            (directory / "lean-toolchain").write_text("leanprover/lean4:v4.36.0-rc1\n")
            (directory / "lake-manifest.json").write_text(json.dumps(manifest))
            manifest["packages"][0]["rev"] = "c" * 40
        self.calls = self.root / "guard-call"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        python = bin_dir / "python3"
        python.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "-m" ] && [ "$2" = "tauceti_tools" ] && '
            '[ "$3" = "upstreams" ] && [ "$4" = "guard" ]; then\n'
            '  printf "%s\\n" "$@" > "$GUARD_CALL"\n'
            '  exit "$GUARD_EXIT"\n'
            'fi\n'
            'exec "$GUARD_PYTHON" "$@"\n'
        )
        python.chmod(0o755)
        self.env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                        GUARD_PYTHON=sys.executable, GUARD_CALL=str(self.calls),
                        GUARD_EXIT="0", TAUCETI_REPO=self.project.content,
                        TAUCETI_CONF_SNAPSHOT=str(self.config))

    def run_guard(self):
        shared = self.root / "shared"
        materialize(self.project, shared)
        return subprocess.run(
            ["bash", str(shared / "check-bump.sh"), str(self.base), str(self.base), str(self.candidate)],
            env=self.env, capture_output=True, text=True,
        )

    def test_materialized_downstream_dispatches_cslib_only_bump_and_preserves_exit_status(self):
        for status in (0, 7):
            with self.subTest(status=status):
                self.env["GUARD_EXIT"] = str(status)
                result = self.run_guard()
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertEqual(self.calls.read_text().splitlines(), [
                    "-m", "tauceti_tools", "upstreams", "guard", "--conf", str(self.config),
                    "--base", str(self.base), "--merge-base", str(self.base),
                    "--checkout", str(self.candidate),
                ])

    def test_legacy_and_unconfigured_projects_keep_mathlib_guard(self):
        for mode in ("canonical", "no-snapshot", "no-repository"):
            with self.subTest(mode=mode):
                with patch.dict(self.env):
                    self.project.raw["repositories"]["content"] = "one/Project"
                    if mode == "canonical":
                        self.project.raw["repositories"]["content"] = CANONICAL
                        self.env["TAUCETI_REPO"] = CANONICAL
                    elif mode == "no-snapshot":
                        self.env.pop("TAUCETI_CONF_SNAPSHOT")
                    else:
                        self.env.pop("TAUCETI_REPO")
                    result = self.run_guard()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("mathlib rev unchanged but the manifest changed", result.stdout)
                    self.assertFalse(self.calls.exists())


class SetupPRTests(SetupFixture):
    def setUp(self):
        super().setUp()
        self.remote = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.git("init", "--bare", "--initial-branch=trunk", str(self.remote))
        self.git("clone", str(self.remote), str(self.seed))
        self.git("-C", str(self.seed), "config", "user.name", "Setup Test")
        self.git("-C", str(self.seed), "config", "user.email", "setup@example.com")
        (self.seed / "README.md").write_text("Initial repository\n")
        self.git("-C", str(self.seed), "add", "README.md")
        self.git("-C", str(self.seed), "commit", "-m", "Initial")
        self.git("-C", str(self.seed), "push", "origin", "trunk")
        self.clones = []
        self.prs = []
        self.fail_push = False
        self.fail_pr = False
        self.existing = ""
        self.real_run = subprocess.run

    @staticmethod
    def git(*args):
        return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + os.devnull, *args],
                              check=True, capture_output=True, text=True).stdout

    def mock_run(self, command, **kwargs):
        if command[:3] == ["gh", "api", "--paginate"]:
            return subprocess.CompletedProcess(command, 0, self.existing, "")
        if command[:3] == ["gh", "repo", "clone"]:
            directory = Path(command[4])
            self.clones.append(directory)
            result = self.real_run(["git", "clone", self.remote.as_uri(), str(directory)], check=True, capture_output=True)
            for key, value in (("commit.gpgsign", "false"), ("core.hooksPath", os.devnull)):
                self.real_run(["git", "-C", str(directory), "config", key, value], check=True)
            return result
        if command[:3] == ["gh", "pr", "create"]:
            self.prs.append((command, Path(command[command.index("--body-file") + 1]).read_text()))
            if self.fail_pr:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)
        if command[:3] == ["git", "config", "--get"]:
            value = "Setup Test" if command[-1] == "user.name" else "setup@example.com"
            return subprocess.CompletedProcess(command, 0, value + "\n", "")
        if "push" in command and self.fail_push:
            return subprocess.CompletedProcess(command, 1)
        if "stdout" not in kwargs and not kwargs.get("capture_output"):
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE
        return self.real_run(command, **kwargs)

    def prepare(self, apply=False):
        output = io.StringIO()
        with patch("tauceti_tools.templates.subprocess.run", side_effect=self.mock_run), redirect_stdout(output):
            configuration_pr(self.project, apply=apply)
        return output.getvalue()

    def test_preview_apply_reuse_branch_and_clean_temporary_checkouts(self):
        preview = self.prepare()
        self.assertFalse(self.clones[-1].exists())
        self.assertFalse(self.prs)
        applied = self.prepare(apply=True)
        self.assertFalse(self.clones[-1].exists())
        branch = re.search(r"Setup branch: (\S+)", preview)[1]
        self.assertIn("Setup branch: " + branch, applied)
        command, body = self.prs[0]
        self.assertEqual(command[command.index("--repo") + 1], "one/Project")
        self.assertEqual(command[command.index("--base") + 1], "trunk")
        self.assertEqual(command[command.index("--head") + 1], branch)
        self.assertIn("Roadmap: none", body)
        self.assertIn(branch, self.git("--git-dir", str(self.remote), "branch", "--list"))

    def test_changed_configuration_changes_branch(self):
        first = self.prepare()
        self.project.raw["features"]["claims"] = True
        second = self.prepare()
        self.assertNotEqual(re.search(r"Setup branch: (\S+)", first)[1],
                            re.search(r"Setup branch: (\S+)", second)[1])
        self.assertTrue(all(not p.exists() for p in self.clones))

    def test_existing_pr_does_not_create_a_clone(self):
        self.existing = "https://github.com/one/Project/pull/1\n"
        self.assertIn(self.existing.strip(), self.prepare(apply=True))
        self.assertFalse(self.clones)

    def test_merged_setup_needs_no_pr_and_cleans_clone(self):
        applied = self.prepare(apply=True)
        branch = re.search(r"Setup branch: (\S+)", applied)[1]
        self.git("--git-dir", str(self.remote), "update-ref", "refs/heads/trunk", "refs/heads/" + branch)
        self.assertIn("Already configured; no PR needed", self.prepare(apply=True))
        self.assertEqual(len(self.prs), 1)
        self.assertFalse(self.clones[-1].exists())

    def test_preview_failure_cleans_clone(self):
        with patch("tauceti_tools.templates.write_project", side_effect=ValueError("render failed")):
            with self.assertRaisesRegex(ValueError, "render failed"):
                self.prepare()
        self.assertFalse(self.clones[-1].exists())

    def test_failed_push_or_pr_creation_retains_prepared_commit(self):
        for failure in ("fail_push", "fail_pr"):
            with self.subTest(failure=failure):
                setattr(self, failure, True)
                with self.assertRaises((ConfigError, subprocess.CalledProcessError)):
                    self.prepare(apply=True)
                setattr(self, failure, False)
                directory = self.clones[-1]
                self.assertTrue(directory.exists())
                self.assertEqual(self.git("-C", str(directory), "log", "-1", "--format=%s").strip(),
                                 "Configure shared Tau Ceti project tooling")


if __name__ == "__main__":
    unittest.main()
