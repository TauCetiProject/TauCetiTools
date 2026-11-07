import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.forge import GITHUB, AmbiguousWrite, ForgeError, Incomplete, Unsupported
from tauceti_tools.forge_cli import _checkout_repository as checkout_repository
from tauceti_tools.forge_cli import run
from tauceti_tools.project import ConfigError, load, parse

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
                run(["--conf", str(self.config), "pr", "view", "12", "--json", "inventedField"])

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
        self.config.write_text(
            CONFIG.replace('content = {forge="source", name="one/Project"}', 'content = "one/Project"')
        )
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
        self.config.write_text(
            CONFIG.replace('content = {forge="source", name="one/Project"}', 'content = "one/Project"')
        )
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

    def test_boolean_native_flags_cannot_bypass_mixed_origin_checks(self):
        with patch("tauceti_tools.forge_cli._delegate") as delegated, self.assertRaises(Unsupported):
            run(
                ["--conf", str(self.config), "pr", "create", "-f", "-R", "git.example.org/one/Project"],
                environment={"GH_ENTERPRISE_TOKEN": "test-only-sentinel"},
            )
        delegated.assert_not_called()

    def test_checkout_configuration_is_not_implicitly_trusted(self):
        (Path(self.temp.name) / "tauceti.toml").write_text(CONFIG)
        with (
            patch("tauceti_tools.forge_cli.load") as loader,
            patch("tauceti_tools.forge_cli._delegate", return_value=0) as delegated,
        ):
            run(["pr", "view", "12", "-R", "one/Project"], environment={})
        loader.assert_not_called()
        delegated.assert_called_once_with(["pr", "view", "12", "-R", "one/Project"], {})

    def test_inherited_snapshot_prevents_online_configuration_refetch(self):
        source = "https://configuration.example.invalid/project.toml"
        with (
            patch("tauceti_tools.forge_cli.load", wraps=load) as loader,
            patch("tauceti_tools.forge_cli.Forgejo") as client,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            client.return_value.pull.return_value = {"number": 12}
            run(
                ["pr", "view", "12", "--json", "number"],
                environment={
                    "TAUCETI_CONF": source,
                    "TAUCETI_CONF_SOURCE": source,
                    "TAUCETI_CONF_SNAPSHOT": str(self.config),
                },
            )
        loader.assert_called_once_with(str(self.config))

    def test_list_passes_bounded_request_to_direct_reader(self):
        with patch("tauceti_tools.forge_cli.Forgejo") as client, contextlib.redirect_stdout(io.StringIO()) as output:
            client.return_value.pulls.return_value = [{"number": 3}, {"number": 2}]
            run(["--conf", str(self.config), "pr", "list", "--limit", "2", "--json", "number"])
        self.assertEqual(json.loads(output.getvalue()), [{"number": 3}, {"number": 2}])
        self.assertEqual(client.return_value.pulls.call_args.kwargs["limit"], 2)

    def test_merged_filter_is_forwarded_to_native_reader(self):
        with patch("tauceti_tools.forge_cli.Forgejo") as client, contextlib.redirect_stdout(io.StringIO()) as output:
            client.return_value.pulls.return_value = [
                {"number": 3, "merged": True},
            ]
            run(["--conf", str(self.config), "pr", "list", "-s", "merged", "-L", "1", "--json", "number"])
        self.assertEqual(json.loads(output.getvalue()), [{"number": 3}])
        self.assertEqual(client.return_value.pulls.call_args.kwargs["state"], "merged")

    def test_issue_filters_and_projection(self):
        with patch("tauceti_tools.forge_cli.Forgejo") as client, contextlib.redirect_stdout(io.StringIO()) as output:
            client.return_value.issues.return_value = [{"number": 3, "title": "Proof", "state": "open"}]
            run(
                [
                    "--conf",
                    str(self.config),
                    "issue",
                    "list",
                    "-A",
                    "operator",
                    "-l",
                    "bug,ready",
                    "--json",
                    "number,title,state",
                ]
            )
        self.assertEqual(json.loads(output.getvalue()), [{"number": 3, "title": "Proof", "state": "OPEN"}])
        self.assertEqual(client.return_value.issues.call_args.kwargs["labels"], ["bug", "ready"])
        self.assertEqual(client.return_value.issues.call_args.kwargs["author"], "operator")

    def test_me_author_uses_selected_forge_account(self):
        with patch("tauceti_tools.forge_cli.Forgejo") as client, contextlib.redirect_stdout(io.StringIO()):
            client.return_value.current_user.return_value = {"id": 2, "login": "forgejo-operator"}
            client.return_value.pulls.return_value = []
            run(["--conf", str(self.config), "pr", "list", "-A", "@me", "--json", "number"])
        self.assertEqual(client.return_value.pulls.call_args.kwargs["author"], "forgejo-operator")

    def test_repository_positional_target_resolves_before_native_request(self):
        with patch("tauceti_tools.forge_cli.Forgejo") as client, contextlib.redirect_stdout(io.StringIO()) as output:
            client.return_value.repository.return_value = {"name": "Project", "default_branch": "main"}
            run(
                [
                    "--conf",
                    str(self.config),
                    "repo",
                    "view",
                    "git.example.org/one/Project",
                    "--json",
                    "name,defaultBranchRef",
                ]
            )
        self.assertEqual(json.loads(output.getvalue()), {"name": "Project", "defaultBranchRef": {"name": "main"}})

    def test_unsupported_commands_fail_before_credential_or_delegation(self):
        for command in [["issue", "create", "--editor"], ["pr", "merge", "3"], ["repo", "clone", "one/Project"]]:
            with (
                patch("tauceti_tools.forge_cli._delegate") as delegate,
                patch("tauceti_tools.forge_cli.Forgejo") as client,
            ):
                with self.assertRaises(Unsupported):
                    run(["--conf", str(self.config), *command], environment={"TAUCETI_FORGEJO_TOKEN_FILE": "/missing"})
                delegate.assert_not_called()
                client.assert_not_called()

    def test_supported_mutations_still_require_credential_origin(self):
        with patch("tauceti_tools.forge_cli.Forgejo") as client, self.assertRaises(ForgeError):
            run(
                ["--conf", str(self.config), "issue", "create", "-t", "Title", "-b", "body"],
                environment={"TAUCETI_FORGEJO_TOKEN_FILE": "/missing"},
            )
        client.assert_not_called()

    def test_mixed_github_comment_preserves_flag_like_body_and_native_argv(self):
        argv = ["issue", "comment", "12", "--body", "-R", "--repo", "github.com/one/Project"]
        with patch("tauceti_tools.forge_cli._delegate", return_value=0) as delegate:
            run(["--conf", str(self.config), *argv], environment={})
        self.assertEqual(delegate.call_args.args[0], argv)
        self.assertEqual(delegate.call_args.args[1]["GH_HOST"], "github.com")

    def test_auth_hostname_has_authority_over_repository_environment(self):
        with (
            patch("tauceti_tools.forge_cli.Forgejo") as client,
            patch("tauceti_tools.forge_cli._delegate") as delegate,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            client.return_value.current_user.return_value = {"id": 2, "login": "native-user"}
            run(
                ["--conf", str(self.config), "auth", "status", "--hostname", "git.example.org"],
                environment={"GH_REPO": "github.com/one/Project", "GH_HOST": "github.com"},
            )
        delegate.assert_not_called()
        self.assertEqual(client.call_args.args[0].url, "https://git.example.org")

    def test_ambiguous_write_has_distinct_exit_status_and_stderr_marker(self):
        from tauceti_tools.forge_cli import main

        stderr = io.StringIO()
        with (
            patch("tauceti_tools.forge_cli.run", side_effect=AmbiguousWrite("POST may have applied")),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(main(), 2)
        self.assertTrue(stderr.getvalue().startswith("gh: ambiguous-write:"))

    def test_unconfigured_bare_repository_is_rejected_before_credentials(self):
        with patch("tauceti_tools.forge_cli.Forgejo") as client, self.assertRaises(Unsupported):
            run(["--conf", str(self.config), "issue", "create", "-t", "Title", "-R", "unknown/repo"], environment={})
        client.assert_not_called()

    def test_ci_repository_is_included_in_host_ambiguity(self):
        with patch("tauceti_tools.forge_cli.Forgejo") as client, self.assertRaisesRegex(Unsupported, "ambiguous"):
            run(["--conf", str(self.config), "pr", "view", "12", "-R", "one/Project"], environment={})
        client.assert_not_called()

    def test_jq_option_payload_rejected_before_credentials(self):
        with patch("tauceti_tools.forge_cli.Forgejo") as client, self.assertRaises(Unsupported):
            run(
                ["--conf", str(self.config), "pr", "view", "12", "-R", "git.example.org/one/Project", "--jq", "-n"],
                environment={},
            )
        client.assert_not_called()

    def test_configured_host_allows_qualified_fork_without_extra_config(self):
        with patch("tauceti_tools.forge_cli.Forgejo") as client, contextlib.redirect_stdout(io.StringIO()):
            client.return_value.pull.return_value = {"number": 12, "title": "Fork proof"}
            run(
                [
                    "--conf",
                    str(self.config),
                    "pr",
                    "view",
                    "12",
                    "-R",
                    "git.example.org/contributor/custom-fork",
                    "--json",
                    "title",
                ]
            )
        self.assertEqual(client.return_value.pull.call_args.args[0].name, "contributor/custom-fork")

    def test_invalid_read_arguments_fail_before_routing(self):
        for command in [["pr", "list", "-l", "bug,"], ["pr", "list", "--search", "anything"], ["pr", "view", "1_2"]]:
            with patch("tauceti_tools.forge_cli.Forgejo") as client, self.assertRaises((Unsupported, ValueError)):
                run(["--conf", str(self.config), *command])
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
