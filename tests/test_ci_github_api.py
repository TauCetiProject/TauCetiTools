"""Hermetic quota/retry/publication regressions; no GitHub requests."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts"
sys.path.insert(0, str(SCRIPTS))
import github_api as api
import finalize_batch as finalizer


def reply(status=200, data=None, headers=None):
    text = f"HTTP/2.0 {status}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    return subprocess.CompletedProcess([], 0 if status < 400 else 1, text + "\r\n" + json.dumps(data), "")


class CIAPI(unittest.TestCase):
    def test_primary_quota_far_reset_makes_one_call_and_marks_trusted_step(self):
        with tempfile.TemporaryDirectory() as directory:
            envfile = Path(directory) / "env"
            with patch.dict(os.environ, GH_TOKEN="secret", GITHUB_ENV=str(envfile)), \
                 patch.object(api.subprocess, "run", return_value=reply(403, {"message": "API rate limit exceeded"},
                    {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "5000"})) as request, \
                 patch.object(api.time, "time", return_value=1000), patch.object(api.time, "sleep") as sleep, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(api.Deferred):
                    api.GitHub(max_wait=180).request("GET", "repos/o/r/actions/runs/1")
            request.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(envfile.read_text(), "TAUCETI_CI_QUOTA_UNAVAILABLE=1\n")

    def test_invalid_reset_never_licenses_fast_retry(self):
        for reset in ["broken", "nan", "inf"]:
            with self.subTest(reset=reset), patch.object(api.subprocess, "run", return_value=reply(403,
                    {"message": "API rate limit exceeded"}, {"X-RateLimit-Reset": reset})) as request, \
                 patch.object(api.time, "sleep") as sleep, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(api.Deferred):
                    api.GitHub(max_wait=10).request("GET", "repos/o/r/pulls/1")
                request.assert_called_once()
                sleep.assert_not_called()

    def test_revalidation_reuses_only_server_confirmed_etag_payload(self):
        with patch.object(api.subprocess, "run", side_effect=[reply(data={"run_attempt": 2}, headers={"ETag": '"abc"'}),
                 reply(304)]) as request, contextlib.redirect_stderr(io.StringIO()):
            client = api.GitHub()
            self.assertEqual(client.request("GET", "repos/o/r/actions/runs/1", conditional=True), {"run_attempt": 2})
            self.assertEqual(client.request("GET", "repos/o/r/actions/runs/1", conditional=True), {"run_attempt": 2})
            self.assertIn('If-None-Match: "abc"', request.call_args.args[0])
            self.assertEqual(request.call_count, 2)

    def test_cli_does_not_apply_jq_to_failed_responses(self):
        with patch.object(api.subprocess, "run", return_value=reply(404, {"message": "not found"})) as request, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(api.main(["repos/o/r/pulls/1", "--jq", ".head.sha"]), 1)
        request.assert_called_once()

    def test_cli_paginates_object_pages_without_losing_authority(self):
        responses = [reply(data={"runners": [1]}, headers={"Link": '<https://api.github.com/orgs/o/actions/runners?page=2>; rel="next"'}),
                     reply(data={"runners": [2]})]
        output = io.StringIO()
        with patch.object(api.subprocess, "run", side_effect=responses), contextlib.redirect_stdout(output), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(api.main(["orgs/o/actions/runners", "--paginate", "--slurp"]), 0)
        self.assertEqual(json.loads(output.getvalue()), [{"runners": [1]}, {"runners": [2]}])

    def test_telemetry_hides_query_values_and_secret_error_text(self):
        output = io.StringIO()
        with patch.dict(os.environ, GH_TOKEN="super-secret", GH_READ_TOKEN="read-secret"), \
             patch.object(api.subprocess, "run", return_value=reply(422, {"message": "super-secret"})), \
             contextlib.redirect_stderr(output):
            with self.assertRaises(api.APIError) as caught:
                api.GitHub().request("GET", "repos/o/r/items?access_token=super-secret")
        self.assertNotIn("super-secret", output.getvalue() + str(caught.exception))

    def test_quota_during_publisher_observation_retains_resumable_build_evidence(self):
        env = dict(STATUS_SHA="a" * 40, GITHUB_RUN_ID="12", GITHUB_RUN_ATTEMPT="2",
                   BUILD_JOB_RESULT="success", BUILD_STATE="success", BUILD_DESCRIPTION="audited",
                   PUBLICATION_REQUIRED="true", BORS_STAGING="false")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intent.json"
            with patch.dict(os.environ, dict(env, TAUCETI_STATUS_INTENT=str(path)), clear=True), \
                 patch.object(finalizer, "terminal_state", side_effect=api.Deferred("ci-read", api.time.time() + 4000)), \
                 patch.object(finalizer, "publish") as publish, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(finalizer.main(), 75)
            publish.assert_not_called()
            intent = json.loads(path.read_text())
            self.assertEqual(intent["schema"], "tauceti-ci.finalization-intent/v1")
            self.assertEqual(intent["evidence"]["BUILD_STATE"], "success")
            with patch.dict(os.environ, CI_STATUS_SOURCE_RUN_ID="13"), \
                 patch.object(finalizer, "terminal_state") as evidence, self.assertRaises(ValueError):
                finalizer.main(["--resume", str(path)])
            evidence.assert_not_called()
            with patch.dict(os.environ, CI_STATUS_SOURCE_RUN_ID="12", CI_STATUS_SOURCE_ATTEMPT="2"), \
                 patch.object(finalizer, "terminal_state", return_value=("success", "publisher confirmed")) as evidence, \
                 patch.object(finalizer, "api", side_effect=[dict(run_attempt=2), [], {}]), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(finalizer.main(["--resume", str(path)]), 0)
            self.assertEqual(evidence.call_args.args[0]["STATUS_SHA"], "a" * 40)
            self.assertFalse(path.exists())

    def test_finalization_intents_accept_only_bounded_known_evidence(self):
        env = dict(STATUS_SHA="a" * 40, GITHUB_RUN_ID="12", GITHUB_RUN_ATTEMPT="2",
                   BUILD_JOB_RESULT="success", BUILD_STATE="success")
        original = finalizer.finalization_intent(env)
        finalizer.validate_intent(original)
        for key, value in [("BUILD_JOB_RESULT", "arbitrary"), ("BUILD_STATE", "green"),
                           ("PUBLICATION_REQUIRED", "maybe"), ("EXTRA", "value"),
                           ("BUILD_DESCRIPTION", "x" * 141), ("BORS_STAGING", "true")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                finalizer.validate_intent(dict(original, evidence=dict(original["evidence"], **{key: value})))

    def test_required_status_retries_without_rebuilding(self):
        env = dict(STATUS_SHA="a" * 40, GITHUB_RUN_ID="12", GITHUB_RUN_ATTEMPT="2")
        payload = dict(state="success", context="build", description="ok",
                       target_url="https://github.com/TauCetiProject/TauCeti/actions/runs/12")
        intent = finalizer.status_intent(env, payload)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intent.json"
            path.write_text(json.dumps(intent))
            with patch.object(finalizer, "terminal_state", side_effect=AssertionError("must not rebuild")), \
                 patch.object(finalizer, "api", side_effect=[{"run_attempt": 2}, [], {}]) as request, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(finalizer.main(["--resume", str(path)]), 0)
            self.assertEqual(request.call_count, 3)


if __name__ == "__main__":
    unittest.main()
