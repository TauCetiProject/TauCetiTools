"""Qualified source candidates executed by trusted GitHub workflows.

The request describes data to build, never a fetch URL, executable or credential
selector. Policy and workflow context come independently from trusted CI code.
An execution result proves a build; it does not grant source approval or merge
authority. Callers must still check fresh source authority and compare-and-swap
the exact base before promotion.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass

from .forge import GITHUB, Forge, RepositoryRef

SCHEMA = "tauceti-ci.source-candidate/v2"
EVENT = "tauceti-source-candidate-v2"
MAX_REQUEST_BYTES = 60 * 1024
MAX_MEMBERS = 128
MAX_RESULT_BYTES = MAX_REQUEST_BYTES + 16 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
RESULT_FILE = "source-candidate-result.json"
VERDICTS = frozenset({"build", "scope", "bump_guard", "publication"})


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"invalid {label} fields")
    return value


def _hex(value, length, label):
    if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None or not int(value, 16):
        raise ValueError(f"invalid {label}")
    return value


def _positive(value, label):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError(f"invalid {label}")
    return value


def _workflow(value):
    if not isinstance(value, str) or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", value):
        raise ValueError("invalid workflow path")
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_json(raw, *, max_bytes=MAX_REQUEST_BYTES):
    if not isinstance(raw, bytes) or len(raw) > max_bytes:
        raise ValueError("candidate JSON exceeds its byte bound")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate candidate JSON field")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("invalid candidate JSON number")

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (UnicodeError, RecursionError) as error:
        raise ValueError("invalid candidate JSON") from error


@dataclass(frozen=True)
class RepositoryIdentity:
    repository: RepositoryRef
    native_id: int

    def __post_init__(self):
        if not isinstance(self.repository, RepositoryRef) or not isinstance(self.repository.forge, Forge):
            raise ValueError("invalid qualified repository")
        _positive(self.native_id, "native repository ID")

    @classmethod
    def parse(cls, value):
        _object(value, {"forge", "name", "id"}, "repository identity")
        forge = _object(value["forge"], {"kind", "url"}, "forge")
        if not all(isinstance(forge[key], str) for key in forge):
            raise ValueError("invalid forge identity")
        return cls(RepositoryRef(Forge(forge["kind"], forge["url"]), value["name"]), value["id"])

    def record(self):
        return {
            "forge": {"kind": self.repository.forge.kind, "url": self.repository.forge.url},
            "name": self.repository.name,
            "id": self.native_id,
        }


@dataclass(frozen=True)
class ExecutionPolicy:
    source: RepositoryIdentity
    execution: RepositoryIdentity
    credential_origin: Forge
    workflow: str
    harness: RepositoryIdentity
    harness_revision: str
    base_branch: str
    execution_branch: str
    require_publication: bool
    config_digest: str

    def __post_init__(self):
        if not all(isinstance(ref, RepositoryIdentity) for ref in (self.source, self.execution, self.harness)):
            raise ValueError("invalid policy repositories")
        if self.source.repository.forge.kind != "forgejo":
            raise ValueError("source-candidate execution requires a Forgejo source")
        if self.execution.repository.forge != GITHUB or self.harness.repository.forge != GITHUB:
            raise ValueError("execution and harness must use GitHub")
        if self.credential_origin != self.source.repository.forge:
            raise ValueError("source credential origin differs from policy")
        _workflow(self.workflow)
        _hex(self.harness_revision, 40, "harness revision")
        _hex(self.config_digest, 64, "project configuration digest")
        for branch in (self.base_branch, self.execution_branch):
            if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", branch):
                raise ValueError("invalid policy branch")
        if type(self.require_publication) is not bool:
            raise ValueError("invalid publication policy")

    @classmethod
    def parse(cls, value, *, configuration):
        _object(
            value,
            {
                "schema",
                "source",
                "execution",
                "credential_origin",
                "workflow",
                "harness",
                "harness_revision",
                "base_branch",
                "execution_branch",
                "require_publication",
            },
            "execution policy",
        )
        if value["schema"] != SCHEMA or not isinstance(configuration, bytes):
            raise ValueError("invalid execution policy schema or configuration")
        origin = _object(value["credential_origin"], {"kind", "url"}, "credential origin")
        if not all(isinstance(origin[key], str) for key in origin):
            raise ValueError("invalid credential origin")
        return cls(
            RepositoryIdentity.parse(value["source"]),
            RepositoryIdentity.parse(value["execution"]),
            Forge(origin["kind"], origin["url"]),
            value["workflow"],
            RepositoryIdentity.parse(value["harness"]),
            value["harness_revision"],
            value["base_branch"],
            value["execution_branch"],
            value["require_publication"],
            hashlib.sha256(configuration).hexdigest(),
        )

    def record(self, *, with_config=True):
        value = {
            "schema": SCHEMA,
            "source": self.source.record(),
            "execution": self.execution.record(),
            "credential_origin": {"kind": self.credential_origin.kind, "url": self.credential_origin.url},
            "workflow": self.workflow,
            "harness": self.harness.record(),
            "harness_revision": self.harness_revision,
            "base_branch": self.base_branch,
            "execution_branch": self.execution_branch,
            "require_publication": self.require_publication,
        }
        if with_config:
            value["config_digest"] = self.config_digest
        return value

    @property
    def digest(self):
        return digest(self.record())


@dataclass(frozen=True)
class Member:
    pr: int
    head: str

    def __post_init__(self):
        _positive(self.pr, "source PR number")
        _hex(self.head, 40, "accepted member head")

    @classmethod
    def parse(cls, value):
        _object(value, {"pr", "head"}, "accepted member")
        return cls(value["pr"], value["head"])

    def record(self):
        return {"pr": self.pr, "head": self.head}


@dataclass(frozen=True)
class CandidateRequest:
    request_id: str
    epoch: str
    policy_digest: str
    source: RepositoryIdentity
    execution: RepositoryIdentity
    workflow_revision: str
    harness_revision: str
    config_digest: str
    batch_id: int
    batch_version: int
    head: str
    base: str
    tree: str
    members: tuple[Member, ...]

    def __post_init__(self):
        for value, label in ((self.request_id, "request ID"), (self.epoch, "queue epoch")):
            _hex(value, 32, label)
        for value, label in ((self.policy_digest, "policy digest"), (self.config_digest, "configuration digest")):
            _hex(value, 64, label)
        for value, label in (
            (self.workflow_revision, "workflow revision"),
            (self.harness_revision, "harness revision"),
            (self.head, "candidate head"),
            (self.base, "candidate base"),
            (self.tree, "candidate tree"),
        ):
            _hex(value, 40, label)
        if not isinstance(self.source, RepositoryIdentity) or not isinstance(self.execution, RepositoryIdentity):
            raise ValueError("invalid request repositories")
        _positive(self.batch_id, "batch ID")
        _positive(self.batch_version, "batch version")
        if self.head == self.base:
            raise ValueError("candidate must advance its exact base")
        if (
            not isinstance(self.members, tuple)
            or not 1 <= len(self.members) <= MAX_MEMBERS
            or not all(isinstance(m, Member) for m in self.members)
        ):
            raise ValueError("invalid accepted members")
        if len({m.pr for m in self.members}) != len(self.members):
            raise ValueError("duplicate source PR in candidate")

    @property
    def retained_ref(self):
        return f"refs/heads/tauceti-ci/{self.epoch}/{self.request_id}/candidate"

    def member_ref(self, member):
        if member not in self.members:
            raise ValueError("member does not belong to candidate")
        return f"refs/heads/tauceti-ci/{self.epoch}/{self.request_id}/member-{member.pr}"

    @classmethod
    def parse(cls, value, *, policy):
        _object(
            value,
            {
                "schema",
                "request_id",
                "epoch",
                "policy_digest",
                "source",
                "execution",
                "workflow_revision",
                "harness_revision",
                "config_digest",
                "batch_id",
                "batch_version",
                "candidate",
                "members",
            },
            "candidate request",
        )
        if value["schema"] != SCHEMA or not isinstance(value["members"], list):
            raise ValueError("invalid candidate request schema")
        if len(canonical(value)) > MAX_REQUEST_BYTES or len(value["members"]) > MAX_MEMBERS:
            raise ValueError("candidate request exceeds its bounds")
        candidate = _object(value["candidate"], {"head", "base", "tree", "retained_ref"}, "candidate")
        request = cls(
            value["request_id"],
            value["epoch"],
            value["policy_digest"],
            RepositoryIdentity.parse(value["source"]),
            RepositoryIdentity.parse(value["execution"]),
            value["workflow_revision"],
            value["harness_revision"],
            value["config_digest"],
            value["batch_id"],
            value["batch_version"],
            candidate["head"],
            candidate["base"],
            candidate["tree"],
            tuple(Member.parse(m) for m in value["members"]),
        )
        request.check_policy(policy)
        if candidate["retained_ref"] != request.retained_ref:
            raise ValueError("candidate retention ref differs from request identity")
        return request

    def check_policy(self, policy):
        if not isinstance(policy, ExecutionPolicy) or (
            self.policy_digest != policy.digest
            or self.source != policy.source
            or self.execution != policy.execution
            or self.harness_revision != policy.harness_revision
            or self.config_digest != policy.config_digest
        ):
            raise ValueError("candidate request differs from trusted policy")

    def record(self):
        return {
            "schema": SCHEMA,
            "request_id": self.request_id,
            "epoch": self.epoch,
            "policy_digest": self.policy_digest,
            "source": self.source.record(),
            "execution": self.execution.record(),
            "workflow_revision": self.workflow_revision,
            "harness_revision": self.harness_revision,
            "config_digest": self.config_digest,
            "batch_id": self.batch_id,
            "batch_version": self.batch_version,
            "candidate": {"head": self.head, "base": self.base, "tree": self.tree, "retained_ref": self.retained_ref},
            "members": [member.record() for member in self.members],
        }

    @property
    def digest(self):
        return digest(self.record())

    def dispatch(self):
        return {"event_type": EVENT, "client_payload": {"request": self.record()}}


def check_workflow_context(request, policy, context, *, harness_file=".github/workflows/pr-build.yml"):
    """Check runner-supplied caller/reusable identities before source credentials are used."""
    request.check_policy(policy)
    expected = {
        "event_name": "repository_dispatch",
        "action": EVENT,
        "repository": policy.execution.repository.name,
        "repository_id": str(policy.execution.native_id),
        "workflow_sha": request.workflow_revision,
        "workflow_ref": f"{policy.execution.repository.name}/{policy.workflow}@refs/heads/{policy.execution_branch}",
        "harness_repository": policy.harness.repository.name,
        "harness_sha": policy.harness_revision,
        "harness_file": harness_file,
    }
    if not isinstance(context, dict) or any(context.get(key) != value for key, value in expected.items()):
        raise ValueError("candidate execution context differs from trusted workflow")
    _positive(context.get("run_id"), "workflow run ID")
    _positive(context.get("attempt"), "workflow attempt")


def check_run_metadata(request, policy, run, *, run_id, attempt, require_completed=True):
    """Check fresh authenticated GitHub run metadata, independently of artifact claims."""
    request.check_policy(policy)
    _positive(run_id, "workflow run ID")
    _positive(attempt, "workflow attempt")
    if not isinstance(run, dict) or any(
        run.get(key) != value
        for key, value in {
            "id": run_id,
            "run_attempt": attempt,
            "head_sha": request.workflow_revision,
            "event": "repository_dispatch",
        }.items()
    ):
        raise ValueError("result does not belong to the expected completed workflow attempt")
    if run.get("status") not in ({"completed"} if require_completed else {"queued", "in_progress", "completed"}):
        raise ValueError("workflow execution is not in an expected state")
    if (
        type(run.get("id")) is not int
        or type(run.get("run_attempt")) is not int
        or run.get("path")
        not in {
            policy.workflow,
            f"{policy.workflow}@{policy.execution_branch}",
            f"{policy.workflow}@refs/heads/{policy.execution_branch}",
        }
    ):
        raise ValueError("result belongs to a different workflow path")
    repository = run.get("repository")
    if (
        not isinstance(repository, dict)
        or type(repository.get("id")) is not int
        or repository["id"] != policy.execution.native_id
        or repository.get("full_name") != policy.execution.repository.name
    ):
        raise ValueError("result belongs to a different execution repository")
    required = f"{policy.harness.repository.name}/.github/workflows/pr-build.yml@{policy.harness_revision}"
    workflows = run.get("referenced_workflows")
    if not isinstance(workflows, list) or not any(
        isinstance(w, dict) and w.get("path") == required and w.get("sha") == policy.harness_revision for w in workflows
    ):
        raise ValueError("result did not execute the pinned reusable build workflow")


def artifact_name(request, run_id, attempt):
    _positive(run_id, "workflow run ID")
    _positive(attempt, "workflow attempt")
    return f"source-candidate-{request.request_id}-{run_id}-{attempt}"


def make_result(request, policy, context, *, tested_commit, tested_tree, verdicts):
    """Called by the isolated trusted finalizer, with trusted job/step evidence."""
    check_workflow_context(request, policy, context)
    _object(verdicts, VERDICTS, "candidate verdicts")
    for key, value in verdicts.items():
        allowed = {"success", "failure", "infrastructure"}
        if key == "publication" and not policy.require_publication:
            allowed.add("not_required")
        if not isinstance(value, str) or value not in allowed:
            raise ValueError("invalid candidate verdict")
    if tested_commit is None and tested_tree is None:
        if verdicts["build"] != "infrastructure" or verdicts["publication"] == "success":
            raise ValueError("missing tested identity cannot produce a successful execution")
        tested = None
    elif tested_commit == request.head and tested_tree == request.tree:
        tested = {"commit": tested_commit, "tree": tested_tree, "base": request.base}
    else:
        raise ValueError("tested candidate differs from the requested commit and tree")
    return {
        "schema": SCHEMA,
        "request": request.record(),
        "request_digest": request.digest,
        "producer": {
            "repository": policy.execution.record(),
            "run_id": context["run_id"],
            "attempt": context["attempt"],
            "workflow": policy.workflow,
            "workflow_revision": request.workflow_revision,
            "harness": policy.harness.record(),
            "harness_revision": policy.harness_revision,
        },
        "tested": tested,
        "verdicts": dict(verdicts),
    }


def verify_result(request, policy, result, run, *, run_id, attempt):
    """An artifact's own producer claims are insufficient without fresh run metadata."""
    check_run_metadata(request, policy, run, run_id=run_id, attempt=attempt)
    _object(result, {"schema", "request", "request_digest", "producer", "tested", "verdicts"}, "candidate result")
    if result["schema"] != SCHEMA or result["request_digest"] != request.digest:
        raise ValueError("result answers a different candidate request")
    returned = CandidateRequest.parse(result["request"], policy=policy)
    if returned != request:
        raise ValueError("result changed the candidate request")
    expected_producer = {
        "repository": policy.execution.record(),
        "run_id": run_id,
        "attempt": attempt,
        "workflow": policy.workflow,
        "workflow_revision": request.workflow_revision,
        "harness": policy.harness.record(),
        "harness_revision": policy.harness_revision,
    }
    producer = _object(result["producer"], expected_producer, "result producer")
    if (
        RepositoryIdentity.parse(producer["repository"]) != policy.execution
        or RepositoryIdentity.parse(producer["harness"]) != policy.harness
    ):
        raise ValueError("invalid producer repositories")
    if producer != expected_producer or type(producer["run_id"]) is not int or type(producer["attempt"]) is not int:
        raise ValueError("result producer differs from the verified workflow attempt")
    verdicts = _object(result["verdicts"], VERDICTS, "candidate verdicts")
    for key, value in verdicts.items():
        allowed = {"success", "failure", "infrastructure"}
        if key == "publication" and not policy.require_publication:
            allowed.add("not_required")
        if not isinstance(value, str) or value not in allowed:
            raise ValueError("invalid result verdict")
    if result["tested"] is None:
        if verdicts["build"] != "infrastructure" or verdicts["publication"] == "success":
            raise ValueError("missing tested identity cannot confirm an execution")
    elif _object(result["tested"], {"commit", "tree", "base"}, "tested candidate") != {
        "commit": request.head,
        "tree": request.tree,
        "base": request.base,
    }:
        raise ValueError("result tested another candidate")
    passed = all(value in {"success", "not_required"} for value in verdicts.values())
    if run.get("conclusion") not in {"success", "failure"} or (passed and run["conclusion"] != "success"):
        raise ValueError("result is not confirmed by the completed workflow conclusion")
    return dict(verdicts)


def verify_artifact(request, policy, metadata, archive, run, *, run_id, attempt):
    """Verify an immutable GitHub artifact without extracting any archive paths."""
    check_run_metadata(request, policy, run, run_id=run_id, attempt=attempt)
    if not isinstance(metadata, dict) or not isinstance(archive, bytes) or not 0 < len(archive) <= MAX_ARTIFACT_BYTES:
        raise ValueError("invalid candidate result artifact")
    _positive(metadata.get("id"), "artifact ID")
    size = metadata.get("size_in_bytes")
    if type(size) is not int or not 0 < size <= MAX_ARTIFACT_BYTES or metadata.get("expired") is not False:
        raise ValueError("unavailable candidate result artifact")
    if metadata.get("name") != artifact_name(request, run_id, attempt):
        raise ValueError("artifact answers a different request or attempt")
    if metadata.get("digest") != "sha256:" + hashlib.sha256(archive).hexdigest():
        raise ValueError("artifact digest differs from authenticated GitHub metadata")
    owner = metadata.get("workflow_run")
    if (
        not isinstance(owner, dict)
        or owner.get("id") != run_id
        or type(owner.get("id")) is not int
        or owner.get("repository_id") != policy.execution.native_id
        or type(owner.get("repository_id")) is not int
        or owner.get("head_sha") != request.workflow_revision
    ):
        raise ValueError("artifact belongs to another workflow execution")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entries = bundle.infolist()
            if (
                len(entries) != 1
                or entries[0].filename != RESULT_FILE
                or entries[0].is_dir()
                or entries[0].flag_bits & 1
                or entries[0].file_size > MAX_RESULT_BYTES
            ):
                raise ValueError("invalid candidate result archive entries")
            with bundle.open(entries[0]) as stream:
                raw = stream.read(MAX_RESULT_BYTES + 1)
            result = read_json(raw, max_bytes=MAX_RESULT_BYTES)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise ValueError("invalid candidate result archive") from error
    return verify_result(request, policy, result, run, run_id=run_id, attempt=attempt)
