import copy
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tauceti_tools import ci_candidate as cc
from tauceti_tools import ci_candidate_workflow as cw
from tauceti_tools.forge import GITHUB, Forge, RepositoryRef


def policy():
    native = Forge("forgejo", "https://source.example")
    return cc.ExecutionPolicy(
        cc.RepositoryIdentity(RepositoryRef(native, "team/Math"), 41),
        cc.RepositoryIdentity(RepositoryRef(GITHUB, "team/Execution"), 42),
        native,
        ".github/workflows/pr-build.yml",
        cc.RepositoryIdentity(RepositoryRef(GITHUB, "team/Tools"), 43),
        "a" * 40,
        "pilot",
        "main",
        True,
        hashlib.sha256(b"trusted configuration").hexdigest(),
    )


def request(config):
    return cc.CandidateRequest(
        "1" * 32,
        "2" * 32,
        config.digest,
        config.source,
        config.execution,
        "b" * 40,
        config.harness_revision,
        config.config_digest,
        5,
        7,
        "c" * 40,
        "d" * 40,
        "e" * 40,
        (cc.Member(17, "f" * 40),),
    )


def context(req, config):
    return {
        "event_name": "repository_dispatch",
        "action": cc.EVENT,
        "repository": config.execution.repository.name,
        "repository_id": str(config.execution.native_id),
        "workflow_sha": req.workflow_revision,
        "workflow_ref": f"{config.execution.repository.name}/{config.workflow}@refs/heads/main",
        "harness_repository": config.harness.repository.name,
        "harness_sha": config.harness_revision,
        "harness_file": ".github/workflows/pr-build.yml",
        "run_id": 101,
        "attempt": 2,
    }


def run(req, config):
    return {
        "id": 101,
        "run_attempt": 2,
        "status": "completed",
        "conclusion": "success",
        "head_sha": req.workflow_revision,
        "event": "repository_dispatch",
        "path": config.workflow,
        "repository": {"id": config.execution.native_id, "full_name": config.execution.repository.name},
        "referenced_workflows": [
            {
                "path": f"{config.harness.repository.name}/.github/workflows/pr-build.yml@{config.harness_revision}",
                "sha": config.harness_revision,
            }
        ],
    }


class CandidateContracts(unittest.TestCase):
    def setUp(self):
        self.policy = policy()
        self.request = request(self.policy)
        self.context = context(self.request, self.policy)
        self.run = run(self.request, self.policy)
        self.verdicts = dict.fromkeys(cc.VERDICTS, "success")
        self.result = cc.make_result(
            self.request,
            self.policy,
            self.context,
            tested_commit=self.request.head,
            tested_tree=self.request.tree,
            verdicts=self.verdicts,
        )

    def verify(self, value=None, run_value=None):
        return cc.verify_result(
            self.request, self.policy, value or self.result, run_value or self.run, run_id=101, attempt=2
        )

    def archive(self, result=None, *, names=None):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name in names or [cc.RESULT_FILE]:
                bundle.writestr(name, cc.canonical(result or self.result))
        raw = out.getvalue()
        metadata = {
            "id": 202,
            "name": cc.artifact_name(self.request, 101, 2),
            "size_in_bytes": len(raw),
            "expired": False,
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "workflow_run": {"id": 101, "repository_id": 42, "head_sha": self.request.workflow_revision},
        }
        return metadata, raw

    def test_round_trip_and_independent_source_and_execution_branches(self):
        decoded = cc.read_json(cc.canonical(self.request.record()))
        self.assertEqual(cc.CandidateRequest.parse(decoded, policy=self.policy), self.request)
        cc.check_workflow_context(self.request, self.policy, self.context)
        self.assertEqual(self.policy.base_branch, "pilot")
        self.assertEqual(self.request.dispatch()["event_type"], cc.EVENT)
        self.assertTrue(self.request.retained_ref.endswith(self.request.request_id + "/candidate"))
        self.assertTrue(self.request.member_ref(self.request.members[0]).endswith("/member-17"))
        self.assertEqual(self.verify(), self.verdicts)

    def test_policy_and_request_cannot_rebind_native_credentials(self):
        for field, replacement in (
            (
                "source",
                cc.RepositoryIdentity(
                    RepositoryRef(Forge("forgejo", "https://other.example"), "team/Math"), 41
                ).record(),
            ),
            ("source", cc.RepositoryIdentity(self.policy.source.repository, 99).record()),
            ("execution", self.policy.source.record()),
            ("harness_revision", "1" * 40),
            ("config_digest", "1" * 64),
            ("policy_digest", "1" * 64),
        ):
            with self.subTest(field=field, replacement=replacement):
                value = self.request.record()
                value[field] = replacement
                with self.assertRaises(ValueError):
                    cc.CandidateRequest.parse(value, policy=self.policy)
        config = self.policy.record(with_config=False)
        config["credential_origin"]["url"] = "https://other.example"
        with self.assertRaises(ValueError):
            cc.ExecutionPolicy.parse(config, configuration=b"trusted configuration")

    def test_changed_trusted_configuration_invalidates_prior_request(self):
        changed = cc.ExecutionPolicy.parse(self.policy.record(with_config=False), configuration=b"new configuration")
        with self.assertRaises(ValueError):
            cc.CandidateRequest.parse(self.request.record(), policy=changed)

    def test_request_rejects_ambiguous_json_native_ids_and_refs(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b"{" + b" " * cc.MAX_REQUEST_BYTES + b"}"):
            with self.subTest(raw=raw[:25]), self.assertRaises(ValueError):
                cc.read_json(raw)
        for modify in (
            lambda v: v["source"].update(id=True),
            lambda v: v.update(batch_id=True),
            lambda v: v.update(batch_version=0),
            lambda v: v["candidate"].update(retained_ref="refs/heads/main"),
            lambda v: v["candidate"].update(tree="0" * 40),
            lambda v: v["members"].append(v["members"][0]),
            lambda v: v.update(executable="curl"),
        ):
            value = self.request.record()
            modify(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                cc.CandidateRequest.parse(value, policy=self.policy)

    def test_actual_workflow_context_must_match_both_code_revisions(self):
        for key, wrong in (
            ("workflow_sha", "f" * 40),
            ("harness_sha", "f" * 40),
            ("harness_repository", "other/Tools"),
            ("repository_id", "99"),
            ("workflow_ref", "team/Execution/.github/workflows/evil.yml@refs/heads/main"),
            ("attempt", True),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                cc.check_workflow_context(self.request, self.policy, dict(self.context, **{key: wrong}))

    def test_forged_result_cannot_answer_another_receipt_or_candidate(self):
        for modify in (
            lambda v: v.update(request_digest="1" * 64),
            lambda v: v["request"].update(epoch="3" * 32),
            lambda v: v["request"]["members"][0].update(head="1" * 40),
            lambda v: v["producer"].update(attempt=1),
            lambda v: v["producer"].update(run_id=102),
            lambda v: v["producer"]["repository"].update(id=99),
            lambda v: v["tested"].update(commit="1" * 40),
            lambda v: v["tested"].update(tree="1" * 40),
            lambda v: v["verdicts"].update(publication="not_required"),
        ):
            value = copy.deepcopy(self.result)
            modify(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.verify(value)

    def test_artifact_claims_do_not_replace_fresh_authenticated_run_metadata(self):
        for modify in (
            lambda v: v.update(run_attempt=3),
            lambda v: v.update(run_attempt=True),
            lambda v: v.update(event="workflow_dispatch"),
            lambda v: v.update(head_sha="1" * 40),
            lambda v: v.update(path=".github/workflows/other.yml"),
            lambda v: v.update(status="in_progress"),
            lambda v: v.update(conclusion="failure"),
            lambda v: v["repository"].update(id=99),
            lambda v: v.update(referenced_workflows=[]),
            lambda v: v["referenced_workflows"][0].update(sha="1" * 40),
        ):
            value = copy.deepcopy(self.run)
            modify(value)
            with self.subTest(value=value), self.assertRaises((ValueError, cc.EvidenceUnavailable)):
                self.verify(run_value=value)

    def test_verified_failed_build_remains_a_failure_result(self):
        result = copy.deepcopy(self.result)
        result["verdicts"]["build"] = "failure"
        value = dict(self.run, conclusion="failure")
        self.assertEqual(self.verify(result, value)["build"], "failure")

    def test_policy_and_source_failures_dominate_infrastructure_for_queue_recovery(self):
        self.assertEqual(
            cc.classify_verdicts(
                dict(self.verdicts, build="infrastructure", scope="failure", publication="infrastructure")
            ),
            "policy_failure",
        )
        self.assertEqual(
            cc.classify_verdicts(dict(self.verdicts, build="infrastructure", bump_guard="failure")), "policy_failure"
        )
        self.assertEqual(
            cc.classify_verdicts(dict(self.verdicts, build="failure", publication="infrastructure")), "source_failure"
        )
        self.assertEqual(cc.classify_verdicts(dict(self.verdicts, publication="infrastructure")), "infrastructure")
        self.assertEqual(cc.classify_verdicts(self.verdicts), "passed")

    def test_artifact_is_bound_to_its_request_run_attempt_and_digest(self):
        metadata, raw = self.archive()
        self.assertEqual(
            cc.verify_artifact(self.request, self.policy, metadata, raw, self.run, run_id=101, attempt=2), self.verdicts
        )
        for modify in (
            lambda v: v.update(expired=True),
            lambda v: v.update(name=cc.artifact_name(self.request, 101, 1)),
            lambda v: v.update(digest="sha256:" + "1" * 64),
            lambda v: v.update(size_in_bytes=cc.MAX_ARTIFACT_BYTES + 1),
            lambda v: v["workflow_run"].update(id=102),
            lambda v: v["workflow_run"].update(repository_id=99),
            lambda v: v["workflow_run"].update(head_sha="1" * 40),
        ):
            changed = copy.deepcopy(metadata)
            modify(changed)
            with self.subTest(value=changed), self.assertRaises(ValueError):
                cc.verify_artifact(self.request, self.policy, changed, raw, self.run, run_id=101, attempt=2)

    def test_artifact_archive_paths_and_additional_entries_are_not_consumed(self):
        for names in (["../" + cc.RESULT_FILE], ["nested/" + cc.RESULT_FILE], [cc.RESULT_FILE, "unexpected.json"]):
            metadata, raw = self.archive(names=names)
            with self.subTest(names=names), self.assertRaises(ValueError):
                cc.verify_artifact(self.request, self.policy, metadata, raw, self.run, run_id=101, attempt=2)

    def test_bad_ingress_is_refused_before_constructing_source_client(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "policy.json").write_bytes(cc.canonical(self.policy.record(with_config=False)))
            (root / "project.toml").write_bytes(b"trusted configuration")
            value = self.request.dispatch()
            value["client_payload"]["request"]["source"]["forge"]["url"] = "https://other.example"
            (root / "event.json").write_bytes(
                cc.canonical({"action": cc.EVENT, "client_payload": value["client_payload"]})
            )
            with (
                patch.object(cw, "Forgejo", side_effect=AssertionError("no credential use")),
                self.assertRaises(ValueError),
            ):
                cw.load_request(root / "policy.json", root / "project.toml", root / "event.json", self.context)


if __name__ == "__main__":
    unittest.main()
