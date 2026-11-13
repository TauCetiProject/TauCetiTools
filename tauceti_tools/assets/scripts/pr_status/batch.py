"""Reconcile the status label of every PR with activity since a point in time.

Usage: batch.py --since <ISO time> [--pr <number>]... [--state-in FILE] [--state-out FILE] [--dry-run]

--since supplies the fallback boundary when no checkpoint is available.

pr-status.yml runs this once per burst of PR events instead of once per event. Its concurrency
group admits one run at a time and keeps only the newest waiting one, so a burst of events costs at
most two runs. A checkpoint preserves pending PRs and the discovery boundary,
covering collapsed events without repeating a completed prefix after an interruption.

A PR has had activity since T if any of these holds:

  - the PR (or its issue: comments, labels, edits) was updated at or after T;
  - a pr-build or Review run for its head was created or updated at or after T (a commit status
    does not move the PR's updated_at, so build results are found through the runs);
  - it was named with --pr (the event that started this run).

For each such PR it reconciles the GitHub lifecycle label from current state.

A rate limit or a listing past GitHub's cap fails
the run. The next run resumes its pending work; only finishing the scan advances its watermark. A single
PR's failure does not: that PR is handed to the next run through --state-out/--state-in and retried
on later passes, so a temporary failure cannot strand a PR or hold healthy PRs back.

Checkpoints contain PR numbers, retry counts, UTC window boundaries and a finite quota
deadline; no PR bodies or credentials. A forced runner loss before artifact upload can
repeat work from the last uploaded checkpoint. Missing state uses the workflow fallback.

Requires authenticated gh and TAUCETI_REVIEW_RUNNER (for labels).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import math
from pathlib import Path
import threading
import time
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode

import core
import labels

REPO = core.REPO
# Runs finish within GitHub's six-hour job limit, so a run that completed after `since` was created
# no earlier than this before it.
RUN_LOOKBACK = dt.timedelta(hours=7)
# PRs reconciled at once. Each takes a few seconds, nearly all of it waiting on GitHub; at
# busy times some fifty PRs are touched in ten minutes, which one at a time would barely keep up with.
# Only different PRs overlap; each label update is checkpointed before proceeding.
PARALLEL = 4


def log(msg):
    print(msg, flush=True)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lines(path: str, jq: str, paginate: bool = False) -> list[str]:
    return [l for l in core.gh_api(path, jq=jq, paginate=paginate).splitlines() if l]


def updated_prs(since: str, completed=None) -> list[int]:
    """PRs whose updated_at is at or after `since`, newest first.
    Paging stops after reaching older activity. ``completed`` maps
    PR numbers to the start times of finished checks; older updates are skipped.
    Same-second updates remain eligible to avoid losing concurrent changes.
    """
    out = []
    page = 1
    while True:
        rows = lines(f"/repos/{REPO}/pulls?state=all&sort=updated&direction=desc&per_page=100&page={page}",
                     jq='.[] | "\\(.number) \\(.updated_at)"')
        done = False
        for row in rows:
            number, updated = row.split()
            if updated < since:
                done = True
                break
            # Skip only updates older than a finished check's start. Writes
            # during the check may cause one extra reconciliation on resume.
            # Same-second updates remain eligible because ordering is unknown.
            if completed and updated < completed.get(number, ''):
                continue
            out.append(int(number))
        if done or len(rows) < 100:
            return out
        page += 1


def open_heads() -> dict[str, list[int]]:
    """Head sha -> the open PRs with that head (usually one; stacked or duplicate PRs can share)."""
    heads: dict[str, list[int]] = {}
    for row in lines(f"/repos/{REPO}/pulls?state=open&per_page=100", jq='.[] | "\\(.head.sha) \\(.number)"',
                     paginate=True):
        sha, number = row.split()
        heads.setdefault(sha, []).append(int(number))
    return heads


def workflow_runs(workflow: str, query: str) -> list[dict]:
    """Read every matching run, splitting saturated listings into creation-time windows.

    Active runs can have been queued long before the checkpoint. Never bound those
    by the recent-activity window: an old run can still finish or paint CI running.
    """
    total = int(core.gh_api(f"/repos/{REPO}/actions/workflows/{workflow}/runs?{query}&per_page=1",
                            jq=".total_count").strip() or 0)
    if total == 0:
        return []
    if total < 1000:
        rows = lines(f"/repos/{REPO}/actions/workflows/{workflow}/runs?{query}&per_page=100",
                     jq='.workflow_runs[] | {head_sha, head_branch, display_title, event, status, created_at, updated_at} | tojson', paginate=True)
        # The listing can grow between the count and pagination. Split rather
        # than accepting a response that might have reached GitHub's cap.
        if len(rows) < 1000:
            return [json.loads(r) for r in rows]

    params = dict(parse_qsl(query))
    created = params.pop('created', None)
    if created is None:
        start = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    elif created.startswith('>='):
        start = dt.datetime.fromisoformat(created[2:].replace('Z', '+00:00'))
        end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    else:
        lower, upper = created.split('..')
        start = dt.datetime.fromisoformat(lower.replace('Z', '+00:00'))
        end = dt.datetime.fromisoformat(upper.replace('Z', '+00:00'))
    seconds = int((end - start).total_seconds())
    if seconds <= 0:
        raise core.RateLimited(f"{workflow} {query}: at least 1,000 runs in one second; cannot split listing")
    middle = start + dt.timedelta(seconds=seconds // 2)
    log(f"{workflow}: splitting saturated run listing ({total} runs)")
    # GitHub's ranges include both ends; use adjacent whole seconds so no run
    # is lost at the boundary or reconciled twice merely because of splitting.
    out = []
    for lower, upper in ((start, middle), (middle + dt.timedelta(seconds=1), end)):
        out.extend(workflow_runs(workflow, urlencode({**params, 'created': f'{iso(lower)}..{iso(upper)}'})))
    return out


ACTIVE = ("requested", "queued", "pending", "waiting", "in_progress")


def touched_by_runs(since: str, heads: dict[str, list[int]], completed=None) -> set[int]:
    """PRs whose pr-build or Review runs moved since `since`.

    ``completed`` filters activity older than the start of a finished PR check.
    """
    prs = set()
    installed = set(lines(f"/repos/{REPO}/actions/workflows?per_page=100",
                          jq=".workflows[].path", paginate=True))
    lookback = iso(dt.datetime.fromisoformat(since.replace("Z", "+00:00")) - RUN_LOOKBACK)
    # A dispatched maintenance build runs from main, so its run head is not the PR head.
    # Discover its PR from the canonical title; this schedules a recheck, never authorizes a merge.
    open_prs = {n for numbers in heads.values() for n in numbers}
    for workflow in ("pr-build.yml", "review.yml"):
        if f".github/workflows/{workflow}" not in installed:
            continue
        for status in ACTIVE + ("completed",):
            # Completed runs by completion (updated_at), not creation: a long build created before
            # `since` may finish after it.
            query = f"status={status}" + (f"&created=>={lookback}" if status == "completed" else "")
            runs = workflow_runs(workflow, query)
            for r in runs:
                if status == "completed" and r["updated_at"] < since:
                    continue
                sha = r["head_sha"]
                numbers = heads.get(sha, [])
                if workflow == "pr-build.yml":
                    event = r.get("event", "pull_request_target")
                    if event not in {"pull_request_target", "workflow_dispatch", "repository_dispatch"}:
                        continue
                    title = r.get("display_title", "")
                    identity = re.fullmatch(r"pr-build pr=([1-9][0-9]*) head=([0-9a-f]{40})", title)
                    if identity:
                        number, sha = int(identity[1]), identity[2]
                        numbers = [number] if number in heads.get(sha, []) else []
                    elif event == "workflow_dispatch":
                        # Legacy manual titles omit H. Recheck the open PR without claiming
                        # that its current head is the one the queued/running job is testing.
                        identity = re.fullmatch(r"pr-build ([1-9][0-9]*)", title)
                        number = int(identity[1]) if identity else None
                        numbers = [number] if number in open_prs else []
                    elif event == "repository_dispatch":
                        # Staging batches and malformed dispatch identities are not PR runs.
                        numbers = []
                prs.update(n for n in numbers
                           if not completed or r['updated_at'] >= completed.get(str(n), ''))
    return prs


def load_progress(path, since):
    """Load a validated checkpoint, or start at the fallback window boundary.

    Record pending PRs, a completed discovery boundary, and the start times of
    finished PR checks. A drained batch advances its watermark to that boundary.
    Resumed batches discover newer activity but skip updates older than the
    start of a finished check, so changes during its reads remain eligible.
    """
    state = {"version": 1, "watermark": since, "active": None, "retry": {}, "blocked_until": 0}
    if path and Path(path).is_file():
        loaded = json.loads(Path(path).read_text())
        # Migrate the former retry-only artifact on the first deployment.
        if isinstance(loaded, dict) and all(k.isdigit() for k in loaded):
            state['retry'] = loaded
        else:
            state = loaded
    if not isinstance(state, dict) or state.get('version') != 1:
        raise ValueError('Invalid progress version')
    def timestamp(value):
        if not isinstance(value, str):
            raise ValueError('Invalid progress timestamp')
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() != dt.timedelta(0):
            raise ValueError('Progress timestamps must be UTC')
    timestamp(state['watermark'])
    blocked = state['blocked_until']
    if type(blocked) not in (int, float) or not 0 <= blocked < math.inf:
        raise ValueError('Invalid quota deadline')
    retry = state['retry']
    if not isinstance(retry, dict) or any(not k.isdigit() or int(k) <= 0 or
            type(v) is not int or v < 1 for k, v in retry.items()):
        raise ValueError('Invalid retry queue')
    active = state['active']
    if active is not None:
        timestamp(active['started_at'])
        completed = active['completed']
        if not isinstance(completed, dict):
            raise ValueError('Invalid completion records')
        for number, finished in completed.items():
            if not number.isdigit() or int(number) <= 0:
                raise ValueError('Invalid completed PR')
            timestamp(finished)
        for field in ('pending',):
            numbers = active[field]
            if not isinstance(numbers, list) or any(type(n) is not int or n <= 0 for n in numbers) or len(set(numbers)) != len(numbers):
                raise ValueError('Invalid pending PRs')
    return state


def save_progress(path, state):
    """Atomically checkpoint pending work, retries and this process's quota hold.

    Merge core's quota deadline into state and atomically replace ``path``.
    A missing path suppresses the file write. Callers must hold the worker lock
    while workers are active. The workflow uploads the file even on failure.
    """
    state['blocked_until'] = max(state['blocked_until'], core._BLOCKED_UNTIL)
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, sort_keys=True))
        temporary.replace(target)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, help="fallback window start, ISO-8601 UTC")
    ap.add_argument("--pr", type=int, action="append", default=[], help="a PR to reconcile regardless")
    ap.add_argument("--dry-run", action="store_true", help="list work; write nothing")
    ap.add_argument("--state-in", help="previous progress/retries JSON checkpoint")
    ap.add_argument("--state-out", help="where to checkpoint progress, including on failure")
    args = ap.parse_args(argv)
    if any(number <= 0 for number in args.pr):
        ap.error('--pr must be a positive PR number')
    try:
        state = load_progress(args.state_in, args.since)
    except (OSError, ValueError, KeyError, TypeError):
        log('::warning::Invalid PR-status checkpoint; rebuilding from the fallback window')
        state = load_progress(None, args.since)
    output = None if args.dry_run else args.state_out
    lock = threading.Lock()
    try:
        if state['blocked_until'] > time.time():
            log(f"deferred until epoch {state['blocked_until']}; pending work retained")
            return 0 if args.dry_run else 1
        if state['active'] is None:
            started = iso(dt.datetime.now(dt.timezone.utc))
            since = iso(dt.datetime.fromisoformat(state['watermark'].replace('Z', '+00:00')) - dt.timedelta(minutes=2))
            heads = open_heads()
            by_update = updated_prs(since)
            by_runs = touched_by_runs(since, heads)
            ordered = list(dict.fromkeys(args.pr + by_update + sorted(by_runs) + sorted(int(n) for n in state['retry'])))
            state['active'] = {"started_at": started, "pending": ordered,
                               "completed": {}}
        else:
            active = state['active']
            discovered = iso(dt.datetime.now(dt.timezone.utc))
            heads = open_heads()
            # Include collapsed events since discovery, but omit activity known
            # to precede the start of an already completed reconciliation.
            since = iso(dt.datetime.fromisoformat(active['started_at'].replace('Z', '+00:00')) - dt.timedelta(minutes=2))
            by_update = updated_prs(since, active['completed'])
            by_runs = touched_by_runs(since, heads, active['completed'])
            active['pending'] = list(dict.fromkeys(args.pr + active['pending'] + by_update + sorted(by_runs)))
            # Only advance after both discovery streams were read completely.
            active['started_at'] = discovered
        active = state['active']
        ordered = list(active['pending'])
        log(f"scan {active['started_at']}: {len(ordered)} pending PRs; {len(state['retry'])} queued retries")
        if args.dry_run:
            for number in ordered:
                log(f"would reconcile #{number}")
            return 0
        save_progress(output, state)
        stop, failures = [], []

        def one(number):
            if stop:
                return
            failed = False
            checked_from = iso(dt.datetime.now(dt.timezone.utc))
            try:
                labels.reconcile(str(number))
            except core.RateLimited as exc:
                stop.append(('quota', str(exc)))
                return
            except Exception as exc:
                failed = True
                log(f"PR #{number}: label reconciliation failed: {exc}")
            with lock:
                key = str(number)
                if failed:
                    failures.append(number)
                    attempt = state['retry'].get(key, 0) + 1
                    state['retry'][key] = attempt
                else:
                    state['retry'].pop(key, None)
                active['completed'][key] = checked_from
                active['pending'].remove(number)
                save_progress(output, state)

        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            list(pool.map(one, ordered))
        if stop:
            log(f"stopping: {stop[0][1]}; {len(active['pending'])} PRs retained")
            return 1
        # Advance to a completely read discovery boundary, never merely to
        # this workflow run's creation time.
        state['watermark'] = active['started_at']
        state['active'] = None
        log(f"done: {len(ordered)} PRs; failures queued for retry: {failures}")
        return 0
    finally:
        if not args.dry_run:
            save_progress(output, state)


if __name__ == "__main__":
    sys.exit(main())
