import copy
import hashlib
import io
import tempfile
import unittest
import zipfile
from dataclasses import replace
from functools import partial
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
        "harness_sha": "a" * 40,
        "harness_ref": f"{config.harness.repository.name}/.github/workflows/pr-build.yml@refs/heads/main",
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
                "path": f"{config.harness.repository.name}/.github/workflows/pr-build.yml@main",
                "sha": "a" * 40,
                "ref": "refs/heads/main",
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
            ("harness_sha", "main"),
            ("harness_sha", None),
            ("harness_ref", "team/Tools/.github/workflows/pr-build.yml@refs/heads/other"),
            ("harness_ref", "fork/Tools/.github/workflows/pr-build.yml@refs/heads/main"),
            ("harness_ref", "team/Tools/.github/workflows/pr-build.yml@" + "a" * 40),
            ("harness_ref", None),
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
            lambda v: v["referenced_workflows"][0].update(sha="main"),
            lambda v: v["referenced_workflows"][0].update(ref="refs/heads/other"),
            lambda v: v["referenced_workflows"][0].pop("ref"),
            lambda v: v["referenced_workflows"][0].update(path="team/Tools/.github/workflows/pr-build.yml@other"),
            lambda v: v["referenced_workflows"][0].update(path="team/Tools/.github/workflows/pr-build.yml@" + "a" * 40),
            lambda v: v["referenced_workflows"][0].update(path="fork/Tools/.github/workflows/pr-build.yml@main"),
        ):
            value = copy.deepcopy(self.run)
            modify(value)
            with self.subTest(value=value), self.assertRaises((ValueError, cc.EvidenceUnavailable)):
                self.verify(run_value=value)

    def test_full_ref_spelling_of_harness_main_is_accepted(self):
        value = copy.deepcopy(self.run)
        value["referenced_workflows"][0]["path"] = "team/Tools/.github/workflows/pr-build.yml@refs/heads/main"
        self.assertEqual(self.verify(run_value=value), self.verdicts)

    def test_policy_with_a_harness_revision_is_refused(self):
        value = self.policy.record(with_config=False)
        value["harness_revision"] = "a" * 40
        with self.assertRaises(ValueError):
            cc.ExecutionPolicy.parse(value, configuration=b"trusted configuration")

    def test_staging_request_record_and_digest_predate_lanes(self):
        self.assertIsNone(self.request.lane)
        self.assertNotIn("lane", self.request.record())
        # Golden digest of this fixture, computed before lanes existed.
        self.assertEqual(self.request.digest, "75d7cfaaa4066b4d21a9cb45deef34ffcd600b765d14e4d66d1adeba0ab7121e")
        self.assertTrue(cc.requires_publication(self.request, self.policy))

    def test_pr_lane_round_trips_and_changes_the_request_digest(self):
        pr = replace(self.request, lane="pr")
        record = pr.record()
        self.assertEqual(record["lane"], "pr")
        self.assertEqual(cc.CandidateRequest.parse(cc.read_json(cc.canonical(record)), policy=self.policy), pr)
        self.assertNotEqual(pr.digest, self.request.digest)
        self.assertFalse(cc.requires_publication(pr, self.policy))
        self.assertFalse(cc.requires_publication(pr, replace(self.policy, require_publication=False)))

    def test_invalid_lane_values_are_refused(self):
        for lane in ("", "staging", "PR", 1, ["pr"]):
            with self.subTest(lane=lane):
                with self.assertRaises(ValueError):
                    replace(self.request, lane=lane)
                with self.assertRaises(ValueError):
                    cc.CandidateRequest.parse(dict(self.request.record(), lane=lane), policy=self.policy)
        with self.assertRaises(ValueError):
            cc.CandidateRequest.parse(dict(self.request.record(), lane=None), policy=self.policy)

    def test_publication_verdicts_follow_the_lane_and_policy(self):
        lax = replace(self.policy, require_publication=False)
        cases = (
            # (lane, policy, allowed publication verdicts)
            (None, self.policy, {"success", "failure", "infrastructure"}),
            (None, lax, {"success", "failure", "infrastructure", "not_required"}),
            ("pr", self.policy, {"not_required"}),
            ("pr", lax, {"not_required"}),
        )
        for lane, config, allowed in cases:
            req = replace(request(config), lane=lane)
            ctx, fresh = context(req, config), run(req, config)
            for value in ("success", "failure", "infrastructure", "not_required"):
                with self.subTest(lane=lane, required=config.require_publication, value=value):
                    verdicts = dict(self.verdicts, publication=value)
                    make = partial(
                        cc.make_result,
                        req,
                        config,
                        ctx,
                        tested_commit=req.head,
                        tested_tree=req.tree,
                        verdicts=verdicts,
                    )
                    if value not in allowed:
                        with self.assertRaises(ValueError):
                            make()
                        continue
                    result = make()
                    conclusion = "success" if value in {"success", "not_required"} else "failure"
                    self.assertEqual(
                        cc.verify_result(
                            req, config, result, dict(fresh, conclusion=conclusion), run_id=101, attempt=2
                        ),
                        verdicts,
                    )

    def test_pr_lane_result_never_verifies_as_staging_evidence(self):
        pr = replace(self.request, lane="pr")
        result = cc.make_result(
            pr,
            self.policy,
            context(pr, self.policy),
            tested_commit=pr.head,
            tested_tree=pr.tree,
            verdicts=dict(self.verdicts, publication="not_required"),
        )
        with self.assertRaises(ValueError):
            self.verify(result)
        # Nor can a staging result be passed off as per-PR evidence.
        with self.assertRaises(ValueError):
            cc.verify_result(pr, self.policy, self.result, run(pr, self.policy), run_id=101, attempt=2)

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


class MixedMemberContracts(unittest.TestCase):
    def setUp(self):
        self.github = cc.RepositoryIdentity(RepositoryRef(GITHUB, "team/Math"), 99)
        self.policy = replace(policy(), member_sources=(policy().source, self.github))
        self.request = replace(
            request(self.policy),
            members=(cc.Member(17, "f" * 40, self.policy.source), cc.Member(17, "e" * 40, self.github)),
        )

    def test_same_native_number_is_distinct_across_enrolled_sources(self):
        value = cc.CandidateRequest.parse(self.request.record(), policy=self.policy)
        self.assertEqual(value.members, self.request.members)
        self.assertNotEqual(value.member_ref(value.members[0]), value.member_ref(value.members[1]))
        self.assertNotEqual(value.members[0].key, value.members[1].key)

    def test_source_enrollment_is_independent_of_execution_repository(self):
        self.assertNotEqual(self.github, self.policy.execution)
        self.request.check_policy(self.policy)
        with self.assertRaises(ValueError):
            replace(self.request, members=(cc.Member(1, "f" * 40, self.policy.execution),)).check_policy(self.policy)

    def test_unknown_sources_and_recreated_native_ids_are_refused(self):
        for identity in (
            replace(self.github, native_id=100),
            cc.RepositoryIdentity(RepositoryRef(Forge("forgejo", "https://other.example"), "team/Math"), 41),
        ):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                replace(self.request, members=(cc.Member(17, "f" * 40, identity),)).check_policy(self.policy)

    def test_legacy_policy_and_implicit_members_do_not_enable_mixed_authority(self):
        legacy = policy()
        with self.assertRaises(ValueError):
            replace(self.request, policy_digest=legacy.digest).check_policy(legacy)
        with self.assertRaises(ValueError):
            replace(self.request, members=(cc.Member(17, "f" * 40),)).check_policy(self.policy)
        with self.assertRaises(ValueError):
            replace(self.request, members=(cc.Member(17, "f" * 40), self.request.members[1]))

    def test_mixed_candidates_always_require_full_lint_without_cross_origin_reads(self):
        client = unittest.mock.Mock()
        self.assertFalse(
            cw.native_lint_policy(self.request, self.policy, context(self.request, self.policy), client=client)
        )
        client.request.assert_not_called()

    def test_qualified_membership_and_policy_change_request_digest(self):
        one = replace(self.request, members=(self.request.members[0],))
        other = replace(self.request, members=(cc.Member(17, "f" * 40, self.github),))
        self.assertNotEqual(one.digest, other.digest)
        self.assertNotEqual(self.policy.digest, policy().digest)
        raw = cc.member_manifest_bytes(self.request.epoch, self.request.request_id, self.request.members)
        self.assertIn(b'"source"', raw)

    def test_policy_round_trip_rejects_duplicate_native_ids_and_empty_enrollment(self):
        self.assertEqual(
            cc.ExecutionPolicy.parse(self.policy.record(with_config=False), configuration=b"trusted configuration"),
            self.policy,
        )
        native = self.policy.source
        alias = replace(native, repository=RepositoryRef(native.repository.forge, "Team/math"))
        with self.assertRaises(ValueError):
            replace(self.policy, member_sources=(native, self.github, alias))
        with self.assertRaises(ValueError):
            replace(self.request, members=(cc.Member(17, "f" * 40, native), cc.Member(17, "e" * 40, alias)))
        row = policy().record(with_config=False)
        row["member_sources"] = []
        with self.assertRaises(ValueError):
            cc.ExecutionPolicy.parse(row, configuration=b"trusted configuration")

    def test_qualified_manifest_requires_enrollment_and_preserves_same_number_sources(self):
        members = self.request.members
        raw = cc.member_manifest_bytes(self.request.epoch, self.request.request_id, members)
        manifest = cc.MemberManifest("9" * 40, hashlib.sha256(raw).hexdigest(), len(members), len(raw))
        original = replace(self.request, member_manifest=manifest)
        incoming = cc.CandidateRequest.parse(original.record(), policy=self.policy)
        self.assertEqual(incoming.bind_members(raw, policy=self.policy).accepted_members, members)
        with self.assertRaises(ValueError):
            incoming.bind_members(raw)
        with self.assertRaises(ValueError):
            incoming.bind_members(raw, policy=replace(self.policy, member_sources=(self.policy.source,)))
        with self.assertRaises(ValueError):
            cc.member_manifest_bytes(self.request.epoch, self.request.request_id, (members[0], cc.Member(18, "e" * 40)))
        legacy_members = (cc.Member(17, "f" * 40),)
        legacy_raw = cc.member_manifest_bytes(self.request.epoch, self.request.request_id, legacy_members)
        legacy_manifest = cc.MemberManifest("9" * 40, hashlib.sha256(legacy_raw).hexdigest(), 1, len(legacy_raw))
        unbound = replace(incoming, member_manifest=legacy_manifest)
        with self.assertRaises(ValueError):
            unbound.bind_members(legacy_raw, policy=self.policy)


if __name__ == "__main__":
    unittest.main()
