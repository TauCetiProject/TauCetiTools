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
            "TAUCETI_GITHUB_STATE_URL",
            "TAUCETI_CONTENT_OWNER",
            "TAUCETI_CONTENT_NAME",
        ):
            self.assertEqual(values[name], "", name)
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
                    "",
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
        self.assertEqual(github.qualified_environment(), github.environment())

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


if __name__ == "__main__":
    unittest.main()
