"""Trusted CI credentials can expire while a quota reset is pending."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts"))
import github_api as api
import github_app_token as app
import finalize_batch as finalizer


def reply(status=200, data=None):
    return subprocess.CompletedProcess([], 0 if status < 400 else 1,
                                       f"HTTP/2.0 {status}\r\n\r\n" + json.dumps(data), "")


class Renewal(unittest.TestCase):
    def test_signing_key_is_private_temporary_and_not_passed_in_child_environment(self):
        paths = []
        def sign(argv, **kwargs):
            path = Path(argv[-1])
            paths.append(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_text(), "private-signing-key")
            self.assertNotIn("CI_STATUS_APP_PRIVATE_KEY", kwargs["env"])
            self.assertNotIn("GH_TOKEN", kwargs["env"])
            return subprocess.CompletedProcess(argv, 0, b"signature", b"")
        with patch.dict(os.environ, CI_STATUS_APP_PRIVATE_KEY="private-signing-key", GH_TOKEN="old-token"), \
             patch.object(app.subprocess, "run", side_effect=sign), patch.object(app.time, "time", return_value=1000):
            jwt = app.signed_jwt("12", "private-signing-key")
        self.assertFalse(paths[0].exists())
        payload = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "==="))
        self.assertEqual(payload, dict(iat=940, exp=1540, iss="12"))
        with patch.object(app.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"private-signing-key")), \
             self.assertRaisesRegex(RuntimeError, "signing failed") as error:
            app.signed_jwt("12", "private-signing-key")
        self.assertNotIn("private-signing-key", str(error.exception))

    def test_renewal_requests_only_repo_scoped_status_permissions(self):
        responses = []
        for data in [dict(id=7), dict(token="new-token")]:
            response = MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.headers = {}
            response.read.return_value = json.dumps(data).encode()
            responses.append(response)
        opener = MagicMock()
        opener.open.side_effect = responses
        with patch.object(app, "signed_jwt", return_value="short-lived-jwt"), \
             patch.object(app.urllib.request, "build_opener", return_value=opener), \
             patch.dict(os.environ, GITHUB_ACTIONS="false"):
            self.assertEqual(app.status_token("owner/project", "12", "private-key"), "new-token")
        first, second = opener.open.call_args_list
        self.assertEqual(first.args[0].full_url, "https://api.github.com/repos/owner/project/installation")
        self.assertEqual(second.args[0].full_url, "https://api.github.com/app/installations/7/access_tokens")
        self.assertEqual(json.loads(second.args[0].data),
                         dict(repositories=["project"], permissions={"statuses": "write"}))
        self.assertIsNone(app.NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_status_401_renews_once_and_reuses_new_token_without_disclosing_secrets(self):
        output = io.StringIO()
        with patch.dict(os.environ, GH_TOKEN="old-token", CI_STATUS_APP_ID="12", CI_STATUS_APP_PRIVATE_KEY="private-key"), \
             patch.object(app, "status_token", return_value="renewed-token") as renew, \
             patch.object(api.subprocess, "run", side_effect=[reply(401), reply(201, {}), reply(201, {})]) as request, \
             patch.object(api.time, "sleep"), contextlib.redirect_stderr(output):
            client = api.GitHub(max_wait=0)
            for _ in range(2):
                client.request("POST", "repos/o/r/statuses/" + "a" * 40, {}, idempotent=True)
        renew.assert_called_once()
        self.assertEqual(renew.call_args.args, ("o/r", "12", "private-key"))
        self.assertEqual(request.call_args.kwargs["env"]["GH_TOKEN"], "renewed-token")
        self.assertNotIn("CI_STATUS_APP_PRIVATE_KEY", request.call_args.kwargs["env"])
        for value in ("old-token", "renewed-token", "private-key"):
            self.assertNotIn(value, output.getvalue())

    def test_no_renewal_on_unrelated_endpoints_or_permissions_failures(self):
        for method, path, status in [("GET", "repos/o/r/pulls/1", 401),
                                     ("POST", "repos/o/r/issues/1/comments", 401),
                                     ("POST", "repos/o/r/statuses/" + "a" * 40, 403)]:
            with self.subTest(path=path, status=status), \
                 patch.dict(os.environ, CI_STATUS_APP_ID="12", CI_STATUS_APP_PRIVATE_KEY="private-key"), \
                 patch.object(app, "status_token") as renew, \
                 patch.object(api.subprocess, "run", return_value=reply(status, {"message": "denied"})), \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(api.APIError):
                api.GitHub(max_wait=0).request(method, path, {})
            renew.assert_not_called()
        with patch.dict(os.environ, CI_STATUS_APP_ID="12", CI_STATUS_APP_PRIVATE_KEY="private-key"), \
             patch.object(app, "status_token", return_value="bad-token") as renew, \
             patch.object(api.subprocess, "run", return_value=reply(401, {"message": "denied"})) as request, \
             patch.object(api.time, "sleep"), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(api.APIError):
            api.GitHub(max_wait=0).request("POST", "repos/o/r/statuses/" + "a" * 40, {})
        renew.assert_called_once()
        self.assertEqual(request.call_count, 2)


class FinalizerStaleness(unittest.TestCase):
    def intent(self):
        return finalizer.status_intent(dict(STATUS_SHA="a" * 40, GITHUB_RUN_ID="12", GITHUB_RUN_ATTEMPT="2"),
            dict(state="success", context="build", description="ok",
                 target_url="https://github.com/TauCetiProject/TauCeti/actions/runs/12"))

    def test_new_attempt_during_quota_wait_cannot_be_overwritten(self):
        now = [0]
        def sleep(delay): now[0] += delay
        deferred = api.Deferred("ci-status-app", 1060)
        with patch.object(finalizer, "api", side_effect=[dict(run_attempt=2), deferred, dict(run_attempt=3)]) as request, \
             patch.object(finalizer.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(finalizer.time, "time", side_effect=lambda: 1000 + now[0]), \
             patch.object(finalizer.time, "sleep", side_effect=sleep), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(finalizer.publish(self.intent()), 1)
        self.assertEqual(now[0], 60)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sum(call.args[0].startswith("/statuses/") for call in request.call_args_list), 1)

    def test_changed_bors_generation_during_quota_wait_is_revalidated(self):
        now = [0]
        def sleep(delay): now[0] += delay
        deferred = api.Deferred("ci-status-app", 1060)
        intent = dict(self.intent(), bors=dict(batch_id=7, base_ref="main", base_sha="b" * 40))
        with patch.object(finalizer, "api", side_effect=[dict(run_attempt=2), deferred, dict(run_attempt=2)]) as request, \
             patch.object(finalizer, "observation", return_value={}), \
             patch.object(finalizer, "candidate_ref", side_effect=["staging-ref", ValueError("obsolete")]) as candidate, \
             patch.object(finalizer.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(finalizer.time, "time", side_effect=lambda: 1000 + now[0]), \
             patch.object(finalizer.time, "sleep", side_effect=sleep), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(finalizer.publish(intent), 1)
        self.assertEqual(candidate.call_count, 2)
        self.assertEqual(sum(call.args[0].startswith("/statuses/") for call in request.call_args_list), 1)


if __name__ == "__main__":
    unittest.main()
