"""GitHub CI routing in a mixed-source project, including the native process boundary."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.forge import ForgeError, Unsupported
from tauceti_tools.forge_cli import run
from tauceti_tools.forge_command import parse_command
from tauceti_tools.project import ConfigError

CONFIG = '''schema_version = 2
[forges.source]
kind = "forgejo"
url = "https://git.example.org"
[repositories]
content = {forge="source", name="one/Project"}
roadmap = {forge="source", name="one/Roadmap"}
ci_archive = "other/Builds"
[ci]
backend = "github-actions"
repository = "one/Project"
'''


class CIRouting(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "project.toml"
        self.config.write_text(CONFIG)
        self.env = {"TAUCETI_FORGEJO_TOKEN_FILE": "/must-not-open"}

    def invoke(self, argv, env=None):
        with (
            patch("tauceti_tools.forge_cli._delegate", return_value=17) as delegate,
            patch("tauceti_tools.forge_cli.Forgejo") as forgejo,
            patch("tauceti_tools.forge_cli._checkout_repository") as checkout,
        ):
            result = run(["--conf", str(self.config), *argv], environment=self.env if env is None else env)
        self.assertEqual(result, 17)
        forgejo.assert_not_called()
        checkout.assert_not_called()
        self.assertEqual(delegate.call_count, 1)
        self.assertEqual(delegate.call_args.args[0], argv)
        return delegate.call_args.args[1]

    def refuse(self, argv, env=None, error=Unsupported, message=""):
        with (
            patch("tauceti_tools.forge_cli._delegate") as delegate,
            patch("tauceti_tools.forge_cli.Forgejo") as forgejo,
            self.assertRaisesRegex(error, message),
        ):
            run(["--conf", str(self.config), *argv], environment=self.env if env is None else env)
        delegate.assert_not_called()
        forgejo.assert_not_called()

    def test_native_ci_command_contracts(self):
        commands = [
            ["run", "list", "-a", "-b", "main", "-c", "a" * 40, "--created", ">2026-01-01", "-e", "push",
             "-L", "10", "-s", "queued", "-u", "operator", "-w", "build.yml", "--json", "databaseId,headSha", "-q", ".[]"],
            ["run", "view", "123", "-a", "2", "-j", "456", "--exit-status", "--log-failed", "-v", "--web=false"],
            ["run", "view", "--job", "456", "--log"],
            ["run", "watch", "123", "--exit-status", "--interval", "120", "--compact"],
            ["run", "rerun", "123", "--failed", "--debug"],
            ["run", "rerun", "123", "--job", "456"],
            ["run", "cancel", "123", "--force"],
            ["run", "delete", "123"],
            ["run", "download", "123", "-D", "out", "-n", "first", "-n", "second", "-p", "log*"],
            ["workflow", "list", "-a", "-L", "20", "--json", "name,path", "-t", "{{.}}"],
            ["workflow", "view", "build.yml", "-r", "main", "-y", "-w=false"],
            ["workflow", "run", "build.yml", "-r", "main", "-F", "request=@request.json", "-f", "mode=check"],
            ["workflow", "run", "build.yml", "--json"],
            ["workflow", "enable", "build.yml"],
            ["workflow", "disable", "build.yml"],
        ]
        for argv in commands:
            with self.subTest(argv=argv):
                env = self.invoke(argv)
                self.assertEqual(env["GH_HOST"], "github.com")
                self.assertEqual(env["GH_REPO"], "github.com/one/Project")
                self.assertEqual(self.env, {"TAUCETI_FORGEJO_TOKEN_FILE": "/must-not-open"})

    def test_source_backed_command_cases(self):
        cases = json.loads((Path(__file__).parent / "fixtures" / "forge_ci_commands.json").read_text())
        for case in cases["cases"]:
            with self.subTest(case=case["id"]):
                # A bare source name may identify two hosts; qualification is a
                # required caller adaptation, not an automatic mirror rewrite.
                self.refuse(case["source_argv"], message=case["source_refusal"])
                if case["qualified_result"] == "refused-unconfigured-ci-target":
                    self.refuse(case["qualified_argv"], message="configured GitHub repository")
                else:
                    env = self.invoke(case["qualified_argv"])
                    self.assertEqual(env["GH_REPO"], "github.com/one/Project")

    def test_string_values_never_select_a_repository(self):
        for prefix in [
            ["workflow", "run", "build.yml", "--ref"],
            ["workflow", "run", "build.yml", "-f"],
            ["workflow", "run", "build.yml", "-F"],
            ["run", "list", "-b"],
            ["run", "list", "--json"],
            ["run", "view", "123", "--template"],
            ["run", "download", "123", "--dir"],
            ["run", "download", "123", "-n"],
        ]:
            for value in ["-R", "--repo=git.example.org/attacker/project", "--conf=/other"]:
                with self.subTest(prefix=prefix, value=value):
                    env = self.invoke([*prefix, value])
                    self.assertEqual(env["GH_REPO"], "github.com/one/Project")

    def test_json_flag_arity_depends_on_command(self):
        self.assertTrue(parse_command(["workflow", "run", "build.yml", "--json"]).options["json"])
        cmd = parse_command(["run", "view", "123", "--json", "--repo=literal"])
        self.assertEqual(cmd.options["json"], "--repo=literal")
        self.assertNotIn("repo", cmd.options)
        self.refuse(["workflow", "run", "build.yml", "--json=fields"])

    def test_explicit_repository_forms_and_precedence(self):
        for argv in [
            ["-Rgithub.com/other/Builds", "run", "view", "123"],
            ["run", "--repo=github.com/other/Builds", "view", "123"],
            ["run", "view", "123", "--repo", "https://github.com/other/Builds"],
        ]:
            with self.subTest(argv=argv):
                env = self.invoke(argv, {**self.env, "GH_REPO": "git.example.org/one/Project", "GH_HOST": "git.example.org"})
                self.assertEqual(env["GH_REPO"], "github.com/other/Builds")
                self.assertEqual(env["GH_HOST"], "github.com")

    def test_environment_repository_is_not_silently_mapped_to_ci(self):
        env = self.invoke(["run", "view", "123"], {"GH_REPO": "github.com/other/Builds"})
        self.assertEqual(env["GH_REPO"], "github.com/other/Builds")
        for selected in ["one/Project", "git.example.org/one/Project", "unknown.example/one/Project"]:
            with self.subTest(selected=selected):
                self.refuse(["run", "view", "123"], {**self.env, "GH_REPO": selected})

    def test_forgejo_commands_are_refused_before_credentials_or_native_execution(self):
        for command in [["run", "list"], ["run", "cancel", "123"], ["workflow", "run", "build.yml"]]:
            self.refuse([*command, "-R", "git.example.org/one/Project"])

    def test_missing_ci_config_requires_explicit_github_selection(self):
        self.config.write_text(CONFIG.partition("[ci]")[0])
        self.refuse(["run", "view", "123"], error=ConfigError)
        self.invoke(["run", "view", "123", "-R", "github.com/other/Builds"])

    def test_explicit_github_selectors_require_a_configured_repository(self):
        for selected in ["github.com/one/Roadmap", "github.com/unconfigured/Builds", "https://github.com/one/Roadmap"]:
            with self.subTest(selected=selected):
                self.refuse(["workflow", "run", "build.yml", "-R", selected], message="configured GitHub repository")
        self.invoke(["run", "view", "123", "-R", "github.com/OTHER/builds"])

    def test_unknown_commands_flags_conflicts_and_extra_arguments_fail_closed(self):
        for argv in [
            ["run", "invented"], ["workflow", "invented"], ["run", "view", "123", "--invented"],
            ["run", "list", "123"], ["run", "view", "123", "456"], ["workflow", "run", "a", "b"],
            ["run", "view", "123", "-R", "github.com/one/Project", "-R", "git.example.org/one/Project"],
            ["run", "view", "123", "-R", ""], ["run", "view", "123", "--repo"],
            ["run", "view", "123", "--log=invalid"],
            ["workflow", "--json", "fields", "run", "build.yml"],
            ["--json", "fields", "workflow", "run", "build.yml"],
        ]:
            with self.subTest(argv=argv):
                self.refuse(argv)

    def test_double_dash_payloads_are_not_routing_flags(self):
        env = self.invoke(["workflow", "view", "--", "--repo=git.example.org/one/Project"])
        self.assertEqual(env["GH_REPO"], "github.com/one/Project")

    def test_enterprise_delegation_keeps_origin_guard(self):
        self.config.write_text(
            CONFIG.replace('[repositories]', '[forges.enterprise]\nkind="github"\nurl="https://ghe.example.org"\n[repositories]')
            .replace('repository = "one/Project"', 'repository = {forge="enterprise", name="one/Project"}')
        )
        argv = ["run", "view", "123", "-R", "ghe.example.org/one/Project"]
        env = {**self.env, "GH_ENTERPRISE_TOKEN": "test-only-sentinel"}
        self.refuse(argv, env, error=ForgeError, message="TAUCETI_GITHUB_ORIGIN")
        self.refuse(argv, {**env, "TAUCETI_GITHUB_ORIGIN": "https://github.com"}, error=ForgeError, message="TAUCETI_GITHUB_ORIGIN")
        delegated = self.invoke(argv, {**env, "TAUCETI_GITHUB_ORIGIN": "https://ghe.example.org"})
        self.assertEqual(delegated["GH_HOST"], "ghe.example.org")

    def test_all_github_projects_retain_unrestricted_native_commands(self):
        self.config.write_text(CONFIG.replace('kind = "forgejo"', 'kind = "github"').replace('https://git.example.org', 'https://github.com'))
        argv = ["run", "future-command", "--future-flag"]
        with patch("tauceti_tools.forge_cli._delegate", return_value=0) as delegate:
            self.assertEqual(run(["--conf", str(self.config), *argv], environment={}), 0)
        delegate.assert_called_once_with(argv, {})

    def test_real_subprocess_preserves_stdin_argv_output_and_exit_status(self):
        native = Path(self.temp.name) / "native-gh"
        native.write_text(f'''#!{sys.executable}
import json, os, sys
print(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "host": os.environ["GH_HOST"], "repo": os.environ["GH_REPO"]}}))
print("native diagnostic", file=sys.stderr)
sys.exit(7)
''')
        native.chmod(0o700)
        argv = ["workflow", "run", "build.yml", "--json", "-Rgithub.com/one/Project"]
        result = subprocess.run(
            [sys.executable, "-m", "tauceti_tools.forge_cli", "--conf", str(self.config), *argv],
            env={"PATH": os.defpath, **self.env, "TAUCETI_REAL_GH": str(native)},
            cwd=Path(__file__).resolve().parents[1],
            input='{"request":"--repo=git.example.org/attacker/project"}\n', text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stderr, "native diagnostic\n")
        self.assertEqual(json.loads(result.stdout), {
            "argv": argv, "stdin": '{"request":"--repo=git.example.org/attacker/project"}\n',
            "host": "github.com", "repo": "github.com/one/Project",
        })

    def test_native_signal_and_interrupt_have_shell_exit_statuses(self):
        from tauceti_tools.forge_cli import _delegate

        with patch("subprocess.run") as child:
            child.return_value.returncode = -15
            self.assertEqual(_delegate(["run", "watch", "123"], {"TAUCETI_REAL_GH": "/native-gh"}), 143)
            child.side_effect = KeyboardInterrupt
            self.assertEqual(_delegate(["run", "watch", "123"], {"TAUCETI_REAL_GH": "/native-gh"}), 130)

    @unittest.skipIf(os.name != "posix", "POSIX signal statuses")
    def test_real_native_termination_and_parent_interrupt(self):
        native = Path(self.temp.name) / "signalling-gh"
        native.write_text(f'''#!{sys.executable}
import os, signal, time
if os.environ["TEST_NATIVE_SIGNAL"] == "TERM":
    os.kill(os.getpid(), signal.SIGTERM)
else:
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(10)
''')
        native.chmod(0o700)
        for mode, status in [("TERM", 143), ("INT", 130)]:
            with self.subTest(mode=mode):
                result = subprocess.run(
                    [sys.executable, "-m", "tauceti_tools.forge_cli", "--conf", str(self.config), "run", "watch", "123"],
                    env={"PATH": os.defpath, "TAUCETI_REAL_GH": str(native), "TEST_NATIVE_SIGNAL": mode},
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
