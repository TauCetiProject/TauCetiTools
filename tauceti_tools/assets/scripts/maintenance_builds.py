#!/usr/bin/env python3
"""Rebuild bot maintenance PRs abandoned without a required build status.

This requests ordinary trusted CI; it never changes a branch, posts a success,
or retries a proof failure. Completed red builds belong to repair workers.
"""

import argparse
import datetime as dt
import re

from github_api import GitHub
from pr_status.core import KEEP_LABELS, automatic_bump

REPO = "TauCetiProject/TauCeti"
GRACE = dt.timedelta(minutes=30)
COOLDOWN = dt.timedelta(hours=6)


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def eligible(pr):
    return (pr.get("state") == "open" and not pr.get("draft")
            and pr["base"]["ref"] == "main"
            and not KEEP_LABELS.intersection(label["name"].lower() for label in pr.get("labels", []))
            and (automatic_bump(pr, repo=REPO) or (
                pr["user"]["login"] == "tauceti-review-bot[bot]"
                and pr["head"]["repo"]["full_name"] == REPO
                and pr["head"]["ref"] == "lint-repair/main")))


def manual_runs(client, now):
    # Manual run.head_sha identifies main, not the PR it builds. Match the
    # workflow's fixed run-name instead, including requests still queued.
    cutoff = (now - COOLDOWN).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = []
    # A run can remain queued longer than the cooldown. Its age never
    # authorizes displacing it with another run in the same concurrency group.
    for status in ("queued", "in_progress", "waiting", "pending", "requested"):
        data = client.request("GET", f"repos/{REPO}/actions/workflows/pr-build.yml/runs"
                              f"?event=workflow_dispatch&status={status}&per_page=100")
        if data["total_count"] > len(data["workflow_runs"]):
            raise RuntimeError("Active maintenance build history truncated")
        result.extend(data["workflow_runs"])
    for page in range(1, 11):
        data = client.request("GET", f"repos/{REPO}/actions/workflows/pr-build.yml/runs"
                              f"?event=workflow_dispatch&created=%3E{cutoff}&per_page=100&page={page}")
        if data["total_count"] > 1000:
            raise RuntimeError("Maintenance rebuild history truncated; refusing duplicate dispatch")
        result.extend(data["workflow_runs"])
        if len(data["workflow_runs"]) < 100:
            return result
    raise RuntimeError("Maintenance rebuild history truncated")


def recover(client, *, dry_run=False, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    candidates = []
    # Two narrow branch listings avoid scanning the ordinary PR backlog.
    for branch in ("bump-mathlib/", "lint-repair/main"):
        if branch.endswith("/"):
            # The target SHA is part of the bump branch name. GitHub's PR search
            # narrows discovery; re-read every selected PR before trusting it.
            data = client.request("GET", f"search/issues?q=repo:{REPO}+is:pr+is:open+head:bump-mathlib/&per_page=100")
            if data.get("incomplete_results") or data["total_count"] > len(data["items"]):
                raise RuntimeError("Maintenance PR discovery truncated")
            candidates.extend(item["number"] for item in data["items"])
        else:
            candidates.extend(p["number"] for p in client.items(
                f"repos/{REPO}/pulls?state=open&head=TauCetiProject:{branch}"))
    recent = None
    requested = []
    for number in sorted(set(candidates)):
        pr = client.request("GET", f"repos/{REPO}/pulls/{number}")
        if not eligible(pr):
            continue
        head = pr["head"]["sha"]
        if not re.fullmatch("[0-9a-f]{40}", head):
            raise ValueError("Invalid maintenance head")
        statuses = client.request("GET", f"repos/{REPO}/commits/{head}/statuses?per_page=100")
        if any(status["context"] == "build" for status in statuses):
            continue
        commit = client.request("GET", f"repos/{REPO}/git/commits/{head}")
        if now - timestamp(commit["committer"]["date"]) < GRACE:
            continue
        runs = client.request("GET", f"repos/{REPO}/actions/workflows/pr-build.yml/runs?head_sha={head}&per_page=100")
        if runs["total_count"] > len(runs["workflow_runs"]):
            raise RuntimeError("Maintenance head build history truncated")
        if any(run["status"] != "completed" or now - timestamp(run["updated_at"]) < GRACE
               for run in runs["workflow_runs"]):
            continue
        if recent is None:
            recent = manual_runs(client, now)
        if any(run.get("display_title") == f"pr-build {number}" for run in recent):
            continue
        fresh = client.request("GET", f"repos/{REPO}/pulls/{number}")
        if not eligible(fresh) or fresh["head"]["sha"] != head:
            continue
        print(f"{'Would rebuild' if dry_run else 'Rebuilding'} maintenance PR #{number} at {head}", flush=True)
        if not dry_run:
            client.request("POST", f"repos/{REPO}/actions/workflows/pr-build.yml/dispatches",
                           {"ref": "main", "inputs": {"pr": str(number), "maintenance": "true"}})
        requested.append(number)
    return requested


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    client = GitHub(max_wait=0)
    try:
        recover(client, dry_run=args.dry_run)
    finally:
        client.report()
