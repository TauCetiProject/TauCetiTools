"""Actionable reports and private GitHub-state enrollment requests."""

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
