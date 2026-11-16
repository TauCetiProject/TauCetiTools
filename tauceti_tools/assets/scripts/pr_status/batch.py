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

A quota or time-budget hold exits 75 (deferred); genuine errors exit 1.
The next run resumes its pending work; only finishing the scan advances its watermark. A single
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
import os
from pathlib import Path
import threading
import time
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode

import core
import labels
import priority
from github_api import Deferred
from transport import ReconciliationAPI
from tauceti_tools.github_state import Unavailable
from tauceti_tools.git_inputs import PreparationUnavailable
import discovery

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
    waiting = state.setdefault('first_pending', {})
    if not isinstance(waiting, dict):
        state['first_pending'] = {}
    else:
        for number, since in list(waiting.items()):
            try:
                timestamp(since)
                if not isinstance(number, str) or not number.isdigit() or int(number) <= 0:
                    raise ValueError('invalid pending identity')
            except (ValueError, TypeError):
                waiting.pop(number, None)
    # Decision/comparison caches are disposable; malformed cache entries must
    # not discard the retained work queue or authorize an unverified decision.
    def number_key(key):
        return isinstance(key, str) and key.isdigit() and int(key) > 0
    decisions = state.get('decisions', {})
    state['decisions'] = {key: record for key, record in decisions.items()
        if number_key(key) and isinstance(record, dict)
        and re.fullmatch('[0-9a-f]{64}', str(record.get('key', '')))
        and type(record.get('valid_until')) in (int, float) and 0 <= record['valid_until'] < math.inf
        and isinstance(record.get('result'), dict)
        and record['result'].get('category') in {*labels.STATUS_LABELS, None}
        and type(record['result'].get('eligible')) is bool
        and isinstance(record['result'].get('head'), str)} if isinstance(decisions, dict) else {}
    comparisons = state.get('comparisons', {})
    state['comparisons'] = {key: record for key, record in comparisons.items()
        if number_key(key) and isinstance(record, dict) and record.get('repository') == REPO
        and all(re.fullmatch('[0-9a-f]{40}', str(record.get(field, '')))
                for field in ('base', 'head', 'merge_base'))} if isinstance(comparisons, dict) else {}
    discovery_state = state.get('shared_discovery')
    if discovery_state is not None and not (
            isinstance(discovery_state, dict) and type(discovery_state.get('revision')) is int
            and discovery_state['revision'] >= 0 and type(discovery_state.get('repair_after')) is int
            and discovery_state['repair_after'] >= 0
            and re.fullmatch('[0-9a-f]{64}', str(discovery_state.get('policy', '')))
            and isinstance(discovery_state.get('heads'), dict)
            and all(number_key(key) and re.fullmatch('[0-9a-f]{40}', str(head))
                    for key, head in discovery_state['heads'].items())):
        state.pop('shared_discovery', None)
    pending_local = state.setdefault('self_tested_pending', {})
    if not isinstance(pending_local, dict):
        raise ValueError('Invalid self-tested pending inventory')
    for key, record in pending_local.items():
        if (not key.isdigit() or int(key) <= 0 or not isinstance(record, dict)
                or not re.fullmatch(r'[0-9a-f]{40}', record.get('head', ''))):
            raise ValueError('Invalid self-tested pending commit')
        timestamp(record.get('first_seen_at'))
        if 'last_attempt_at' in record:
            timestamp(record['last_attempt_at'])
    blocked = state['blocked_until']
    if type(blocked) not in (int, float) or not 0 <= blocked < math.inf:
        raise ValueError('Invalid quota deadline')
    holds = state.setdefault('quota_holds', [])
    if not isinstance(holds, list):
        raise ValueError('Invalid quota holds')
    for hold in holds:
        if (not isinstance(hold, dict) or not isinstance(hold.get('credential'), str)
                or hold.get('resource') not in {'core', 'graphql', 'search'}
                or type(hold.get('until')) not in (int, float)
                or not 0 <= hold['until'] < math.inf):
            raise ValueError('Invalid quota hold')
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
            for number in numbers:
                state['first_pending'].setdefault(str(number), active['started_at'])
    return state


def save_progress(path, state):
    """Atomically checkpoint pending work, retries and this process's quota hold.

    Merge core's quota deadline into state and atomically replace ``path``.
    A missing path suppresses the file write. Callers must hold the worker lock
    while workers are active. The workflow uploads the file even on failure.
    """
    state['blocked_until'] = max(state['blocked_until'], core._BLOCKED_UNTIL)
    if core.transport is not None:
        state['quota_holds'] = core.transport.holds()
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
    ap.add_argument("--prioritize-self-tested", action="store_true", help="assess pending local assertions before run discovery")
    ap.add_argument("--self-tested-limit", type=int, default=100, help="maximum priority assessments in one batch")
    ap.add_argument("--budget-seconds", type=int, default=2400, help="stop starting work before this batch duration")
    ap.add_argument("--all-open", action="store_true", help="checkpoint a complete open-PR sweep without workflow-history discovery")
    args = ap.parse_args(argv)
    if any(number <= 0 for number in args.pr) or args.self_tested_limit <= 0 or args.budget_seconds <= 0:
        ap.error('PR numbers, self-tested limit and budget must be positive')
    try:
        state = load_progress(args.state_in, args.since)
    except (OSError, ValueError, KeyError, TypeError):
        log('::warning::Invalid PR-status checkpoint; rebuilding from the fallback window')
        state = load_progress(None, args.since)
    output = None if args.dry_run else args.state_out
    previous_transport = core.transport
    core.transport = ReconciliationAPI(state['quota_holds'], decisions=state.setdefault('decisions', {}),
                                       comparisons=state.setdefault('comparisons', {}))
    outcome = {'status': 'error', 'reason': 'batch interrupted'}

    def deferred(reason):
        outcome.update(status='deferred', reason=reason)
        log(f'deferred: {reason}; progress retained')
        return 75

    lock = core.transport.decision_lock
    deadline = time.monotonic() + args.budget_seconds
    priority_observed = False
    try:
        if state['blocked_until'] > time.time():
            return deferred(f"quota hold until epoch {state['blocked_until']}")
        if any(h['until'] > time.time() for h in state['quota_holds']):
            return deferred('persisted credential/resource quota hold')
        if args.prioritize_self_tested:
            state['self_tested_pending'] = priority.scan(REPO, state['self_tested_pending'],
                                                       now=iso(dt.datetime.now(dt.timezone.utc)))
            priority_observed = True
            if state['self_tested_pending']:
                save_progress(output, state)
                ordered = sorted(state['self_tested_pending'],
                                 key=lambda n: (state['self_tested_pending'][n].get('last_attempt_at', ''),
                                                state['self_tested_pending'][n]['first_seen_at'], int(n)))
                if args.dry_run:
                    for number in ordered:
                        log(f"would prioritize self-tested #{number}")
                    outcome.update(status='planned', reason='dry run')
                    return 0
                priority_observed = False
                stop = []

                def assess_local(number):
                    if stop or time.monotonic() >= deadline:
                        return
                    with lock:
                        state['self_tested_pending'][number]['last_attempt_at'] = iso(dt.datetime.now(dt.timezone.utc))
                        save_progress(output, state)
                    try:
                        labels.reconcile(number)
                    except (core.RateLimited, Deferred) as exc:
                        stop.append(str(exc))
                    except Exception as exc:
                        log(f"PR #{number}: priority assessment failed: {exc}")

                with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
                    list(pool.map(assess_local, ordered[:args.self_tested_limit]))
                if stop:
                    log(f"priority assessment stopped: {stop[0]}")
                    return deferred(stop[0])
                state['self_tested_pending'] = priority.scan(REPO, state['self_tested_pending'],
                                                           now=iso(dt.datetime.now(dt.timezone.utc)))
                priority_observed = True
                save_progress(output, state)
                if state['self_tested_pending']:
                    log('self-tested decisions remain; checkpointed before broad run discovery')
                    return deferred('self-tested decisions remain')
        if time.monotonic() >= deadline:
            log('batch time budget reached before broad run discovery')
            return deferred('batch time budget reached before discovery')
        if core.transport.shared_evidence is not None:
            discovered = iso(dt.datetime.now(dt.timezone.utc))
            dirty = discovery.discover(core.transport.shared_evidence.state, state, core.transport.policy,
                                       full=args.all_open and state['active'] is None)
            active = state['active'] or dict(started_at=discovered, pending=[], completed={})
            active['pending'] = list(dict.fromkeys(args.pr + active['pending'] + dirty
                                                  + sorted(map(int, state['retry']))))
            active['started_at'] = discovered
            state['active'] = active
        elif state['active'] is None:
            started = iso(dt.datetime.now(dt.timezone.utc))
            since = iso(dt.datetime.fromisoformat(state['watermark'].replace('Z', '+00:00')) - dt.timedelta(minutes=2))
            heads = open_heads()
            by_update = sorted(n for numbers in heads.values() for n in numbers) if args.all_open else updated_prs(since)
            by_runs = set() if args.all_open else touched_by_runs(since, heads)
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
            by_update = ([] if args.all_open else updated_prs(since, active['completed']))
            by_runs = set() if args.all_open else touched_by_runs(since, heads, active['completed'])
            active['pending'] = list(dict.fromkeys(args.pr + active['pending'] + by_update + sorted(by_runs)))
            # Only advance after both discovery streams were read completely.
            active['started_at'] = discovered
        active = state['active']
        ordered = list(active['pending'])
        for number in ordered:
            state['first_pending'].setdefault(str(number), active['started_at'])
        log(f"scan {active['started_at']}: {len(ordered)} pending PRs; {len(state['retry'])} queued retries")
        if args.dry_run:
            for number in ordered:
                log(f"would reconcile #{number}")
            outcome.update(status='planned', reason='dry run')
            return 0
        save_progress(output, state)
        stop, failures, deferred_evidence = [], [], []

        def one(number):
            if stop:
                return
            if time.monotonic() >= deadline:
                stop.append(('budget', 'batch time budget reached'))
                return
            failed = False
            checked_from = iso(dt.datetime.now(dt.timezone.utc))
            try:
                labels.reconcile(str(number))
            except (core.RateLimited, Deferred) as exc:
                stop.append(('quota', str(exc)))
                return
            except (Unavailable, PreparationUnavailable) as exc:
                deferred_evidence.append(number)
                log(f'PR #{number}: {exc}; pending repair retained')
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
                    state['first_pending'].pop(key, None)
                    if core.transport.shared_evidence is not None:
                        header = core.transport.shared_evidence.headers.get(number)
                        if header is not None:
                            heads = state['shared_discovery']['heads']
                            if header['state'] == 'open':
                                heads[key] = header['head']['sha']
                            else:
                                heads.pop(key, None)
                                state['decisions'].pop(key, None)
                                state['comparisons'].pop(key, None)
                active['completed'][key] = checked_from
                active['pending'].remove(number)
                save_progress(output, state)

        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            list(pool.map(one, ordered))
        if stop:
            log(f"stopping: {stop[0][1]}; {len(active['pending'])} PRs retained")
            if failures:
                outcome.update(status='error', reason='assessment failures alongside deferred work')
                return 1
            return deferred(stop[0][1])
        if deferred_evidence:
            if failures:
                outcome.update(status='error', reason='assessment failures alongside incomplete shared evidence')
                return 1
            return deferred(f'{len(deferred_evidence)} PRs await complete shared evidence')
        # Advance to a completely read discovery boundary, never merely to
        # this workflow run's creation time.
        state['watermark'] = active['started_at']
        state['active'] = None
        log(f"done: {len(ordered)} PRs; failures queued for retry: {failures}")
        outcome.update(status='error' if failures else 'completed', reason='discovery boundary completed')
        return 1 if failures else 0
    except (Deferred, core.RateLimited, Unavailable) as exc:
        return deferred(str(exc))
    finally:
        if args.prioritize_self_tested:
            priority.report(state['self_tested_pending'], observed=priority_observed,
                            now=iso(dt.datetime.now(dt.timezone.utc)))
        try:
            if not args.dry_run:
                state['last_outcome'] = outcome
                save_progress(output, state)
        finally:
            try:
                core.transport.report()
            finally:
                core.transport = previous_transport
        log('pr-status-outcome: ' + json.dumps(outcome, sort_keys=True))
        oldest = min(state['first_pending'].values(), default=None)
        age = max(0, int(time.time()-dt.datetime.fromisoformat(oldest.replace('Z', '+00:00')).timestamp())) if oldest else 0
        log('pr-status-pending: ' + json.dumps(dict(count=len(state['first_pending']), oldest_seconds=age)))
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            pending = len((state.get('active') or {}).get('pending', []))
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
                stream.write(f"\nPR status reconciliation: **{outcome['status']}**. "
                             f"{pending} pending; {len(state['retry'])} queued errors; "
                             f"oldest observed wait {age}s.\n")


if __name__ == "__main__":
    sys.exit(main())
