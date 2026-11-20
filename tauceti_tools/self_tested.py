"""The manual self-tested PR route. Staging still builds and audits every candidate.

Ordinary PRs qualify with mathematics-only diffs from a team member. Automatic
Mathlib bumps (the review bot's `bump-mathlib/` branches in the base repository)
qualify when a team member applied the label, the diff touches only the library
and the Lake pins, and the packaged check-bump.sh accepts the pins.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from functools import lru_cache

LABEL = "self-tested"
CONTEXT = "self-tested"
DESCRIPTION = "PR CI skipped: self-tested."
DEFAULT_TEAM = "TauCetiProject/self-tested"
EXCLUDED_LABELS = {"full-lint", "lint-repair"}
BUMP_BRANCH = "bump-mathlib/"
BUMP_AUTHOR = "tauceti-review-bot[bot]"  # bump_latest.BOT opens every automatic bump.
PIN_FILES = ("lake-manifest.json", "lean-toolchain")


def context(number):
    # Commit statuses are shared by PRs with the same head. Keep each assertion PR-bound.
    return f"{CONTEXT}/{int(number)}"


def team(repo):
    return os.environ.get("TAUCETI_SELF_TESTED_TEAM", DEFAULT_TEAM if repo == "TauCetiProject/TauCeti" else "")


def label_names(labels):
    return {str(x.get("name", "") if isinstance(x, dict) else x).lower() for x in labels or []}


def requested(labels, branch=""):
    """A `bump-mathlib/` branch is requested here, but qualifies only as an `automatic_bump`."""
    names = label_names(labels)
    return LABEL in names and not names & EXCLUDED_LABELS and not branch.startswith("lint-repair/")


def automatic_bump(pr, repo):
    """The review bot's own Mathlib bump PR, pushed to a branch of the base repository."""
    head = pr.get("head") or {}
    return bool(
        str(head.get("ref") or "").startswith(BUMP_BRANCH)
        and repo
        and (head.get("repo") or {}).get("full_name") == repo
        and (pr.get("user") or {}).get("login") == BUMP_AUTHOR
    )


def latest_status(rows, context=CONTEXT):
    rows = [r for r in rows or [] if (r.get("context") or "") == context]
    return max(rows, key=lambda r: r.get("id") or 0, default={})


def accepted(labels, status=None, *, member=False, branch=""):
    """The persistent author declaration is sufficient; old receipt statuses are ignored.

    Callers that grant admission must also validate membership and source scope.
    Review scheduling can use this signal without granting merge authority.
    """
    return bool(member and requested(labels, branch))


def gh_read(path, *, token=None):
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
        env.pop("GITHUB_TOKEN", None)
    result = subprocess.run(["gh", "api", path], env=env, capture_output=True, text=True, timeout=30)
    if result.returncode:
        error = RuntimeError("self-tested GitHub observation unavailable")
        if "HTTP 404" in result.stderr:
            error.status = 404
        raise error
    return json.loads(result.stdout)


_MEMBERS = {}
# How long a cached answer serves callers that pass cache=True. Admission keeps this short so a
# removal from the team takes effect quickly; a one-off report (the readiness audit) extends it to
# its whole run, because it asks about the same few authors for thousands of pull requests.
MEMBERSHIP_CACHE_SECONDS = 60.0


def membership(repo, author, read=gh_read, *, strict=False, cache=False):
    configured = team(repo)
    if not configured or not author:
        return False
    key = configured, author
    if cache and key in _MEMBERS and time.monotonic() - _MEMBERS[key][0] < MEMBERSHIP_CACHE_SECONDS:
        return _MEMBERS[key][1]
    try:
        org, slug = configured.split("/", 1)
        path = f"orgs/{org}/teams/{slug}/memberships/{author}"
        token = os.environ.get("SELF_TESTED_TEAM_TOKEN") or os.environ.get("TAUCETI_TEAM_TOKEN")
        value = (gh_read(path, token=token) if token else read(path)).get("state") == "active"
        if cache:
            _MEMBERS[key] = time.monotonic(), value
        return value
    except (
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        if getattr(error, "status", None) == 404:
            # A definite answer (not a member, or not visible to this token), so cache it too.
            if cache:
                _MEMBERS[key] = time.monotonic(), False
            return False
        if strict:
            raise
        return False  # Unknown membership never grants admission.


def items(read, path):
    page = 1
    while True:
        rows = read(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
        if not isinstance(rows, list):
            raise RuntimeError("incomplete self-tested source evidence")
        yield from rows
        if len(rows) < 100:
            return
        page += 1


class _ReadAPI:
    def __init__(self, read):
        self.read = read

    def request(self, method, path):
        if method != "GET":
            raise ValueError("self-tested eligibility is read-only")
        return self.read(path)

    def items(self, path):
        return list(items(self.read, path))


@lru_cache(maxsize=1)
def _bump_policy():
    from tauceti_tools.templates import import_shared

    return import_shared("self_tested.py", "_self_tested_bump_guard")


def eligible(pr, repo, *, paths=None, read=gh_read, member_read=None):
    """Validate the current persistent declaration inline; no statuses or writes.

    Ordinary mathematics uses the author's team membership and exact diff paths.
    The automatic-bump exception still validates its labeller and real pin guard.
    Membership is cached for at most one minute within a consumer process.
    """
    branch = (pr.get("head") or {}).get("ref", "")
    if (
        not team(repo)
        or pr.get("state") != "open"
        or (pr.get("base") or {}).get("ref") != os.environ.get("TAUCETI_BASE_BRANCH", "main")
        or not requested(pr.get("labels"), branch)
    ):
        return False
    bump = branch.startswith(BUMP_BRANCH)
    author = (pr.get("user") or {}).get("login", "")
    if bump:
        if not automatic_bump(pr, repo):
            return False
        events = list(items(read, f"repos/{repo}/issues/{pr['number']}/events"))
        labels = [e for e in events if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == LABEL]
        if not labels:
            return False
        author = (max(labels, key=lambda e: e.get("id", 0)).get("actor") or {}).get("login", "")
    if not membership(repo, author, member_read or read, strict=True, cache=True):
        return False
    if paths is None:
        files = list(items(read, f"repos/{repo}/pulls/{pr['number']}/files"))
        if pr.get("changed_files") is not None and len(files) != pr["changed_files"]:
            raise RuntimeError("incomplete self-tested diff")
        paths = [f["filename"] for f in files]
        paths.extend(f.get("previous_filename", "") for f in files if f.get("status") == "renamed")
    if not (allowed_bump_paths(paths) if bump else allowed_paths(paths)):
        return False
    if bump:
        verdict, _ = _bump_policy().check_bump(_ReadAPI(read), repo, pr["head"]["sha"])
        if verdict is None:
            raise RuntimeError("bump-guard evidence unavailable")
        return verdict
    return True


def observe(repo, number, head, read=gh_read):
    """Read current PR evidence directly; no Actions receipt or label history."""
    try:
        pr = read(f"repos/{repo}/pulls/{number}")
        return pr.get("head", {}).get("sha") == head and eligible(pr, repo, read=read)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError, subprocess.SubprocessError):
        return False


def allowed_paths(paths, prefix="TauCeti/"):
    return bool(paths) and all(p.startswith(prefix) and "\n" not in p and "\r" not in p for p in paths)


def allowed_bump_paths(paths, prefix="TauCeti/"):
    """Library paths plus the Lake pins, changing at least one pin. Lakefiles never qualify."""
    return bool(set(paths) & set(PIN_FILES)) and all(
        (p.startswith(prefix) or p in PIN_FILES) and "\n" not in p and "\r" not in p for p in paths
    )
