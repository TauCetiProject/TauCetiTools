#!/usr/bin/env python3
"""Recover abandoned PR builds through the ordinary default-branch workflow."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from github_api import APIError, Deferred, GitHub
from pr_ci_lifecycle import ACTIVE, ordinary, pr_numbers, reconcile, reserved, runs

INVENTORY = """
query($owner: String!, $name: String!, $after: String) {
  rateLimit { cost }
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    pullRequests(states: OPEN, first: 100, after: $after,
                 orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes {
        number headRefOid isDraft updatedAt createdAt
        labels(first: 100) { nodes { name } }
        commits(last: 1) { nodes { commit { oid committedDate status { contexts { context state targetUrl createdAt description } } } } }
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
    object(oid: $head) { ... on Commit { status { contexts { context state targetUrl } } } }
  }
}
"""


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def labels(pull):
    return {label["name"].lower() for label in (pull.get("labels") or {}).get("nodes", [])}


def self_tested(pull):
    """Whether the `self-tested` label routes this PR around PR CI, from any author.

    The existing PR assessment owns the routing label. Do not cancel a
    label-event run or dispatch a build while it makes that decision.
    """
    names = labels(pull)
    return "self-tested" in names and not names & {"full-lint", "lint-repair"}


def build_status(commit):
    """The head's `build` status row with a final state, or None."""
    rows = (commit.get("status") or {}).get("contexts", [])
    return next(
        (row for row in rows
         if row["context"] == "build" and row.get("state", "").lower() in {"success", "failure", "error"}),
        None,
    )


def has_preflight(pull, commit):
    return build_status(commit) is not None or self_tested(pull)


def status_run(row):
    """The Actions run a `build` status links to, or None."""
    match = re.search(r"/actions/runs/([1-9][0-9]*)", (row or {}).get("targetUrl") or "")
    return int(match[1]) if match else None


def failed_run(commit):
    """The run whose failing `build` status is the head's current verdict, or None."""
    row = build_status(commit)
    return status_run(row) if row and row["state"].lower() in {"failure", "error"} else None


def verdict(pull):
    """The head, run and posting time of the head's failing `build` status: a rerun of the same run
    posts a new status, so it is a new verdict. None without a failing status."""
    commit = pull["commits"]["nodes"][-1]["commit"]
    run_id = failed_run(commit)
    return None if run_id is None else dict(head=pull["headRefOid"], run=run_id, posted=build_status(commit).get("createdAt"))


def inventory(api, repo, observed=None):
    """Non-draft PRs needing a build; `observed` also collects every consistent non-draft head."""
    owner, name = repo.split("/")
    after, pulls = None, []
    while True:
        data = api.graphql(INVENTORY, dict(owner=owner, name=name, after=after))["repository"]
        page = data["pullRequests"]
        for pull in page["nodes"]:
            commit = pull["commits"]["nodes"][-1]["commit"]
            if pull["isDraft"]:
                continue
            if commit["oid"] != pull["headRefOid"] or not has_preflight(pull, commit):
                pulls.append(pull)
            if observed is not None and commit["oid"] == pull["headRefOid"]:
                observed.append(pull)
        if not page["pageInfo"]["hasNextPage"]:
            return data["defaultBranchRef"]["name"], pulls
        after = page["pageInfo"]["endCursor"]



def matches(run, number, head):
    """An obsolete, explicitly bound PR head cannot reserve a newer head."""
    bound = re.fullmatch(r"pr-build pr=([1-9][0-9]*) head=([0-9a-f]{40})", run.get("display_title", ""))
    if bound:
        return int(bound[1]) == number and bound[2] == head
    if run.get("event") == "pull_request_target":
        return run["head_sha"] == head
    return number in pr_numbers(run) or run["head_sha"] == head


def still_missing(api, repo, number, head, interrupted=None):
    """The head still lacks a build, or (`interrupted`: a run id) its verdict is still that run's failure."""
    owner, name = repo.split("/")
    data = api.graphql(CURRENT, dict(owner=owner, name=name, pr=number, head=head))["repository"]
    pull, commit = data["pullRequest"], data["object"]
    if not (pull and commit and pull["state"] == "OPEN" and not pull["isDraft"] and pull["headRefOid"] == head):
        return False
    if interrupted is not None:
        return not self_tested({**pull, "number": number}) and failed_run(commit) == interrupted
    return not has_preflight({**pull, "number": number}, commit)


def quota_reserved(api, reserve, budget=0):
    """Leave the shared installation's remaining budget for essential queue operations.

    `budget` caps this pass's own requests (0: no cap), so the sweeper's share of the shared
    installation is fixed however much the inventory grows.
    """
    return reserved(api, reserve, budget)



def old(run, now, min_age):
    """A new attempt or a recent transition receives the full acquisition grace."""
    try:
        times = [timestamp(run[k]) for k in ("created_at", "updated_at", "run_started_at") if run.get(k)]
        return bool(times) and now - max(times) >= min_age
    except (ValueError, TypeError, AttributeError):
        return False


def unacquired(api, repo, run):
    """Require complete terminal-job evidence; real pending jobs keep their place."""
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
        or job.get("status") != "completed"
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
            orphan = "not yet queued" in message or "not been queued yet" in message
            if error.status != 409 or not terminal or not orphan:
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
             or job.get("name", "").split(" / ")[-1] == "pick-runner"
             or job.get("name", "").split(" / ")[-1] == "sandboxed-build"
             and not job.get("runner_id") and not job.get("runner_name"))
        for job in jobs
    )


# The `build` description PR CI's report step posts when its build was cut off before the tested
# head was recorded (pr-build.yml). A label event's `skip_build` dispatch cancels an in-flight build
# through the per-PR concurrency group this way, so such a head can wait any length of time.
CUT_OFF_DESCRIPTION = "build not run — tested H/B/T identity missing or invalid"
# How far back the hourly pass lists cancelled runs. Every interruption is seen by many passes
# within it; the report's memory keeps what an earlier pass learned about a head after that.
INTERRUPTED_WINDOW = 2 * 86400
# A head is stuck once it has gone this long after its last push without CI or a final state.
STUCK_AFTER = 3 * 3600
# A stuck period survives a missed pass or two, not a sweeper that stopped running.
STUCK_STREAK_GAP = 3 * 3600
STUCK_SCHEMA = "tauceti.ci-recovery.stuck-heads/v1"
# Labels that already give a head a clear next mover. `ci-failed` is not one: on a head without a
# `build` status it is left over from an earlier head, and on an interrupted build it describes the
# infrastructure failure that left the head stuck.
FINAL_LABELS = frozenset({"awaiting-author", "needs-human-review", "merge-conflict", "hold", "wip", "keep", "on-hold"})


def interrupted(api, repo, run):
    """The run's build was cut off rather than failed: its `sandboxed-build` job acquired a runner
    and ended `cancelled`, which is how GitHub ends a job at its six-hour timeout."""
    if not ordinary(run, repo) or run.get("status") != "completed" or run.get("conclusion") != "cancelled":
        return False
    jobs = unacquired(api, repo, run)
    return jobs is not None and any(
        job.get("name", "").split(" / ")[-1] == "sandboxed-build" and job.get("conclusion") == "cancelled"
        and (job.get("runner_id") or job.get("runner_name"))
        for job in jobs
    )


def retried(run):
    """An interrupted run is itself the head's retry when a dispatch or a rerun produced it."""
    return run.get("event") != "pull_request_target" or (run.get("run_attempt") or 1) > 1


def interrupted_heads(api, repo, observed, now, memory, api_reserve=0, api_budget=0):
    """Split the open heads whose current `build` failure is an interrupted run into those owed one
    automatic retry and those whose retry is spent. Also report whether every candidate was
    classified before the pass budget ran out.

    `memory` (from the previous report) remembers, per PR, the head and interrupted run already
    retried (`retried`) or found to be an interrupted retry itself (`exhausted`); it holds while the
    head's status still names that run. Otherwise one listing of recently cancelled pr-build runs
    finds the candidates, and each costs one more request (its jobs).
    """
    candidates, exhausted = {}, {}
    for pull in observed:
        run_id = failed_run(pull["commits"]["nodes"][-1]["commit"])
        if run_id is None or self_tested(pull):
            continue
        key, current = str(pull["number"]), verdict(pull)
        known = [memory.get(kind, {}).get(key) or {} for kind in ("retried", "exhausted")]
        if any({k: entry.get(k) for k in current} == current for entry in known):
            exhausted[pull["number"]] = run_id
        else:
            candidates.setdefault(run_id, []).append(pull)
    if not candidates:
        return [], exhausted, True
    if quota_reserved(api, api_reserve, api_budget):
        return [], exhausted, False
    since = dt.datetime.fromtimestamp(now - INTERRUPTED_WINDOW, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    retry, complete, listed = [], True, set()

    def classify(run):
        nonlocal complete
        try:
            if not interrupted(api, repo, run):
                return
        except APIError as error:
            print(f"ci-recovery: run {run['id']}: {error}", file=sys.stderr)
            complete = False
            return
        for pull in candidates[run["id"]]:
            if retried(run):
                exhausted[pull["number"]] = run["id"]
            else:
                retry.append(dict(pull, interrupted=run))

    # Lazily paginated: the budget is checked before each further page or jobs read.
    for run in runs(api, repo, f"status=cancelled&created=%3E%3D{since}", include_ignored=True):
        if quota_reserved(api, api_reserve, api_budget):
            return retry, exhausted, False
        listed.add(run["id"])
        if run["id"] in candidates:
            classify(run)
    # A head whose build was cut off before the listing window still owes its one retry: read its
    # run directly. Only the cut-off description qualifies, so real failures cost nothing here.
    for run_id, pulls in candidates.items():
        if run_id in listed or not any(cut_off_build(p) for p in pulls):
            continue
        if quota_reserved(api, api_reserve, api_budget):
            return retry, exhausted, False
        try:
            run = api.request("GET", f"repos/{repo}/actions/runs/{run_id}")
        except APIError as error:
            print(f"ci-recovery: run {run_id}: {error}", file=sys.stderr)
            complete = False
            continue
        if not isinstance(run, dict) or run.get("id") != run_id:
            continue
        if quota_reserved(api, api_reserve, api_budget):
            return retry, exhausted, False  # Its jobs read waits for the next pass.
        classify(run)
    return retry, exhausted, complete


def cut_off_build(pull):
    """Whether the head's failing `build` status is PR CI's report of a build cut off mid-run."""
    row = build_status(pull["commits"]["nodes"][-1]["commit"]) or {}
    return row.get("state", "").lower() == "failure" and (row.get("description") or "").startswith(CUT_OFF_DESCRIPTION)


def last_push(pull):
    """When the head arrived, approximately: its commit time, or the PR's creation if that is later.
    (An old commit pushed later looks older; the queue's alert waits six hours, which absorbs that.)"""
    commit = pull["commits"]["nodes"][-1]["commit"]
    times = []
    for value in (commit.get("committedDate"), pull.get("createdAt")):
        try:
            times.append(timestamp(value))
        except (ValueError, TypeError, AttributeError):
            pass
    return max(times, default=None)


def stuck_heads(observed, active, exhausted, now):
    """Open heads that, three hours after their last push, have neither CI nor a final state: no live
    pr-build, and no `build` status or an interrupted build whose one retry is spent."""
    heads = []
    for pull in observed:
        number, head = pull["number"], pull["headRefOid"]
        pushed = last_push(pull)
        if self_tested(pull) or labels(pull) & FINAL_LABELS or pushed is None or now - pushed < STUCK_AFTER:
            continue
        if any(run.get("status") in ACTIVE and matches(run, number, head) for run in active):
            continue
        commit = pull["commits"]["nodes"][-1]["commit"]
        if number in exhausted:
            entry = dict(reason="retry-interrupted", run=exhausted[number])
        elif build_status(commit) is None:
            entry = dict(reason="no-ci", run=None)
        else:
            continue
        heads.append(dict(pr=number, head=head, last_push_at=pushed, **entry))
    heads.sort(key=lambda h: (h["last_push_at"], h["pr"]))
    return heads


def read_stuck_report(path, repo):
    """The previous report, or an empty one."""
    try:
        previous = json.loads(Path(path).expanduser().read_text())
        if previous.get("schema") == STUCK_SCHEMA and previous.get("repo") == repo:
            return previous
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {}


# How long the retry memory keeps a PR it no longer sees open and ready (closed, or a draft).
MEMORY_DAYS = 14


def remember(previous, observed, exhausted, dispatched, now):
    """The retry memory to keep. An entry holds while its PR's head still has the same verdict; a PR
    the pass did not observe keeps its entry for `MEMORY_DAYS`, so a draft round trip forgets nothing."""
    current = {str(p["number"]): verdict(p) for p in observed}
    memory = {}
    for kind in ("retried", "exhausted"):
        kept = {}
        for key, entry in ((previous.get("memory") or {}).get(kind) or {}).items():
            if not isinstance(entry, dict) or type(entry.get("seen")) not in (int, float):
                continue
            if key in current:
                if current[key] and {k: entry.get(k) for k in current[key]} == current[key]:
                    kept[key] = dict(entry, seen=now)
            elif now - entry["seen"] <= MEMORY_DAYS * 86400:
                kept[key] = entry
        memory[kind] = kept
    for number in exhausted:
        if str(number) not in memory["retried"] and current.get(str(number)):
            memory["exhausted"][str(number)] = dict(current[str(number)], seen=now)
    for number, pull in dispatched:
        memory["retried"][str(number)] = dict(verdict(pull), seen=now)
    return memory


def write_stuck_report(path, repo, heads, now, complete, previous=None, memory=None):
    """Replace the report the queue's status and alert read, keeping the start of a continuing stuck
    period. A report that cannot be written is logged (`written` is False); recovery goes on."""
    path = Path(path).expanduser()
    previous = read_stuck_report(path, repo) if previous is None else previous
    since = None
    if heads:
        since = now
        observed = previous.get("observed_at")
        if (previous.get("heads") and type(previous.get("stuck_since")) in (int, float)
                and type(observed) in (int, float) and 0 <= now - observed <= STUCK_STREAK_GAP):
            since = min(previous["stuck_since"], now)
    report = dict(schema=STUCK_SCHEMA, repo=repo, observed_at=now, complete=complete, stuck_since=since, heads=heads,
                  memory=memory if memory is not None else previous.get("memory") or {})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(report, sort_keys=True) + "\n")
        os.replace(temporary, path)
        report["written"] = True
    except OSError as error:
        print(f"ci-recovery: stuck-head report {path}: {error}", file=sys.stderr)
        report["written"] = False
    return report


def report_stuck(heads, now):
    """One journal line per stuck head, then the count and the oldest's age."""
    for head in heads:
        print(json.dumps(dict(stuck_head=head["pr"], head=head["head"], reason=head["reason"],
                              hours=round((now - head["last_push_at"]) / 3600, 1), run=head["run"])), flush=True)
    oldest = f", oldest {int((now - heads[0]['last_push_at']) // 3600)}h" if heads else ""
    print(f"ci-recovery: stuck heads: {len(heads)}{oldest}", file=sys.stderr)


def recover(api, repo, *, apply=False, limit=0, min_age=5400, now=None, api_reserve=0, api_budget=0, cleanup=True,
            stuck_report=None):
    now = dt.datetime.now(dt.timezone.utc).timestamp() if now is None else now
    observed = []
    branch, pulls = inventory(api, repo, observed)

    abandoned, attempted = {}, set()

    def blocking(run, *, fresh=False):
        # New/unqualified runs protect work without extra API calls. Old runs
        # require live attempt and job evidence, including immediately before
        # dispatch so acquisition racing an earlier inventory remains protected.
        proven = run["id"] in abandoned and abandoned[run["id"]] == run.get("run_attempt")
        if (run.get("status") in {"pending", "waiting"} or not ordinary(run, repo)
                or not proven and not old(run, now, min_age)):
            return True
        if proven and not fresh:
            return False
        try:
            current = api.request("GET", f"repos/{repo}/actions/runs/{run['id']}")
            if current.get("status") == "completed":
                return False
            if (current.get("id") != run["id"] or not ordinary(current, repo)
                    or current.get("status") in {"pending", "waiting"}
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

    active, all_active, recent = [], [], {}
    # One repository-wide inventory, independent of worker count. Older live
    # dispatches must also be seen even though their SHA belongs to main.
    for state in ("queued", "in_progress", "waiting", "pending", "requested"):
        # Cancellation changes the paginated live collection. Finish reading
        # its pages first so removing page one cannot skip page two's runs.
        for run in list(runs(api, repo, f"status={state}", include_ignored=True)):
            all_active.append(run)
            if run.get("display_title", "").startswith("Ignored label event "):
                continue
            active.append(run)
    if cleanup:
        reconcile(api, repo, active=all_active, apply=apply, api_reserve=api_reserve, api_budget=api_budget)
    since = dt.datetime.fromtimestamp(now - max(86400, min_age), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pass_started = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for event in ("workflow_dispatch", "repository_dispatch"):
        for run in runs(api, repo, f"event={event}&created=%3E%3D{since}"):
            for number in pr_numbers(run):
                if number not in recent or timestamp(run["created_at"]) > timestamp(recent[number]["created_at"]):
                    recent[number] = run

    # An interrupted build gets one automatic retry; a head whose retry was interrupted too, or that
    # has had no CI for three hours, is reported (before any dispatch can fail the pass).
    previous = read_stuck_report(stuck_report, repo) if stuck_report else {}
    retry, exhausted, complete = interrupted_heads(
        api, repo, observed, now, previous.get("memory") or {}, api_reserve, api_budget
    )
    heads = stuck_heads(observed, active, exhausted, now)
    report_stuck(heads, now)
    retries = []
    if stuck_report:
        write_stuck_report(stuck_report, repo, heads, now, complete, previous,
                           remember(previous, observed, exhausted, retries, now))
        # The report is the retry memory: without it, an interrupted build is reported, never retried.
        pulls.extend(retry)

    # Retry the least recently attempted PRs first, so a persistent failure
    # cannot monopolize the next sweep. GitHub's runs provide the retry memory.
    pulls.sort(key=lambda p: timestamp(recent.get(p["number"], {}).get("created_at", p["updatedAt"])))
    selected = []
    for pull in pulls:
        if quota_reserved(api, api_reserve, api_budget):
            print("ci-recovery: shared API reserve or pass budget reached; next pass will resume", file=sys.stderr)
            break
        number, head = pull["number"], pull["headRefOid"]
        cut_off = (pull.get("interrupted") or {}).get("id")
        if any(matches(run, number, head) and blocking(run) for run in active):
            continue
        try:
            history = list(runs(api, repo, f"head_sha={head}"))
            if number in recent and matches(recent[number], number, head):
                history.append(recent[number])
            latest = max(history, key=lambda r: timestamp(r.get("run_started_at") or r["created_at"]), default=None)
            if latest and latest["status"] != "completed" and blocking(latest):
                continue
            # Lost events can leave no run at all. Allow startup grace from the
            # PR update in that case instead of stranding it permanently. An
            # interrupted run's verdict is already final; it needs no grace.
            if not (cut_off and latest and latest["id"] == cut_off) and (
                    now - timestamp(latest["updated_at"] if latest else pull["updatedAt"]) < min_age
                    and (not latest or latest["id"] not in abandoned
                         or abandoned[latest["id"]] != latest.get("run_attempt"))):
                if not latest or not cancelled_before_build(api, repo, latest):
                    continue
            # Recheck immediately before dispatch. Closed PRs, moved heads and
            # results arriving during the inventory must never trigger a retry.
            if not still_missing(api, repo, number, head, cut_off):
                continue
            if apply:
                # Refresh this immutable head, and known manual attempts that
                # could have been rerun. Then look for new dispatches on main's
                # SHA since this pass began. This preserves the final live
                # fence without rescanning every old queued PR for every PR.
                fresh = list(runs(api, repo, f"head_sha={head}"))
                known = {r["id"]: r for r in [*history, *active] if matches(r, number, head)}
                for run in known.values():
                    fresh.append(api.request("GET", f"repos/{repo}/actions/runs/{run['id']}"))
                for event in ("workflow_dispatch", "repository_dispatch"):
                    fresh.extend(runs(api, repo, f"event={event}&created=%3E%3D{pass_started}"))
                if any(run.get("status") in ACTIVE and matches(run, number, head) and blocking(run, fresh=True)
                       for run in fresh):
                    continue
            if apply and not still_missing(api, repo, number, head, cut_off):
                continue
        except APIError as error:
            # One inaccessible/deleted PR must not strand the other candidates.
            print(f"ci-recovery: PR #{number}: {error}", file=sys.stderr)
            continue
        if apply and cut_off:
            # The retry is spent once requested, whether or not its build ever reports. Record it
            # first; a retry that cannot be remembered is not made.
            memory = remember(previous, observed, exhausted, [*retries, (number, pull)], now)
            if not write_stuck_report(stuck_report, repo, heads, now, complete, previous, memory)["written"]:
                continue
            retries.append((number, pull))
        if apply:
            # Never repeat an ambiguous dispatch POST. The next sweep sees the
            # replacement run. A rejected dispatch fails the sweep visibly.
            api.request(
                "POST",
                f"repos/{repo}/actions/workflows/pr-build.yml/dispatches",
                {"ref": branch, "inputs": {"pr": str(number)}},
            )
        entry = dict(pr=number, head=head, previous_run=latest["id"] if latest else None, dispatched=apply)
        if cut_off:
            entry["retry_of_interrupted"] = cut_off
        selected.append(entry)
        print(json.dumps(entry), flush=True)
        if limit and len(selected) >= limit:
            break
    # Useful current-head recovery comes first. Ancient uncancellable shells
    # must not consume the pass's API budget before missing builds can dispatch.
    for run in all_active:
        if quota_reserved(api, api_reserve, api_budget):
            print("ci-recovery: cleanup reached the shared API reserve or pass budget; next pass will resume", file=sys.stderr)
            break
        if (not run.get("display_title", "").startswith("Ignored label event ")
                and any(matches(run, pull["number"], pull["headRefOid"]) for pull in pulls)):
            continue
        blocking(run)
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
    parser.add_argument(
        "--api-budget", type=int, default=0, help="stop this pass after this many GitHub API requests; 0 means no cap"
    )
    parser.add_argument(
        "--stuck-report",
        default=os.environ.get("TAUCETI_CI_STUCK_REPORT") or None,
        help="write the stuck-head report the merge queue's status and alert read (default: $TAUCETI_CI_STUCK_REPORT)",
    )
    parser.add_argument("--cleanup-only", action="store_true", help="cancel obsolete PR CI without dispatching builds")
    parser.add_argument("--pr", type=int, help="restrict cleanup to a PR; implies --cleanup-only")
    args = parser.parse_args()
    if (
        min(args.limit, args.min_age_minutes, args.api_reserve, args.api_budget) < 0
        or args.pr is not None
        and args.pr < 1
    ):
        parser.error("limit, minimum age, API reserve and budget must be nonnegative; PR number must be positive")
    api = GitHub(max_wait=0)
    try:
        if args.cleanup_only or args.pr is not None:
            reconcile(
                api,
                args.repo,
                apply=args.apply,
                number=args.pr,
                api_reserve=args.api_reserve,
                api_budget=args.api_budget,
                strict=True,
            )
            return 0
        selected = recover(
            api,
            args.repo,
            apply=args.apply,
            limit=args.limit,
            min_age=args.min_age_minutes * 60,
            api_reserve=args.api_reserve,
            api_budget=args.api_budget,
            stuck_report=args.stuck_report,
        )
        print(
            f"ci-recovery: {'dispatched' if args.apply else 'would dispatch'} {len(selected)} build(s)", file=sys.stderr
        )
    except (APIError, Deferred, RuntimeError) as error:
        print(f"ci-recovery: {error}; the next scheduled sweep will retry", file=sys.stderr)
        return 1
    finally:
        api.report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
