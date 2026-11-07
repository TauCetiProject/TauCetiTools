import copy
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import patch

import yaml

from tauceti_tools import ci_candidate as cc
from tauceti_tools import ci_candidate_workflow as cw
from tauceti_tools.assets.scripts import github_api
from tauceti_tools.ci_candidate_evidence import EvidenceUnavailable, GitHubEvidence
from tauceti_tools.forge import GITHUB
from tauceti_tools.project import ConfigError, parse
from tauceti_tools.upstreams import ResolutionUnavailable
from tests import test_ci_candidate as fixtures

context, policy, request, run = fixtures.context, fixtures.policy, fixtures.request, fixtures.run

CONFIG = b"""schema_version=2
[project]
branch="pilot"
[forges.native]
kind="forgejo"
url="https://source.example"
[repositories]
content={forge="native",name="team/Math"}
roadmap={forge="native",name="team/Roadmap"}
[lean]
roots=["Math"]
[ci]
backend="github-actions"
repository="team/Execution"
[[upstreams]]
package="mathlib"
repo="leanprover-community/mathlib4"
branch="master"
"""


class Workflow(unittest.TestCase):
    def setUp(self):
        # All incomplete-evidence waits in these tests advance a bounded fake
        # clock; a missing field must not turn a regression into a wall-time wait.
        self.elapsed = 0
        original_publication = cw.publication_verdict
        publication_patch = patch.object(
            cw, "publication_verdict", partial(original_publication, clock=lambda: self.elapsed, sleep=self.advance)
        )
        publication_patch.start()
        self.addCleanup(publication_patch.stop)
        self.policy = cc.ExecutionPolicy.parse(policy().record(with_config=False), configuration=CONFIG)
        self.request = request(self.policy)
        self.context = context(self.request, self.policy)
        self.evidence = {
            "tested_commit": self.request.head,
            "tested_tree": self.request.tree,
            "build_state": "success",
            "scope": "success",
            "bump_guard": "success",
            "publication_staged": "true",
            "scope_complete": "1",
            "scope_rejected": "",
            "pins_known": "1",
            "bump_complete": "1",
            "size_complete": "1",
            "infrastructure": "",
            "build_outcome": "success",
            "job_result": "success",
        }

    def advance(self, seconds):
        self.elapsed += seconds

    def test_source_build_bootstrap_uses_qualified_settings_without_enabling_legacy_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = root / "tauceti.toml"
            conf.write_bytes(CONFIG)
            env = cw.bootstrap(self.request, self.policy, self.context, conf, root / "scripts")
            self.assertEqual(json.loads(env["TAUCETI_SOURCE_REPOSITORY"]), self.policy.source.record())
            self.assertEqual(json.loads(env["TAUCETI_CI_EXECUTION_REPOSITORY"]), self.policy.execution.record())
            self.assertNotIn("TAUCETI_REPO", env)
            self.assertEqual(env["TAUCETI_GITHUB_STATE_URL"], "")
            self.assertEqual(env["TAUCETI_BORS_URL"], "")
            self.assertEqual(env["TAUCETI_LEAN_ROOT"], "Math")
            self.assertEqual(env["TAUCETI_CACHE_REPOSITORY"], "team/Execution")
            self.assertIn('--repo "${TAUCETI_CACHE_REPOSITORY:-', (root / "scripts/lake-cache-get.sh").read_text())
            (root / "bin").mkdir()
            fake_lake = root / "bin/lake"
            fake_lake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$CAPTURE_CACHE_ARGS"\n')
            fake_lake.chmod(0o755)
            (root / "candidate").mkdir()
            cache_env = dict(
                os.environ,
                **env,
                PATH=str(root / "bin") + os.pathsep + os.environ["PATH"],
                RUNNER_TEMP=str(root),
                CAPTURE_CACHE_ARGS=str(root / "cache-args"),
            )
            child = subprocess.run(
                ["bash", str(root / "scripts/lake-cache-get.sh"), str(root / "candidate")],
                env=cache_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            arguments = (root / "cache-args").read_text().splitlines()
            self.assertEqual(arguments[arguments.index("--repo") + 1], self.policy.execution.repository.name)
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from tauceti_tools.assets.scripts.lake_cache_probe import DEFAULT_REPOSITORY; print(DEFAULT_REPOSITORY)",
                ],
                env=cache_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(child.stdout.strip(), self.policy.execution.repository.name)
            self.assertIn('module_re="^Math(', (root / "scripts/lint-scope.sh").read_text())
            project = parse(CONFIG)
            self.assertEqual(project.roots, ["Math"])
            self.assertEqual(project.roadmap_dir, "Roadmap")
            self.assertEqual(project.github_upstreams[0]["repo"], "leanprover-community/mathlib4")
            with self.assertRaisesRegex(ConfigError, "GitHub-only"):
                project.environment()

    def test_bootstrap_rejects_project_source_or_execution_mismatch(self):
        for old, new in ((b"team/Math", b"other/Math"), (b"team/Execution", b"other/Execution")):
            with self.subTest(old=old), tempfile.TemporaryDirectory() as directory:
                conf = Path(directory) / "tauceti.toml"
                conf.write_bytes(CONFIG.replace(old, new))
                with self.assertRaises(ValueError):
                    cw.bootstrap(self.request, self.policy, self.context, conf, Path(directory) / "scripts")

    def publisher_reader(self, *, publisher_changes=None, builder_changes=None, run_changes=None, jobs_changes=None):
        metadata = run(self.request, self.policy)
        metadata.update(status="in_progress", conclusion=None, run_started_at="2026-11-07T20:37:33Z")
        metadata.update(run_changes or {})
        publisher = {
            "name": "validate / publish-merge-group-cache / publish",
            "status": "completed",
            "conclusion": "success",
            "runner_id": 9002,
            "runner_group_id": 0,
            "runner_group_name": "GitHub Actions",
            "labels": ["ubuntu-24.04"],
            "started_at": "2026-11-07T20:40:00Z",
            "completed_at": "2026-11-07T20:41:00Z",
            "steps": [
                {
                    "name": "Confirm the exact revision map on the public endpoint",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-11-07T20:40:30Z",
                    "completed_at": "2026-11-07T20:40:40Z",
                }
            ],
        }
        publisher.update(publisher_changes or {})
        builder = {
            "name": "validate / sandboxed-build",
            "runner_id": 9001,
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-11-07T20:37:40Z",
            "completed_at": "2026-11-07T20:39:50Z",
        }
        builder.update(builder_changes or {})
        jobs = {"total_count": 2, "jobs": [builder, publisher]}
        jobs.update(jobs_changes or {})

        class Reader:
            def get(self, path):
                return copy.deepcopy(jobs if path.endswith("jobs?per_page=100") else metadata)

        return Reader()

    def test_finalizer_requires_the_actual_isolated_publisher_confirmation(self):
        result = cw.finalize(self.request, self.policy, self.context, self.evidence, reader=self.publisher_reader())
        self.assertEqual(result["verdicts"], dict.fromkeys(cc.VERDICTS, "success"))
        for changes in (
            {"runner_id": 9001},
            {"runner_group_id": 1},
            {"labels": ["self-hosted"]},
            {"steps": []},
            {"conclusion": "failure"},
            {"runner_id": 0},
        ):
            with self.subTest(changes=changes):
                result = cw.finalize(
                    self.request,
                    self.policy,
                    self.context,
                    self.evidence,
                    reader=self.publisher_reader(publisher_changes=changes),
                )
                self.assertEqual(result["verdicts"]["publication"], "infrastructure")

    def test_publication_cannot_reuse_an_older_attempt_or_partial_jobs_response(self):
        for reader in (
            self.publisher_reader(run_changes={"run_attempt": 3}),
            self.publisher_reader(jobs_changes={"total_count": 3}),
        ):
            result = cw.finalize(self.request, self.policy, self.context, self.evidence, reader=reader)
            self.assertEqual(result["verdicts"]["publication"], "infrastructure")

    def test_publication_rejects_relabelled_jobs_with_old_execution_timestamps(self):
        # The live partial-rerun rehearsal returned new IDs/run_attempt=2 for
        # reused jobs whose steps and execution timestamps still came from 1.
        for builder_changes, publisher_changes in (
            (
                {"id": 128521820800, "run_attempt": 2, "started_at": "2026-11-07T20:28:34Z"},
                {},
            ),
            ({}, {"id": 128521820801, "run_attempt": 2, "started_at": "2026-11-07T20:33:00Z"}),
            ({"started_at": "2026-11-07T20:37:33Z"}, {}),
        ):
            with self.subTest(builder=builder_changes, publisher=publisher_changes):
                result = cw.finalize(
                    self.request,
                    self.policy,
                    self.context,
                    self.evidence,
                    reader=self.publisher_reader(builder_changes=builder_changes, publisher_changes=publisher_changes),
                )
                self.assertEqual(result["verdicts"]["publication"], "infrastructure")

    def test_publication_rejects_captured_reused_job_execution(self):
        observation = json.loads((Path(__file__).parent / "fixtures/source-ci-partial-rerun.json").read_bytes())
        reused = dict(observation["job"])
        self.assertEqual(reused["run_attempt"], self.context["attempt"])
        # Project the captured successful receipt job into the builder role;
        # execution timestamps/steps and the relabelled ID stay exactly as read.
        reused["name"] = "validate / sandboxed-build"
        result = cw.finalize(
            self.request,
            self.policy,
            self.context,
            self.evidence,
            reader=self.publisher_reader(
                builder_changes=reused, run_changes={"run_started_at": observation["run_started_at"]}
            ),
        )
        self.assertEqual(result["verdicts"]["publication"], "infrastructure")

    def test_publication_requires_complete_ordered_successful_execution_evidence(self):
        baseline = self.publisher_reader()
        path = "/actions/runs/101/attempts/2/jobs?per_page=100"
        good_step = baseline.get(path)["jobs"][1]["steps"][0]
        cases = [
            {"run_changes": {"run_started_at": value}}
            for value in (None, "", True, "2026-11-07", "2026-02-30T00:00:00Z", "2026-11-07T20:37:33+25:00")
        ]
        cases += [
            {"builder_changes": {"completed_at": None}},
            {"builder_changes": {"started_at": None}},
            {"builder_changes": {"status": "in_progress"}},
            {"builder_changes": {"conclusion": "failure"}},
            {"builder_changes": {"completed_at": "2026-11-07T20:37:39Z"}},
            {"publisher_changes": {"started_at": None}},
            {"publisher_changes": {"completed_at": None}},
            {"publisher_changes": {"started_at": "bad timestamp"}},
        ]
        cases += [{"builder_changes": {"completed_at": "2026-11-07T20:40:01Z"}}]
        cases += [{"publisher_changes": {"steps": value}} for value in (None, [], [good_step, good_step])]
        cases += [
            {"publisher_changes": {"steps": [dict(good_step, **change)]}}
            for change in (
                {"status": "in_progress"},
                {"conclusion": "failure"},
                {"started_at": "2026-11-07T20:33:10Z"},
                {"completed_at": "2026-11-07T20:41:01Z"},
                {"completed_at": "2026-11-07T20:40:29Z"},
                {"completed_at": None},
            )
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                result = cw.finalize(
                    self.request,
                    self.policy,
                    self.context,
                    self.evidence,
                    reader=self.publisher_reader(**changes),
                )
                self.assertEqual(result["verdicts"]["publication"], "infrastructure")

    def test_publication_normalizes_documented_fractional_offset_step_times_and_boundaries(self):
        step = self.publisher_reader().get("/actions/runs/101/attempts/2/jobs?per_page=100")["jobs"][1]["steps"][0]
        step.update(started_at="2026-11-07T12:40:00.000-08:00", completed_at="2026-11-07T12:41:00.999-08:00")
        reader = self.publisher_reader(
            builder_changes={"started_at": "2026-11-07T20:37:34Z", "completed_at": "2026-11-07T20:40:00Z"},
            publisher_changes={"steps": [None, step]},
        )
        result = cw.finalize(self.request, self.policy, self.context, self.evidence, reader=reader)
        self.assertEqual(result["verdicts"]["publication"], "success")

    def test_publication_retries_incomplete_job_evidence_then_accepts_complete_evidence(self):
        complete = self.publisher_reader()
        incomplete = self.publisher_reader(publisher_changes={"steps": None})
        reader = self.publisher_reader()
        reader.get = lambda path: (incomplete if self.elapsed < 30 else complete).get(path)
        result = cw.finalize(self.request, self.policy, self.context, self.evidence, reader=reader)
        self.assertEqual(result["verdicts"]["publication"], "success")
        self.assertEqual(self.elapsed, 30)

    def test_publication_incomplete_evidence_wait_is_bounded(self):
        result = cw.finalize(
            self.request,
            self.policy,
            self.context,
            self.evidence,
            reader=self.publisher_reader(publisher_changes={"steps": []}),
        )
        self.assertEqual(result["verdicts"]["publication"], "infrastructure")
        self.assertEqual(self.elapsed, 1200)

    def test_publication_observation_retries_a_transient_api_failure_within_the_deadline(self):
        reader = self.publisher_reader()
        original = reader.get
        reads, elapsed = [], [0]

        def transient(path):
            reads.append(path)
            if len(reads) == 1:
                raise EvidenceUnavailable("transient API outage")
            return original(path)

        reader.get = transient
        verdict = cw.publication_verdict(
            self.request,
            self.policy,
            self.context,
            reader,
            clock=lambda: elapsed[0],
            sleep=lambda delay: elapsed.__setitem__(0, elapsed[0] + delay),
        )
        self.assertEqual(verdict, "success")
        self.assertEqual(elapsed, [30])

    def test_no_tested_identity_is_an_infrastructure_result_without_a_publication_claim(self):
        evidence = dict(self.evidence, tested_commit="", tested_tree="", build_state="failure", job_result="failure")
        result = cw.finalize(self.request, self.policy, self.context, evidence)
        self.assertIsNone(result["tested"])
        self.assertEqual(result["verdicts"]["build"], "infrastructure")
        metadata = dict(run(self.request, self.policy), conclusion="failure")
        verdicts = cc.verify_result(self.request, self.policy, result, metadata, run_id=101, attempt=2)
        self.assertEqual(verdicts["publication"], "infrastructure")

    def test_required_publication_cannot_be_downgraded_by_job_outputs(self):
        result = cw.finalize(self.request, self.policy, self.context, dict(self.evidence, publication_staged="false"))
        self.assertEqual(result["verdicts"]["publication"], "infrastructure")

    def test_uncompleted_scope_pin_and_member_size_checks_are_infrastructure(self):
        for field, verdict in (
            ("scope_complete", "scope"),
            ("size_complete", "scope"),
            ("pins_known", "bump_guard"),
            ("bump_complete", "bump_guard"),
        ):
            with self.subTest(field=field):
                result = cw.finalize(
                    self.request,
                    self.policy,
                    self.context,
                    dict(self.evidence, **{field: "", verdict if verdict != "bump_guard" else "bump_guard": "failure"}),
                    reader=self.publisher_reader(),
                )
                self.assertEqual(result["verdicts"][verdict], "infrastructure")

    def test_decided_scope_rejection_survives_later_incomplete_checks(self):
        result = cw.finalize(
            self.request,
            self.policy,
            self.context,
            dict(self.evidence, scope="failure", scope_rejected="1", size_complete="", build_outcome="skipped"),
        )
        self.assertEqual(result["verdicts"]["scope"], "failure")
        self.assertEqual(cc.classify_verdicts(result["verdicts"]), "policy_failure")

    def test_explicitly_unrequired_publication_remains_bound_to_policy(self):
        config = replace(self.policy, require_publication=False)
        req = request(config)
        result = cw.finalize(req, config, context(req, config), self.evidence)
        self.assertEqual(result["verdicts"]["publication"], "not_required")


class Evidence(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CandidateContracts()
        fixture.setUp()
        self.fixture = fixture
        self.metadata, self.raw = fixture.archive()

    def reader(self, *, current_attempt=2):
        fixture, metadata, raw = self.fixture, self.metadata, self.raw

        class Reader(GitHubEvidence):
            def __init__(self):
                super().__init__(fixture.policy, token="execution-fixture-token", token_origin=GITHUB)
                self.reads = []
                self.run_reads = 0

            def _read(self, req, limit):
                self.reads.append(req.full_url)
                return json.dumps({"id": 43, "full_name": "team/Tools"}).encode()

            def get(self, path):
                self.reads.append(path)
                if path == "/actions/runs/101":
                    self.run_reads += 1
                    return dict(fixture.run, run_attempt=current_attempt if self.run_reads > 1 else 2)
                if path.startswith("/actions/runs/101/artifacts?"):
                    return {"total_count": 1, "artifacts": [metadata]}
                if path == "/actions/artifacts/202":
                    return metadata
                raise AssertionError(path)

            def artifact_bytes(self, artifact_id):
                if artifact_id != 202:
                    raise AssertionError(artifact_id)
                return raw

        return Reader()

    def test_evidence_reader_checks_authenticated_metadata_again_after_download(self):
        reader = self.reader()
        result = reader.result(self.fixture.request, run_id=101, attempt=2)
        self.assertEqual(result["kind"], "result")
        self.assertEqual(result["verdicts"], self.fixture.verdicts)
        self.assertEqual(reader.run_reads, 2)
        self.assertIn("/actions/artifacts/202", reader.reads)
        with self.assertRaises(EvidenceUnavailable):
            self.reader(current_attempt=3).result(self.fixture.request, run_id=101, attempt=2)

    def test_evidence_reader_rejects_partial_or_duplicate_artifacts(self):
        for listing in (
            {"total_count": 2, "artifacts": []},
            {"total_count": 2, "artifacts": [self.metadata, self.metadata]},
        ):
            reader = self.reader()
            original = reader.get
            reader.get = lambda path, original=original, listing=listing: (
                listing if "/artifacts?" in path else original(path)
            )
            with self.subTest(listing=listing), self.assertRaises(EvidenceUnavailable):
                reader.result(self.fixture.request, run_id=101, attempt=2)

    def test_github_token_is_removed_before_the_artifact_storage_redirect(self):
        calls = []
        raw = self.raw

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, size):
                return raw[:size]

        class Opener:
            def open(self, req, *, timeout):
                calls.append(req)
                if len(calls) == 1:
                    raise urllib.error.HTTPError(
                        req.full_url,
                        302,
                        "Found",
                        {"Location": "https://storage.example/signed?token=artifact"},
                        io.BytesIO(),
                    )
                return Response()

        reader = GitHubEvidence(self.fixture.policy, token="execution-token", token_origin=GITHUB, opener=Opener())
        self.assertEqual(reader.artifact_bytes(202), raw)
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer execution-token")
        self.assertIsNone(calls[1].get_header("Authorization"))
        self.assertNotIn("execution-token", calls[1].full_url)


class PinGuard(unittest.TestCase):
    def test_source_bump_policy_distinguishes_external_failure_from_rejected_pins(self):
        config = policy()
        req = request(config)
        cases = [
            (None, {"BUMP_VALIDATED": "1"}),
            (ConfigError("backwards pin"), {"BUMP_BAD": "1", "INFRA": "1"}),
            (subprocess.CalledProcessError(1, ["gh", "api"]), {"INFRA": "1"}),
            (ResolutionUnavailable("toolchain unavailable"), {"INFRA": "1"}),
        ]
        for error, expected in cases:
            with (
                self.subTest(error=error),
                patch.object(cw, "load", return_value=parse(CONFIG)),
                patch("tauceti_tools.upstreams.guard", side_effect=error),
            ):
                self.assertEqual(
                    cw.bump_policy(req, config, context(req, config), "trusted.toml", "base", "mergebase", "pr"),
                    expected,
                )

    def test_member_policy_cli_stops_the_build_after_persisting_rejection_evidence(self):
        config = policy()
        req = request(config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rejection = {"SOURCE_SIZE_POLICY_COMPLETE": "1", "TOO_LARGE": "1"}
            with (
                patch.object(cw, "load_request", return_value=(req, config)),
                patch.object(cw, "context_from_environment", return_value=context(req, config)),
                patch.object(cw, "member_size_policy", return_value=rejection),
            ):
                status = cw.main(
                    [
                        "member-policy",
                        "--policy",
                        "trusted-policy.json",
                        "--configuration",
                        "trusted.toml",
                        "--workspace",
                        str(root),
                        "--output",
                        str(root / "member-policy.json"),
                        "--github-env",
                        str(root / "environment"),
                    ]
                )
            self.assertEqual(status, 1)
            self.assertEqual(json.loads((root / "member-policy.json").read_text()), rejection)
            self.assertIn("TOO_LARGE=1\n", (root / "environment").read_text())

    def member_policy(self, *, added_lines=0, existing_lines=0, pin_changed=False, binary=False, orphan=False):
        config = policy()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = root / "writer"
            subprocess.run(["git", "init", "-q", str(writer)], check=True)
            env = dict(
                os.environ,
                GIT_AUTHOR_NAME="Fixture",
                GIT_AUTHOR_EMAIL="fixture@example.invalid",
                GIT_COMMITTER_NAME="Fixture",
                GIT_COMMITTER_EMAIL="fixture@example.invalid",
                GIT_CONFIG_GLOBAL="/dev/null",
                GIT_CONFIG_NOSYSTEM="1",
            )

            def git(*args):
                return (
                    subprocess.check_output(["git", "-C", str(writer), *args], env=env, stderr=subprocess.DEVNULL)
                    .decode()
                    .strip()
                )

            (writer / "Old.lean").write_text("old\n")
            (writer / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            if added_lines:
                (writer / "New.lean").write_text("new\n" * added_lines)
            if existing_lines:
                (writer / "Old.lean").write_text("old\n" + "new\n" * existing_lines)
            if pin_changed:
                (writer / "lean-toolchain").write_text("leanprover/lean4:v4.36.0\n")
            if binary:
                (writer / "Binary.lean").write_bytes(b"\0unreadable line statistics\n")
            git("add", ".")
            git("commit", "-qm", "candidate")
            head, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
            member_head = head
            if orphan:
                git("checkout", "--orphan", "unrelated", "-q")
                git("commit", "-qm", "unrelated root")
                member_head = git("rev-parse", "HEAD")
            workspace = root / "job"
            workspace.mkdir()
            subprocess.run(
                ["git", "clone", "-q", "--bare", str(writer), str(workspace / ".ci-source.git")], check=True, env=env
            )
            req = replace(request(config), head=head, base=base, tree=tree, members=(cc.Member(17, member_head),))
            return cw.member_size_policy(req, config, context(req, config), workspace)

    def test_retained_member_new_file_and_total_size_guards_use_real_local_objects(self):
        self.assertEqual(
            self.member_policy(added_lines=1001), {"SOURCE_SIZE_POLICY_COMPLETE": "1", "NEWFILE_TOO_LONG": "1"}
        )
        self.assertEqual(
            self.member_policy(existing_lines=2001), {"SOURCE_SIZE_POLICY_COMPLETE": "1", "TOO_LARGE": "1"}
        )
        self.assertEqual(
            self.member_policy(existing_lines=2001, pin_changed=True), {"SOURCE_SIZE_POLICY_COMPLETE": "1"}
        )
        self.assertEqual(self.member_policy(binary=True), {"SOURCE_SIZE_POLICY_COMPLETE": "1"})
        self.assertEqual(
            self.member_policy(added_lines=1, orphan=True),
            {"SOURCE_SIZE_POLICY_COMPLETE": "1", "SOURCE_MEMBER_POLICY_REJECTED": "1"},
        )

    def test_source_pin_changes_use_the_configured_upstream_guard_without_legacy_source_bindings(self):
        root_tools = Path(__file__).resolve().parents[1]
        script = root_tools / "tauceti_tools/assets/scripts/check-bump.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("base", "mergebase", "candidate"):
                (root / name).mkdir()
                (root / name / "lakefile.toml").write_text('name="Example"\n')
                (root / name / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
            (root / "candidate/lean-toolchain").write_text("leanprover/lean4:v4.36.0\n")
            package = root / "tauceti_tools"
            package.mkdir()
            (package / "__init__.py").touch()
            (package / "__main__.py").write_text(
                "import sys\nfrom pathlib import Path\nassert sys.argv[1:3]==['upstreams','guard']\nPath('native-pin-guard-called').touch()\n"
            )
            env = dict(
                os.environ,
                TAUCETI_CONF_SNAPSHOT=str(root / "trusted.toml"),
                TAUCETI_SOURCE_CANDIDATE="true",
                PYTHONPATH=str(root),
            )
            env.pop("TAUCETI_REPO", None)
            child = subprocess.run(
                ["bash", str(script), str(root / "base"), str(root / "mergebase"), str(root / "candidate")],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            self.assertTrue((root / "native-pin-guard-called").exists())


class NativeGuard(unittest.TestCase):
    def test_github_source_metadata_and_status_requests_are_refused_before_credentials(self):
        env = {
            "TAUCETI_SOURCE_CANDIDATE": "true",
            "TAUCETI_CI_GITHUB_READ_REPOSITORIES": json.dumps(
                ["team/Execution", "team/Tools", "leanprover-community/mathlib4"]
            ),
            "TAUCETI_CI_EXECUTION_REPOSITORY": json.dumps(policy().execution.record()),
        }
        with (
            patch.dict(os.environ, env),
            patch.object(github_api.GitHub, "_credential", side_effect=AssertionError("must not choose credentials")),
        ):
            for method, path in (
                ("GET", "repos/team/Execution/pulls/17"),
                ("POST", "repos/team/Execution/statuses/" + "a" * 40),
                ("GET", "repos/team/Math/pulls/17"),
                ("POST", "graphql"),
            ):
                with self.subTest(method=method, path=path), self.assertRaises(ValueError):
                    github_api.GitHub().request(method, path)

    def test_execution_read_is_explicitly_bound_to_github_despite_ambient_host(self):
        env = {
            "TAUCETI_SOURCE_CANDIDATE": "true",
            "GH_HOST": "source.example",
            "TAUCETI_CI_GITHUB_READ_REPOSITORIES": json.dumps(["team/Execution"]),
            "TAUCETI_CI_EXECUTION_REPOSITORY": json.dumps(policy().execution.record()),
        }
        response = subprocess.CompletedProcess([], 0, "HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n{}", "")
        with patch.dict(os.environ, env), patch.object(github_api.subprocess, "run", return_value=response) as child:
            self.assertEqual(github_api.GitHub().request("GET", "repos/team/Execution/actions/runs/101"), {})
        self.assertIn("--hostname", child.call_args.args[0])
        self.assertEqual(child.call_args.kwargs["env"]["GH_HOST"], "github.com")


class ShippedWorkflow(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.value = yaml.safe_load((self.root / ".github/workflows/pr-build.yml").read_text())

    def step(self, name, *, job="sandboxed-build"):
        return next(step for step in self.value["jobs"][job]["steps"] if step.get("name") == name)

    def test_source_identity_shell_executes_ingress_and_never_the_legacy_helpers(self):
        shell = self.step("Resolve PR head")["run"]
        # Non-source expressions are irrelevant in the selected branch. If the
        # source branch accidentally falls through, the failing stubs expose it.
        shell = re.sub(r"\$\{\{.*?\}\}", "legacy-unreachable", shell)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "tauceti_tools"
            package.mkdir()
            (package / "__init__.py").touch()
            (package / "ci_candidate_workflow.py").write_text(
                "import json,sys\nfrom pathlib import Path\n"
                "assert sys.argv[1]=='resolve'\n"
                "assert sys.argv[sys.argv.index('--policy')+1]=='gate/.tauceti/ci-source.json'\n"
                "assert sys.argv[sys.argv.index('--configuration')+1]=='gate/tauceti.toml'\n"
                "Path('source-called.json').write_text(json.dumps(sys.argv))\n"
            )
            env = dict(
                os.environ,
                SOURCE_CANDIDATE="true",
                RUNNER_TEMP=str(root),
                GITHUB_OUTPUT=str(root / "out"),
                GITHUB_ENV=str(root / "env"),
                PYTHONPATH=str(root),
            )
            child = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", shell], cwd=root, env=env, capture_output=True, text=True
            )
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            self.assertTrue((root / "source-called.json").is_file())

    def test_source_executable_pin_mode_is_a_decided_rejection_even_with_unchanged_blob(self):
        shell = self.step("Scope guard — allow ${{ inputs.lean_root }} sources and validated pins")["run"]
        shell = shell[shell.index("pins_changed=0") : shell.index('if [ "$pins_changed" = "1" ]')]
        shell = shell.replace("${{ steps.pr.outputs.mergebase }}", "base").replace(
            "${{ steps.pr.outputs.sha }}", "head"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gate/scripts").mkdir(parents=True)
            (root / "gate/scripts/ci_git.py").write_text(
                "import os,sys\n"
                "mode=os.environ['PIN_MODE'] if sys.argv[sys.argv.index('--commit')+1]=='head' else '100644'\n"
                "print(mode+' blob '+('a'*40))\n"
            )
            for mode in ("100644", "100755", "120000"):
                with self.subTest(mode=mode):
                    output = root / "env"
                    output.write_text("")
                    child = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", "pins_known=1\n" + shell],
                        cwd=root,
                        env=dict(os.environ, SOURCE_CANDIDATE="true", PIN_MODE=mode, GITHUB_ENV=str(output)),
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
                    if mode == "100644":
                        self.assertEqual(output.read_text(), "PINS_KNOWN=1\n")
                    else:
                        self.assertEqual(output.read_text(), "CONFIG_BAD=1\nINFRA=1\nCONFIG_BAD=1\nINFRA=1\n")

    def test_source_result_reporting_records_verdicts_without_github_status_writes(self):
        shell = self.step("Report build and bump-guard statuses to the PR head commit")["run"]
        start = shell.index("post_status() {")
        end = shell.index("\n          }", start) if "\n          }" in shell[start:] else shell.index("\n}", start)
        function = shell[start:end] + "\n}\npost_status scope success approved\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "outputs"
            child = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", function],
                cwd=root,
                env=dict(
                    os.environ,
                    SOURCE_CANDIDATE="true",
                    SCOPE_OK="1",
                    SOURCE_SIZE_POLICY_COMPLETE="1",
                    GITHUB_OUTPUT=str(output),
                ),
                capture_output=True,
                text=True,
            )
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            self.assertEqual(output.read_text(), "scope-state=success\n")

    def test_source_restricted_paths_cannot_inherit_a_legacy_bors_waiver(self):
        shell = self.step("Report build and bump-guard statuses to the PR head commit")["run"]
        values = {
            "env.SCOPE_OK": "1",
            "env.PINS_KNOWN": "1",
            "env.OUT_OF_SCOPE": "1",
            "env.IS_BATCH": "true",
            "steps.build.outcome": "success",
            "inputs.lean_root": "Math",
        }
        shell = re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda match: values.get(match[1], ""), shell)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gate/scripts").mkdir(parents=True)
            (root / "gate/scripts/prepare_pr_integration.py").touch()
            output = root / "outputs"
            env = dict(
                os.environ,
                SOURCE_CANDIDATE="true",
                MERGE_BACKEND="bors",
                IS_BATCH="true",
                OUT_OF_SCOPE="1",
                SCOPE_OK="1",
                SOURCE_SIZE_POLICY_COMPLETE="1",
                RAW_HEAD="a" * 40,
                CAPTURED_BASE="b" * 40,
                CANDIDATE_MODE="frozen-candidate",
                TESTED_SHA="a" * 40,
                TESTED_TREE="c" * 40,
                RUNNER_TEMP=str(root),
                GITHUB_OUTPUT=str(output),
            )
            child = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", shell], cwd=root, env=env, capture_output=True, text=True
            )
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            self.assertIn("scope-state=failure\n", output.read_text())

    def test_source_scope_reports_its_own_policy_independently_of_publication_outages(self):
        shell = self.step("Report build and bump-guard statuses to the PR head commit")["run"]
        start = shell.index("post_status() {")
        function = shell[start : shell.index("\n}", start)] + '\n}\npost_status scope failure "unrelated outage"\n'
        cases = [
            ({"INFRA": "1"}, "success"),
            ({"SCOPE_OK": ""}, "infrastructure"),
            ({"SOURCE_SIZE_POLICY_COMPLETE": ""}, "infrastructure"),
            ({"TOO_LARGE": "1"}, "failure"),
            ({"SOURCE_MEMBER_POLICY_REJECTED": "1"}, "failure"),
            ({"SOURCE_SCOPE_DIFF_UNKNOWN": "1"}, "infrastructure"),
            ({"SOURCE_EMPTY_DIFF": "1", "SOURCE_SIZE_POLICY_COMPLETE": ""}, "failure"),
        ]
        for flags, expected in cases:
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "output"
                env = dict(
                    os.environ,
                    SOURCE_CANDIDATE="true",
                    SCOPE_OK="1",
                    SOURCE_SIZE_POLICY_COMPLETE="1",
                    GITHUB_OUTPUT=str(output),
                )
                env.update(flags)
                child = subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", function], env=env, capture_output=True, text=True
                )
                self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
                self.assertEqual(
                    output.read_text(),
                    ("source-scope-rejected=1\n" if expected == "failure" else "") + f"scope-state={expected}\n",
                )

    def test_native_secret_is_only_in_explicit_source_read_steps(self):
        jobs = self.value["jobs"]
        for job in jobs.values():
            if "steps" not in job:
                continue
            for step in job["steps"]:
                serialized = json.dumps(step.get("env", {}))
                if "secrets.CI_SOURCE_READ_TOKEN" in serialized:
                    self.assertIn(
                        step["name"],
                        {
                            "Resolve PR head",
                            "Fetch complete immutable source histories",
                        },
                    )
                if step.get("id") == "build":
                    self.assertNotIn("CI_SOURCE_READ_TOKEN", serialized + step["run"])
        publisher = yaml.safe_load((self.root / ".github/workflows/publish-lake-cache.yml").read_text())
        steps = publisher["jobs"]["publish"]["steps"]
        self.assertTrue(
            next(s for s in steps if s.get("name") == "Check out the immutable toolchain pin")["if"].find(
                "!inputs.source_candidate_enabled"
            )
            >= 0
        )
        self.assertEqual(
            [s["name"] for s in steps if "secrets.CI_SOURCE_READ_TOKEN" in json.dumps(s.get("env", {}))],
            ["Read the source candidate's immutable toolchain pin"],
        )


if __name__ == "__main__":
    unittest.main()
