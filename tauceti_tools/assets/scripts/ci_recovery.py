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
        commits(last: 1) { nodes { commit { oid status { contexts { context } } } } }
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
    pullRequest(number: $pr) { state isDraft headRefOid }
    object(oid: $head) { ... on Commit { status { contexts { context } } } }
  }
}
"""


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def inventory(api, repo):
    owner, name = repo.split("/")
    after, pulls = None, []
    while True:
        data = api.graphql(INVENTORY, dict(owner=owner, name=name, after=after))["repository"]
        page = data["pullRequests"]
        for pull in page["nodes"]:
            commit = pull["commits"]["nodes"][-1]["commit"]
            if not pull["isDraft"] and (
                commit["oid"] != pull["headRefOid"]
                or not any(row["context"] == "build" for row in (commit.get("status") or {}).get("contexts", []))
            ):
                pulls.append(pull)
        if not page["pageInfo"]["hasNextPage"]:
            return data["defaultBranchRef"]["name"], pulls
        after = page["pageInfo"]["endCursor"]


def runs(api, repo, query):
    page = 1
    while True:
        result = api.request(
            "GET", f"repos/{repo}/actions/workflows/pr-build.yml/runs?{query}&per_page=100&page={page}"
        )
        rows = result["workflow_runs"]
        yield from rows
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
        and not any(row["context"] == "build" for row in (commit.get("status") or {}).get("contexts", []))
    )


def recover(api, repo, *, apply=False, limit=20, min_age=5400, now=None):
    now = dt.datetime.now(dt.timezone.utc).timestamp() if now is None else now
    branch, pulls = inventory(api, repo)
    if not pulls:
        return []

    active_prs, active_heads, recent = set(), set(), {}
    # One repository-wide inventory, independent of worker count. Older live
    # dispatches must also be seen even though their SHA belongs to main.
    for state in ("queued", "in_progress", "waiting", "pending", "requested"):
        for run in runs(api, repo, f"status={state}"):
            active_prs.update(pr_numbers(run))
            active_heads.add(run["head_sha"])
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
        number, head = pull["number"], pull["headRefOid"]
        if number in active_prs or head in active_heads:
            continue
        try:
            history = list(runs(api, repo, f"head_sha={head}"))
            if number in recent:
                history.append(recent[number])
            latest = max(history, key=lambda r: timestamp(r.get("run_started_at") or r["created_at"]), default=None)
            if latest and latest["status"] != "completed":
                continue
            # Lost events can leave no run at all. Allow startup grace from the
            # PR update in that case instead of stranding it permanently.
            if now - timestamp(latest["updated_at"] if latest else pull["updatedAt"]) < min_age:
                continue
            # Recheck immediately before dispatch. Closed PRs, moved heads and
            # results arriving during the inventory must never trigger a retry.
            if not still_missing(api, repo, number, head):
                continue
            # Refresh active runs after the slower inventory/history reads. A
            # manual replacement can be on main's SHA, so a head-only query is insufficient.
            if apply and any(
                number in pr_numbers(run) or run["head_sha"] == head
                for state in ("queued", "in_progress", "waiting", "pending", "requested")
                for run in runs(api, repo, f"status={state}")
            ):
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
        if len(selected) >= limit:
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
    parser.add_argument("--limit", type=int, default=20, help="maximum builds to recover in this sweep")
    parser.add_argument(
        "--min-age-minutes", type=int, default=90, help="allow the existing status replay to finish first"
    )
    args = parser.parse_args()
    if args.limit < 1 or args.min_age_minutes < 0:
        parser.error("limit must be positive and minimum age nonnegative")
    api = GitHub(max_wait=0)
    try:
        selected = recover(api, args.repo, apply=args.apply, limit=args.limit, min_age=args.min_age_minutes * 60)
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
