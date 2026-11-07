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
from dataclasses import dataclass, replace
from functools import cached_property

from .forge import GITHUB, Forge, RepositoryRef

SCHEMA = "tauceti-ci.source-candidate/v2"
EVENT = "tauceti-source-candidate-v2"
MAX_REQUEST_BYTES = 60 * 1024
MAX_MEMBERS = 128
MAX_RETAINED_MEMBERS = 8192
MAX_MEMBER_MANIFEST_BYTES = 1024 * 1024
MEMBER_MANIFEST_SCHEMA = "tauceti-ci.accepted-members/v1"
MEMBER_MANIFEST_FILE = "accepted-members.json"
MAX_RESULT_BYTES = MAX_REQUEST_BYTES + 16 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
RESULT_FILE = "source-candidate-result.json"
RECEIPT_SCHEMA = "tauceti-ci.source-candidate-request/v1"
RECEIPT_FILE = "source-candidate-request.json"
VERDICTS = frozenset({"build", "scope", "bump_guard", "publication"})


class EvidenceUnavailable(RuntimeError):
    """Execution evidence is transiently unavailable or superseded; no verdict is granted."""


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


def member_manifest_bytes(epoch, request_id, members):
    """Create data-only retained membership, bound to one persisted queue request."""
    _hex(epoch, 32, "queue epoch")
    _hex(request_id, 32, "request ID")
    if (
        not isinstance(members, tuple)
        or not 1 <= len(members) <= MAX_RETAINED_MEMBERS
        or not all(isinstance(member, Member) for member in members)
        or len({member.pr for member in members}) != len(members)
    ):
        raise ValueError("invalid retained members")
    raw = canonical(
        {
            "schema": MEMBER_MANIFEST_SCHEMA,
            "epoch": epoch,
            "request_id": request_id,
            "members": [member.record() for member in members],
        }
    )
    if len(raw) > MAX_MEMBER_MANIFEST_BYTES:
        raise ValueError("retained member manifest exceeds its byte bound")
    return raw


@dataclass(frozen=True)
class MemberManifest:
    commit: str
    sha256: str
    count: int
    size: int

    def __post_init__(self):
        _hex(self.commit, 40, "member manifest commit")
        _hex(self.sha256, 64, "member manifest digest")
        _positive(self.count, "member manifest count")
        _positive(self.size, "member manifest size")
        if self.count > MAX_RETAINED_MEMBERS or self.size > MAX_MEMBER_MANIFEST_BYTES:
            raise ValueError("retained member manifest exceeds its bounds")

    @classmethod
    def parse(cls, value):
        _object(value, {"commit", "sha256", "count", "size"}, "member manifest")
        return cls(value["commit"], value["sha256"], value["count"], value["size"])

    def record(self):
        return {"commit": self.commit, "sha256": self.sha256, "count": self.count, "size": self.size}

    def read(self, raw, *, epoch, request_id):
        if not isinstance(raw, bytes) or len(raw) != self.size or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("retained membership differs from the request")
        value = read_json(raw, max_bytes=MAX_MEMBER_MANIFEST_BYTES)
        _object(value, {"schema", "epoch", "request_id", "members"}, "retained member manifest")
        if (
            value["schema"] != MEMBER_MANIFEST_SCHEMA
            or value["epoch"] != epoch
            or value["request_id"] != request_id
            or not isinstance(value["members"], list)
            or len(value["members"]) != self.count
        ):
            raise ValueError("retained member manifest identity differs from the request")
        members = tuple(Member.parse(member) for member in value["members"])
        if member_manifest_bytes(epoch, request_id, members) != raw:
            raise ValueError("retained member manifest must use canonical encoding")
        return members


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
    member_manifest: MemberManifest | None = None

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
        limit = MAX_MEMBERS if self.member_manifest is None else MAX_RETAINED_MEMBERS
        if (
            not isinstance(self.members, tuple)
            or len(self.members) > limit
            or not all(isinstance(m, Member) for m in self.members)
        ):
            raise ValueError("invalid accepted members")
        if self.member_manifest is None:
            if not self.members:
                raise ValueError("candidate must have accepted members")
        elif not isinstance(self.member_manifest, MemberManifest):
            raise ValueError("invalid retained member manifest")
        elif self.members:
            self.member_manifest.read(
                member_manifest_bytes(self.epoch, self.request_id, self.members),
                epoch=self.epoch,
                request_id=self.request_id,
            )
        if len({m.pr for m in self.members}) != len(self.members):
            raise ValueError("duplicate source PR in candidate")

    @property
    def accepted_members(self):
        if not self.members:
            raise ValueError("retained membership has not been verified")
        return self.members

    @cached_property
    def _member_set(self):
        return frozenset(self.accepted_members)

    @cached_property
    def accepted_heads(self):
        return tuple(dict.fromkeys(member.head for member in self.accepted_members))

    @property
    def manifest_ref(self):
        if self.member_manifest is None:
            raise ValueError("inline candidate has no retained member manifest")
        return f"refs/heads/tauceti-ci/{self.epoch}/{self.request_id}/members"

    def bind_members(self, raw):
        if self.member_manifest is None:
            raise ValueError("inline candidate cannot bind external membership")
        return replace(self, members=self.member_manifest.read(raw, epoch=self.epoch, request_id=self.request_id))

    @property
    def retained_ref(self):
        return f"refs/heads/tauceti-ci/{self.epoch}/{self.request_id}/candidate"

    def member_ref(self, member):
        if self.member_manifest is not None:
            raise ValueError("retained membership uses manifest parents rather than per-member branches")
        if member not in self._member_set:
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
        if value["schema"] != SCHEMA or not isinstance(value["members"], (list, dict)):
            raise ValueError("invalid candidate request schema")
        if len(canonical(value)) > MAX_REQUEST_BYTES:
            raise ValueError("candidate request exceeds its bounds")
        manifest = None
        if isinstance(value["members"], dict):
            _object(value["members"], {"manifest"}, "retained membership reference")
            manifest = MemberManifest.parse(value["members"]["manifest"])
            members = ()
        else:
            if len(value["members"]) > MAX_MEMBERS:
                raise ValueError("inline candidate request exceeds its member bound")
            members = tuple(Member.parse(member) for member in value["members"])
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
            members,
            manifest,
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
            "members": {"manifest": self.member_manifest.record()}
            if self.member_manifest is not None
            else [member.record() for member in self.members],
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
    if isinstance(run, dict) and type(run.get("run_attempt")) is int and run["run_attempt"] != attempt:
        raise EvidenceUnavailable("workflow attempt has been superseded")
    if not isinstance(run, dict) or any(
        run.get(key) != value
        for key, value in {
            "id": run_id,
            "run_attempt": attempt,
            "head_sha": request.workflow_revision,
            "event": "repository_dispatch",
        }.items()
    ):
        raise ValueError("evidence does not belong to the expected workflow attempt")
    if run.get("status") not in ({"completed"} if require_completed else {"queued", "in_progress", "completed"}):
        raise EvidenceUnavailable("workflow execution is not in an expected state")
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
        raise ValueError("evidence belongs to a different workflow path")
    repository = run.get("repository")
    if (
        not isinstance(repository, dict)
        or type(repository.get("id")) is not int
        or repository["id"] != policy.execution.native_id
        or repository.get("full_name") != policy.execution.repository.name
    ):
        raise ValueError("evidence belongs to a different execution repository")
    required = f"{policy.harness.repository.name}/.github/workflows/pr-build.yml@{policy.harness_revision}"
    workflows = run.get("referenced_workflows")
    if not isinstance(workflows, list) or not any(
        isinstance(w, dict) and w.get("path") == required and w.get("sha") == policy.harness_revision for w in workflows
    ):
        raise ValueError("evidence did not execute the pinned reusable build workflow")


def artifact_name(request, run_id, attempt):
    _positive(run_id, "workflow run ID")
    _positive(attempt, "workflow attempt")
    return f"source-candidate-{request.request_id}-{run_id}-{attempt}"


def receipt_artifact_name(request, run_id, attempt):
    _positive(run_id, "workflow run ID")
    _positive(attempt, "workflow attempt")
    return f"source-candidate-request-{request.request_id}-{run_id}-{attempt}"


def _producer(request, policy, run_id, attempt):
    return {
        "repository": policy.execution.record(),
        "run_id": run_id,
        "attempt": attempt,
        "workflow": policy.workflow,
        "workflow_revision": request.workflow_revision,
        "harness": policy.harness.record(),
        "harness_revision": policy.harness_revision,
    }


def make_receipt(request, policy, context):
    """Trusted bootstrap accepted this exact request; no source or build verdict is granted."""
    check_workflow_context(request, policy, context)
    return {
        "schema": RECEIPT_SCHEMA,
        "request": request.record(),
        "request_digest": request.digest,
        "producer": _producer(request, policy, context["run_id"], context["attempt"]),
    }


def _verify_request_claim(request, policy, value, *, run_id, attempt):
    if value["request_digest"] != request.digest:
        raise ValueError("evidence answers a different candidate request")
    returned = CandidateRequest.parse(value["request"], policy=policy)
    if returned.record() != request.record():
        raise ValueError("evidence changed the candidate request")
    expected = _producer(request, policy, run_id, attempt)
    producer = _object(value["producer"], expected, "evidence producer")
    if (
        RepositoryIdentity.parse(producer["repository"]) != policy.execution
        or RepositoryIdentity.parse(producer["harness"]) != policy.harness
    ):
        raise ValueError("invalid evidence producer repositories")
    if producer != expected or type(producer["run_id"]) is not int or type(producer["attempt"]) is not int:
        raise ValueError("evidence producer differs from the verified workflow attempt")


def verify_receipt(request, policy, receipt, run, *, run_id, observed_attempt):
    """Verify request ownership of a run, including one that is still running.

    The receipt proves trusted setup accepted the request. It does not prove
    source availability, successful execution, source approval or merge authority.
    """
    check_run_metadata(request, policy, run, run_id=run_id, attempt=observed_attempt, require_completed=False)
    _object(receipt, {"schema", "request", "request_digest", "producer"}, "candidate request receipt")
    if receipt["schema"] != RECEIPT_SCHEMA:
        raise ValueError("invalid candidate request receipt schema")
    producer = receipt["producer"]
    if not isinstance(producer, dict):
        raise ValueError("invalid request receipt producer")
    receipt_attempt = _positive(producer.get("attempt"), "receipt attempt")
    if receipt_attempt > observed_attempt:
        raise EvidenceUnavailable("receipt belongs to a newer workflow attempt")
    # GitHub reruns retain the original event payload, workflow SHA and run ID.
    # Ownership survives a partial rerun, even when bootstrap is not rerun.
    _verify_request_claim(request, policy, receipt, run_id=run_id, attempt=receipt_attempt)
    return {"request_digest": request.digest, "receipt_attempt": receipt_attempt, "observed_attempt": observed_attempt}


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
        "producer": _producer(request, policy, context["run_id"], context["attempt"]),
        "tested": tested,
        "verdicts": dict(verdicts),
    }


def verify_result(request, policy, result, run, *, run_id, attempt):
    """An artifact's own producer claims are insufficient without fresh run metadata."""
    check_run_metadata(request, policy, run, run_id=run_id, attempt=attempt)
    _object(result, {"schema", "request", "request_digest", "producer", "tested", "verdicts"}, "candidate result")
    if result["schema"] != SCHEMA:
        raise ValueError("result answers a different candidate request")
    _verify_request_claim(request, policy, result, run_id=run_id, attempt=attempt)
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
        raise EvidenceUnavailable("result is not confirmed by the completed workflow conclusion")
    return dict(verdicts)


def classify_verdicts(verdicts):
    """Policy/source failures dominate unrelated infrastructure failures.

    A skipped build cannot hide a decided policy rejection. Infrastructure may
    be retried under the queue's bounded recovery policy; policy rejection needs
    source review/repair rather than another identical dispatch.
    """
    _object(verdicts, VERDICTS, "candidate verdicts")
    if any(
        not isinstance(value, str) or value not in {"success", "failure", "infrastructure", "not_required"}
        for value in verdicts.values()
    ) or any(value == "not_required" for key, value in verdicts.items() if key != "publication"):
        raise ValueError("invalid result verdict classification")
    if verdicts["scope"] == "failure" or verdicts["bump_guard"] == "failure":
        return "policy_failure"
    if verdicts["build"] == "failure":
        return "source_failure"
    if any(value not in {"success", "not_required"} for value in verdicts.values()):
        return "infrastructure"
    return "passed"


def _artifact_payload(request, policy, metadata, archive, run, *, run_id, attempt, name, filename, completed):
    """Verify an immutable GitHub artifact without extracting any archive paths."""
    check_run_metadata(request, policy, run, run_id=run_id, attempt=attempt, require_completed=completed)
    if not isinstance(metadata, dict) or not isinstance(archive, bytes) or not 0 < len(archive) <= MAX_ARTIFACT_BYTES:
        raise ValueError("invalid candidate evidence artifact")
    _positive(metadata.get("id"), "artifact ID")
    size = metadata.get("size_in_bytes")
    if type(size) is not int or not 0 < size <= MAX_ARTIFACT_BYTES or metadata.get("expired") is not False:
        raise ValueError("unavailable candidate evidence artifact")
    if metadata.get("name") != name:
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
                or entries[0].filename != filename
                or entries[0].is_dir()
                or entries[0].flag_bits & 1
                or entries[0].file_size > MAX_RESULT_BYTES
            ):
                raise ValueError("invalid candidate evidence archive entries")
            with bundle.open(entries[0]) as stream:
                raw = stream.read(MAX_RESULT_BYTES + 1)
            result = read_json(raw, max_bytes=MAX_RESULT_BYTES)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise ValueError("invalid candidate evidence archive") from error
    return result


def verify_artifact(request, policy, metadata, archive, run, *, run_id, attempt):
    result = _artifact_payload(
        request,
        policy,
        metadata,
        archive,
        run,
        run_id=run_id,
        attempt=attempt,
        name=artifact_name(request, run_id, attempt),
        filename=RESULT_FILE,
        completed=True,
    )
    return verify_result(request, policy, result, run, run_id=run_id, attempt=attempt)


def verify_receipt_artifact(request, policy, metadata, archive, run, *, run_id, observed_attempt, receipt_attempt):
    _positive(observed_attempt, "observed workflow attempt")
    _positive(receipt_attempt, "receipt attempt")
    if receipt_attempt > observed_attempt:
        raise EvidenceUnavailable("receipt belongs to a newer workflow attempt")
    receipt = _artifact_payload(
        request,
        policy,
        metadata,
        archive,
        run,
        run_id=run_id,
        attempt=observed_attempt,
        name=receipt_artifact_name(request, run_id, receipt_attempt),
        filename=RECEIPT_FILE,
        completed=False,
    )
    proof = verify_receipt(request, policy, receipt, run, run_id=run_id, observed_attempt=observed_attempt)
    if proof["receipt_attempt"] != receipt_attempt:
        raise ValueError("receipt producer differs from its artifact attempt")
    return proof
