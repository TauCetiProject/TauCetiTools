#!/usr/bin/env python3
"""Bound queued fleet startup using another attempt of the SAME workflow run.

Using rerun instead of repository_dispatch preserves bors's unique staging-run
identity. The picker skips machines whose earlier attempt never acquired a runner.
Force-cancel bypasses always() finalizers; failed recovery publishes a fenced
infrastructure verdict. PR retries require the original head to remain current.
"""

import os
import re
import sys
import time
import urllib.error
from http.client import HTTPException

from github_api import APIError, Deferred, GitHub

TITLE = re.compile(
    r"^bors branch=[^ ]+ batch=[1-9][0-9]* head=(?P<head>[0-9a-f]{40}) base=[0-9a-f]{40}$"
)

PR_TITLE = re.compile(r"^pr-build pr=(?P<pr>[1-9][0-9]*) head=(?P<head>[0-9a-f]{40})$")

_client = None


def api(path, data=None):
    """Revalidate reads cheaply, and only retry definitely rejected mutations."""
    global _client
    if _client is None:
        _client = GitHub(max_wait=180, jitter=2)
    return _client.request("GET" if data is None else "POST",
                           "repos/TauCetiProject/TauCeti" + path, data,
                           conditional=data is None,
                           idempotent=path.startswith("/statuses/"))


def eligible(run, *, policy=False):
    """Accept immutable build identities from the trusted repository and workflow."""
    staging = run.get("event") == "repository_dispatch" and bool(TITLE.fullmatch(run.get("display_title", "")))
    pr = policy and run.get("event") in ("pull_request_target", "repository_dispatch") and bool(PR_TITLE.fullmatch(run.get("display_title", "")))
    attempt = run.get("run_attempt")
    return ((staging or pr) and type(attempt) is int and attempt >= 1
            and (policy or attempt == 1) and run.get("path") == ".github/workflows/pr-build.yml"
            and run.get("repository", {}).get("full_name") == "TauCetiProject/TauCeti")



def build_job(data):
    """Return the unique sandbox job, or None before it appears.

    Truncated or ambiguous observations raise ValueError and cannot authorize
    cancellation. At most one complete page of jobs is accepted.
    """
    jobs = data["jobs"]
    if data["total_count"] != len(jobs) or len(jobs) > 100:
        raise ValueError("incomplete job observation")
    found = [j for j in jobs if j["name"] == "sandboxed-build"]
    if len(found) > 1:
        raise ValueError("ambiguous sandbox job")
    return found[0] if found else None


def recover(
    run_id, *, fetch=api, clock=time.monotonic, sleep=time.sleep, policy=False
):
    """Bound first-attempt fleet acquisition and request a same-run Namespace retry.

    Read observations until a fleet job acquires a runner or waits two minutes.
    Only a never-acquired job is force-canceled. Once terminal, request exactly
    one rerun; re-read after an ambiguous response instead of repeating mutation.
    A failed handoff posts a failure on the verified staging head so bors can
    recover. Acquired jobs retain normal reporting. Incomplete evidence before
    cancellation cannot authorize mutation; unconfirmed handoff raises an error.
    """
    operation = fetch

    def fetch(path, data=None):
        # Reads are safe to retry. Never repeat a mutation with a lost reply.
        for attempt in range(3):
            try:
                return operation(path) if data is None else operation(path, data)
            except (OSError, HTTPException):
                if data is not None or attempt == 2:
                    raise
                sleep(5 * (attempt + 1))

    run = fetch(f"/actions/runs/{run_id}")
    if not eligible(run, policy=policy):
        return "not-an-initial-bors-attempt"
    try:
        return wait_for_fleet(run_id, fetch=fetch, clock=clock, sleep=sleep, run=run, policy=policy)
    except Deferred:
        raise  # Do not spend an exhausted credential on failure-status probes.
    except (OSError, HTTPException, ValueError, KeyError, TypeError, RuntimeError):
        failure_status(run_id, run, fetch=fetch, policy=policy)
        raise


def wait_for_fleet(run_id, *, fetch, clock, sleep, run, policy=False):
    """Allow seven minutes for placement, then two full minutes for acquisition.

    GitHub's job created_at can precede dependency completion. Only the first
    observed fleet labels start the monotonic acquisition timer.
    """
    attempt = run["run_attempt"]
    deadline = clock() + 420
    placed = False
    next_run_read = clock() + 30
    latest = run
    while clock() < deadline:
        # Job observations carry placement/acquisition evidence. Run-attempt
        # polling needs only one read per 30s, plus a fresh fence before cancel.
        if clock() >= next_run_read:
            latest = fetch(f"/actions/runs/{run_id}")
            next_run_read = clock() + 30
        if latest.get("run_attempt") != attempt or latest.get("status") == "completed":
            return "already-terminal-or-retried"
        job = build_job(fetch(f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"))
        if job:
            labels = job.get("labels", [])
            if labels and not {"tauceti-fleet", "tauceti-fleet-pr", "tauceti-fleet-staging"}.intersection(labels):
                return "cloud-selected" if policy else "namespace-selected"
            if job.get("runner_name") or job.get("runner_id") or job["status"] in ("in_progress", "completed"):
                return "runner-acquired"
            if labels and not placed:
                placed = True
                deadline = clock() + 120
        sleep(10)
    if not placed:
        raise TimeoutError("Picker placement was not observable within seven minutes")
    return handoff(run_id, run, fetch=fetch, clock=clock, sleep=sleep, policy=policy)


def handoff(run_id, run, *, fetch, clock, sleep, policy=False):
    """Force-stop one verified attempt and reconcile its single rerun request.

    The caller owns terminal failure reporting if any observation or mutation
    fails. No mutation is repeated after an ambiguous HTTP result.
    """
    attempt = run["run_attempt"]
    latest = fetch(f"/actions/runs/{run_id}")
    if latest.get("run_attempt") != attempt or latest.get("status") == "completed":
        return "already-terminal-or-retried"
    # Confirm PR metadata is readable before cancelling; repeat the head guard
    # immediately before rerun to detect pushes during cancellation.
    current_pr_head(run, fetch)
    # Mutations are not automatically retried after an ambiguous HTTP failure.
    try:
        fetch(f"/actions/runs/{run_id}/force-cancel", {})
    except (urllib.error.HTTPError, APIError) as error:
        code = getattr(error, "code", getattr(error, "status", 0))
        if 400 <= code < 500 and code not in {409, 429}:
            # Definite rejection: ordinary cancellation still has a finalizer
            # and cannot silently strand bors.
            fetch(f"/actions/runs/{run_id}/cancel", {})
        # A conflict or server error may mean it was accepted: reconcile.
    except (OSError, HTTPException):
        pass  # The server may have accepted it: reconcile before further action.
    deadline = clock() + 120
    while clock() < deadline:
        latest = fetch(f"/actions/runs/{run_id}")
        if latest.get("run_attempt") != attempt:
            return "another-recovery-already-advanced-attempt"
        if latest["status"] == "completed":
            if latest.get("conclusion") != "cancelled":
                return "completed-during-cancellation"
            final_job = build_job(
                fetch(f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
            )
            if not final_job or final_job.get("runner_name") or final_job.get("runner_id"):
                # A real build raced acquisition; normal bors recovery owns it.
                failure_status(run_id, run, fetch=fetch, policy=policy)
                return "runner-acquired-during-cancellation"
            if PR_TITLE.fullmatch(run.get("display_title", "")) and not current_pr_head(run, fetch):
                return "superseded-pr-cancelled"
            try:
                fetch(f"/actions/runs/{run_id}/rerun", {})
                return "rerun-with-updated-placement-requested" if policy else "rerun-on-namespace-requested"
            except (OSError, HTTPException, APIError):
                # POST may have succeeded despite a lost response. Observe the
                # successor before deciding to hand recovery back to bors.
                deadline = clock() + 60
                while clock() < deadline:
                    if fetch(f"/actions/runs/{run_id}").get("run_attempt", 0) > attempt:
                        return "rerun-with-updated-placement-observed" if policy else "rerun-on-namespace-observed"
                    sleep(5)
                raise RuntimeError("Namespace retry not confirmed; bors infrastructure recovery requested") from None
        sleep(5)
    raise TimeoutError("Force cancellation not confirmed; bors infrastructure recovery requested")


def current_pr_head(run, fetch):
    match = PR_TITLE.fullmatch(run.get("display_title", ""))
    return not match or fetch(f"/pulls/{match.group('pr')}")["head"]["sha"] == match.group("head")


def failure_status(run_id, original, *, fetch=api, policy=False):
    """Report failed handoff only for the verified original attempt.

    A successor attempt must retain ownership of its statuses. This is a
    best-effort GET/POST fence, matching the finalizer's run-attempt check.
    """
    latest = fetch(f"/actions/runs/{run_id}")
    if (not eligible(latest, policy=policy) or latest.get("run_attempt") != original.get("run_attempt")
            or latest["display_title"] != original["display_title"]):
        return
    match = TITLE.fullmatch(original["display_title"]) or PR_TITLE.fullmatch(original["display_title"])
    try:
        if not current_pr_head(original, fetch):
            return
    except (OSError, HTTPException, ValueError, KeyError, TypeError, RuntimeError):
        return  # Unknown current head cannot authorize status publication.
    head = match.group("head")
    fetch(f"/statuses/{head}", dict(
        context="build", state="failure", description="fleet startup recovery failed; retry this build",
        target_url=f"https://github.com/TauCetiProject/TauCeti/actions/runs/{run_id}",
    ))


def main():
    try:
        run_id = int(os.environ["FLEET_RUN_ID"])
        if run_id <= 0:
            raise ValueError("run id")
        print(recover(run_id, policy=os.environ.get("FLEET_ROUTING_POLICY_ENABLED") == "true"))
        return 0
    except (OSError, HTTPException, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(
            "::error::Fleet startup recovery incomplete: "
            + type(exc).__name__
            + "; inspect the original workflow run",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
