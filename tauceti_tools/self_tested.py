"""The manual self-tested PR route. Staging still builds and audits every candidate."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request

LABEL = "self-tested"
CONTEXT = "self-tested"
DESCRIPTION = "PR CI skipped: self-tested."
DEFAULT_TEAM = "TauCetiProject/self-tested"
EXCLUDED_LABELS = {"full-lint", "lint-repair"}


def context(number):
    # Commit statuses are shared by PRs with the same head. Keep each assertion PR-bound.
    return f"{CONTEXT}/{int(number)}"


def team(repo):
    return os.environ.get("TAUCETI_SELF_TESTED_TEAM", DEFAULT_TEAM if repo == "TauCetiProject/TauCeti" else "")


def label_names(labels):
    return {str(x.get("name", "") if isinstance(x, dict) else x).lower() for x in labels or []}


def requested(labels, branch=""):
    names = label_names(labels)
    return LABEL in names and not names & EXCLUDED_LABELS and not branch.startswith(("bump-mathlib/", "lint-repair/"))


def latest_status(rows, context=CONTEXT):
    rows = [r for r in rows or [] if (r.get("context") or "") == context]
    return max(rows, key=lambda r: r.get("id") or 0, default={})


def accepted(labels, status, *, member=False, branch=""):
    """`status` is the latest dedicated status on the current head, never a build result."""
    return bool(member and requested(labels, branch) and (status or "").lower() == "success")


def gh_read(path):
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("self-tested GitHub observation unavailable")
    return json.loads(result.stdout)


def membership(repo, author, read=gh_read):
    configured = team(repo)
    if not configured or not author:
        return False
    try:
        org, slug = configured.split("/", 1)
        return read(f"orgs/{org}/teams/{slug}/memberships/{author}").get("state") == "active"
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError, subprocess.SubprocessError):
        return False  # Ordinary CI remains available when org membership cannot be read.


def observe(repo, number, head, read=gh_read):
    """Consume the trusted picker's PR/head-bound policy result and current label.

    The picker and existing label reconciliation resolve team membership. Consumers
    use that result just as they use trusted scope/build statuses; no second App
    permission or independently cached team roster is needed.
    """
    if not team(repo):
        return False
    try:
        pr = read(f"repos/{repo}/pulls/{number}")
        if (
            pr.get("state") != "open"
            or pr.get("head", {}).get("sha") != head
            or pr.get("base", {}).get("ref") != os.environ.get("TAUCETI_BASE_BRANCH", "main")
            or not requested(pr.get("labels"), pr.get("head", {}).get("ref", ""))
        ):
            return False
        rows = read(f"repos/{repo}/commits/{head}/status?per_page=100").get("statuses", [])
        status = latest_status(rows, context(number)).get("state", "")
        return accepted(
            pr.get("labels"),
            status,
            member=True,
            branch=pr.get("head", {}).get("ref", ""),
        )
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError, subprocess.SubprocessError):
        return False


def allowed_paths(paths, prefix="TauCeti/"):
    return bool(paths) and all(p.startswith(prefix) and "\n" not in p and "\r" not in p for p in paths)


def in_staging(repo, number, head):
    """Use the queue's existing immutable member snapshot; unknown means ordinary CI."""
    url = os.environ.get(
        "TAUCETI_BORS_URL", "https://bors.taucetiproject.org" if repo == "TauCetiProject/TauCeti" else ""
    )
    repository_id = os.environ.get("TAUCETI_BORS_REPOSITORY_ID", "1" if repo == "TauCetiProject/TauCeti" else "")
    if not url or not repository_id:
        return False
    try:
        request = urllib.request.Request(
            f"{url.rstrip('/')}/repositories/{repository_id}/active-batches?base=main",
            headers={"Cache-Control": "no-cache", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
        if (
            data.get("schema") != "tauceti-bors.observation/v1"
            or data.get("repo") != repo
            or data.get("base") != "main"
        ):
            return False
        return any(
            m.get("pr") == number and m.get("head_sha") == head
            for batch in data.get("batches", [])
            if batch.get("state") in {"waiting", "running"}
            for m in batch.get("members", [])
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False
