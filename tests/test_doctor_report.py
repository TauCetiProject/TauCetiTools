"""Actionable reports and private GitHub-state enrollment requests."""

import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tauceti_tools.cli import main
from tauceti_tools.doctor import Finding, extra_findings, render
from tauceti_tools.github_state_setup import _NoRedirect, run
from tauceti_tools.project import parse
from tauceti_tools.scaffold import initial_configuration


class DoctorReportTests(unittest.TestCase):
    def test_dependency_overrides_are_distinct_from_invalid_update_policy(self):
        from tauceti_tools.doctor import dependency_findings

        project = parse(initial_configuration("one/Project", "human").encode(), "/tmp/tauceti.toml")
        project.raw["upstreams"] = [{"package": "cslib", "repo": "leanprover/cslib", "update": True}]
        cslib = {"name": "cslib", "type": "git", "url": "https://github.com/leanprover/cslib",
                 "rev": "a" * 40, "inputRev": "main", "inherited": False}
        mathlib = {"name": "mathlib", "type": "git", "url": "https://github.com/leanprover-community/mathlib4",
                   "rev": "b" * 40, "inherited": True}

        class GitHub:
            def read(self, path):
                if "lean-toolchain" in path:
                    value = "leanprover/lean4:v4.36.0-rc1"
                elif "repos/one/Project/" in path:
                    value = json.dumps({"packages": [cslib, mathlib]})
                else:
                    value = json.dumps({"packages": [dict(mathlib, rev="c" * 40)]})
                return {"content": base64.b64encode(value.encode()).decode()}, ""

        finding, = dependency_findings(project, GitHub(), "doctor")
        self.assertEqual(finding.status, "pending")
        self.assertIn("c" * 40, finding.summary)
        self.assertIn("b" * 40, finding.summary)
        self.assertIn("do not prove", "\n".join(finding.steps))
        project.raw["upstreams"][0]["branch"] = "other"
        finding, = dependency_findings(project, GitHub(), "doctor")
        self.assertEqual(finding.status, "failed")
        self.assertIn("inputRev = 'main'", finding.summary)
        project.raw["upstreams"][0]["branch"] = "main"
        project.raw["upstreams"].append({"package": "mathlib", "repo": "leanprover-community/mathlib4", "update": True})
        finding, = dependency_findings(project, GitHub(), "doctor")
        self.assertEqual(finding.status, "failed")
        self.assertIn("mathlib has update = true but is inherited", finding.summary)

    def test_access_precedes_version_selection_and_private_projects_are_explicit(self):
        from tauceti_tools.doctor import diagnose

        class GitHub:
            def read(self, path, **kwargs):
                if path == "repos/one/Project":
                    return {"private": True, "default_branch": "main", "owner": {"type": "Organization"}}, ""
                return None, "GitHub HTTP 404; inaccessible does not establish absence"

        project = parse(initial_configuration("one/Project", "human").encode(), "/tmp/tauceti.toml")
        findings = diagnose(project, github=GitHub())
        self.assertEqual(next(f for f in findings if f.id == "repositories.content.visibility").status, "failed")
        output = io.StringIO()
        with redirect_stdout(output):
            render(findings)
        self.assertLess(output.getvalue().index("Roadmap repository"), output.getvalue().index("Tools version"))

    def test_first_run_from_missing_empty_and_invalid_configuration(self):
        from tauceti_tools.project import ConfigError, load

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "tauceti.toml"
            for contents in (None, b"", b"# settings will go here\n", b"schema_version = 7\n", b"[broken"):
                if contents is not None:
                    config.write_bytes(contents)
                with self.assertRaises(ConfigError) as caught:
                    load(str(config))
                report = str(caught.exception)
                self.assertIn("tauceti-tools init --repo", report)
                self.assertIn("gh api user --jq .login", report)
                self.assertIn("tauceti-tools doctor --conf", report)
                self.assertIn(str(config), report)
                if contents is not None:
                    self.assertEqual(config.read_bytes(), contents)
            config.write_bytes(b"")
            example = config.with_name("tauceti.toml.example")
            example.write_text("preserve this reference")
            with patch.dict(os.environ, {"TAUCETI_CONF_SNAPSHOT": "/private/snapshot.toml",
                                         "TAUCETI_CONF_SOURCE": str(config)}):
                with self.assertRaises(ConfigError) as caught:
                    parse(b"schema_version=99", "/private/snapshot.toml")
            self.assertIn(str(config), str(caught.exception))
            self.assertNotIn("/private/snapshot.toml", str(caught.exception))
            self.assertIn("tauceti.toml.example.2", str(caught.exception))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["init", "--repo", "one/Epsilon-Eridani", "--owner-login", "human",
                                       "--output", str(config)]), 0)
            project = load(str(config))
            self.assertEqual(project.roots, ["EpsilonEridani"])
            self.assertEqual(project.raw["merge"]["backend"], "github-direct")
            original = config.read_bytes()
            with redirect_stdout(io.StringIO()), patch("sys.stderr", new_callable=io.StringIO) as errors:
                self.assertEqual(main(["init", "--repo", "one/Project", "--owner-login", "human",
                                       "--output", str(config)]), 2)
            self.assertIn("--output", errors.getvalue())
            self.assertEqual(config.read_bytes(), original)

    def test_first_diagnosis_and_optional_credentials_have_specific_next_steps(self):
        from tauceti_tools.doctor import diagnose

        class GitHub:
            def read(self, path, **kwargs):
                if path in ("repos/one/Project", "repos/one/ProjectRoadmap"):
                    return {"owner": {"type": "Organization"}, "default_branch": "main"}, ""
                return None, "GitHub HTTP 404; inaccessible does not establish absence"

        project = parse(initial_configuration("one/Project", "human").encode(), "/tmp/tauceti.toml")
        project.raw["features"].update(docs=True, progress=True, claims=True, ci_archive=True,
                                       archives=True, zulip=True, fleet=True, security_audit=True)
        for name in ("github_state", "bors", "cache"):
            project.raw["services"][name]["mode"] = "hosted"
        rows = {f.id: f for f in diagnose(project, github=GitHub())}
        self.assertIn('[tools.tools]', "\n".join(rows["pins.tools"].steps))
        rules = "\n".join(rows["rules.content"].steps)
        self.assertIn("Clear Require branches to be up to date", rules)
        for name, expected in (("LAKE_CACHE_KEY", "Object Read & Write"), ("ZULIP_EMAIL", "email="),
                               ("ZULIP_API_KEY", "key="), ("FLEET_READ_TOKEN", "features.fleet = false")):
            steps = "\n".join(rows["secrets." + name].steps)
            self.assertIn(expected, steps)
            self.assertNotIn("Click Create GitHub App", steps)
        self.assertIn("Generate a private key", "\n".join(rows["secrets.APP_PRIVATE_KEY"].steps))
        self.assertIn("lakefile.toml", "\n".join(rows["lean.roadmap"].steps))
        for finding in rows.values():
            if finding.status not in ("ready", "disabled"):
                self.assertTrue(finding.steps, finding.id)

    def test_missing_github_login_stops_cascading_access_failures(self):
        from tauceti_tools.doctor import diagnose

        project = parse(initial_configuration("one/Project", "human").encode(), "/tmp/tauceti.toml")
        with patch("subprocess.run", side_effect=FileNotFoundError):
            findings = diagnose(project)
        self.assertEqual([f.id for f in findings], ["github.auth"])
        self.assertIn("https://cli.github.com/", findings[0].urls)
        self.assertIn("gh auth login", findings[0].commands[0])
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "tauceti.toml"
            config.write_text(initial_configuration("one/Project", "human"))
            with patch("subprocess.run", side_effect=FileNotFoundError), \
                    patch("sys.stderr", new_callable=io.StringIO) as errors:
                self.assertEqual(main(["doctor", "--conf", str(config), "--check", "rules.content"]), 2)
            self.assertIn("Cannot inspect rules.content", errors.getvalue())
            self.assertIn("gh auth login", errors.getvalue())
            self.assertNotIn("Unknown check", errors.getvalue())

    def test_check_only_filters_and_keeps_operator_procedure(self):
        class GitHub:
            def read(self, path, **kwargs):
                return {"id": 123}, ""

        project = parse(initial_configuration("one/Project", "human").encode(), "/tmp/tauceti.toml")
        project.raw["services"]["github_state"].update(mode="hosted", url="https://state.example.org")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "TAUCETI_GITHUB_STATE_TOKEN_FILE": str(Path(tmp) / "client-token"),
        }):
            findings = extra_findings(project, GitHub(), "doctor")
        for f in findings:
            f.optional = True
        with patch("tauceti_tools.cli.configuration_source", return_value=project.source), \
                patch("tauceti_tools.cli.load", return_value=project), \
                patch("tauceti_tools.doctor.diagnose", return_value=findings):
            outputs = []
            for args in (["doctor"], ["doctor", "--check", "services.github_state"],
                         ["doctor", "--check", "services.github_state.enrollment"]):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(args), 0)
                outputs.append(output.getvalue())
            for output in outputs:
                self.assertIn("ALLOWED_REPOSITORIES", output)
                self.assertIn("CLIENT_TOKENS", output)
                self.assertIn("github-state bootstrap", output)
                self.assertIn("github-state request", output)
                self.assertNotIn("Operator instructions:", output)
            self.assertNotIn("Disabled:", outputs[1])
            steps = [line for line in outputs[0].splitlines() if line.startswith("  1.")]
            self.assertEqual(len(steps), 1)  # Enrollment and token repair share one procedure.

    def test_human_summary_keeps_json_evidence(self):
        findings = [Finding("pins.tools", "ready", "Commit abc123", "Immutable version"),
                    Finding("actions.enabled", "failed", "Actions disabled", "Actions enabled",
                            steps=["Open Settings → Actions → General; enable Actions and Save."])]
        human = io.StringIO()
        with redirect_stdout(human):
            render(findings)
        self.assertIn("1 checks passed", human.getvalue())
        self.assertIn("enable Actions and Save", human.getvalue())
        self.assertNotIn("Commit abc123", human.getvalue())
        machine = io.StringIO()
        with redirect_stdout(machine):
            render(findings, json_output=True)
        self.assertEqual(json.loads(machine.getvalue()), [f.json() for f in findings])

    def test_request_reuses_private_token_and_shares_only_scoped_hash(self):
        project = parse(initial_configuration("one/Project", "human").encode(), "/private/local/tauceti.toml")
        project.raw["services"]["github_state"]["mode"] = "hosted"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "TAUCETI_GITHUB_STATE_TOKEN_FILE": str(Path(tmp) / "state/client-token"),
        }):
            output = io.StringIO()
            with redirect_stdout(output), patch("subprocess.run") as process:
                run(project, SimpleNamespace(action="request", installation=123))
                token_path = Path(tmp) / "state/client-token"
                token = token_path.read_text().strip()
                run(project, SimpleNamespace(action="request", installation=456))
                process.assert_not_called()
            self.assertEqual(token_path.read_text().strip(), token)
            request = (token_path.parent / "access-request.md").read_text()
            self.assertNotIn(token, request + output.getvalue())
            self.assertNotIn("/private/local", request)
            self.assertIn("https://raw.githubusercontent.com/one/Project/main/tauceti.toml", request)
            self.assertIn("App installation ID: 456", request)
            grant = json.loads((token_path.parent / "client-grant.json").read_text())
            self.assertEqual(grant, {"hash": hashlib.sha256(token.encode()).hexdigest(),
                                     "repositories": ["one/Project"]})
            for path in token_path.parent.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            retained = token_path.parent / "all-grants.json"
            retained.write_text("existing grants")
            with self.assertRaises(FileExistsError):
                run(project, SimpleNamespace(action="initial-grants", token_file=token_path,
                                             repository="one/Project", output=retained))
            self.assertEqual(retained.read_text(), "existing grants")

    def test_bootstrap_uses_operator_endpoint_and_refuses_redirects(self):
        project = parse(initial_configuration("one/Project", "human").encode(), "/tmp/tauceti.toml")
        project.raw["services"]["github_state"]["url"] = "https://downstream-controlled.example.org"
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "admin-token"
            token.write_text("operator-secret")
            with patch("tauceti_tools.cli.configuration_source", return_value=project.source), \
                    patch("tauceti_tools.cli.load", return_value=project), \
                    patch("urllib.request.build_opener") as opener, redirect_stdout(io.StringIO()):
                self.assertEqual(main(["github-state", "bootstrap", "--admin-token-file", str(token)]), 0)
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual(request.full_url, "https://github-state.taucetiproject.org/admin/bootstrap")
                self.assertEqual(json.loads(request.data), {"repository": "one/Project"})
                self.assertIsInstance(opener.call_args.args[0], _NoRedirect)
                self.assertIsNone(opener.call_args.args[0].redirect_request(
                    request, None, 302, "redirect", {}, "https://another.example.org"))


if __name__ == "__main__":
    unittest.main()
