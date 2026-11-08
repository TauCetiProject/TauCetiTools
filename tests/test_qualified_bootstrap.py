"""Mixed-forge subprocesses keep one snapshot and cannot inherit GitHub destinations."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_tools import project
from tauceti_tools.scaffold import initial_configuration

CONFIG = """schema_version = 2
[forges.source]
kind = "forgejo"
url = "https://source.example"
[repositories]
content = {forge="source", name="one/Project"}
roadmap = {forge="source", name="one/Roadmap"}
ci = "one/Archive"
[ci]
backend = "github-actions"
repository = "one/Execution"
[tools.review]
repo = "one/Review"
ref = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
"""


class QualifiedBootstrap(unittest.TestCase):
    def test_native_exports_clear_all_legacy_destinations_and_preserve_credentials(self):
        legacy = project.parse(initial_configuration("TauCetiProject/TauCeti", "human").encode()).environment()
        native = project.parse(CONFIG.encode())
        values = native.qualified_environment()
        self.assertLessEqual(legacy.keys(), values.keys())
        for name in (
            "TAUCETI_REPO",
            "TAUCETI_ROADMAP_REPO",
            "TAUCETI_CI_REPO",
            "TAUCETI_LEDGER_REPO",
            "TAUCETI_CONTENT_OWNER",
            "TAUCETI_CONTENT_NAME",
        ):
            self.assertEqual(values[name], project.QUALIFIED_ONLY, name)
        self.assertEqual(values["TAUCETI_GITHUB_STATE_URL"], "")
        self.assertEqual(values["TAUCETI_BORS_URL"], "")
        self.assertEqual(values["TAUCETI_BORS_REPOSITORY_ID"], "")
        self.assertEqual(values["TAUCETI_LEAN_ROOTS"], '["Project"]')
        self.assertEqual(native.repository_ref("content").forge.url, "https://source.example")
        self.assertEqual(native.ci_repository_ref().name, "one/Execution")
        self.assertEqual(native.repository_ref("ci").name, "one/Archive")
        self.assertEqual(native.tool_repository_ref("review").name, "one/Review")
        self.assertIsNone(native.tool_repository_ref("progress"))
        for name in ("GH_TOKEN", "TAUCETI_FORGEJO_ORIGIN", "TAUCETI_FORGEJO_TOKEN_FILE"):
            self.assertNotIn(name, values)
        with self.assertRaisesRegex(project.ConfigError, "GitHub-only"):
            native.environment()

    def test_qualified_children_reuse_the_snapshot_and_legacy_children_reject_it(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ), patch.object(project, "_active", None):
            root = Path(tmp)
            config = root / "project.toml"
            config.write_text(CONFIG)
            for name in ("TAUCETI_CONF", "TAUCETI_CONF_SOURCE", "TAUCETI_CONF_SNAPSHOT"):
                os.environ.pop(name, None)
            os.environ.update(XDG_STATE_HOME=str(root), TAUCETI_REPO="old/Mirror", GH_TOKEN="sentinel")
            native = project.bootstrap_qualified(["--conf", str(config)])
            snapshot = Path(os.environ["TAUCETI_CONF_SNAPSHOT"])
            self.assertEqual(snapshot.read_text(), CONFIG)
            config.write_text(CONFIG.replace("https://source.example", "https://changed.example"))
            code = (
                "import json,os; from tauceti_tools.project import bootstrap_qualified; "
                "p=bootstrap_qualified([]); print(json.dumps([p.repository_ref('content').url, "
                "p.ci_repository_ref().url,os.environ['TAUCETI_REPO'],os.environ['GH_TOKEN'],"
                "os.environ['TAUCETI_CONF_SNAPSHOT']]))"
            )
            result = json.loads(subprocess.check_output([sys.executable, "-c", code], text=True))
            self.assertEqual(
                result,
                [
                    "https://source.example/one/Project",
                    "https://github.com/one/Execution",
                    project.QUALIFIED_ONLY,
                    "sentinel",
                    str(snapshot),
                ],
            )
            legacy = subprocess.run(
                [sys.executable, "-c", "from tauceti_tools.project import bootstrap; bootstrap([])"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(legacy.returncode, 0)
            self.assertIn("GitHub-only", legacy.stderr)
            self.assertIs(project.active(), native)
            with self.assertRaises(project.ConfigError):
                project.bootstrap([])
            self.assertIs(project.active(), native)

    def test_github_qualified_exports_preserve_existing_configuration_behavior(self):
        github = project.parse(initial_configuration("TauCetiProject/TauCeti", "human").encode())
        qualified = github.qualified_environment()
        self.assertEqual({key: qualified[key] for key in github.environment()}, github.environment())
        self.assertEqual(qualified["TAUCETI_QUALIFIED_ONLY"], "")

    def test_ordinary_github_exports_preserve_operator_owned_overrides(self):
        github = project.parse(initial_configuration("one/Project", "human").encode())
        overrides = dict(
            MERGE_BACKEND="bors",
            SELF_TESTED_MERGE_BACKEND="bors",
            CLAIM_REPO="operator/Claims",
            CLAIM_GIT_URL="https://github.com/operator/Claims",
            TAUCETI_CACHE_REPOSITORY="one/Execution",
        )
        with patch.dict(os.environ, overrides):
            os.environ.pop("TAUCETI_QUALIFIED_ONLY", None)
            exported = github.qualified_environment()
            self.assertFalse(set(overrides) & exported.keys())
            os.environ.update(exported)
            for key, value in overrides.items():
                self.assertEqual(os.environ[key], value)

    def test_generic_export_refuses_a_source_candidate_context_without_changing_its_guard(self):
        from tauceti_tools.assets.scripts import github_api

        native = project.parse(CONFIG.encode())
        context = dict(
            TAUCETI_SOURCE_CANDIDATE="true",
            TAUCETI_SOURCE_REPOSITORY='{"name":"one/Project"}',
            TAUCETI_CI_EXECUTION_REPOSITORY='{"name":"one/Execution"}',
            TAUCETI_CI_GITHUB_READ_REPOSITORIES='["one/Execution"]',
            TAUCETI_CACHE_REPOSITORY="one/Execution",
        )
        with patch.dict(os.environ, context), patch.object(project, "_active", None):
            before = dict(os.environ)
            with self.assertRaisesRegex(project.ConfigError, "source-candidate CI context"):
                native.qualified_environment()
            with self.assertRaises(project.ConfigError):
                project.bootstrap_qualified([])
            self.assertEqual(dict(os.environ), before)
            self.assertIsNone(project.active())
            with patch.object(
                github_api.GitHub, "_credential", side_effect=AssertionError("must not select credentials")
            ):
                with self.assertRaises(ValueError):
                    github_api.GitHub().request("POST", "repos/one/Execution/statuses/" + "a" * 40)

    def test_raw_claim_helper_refuses_before_git_and_shell_defaults_do_not_choose_github(self):
        native = project.parse(CONFIG.encode())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            called = root / "git-called"
            git = root / "git"
            git.write_text('#!/bin/sh\ntouch "' + str(called) + '"\nexit 99\n')
            git.chmod(0o700)
            env = dict(os.environ, **native.qualified_environment())
            env["PATH"] = str(root) + os.pathsep + os.environ["PATH"]
            script = Path(project.__file__).parent / "assets/scripts/claim.sh"
            result = subprocess.run(
                ["bash", str(script), "acquire", "review/test"], env=env, capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("explicit ForgejoGitClaim", result.stderr)
            self.assertFalse(called.exists())
            selected = subprocess.check_output(
                ["bash", "-c", 'printf "%s" "${TAUCETI_REPO:-TauCetiProject/TauCeti}"'], env=env, text=True
            )
            self.assertEqual(selected, project.QUALIFIED_ONLY)

    def test_configured_github_dependencies_remain_visible_and_native_ones_are_not_empty(self):
        dependencies = '\n[[upstreams]]\nrepo="one/Dependency"\npackage="dep"\n'
        native = project.parse((CONFIG + dependencies).encode())
        self.assertEqual(
            json.loads(native.qualified_environment()["TAUCETI_UPSTREAMS"]),
            [{"repo": "one/Dependency", "package": "dep"}],
        )
        other = project.parse(
            (
                CONFIG + dependencies.replace('repo="one/Dependency"', 'repo={forge="source",name="one/Dependency"}')
            ).encode()
        )
        with self.assertRaises(ValueError):
            json.loads(other.qualified_environment()["TAUCETI_UPSTREAMS"])

    def test_native_exports_do_not_retain_configured_legacy_bors_approval(self):
        native = project.parse(
            (CONFIG + '\n[services.bors]\nmode="custom"\nurl="https://queue.example"\nrepository_id=44\n').encode()
        )
        values = native.qualified_environment()
        self.assertEqual(values["TAUCETI_BORS_URL"], "")
        self.assertEqual(values["TAUCETI_BORS_REPOSITORY_ID"], "")
        self.assertEqual(values["TAUCETI_MERGE_BACKEND"], "queue")
        self.assertEqual(values["MERGE_BACKEND"], "queue")
        self.assertEqual(values["SELF_TESTED_MERGE_BACKEND"], "queue")

    def test_typed_pins_keep_rubric_overrides_and_have_no_default_repository(self):
        native = project.parse(CONFIG.encode())
        self.assertEqual(native.tool_pin("review"), (native.tool_repository_ref("review"), "a" * 40))
        self.assertEqual(native.rubrics_pin(), native.tool_pin("review"))
        mismatched = CONFIG.replace(
            'ci = "one/Archive"', 'ci = "one/Archive"\nreview={forge="source",name="one/Rubrics"}'
        )
        with self.assertRaises(project.ConfigError):
            project.parse(mismatched.encode()).rubrics_pin()
        override = CONFIG + '\n[review]\nrubrics_ref="' + "b" * 40 + '"\n'
        self.assertEqual(project.parse(override.encode()).rubrics_pin()[1], "b" * 40)
        bare = CONFIG.split("[tools.review]")[0]
        self.assertIsNone(project.parse(bare.encode()).rubrics_pin())
        custom = bare.replace('ci = "one/Archive"', 'ci = "one/Archive"\nreview={forge="source",name="one/Rubrics"}')
        with self.assertRaises(project.ConfigError):
            project.parse(custom.encode()).rubrics_pin()
        pinned = project.parse((custom + '\n[review]\nrubrics_ref="' + "c" * 40 + '"\n').encode())
        self.assertEqual(pinned.rubrics_pin(), (pinned.repository_ref("review"), "c" * 40))

    def test_github_bootstrap_clears_a_prior_qualified_context(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ), patch.object(project, "_active", None):
            root = Path(tmp)
            config = root / "github.toml"
            config.write_text(initial_configuration("one/Project", "human"))
            os.environ.update(project.parse(CONFIG.encode()).qualified_environment())
            os.environ["XDG_STATE_HOME"] = str(root)
            github = project.bootstrap(["--conf", str(config)])
            self.assertEqual(os.environ["TAUCETI_REPO"], "one/Project")
            self.assertEqual(os.environ["TAUCETI_QUALIFIED_ONLY"], "")
            self.assertEqual(os.environ["CLAIM_GIT_URL"], "")
            self.assertEqual(github.content, "one/Project")

    def test_legacy_active_guard_holds_even_without_a_discoverable_source(self):
        native = project.parse(CONFIG.encode())
        with patch.object(project, "_active", native), patch.object(project, "configuration_source", return_value=None):
            with self.assertRaises(project.ConfigError):
                project.bootstrap([])
            self.assertIs(project.bootstrap_qualified([]), native)

    def test_cli_exports_an_explicit_qualified_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "project.toml"
            config.write_text(CONFIG)
            env = dict(os.environ, XDG_STATE_HOME=str(root), TAUCETI_GITHUB_STATE_URL="https://old.example")
            for name in ("TAUCETI_CONF_SOURCE", "TAUCETI_CONF_SNAPSHOT"):
                env.pop(name, None)
            command = [sys.executable, "-m", "tauceti_tools", "--conf", str(config), "config", "environment"]
            refused = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("GitHub-only", refused.stderr)
            result = subprocess.run([*command, "--qualified"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            values = json.loads(result.stdout)
            self.assertEqual(values["TAUCETI_GITHUB_STATE_URL"], "")
            self.assertEqual(Path(values["TAUCETI_CONF_SNAPSHOT"]).read_text(), CONFIG)
            output = root / "github-env"
            exported = subprocess.run(
                [*command, "--qualified", "--github-env", str(output)], env=env, capture_output=True, text=True
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertIn("TAUCETI_REPO=" + project.QUALIFIED_ONLY + "\n", output.read_text())
            self.assertIn("TAUCETI_QUALIFIED_ONLY=true\n", output.read_text())
            invalid = subprocess.run([*command[:-1], "show", "--qualified"], env=env, capture_output=True, text=True)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("--qualified requires", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
