"""Project boundaries, pinned configuration inheritance and read-only repair previews."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.doctor import diagnose, repair
from tauceti_tools.project import ConfigError, bootstrap, parse
from tauceti_tools.scaffold import initial_configuration


class ProjectTests(unittest.TestCase):
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
        findings = diagnose(project, github=Unavailable())
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


if __name__ == "__main__":
    unittest.main()
