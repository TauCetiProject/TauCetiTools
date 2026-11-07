"""Early request ownership is distinct from a completed source build verdict."""

import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import yaml

from tauceti_tools import ci_candidate as cc
from tauceti_tools import ci_candidate_workflow as cw
from tauceti_tools.ci_candidate_evidence import GitHubEvidence
from tauceti_tools.forge import GITHUB
from tests import test_ci_candidate_workflow as workflow_fixtures
from tests.test_ci_candidate import context, policy, request, run
from tests.test_ci_member_manifest import retained_request

CONFIG = workflow_fixtures.CONFIG


class Receipts(unittest.TestCase):
    def setUp(self):
        self.policy = policy()
        self.request = request(self.policy)
        self.context = context(self.request, self.policy)
        self.run = dict(run(self.request, self.policy), status="in_progress", conclusion=None)
        self.receipt = cc.make_receipt(self.request, self.policy, self.context)

    def archive(self, *, names=None, receipt=None):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name in names or [cc.RECEIPT_FILE]:
                bundle.writestr(name, cc.canonical(self.receipt if receipt is None else receipt))
        raw = output.getvalue()
        metadata = {
            "id": 202,
            "name": cc.receipt_artifact_name(self.request, 101, 2),
            "size_in_bytes": len(raw),
            "expired": False,
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "workflow_run": {"id": 101, "repository_id": 42, "head_sha": self.request.workflow_revision},
        }
        return metadata, raw

    def test_receipt_identifies_a_running_request_without_a_build_or_source_verdict(self):
        for status, conclusion in (
            ("queued", None),
            ("in_progress", None),
            ("completed", "failure"),
            ("completed", "success"),
            ("completed", "cancelled"),
        ):
            with self.subTest(status=status, conclusion=conclusion):
                current = dict(self.run, status=status, conclusion=conclusion)
                self.assertEqual(
                    cc.verify_receipt(self.request, self.policy, self.receipt, current, run_id=101, observed_attempt=2),
                    {"request_digest": self.request.digest, "receipt_attempt": 2, "observed_attempt": 2},
                )
        self.assertEqual(set(self.receipt), {"schema", "request", "request_digest", "producer"})
        for count in (1197, 2394):
            config, original, _raw = retained_request(count)
            incoming = cc.CandidateRequest.parse(original.record(), policy=config)
            receipt = cc.make_receipt(incoming, config, context(incoming, config))
            self.assertLess(len(cc.canonical(receipt)), 4096)
            self.assertEqual(
                cc.verify_receipt(original, config, receipt, run(original, config), run_id=101, observed_attempt=2),
                {"request_digest": original.digest, "receipt_attempt": 2, "observed_attempt": 2},
            )

    def test_untrusted_receipt_claims_and_changed_attempts_do_not_bind_a_run(self):
        mutations = [
            lambda value: value.update(schema=cc.SCHEMA),
            lambda value: value.update(request_digest="0" * 64),
            lambda value: value["request"]["candidate"].update(head="f" * 40),
            lambda value: value["producer"].update(run_id=102),
            lambda value: value["producer"].update(attempt=0),
            lambda value: value["producer"]["repository"].update(id=99),
            lambda value: value["producer"].update(harness_revision="f" * 40),
            lambda value: value.update(verdicts=dict.fromkeys(cc.VERDICTS, "success")),
        ]
        for mutate in mutations:
            changed = copy.deepcopy(self.receipt)
            mutate(changed)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                cc.verify_receipt(self.request, self.policy, changed, self.run, run_id=101, observed_attempt=2)
        with self.assertRaises(cc.EvidenceUnavailable):
            cc.verify_receipt(
                self.request, self.policy, self.receipt, dict(self.run, run_attempt=3), run_id=101, observed_attempt=2
            )
        for change in (
            {"head_sha": "f" * 40},
            {"event": "push"},
            {"repository": {"id": 99, "full_name": "team/Execution"}},
            {"path": ".github/workflows/other.yml"},
            {"referenced_workflows": []},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                cc.verify_receipt(
                    self.request, self.policy, self.receipt, dict(self.run, **change), run_id=101, observed_attempt=2
                )

    def test_receipt_and_result_artifacts_are_not_interchangeable(self):
        metadata, raw = self.archive()
        self.assertEqual(
            cc.verify_receipt_artifact(
                self.request, self.policy, metadata, raw, self.run, run_id=101, observed_attempt=2, receipt_attempt=2
            ),
            {"request_digest": self.request.digest, "receipt_attempt": 2, "observed_attempt": 2},
        )
        completed = dict(self.run, status="completed", conclusion="success")
        with self.assertRaises(ValueError):
            cc.verify_result(self.request, self.policy, self.receipt, completed, run_id=101, attempt=2)
        with self.assertRaises(ValueError):
            cc.verify_artifact(self.request, self.policy, metadata, raw, completed, run_id=101, attempt=2)
        with self.assertRaisesRegex(ValueError, "entries"):
            cc.verify_artifact(
                self.request,
                self.policy,
                dict(metadata, name=cc.artifact_name(self.request, 101, 2)),
                raw,
                completed,
                run_id=101,
                attempt=2,
            )
        for names in (["../request.json"], [cc.RECEIPT_FILE, "extra.json"], [cc.RESULT_FILE]):
            metadata, raw = self.archive(names=names)
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, "entries"):
                cc.verify_receipt_artifact(
                    self.request,
                    self.policy,
                    metadata,
                    raw,
                    self.run,
                    run_id=101,
                    observed_attempt=2,
                    receipt_attempt=2,
                )
        metadata, raw = self.archive()
        for change in (
            {"name": cc.receipt_artifact_name(self.request, 101, 1)},
            {"digest": "sha256:" + "0" * 64},
            {"expired": True},
            {"workflow_run": {"id": 102, "repository_id": 42, "head_sha": self.request.workflow_revision}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                cc.verify_receipt_artifact(
                    self.request,
                    self.policy,
                    dict(metadata, **change),
                    raw,
                    self.run,
                    run_id=101,
                    observed_attempt=2,
                    receipt_attempt=2,
                )

    def reader(self, *, current_attempt=None, observed_attempt=2):
        fixture = self
        metadata, raw = self.archive()

        class Reader(GitHubEvidence):
            def __init__(self):
                super().__init__(fixture.policy, token="execution-token", token_origin=GITHUB)
                self.run_reads = 0

            def _read(self, req, limit):
                return json.dumps({"id": 43, "full_name": "team/Tools"}).encode()

            def get(self, path):
                if path == "/actions/runs/101":
                    self.run_reads += 1
                    if self.run_reads == 1:
                        return dict(fixture.run, run_attempt=observed_attempt)
                    return dict(
                        fixture.run,
                        run_attempt=observed_attempt if current_attempt is None else current_attempt,
                        status="completed",
                        conclusion="failure",
                    )
                if path.startswith("/actions/runs/101/artifacts?"):
                    return {"total_count": 1, "artifacts": [metadata]}
                if path == "/actions/artifacts/202":
                    return metadata
                raise AssertionError(path)

            def artifact_bytes(self, artifact_id):
                fixture.assertEqual(artifact_id, 202)
                return raw

        return Reader()

    def test_api_reader_checks_fresh_attempt_without_granting_a_verdict(self):
        reader = self.reader()
        proof = reader.receipt(self.request, run_id=101, observed_attempt=2)
        self.assertEqual(proof["kind"], "request")
        self.assertEqual(proof["request_digest"], self.request.digest)
        self.assertNotIn("verdicts", proof)
        self.assertNotIn("classification", proof)
        self.assertNotIn("attempt", proof)
        self.assertEqual((proof["receipt_attempt"], proof["observed_attempt"]), (2, 2))
        self.assertEqual(reader.run_reads, 2)
        with self.assertRaises(cc.EvidenceUnavailable):
            self.reader(current_attempt=3).receipt(self.request, run_id=101, observed_attempt=2)
        with self.assertRaises(cc.EvidenceUnavailable):
            self.reader().result(self.request, run_id=101, attempt=2)
        reader = self.reader()
        original = reader.get
        reader.get = lambda path: {"total_count": 0, "artifacts": []} if "/artifacts?" in path else original(path)
        with self.assertRaises(cc.EvidenceUnavailable):
            reader.receipt(self.request, run_id=101, observed_attempt=2)

    def test_partial_reruns_preserve_run_ownership_without_promoting_old_results(self):
        reader = self.reader(observed_attempt=3)
        proof = reader.receipt(self.request, run_id=101, observed_attempt=3)
        self.assertEqual((proof["receipt_attempt"], proof["observed_attempt"]), (2, 3))
        self.assertEqual(proof["kind"], "request")
        self.assertNotIn("verdicts", proof)
        with self.assertRaises(cc.EvidenceUnavailable):
            reader.result(self.request, run_id=101, attempt=3)
        metadata, raw = self.archive()
        with self.assertRaises(ValueError):
            cc.verify_receipt_artifact(
                self.request,
                self.policy,
                metadata,
                raw,
                dict(self.run, run_attempt=3),
                run_id=101,
                observed_attempt=3,
                receipt_attempt=1,
            )
        with self.assertRaises(cc.EvidenceUnavailable):
            cc.verify_receipt_artifact(
                self.request, self.policy, metadata, raw, self.run, run_id=101, observed_attempt=2, receipt_attempt=3
            )

    def test_multiple_request_receipts_and_future_attempts_cannot_be_adopted(self):
        metadata, _raw = self.archive()
        for name in (
            cc.receipt_artifact_name(self.request, 101, 3),
            cc.receipt_artifact_name(self.request, 101, 2).replace(self.request.request_id, "f" * 32),
        ):
            reader = self.reader()
            original = reader.get
            listing = {"total_count": 2, "artifacts": [metadata, dict(metadata, id=203, name=name)]}
            reader.get = lambda path, original=original, listing=listing: (
                listing if "/artifacts?" in path else original(path)
            )
            with self.subTest(name=name), self.assertRaises(cc.EvidenceUnavailable):
                reader.receipt(self.request, run_id=101, observed_attempt=2)
        reader = self.reader(observed_attempt=3)
        original = reader.get
        listing = {
            "total_count": 2,
            "artifacts": [metadata, dict(metadata, id=203, name=cc.receipt_artifact_name(self.request, 101, 1))],
        }
        reader.get = lambda path: listing if "/artifacts?" in path else original(path)
        self.assertEqual(reader.receipt(self.request, run_id=101, observed_attempt=3)["receipt_attempt"], 2)

    def test_bootstrap_cli_writes_exclusive_receipt_before_any_source_access(self):
        config = cc.ExecutionPolicy.parse(policy().record(with_config=False), configuration=CONFIG)
        req = request(config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = root / "tauceti.toml"
            conf.write_bytes(CONFIG)
            receipt = root / cc.RECEIPT_FILE
            output = root / "outputs"
            args = [
                "bootstrap",
                "--policy",
                "trusted-policy",
                "--configuration",
                str(conf),
                "--output",
                str(root / "bootstrap.json"),
                "--scripts",
                str(root / "scripts"),
                "--lean-root",
                "Math",
                "--roadmap-dir",
                "Roadmap",
                "--receipt-output",
                str(receipt),
                "--github-output",
                str(output),
            ]
            with (
                patch.object(cw, "load_request", return_value=(req, config)),
                patch.object(cw, "context_from_environment", return_value=context(req, config)),
                patch.object(cw, "Forgejo", side_effect=AssertionError("source access during bootstrap")),
                patch.object(cw, "GitHubEvidence", side_effect=AssertionError("API access during bootstrap")),
                patch.dict(os.environ, CI_SOURCE_READ_TOKEN="must-not-be-used"),
            ):
                self.assertEqual(cw.main(args), 0)
                actual = cc.read_json(receipt.read_bytes(), max_bytes=cc.MAX_RESULT_BYTES)
                self.assertEqual(actual, cc.make_receipt(req, config, context(req, config)))
                self.assertNotIn("must-not-be-used", receipt.read_text())
                self.assertEqual(output.read_text(), f"request-artifact-name={cc.receipt_artifact_name(req, 101, 2)}\n")
                before = receipt.read_bytes()
                with self.assertRaises(FileExistsError):
                    cw.main(args)
                self.assertEqual(receipt.read_bytes(), before)

    def test_failed_setup_or_receipt_upload_leaves_only_infrastructure_verdicts(self):
        fixture = workflow_fixtures.Workflow()
        fixture.setUp()
        evidence = dict.fromkeys(fixture.evidence, "")
        evidence.update(job_result="failure", build_outcome="skipped")
        result = cw.finalize(self.request, self.policy, self.context, evidence)
        self.assertIsNone(result["tested"])
        self.assertEqual(set(result["verdicts"].values()), {"infrastructure"})
        self.assertEqual(cc.classify_verdicts(result["verdicts"]), "infrastructure")

    def test_workflow_publishes_receipt_before_native_reads_or_dependency_execution(self):
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/pr-build.yml").read_text())
        steps = workflow["jobs"]["sandboxed-build"]["steps"]
        names = [step.get("name") for step in steps]
        setup = steps[names.index("Prepare trusted project scripts")]
        upload_index = names.index("Publish verified source request receipt")
        upload = steps[upload_index]
        self.assertLess(names.index("Prepare trusted project scripts"), upload_index)
        self.assertLess(upload_index, names.index("Resolve PR head"))
        self.assertEqual(setup["id"], "trusted-setup")
        self.assertIn('--receipt-output "$RUNNER_TEMP/source-candidate-request.json"', setup["run"])
        self.assertNotIn("CI_SOURCE_READ_TOKEN", setup["env"])
        self.assertEqual(upload["if"], "${{ env.SOURCE_CANDIDATE == 'true' }}")
        self.assertEqual(upload["uses"], "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02")
        self.assertEqual(upload["with"]["name"], "${{ steps.trusted-setup.outputs.request-artifact-name }}")
        self.assertEqual(upload["with"]["path"], "${{ runner.temp }}/source-candidate-request.json")
        self.assertEqual(upload["with"]["if-no-files-found"], "error")


if __name__ == "__main__":
    unittest.main()
