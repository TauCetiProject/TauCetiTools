#!/usr/bin/env python3
"""Recover abandoned PR builds through the ordinary default-branch workflow."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

from github_api import APIError, Deferred, GitHub

INVENTORY = """
query($owner: String!, $name: String!, $after: String) {
  rateLimit { cost }
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    pullRequests(states: OPEN, first: 100, after: $after,
                 orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes {
        number headRefOid isDraft updatedAt
        labels(first: 100) { nodes { name } }
        commits(last: 1) { nodes { commit { oid status { contexts { context state } } } } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

CURRENT = """
query($owner: String!, $name: String!, $pr: Int!, $head: GitObjectID!) {
  rateLimit { cost }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) { state isDraft headRefOid labels(first: 100) { nodes { name } } }
    object(oid: $head) { ... on Commit { status { contexts { context state } } } }
  }
}
"""


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def has_preflight(pull, commit):
    rows = (commit.get("status") or {}).get("contexts", [])
    labels = {label["name"].lower() for label in (pull.get("labels") or {}).get("nodes", [])}
    return any(
        row["context"] == "build" and row.get("state", "").lower() in {"success", "failure", "error"} for row in rows
    ) or (
        "self-tested" in labels
        and not labels & {"full-lint", "lint-repair"}
        and any(
            row["context"] == f"self-tested/{pull['number']}" and row.get("state", "").lower() == "success"
            for row in rows
        )
    )


def inventory(api, repo):
    owner, name = repo.split("/")
    after, pulls = None, []
    while True:
        data = api.graphql(INVENTORY, dict(owner=owner, name=name, after=after))["repository"]
        page = data["pullRequests"]
        for pull in page["nodes"]:
            commit = pull["commits"]["nodes"][-1]["commit"]
            if not pull["isDraft"] and (commit["oid"] != pull["headRefOid"] or not has_preflight(pull, commit)):
                pulls.append(pull)
        if not page["pageInfo"]["hasNextPage"]:
            return data["defaultBranchRef"]["name"], pulls
        after = page["pageInfo"]["endCursor"]


def runs(api, repo, query, *, include_ignored=False):
    page = 1
    while True:
        result = api.request(
            "GET", f"repos/{repo}/actions/workflows/pr-build.yml/runs?{query}&per_page=100&page={page}"
        )
        rows = result["workflow_runs"]
        yield from (
            row for row in rows
            if include_ignored or not row.get("display_title", "").startswith("Ignored label event ")
        )
        if len(rows) < 100:
            return
        page += 1


def pr_numbers(run):
    numbers = {p["number"] for p in run.get("pull_requests", [])}
    # Dispatches have no pull_requests association, and use the default branch's
    # SHA rather than the tested PR head. Include both current and legacy titles.
    match = re.fullmatch(r"pr-build (?:pr=)?([1-9][0-9]*)(?: head=[0-9a-f]{40})?", run.get("display_title", ""))
    if match:
        numbers.add(int(match[1]))
    return numbers


def matches(run, number, head):
    """An obsolete, explicitly bound PR head cannot reserve a newer head."""
    bound = re.fullmatch(r"pr-build pr=([1-9][0-9]*) head=([0-9a-f]{40})", run.get("display_title", ""))
    if bound:
        return int(bound[1]) == number and bound[2] == head
    if run.get("event") == "pull_request_target":
        return run["head_sha"] == head
    return number in pr_numbers(run) or run["head_sha"] == head


def still_missing(api, repo, number, head):
    owner, name = repo.split("/")
    data = api.graphql(CURRENT, dict(owner=owner, name=name, pr=number, head=head))["repository"]
    pull, commit = data["pullRequest"], data["object"]
    return bool(
        pull
        and commit
        and pull["state"] == "OPEN"
        and not pull["isDraft"]
        and pull["headRefOid"] == head
        and not has_preflight({**pull, "number": number}, commit)
    )


def quota_reserved(api, reserve):
    """Leave the shared installation's remaining budget for essential queue operations."""
    if not reserve:
        return False
    for stat in getattr(api, "stats", {}).values():
        for bucket in stat.get("buckets", {}).values():
            remaining = bucket.get("remaining")
            if remaining is not None and str(remaining).isdigit() and int(remaining) < reserve:
                return True
    return False


ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}


def ordinary(run, repo):
    """Staging belongs to its controller; only ordinary trusted PR workflows qualify."""
    return (
        run.get("path") == ".github/workflows/pr-build.yml"
        and run.get("repository", {}).get("full_name") == repo
        and (run.get("event") in {"pull_request_target", "workflow_dispatch"}
             or run.get("event") == "repository_dispatch" and bool(pr_numbers(run)))
        and not run.get("display_title", "").startswith("bors ")
    )


def old(run, now, min_age):
    """A new attempt or a recent transition receives the full acquisition grace."""
    try:
        times = [timestamp(run[k]) for k in ("created_at", "updated_at", "run_started_at") if run.get(k)]
        return bool(times) and now - max(times) >= min_age
    except (ValueError, TypeError, AttributeError):
        return False


def unacquired(api, repo, run):
    """Require complete attempt-bound job evidence; never stop an acquired job."""
    attempt = run.get("run_attempt")
    if type(attempt) is not int or attempt < 1:
        return None
    data = api.request(
        "GET", f"repos/{repo}/actions/runs/{run['id']}/attempts/{attempt}/jobs?per_page=100"
    )
    if not isinstance(data, dict):
        return None
    jobs = data.get("jobs")
    if (not isinstance(jobs, list) or type(data.get("total_count")) is not int
            or data["total_count"] != len(jobs) or len(jobs) > 100):
        return None
    if any(
        not isinstance(job, dict)
        or type(job.get("id")) is not int or job["id"] < 1
        or job.get("run_id", run["id"]) != run["id"]
        or job.get("run_attempt", attempt) != attempt
        or job.get("status") not in ACTIVE | {"completed"}
        or job.get("status") == "in_progress"
        or job.get("status") != "completed" and (job.get("runner_id") or job.get("runner_name"))
        for job in jobs
    ):
        return None
    if len({job["id"] for job in jobs}) != len(jobs):
        return None
    return jobs


def cancel_unacquired(api, repo, run, jobs):
    """Cancel stale queued work; tolerate GitHub's explicit orphan-shell error.

    An ambiguous POST is never repeated. A subsequent sweep observes its result.
    GitHub can reject both endpoints for a workflow with zero or entirely
    terminal jobs. That shell must not permanently suppress a replacement.
    """
    for operation in ("cancel", "force-cancel"):
        try:
            api.request("POST", f"repos/{repo}/actions/runs/{run['id']}/{operation}", {})
            return "cancel-requested"
        except APIError as error:
            message = str(error.data.get("message", "")).lower() if isinstance(error.data, dict) else ""
            terminal = all(job["status"] == "completed" for job in jobs)
            if error.status != 409 or not terminal or "not yet queued" not in message:
                raise
    return "uncancellable-terminal-shell"


def cancelled_before_build(api, repo, run):
    """GitHub retains restart evidence, without a separate recovery ledger.

    A cancelled attempt that never acquired the sandbox has no build verdict
    left to replay. Do not add another grace period after a recovery process
    stops between cancellation and replacement dispatch.
    """
    if not ordinary(run, repo) or run.get("conclusion") != "cancelled":
        return False
    jobs = unacquired(api, repo, run)
    return jobs is not None and all(
        job.get("status") == "completed"
        and (job.get("name", "").startswith("pick-runner /")
             or job.get("name") == "sandboxed-build" and not job.get("runner_id") and not job.get("runner_name"))
        for job in jobs
    )


def recover(api, repo, *, apply=False, limit=0, min_age=5400, now=None, api_reserve=0):
    now = dt.datetime.now(dt.timezone.utc).timestamp() if now is None else now
    branch, pulls = inventory(api, repo)

    abandoned, attempted = {}, set()

    def blocking(run, *, fresh=False):
        # New/unqualified runs protect work without extra API calls. Old runs
        # require live attempt and job evidence, including immediately before
        # dispatch so acquisition racing an earlier inventory remains protected.
        proven = run["id"] in abandoned and abandoned[run["id"]] == run.get("run_attempt")
        if not ordinary(run, repo) or not proven and not old(run, now, min_age):
            return True
        if proven and not fresh:
            return False
        try:
            current = api.request("GET", f"repos/{repo}/actions/runs/{run['id']}")
            if current.get("status") == "completed":
                return False
            if (current.get("id") != run["id"] or not ordinary(current, repo)
                    or current.get("run_attempt") != run.get("run_attempt")
                    or not proven and not old(current, now, min_age)
                    or current.get("status") not in ACTIVE):
                return True
            jobs = unacquired(api, repo, current)
            if jobs is None:
                return True
            key = (current["id"], current["run_attempt"])
            if apply and key not in attempted:
                attempted.add(key)
                result = cancel_unacquired(api, repo, current, jobs)
                print(json.dumps(dict(stale_run=current["id"], recovery=result)), flush=True)
            elif apply and not proven:
                return True  # An earlier cancellation had an ambiguous or rejected response.
            abandoned[current["id"]] = current["run_attempt"]
            return False
        except APIError as error:
            print(f"ci-recovery: run {run['id']}: {error}", file=sys.stderr)
            return True

    active, recent = [], {}
    # One repository-wide inventory, independent of worker count. Older live
    # dispatches must also be seen even though their SHA belongs to main.
    for state in ("queued", "in_progress", "waiting", "pending", "requested"):
        # Cancellation changes the paginated live collection. Finish reading
        # its pages first so removing page one cannot skip page two's runs.
        for run in list(runs(api, repo, f"status={state}", include_ignored=True)):
            if quota_reserved(api, api_reserve):
                print("ci-recovery: shared API reserve reached; next pass will resume", file=sys.stderr)
                return []
            if not blocking(run) or run.get("display_title", "").startswith("Ignored label event "):
                continue
            active.append(run)
    since = dt.datetime.fromtimestamp(now - max(86400, min_age), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for event in ("workflow_dispatch", "repository_dispatch"):
        for run in runs(api, repo, f"event={event}&created=%3E%3D{since}"):
            for number in pr_numbers(run):
                if number not in recent or timestamp(run["created_at"]) > timestamp(recent[number]["created_at"]):
                    recent[number] = run

    # Retry the least recently attempted PRs first, so a persistent failure
    # cannot monopolize the next sweep. GitHub's runs provide the retry memory.
    pulls.sort(key=lambda p: timestamp(recent.get(p["number"], {}).get("created_at", p["updatedAt"])))
    selected = []
    for pull in pulls:
        if quota_reserved(api, api_reserve):
            print("ci-recovery: shared API reserve reached; next pass will resume", file=sys.stderr)
            break
        number, head = pull["number"], pull["headRefOid"]
        if any(matches(run, number, head) for run in active):
            continue
        try:
            history = list(runs(api, repo, f"head_sha={head}"))
            if number in recent and matches(recent[number], number, head):
                history.append(recent[number])
            latest = max(history, key=lambda r: timestamp(r.get("run_started_at") or r["created_at"]), default=None)
            if latest and latest["status"] != "completed" and blocking(latest):
                continue
            # Lost events can leave no run at all. Allow startup grace from the
            # PR update in that case instead of stranding it permanently.
            if (now - timestamp(latest["updated_at"] if latest else pull["updatedAt"]) < min_age
                    and (not latest or latest["id"] not in abandoned
                         or abandoned[latest["id"]] != latest.get("run_attempt"))):
                if not latest or not cancelled_before_build(api, repo, latest):
                    continue
            # Recheck immediately before dispatch. Closed PRs, moved heads and
            # results arriving during the inventory must never trigger a retry.
            if not still_missing(api, repo, number, head):
                continue
            # Refresh active runs after the slower inventory/history reads. A
            # manual replacement can be on main's SHA, so a head-only query is insufficient.
            if apply and any(
                matches(run, number, head) and blocking(run, fresh=True)
                for state in ("queued", "in_progress", "waiting", "pending", "requested")
                for run in list(runs(api, repo, f"status={state}"))
            ):
                continue
            if apply and not still_missing(api, repo, number, head):
                continue
        except APIError as error:
            # One inaccessible/deleted PR must not strand the other candidates.
            print(f"ci-recovery: PR #{number}: {error}", file=sys.stderr)
            continue
        if apply:
            # Never repeat an ambiguous dispatch POST. The next sweep sees the
            # replacement run. A rejected dispatch fails the sweep visibly.
            api.request(
                "POST",
                f"repos/{repo}/actions/workflows/pr-build.yml/dispatches",
                {"ref": branch, "inputs": {"pr": str(number)}},
            )
        entry = dict(pr=number, head=head, previous_run=latest["id"] if latest else None, dispatched=apply)
        selected.append(entry)
        print(json.dumps(entry), flush=True)
        if limit and len(selected) >= limit:
            break
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY"), required=not os.environ.get("GITHUB_REPOSITORY")
    )
    parser.add_argument(
        "--apply", action="store_true", help="dispatch trusted builds; otherwise only report candidates"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="optional maximum builds in this sweep; 0 means no count cap"
    )
    parser.add_argument(
        "--min-age-minutes", type=int, default=90, help="allow the existing status replay to finish first"
    )
    parser.add_argument("--api-reserve", type=int, default=0, help="leave this many shared GitHub API requests unused")
    args = parser.parse_args()
    if args.limit < 0 or args.min_age_minutes < 0 or args.api_reserve < 0:
        parser.error("limit, minimum age and API reserve must be nonnegative")
    api = GitHub(max_wait=0)
    try:
        selected = recover(
            api,
            args.repo,
            apply=args.apply,
            limit=args.limit,
            min_age=args.min_age_minutes * 60,
            api_reserve=args.api_reserve,
        )
        print(
            f"ci-recovery: {'dispatched' if args.apply else 'would dispatch'} {len(selected)} build(s)", file=sys.stderr
        )
    except (APIError, Deferred) as error:
        print(f"ci-recovery: {error}; the next scheduled sweep will retry", file=sys.stderr)
        return 1
    finally:
        api.report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
