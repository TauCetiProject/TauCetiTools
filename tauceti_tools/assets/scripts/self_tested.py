#!/usr/bin/env python3
"""Recognise local testing inside the existing PR assessment. Never runs candidate code."""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from github_api import APIError, Deferred, GitHub

from tauceti_tools.self_tested import (
    BUMP_BRANCH,
    DESCRIPTION,
    PIN_FILES,
    allowed_bump_paths,
    allowed_paths,
    automatic_bump,
    label_names,
    latest_status,
    membership,
    requested,
)
from tauceti_tools.self_tested import (
    context as assertion_context_name,
)


def current_label(api, repo, number, head):
    """Read GitHub's existing timeline, so a push cannot inherit an old assertion.

    This runs only when the existing assessment has no result for this head.
    Cancellation of a label-triggered Actions run cannot lose the assertion.
    """
    owner, name = repo.split("/", 1)
    query = """query($owner:String!, $name:String!, $number:Int!, $before:String) {
      repository(owner:$owner,name:$name) { pullRequest(number:$number) {
        headRefOid timelineItems(last:100,before:$before,
          itemTypes:[LABELED_EVENT,UNLABELED_EVENT,HEAD_REF_FORCE_PUSHED_EVENT,PULL_REQUEST_COMMIT]) {
          nodes { __typename
            ... on LabeledEvent { label { name } actor { login } }
            ... on UnlabeledEvent { label { name } }
            ... on HeadRefForcePushedEvent { afterCommit { oid } }
            ... on PullRequestCommit { commit { oid } }
          } pageInfo { hasPreviousPage startCursor }
        }
      }}
    }"""
    before, label_actor = None, None
    while True:
        value = api.graphql(query, dict(owner=owner, name=name, number=number, before=before))["repository"]["pullRequest"]
        if not value or value["headRefOid"] != head:
            raise RuntimeError("PR head changed while reading its self-tested assertion")
        page = value["timelineItems"]
        for event in reversed(page["nodes"]):
            kind = event["__typename"]
            if kind in {"LabeledEvent", "UnlabeledEvent"} and (event.get("label") or {}).get("name") == "self-tested":
                if label_actor is None:
                    if kind == "UnlabeledEvent":
                        return False, None
                    label_actor = (event.get("actor") or {}).get("login") or ""
            elif kind == "HeadRefForcePushedEvent" or (kind == "PullRequestCommit" and event["commit"]["oid"] == head):
                return label_actor is not None, label_actor
        if not page["pageInfo"]["hasPreviousPage"]:
            return False, None
        before = page["pageInfo"]["startCursor"]


# Only these files feed check-bump.sh; candidate code is never fetched or run.
GUARD_FILES = ("lakefile.toml", "lakefile.lean", *PIN_FILES)


def fetch_guard_files(api, repo, sha, directory):
    for name in GUARD_FILES:
        try:
            data = api.request("GET", f"repos/{repo}/contents/{name}?ref={sha}")
        except APIError as error:
            if error.status == 404:
                continue  # A missing file stays absent, as check-bump.sh expects.
            raise
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
            raise RuntimeError(f"{name} at {sha} is not a readable file")
        (directory / name).write_bytes(base64.b64decode(data.get("content") or ""))


def check_bump(api, repo, head):
    """Run the packaged check-bump.sh on main's tip, the merge base and the head.

    Returns (verdict, output): True or False is check-bump.sh's verdict, None means
    its inputs could not be read. Each directory holds only the four Lake files.
    """
    try:
        base = api.request("GET", f"repos/{repo}/branches/main")["commit"]["sha"]
        merge_base = api.request("GET", f"repos/{repo}/compare/{base}...{head}?per_page=1")["merge_base_commit"]["sha"]
        # The upstream queries inside check-bump.sh use the same read credential.
        env = api._credential(True)[1] if hasattr(api, "_credential") else dict(os.environ)
        for name in ("TAUCETI_TEAM_TOKEN", "SELF_TESTED_TEAM_TOKEN", "SELF_TESTED_WRITE_TOKEN"):
            env.pop(name, None)
        with tempfile.TemporaryDirectory(prefix="tauceti-self-tested-bump-") as tmp:
            dirs = []
            for name, sha in (("base", base), ("mergebase", merge_base), ("pr", head)):
                directory = Path(tmp) / name
                directory.mkdir()
                fetch_guard_files(api, repo, sha, directory)
                dirs.append(str(directory))
            result = subprocess.run(
                ["bash", str(Path(__file__).resolve().with_name("check-bump.sh")), *dirs],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
    except (
        APIError,
        Deferred,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        return None, f"bump-guard inputs unavailable: {type(error).__name__}"
    return result.returncode == 0, result.stdout + result.stderr


def decide(api, repo, number, *, pr=None, rows=None, paths=None):
    """Assess the live routing label using the evidence collected for graduation.

    There is no label-event handshake or separate picker approval. The existing
    assessment publishes its decision on this immutable head for queue admission.
    A subsequent head needs a fresh assessment; review and staging still apply.
    """
    pr = pr or api.request("GET", f"repos/{repo}/pulls/{number}")
    head = pr["head"]["sha"]
    branch = pr["head"]["ref"]
    labels = pr.get("labels", [])
    names = label_names(labels)
    if pr.get("state") != "open" or pr["base"]["ref"] != "main" or "self-tested" not in names:
        return False
    if rows is None:
        rows = api.request("GET", f"repos/{repo}/commits/{head}/status?per_page=100").get("statuses", [])
    assertion_context = assertion_context_name(number)
    prior = latest_status(rows, assertion_context)
    eligible = requested(labels, branch) and os.environ.get("SELF_TESTED_MERGE_BACKEND") == "bors"

    def read_team(path):
        return api.request("GET", path)

    bump = branch.startswith(BUMP_BRANCH)
    if eligible:
        bound, labeller = current_label(api, repo, number, head)
        eligible = bound
    if eligible:
        actor = labeller if bump else pr.get("user", {}).get("login")
        eligible = (not bump or automatic_bump(pr, repo)) and membership(repo, actor, read_team, strict=True)
    if eligible:
        if paths is None:
            files = api.items(f"repos/{repo}/pulls/{number}/files?per_page=100")
            paths = [f["filename"] for f in files]
            paths.extend(f.get("previous_filename", "") for f in files if f.get("status") == "renamed")
            if len(files) != pr.get("changed_files"):
                raise RuntimeError("Incomplete self-tested diff; retry the PR assessment")
        eligible = allowed_bump_paths(paths) if bump else allowed_paths(paths)
    guard = None
    if eligible and bump:
        guard, output = check_bump(api, repo, head)
        print(output)
        if guard is None:
            raise RuntimeError("Bump-guard evidence unavailable; retry the PR assessment")
        eligible = guard
    after = api.request("GET", f"repos/{repo}/pulls/{number}")
    if (after["head"]["sha"] != head or after["base"]["ref"] != pr["base"]["ref"]
            or label_names(after.get("labels")) & {"self-tested", "full-lint", "lint-repair"}
            != names & {"self-tested", "full-lint", "lint-repair"}):
        raise RuntimeError("PR changed during self-tested assessment; retry")
    target_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    if guard is False:
        api.request("POST", f"repos/{repo}/statuses/{head}", {
            "state": "failure", "context": "bump-guard",
            "description": "self-tested: check-bump.sh rejected the Lake pins; ordinary PR CI required",
            "target_url": target_url,
        }, idempotent=True)
    if eligible:
        # Publish the scope/bump result before the head-bound admission result.
        statuses = (("bump-guard", "self-tested: check-bump.sh validated the Lake pins"),
                    ("scope", "self-tested: mathematics plus Lake pins")) if bump else (
                    ("scope", "self-tested: mathematics-only diff"),
                    ("bump-guard", "self-tested: dependency and toolchain pins unchanged"))
        for context, description in (*statuses, (assertion_context, DESCRIPTION)):
            if latest_status(rows, context).get("state") == "success":
                continue
            api.request("POST", f"repos/{repo}/statuses/{head}", {
                "state": "success", "context": context, "description": description,
                "target_url": target_url,
            }, idempotent=True)
    else:
        if prior.get("state") == "success":
            api.request("POST", f"repos/{repo}/statuses/{head}", {
                "state": "failure", "context": assertion_context,
                "description": "self-tested no longer applies; ordinary PR CI required", "target_url": target_url,
            }, idempotent=True)
        if requested(labels, branch):
            # Use the existing App's label credential. Its unlabeled event starts
            # ordinary PR CI; never dispatch a competing replacement here.
            api.request("DELETE", f"repos/{repo}/issues/{number}/labels/self-tested")
    return eligible


@lru_cache(maxsize=128)
def _team_read(path):
    # One membership read per author in this bounded assessment batch.
    try:
        return GitHub(max_wait=0).request("GET", path)
    except APIError as error:
        if error.status == 404:
            return {"state": "absent"}
        raise


def reconcile(number, repo, *, pr=None, statuses=None, paths=None):
    """Called only by the existing PR assessment; errors retain its normal retry work."""
    token = os.environ.get("SELF_TESTED_WRITE_TOKEN")
    if not token:
        from tauceti_tools.self_tested import accepted
        return bool(pr and accepted(pr.get("labels"), (statuses or {}).get(assertion_context_name(number)), member=True))

    class StatusAPI(GitHub):
        def _credential(self, read):
            role, env = super()._credential(read)
            if not read:
                env["GH_TOKEN"] = token
                env.pop("GITHUB_TOKEN", None)
            return role, env

    class AssessmentAPI(GitHub):
        def __init__(self):
            super().__init__(max_wait=0)
            self.status_api = StatusAPI(max_wait=0)

        def request(self, method, path, *args, **kwargs):
            if method == "POST" and "/statuses/" in path:
                return self.status_api.request(method, path, *args, **kwargs)
            if method == "GET" and path.startswith("orgs/") and "/memberships/" in path:
                return _team_read(path)
            return super().request(method, path, *args, **kwargs)

    rows = ([{"context": context, "state": state} for context, state in statuses.items()]
            if statuses is not None else None)
    return decide(AssessmentAPI(), repo, int(number), pr=pr, rows=rows, paths=paths)
