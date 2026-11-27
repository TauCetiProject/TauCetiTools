"""Read-only adapter to the exact TauCetiReview gate used by Auto-merge.

Labels and pipeline health call the deployed gate, rather than implementing a
second interpretation of scoreboards. Set TAUCETI_REVIEW_RUNNER to its pinned
checkout's runner directory (the workflows provision it without credentials).
"""

from concurrent.futures import ThreadPoolExecutor
import functools
from datetime import datetime, timezone
import importlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

import core
from tauceti_tools.survey_facts import deprecation_repair_finished

@functools.lru_cache(maxsize=1)
def engine():
    runner = Path(os.environ.get("TAUCETI_REVIEW_RUNNER", ".tauceti-review/runner")).resolve()
    if not (runner / "merge_from_scoreboard.py").is_file():
        raise RuntimeError("Pinned review engine missing; set TAUCETI_REVIEW_RUNNER")
    sys.path.insert(0, str(runner))
    return importlib.import_module("merge_from_scoreboard")


def sweep_engine():
    engine()
    return importlib.import_module("sweep")


def diff_engine():
    engine()
    return importlib.import_module("pr_diff")


# PR CI's `scope` descriptions for the PR-level diff guards (.github/workflows/pr-build.yml), in
# its order. A self-tested PR skips PR CI, so these are posted for it here, word for word.
SCOPE_TOO_LARGE = ("diff exceeds 3000 added lines, 2000 removed lines or 2000 files \u2014 split into smaller "
                   "PRs to keep review manageable")
SCOPE_UNMEASURED = "could not measure the PR diff from local Git objects \u2014 re-run pr-build"
SCOPE_NEW_FILE = "a newly added .lean file exceeds 1250 lines \u2014 split it (existing files may grow to 1500)"
SCOPE_CONTENT = ("adds binary or non-UTF-8 content, an LFS pointer, a submodule or a diff-hiding "
                 ".gitattributes; see the run")
# The queue's sentence for an unmeasurable diff (TauCetiWorker tauceti_queue/diff_guard.py).
UNMEASURABLE = "could not measure the PR's diff from local Git objects"


def diff_guards(repo, merge_base, head, remote=None):
    """The PR-level diff guards PR CI would have applied: (violations, `scope` description).

    The measure is `ci_git.pr_guards` without path scope on the exact merge base and head, the
    one queue admission and the review engine apply to a PR that skipped PR CI, so all three
    agree. Only the two endpoint trees and the changed blobs are fetched. Unreadable statistics
    fail closed, as there; a Git or host failure defers the assessment.
    """
    import ci_git
    from tauceti_tools import git_inputs
    try:
        with git_inputs.snapshot(remote or 'https://github.com/' + repo, merge_base, head) as (directory, env):
            git_inputs.fetch_changed_blobs(directory, merge_base, head, env)
            with ci_git.repository_reader(directory) as git:
                try:
                    result = ci_git.pr_guards(git, merge_base, head, scope=False)
                except ValueError:
                    return [UNMEASURABLE], SCOPE_UNMEASURED
    except subprocess.SubprocessError as error:
        raise git_inputs.PreparationUnavailable(
            f'PR-level diff guards unavailable ({type(error).__name__})') from error
    if result["too_large"]:
        return result["violations"], SCOPE_TOO_LARGE
    if result["new_files_too_long"]:
        return result["violations"], SCOPE_NEW_FILE
    if result["forbidden_content"]:
        return result["violations"], SCOPE_CONTENT
    return [], ""


def red_build(repo, head, statuses, number=None):
    """Whether `head`'s `build` status is a completed PR build that failed.

    The evidence carries only each context's state, and a build cut off by a cancellation or a
    timeout also reports a failure. So only a failing head pays for more: its status description,
    and the run the status links to, which must have completed without being cancelled. A later
    green build of the same head also clears it (`green_rebuild`).
    """
    from tauceti_tools import self_tested
    if not self_tested.red_build(statuses.get("build", "")):
        return False
    owner, name = repo.split("/", 1)
    data = graphql_data(BUILD_STATUS, owner=owner, name=name, head=head)
    row = ((((data.get("repository") or {}).get("object") or {}).get("status") or {}).get("context")) or {}
    if not self_tested.red_build(row.get("state"), row.get("description")):
        return False
    match = re.search(r"/actions/runs/([1-9][0-9]*)", row.get("targetUrl") or "")
    if not match:
        return False  # Not PR CI's own report.
    run = json.loads(core.gh_api(f"repos/{repo}/actions/runs/{match[1]}", jq=RUN_FIELDS) or "{}")
    if run.get("status") != "completed" or run.get("conclusion") == "cancelled":
        return False
    since = row.get("createdAt") or ""
    if run.get("conclusion") == "success" and built_green(repo, run, since):
        return False  # The reporting run was rerun, and its latest attempt built the head green.
    return not green_rebuild(repo, number, head, since)


RUN_FIELDS = "{id, event, head_sha, display_title, created_at, run_started_at, run_attempt, status, conclusion}"


def built_green(repo, run, since):
    """Whether `run`'s latest attempt started after `since` and its sandboxed build succeeded.

    The report step fails the job whenever the build status it reports is not success, and a
    run that skipped PR CI skips this job."""
    started = run.get("run_started_at") or run.get("created_at") or ""
    if not since or started <= since.replace("+00:00", "Z"):
        return False
    jobs = json.loads(core.gh_api(
        f"repos/{repo}/actions/runs/{run['id']}/attempts/{run.get('run_attempt') or 1}/jobs?per_page=100",
        jq="[.jobs[] | {name, conclusion}]") or "[]")
    return any(j.get("name", "").split(" / ")[-1] == "sandboxed-build" and j.get("conclusion") == "success"
               for j in jobs)


def green_rebuild(repo, number, head, since):
    """Whether a pr-build run of this head, started after its failing status, built it green.

    PR CI does not publish a self-tested head's build status (its intent is obsolete), so a green
    rebuild leaves the red status in place. A run is this head's when it is bound to it: by its
    commit (a pull_request_target run), its `pr-build pr=N head=SHA` title, or, for a workflow or
    repository dispatch titled `pr-build N`, by starting after the failing status of the head the PR still has.
    Listings return successful runs by their latest attempt, so a rerun counts by its own start.
    """
    if not since:
        return False
    stamp = since.replace("+00:00", "Z")
    base = f"repos/{repo}/actions/workflows/pr-build.yml/runs?status=success&per_page=100"

    def listing(query):
        pages = core.gh_api(f"{base}&{query}", jq=f"[.workflow_runs[] | {RUN_FIELDS}]", paginate=True)
        return [run for line in pages.splitlines() if line.strip() for run in json.loads(line)]
    runs = listing(f"head_sha={head}")
    if number is not None:
        # A maintenance dispatch and a human approval's repository_dispatch both build the PR's
        # current head on the default branch's commit, titled `pr-build N`.
        for event in ("workflow_dispatch", "repository_dispatch"):
            runs += listing(f"event={event}&created=%3E%3D{stamp}")
    for run in sorted({r["id"]: r for r in runs}.values(), key=lambda r: -r["id"]):
        target = run.get("event") == "pull_request_target"
        title = run.get("display_title")
        bound = ((target and run.get("head_sha") == head)
                 or (number is not None and title == f"pr-build pr={number} head={head}")
                 or (number is not None and not target and title == f"pr-build {number}"))
        if bound and built_green(repo, run, since):
            return True
    return False


def routing_state(pr):
    labels = {label["name"].lower() for label in pr.get("labels", [])}
    return (pr["head"]["sha"], pr["base"]["ref"], pr["base"].get("sha"), pr["state"], pr.get("draft"),
            labels & (sweep_engine().KEEP_LABELS | {"self-tested", "full-lint", "lint-repair"}))


def meta_merge_base(scoreboard):
    """The merge base a (board, meta) scoreboard pair records having reviewed, or ""."""
    return scoreboard[1].get("merge_base_sha") or ""


def required_rubrics(gate, repo, number):
    """The rubrics PR `number` of `repo` is judged against.

    An engine that is phasing a rubric in asks it only of PRs above a cutoff, and says so through
    `rubrics_for`. An earlier pin asks its one list of every PR."""
    rubrics_for = getattr(gate, "rubrics_for", None)
    return list(rubrics_for(repo, number) if rubrics_for else gate.DEFAULT_RUBRICS)


def preliminary(pr):
    """Reporting states that require no review history, merge base or diff."""
    result = dict(head=pr['head']['sha'], number=pr['number'], eligible=False,
                  category=None, reason='PR is closed', gate=None)
    if pr['state'] != 'open':
        return result
    labels = {label['name'].lower() for label in pr.get('labels', [])}
    if pr.get('draft') or labels & sweep_engine().KEEP_LABELS:
        result.update(category='on-hold', reason='PR is a draft or intentionally held')
    elif pr['base']['ref'] != 'main':
        result.update(category='awaiting-dependency', reason='PR targets another branch, not main')
    elif pr.get('mergeable') is False:
        result.update(category='awaiting-author', reason='GitHub reports a merge conflict with the base')
    else:
        return None
    return result


# The merge queue's GitHub App. Its head-bound recovery requests say it refused this exact head.
QUEUE_LOGINS = frozenset({"tauceti-queue[bot]"})
RECOVERY_PREFIX = "Merge-queue recovery for head `"


def queue_recovery_requested(comments, head):
    """Whether the merge queue refused `head` and handed it back for repair.

    Only the queue's own comments count, and only for the current head: a push makes the
    request stale, and the PR returns to ordinary CI and review."""
    marker = f"<!--tauceti-rebase:v1 {head}-->"
    for comment in comments or []:
        body = comment.get("body") or ""
        if ((comment.get("user") or {}).get("login") in QUEUE_LOGINS and body.startswith(RECOVERY_PREFIX)
                and marker in body.splitlines()):
            return True
    return False


def classify(pr, comments, statuses, paths, merge_base_sha, now=None, self_tested=False, repo=None):
    """Pure evaluation of a fetched PR. Reservations and queue capacity are separate.

    `paths` are the PR's changed paths and `merge_base_sha` the merge base its diff is taken
    from; the gate accepts a scoreboard only for the merge base it reviewed."""
    cheap = preliminary(pr)
    if cheap is not None:
        return cheap
    gate = engine()
    rubrics = required_rubrics(gate, repo or core.REPO, pr["number"])
    head = pr["head"]["sha"]
    result = {"head": head, "number": pr["number"], "eligible": False,
              "category": None, "reason": "", "gate": None}
    verdict = gate.decide_from_comments(
        comments, head, set(rubrics), paths,
        statuses.get("build", ""), statuses.get("bump-guard", ""),
        scope=statuses.get("scope", ""), now=now, merge_base_sha=merge_base_sha,
        **({"self_tested": True} if self_tested else {}))
    result["gate"] = verdict
    result["reason"] = verdict["reason"]
    if verdict["merge"] and queue_recovery_requested(comments, head):
        # Every per-PR prerequisite passes, but the queue already refused this head (a conflict
        # with main, an emptied contribution, a missing source): the author must push a new one.
        result.update(category="awaiting-author",
                      reason="The merge queue refused this head and requested a repair; push a new head")
    elif verdict["merge"]:
        result.update(category="ready-to-merge", eligible=True)
    elif statuses.get("build", "").lower() != "success" and not self_tested:
        result["category"] = ("ci-failed" if statuses.get("build", "").lower()
                              in {"failure", "error"} else "awaiting-CI")
    else:
        latest = gate.latest_current_scoreboard(comments, head)
        if latest is None or latest[1].get("mode") == "init":
            result["category"] = "awaiting-review"
        elif not verdict["review_safe"] and meta_merge_base(latest) != merge_base_sha:
            # The newest review judged a different diff (retargeted PR or rewritten base), so its
            # verdict, blocking or not, says nothing about the current one.
            result["category"] = "awaiting-review"
        elif not verdict["review_safe"]:
            board, meta = latest
            states = meta.get("states")
            if not isinstance(states, dict) or not states:
                states = gate.states_from_table(board.get("body"))
            blocking = any(states.get(r) in {"blocking_request", "blocking_block"}
                           for r in rubrics)
            result["category"] = ("awaiting-author" if blocking else "awaiting-review")
        else:
            paths = set(paths)
            human = not paths or any(not (p.startswith("TauCeti/") or p in gate.DEFAULT_ALLOW)
                                     for p in paths)
            if human:
                result["category"] = "needs-human-review"
            else:
                guards = [statuses.get("scope", "")]
                if paths & {"lake-manifest.json", "lean-toolchain"}:
                    guards.append(statuses.get("bump-guard", ""))
                result["category"] = ("merge-check-failed" if any(g.lower() in {"failure", "error"}
                                                        for g in guards) else "awaiting-CI")
    return result


# Use GraphQL for evidence so the all-open audit does not spend one REST request
# per metadata/comments/status read. Status.contexts contains the latest value of
# each commit-status context; check runs are intentionally not interchangeable.
_FIELDS = """number state isDraft baseRefName baseRefOid headRefOid headRefName mergeable
  author { login } headRepository { nameWithOwner }
  labels(first:100) { nodes { name } pageInfo { hasNextPage } }
  commits(last:1) { nodes { commit { oid status { contexts { context state } } } } }
"""


BUILD_STATUS = """query($owner:String!, $name:String!, $head:GitObjectID!) {
  repository(owner:$owner, name:$name) { object(oid:$head) { ... on Commit {
    status { context(name:"build") { state description targetUrl createdAt } } } } } }"""


def graphql(query, **variables):
    return graphql_data(query, **variables)["repository"]["pullRequest"]


def graphql_data(query, **variables):
    if core.transport is not None:
        return core.transport.client().graphql(query, variables)
    if time.time() < core._BLOCKED_UNTIL:
        raise core.RateLimited("GitHub reads paused after a rate limit")
    cmd = ["gh", "api", "graphql", "-f", "query=" + query]
    for key, value in variables.items():
        cmd += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    out = subprocess.run(cmd, text=True, capture_output=True)
    # GraphQL can report RATE_LIMITED in an HTTP-200 error response.
    try:
        payload = json.loads(out.stdout or "{}")
    except ValueError:
        payload = {}
    errors = payload.get("errors", [])
    if core._rate_limited(out.stderr) or any(e.get("type") == "RATE_LIMITED" for e in errors):
        core._block_for(core.RATE_LIMIT_MAX_WAIT_SECONDS)
        raise core.RateLimited("GitHub rate limited the readiness audit; retry on the next run")
    if out.returncode or errors or not payload.get("data"):
        raise RuntimeError(f"Readiness GraphQL read failed: {out.stderr.strip() or errors}")
    return payload["data"]


def _rest_login(author):
    """GraphQL names an App `tauceti-queue`; REST and the shared evidence call it `tauceti-queue[bot]`."""
    author = author or {}
    login = author.get("login")
    return login + "[bot]" if login and author.get("__typename") == "Bot" else login


def evidence(number, repo, comments=True):
    if core.transport is not None and core.transport.shared_evidence is not None:
        if core.transport.shared_evidence.state.repository != repo:
            raise RuntimeError('Shared evidence repository identity mismatch')
        return core.transport.shared_evidence.read(number, comments=comments, preliminary=preliminary)
    owner, name = repo.split("/", 1)
    comment_fields = ("comments(first:100, after:$cursor) { nodes { body updatedAt createdAt author { login __typename } } "
                      "pageInfo { hasNextPage endCursor } }" if comments else "")
    declaration = ", $cursor:String" if comments else ""
    query = ("query($owner:String!, $name:String!, $number:Int!" + declaration + ") { "
             "repository(owner:$owner,name:$name) { pullRequest(number:$number) { " +
             _FIELDS + comment_fields + " } } rateLimit { cost remaining resetAt } }")
    variables = dict(owner=owner, name=name, number=number)
    rows = []
    first = None
    while True:
        node = graphql(query, **variables)
        if not node or node["labels"]["pageInfo"]["hasNextPage"]:
            raise RuntimeError("Missing PR or truncated labels; refusing readiness")
        if first is None:
            first = node
        elif node["headRefOid"] != first["headRefOid"]:
            raise RuntimeError("PR head moved during comment pagination")
        if not comments:
            break
        rows.extend({"body": c["body"], "user": {"login": _rest_login(c.get("author"))}, "updated_at": c["updatedAt"],
                     "created_at": c["createdAt"]} for c in node["comments"]["nodes"])
        page = node["comments"]["pageInfo"]
        if not page["hasNextPage"]:
            break
        variables["cursor"] = page["endCursor"]
    commit = first["commits"]["nodes"][-1]["commit"]
    if commit["oid"] != first["headRefOid"]:
        raise RuntimeError("Commit statuses do not belong to the current PR head")
    pr = {"number": number, "state": first["state"].lower(), "draft": first["isDraft"],
          "head": {"sha": first["headRefOid"], "ref": first.get("headRefName", ""),
                   "repo": {"full_name": (first.get("headRepository") or {}).get("nameWithOwner", "")}},
          "user": {"login": (first.get("author") or {}).get("login", "")},
          "base": {"ref": first["baseRefName"], "sha": first["baseRefOid"]},
          "labels": first["labels"]["nodes"],
          "mergeable": {"MERGEABLE": True, "CONFLICTING": False}.get(first["mergeable"])}
    statuses = {c["context"]: c["state"].lower()
                for c in (commit["status"] or {}).get("contexts", [])}
    return pr, rows, statuses


def assess(pr, repo=None, now=None):
    """Fetch fresh evidence and bind the reported decision to the current head."""
    repo = repo or core.REPO
    number = int(pr)
    current, comments, statuses = evidence(number, repo)
    head = current["head"]["sha"]
    # Bind the review to the unique base/head comparison, reusing verified
    # commit ancestry when shared evidence is enabled.
    cheap = preliminary(current)
    if cheap is not None:
        merge_base = ''
    elif core.transport is not None and core.transport.git_cache is not None:
        merge_base = core.transport.merge_base(repo, number, current['base']['sha'], head)
    else:
        merge_base = core.gh_api(f"repos/{repo}/compare/{current['base']['sha']}...{head}?per_page=1",
                                 jq=".merge_base_commit.sha").strip()
    if not merge_base and cheap is None:
        raise RuntimeError(f"No merge base for #{number} from the compare API")
    from tauceti_tools import self_tested as declarations
    # The decision key holds status states, not descriptions, so a cut-off build replaced by a
    # real failure would keep its cached answer. A labelled failing head is always reassessed.
    contested = (cheap is None and declarations.LABEL in declarations.label_names(current.get("labels"))
                 and declarations.red_build(statuses.get("build", "")))
    cached, key = None, None
    if core.transport is not None and core.transport.shared_evidence is not None and not contested:
        from shared import decision_key
        from labels import STATUS_LABELS
        key = decision_key(current, comments, statuses, merge_base, core.transport.policy, STATUS_LABELS, repo=repo)
        with core.transport.decision_lock:
            candidate = core.transport.decisions.get(str(number))
            if candidate and candidate.get('key') == key and candidate.get('valid_until', 0) > time.time():
                cached = candidate
                core.transport.cache_hits += 1
    if cached is not None:
        result = dict(cached['result'])
    else:
        from tauceti_tools.self_tested import requested
        from self_tested import reconcile as reconcile_local_route, decide as decide_local_route
        paths = None
        local = False
        def changed_paths():
            if core.transport is not None and core.transport.shared_evidence is not None:
                return core.transport.changed_paths(repo, number, merge_base, head)
            return diff_engine().git_diff(f'https://github.com/{repo}', merge_base, head)
        # A PR build that ran on this head and failed removes the label (labels.py), for any
        # author, so the PR follows the ordinary ci-failed path from this assessment on. That
        # includes a label a full-lint or lint-repair route leaves inert for now.
        drop = contested and red_build(repo, head, statuses, number)
        labelled = not drop and cheap is None and requested(current.get("labels"), current["head"].get("ref", ""))
        if labelled:
            paths = changed_paths()
            local = (decide_local_route(core.transport.client(), repo, number, pr=current, paths=paths)
                     if core.transport is not None else reconcile_local_route(number, repo, pr=current, paths=paths))
            if local and set(paths) & {"lake-manifest.json", "lean-toolchain"}:
                statuses["bump-guard"] = "success"  # Real inline guard, never a published receipt.
        result = classify(current, comments, statuses, paths or [], merge_base, now=now, self_tested=local)
        if paths is None and result["category"] in {"needs-human-review", "merge-check-failed"}:
            paths = changed_paths()
            result = classify(current, comments, statuses, paths, merge_base, now=now, self_tested=local)
        if drop:
            result["drop_self_tested"] = True
        elif labelled:
            # The label alone skips PR CI (pr-build.yml), so apply its diff guards here, labelled
            # as PR CI's would be: an oversized diff is a failed build, a long new file a failed
            # merge check. Admission refuses the same heads with the same sentences.
            violations, scope = diff_guards(repo, merge_base, head)
            if violations:
                result.update(eligible=False, category="merge-check-failed" if scope == SCOPE_NEW_FILE
                              else "ci-failed", reason="PR-level diff guards failed: " + "; ".join(violations))
                if statuses.get("scope") not in {"failure", "error"}:
                    result["scope_failure"] = scope
    after, _, _ = evidence(number, repo, comments=False)
    if routing_state(after) != routing_state(current):
        result.pop("scope_failure", None)  # Measured for the old head; never post it on the new one.
        result.pop("drop_self_tested", None)
        result.update(eligible=False, category="awaiting-CI" if after["state"] == "open" else None,
                      reason="PR changed while collecting merge evidence; awaiting reconciliation",
                      head=after["head"]["sha"])
    elif after.get("mergeable") is False and result["eligible"]:
        result.update(eligible=False, category="awaiting-author",
                      reason="GitHub reports a merge conflict with the base")
    elif (after["state"] == "open" and not after.get("draft")
          and not ({l["name"].lower() for l in after.get("labels", [])} & sweep_engine().KEEP_LABELS)
          and any(l["name"] == "needs-rebase" for l in after.get("labels", []))
          and all(statuses.get(k, "").lower() == "success" for k in ["build", "scope", "bump-guard"])
          and deprecation_repair_finished(comments, head)):
        # Reuse the ordinary status reconciliation after fresh-head CI. No new
        # per-PR workflow, worker protocol, or Bors state is needed for cleanup.
        result["clear_deprecation_request"] = True
    result["checked_at"] = int(time.time()) if now is None else now
    result["labels"] = [label['name'] for label in after.get('labels', [])]
    if key is not None and cached is None and routing_state(after) == routing_state(current):
        from shared import valid_until
        with core.transport.decision_lock:
            core.transport.decisions[str(number)] = dict(key=key, valid_until=valid_until(current, comments, repo),
                                                        result=dict(result))
    return result


# The TauCeti merge queue's status, whose `waiting` inputs are approved but not yet in a batch.
QUEUE_STATUS = "https://queue.taucetiproject.org/status"
TAUCETI_QUEUE_REPO = "TauCetiProject/TauCeti"
QUEUE_STATUS_MAX_BYTES = 16 * 1024 * 1024


def tauceti_queue_numbers():
    """GitHub PR numbers the TauCeti merge queue holds: batch members and approved inputs waiting.

    The project lands through its own queue, not GitHub's native merge queue, so GitHub's
    `mergeQueue` entries are empty and say nothing about what is queued. The bors observation
    lists the GitHub members of every active batch; the queue's status adds the approved inputs
    not yet placed in one. Forgejo inputs are left out: the readiness audit covers GitHub alone.
    """
    from bors_observation import observation  # scripts/, on the path of every caller

    batches = observation().get("batches")
    if not isinstance(batches, list) or not all(
            isinstance(batch, dict) and isinstance(batch.get("members"), list)
            and all(isinstance(m, dict) and type(m.get("pr")) is int for m in batch["members"])
            for batch in batches):
        raise ValueError("unexpected bors observation batch shape")
    members = {member["pr"] for batch in batches for member in batch["members"]}
    request = urllib.request.Request(QUEUE_STATUS, headers={
        "Cache-Control": "no-cache", "User-Agent": "TauCetiReadiness/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(QUEUE_STATUS_MAX_BYTES + 1)
    if len(raw) > QUEUE_STATUS_MAX_BYTES:
        raise ValueError("queue status exceeds bound")
    status = json.loads(raw)
    waiting = status.get("waiting") if isinstance(status, dict) else None
    inputs = waiting.get("inputs") if isinstance(waiting, dict) else None
    if not isinstance(inputs, list) or not all(
            isinstance(i, dict) and type(i.get("number")) is int
            and (i.get("source_identity") is None or isinstance(i["source_identity"], dict))
            for i in inputs):
        raise ValueError("unexpected queue status waiting.inputs shape")

    def forge(item):
        found = (item.get("source_identity") or {}).get("forge", {"kind": "github"})
        if not isinstance(found, dict) or not isinstance(found.get("kind"), str):
            raise ValueError("unexpected queue status source_identity shape")
        return found["kind"]

    return members | {i["number"] for i in inputs if forge(i) == "github"}


def queue_snapshot(repo):
    """Read the queue once per report: TauCeti's own queue, plus GitHub's for Auto-merge's reservation policy."""
    if time.time() < core._BLOCKED_UNTIL:
        raise core.RateLimited("Queue read skipped after a rate limit")
    sweep = sweep_engine()
    sweep.REPO = repo
    entries = sweep.queue_entries()
    try:
        # The TauCeti queue serves one repository; its PR numbers mean nothing for another.
        held = tauceti_queue_numbers() if repo == TAUCETI_QUEUE_REPO else set()
    except (ImportError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Unknown rather than empty: an empty set would report every eligible PR as not queued.
        raise RuntimeError(f"TauCeti queue read failed: {exc}") from exc
    return {"known": True, "numbers": sorted({e["number"] for e in entries} | held),
            "reservation_holder": sweep.reservation_holder(entries, datetime.now(timezone.utc))}


# Pull requests assessed at once. Each assessment is a few sequential reads, so one at a time
# leaves the audit waiting on the network for well over an hour at ~3,000 open pull requests.
# Kept small: GitHub's secondary limits penalise bursts of concurrent requests from one token.
AUDIT_WORKERS = max(1, int(os.environ.get("TAUCETI_READINESS_WORKERS", "4") or "4"))


def _audit_one(number, repo):
    try:
        return assess(number, repo=repo)
    except (RuntimeError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        return {"number": number, "eligible": None, "category": None, "reason": str(exc)}


def audit(snapshot):
    """Validate current stages without trusting labels; retain failures as unknown."""
    try:
        engine()
    except RuntimeError as exc:
        # Publish an explicitly unverified report if the policy checkout failed.
        return {"checked_at": int(time.time()), "prs": {},
                "queue": {"known": False, "reason": str(exc)}, "error": str(exc)}
    repo = snapshot["repo"]
    numbers = [pr["number"] for pr in snapshot["prs"] if pr["state"] == "OPEN" and not pr["is_draft"]]
    # The audit takes minutes, and the queue moves meanwhile. A batch landing during it takes its
    # members out of the queue after they were assessed open and eligible, so the queue is also read
    # before the assessments, and a pull request that read held counts as queued if the queue has
    # landed it since. One withdrawn meanwhile, still open, does not: it needs queueing again.
    started = time.time()
    before = _queue_or_unknown(repo)
    with ThreadPoolExecutor(max_workers=AUDIT_WORKERS) as pool:
        futures = [pool.submit(_audit_one, number, repo) for number in numbers]
        assessed = [future.result() for future in futures]
    results = {str(number): result for number, result in zip(numbers, assessed)}
    queue = _queue_or_unknown(repo)
    if queue["known"] and before["known"]:
        gone = set(before["numbers"]) - set(queue["numbers"])
        if gone:
            try:
                landed = landed_github_numbers_since(started) if repo == TAUCETI_QUEUE_REPO else set()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                landed = set()
                queue["landed_check"] = f"unavailable: {exc}"
            queue["numbers"] = sorted(set(queue["numbers"]) | (gone & landed))
            queue["landed_during_audit"] = sorted(gone & landed)
    return {"checked_at": int(time.time()), "prs": results, "queue": queue}


QUEUE_LANDINGS = "https://queue.taucetiproject.org/statistics/landings?days=1"


def landed_github_numbers_since(moment):
    """GitHub PR numbers the TauCeti queue landed at or after `moment` (unix seconds)."""
    request = urllib.request.Request(QUEUE_LANDINGS, headers={
        "Cache-Control": "no-cache", "User-Agent": "TauCetiReadiness/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(QUEUE_STATUS_MAX_BYTES + 1)
    if len(raw) > QUEUE_STATUS_MAX_BYTES:
        raise ValueError("queue landings exceed bound")
    feed = json.loads(raw)
    entries = feed.get("landings") if isinstance(feed, dict) else None
    if (feed.get("fields") != ["forge", "number", "first_approved_at", "landed_at"]
            or not isinstance(entries, list)
            or not all(isinstance(e, list) and len(e) == 4 and type(e[1]) is int
                       and isinstance(e[3], (int, float)) for e in entries)):
        raise ValueError("unexpected queue landings shape")
    return {e[1] for e in entries if e[0] == "github" and e[3] >= moment}


def _queue_or_unknown(repo):
    try:
        return queue_snapshot(repo)
    except (RuntimeError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        return {"known": False, "reason": str(exc)}
