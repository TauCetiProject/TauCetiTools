"""Finalizer retries report publication outcomes without replaying obsolete verdicts."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts"))
import finalize_batch as finalizer


ENV = dict(STATUS_SHA="a" * 40, GITHUB_RUN_ID="12", GITHUB_RUN_ATTEMPT="2",
           BUILD_JOB_RESULT="failure", BUILD_STATE="failure", BUILD_DESCRIPTION="audit failed",
           PUBLICATION_REQUIRED="false", BORS_STAGING="false")


class Recovery(unittest.TestCase):
    def intent(self, state="failure"):
        return finalizer.status_intent(ENV, dict(state=state, context="build", description="audit result",
            target_url="https://github.com/TauCetiProject/TauCeti/actions/runs/12"))

    def test_original_failed_batch_returns_failure_but_removes_published_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "intent.json"
            with patch.dict(os.environ, dict(ENV, TAUCETI_STATUS_INTENT=str(target)), clear=True), \
                 patch.object(finalizer, "api", side_effect=[dict(run_attempt=2), [], {}]) as request, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(finalizer.main(), 1)
            self.assertFalse(target.exists())
            self.assertEqual(request.call_args.args[1]["state"], "failure")

    def test_resumed_failure_publication_is_successful_recovery_and_forgets_intent(self):
        for pending_evidence in (False, True):
            with self.subTest(pending_evidence=pending_evidence), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "intent.json"
                path.write_text(json.dumps(finalizer.finalization_intent(ENV) if pending_evidence else self.intent()))
                with patch.object(finalizer, "api", side_effect=[dict(run_attempt=2), [], {}]) as request, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(finalizer.main(["--resume", str(path)]), 0)
                self.assertFalse(path.exists())
                self.assertEqual(request.call_args.args[1]["state"], "failure")

    def test_newer_build_status_blocks_both_stale_success_and_failure(self):
        for state in ("success", "failure"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "intent.json"
                path.write_text(json.dumps(self.intent(state)))
                status = dict(context="build", state="success",
                              target_url="https://github.com/TauCetiProject/TauCeti/actions/runs/13", created_at="2026-10-21T01:00:00Z")
                with patch.object(finalizer, "api", side_effect=[dict(run_attempt=2, run_started_at="2026-10-21T00:00:00Z"), [status]]) as request, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(finalizer.main(["--resume", str(path)]), 0)
                self.assertFalse(path.exists())
                self.assertEqual(request.call_count, 2)
                self.assertIn("/commits/" + "a" * 40 + "/statuses?per_page=100", request.call_args.args[0])

    def test_status_fence_requires_exact_repository_run_url_and_context(self):
        for context, url in [("scope", "https://github.com/TauCetiProject/TauCeti/actions/runs/13"),
                             ("build", "https://github.com/other/repo/actions/runs/13"),
                             ("build", "https://github.com/TauCetiProject/TauCeti/actions/runs/13/evil"),
                             ("build", "https://github.com/TauCetiProject/TauCeti/actions/runs/11")]:
            with self.subTest(context=context, url=url), patch.object(finalizer, "api", return_value=[dict(context=context, target_url=url)]):
                self.assertFalse(finalizer.newer_build_status(self.intent()))

    def test_only_newest_build_status_is_authoritative(self):
        newer_history = dict(context="build", target_url="https://github.com/TauCetiProject/TauCeti/actions/runs/13", created_at="2026-10-21T01:00:00Z")
        for current in [dict(context="build", target_url="https://github.com/TauCetiProject/TauCeti/actions/runs/12"),
                        dict(context="build", target_url="https://github.com/TauCetiProject/TauCeti/actions/runs/11"),
                        dict(context="build", target_url="https://other.example/current-verdict")]:
            with self.subTest(current=current), patch.object(finalizer, "api", return_value=[current, newer_history]):
                self.assertFalse(finalizer.newer_build_status(self.intent()))
        with patch.object(finalizer, "api", return_value=[dict(context="scope"), newer_history]):
            self.assertTrue(finalizer.newer_build_status(self.intent(), "2026-10-21T00:00:00Z"))

    def test_rerun_can_replace_a_higher_run_id_status_created_before_this_attempt(self):
        intent = self.intent()
        newer = dict(context="build", target_url="https://github.com/TauCetiProject/TauCeti/actions/runs/13",
                     created_at="2026-10-21T01:00:00Z")
        with patch.object(finalizer, "api", side_effect=[dict(run_attempt=2, run_started_at="2026-10-21T02:00:00Z"), [newer], {}]) as request, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(finalizer.publish(intent, resume=True), 0)
        self.assertEqual(request.call_args.args[0], "/statuses/" + "a" * 40)
        with patch.object(finalizer, "api", return_value=[newer]):
            self.assertTrue(finalizer.newer_build_status(intent, "2026-10-21T00:00:00Z"))
            self.assertFalse(finalizer.newer_build_status(intent, "2026-10-21T01:00:00Z"))

    def test_status_history_uses_the_renewable_status_credential(self):
        from unittest.mock import Mock
        client = Mock()
        with patch.object(finalizer, "_client", client):
            finalizer.api("/commits/" + "a" * 40 + "/statuses?per_page=100")
        self.assertEqual(client.request.call_args.kwargs["read"], False)

    def test_newer_status_appearing_during_quota_wait_is_rechecked(self):
        now = [0]
        def sleep(delay): now[0] += delay
        newer = dict(context="build", target_url="https://github.com/TauCetiProject/TauCeti/actions/runs/13", created_at="2026-10-21T01:00:00Z")
        deferred = finalizer.Deferred("ci-status-app", 1060)
        with patch.object(finalizer, "api", side_effect=[dict(run_attempt=2), [], deferred, dict(run_attempt=2, run_started_at="2026-10-21T00:00:00Z"), [newer]]) as request, \
             patch.object(finalizer.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(finalizer.time, "time", side_effect=lambda: 1000 + now[0]), \
             patch.object(finalizer.time, "sleep", side_effect=sleep), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(finalizer.publish(self.intent(), resume=True), 0)
        self.assertEqual(sum(call.args[0].startswith("/statuses/") for call in request.call_args_list), 1)
        self.assertEqual(sum(call.args[0].startswith("/commits/") for call in request.call_args_list), 2)

    def test_token_renewal_failure_retries_boundedly_without_losing_intent(self):
        now = [0]
        def sleep(delay): now[0] += delay
        def unavailable(path, payload=None):
            if path.startswith("/actions/"): return dict(run_attempt=2)
            if path.startswith("/commits/"): return []
            raise RuntimeError("CI status App renewal unavailable")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intent.json"
            path.write_text(json.dumps(self.intent()))
            with patch.object(finalizer, "api", side_effect=unavailable) as request, \
                 patch.object(finalizer, "post_retry_seconds", return_value=25), \
                 patch.object(finalizer.time, "monotonic", side_effect=lambda: now[0]), \
                 patch.object(finalizer.time, "sleep", side_effect=sleep), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(finalizer.main(["--resume", str(path)]), 1)
            self.assertEqual(now[0], 25)
            self.assertTrue(path.exists())
            self.assertEqual(sum(call.args[0].startswith("/statuses/") for call in request.call_args_list), 3)


if __name__ == "__main__":
    unittest.main()
