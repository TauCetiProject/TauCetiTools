"""Credential checks follow the step that ran, rather than a run's original age."""

import base64
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import test_doctor_setup as setup
from tauceti_tools.cli import main
from tauceti_tools.doctor import render


class CredentialChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = setup.DoctorSetupTests()
        self.fixture.setUp()
        self.responses = self.fixture.responses
        self.fixture.publish_package("one/Project")
        for workflow in ("pr-build.yml", "auto-merge.yml"):
            self.responses[f"repos/one/Project/contents/.github/workflows/{workflow}?ref=main"] = {
                "encoding": "base64", "content": base64.b64encode(b"on:\n  merge_group:\n    types: [checks_requested]\n").decode()}
        self.responses["repos/one/Project/actions/secrets"] = {"secrets": [
            {"name": name, "updated_at": "2026-10-22T10:00:00Z"} for name in ("APP_ID", "APP_PRIVATE_KEY")]}
        self.responses["repos/one/Project/actions/organization-secrets"] = {"secrets": []}
        self.run = {"id": 77, "run_attempt": 2, "conclusion": "success",
                    "created_at": "2026-10-21T10:00:00Z", "run_started_at": "2026-10-22T11:00:00Z"}
        self.responses["repos/one/Project/actions/workflows/auto-merge.yml/runs?per_page=10"] = {"workflow_runs": [self.run]}
        self.step = {"name": "Mint merge token", "conclusion": "success", "started_at": "2026-10-22T11:01:00Z"}
        self.responses["repos/one/Project/actions/runs/77/attempts/2/jobs"] = {"jobs": [{"steps": [self.step]}]}

    def finding(self):
        return self.fixture.findings()["evidence.auto-merge"]

    def test_successful_rerun_after_secret_update_is_ready(self):
        finding = self.finding()
        self.assertEqual(finding.status, "ready", finding.summary)
        self.assertIn("attempt 2", finding.summary)
        # The older attempt must never supply the verdict.
        self.responses["repos/one/Project/actions/runs/77/jobs"] = {"jobs": [{"steps": [dict(self.step, conclusion="failure")]}]}
        self.assertEqual(self.finding().status, "ready")

    def test_newer_unrelated_run_does_not_hide_a_successful_rerun(self):
        for conclusion in ("skipped", "success"):
            with self.subTest(conclusion=conclusion):
                newer = {"id": 78, "run_attempt": 1, "conclusion": conclusion,
                         "created_at": "2026-10-22T12:00:00Z", "run_started_at": "2026-10-22T12:00:00Z"}
                self.responses["repos/one/Project/actions/workflows/auto-merge.yml/runs?per_page=10"] = {
                    "workflow_runs": [newer, self.run]}
                self.responses["repos/one/Project/actions/runs/78/attempts/1/jobs"] = {"jobs": [{"steps": []}]}
                finding = self.finding()
                self.assertEqual(finding.status, "ready", finding.summary)
                self.assertIn("Run 77, attempt 2", finding.summary)

    def test_offsets_and_fractional_seconds_are_compared_as_times(self):
        self.step["started_at"] = "2026-10-22T03:01:00.123-07:00"
        self.assertEqual(self.finding().status, "ready")
        self.step.update(conclusion="failure", started_at="2026-10-22T02:59:00.123-07:00")
        self.assertEqual(self.finding().status, "pending")
        self.assertIn("before the latest", self.finding().summary)

    def test_unreadable_settings_are_not_reported_missing(self):
        self.responses["repos/one/Project/actions/secrets"] = 403
        self.step["conclusion"] = "skipped"
        finding = self.finding()
        self.assertEqual(finding.status, "pending")
        self.assertIn("Cannot read App credential settings", finding.summary)
        self.assertNotIn("settings are missing", finding.summary)

    def test_visible_repository_secrets_do_not_require_organization_secret_access(self):
        self.responses["repos/one/Project/actions/organization-secrets"] = 403
        self.assertEqual(self.finding().status, "ready")

    def test_unused_organization_secret_rotation_does_not_invalidate_repository_use(self):
        self.responses["repos/one/Project/actions/organization-secrets"] = {"secrets": [
            {"name": name, "updated_at": "2027-01-01T00:00:00Z"} for name in ("APP_ID", "APP_PRIVATE_KEY")]}
        self.assertEqual(self.finding().status, "ready")

    def test_build_failure_does_not_hide_branch_rule_instructions(self):
        self.responses["repos/one/Project/actions/workflows/pr-build.yml/runs?per_page=1"] = {
            "workflow_runs": [{"id": 88, "run_attempt": 1, "conclusion": "failure"}]}
        self.responses["repos/one/Project/actions/runs/88/attempts/1/jobs"] = {"jobs": [{"steps": []}]}
        rows = self.fixture.findings()
        self.assertEqual(rows["evidence.pr-build"].status, "failed")
        self.assertIn("Enable Require merge queue", " ".join(rows["rules.content"].steps))

    def test_failed_step_counts_even_if_overall_run_succeeded(self):
        self.step["conclusion"] = "failure"
        self.assertEqual(self.finding().status, "failed")
        self.assertIn("credential use failed", self.finding().summary)

    def test_skipped_or_pre_update_step_stays_unchecked(self):
        self.step["conclusion"] = "skipped"
        self.assertEqual(self.finding().status, "pending")
        self.assertIn("skipped", self.finding().summary)
        self.step.update(conclusion="success", started_at="2026-10-22T09:00:00Z")
        self.assertEqual(self.finding().status, "pending")
        self.assertIn("before the latest", self.finding().summary)

    def test_step_that_has_not_started_stays_unchecked(self):
        self.step.update(conclusion=None, started_at=None)
        self.run.update(status="queued", conclusion=None, run_started_at=None)
        self.assertEqual(self.finding().status, "pending")

    def test_deleted_credentials_are_not_verified_by_an_old_success(self):
        self.responses["repos/one/Project/actions/secrets"] = {"secrets": []}
        self.assertEqual(self.finding().status, "pending")
        self.assertIn("settings are missing", self.finding().summary)

    def test_missing_settings_not_run_and_unreadable_records_have_different_messages(self):
        self.step["conclusion"] = "skipped"
        self.responses["repos/one/Project/actions/secrets"] = {"secrets": []}
        self.assertIn("settings are missing", self.finding().summary)
        self.responses["repos/one/Project/actions/workflows/auto-merge.yml/runs?per_page=10"] = {"workflow_runs": []}
        self.assertIn("No workflow run yet", self.finding().summary)
        self.responses["repos/one/Project/actions/workflows/auto-merge.yml/runs?per_page=10"] = {"workflow_runs": [self.run]}
        self.responses["repos/one/Project/actions/runs/77/attempts/2/jobs"] = 403
        finding = self.finding()
        self.assertEqual(finding.status, "pending")
        self.assertIn("cannot read its jobs", finding.summary)
        self.assertNotIn("private response body", finding.summary)

    def test_plain_text_has_who_next_action_and_verification_without_new_json_fields(self):
        finding = self.finding()
        self.step["conclusion"] = "skipped"
        finding = self.finding()
        output = io.StringIO()
        with redirect_stdout(output):
            render([finding])
        for text in ("Who:", "Check again:", "no need to create a PR", "App-token step"):
            self.assertIn(text, output.getvalue().replace("\n", " "))
        self.assertEqual(set(finding.json()), {
            "id", "status", "summary", "expected", "role", "commands", "urls", "steps",
            "verify", "section", "repair", "optional", "manual"})
        with patch("tauceti_tools.doctor.diagnose", return_value=[finding]), \
                patch("tauceti_tools.cli.load", return_value=self.fixture.project), \
                patch("tauceti_tools.cli.bootstrap"), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["doctor", "--conf", "/tmp/tauceti.toml", "--check", finding.id]), 0)

    def test_selected_check_gets_the_same_prerequisite_message(self):
        finding = self.fixture.findings()["roadmap.workflows"]
        self.assertIn("Finish", " ".join(finding.steps))
        self.assertFalse(finding.repair)
        with patch("tauceti_tools.doctor.diagnose", return_value=list(self.fixture.findings().values())), \
                patch("tauceti_tools.cli.load", return_value=self.fixture.project), \
                patch("tauceti_tools.cli.bootstrap"), redirect_stdout(io.StringIO()) as output:
            main(["doctor", "--conf", "/tmp/tauceti.toml", "--check", finding.id])
        self.assertIn("Finish", output.getvalue())
        self.assertNotIn("--fix roadmap.workflows", output.getvalue())

    def test_new_content_package_waits_for_workflow_helpers(self):
        del self.responses["repos/one/Project/contents/lakefile.toml?ref=main"]
        del self.responses["repos/one/Project/contents/.github/workflows/pr-build.yml?ref=main"]
        rows = self.fixture.findings()
        self.assertIn("Finish Project files and workflows first", " ".join(rows["lean.content"].steps))
        self.assertNotIn("$ lake build", " ".join(rows["lean.content"].steps))


if __name__ == "__main__":
    unittest.main()
