import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.forge import GITHUB, Unsupported
from tauceti_tools.forge_cli import run
from tauceti_tools.project import ConfigError, parse

CONFIG = """schema_version = 2
[forges.source]
kind = "forgejo"
url = "https://git.example.org"
[repositories]
content = {forge="source", name="one/Project"}
roadmap = "one/Roadmap"
[ci]
backend = "github-actions"
repository = "one/Project"
"""


class ConfigurationTests(unittest.TestCase):
    def test_mixed_hosting_and_legacy_guard(self):
        project = parse(CONFIG.encode())
        self.assertEqual(project.repository_ref("content").forge.kind, "forgejo")
        self.assertEqual(project.repository_ref("roadmap").forge, GITHUB)
        self.assertEqual(project.ci_repository_ref().forge, GITHUB)
        self.assertFalse(project.legacy)
        legacy = parse(b'schema_version=1\n[repositories]\ncontent="one/Project"\nroadmap="one/Roadmap"')
        self.assertEqual(legacy.key, "one__project")
        self.assertNotEqual(project.key, legacy.key)
        with self.assertRaisesRegex(ConfigError, "GitHub-only"):
            project.environment()
        with self.assertRaisesRegex(ConfigError, "GitHub-only"):
            _ = project.content

    def test_invalid_connections_refs_and_execution_provider(self):
        for text in [
            CONFIG.replace('forge="source"', 'forge="missing"'),
            CONFIG.replace('kind = "forgejo"', 'kind = "unknown"'),
            CONFIG.replace('name="one/Project"', 'name="one/.."'),
            CONFIG.replace('repository = "one/Project"', 'repository = {forge="source", name="one/Project"}'),
            CONFIG.replace("schema_version = 2", "schema_version = 2.0"),
        ]:
            with self.subTest(text=text), self.assertRaises(ConfigError):
                parse(text.encode())

    def test_existing_policy_validation_still_applies(self):
        with self.assertRaises(ConfigError):
            parse((CONFIG + '\n[tools.tools]\nrepo="one/Tools"\nref="main"\n').encode())


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "project.toml"
        self.config.write_text(CONFIG)

    def test_forgejo_view_json_projection(self):
        with patch("tauceti_tools.forge_cli.Forgejo") as client, contextlib.redirect_stdout(io.StringIO()) as output:
            client.return_value.pull.return_value = {
                "number": 12,
                "title": "Proof",
                "state": "open",
                "head": {"sha": "a" * 40},
            }
            code = run(["--conf", str(self.config), "pr", "view", "12", "--json", "number,title,headRefOid"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"number": 12, "title": "Proof", "headRefOid": "a" * 40})

    def test_unsupported_mutation_does_not_delegate(self):
        with patch("tauceti_tools.forge_cli._delegate") as delegate:
            with self.assertRaises(Unsupported):
                run(["--conf", str(self.config), "pr", "merge", "12"])
            delegate.assert_not_called()

    def test_unknown_json_field_is_not_fabricated(self):
        with patch("tauceti_tools.forge_cli.Forgejo") as client:
            client.return_value.pull.return_value = {"number": 12}
            with self.assertRaises(Unsupported):
                run(["--conf", str(self.config), "pr", "view", "12", "--json", "statusCheckRollup"])

    def test_explicit_github_host_delegates_with_qualified_environment(self):
        with patch("tauceti_tools.forge_cli._delegate", return_value=0) as delegate:
            run(["--conf", str(self.config), "pr", "view", "12", "-R", "github.com/one/Project"])
        self.assertEqual(delegate.call_args.args[1]["GH_REPO"], "github.com/one/Project")
        self.assertEqual(delegate.call_args.args[1]["GH_HOST"], "github.com")

    def test_same_bare_repository_on_two_forges_is_ambiguous(self):
        self.config.write_text(CONFIG.replace('roadmap = "one/Roadmap"', 'roadmap = "one/Project"'))
        with self.assertRaisesRegex(Unsupported, "ambiguous"):
            run(["--conf", str(self.config), "pr", "view", "12", "-R", "one/Project"])

    def test_delegation_removes_wrapper_flags(self):
        with patch("subprocess.run") as command:
            from tauceti_tools.forge_cli import _delegate

            _delegate(
                ["--conf=a", "pr", "view", "12", "-R", "github.com/one/Project"],
                {**os.environ, "TAUCETI_REAL_GH": "/usr/bin/real-gh"},
                remove_conf=True,
            )
        self.assertEqual(command.call_args.args[0], ["/usr/bin/real-gh", "pr", "view", "12"])


if __name__ == "__main__":
    unittest.main()
