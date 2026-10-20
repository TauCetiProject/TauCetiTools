#!/usr/bin/env python3
"""S2 admin CLI defaults to read-only and requires explicit identities to mutate."""

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services/github-state/scripts"))
spec = importlib.util.spec_from_file_location("state_migrate", ROOT / "services/github-state/scripts/migrate.py")
migrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate)


def run(*args):
    calls = []

    def call(environment, body, **options):
        calls.append((environment, body))
        return {"phase": "source_verified"}

    with patch.object(sys, "argv", ["migrate.py", *args]), patch.object(migrate, "call", call):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            migrate.main()
    return calls


class MigrationTests(unittest.TestCase):
    def test_service_overrides_and_credential_url_rejection(self):
        with patch.object(
            sys,
            "argv",
            [
                "migrate.py",
                "--environment",
                "test",
                "--service-url",
                "https://example.org",
                "--credentials-dir",
                "/private/credentials",
            ],
        ):
            with patch.object(migrate, "call", return_value={}) as call, contextlib.redirect_stdout(io.StringIO()):
                migrate.main()
                self.assertEqual(
                    call.call_args.kwargs,
                    {"service_url": "https://example.org", "credentials_dir": Path("/private/credentials")},
                )
        for url in ["http://example.org", "https://user:secret@example.org", "https://example.org?token=secret"]:
            output = io.StringIO()
            with patch.object(sys, "argv", ["migrate.py", "--environment", "test", "--service-url", url]):
                with contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as error:
                    migrate.main()
                self.assertEqual(error.exception.code, 2)
                self.assertNotIn("secret", output.getvalue())

    def test_supervised_resume_advances_past_source_verification(self):
        responses = iter([{"phase": "source_verified", "cutover": {"environment": "test"}}, {"phase": "complete"}])
        argv = ["migrate.py", "--environment", "test", "--generation", "c707ddba-076d-4a63-8696-96533031ce88", "--target", "neon-test", "--apply", "--steps", "10", "resume"]
        with patch.object(sys, "argv", argv), patch.object(migrate, "call", side_effect=lambda *_args: next(responses)) as call:
            with contextlib.redirect_stdout(io.StringIO()):
                migrate.main()
        self.assertEqual(call.call_count, 2)

    def test_bulk_resume_returns_when_replay_owners_take_over(self):
        responses = iter([{"phase": "importing"}, {"phase": "replaying", "cutover": {"target_active": True}}])
        argv = ["migrate.py", "--environment", "test", "--generation", "c707ddba-076d-4a63-8696-96533031ce88", "--target", "neon-test", "--apply", "--steps", "10000", "resume"]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(migrate, "call", side_effect=lambda *_args: next(responses)) as call:
            with contextlib.redirect_stdout(output):
                migrate.main()
        self.assertEqual(call.call_count, 2)
        self.assertIn('"phase": "replaying"', output.getvalue())

    def test_bounded_batch_steps(self):
        args = ["--environment", "test", "--apply", "--generation",
                "c707ddba-076d-4a63-8696-96533031ce88", "--target", "neon-test"]
        self.assertEqual(run(*args, "resume")[0][1]["batch_steps"], 1)
        self.assertEqual(run(*args, "--batch-steps", "100", "resume")[0][1]["batch_steps"], 100)
        for value in ["0", "101", "-1"]:
            with self.assertRaises(SystemExit):
                run(*args, "--batch-steps", value, "resume")

    def test_finish_observation_requires_and_transmits_policy(self):
        args = ["--environment", "test", "--apply", "--generation",
                "c707ddba-076d-4a63-8696-96533031ce88", "--target", "neon-test"]
        with self.assertRaises(SystemExit):
            run(*args, "finish-observation")
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / "policy.json"
            policy.write_text('{"mode":"dry_run","disabled":[],"pinned":[]}')
            calls = run(*args, "--retention-policy", str(policy), "finish-observation")
            self.assertEqual(calls[0][1]["operation"], "finish-observation")
            self.assertEqual(calls[0][1]["retention_policy"]["mode"], "dry_run")

    def test_cli_safety(self):
        for command in ["preflight", "start", "freeze", "resume", "abandon"]:
            assert run("--environment", "production", command) == [
                (
                    "production",
                    {"repository": "TauCetiProject/TauCeti", "environment": "production", "operation": "preflight"},
                )
            ]
        for args in [[], ["--environment", "test", "--apply", "freeze"], ["--environment", "test", "--steps", "0"]]:
            try:
                run(*args)
            except SystemExit as error:
                assert error.code == 2
            else:
                raise AssertionError("CLI accepted missing environment/identity or invalid bounds")

        calls = run(
            "--environment",
            "test",
            "--apply",
            "--generation",
            "c707ddba-076d-4a63-8696-96533031ce88",
            "--target",
            "neon-test-branch-database",
            "--steps",
            "10",
            "resume",
        )
        assert len(calls) == 1, "stop when the source is verified"
        assert calls[0][1]["operation"] == "step" and calls[0][1]["apply"] is True

        # Arbitrary proxy bodies/driver messages must never be printed. Only fixed
        # reason codes may be surfaced, and error parsing has a bounded read.
        class ErrorBody(io.BytesIO):
            def read(self, size=-1):
                assert size == 4096
                return super().read(size)

        with patch.object(migrate, "service", return_value=("https://service.test", Path("/private"))):
            with patch.object(Path, "read_text", return_value="private-admin-token"):
                cases = [
                    (
                        urllib.error.HTTPError(
                            "https://service.test",
                            500,
                            "private password",
                            {},
                            ErrorBody(b'{"error":"private_credential"}'),
                        ),
                        None,
                    ),
                    (
                        urllib.error.HTTPError(
                            "https://service.test",
                            400,
                            "bad request",
                            {},
                            ErrorBody(b'{"error":"migration_identity_conflict"}'),
                        ),
                        "migration_identity_conflict",
                    ),
                    (
                        urllib.error.HTTPError(
                            "https://service.test", 400, "bad request", {}, ErrorBody(b'{"error":{}}')
                        ),
                        None,
                    ),
                    (urllib.error.URLError("private database credential"), None),
                ]
                for error, expected in cases:
                    with patch.object(migrate.urllib.request, "urlopen", side_effect=error):
                        try:
                            migrate.call("test", {})
                        except SystemExit as result:
                            assert "private" not in str(result) and "credential" not in str(result)
                            if expected:
                                assert expected in str(result)
                        else:
                            raise AssertionError("transport error was ignored")


if __name__ == "__main__":
    unittest.main()
