"""Finalizer retries report publication outcomes without replaying obsolete verdicts."""
import contextlib
import copy
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


class Withdrawn(unittest.TestCase):
    def setUp(self):
        self.env = dict(ENV, BUILD_JOB_RESULT="success", BUILD_STATE="success",
                        BORS_STAGING="true", BORS_BATCH_ID="1547", BORS_BASE_REF="main",
                        BORS_BASE_SHA="b" * 40)
        # Captured scoped fields from the public Bors endpoint for withdrawn batch 1547.
        self.data = json.loads((Path(__file__).parent / "fixtures/withdrawn-batch1547.json").read_text())

    def state(self, data):
        return finalizer.terminal_state(self.env, observe=lambda *_: data)

    def test_only_an_explicit_correctly_scoped_cancellation_proves_withdrawal(self):
        self.assertEqual(self.state(self.data), ("withdrawn", "batch withdrawn; no verdict posted"))
        for field, value in [("repo", "other/repo"), ("base", "other"), ("schema", "unknown"),
                             ("requested_batch", None)]:
            with self.subTest(field=field):
                self.assertEqual(self.state(dict(self.data, **{field: value}))[0], "failure")
        for field, value in [("id", 1548), ("id", True), ("id", "1547"), ("base", "other"),
                             ("state", "waiting"), ("state", "error"), ("state", "unknown")]:
            data = copy.deepcopy(self.data)
            data["requested_batch"][field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(self.state(data)[0], "failure")

    def test_captured_response_passes_the_real_observation_parser(self):
        response = io.BytesIO(json.dumps(self.data).encode())
        with patch("bors_observation.urllib.request.urlopen", return_value=response) as request:
            self.assertEqual(finalizer.terminal_state(self.env),
                             ("withdrawn", "batch withdrawn; no verdict posted"))
        self.assertIn("base=main&batch_id=1547", request.call_args.args[0].full_url)

    def test_live_and_resumed_finalization_retire_withdrawal_without_any_status_request(self):
        terminal_state = finalizer.terminal_state
        for resume in (False, True):
            with self.subTest(resume=resume), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "intent.json"
                path.write_text(json.dumps(finalizer.finalization_intent(self.env)))
                output = io.StringIO()
                with patch.dict(os.environ, dict(self.env, TAUCETI_STATUS_INTENT=str(path)), clear=True), \
                     patch.object(finalizer, "terminal_state",
                                  side_effect=lambda env: terminal_state(env, observe=lambda *_: self.data)), \
                     patch.object(finalizer, "api") as request, contextlib.redirect_stdout(output):
                    args = ["--resume", str(path)] if resume else []
                    self.assertEqual(finalizer.main(args), 0)
                request.assert_not_called()
                self.assertFalse(path.exists())
                self.assertIn("batch withdrawn; no verdict posted", output.getvalue())

    def test_withdrawal_during_status_retry_retires_existing_success_intent(self):
        intent = finalizer.status_intent(self.env, dict(state="success", context="build",
            description="audited", target_url="https://github.com/TauCetiProject/TauCeti/actions/runs/12"))
        for resume in (False, True):
            for conclusion in (None, "cancelled"):
                with self.subTest(resume=resume, conclusion=conclusion), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "intent.json"
                    path.write_text(json.dumps(intent))
                    with patch.object(finalizer, "observation", return_value=self.data), \
                         patch.object(finalizer, "api", side_effect=[dict(run_attempt=2, conclusion=conclusion), []]) as request, \
                         contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(finalizer.publish(intent, target=str(path), resume=resume), 0)
                    self.assertEqual(request.call_count, 2)
                    self.assertFalse(path.exists())

    def test_real_build_and_publisher_failures_remain_failures(self):
        observe = unittest.mock.Mock(side_effect=AssertionError("failed audits must remain failures"))
        for result in ("cancelled", "skipped"):
            env = dict(self.env, BUILD_JOB_RESULT=result)
            self.assertEqual(finalizer.terminal_state(env, observe=observe)[0], "failure")
        for result in ("success", "failure"):
            env = dict(self.env, BUILD_JOB_RESULT=result, BUILD_STATE="failure")
            self.assertEqual(finalizer.terminal_state(env, observe=observe)[0], "failure")
        env = dict(self.env, PUBLICATION_REQUIRED="true")
        wait = unittest.mock.Mock(return_value=("failure", "publication failed"))
        self.assertEqual(finalizer.terminal_state(env, wait=wait, observe=observe),
                         ("failure", "publication failed"))

    def test_canceled_run_with_invalid_or_unavailable_bors_still_posts_failure(self):
        intent = finalizer.status_intent(self.env, dict(state="success", context="build",
            description="audited", target_url="https://github.com/TauCetiProject/TauCeti/actions/runs/12"))
        for data in (None, dict(self.data, repo="other/repo"), OSError("unavailable"),
                     ValueError("incorrectly scoped bors observation")):
            observation = dict(side_effect=data) if isinstance(data, Exception) else dict(return_value=data)
            with self.subTest(data=data), patch.object(finalizer, "observation", **observation), \
                 patch.object(finalizer, "api", side_effect=[dict(run_attempt=2, conclusion="cancelled"), [], {}]) as request, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(finalizer.publish(intent), 1)
            self.assertEqual(request.call_args.args[1]["state"], "failure")


if __name__ == "__main__":
    unittest.main()
