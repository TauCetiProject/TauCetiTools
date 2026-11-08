"""Trusted workflow ingress and data-only result production for source candidates."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .assets.scripts import ci_checkout, ci_git
from .ci_candidate import (
    MAX_MEMBER_MANIFEST_BYTES,
    MAX_MEMBERS,
    MEMBER_MANIFEST_FILE,
    CandidateRequest,
    ExecutionPolicy,
    artifact_name,
    canonical,
    check_run_metadata,
    check_workflow_context,
    make_receipt,
    make_result,
    read_json,
    receipt_artifact_name,
)
from .ci_candidate_evidence import EvidenceUnavailable, GitHubEvidence
from .forge import GITHUB, ForgeError, Forgejo
from .project import ConfigError, load
from .templates import materialize


def load_request(
    policy_path, configuration_path, event_path, context, *, harness_file=".github/workflows/pr-build.yml"
):
    """Paths are selected by trusted workflow code, never by the dispatch payload."""
    policy = ExecutionPolicy.parse(
        read_json(Path(policy_path).read_bytes()), configuration=Path(configuration_path).read_bytes()
    )
    event = read_json(Path(event_path).read_bytes(), max_bytes=1024 * 1024)
    if not isinstance(event, dict) or event.get("action") != context.get("action"):
        raise ValueError("candidate event differs from workflow context")
    payload = event.get("client_payload")
    if not isinstance(payload, dict) or set(payload) != {"request"}:
        raise ValueError("invalid source candidate dispatch payload")
    request = CandidateRequest.parse(payload["request"], policy=policy)
    check_workflow_context(request, policy, context, harness_file=harness_file)
    return request, policy


def check_source_repository(policy, client):
    """Fresh native identity: repository-name reuse cannot select another repository."""
    if client.forge != policy.credential_origin:
        raise ValueError("source API client belongs to a different credential origin")
    value = client.request("GET", f"repos/{policy.source.repository.name}")
    if not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] != policy.source.native_id:
        raise ValueError("source repository identity changed")
    if value.get("full_name") != policy.source.repository.name:
        raise ValueError("source repository name changed")


def retained_members(request, policy, context, *, token=None, client=None):
    """Read a request-bound data blob through an independently scoped native credential.

    No candidate checkout, executable configuration or request-chosen path is
    loaded. Commit/tree/blob links are checked with Git before the bounded blob
    read, just as they are for the publisher's toolchain pin.
    """
    check_workflow_context(request, policy, context)
    if request.member_manifest is None:
        raise ValueError("candidate does not use retained membership")
    client = client or Forgejo(policy.credential_origin, token=token, max_bytes=2 * MAX_MEMBER_MANIFEST_BYTES)
    check_source_repository(policy, client)
    with tempfile.TemporaryDirectory(prefix="source-members-") as directory:
        root = Path(directory)
        git = ci_git.Git(
            root / "members.git", root, token=token, token_origin=policy.credential_origin, follow_redirects=False
        )
        git.run(
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            "--depth=1",
            "--filter=tree:1",
            policy.source.repository.clone_url,
            f"+{request.manifest_ref}:refs/ci/members",
        )
        actual = git.run("rev-parse", "refs/ci/members").stdout.decode().strip()
        if git.commit(actual) != request.member_manifest.commit:
            raise ValueError("retained membership commit differs from the request")
        commit_header = git.run("cat-file", "commit", actual).stdout.split(b"\n\n", 1)[0]
        entries = git.run("ls-tree", "-z", actual).stdout.split(b"\0")
        if len(entries) != 2 or entries[1] != b"":
            raise ValueError("retained membership tree must contain exactly one data file")
        metadata, name = entries[0].split(b"\t", 1)
        mode, kind, blob_id = metadata.split(b" ")
        if mode != b"100644" or kind != b"blob" or name != MEMBER_MANIFEST_FILE.encode():
            raise ValueError("retained membership must be an ordinary data file")
        blob_id = ci_git.sha(blob_id.decode("ascii"))
    blob = client.request("GET", f"repos/{policy.source.repository.name}/git/blobs/{blob_id}")
    manifest = request.member_manifest
    if (
        not isinstance(blob, dict)
        or blob.get("sha") != blob_id
        or blob.get("encoding") != "base64"
        or type(blob.get("size")) is not int
        or blob["size"] != manifest.size
        or not isinstance(blob.get("content"), str)
    ):
        raise ValueError("invalid retained membership blob")
    raw = base64.b64decode(blob["content"], validate=True)
    if len(raw) != manifest.size or hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest() != blob_id:
        raise ValueError("retained membership differs from its Git blob")
    bound = request.bind_members(raw)
    check_member_parents(bound, commit_header)
    check_source_repository(policy, client)
    return bound, raw


def check_member_parents(request, header):
    """The sole retained manifest ref keeps every accepted source history reachable."""
    parents = tuple(line[7:].decode("ascii") for line in header.split(b"\n") if line.startswith(b"parent "))
    if parents != request.accepted_heads:
        raise ValueError("retained manifest parents differ from accepted member heads")


def bootstrap(request, policy, context, configuration_path, scripts, *, policy_dir=None):
    """Expose portable build settings and explicit role identities, without legacy source bindings."""
    check_workflow_context(request, policy, context)
    project = load(str(configuration_path))
    if (
        project.repository_ref("content") != policy.source.repository
        or project.ci_repository_ref() != policy.execution.repository
        or project.branch != policy.base_branch
    ):
        raise ValueError("project source/execution bindings differ from CI policy")
    materialize(project, Path(scripts), policy_dir=Path(policy_dir) if policy_dir else None, source_ci=True)
    local = project._local_environment()
    configuration_path = str(Path(configuration_path).resolve())
    return {
        **{
            key: local[key]
            for key in (
                "TAUCETI_BASE_BRANCH",
                "TAUCETI_PROJECT_KEY",
                "TAUCETI_LEAN_ROOT",
                "TAUCETI_LEAN_ROOTS",
                "TAUCETI_ROADMAP_DIR",
                "TAUCETI_COMPLETED_DIR",
                "TAUCETI_CACHE_ARTIFACT_URL",
                "TAUCETI_CACHE_REVISION_URL",
            )
        },
        "TAUCETI_CONF": configuration_path,
        "TAUCETI_CONF_SOURCE": configuration_path,
        "TAUCETI_CONF_SNAPSHOT": configuration_path,
        "TAUCETI_SOURCE_CANDIDATE": "true",
        "TAUCETI_SOURCE_REPOSITORY": json.dumps(policy.source.record(), sort_keys=True),
        "TAUCETI_CI_EXECUTION_REPOSITORY": json.dumps(policy.execution.record(), sort_keys=True),
        "TAUCETI_CACHE_REPOSITORY": policy.execution.repository.name,
        "CI_SOURCE_REQUIRE_PUBLICATION": "true" if policy.require_publication else "false",
        "TAUCETI_UPSTREAMS": json.dumps(project.github_upstreams),
        "TAUCETI_CI_GITHUB_READ_REPOSITORIES": json.dumps(
            sorted(
                {
                    policy.execution.repository.name,
                    policy.harness.repository.name,
                    *(upstream["repo"] for upstream in project.github_upstreams),
                }
            )
        ),
        "TAUCETI_GITHUB_STATE_URL": "",
        "MERGE_BACKEND": "queue",
        "TAUCETI_MERGE_BACKEND": "queue",
        "TAUCETI_BORS_URL": "",
        "TAUCETI_BORS_REPOSITORY_ID": "",
    }


def resolve(request, policy, context, store, *, token=None, client=None):
    """Fetch the authority-derived refs in one transfer and verify H/B/tree/members."""
    check_workflow_context(request, policy, context)
    client = client or Forgejo(policy.credential_origin, token=token)
    check_source_repository(policy, client)
    store = Path(store)
    if store.exists() or store.is_symlink():
        raise ValueError("candidate identity store must be new")
    with tempfile.TemporaryDirectory(prefix="source-candidate-home-") as home:
        git = ci_git.Git(store, home, token=token, token_origin=policy.credential_origin, follow_redirects=False)
        refs = [(request.retained_ref, "refs/ci/head"), (request.base, "refs/ci/base")]
        if request.member_manifest is not None:
            refs.append((request.manifest_ref, "refs/ci/members"))
        else:
            refs.extend((request.member_ref(m), f"refs/ci/member-{m.pr}") for m in request.accepted_members)
        git.fetch(policy.source.repository.clone_url, refs)
        for ref, expected in [
            ("refs/ci/head", request.head),
            ("refs/ci/base", request.base),
            *(
                ([("refs/ci/members", request.member_manifest.commit)])
                if request.member_manifest is not None
                else [(f"refs/ci/member-{m.pr}", m.head) for m in request.accepted_members]
            ),
        ]:
            actual = git.run("rev-parse", ref).stdout.decode().strip()
            if git.commit(actual) != expected:
                raise ValueError("retained source object differs from the request")
        if request.member_manifest is not None:
            check_member_parents(
                request, git.run("cat-file", "commit", request.member_manifest.commit).stdout.split(b"\n\n", 1)[0]
            )
            for head in request.accepted_heads:
                git.commit(head)
        if git.run("merge-base", "--all", request.base, request.head).stdout.decode().splitlines() != [request.base]:
            raise ValueError("candidate does not advance its exact source base")
        # The identity transfer deliberately omits trees. Read the tree OID
        # from the verified commit object without asking Git to walk its tree.
        header = git.run("cat-file", "commit", request.head).stdout.split(b"\n", 1)[0]
        if not header.startswith(b"tree "):
            raise ValueError("candidate commit has no tree identity")
        tree = ci_git.sha(header[5:].decode("ascii"))
        if tree != request.tree:
            raise ValueError("candidate tree differs from the request")
    check_source_repository(policy, client)
    result = {
        "request_digest": request.digest,
        "head": request.head,
        "base": request.base,
        "mergebase": request.base,
        "tree": request.tree,
    }
    result["lint_scoped"] = native_lint_policy(request, policy, context, client=client)
    return result


def fetch(request, policy, context, workspace, *, token=None, client=None):
    """Use the complete-history source helper; credentials never enter the checkout."""
    check_workflow_context(request, policy, context)
    client = client or Forgejo(policy.credential_origin, token=token)
    check_source_repository(policy, client)
    value = ci_checkout.fetch_sources(
        policy.source.repository,
        policy.source.repository,
        request.head,
        request.base,
        request.base,
        workspace,
        token=token,
        credential_origin=policy.credential_origin,
    )
    with tempfile.TemporaryDirectory(prefix="source-member-home-") as home:
        git = ci_checkout.SourceGit(
            Path(value["store"]), home, token=token, token_origin=policy.credential_origin, follow_redirects=False
        )
        if request.member_manifest is not None:
            git.fetch_sources(policy.source.repository.clone_url, [(request.manifest_ref, "refs/ci/members")])
            if (
                git.commit(git.run("rev-parse", "refs/ci/members").stdout.decode().strip())
                != request.member_manifest.commit
            ):
                raise ValueError("fetched member manifest differs from the request")
            check_member_parents(
                request, git.run("cat-file", "commit", request.member_manifest.commit).stdout.split(b"\n\n", 1)[0]
            )
            for head in request.accepted_heads:
                git.commit(head)
            git.run("commit-graph", "write", "--reachable")
        else:
            git.fetch_sources(
                policy.source.repository.clone_url,
                [(request.member_ref(m), f"refs/ci/member-{m.pr}") for m in request.accepted_members],
            )
            for member in request.accepted_members:
                if git.run("rev-parse", f"refs/ci/member-{member.pr}").stdout.decode().strip() != member.head:
                    raise ValueError("fetched member differs from the accepted head")
    check_source_repository(policy, client)
    return value


def member_size_policy(request, policy, context, workspace):
    """Apply existing per-PR caps to exact retained members using only local Git objects."""
    check_workflow_context(request, policy, context)
    objects = Path(workspace) / ci_checkout.STORE / "objects"
    values = {"SOURCE_SIZE_POLICY_COMPLETE": "1"}
    with tempfile.TemporaryDirectory(prefix="source-member-policy-") as home:
        git = ci_git.Git(Path(home) / "reader.git", home, objects=objects)
        for member in request.accepted_members:
            git.commit(member.head)
            comparison = git.run("merge-base", "--all", request.base, member.head, check=False)
            if comparison.returncode == 1 and not comparison.stdout:
                values["SOURCE_MEMBER_POLICY_REJECTED"] = "1"
                continue
            comparison.check_returncode()
            bases = comparison.stdout.decode().splitlines()
            if len(bases) != 1:
                values["SOURCE_MEMBER_POLICY_REJECTED"] = "1"
                continue
            rows = git.run(
                "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z", bases[0], member.head, "--"
            ).stdout.split(b"\0")
            changed = {}
            for row in rows:
                if not row:
                    continue
                adds, deletes, name = row.split(b"\t", 2)
                if adds == deletes == b"-":
                    # Preserve GitHub's zero line counts for binary files;
                    # they still count toward file limits and path scope.
                    changed[name] = (0, 0)
                    continue
                if not adds.isdigit() or not deletes.isdigit():
                    values["DIFF_UNKNOWN"] = "1"
                    values["SOURCE_SIZE_POLICY_COMPLETE"] = "0"
                    continue
                changed[name] = (int(adds), int(deletes))
            pins = bool({b"lake-manifest.json", b"lean-toolchain"} & changed.keys())
            if len(changed) > 2000 or (
                not pins and (sum(a for a, _ in changed.values()) > 2000 or sum(d for _, d in changed.values()) > 2000)
            ):
                values["TOO_LARGE"] = "1"
            added = git.run(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--name-only",
                "--diff-filter=A",
                "-z",
                bases[0],
                member.head,
                "--",
            ).stdout.split(b"\0")
            if any(name.endswith(b".lean") and changed.get(name, (0, 0))[0] > 1000 for name in added if name):
                values["NEWFILE_TOO_LONG"] = "1"
    return values


def bump_policy(request, policy, context, configuration_path, base, mergebase, checkout):
    """Distinguish rejected pins from a failed external lookup/resolution."""
    from .upstreams import ResolutionUnavailable, guard

    check_workflow_context(request, policy, context)
    project = load(str(configuration_path))
    try:
        guard(project, Path(base), Path(mergebase), Path(checkout))
    except (ResolutionUnavailable, OSError, subprocess.SubprocessError, KeyError, TypeError):
        return {"INFRA": "1"}
    except (ConfigError, ValueError):
        return {"BUMP_BAD": "1", "INFRA": "1"}
    return {"BUMP_VALIDATED": "1"}


def toolchain_pin(request, policy, context, store, *, token=None, client=None):
    """The publisher reads one blob bound to the retained commit's verified root tree.

    No candidate checkout or configuration is loaded on the publisher. A shallow
    tree-only transfer plus one immutable blob read avoids cloning full histories
    again and never resolves an ambiguous SHA-looking branch through contents APIs.
    """
    check_workflow_context(request, policy, context, harness_file=".github/workflows/publish-lake-cache.yml")
    client = client or Forgejo(policy.credential_origin, token=token, max_bytes=64 * 1024)
    check_source_repository(policy, client)
    store = Path(store)
    if store.exists() or store.is_symlink():
        raise ValueError("toolchain identity store must be new")
    with tempfile.TemporaryDirectory(prefix="source-toolchain-home-") as home:
        git = ci_git.Git(store, home, token=token, token_origin=policy.credential_origin, follow_redirects=False)
        git.run(
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            "--depth=1",
            "--filter=tree:1",
            policy.source.repository.clone_url,
            f"+{request.retained_ref}:refs/ci/head",
        )
        actual = git.run("rev-parse", "refs/ci/head").stdout.decode().strip()
        if git.commit(actual) != request.head:
            raise ValueError("publisher retained commit differs from the request")
        header = git.run("cat-file", "commit", actual).stdout.split(b"\n", 1)[0]
        if header != b"tree " + request.tree.encode("ascii"):
            raise ValueError("publisher candidate tree differs from the request")
        entries = git.run("ls-tree", "-z", actual, "--", "lean-toolchain").stdout.split(b"\0")
        if len(entries) != 2 or entries[1] != b"":
            raise ValueError("publisher toolchain pin is unavailable")
        metadata, name = entries[0].split(b"\t", 1)
        mode, kind, blob_id = metadata.split(b" ")
        if mode not in {b"100644", b"100755"} or kind != b"blob" or name != b"lean-toolchain":
            raise ValueError("publisher toolchain pin must be an ordinary file")
        blob_id = ci_git.sha(blob_id.decode("ascii"))
    blob = client.request("GET", f"repos/{policy.source.repository.name}/git/blobs/{blob_id}")
    if (
        not isinstance(blob, dict)
        or blob.get("sha") != blob_id
        or blob.get("encoding") != "base64"
        or type(blob.get("size")) is not int
        or not 0 < blob["size"] <= 512
        or not isinstance(blob.get("content"), str)
    ):
        raise ValueError("invalid immutable toolchain blob")
    raw = base64.b64decode(blob["content"], validate=True)
    if len(raw) != blob["size"] or hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest() != blob_id:
        raise ValueError("toolchain blob differs from its Git identity")
    if not re.fullmatch(rb"leanprover/lean4:[A-Za-z0-9][A-Za-z0-9_.-]*\n?", raw):
        raise ValueError("candidate toolchain is not an official Lean channel")
    check_source_repository(policy, client)
    return raw


def native_lint_policy(request, policy, context, *, client):
    """Collect native metadata before any dependency executable runs on the job."""
    check_workflow_context(request, policy, context)
    if len(request.accepted_members) > MAX_MEMBERS:
        return False
    started = time.monotonic()
    try:
        check_source_repository(policy, client)
        for member in request.accepted_members:
            if time.monotonic() - started > 45:
                raise ValueError("native lint policy time bound reached")
            pull = client.request("GET", f"repos/{policy.source.repository.name}/pulls/{member.pr}")
            if not isinstance(pull, dict) or type(pull.get("number")) is not int or pull["number"] != member.pr:
                raise ValueError("native lint policy PR unavailable")
            head, labels = pull.get("head"), pull.get("labels")
            if (
                not isinstance(head, dict)
                or head.get("sha") != member.head
                or not isinstance(head.get("ref"), str)
                or not head["ref"]
            ):
                raise ValueError("native lint policy head changed or unavailable")
            if head["ref"].startswith("lint-repair/"):
                raise ValueError("native lint repair requires full lint")
            if (
                not isinstance(labels, list)
                or len(labels) > 100
                or any(
                    not isinstance(label, dict) or not isinstance(label.get("name"), str) or not label["name"]
                    for label in labels
                )
            ):
                raise ValueError("native lint labels unavailable")
            if any("full-lint" in label["name"].split(",") for label in labels):
                raise ValueError("native full-lint label is present")
        check_source_repository(policy, client)
    except (ForgeError, ValueError, OSError):
        return False
    return True


def lint_scope(request, policy, context, checkout, destination, configuration_path, *, observation):
    """Only saved trusted native policy and local objects; no credential/network use."""
    check_workflow_context(request, policy, context)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if (
        not isinstance(observation, dict)
        or observation.get("request_digest") != request.digest
        or observation.get("lint_scoped") is not True
    ):
        return {"LINT_ONLY_MODULES": "", "LINT_SCOPE_DIR": str(destination)}
    from .assets.scripts import lint_scope_files

    try:
        files = [
            name.decode("utf-8")
            for name in lint_scope_files.changed_files(str(checkout), request.base, request.head).split(b"\0")
            if name
        ]
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"LINT_ONLY_MODULES": "", "LINT_SCOPE_DIR": str(destination)}
    # The renderer gives the shared shell script its project-specific root, but
    # this module obtains it from the independently pinned project configuration.
    project = load(str(configuration_path))
    prefix = project.roots[0].replace(".", "/") + "/"
    modules, unmatched = [], []
    for name in files:
        if name.startswith(prefix) and re.fullmatch(
            re.escape(prefix[:-1]) + r"(/[A-Za-z_][A-Za-z0-9_']*)+\.lean", name
        ):
            modules.append(name[:-5].replace("/", "."))
        elif name.endswith(".lean"):
            unmatched.append(name)
    (destination / "modules.txt").write_text("".join(m + "\n" for m in sorted(set(modules))))
    (destination / "unmatched.txt").write_text("".join(name + "\n" for name in unmatched))
    return {"LINT_ONLY_MODULES": str(destination / "modules.txt"), "LINT_SCOPE_DIR": str(destination)}


class PublicationPending(EvidenceUnavailable):
    """Authenticated job/step evidence is not complete yet; retry within the deadline."""


def publication_timestamp(value):
    """Normalize bounded RFC3339 times to the job/run API's whole-second precision."""
    if value is None:
        raise PublicationPending("missing publication execution timestamp")
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})", value, re.ASCII
    ):
        raise EvidenceUnavailable("invalid publication execution timestamp")
    try:
        # Fractions are intentionally discarded before parsing, including on
        # Python 3.10 whose fromisoformat accepts fewer fractional formats.
        seconds = re.sub(r"\.\d{1,9}(?=Z|[+-])", "", value)
        return datetime.fromisoformat(seconds.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise EvidenceUnavailable("invalid publication execution timestamp") from error


def publication_execution(run, builder, publisher):
    """Reused jobs can have new IDs/current run_attempt but retain old timestamps."""
    attempt_start = publication_timestamp(run.get("run_started_at"))
    build_start = publication_timestamp(builder.get("started_at"))
    if build_start <= attempt_start:
        return False
    build_end = publication_timestamp(builder.get("completed_at"))
    publish_start = publication_timestamp(publisher.get("started_at"))
    publish_end = publication_timestamp(publisher.get("completed_at"))
    steps = publisher.get("steps")
    if steps is None:
        raise PublicationPending("missing publisher step evidence")
    if not isinstance(steps, list):
        raise EvidenceUnavailable("missing publisher step evidence")
    confirmations = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("name") == "Confirm the exact revision map on the public endpoint"
    ]
    if not confirmations and not steps:
        raise PublicationPending("missing publisher confirmation")
    if len(confirmations) != 1:
        raise EvidenceUnavailable("missing or ambiguous publisher confirmation")
    confirmation = confirmations[0]
    for entry in (builder, publisher, confirmation):
        if entry.get("status") in {None, "queued", "in_progress", "waiting", "pending", "requested"}:
            raise PublicationPending("publication execution is not complete")
        if entry.get("status") != "completed":
            return False
        if entry.get("conclusion") is None:
            raise PublicationPending("publication conclusion is not available")
        if entry.get("conclusion") != "success":
            return False
    confirm_start = publication_timestamp(confirmation.get("started_at"))
    confirm_end = publication_timestamp(confirmation.get("completed_at"))
    # Strictly later: equal second-resolution timestamps cannot distinguish a
    # reused job from a fresh execution in an immediately requested rerun.
    return attempt_start < build_start <= build_end <= publish_start <= confirm_start <= confirm_end <= publish_end


def publication_verdict(request, policy, context, reader, *, clock=time.monotonic, sleep=time.sleep):
    """Require this attempt's isolated publisher and its exact-map confirmation."""
    if not policy.require_publication:
        return "not_required"
    start = clock()
    acquired = False
    while clock() - start < 1200:
        try:
            run = reader.get(f"/actions/runs/{context['run_id']}")
            value = reader.get(f"/actions/runs/{context['run_id']}/attempts/{context['attempt']}/jobs?per_page=100")
        except EvidenceUnavailable:
            if not acquired and clock() - start >= 300:
                return "infrastructure"
            sleep(30)
            continue
        check_run_metadata(
            request, policy, run, run_id=context["run_id"], attempt=context["attempt"], require_completed=False
        )
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("jobs"), list)
            or type(value.get("total_count")) is not int
            or value["total_count"] != len(value["jobs"])
            or len(value["jobs"]) > 100
        ):
            raise EvidenceUnavailable("incomplete publisher job evidence")

        def matching(suffix, jobs=value["jobs"]):
            return [
                job
                for job in jobs
                if isinstance(job, dict)
                and isinstance(job.get("name"), str)
                and (job["name"] == suffix or job["name"].endswith(" / " + suffix))
            ]

        publishers, builders = matching("publish-merge-group-cache / publish"), matching("sandboxed-build")
        if len(publishers) > 1 or len(builders) != 1:
            raise EvidenceUnavailable("ambiguous publisher/build job evidence")
        if publishers:
            job = publishers[0]
            runner = job.get("runner_id")
            isolated = False
            if type(runner) is int and runner > 0:
                acquired = True
                build_runner = builders[0].get("runner_id")
                if (
                    job.get("runner_group_id") != 0
                    or job.get("runner_group_name") != "GitHub Actions"
                    or job.get("labels") != ["ubuntu-24.04"]
                    or type(build_runner) is not int
                    or build_runner <= 0
                    or build_runner == runner
                ):
                    raise EvidenceUnavailable("publisher did not acquire an isolated hosted runner")
                isolated = True
            if job.get("status") == "completed":
                if isolated and job.get("conclusion") == "success":
                    try:
                        if publication_execution(run, builders[0], job):
                            return "success"
                    except PublicationPending:
                        sleep(30)
                        continue
                return "infrastructure"
        if not acquired and clock() - start >= 300:
            return "infrastructure"
        sleep(30)
    return "infrastructure"


def finalize(request, policy, context, evidence, *, reader=None):
    check_workflow_context(request, policy, context)
    if not isinstance(evidence, dict) or set(evidence) != {
        "job_result",
        "build_state",
        "scope",
        "bump_guard",
        "publication_staged",
        "tested_commit",
        "tested_tree",
        "scope_complete",
        "scope_rejected",
        "pins_known",
        "bump_complete",
        "size_complete",
        "infrastructure",
        "build_outcome",
    }:
        raise ValueError("invalid trusted build evidence")
    tested_commit, tested_tree = evidence.get("tested_commit"), evidence.get("tested_tree")
    identity = tested_commit == request.head and tested_tree == request.tree
    if not identity:
        tested_commit = tested_tree = None
    state = evidence.get("build_state")
    build = (
        state
        if identity
        and state in {"success", "failure"}
        and evidence.get("job_result") in {"success", "failure"}
        and evidence.get("infrastructure") != "1"
        and evidence.get("build_outcome") in {"success", "failure"}
        else "infrastructure"
    )
    if build == "success" and evidence.get("job_result") != "success":
        build = "infrastructure"
    verdicts = {
        "build": build,
        "scope": evidence.get("scope") if evidence.get("scope") in {"success", "failure"} else "infrastructure",
        "bump_guard": evidence.get("bump_guard")
        if evidence.get("bump_guard") in {"success", "failure"}
        else "infrastructure",
        "publication": "infrastructure" if policy.require_publication else "not_required",
    }
    if evidence.get("scope_rejected") == "1" and evidence.get("scope") == "failure":
        verdicts["scope"] = "failure"
    elif evidence.get("scope_complete") != "1" or evidence.get("size_complete") != "1":
        verdicts["scope"] = "infrastructure"
    if evidence.get("pins_known") != "1" or evidence.get("bump_complete") != "1":
        verdicts["bump_guard"] = "infrastructure"
    if build == "success":
        if policy.require_publication and evidence.get("publication_staged") != "true":
            verdicts["publication"] = "infrastructure"
        else:
            try:
                verdicts["publication"] = publication_verdict(request, policy, context, reader)
            except (EvidenceUnavailable, ValueError, OSError):
                verdicts["publication"] = "infrastructure"
    return make_result(
        request, policy, context, tested_commit=tested_commit, tested_tree=tested_tree, verdicts=verdicts
    )


def context_from_environment(env):
    names = {
        "event_name": "GITHUB_EVENT_NAME",
        "action": "CI_SOURCE_EVENT_ACTION",
        "repository": "GITHUB_REPOSITORY",
        "repository_id": "GITHUB_REPOSITORY_ID",
        "workflow_sha": "GITHUB_WORKFLOW_SHA",
        "workflow_ref": "GITHUB_WORKFLOW_REF",
        "harness_repository": "CI_SOURCE_HARNESS_REPOSITORY",
        "harness_sha": "CI_SOURCE_HARNESS_SHA",
        "harness_file": "CI_SOURCE_HARNESS_FILE",
    }
    context = {key: env.get(name) for key, name in names.items()}
    for key, name in (("run_id", "GITHUB_RUN_ID"), ("attempt", "GITHUB_RUN_ATTEMPT")):
        value = env.get(name)
        if (
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdecimal()
            or value.startswith("0")
            or len(value) > 19
        ):
            raise ValueError("missing trusted workflow run identity")
        context[key] = int(value)
    return context


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "bootstrap",
            "resolve",
            "fetch",
            "toolchain",
            "lint-scope",
            "member-policy",
            "bump-policy",
            "finalize",
        ),
    )
    parser.add_argument("--policy", required=True)
    parser.add_argument("--configuration", required=True)
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--store")
    parser.add_argument("--workspace")
    parser.add_argument("--output", required=True)
    parser.add_argument("--github-output")
    parser.add_argument("--github-env")
    parser.add_argument("--scripts")
    parser.add_argument("--policy-dir")
    parser.add_argument("--toolchain-output")
    parser.add_argument("--checkout")
    parser.add_argument("--destination")
    parser.add_argument("--evidence")
    parser.add_argument("--observation")
    parser.add_argument("--revision")
    parser.add_argument("--artifact-name")
    parser.add_argument("--lean-root")
    parser.add_argument("--roadmap-dir")
    parser.add_argument("--base-dir")
    parser.add_argument("--mergebase-dir")
    parser.add_argument("--members-file")
    parser.add_argument("--members-output")
    parser.add_argument("--receipt-output")
    args = parser.parse_args(argv)
    context = context_from_environment(os.environ)
    request, policy = load_request(
        args.policy,
        args.configuration,
        args.event,
        context,
        harness_file=".github/workflows/publish-lake-cache.yml"
        if args.command == "toolchain"
        else ".github/workflows/pr-build.yml",
    )
    # Only the independently provisioned native read credential is used here.
    token = os.environ.get("CI_SOURCE_READ_TOKEN") or None
    if request.member_manifest is not None:
        if args.command == "resolve":
            if not args.members_output:
                parser.error("retained membership resolution requires --members-output")
            request, raw = retained_members(request, policy, context, token=token)
            with open(args.members_output, "xb") as output:
                output.write(raw)
        elif args.command in {"fetch", "member-policy"}:
            if not args.members_file:
                parser.error("this operation requires verified --members-file")
            with open(args.members_file, "rb") as source:
                raw = source.read(MAX_MEMBER_MANIFEST_BYTES + 1)
            request = request.bind_members(raw)
    if args.command == "bootstrap":
        if not args.scripts:
            parser.error("bootstrap requires --scripts")
        result = bootstrap(request, policy, context, args.configuration, args.scripts, policy_dir=args.policy_dir)
        if args.lean_root != result["TAUCETI_LEAN_ROOT"] or args.roadmap_dir != result["TAUCETI_ROADMAP_DIR"]:
            raise ValueError("workflow library/roadmap inputs differ from trusted project")
        if args.receipt_output:
            with open(args.receipt_output, "xb") as output:
                output.write(canonical(make_receipt(request, policy, context)))
    elif args.command == "resolve":
        if not args.store:
            parser.error("resolve requires --store")
        result = resolve(request, policy, context, args.store, token=token)
    elif args.command == "fetch":
        if not args.workspace:
            parser.error("fetch requires --workspace")
        result = fetch(request, policy, context, args.workspace, token=token)
    elif args.command == "toolchain":
        if not args.store or not args.toolchain_output:
            parser.error("toolchain requires --store and --toolchain-output")
        if args.revision != request.head or args.artifact_name != "lake-cache-staging-" + request.head:
            raise ValueError("publisher revision or staged artifact differs from source request")
        raw = toolchain_pin(request, policy, context, args.store, token=token)
        Path(args.toolchain_output).write_bytes(raw)
        result = {"head": request.head, "tree": request.tree, "toolchain": raw.decode().strip()}
    elif args.command == "lint-scope":
        if not args.checkout or not args.destination:
            parser.error("lint-scope requires --checkout and --destination")
        observation = read_json(Path(args.observation).read_bytes()) if args.observation else None
        result = lint_scope(
            request, policy, context, args.checkout, args.destination, args.configuration, observation=observation
        )
    elif args.command == "member-policy":
        if not args.workspace:
            parser.error("member-policy requires --workspace")
        result = member_size_policy(request, policy, context, args.workspace)
    elif args.command == "bump-policy":
        if not args.base_dir or not args.mergebase_dir or not args.checkout:
            parser.error("bump-policy requires --base-dir, --mergebase-dir and --checkout")
        result = bump_policy(
            request, policy, context, args.configuration, args.base_dir, args.mergebase_dir, args.checkout
        )
    else:
        if not args.evidence:
            parser.error("finalize requires --evidence")
        evidence = read_json(Path(args.evidence).read_bytes())
        reader = GitHubEvidence(
            policy, token=os.environ.get("GH_READ_TOKEN") or os.environ.get("GH_TOKEN"), token_origin=GITHUB
        )
        result = finalize(request, policy, context, evidence, reader=reader)
    Path(args.output).write_text(json.dumps(result, sort_keys=True) + "\n")
    if args.github_output and args.command == "resolve":
        with open(args.github_output, "a") as output:
            output.write(
                f"num=source-batch\nsha={request.head}\nbase_ref={request.base}\nbase_sha={request.base}\nmergebase={request.base}\nstatus_sha={request.head}\nrepo={policy.source.repository.name}\nauthor=\n"
            )
    if args.github_output and args.command == "bootstrap" and args.receipt_output:
        with open(args.github_output, "a") as output:
            output.write(
                f"request-artifact-name={receipt_artifact_name(request, context['run_id'], context['attempt'])}\n"
            )
    if args.github_env:
        with open(args.github_env, "a") as output:
            if args.command in {"bootstrap", "lint-scope", "member-policy", "bump-policy"}:
                for key, value in result.items():
                    if not isinstance(value, str) or "\n" in value or "\r" in value:
                        raise ValueError("invalid workflow environment setting")
                    output.write(f"{key}={value}\n")
            elif args.command == "resolve":
                output.write("MERGEBASE_EXACT=1\n")
            elif args.command == "fetch":
                output.write(f"CI_SOURCE_OBJECTS={Path(result['store']) / 'objects'}\n")
            elif args.command == "toolchain":
                project = load(args.configuration)
                cache = project.service("cache")
                for name, value in (
                    ("TAUCETI_CACHE_REPOSITORY", policy.execution.repository.name),
                    ("TAUCETI_CACHE_ARTIFACT_URL", cache.get("artifact_url", "")),
                    ("TAUCETI_CACHE_REVISION_URL", cache.get("revision_url", "")),
                ):
                    if not isinstance(value, str) or "\n" in value or "\r" in value:
                        raise ValueError("invalid public cache configuration")
                    output.write(f"{name}={value}\n")
    if args.command == "finalize":
        if args.github_output:
            with open(args.github_output, "a") as output:
                output.write(f"artifact-name={artifact_name(request, context['run_id'], context['attempt'])}\n")
        return 0 if all(value in {"success", "not_required"} for value in result["verdicts"].values()) else 1
    if args.command == "member-policy" and any(
        result.get(flag) == "1"
        for flag in ("TOO_LARGE", "DIFF_UNKNOWN", "NEWFILE_TOO_LONG", "SOURCE_MEMBER_POLICY_REJECTED")
    ):
        print("::error::source member review-size policy rejected this candidate; inspect source-member-policy.json")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, ForgeError, EvidenceUnavailable, subprocess.SubprocessError) as error:
        # Remote/API exceptions are already scrubbed; avoid replaying Git stderr
        # or attacker-controlled JSON into GitHub workflow command syntax.
        raise SystemExit(f"source candidate {type(error).__name__}") from None
