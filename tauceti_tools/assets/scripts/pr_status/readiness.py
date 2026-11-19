"""Read-only adapter to the exact TauCetiReview gate used by Auto-merge.

Labels and pipeline health call the deployed gate, rather than implementing a
second interpretation of scoreboards. Set TAUCETI_REVIEW_RUNNER to its pinned
checkout's runner directory (the workflows provision it without credentials).
"""

import functools
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

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
SCOPE_TOO_LARGE = ("diff exceeds 2000 added/removed lines or 2000 files \u2014 split into smaller PRs "
                   "to keep review manageable")
SCOPE_UNMEASURED = "could not measure the PR diff from local Git objects \u2014 re-run pr-build"
SCOPE_NEW_FILE = "a newly added .lean file exceeds 1000 lines \u2014 split it (existing files may grow to 1500)"
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
    return [], ""


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
    if verdict["merge"]:
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


def graphql(query, **variables):
    if core.transport is not None:
        data = core.transport.client().graphql(query, variables)
        return data['repository']['pullRequest']
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
    return payload["data"]["repository"]["pullRequest"]


def evidence(number, repo, comments=True):
    if core.transport is not None and core.transport.shared_evidence is not None:
        if core.transport.shared_evidence.state.repository != repo:
            raise RuntimeError('Shared evidence repository identity mismatch')
        return core.transport.shared_evidence.read(number, comments=comments, preliminary=preliminary)
    owner, name = repo.split("/", 1)
    comment_fields = ("comments(first:100, after:$cursor) { nodes { body updatedAt createdAt author { login } } "
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
        rows.extend({"body": c["body"], "user": {"login": (c.get("author") or {}).get("login")}, "updated_at": c["updatedAt"],
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
    cached, key = None, None
    if core.transport is not None and core.transport.shared_evidence is not None:
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
        if cheap is None and requested(current.get("labels"), current["head"].get("ref", "")):
            paths = changed_paths()
            local = (decide_local_route(core.transport.client(), repo, number, pr=current, paths=paths)
                     if core.transport is not None else reconcile_local_route(number, repo, pr=current, paths=paths))
            if local and set(paths) & {"lake-manifest.json", "lean-toolchain"}:
                statuses["bump-guard"] = "success"  # Real inline guard, never a published receipt.
        result = classify(current, comments, statuses, paths or [], merge_base, now=now, self_tested=local)
        if paths is None and result["category"] in {"needs-human-review", "merge-check-failed"}:
            paths = changed_paths()
            result = classify(current, comments, statuses, paths, merge_base, now=now, self_tested=local)
        if cheap is None and requested(current.get("labels"), current["head"].get("ref", "")):
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


def queue_snapshot(repo):
    """Read the queue once per report, using Auto-merge's reservation policy."""
    if time.time() < core._BLOCKED_UNTIL:
        raise core.RateLimited("Queue read skipped after a rate limit")
    sweep = sweep_engine()
    sweep.REPO = repo
    entries = sweep.queue_entries()
    return {"known": True, "numbers": [e["number"] for e in entries],
            "reservation_holder": sweep.reservation_holder(entries, datetime.now(timezone.utc))}


def audit(snapshot):
    """Validate current stages without trusting labels; retain failures as unknown."""
    try:
        engine()
    except RuntimeError as exc:
        # Publish an explicitly unverified report if the policy checkout failed.
        return {"checked_at": int(time.time()), "prs": {},
                "queue": {"known": False, "reason": str(exc)}, "error": str(exc)}
    repo = snapshot["repo"]
    results = {}
    for pr in snapshot["prs"]:
        if pr["state"] != "OPEN" or pr["is_draft"]:
            continue
        number = pr["number"]
        try:
            results[str(number)] = assess(number, repo=repo)
        except (RuntimeError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
            results[str(number)] = {"number": number, "eligible": None,
                                    "category": None, "reason": str(exc)}
    try:
        queue = queue_snapshot(repo)
    except (RuntimeError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        queue = {"known": False, "reason": str(exc)}
    return {"checked_at": int(time.time()), "prs": results, "queue": queue}
