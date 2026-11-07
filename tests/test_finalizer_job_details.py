"""Recover omitted Actions job steps while preserving publisher identity and confirmation."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts"
sys.path.insert(0, str(SCRIPTS))
import finalize_batch as finalizer

ROOT = SCRIPTS.parent


class Clock:
    now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PublisherJobDetailsTests(unittest.TestCase):
    def wait_missing_steps(self, detail):
        fixture = json.loads((ROOT / "scripts/publication_job_details_fixture.json").read_text())
        listing_path = f"/actions/runs/{fixture['run_id']}/attempts/{fixture['attempt']}/jobs?per_page=100"
        detail_path = f"/actions/jobs/{fixture['job_response']['id']}"

        def respond(path):
            if path == listing_path:
                return copy.deepcopy(fixture["jobs_response"])
            self.assertEqual(path, detail_path)
            if isinstance(detail, Exception):
                raise detail
            return copy.deepcopy(detail)

        fetch, clock = Mock(side_effect=respond), Clock()
        with contextlib.redirect_stdout(io.StringIO()):
            result = finalizer.wait_publication(fixture["run_id"], fixture["attempt"],
                                                fetch=fetch, clock=clock, sleep=clock.sleep)
        return result, clock.now, fetch

    def test_real_completed_publisher_recovers_confirmation_from_individual_job(self):
        fixture = json.loads((ROOT / "scripts/publication_job_details_fixture.json").read_text())
        publisher = finalizer.publication_job(fixture["jobs_response"])
        self.assertEqual(publisher["steps"], [])
        self.assertTrue(any(step["name"] == finalizer.CONFIRM and step["conclusion"] == "success"
                            for step in fixture["job_response"]["steps"]))
        result, elapsed, fetch = self.wait_missing_steps(fixture["job_response"])
        self.assertEqual(result[0], "success")
        self.assertEqual(elapsed, 0)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [
            "/actions/runs/40116632015/attempts/1/jobs?per_page=100", "/actions/jobs/120942874104"])

    def test_individual_publisher_rejects_changed_identity_or_isolation(self):
        fixture = json.loads((ROOT / "scripts/publication_job_details_fixture.json").read_text())
        for field, value in [("id", 1), ("id", True), ("run_id", 1), ("run_attempt", 2),
                             ("run_attempt", True), ("name", "other publisher"),
                             ("head_sha", "f" * 40), ("runner_name", "other runner"),
                             ("labels", ["ubuntu-latest"]), ("status", "in_progress"),
                             ("conclusion", "failure"), ("steps", [None])]:
            with self.subTest(field=field, value=value):
                detail = dict(fixture["job_response"], **{field: value})
                result, elapsed, _ = self.wait_missing_steps(detail)
                self.assertEqual(result[0], "failure")
                self.assertEqual(elapsed, finalizer.ACQUISITION_SECONDS)

    def test_missing_or_failed_individual_confirmation_never_becomes_green(self):
        fixture = json.loads((ROOT / "scripts/publication_job_details_fixture.json").read_text())
        for steps in [[], [dict(name=finalizer.CONFIRM, conclusion="failure")],
                      [dict(name=finalizer.CONFIRM, conclusion="skipped")],
                      [dict(name="unrelated step", conclusion="success")]]:
            with self.subTest(steps=steps):
                result, _, _ = self.wait_missing_steps(dict(fixture["job_response"], steps=steps))
                self.assertEqual(result[0], "failure")

    def test_individual_job_transport_error_is_bounded_and_never_green(self):
        result, elapsed, _ = self.wait_missing_steps(OSError("job detail unavailable"))
        self.assertEqual(result[0], "failure")
        self.assertEqual(elapsed, finalizer.ACQUISITION_SECONDS)

    def test_incomplete_listed_identity_cannot_fetch_an_unscoped_job(self):
        fixture = json.loads((ROOT / "scripts/publication_job_details_fixture.json").read_text())
        publisher = finalizer.publication_job(fixture["jobs_response"])
        for field, value in [("id", True), ("head_sha", None), ("run_id", 1),
                             ("run_attempt", 2), ("name", "other publisher")]:
            with self.subTest(field=field, value=value):
                fetch = Mock(side_effect=AssertionError("unscoped detail read"))
                with self.assertRaises(ValueError):
                    finalizer.publication_job_details(dict(publisher, **{field: value}),
                                                     fixture["run_id"], fixture["attempt"], fetch=fetch)
                fetch.assert_not_called()

