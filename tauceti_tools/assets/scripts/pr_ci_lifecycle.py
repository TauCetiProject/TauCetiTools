#!/usr/bin/env python3
"""Cancel obsolete ordinary PR builds; staging remains owned by its controller."""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from urllib.parse import parse_qsl, urlencode

from github_api import APIError

from tauceti_tools.self_tested import eligible, requested

ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}


def ordinary(run, repo):
    """Staging belongs to its controller; only ordinary trusted PR workflows qualify."""
    return (
        run.get("path") == ".github/workflows/pr-build.yml"
        and run.get("repository", {}).get("full_name") == repo
        and (
            run.get("event") in {"pull_request_target", "workflow_dispatch"}
            or run.get("event") == "repository_dispatch"
            and bool(pr_numbers(run))
        )
        and not run.get("display_title", "").startswith("bors ")
    )


def pr_numbers(run):
    numbers = {p["number"] for p in run.get("pull_requests", [])}
    # Dispatches have no pull_requests association, and use the default branch's
    # SHA rather than the tested PR head. Include both current and legacy titles.
    match = re.fullmatch(r"pr-build (?:pr=)?([1-9][0-9]*)(?: head=[0-9a-f]{40})?", run.get("display_title", ""))
    if match:
        numbers.add(int(match[1]))
    return numbers


def runs(api, repo, query, *, include_ignored=False):
    """Snapshot every range before mutation, splitting GitHub's 1,000-run cap.

    Splits use inclusive, disjoint second-resolution windows so equal timestamps
    at a page boundary cannot disappear. An unsplittable overflow fails visibly.
    """
    params = dict(parse_qsl(query))
    created = params.pop("created", None)
    lower, upper = None, None
    if created:
        if created.startswith(">="):
            lower = int(dt.datetime.fromisoformat(created[2:].replace("Z", "+00:00")).timestamp())
        elif created.startswith("<"):
            upper = int(dt.datetime.fromisoformat(created[1:].replace("Z", "+00:00")).timestamp()) - 1
        else:
            raise ValueError("unsupported CI inventory date window")
    seen = set()

    def iso(value):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def scan(lo, hi):
        values = dict(params)
        if lo is not None or hi is not None:
            values["created"] = (
                f"{iso(lo)}..{iso(hi)}"
                if lo is not None and hi is not None
                else f">={iso(lo)}"
                if lo is not None
                else f"<={iso(hi)}"
            )
        base = f"repos/{repo}/actions/workflows/pr-build.yml/runs?{urlencode(values)}&per_page=100"
        first = api.request("GET", base + "&page=1")
        count = first.get("total_count", 0)
        if count > 1000:
            rows = first["workflow_runs"]
            if not rows:
                raise RuntimeError("incomplete overflowing CI inventory")
            # Newest first. A finite upper bound prevents fresh arrivals from
            # making recursive scans unbounded.
            high = (
                hi
                if hi is not None
                else max(
                    int(dt.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).timestamp()) for r in rows
                )
            )
            low = lo if lo is not None else 0
            if high <= low:
                raise RuntimeError("more than 1,000 CI runs in a single second; inventory incomplete")
            boundary = (
                min(int(dt.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).timestamp()) for r in rows)
                - 1
            )
            middle = boundary if low <= boundary < high else (low + high) // 2
            yield from scan(middle + 1, high)
            yield from scan(low, middle)
            return
        page, data = 1, first
        while True:
            rows = data["workflow_runs"]
            for row in rows:
                if row["id"] not in seen:
                    seen.add(row["id"])
                    if include_ignored or not row.get("display_title", "").startswith("Ignored label event "):
                        yield row
            if len(rows) < 100:
                return
            if page >= 10:
                # Exactly 1,000 is complete only when GitHub said so.
                if count == 1000:
                    return
                raise RuntimeError("CI inventory reached GitHub's result cap without a complete count")
            page += 1
            data = api.request("GET", base + f"&page={page}")

    yield from scan(lower, upper)


def identity(run):
    """Never mistake a dispatch's workflow SHA for the tested PR head."""
    bound = re.fullmatch(r"pr-build pr=([1-9][0-9]*) head=([0-9a-f]{40})", run.get("display_title", ""))
    numbers = pr_numbers(run)
    if bound:
        number, head = int(bound[1]), bound[2]
        if numbers != {number} or head == "0" * 40:
            return None
        return number, head
    if len(numbers) != 1:
        return None
    number = next(iter(numbers))
    if run.get("event") == "pull_request_target" and re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", "")):
        return number, run["head_sha"]
    # Legacy manual runs still identify their PR, allowing closed-PR cleanup,
    # but cannot prove supersession or head-bound local testing.
    return number, None


def reason(run, pull, *, local=False):
    binding = identity(run)
    if not binding or not pull or pull.get("number") != binding[0]:
        return None
    if pull.get("state") in {"CLOSED", "MERGED"}:
        return "pr-closed"
    if pull.get("state") != "OPEN":
        return None
    current_head = pull.get("headRefOid")
    if not isinstance(current_head, str) or not re.fullmatch(r"[0-9a-f]{40}", current_head):
        return None
    head = binding[1]
    if head and head != current_head:
        return "head-superseded"
    labels = (pull.get("labels") or {}).get("nodes", [])
    if head and head == current_head and local and requested(labels, pull.get("headRefName", "")):
        return "self-tested"
    return None


def local_route(api, repo, pull):
    if not pull or not requested((pull.get("labels") or {}).get("nodes", []), pull.get("headRefName", "")):
        return False
    raw = {
        "number": pull["number"],
        "state": pull["state"].lower(),
        "labels": (pull.get("labels") or {}).get("nodes", []),
        "user": {"login": (pull.get("author") or {}).get("login", "")},
        "base": {"ref": pull.get("baseRefName", "")},
        "head": {
            "sha": pull.get("headRefOid", ""),
            "ref": pull.get("headRefName", ""),
            "repo": {"full_name": (pull.get("headRepository") or {}).get("nameWithOwner", "")},
        },
    }
    return eligible(raw, repo, read=lambda path: api.request("GET", path))


PULL_FIELDS = """number state headRefOid headRefName baseRefName author{login} headRepository{nameWithOwner} labels(first:100){nodes{name} pageInfo{hasNextPage}}
 commits(last:1){nodes{commit{oid status{contexts{context state}}}}}"""


def pulls(api, repo, numbers):
    owner, name = repo.split("/")
    values = {}
    for offset in range(0, len(numbers), 40):
        fields = " ".join(f"p{n}:pullRequest(number:{n}){{{PULL_FIELDS}}}" for n in numbers[offset : offset + 40])
        data = api.graphql(
            "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){" + fields + "}}",
            dict(owner=owner, name=name),
        )["repository"]
        for n in numbers[offset : offset + 40]:
            values[n] = data[f"p{n}"]
    return values


def reserved(api, reserve):
    return (
        any(
            str(b.get("remaining", "")).isdigit() and int(b["remaining"]) < reserve
            for stat in getattr(api, "stats", {}).values()
            for b in stat.get("buckets", {}).values()
        )
        if reserve
        else False
    )


def reconcile(api, repo, *, active=None, apply=False, number=None, api_reserve=0, strict=False):
    # Finish every page before mutating the live collection. Each new pass scans
    # current state; an interrupted or ambiguous POST needs no private ledger.
    if active is None:
        queries = [""]
        if number is not None:
            # PR event path: branch builds plus manual dispatches (whose branch
            # is main). The periodic pass covers every historical branch.
            pull = api.request("GET", f"repos/{repo}/pulls/{number}")
            branch = (pull.get("head") or {}).get("ref")
            if not branch:
                raise RuntimeError("PR branch unavailable for targeted cleanup")
            queries = ["&" + urlencode({"branch": branch}), "&event=workflow_dispatch", "&event=repository_dispatch"]
        active = [r for query in queries for state in sorted(ACTIVE) for r in runs(api, repo, f"status={state}{query}")]
    candidates = {
        r["id"]: r
        for r in active
        if ordinary(r, repo)
        and r.get("status") in ACTIVE
        and identity(r)
        and (number is None or identity(r)[0] == number)
    }
    numbers = sorted({identity(r)[0] for r in candidates.values()})
    snapshots = pulls(api, repo, numbers)
    counts = {"pr-closed": 0, "head-superseded": 0, "self-tested": 0, "deferred": 0}
    results = []
    oldest = 0.0
    observed = 0
    for run in candidates.values():
        if reserved(api, api_reserve):
            print("ci-lifecycle: shared API reserve reached; next pass will resume", file=sys.stderr)
            break
        number = identity(run)[0]
        try:
            initial = snapshots.get(number)
            why = reason(run, initial, local=local_route(api, repo, initial))
        except APIError as error:
            counts["deferred"] += 1
            print(f"ci-lifecycle: run {run['id']}: {error}; next pass will recheck", file=sys.stderr)
            continue
        if not why:
            continue
        observed += 1
        try:
            oldest = max(
                oldest,
                dt.datetime.now(dt.timezone.utc).timestamp()
                - dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).timestamp(),
            )
        except (KeyError, ValueError, TypeError):
            pass
        try:
            current = api.request("GET", f"repos/{repo}/actions/runs/{run['id']}")
            if (
                not ordinary(current, repo)
                or current.get("id") != run["id"]
                or current.get("status") not in ACTIVE
                or type(run.get("run_attempt")) is not int
                or current.get("run_attempt") != run["run_attempt"]
                or identity(current) != identity(run)
            ):
                continue
            # This is the final state read before the cancellation. A reopened
            # PR, restored head or removed label invalidates the earlier decision.
            fresh = pulls(api, repo, [number]).get(number)
            why = reason(current, fresh, local=local_route(api, repo, fresh))
            if not why:
                continue
            if apply:
                api.request("POST", f"repos/{repo}/actions/runs/{run['id']}/cancel", {})
            entry = dict(
                run_id=run["id"],
                attempt=run["run_attempt"],
                pr=number,
                head=identity(run)[1],
                reason=why,
                cancellation_requested=apply,
            )
            counts[why] += 1
            results.append(entry)
            print(json.dumps(entry), flush=True)
        except APIError as error:
            counts["deferred"] += 1
            print(f"ci-lifecycle: run {run['id']}: {error}; next pass will recheck", file=sys.stderr)
    print(
        json.dumps(
            dict(
                ci_lifecycle=counts,
                observed_runs=len(candidates),
                obsolete_observed=observed,
                oldest_obsolete_age_seconds=round(oldest),
                overdue=oldest > 300,
                apply=apply,
            )
        ),
        flush=True,
    )
    if strict and counts["deferred"]:
        raise RuntimeError(f"{counts['deferred']} obsolete CI cancellations deferred; see per-run errors")
    return results
