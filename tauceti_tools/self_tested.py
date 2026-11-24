"""The manual self-tested PR route. Staging still builds and audits every candidate.

Any author may declare a PR self-tested. Ordinary PRs qualify with mathematics-only
diffs. Automatic Mathlib bumps (the review bot's `bump-mathlib/` branches in the base
repository) qualify when the diff touches only the library and the Lake pins, and the
packaged check-bump.sh accepts the pins. A PR build that ran on the current head and
failed removes the label (`red_build`, applied by the PR status labels), for any author.
"""

from __future__ import annotations

import json
import os
import subprocess
from functools import lru_cache

LABEL = "self-tested"
CONTEXT = "self-tested"
DESCRIPTION = "PR CI skipped: self-tested."
DEFAULT_REPO = "TauCetiProject/TauCeti"
EXCLUDED_LABELS = {"full-lint", "lint-repair"}
BUMP_BRANCH = "bump-mathlib/"
BUMP_AUTHOR = "tauceti-review-bot[bot]"  # bump_latest.BOT opens every automatic bump.
PIN_FILES = ("lake-manifest.json", "lean-toolchain")
# PR CI's `build` description for a build cut off before it recorded its tested head
# (pr-build.yml): a cancellation or timeout, which ci_recovery retries, not a verdict.
CUT_OFF_DESCRIPTION = "build not run \u2014 tested H/B/T identity missing or invalid"


def context(number):
    # Commit statuses are shared by PRs with the same head. Keep each assertion PR-bound.
    return f"{CONTEXT}/{int(number)}"


def enabled(repo):
    """Whether `repo` honours the label: TauCeti, or the repository `TAUCETI_SELF_TESTED_REPO` names."""
    return bool(repo) and repo == os.environ.get("TAUCETI_SELF_TESTED_REPO", DEFAULT_REPO)


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


def red_build(state, description=""):
    """Whether a head's `build` status is a completed, failed PR build.

    Such a build removes the label from that head's PR, whoever the author: the label
    skips PR CI, it does not overrule PR CI that ran and failed. A cut-off build is
    infrastructure, not a verdict, and leaves the label in place.
    """
    return (str(state or "").lower() in {"failure", "error"}
            and not str(description or "").startswith(CUT_OFF_DESCRIPTION))


def latest_status(rows, context=CONTEXT):
    rows = [r for r in rows or [] if (r.get("context") or "") == context]
    return max(rows, key=lambda r: r.get("id") or 0, default={})


def accepted(labels, status=None, *, member=None, branch=""):
    """The persistent author declaration is sufficient; old receipt statuses are ignored.

    Callers that grant admission must also validate source scope.
    Review scheduling can use this signal without granting merge authority.
    `member` is ignored: the label counts from any author.
    """
    return requested(labels, branch)


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

    Any author's label counts. Ordinary mathematics needs exact diff paths; the
    automatic-bump exception needs its bump-only paths and the real pin guard.
    `member_read` is ignored.
    """
    branch = (pr.get("head") or {}).get("ref", "")
    if (
        not enabled(repo)
        or pr.get("state") != "open"
        or (pr.get("base") or {}).get("ref") != os.environ.get("TAUCETI_BASE_BRANCH", "main")
        or not requested(pr.get("labels"), branch)
    ):
        return False
    bump = branch.startswith(BUMP_BRANCH)
    if bump and not automatic_bump(pr, repo):
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
