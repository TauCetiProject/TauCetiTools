import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.forge import GITHUB, ForgeError, Incomplete, Unsupported
from tauceti_tools.forge_cli import _checkout_repository as checkout_repository
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

    def test_qualified_github_upstreams_are_normalized_for_legacy_consumers(self):
        project = parse(
            (
                CONFIG.replace('kind = "forgejo"', 'kind = "github"').replace(
                    "https://git.example.org", "https://github.com"
                )
                + '\n[[upstreams]]\nrepo={forge="source",name="one/Dependency"}\npackage="dep"\n'
            ).encode()
        )
        self.assertEqual(project.upstreams[0]["repo"], "one/Dependency")
        self.assertEqual(json.loads(project.environment()["TAUCETI_UPSTREAMS"])[0]["repo"], "one/Dependency")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "project.toml"
        self.config.write_text(CONFIG)
        routing = patch(
            "tauceti_tools.forge_cli._checkout_repository",
            return_value=parse(CONFIG.encode()).repository_ref("content"),
        )
        routing.start()
        self.addCleanup(routing.stop)

    def test_foreign_origin_never_receives_token(self):
        with patch("tauceti_tools.forge_cli.Forgejo") as client:
            for origin in [None, "https://other.example.org", "http://127.0.0.1:1234"]:
                env = {"TAUCETI_FORGEJO_TOKEN_FILE": "/private/token"}
                if origin:
                    env["TAUCETI_FORGEJO_ORIGIN"] = origin
                with self.subTest(origin=origin), self.assertRaises(ForgeError):
                    run(["--conf", str(self.config), "pr", "view", "12"], environment=env)
            client.assert_not_called()

    def test_implicit_github_delegation_preserves_checkout_detection(self):
        self.config.write_text(
            CONFIG.replace('content = {forge="source", name="one/Project"}', 'content = "one/Project"')
        )
        with patch("tauceti_tools.forge_cli._delegate", return_value=0) as delegate:
            run(["--conf", str(self.config), "pr", "view", "12"], environment={})
        self.assertEqual(delegate.call_args.args[1], {})

    def test_attacker_config_cannot_select_a_credential_origin(self):
        self.config.write_text(CONFIG.replace("https://git.example.org", "http://127.0.0.1:2345"))
        with patch("tauceti_tools.forge_cli.Forgejo") as client:
            with self.assertRaises(ForgeError):
                run(
                    ["--conf", str(self.config), "pr", "view", "12", "-R", "127.0.0.1:2345/one/Project"],
                    environment={
                        "TAUCETI_FORGEJO_TOKEN_FILE": "/private/nonexistent-token",
                        "TAUCETI_FORGEJO_ORIGIN": "http://127.0.0.1:2346",
                    },
                )
            client.assert_not_called()

    def test_enterprise_token_requires_trusted_origin(self):
        self.config.write_text(CONFIG.replace('kind = "forgejo"', 'kind = "github"'))
        with patch("tauceti_tools.forge_cli._delegate") as delegated, self.assertRaises(ForgeError):
            run(
                ["--conf", str(self.config), "pr", "view", "12", "-R", "git.example.org/one/Project"],
                environment={"GH_ENTERPRISE_TOKEN": "test-only-sentinel"},
            )
        delegated.assert_not_called()

    def test_mixed_project_requires_checkout_match(self):
        ref = parse(CONFIG.encode()).repository_ref("content")
        with patch("subprocess.run") as git:
            git.return_value.returncode = 0
            git.return_value.stdout = "git@git.example.org:one/Project.git\n"
            self.assertEqual(checkout_repository([ref]), ref)
            git.return_value.stdout = "https://other.example.org/one/Project.git\n"
            with self.assertRaises(Unsupported):
                checkout_repository([ref])

    def test_missing_source_fields_are_unknown(self):
        with patch("tauceti_tools.forge_cli.Forgejo") as client:
            client.return_value.pull.return_value = {"number": 12}
            for field in ["isDraft", "state", "headRefOid", "author"]:
                with self.subTest(field=field), self.assertRaises(Incomplete):
                    run(["--conf", str(self.config), "pr", "view", "12", "--json", field])

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
            run(
                ["--conf", str(self.config), "pr", "view", "12", "-R", "github.com/one/Project"],
                environment={**os.environ, "TAUCETI_REAL_GH": "/usr/bin/real-gh"},
            )
        self.assertEqual(
            command.call_args.args[0], ["/usr/bin/real-gh", "pr", "view", "12", "-R", "github.com/one/Project"]
        )

    def test_native_payloads_that_look_like_flags_are_preserved(self):
        for payload in ["--conf=private-text", "--repo=literal-text", "-R", "--json"]:
            with self.subTest(payload=payload), patch("subprocess.run") as command:
                run(
                    [
                        "--conf",
                        str(self.config),
                        "pr",
                        "comment",
                        "12",
                        "--body",
                        payload,
                        "-R",
                        "github.com/one/Project",
                    ],
                    environment={**os.environ, "TAUCETI_REAL_GH": "/usr/bin/real-gh"},
                )
                self.assertEqual(
                    command.call_args.args[0],
                    ["/usr/bin/real-gh", "pr", "comment", "12", "--body", payload, "-R", "github.com/one/Project"],
                )

    def test_native_short_body_option_preserves_flag_text(self):
        with patch("subprocess.run") as command:
            run(
                [
                    "--conf",
                    str(self.config),
                    "pr",
                    "comment",
                    "12",
                    "-b",
                    "--conf=literal",
                    "-R",
                    "github.com/one/Project",
                ],
                environment={**os.environ, "TAUCETI_REAL_GH": "/usr/bin/real-gh"},
            )
        self.assertIn("--conf=literal", command.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
