#!/usr/bin/env python3
"""Decide the PR route before allocating an expensive runner. Never runs candidate code."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path

from github_api import APIError, Deferred, GitHub

from tauceti_tools.self_tested import (
    BUMP_BRANCH,
    DESCRIPTION,
    PIN_FILES,
    allowed_bump_paths,
    allowed_paths,
    automatic_bump,
    in_staging,
    label_names,
    latest_status,
    membership,
    requested,
)
from tauceti_tools.self_tested import (
    context as assertion_context_name,
)


def recent_label(api, repo, number):
    import time
    from datetime import datetime, timezone

    events = api.items(f"repos/{repo}/issues/{number}/events?per_page=100")
    labelled = [
        e for e in events if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == "self-tested"
    ]
    if not labelled:
        return False
    latest = max(e.get("created_at", "") for e in labelled)
    at = datetime.fromisoformat(latest.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    return time.time() - at < 300


def labeller(api, repo, number):
    """The actor of the latest `self-tested` labeled event, or None when unreadable."""
    try:
        events = api.items(f"repos/{repo}/issues/{number}/events?per_page=100")
        labelled = [
            e for e in events if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == "self-tested"
        ]
        if not labelled:
            return None
        latest = max(labelled, key=lambda e: (e.get("created_at") or "", e.get("id") or 0))
        return (latest.get("actor") or {}).get("login") or None
    except (APIError, Deferred, OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        return None  # Ordinary CI remains available when the label's author cannot be read.


# Only these files feed check-bump.sh; candidate code is never fetched or run.
GUARD_FILES = ("lakefile.toml", "lakefile.lean", *PIN_FILES)


def fetch_guard_files(api, repo, sha, directory):
    for name in GUARD_FILES:
        try:
            data = api.request("GET", f"repos/{repo}/contents/{name}?ref={sha}")
        except APIError as error:
            if error.status == 404:
                continue  # A missing file stays absent, as check-bump.sh expects.
            raise
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
            raise RuntimeError(f"{name} at {sha} is not a readable file")
        (directory / name).write_bytes(base64.b64decode(data.get("content") or ""))


def check_bump(api, repo, head):
    """Run the packaged check-bump.sh on main's tip, the merge base and the head.

    Returns (verdict, output): True or False is check-bump.sh's verdict, None means
    its inputs could not be read. Each directory holds only the four Lake files.
    """
    try:
        base = api.request("GET", f"repos/{repo}/branches/main")["commit"]["sha"]
        merge_base = api.request("GET", f"repos/{repo}/compare/{base}...{head}?per_page=1")["merge_base_commit"]["sha"]
        # The upstream queries inside check-bump.sh use the same read credential.
        env = api._credential(True)[1] if hasattr(api, "_credential") else dict(os.environ)
        for name in ("TAUCETI_TEAM_TOKEN", "SELF_TESTED_TEAM_TOKEN", "SELF_TESTED_WRITE_TOKEN"):
            env.pop(name, None)
        with tempfile.TemporaryDirectory(prefix="tauceti-self-tested-bump-") as tmp:
            dirs = []
            for name, sha in (("base", base), ("mergebase", merge_base), ("pr", head)):
                directory = Path(tmp) / name
                directory.mkdir()
                fetch_guard_files(api, repo, sha, directory)
                dirs.append(str(directory))
            result = subprocess.run(
                ["bash", str(Path(__file__).resolve().with_name("check-bump.sh")), *dirs],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
    except (
        APIError,
        Deferred,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        return None, f"bump-guard inputs unavailable: {type(error).__name__}"
    return result.returncode == 0, result.stdout + result.stderr


def request_ordinary(api, repo, number):
    api.request(
        "POST",
        f"repos/{repo}/actions/workflows/pr-build.yml/dispatches",
        {"ref": "main", "inputs": {"pr": str(number)}},
        idempotent=True,
    )


def active_build(api, repo, number):
    # Reuse the existing recovery inventory, including dispatches on main's SHA.
    from ci_recovery import pr_numbers, runs

    return any(
        number in pr_numbers(run)
        for state in ("queued", "in_progress", "waiting", "pending", "requested")
        for run in runs(api, repo, f"status={state}")
    )


def request_missing_build(api, repo, number, rows):
    if not latest_status(rows, "build") and not active_build(api, repo, number):
        request_ordinary(api, repo, number)


def decide(api, repo, number, event=None, *, reconcile=False):
    event = event or {}
    pr = api.request("GET", f"repos/{repo}/pulls/{number}")
    head = pr["head"]["sha"]
    branch = pr["head"]["ref"]
    labels = pr.get("labels", [])
    if pr.get("state") != "open" or pr["base"]["ref"] != "main":
        return False
    names = label_names(labels)
    removing = event.get("action") == "unlabeled" and (event.get("label") or {}).get("name") == "self-tested"
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if "self-tested" not in names and not removing and not manual:
        return False
    rows = api.request("GET", f"repos/{repo}/commits/{head}/status?per_page=100").get("statuses", [])
    assertion_context = assertion_context_name(number)
    prior = latest_status(rows, assertion_context)
    if prior.get("state") == "success" and not names & {"full-lint", "lint-repair"} and in_staging(repo, number, head):
        return True  # Existing accepted membership continues through mandatory staging.
    if "self-tested" not in names:
        return False
    event_head = (event.get("pull_request") or {}).get("head", {}).get("sha")
    bind_label = (
        event.get("action") == "labeled"
        and (event.get("label") or {}).get("name") == "self-tested"
        and event_head == head
    )
    # A dedicated head status remembers which commit the label asserted. A missed
    # push event cannot carry an old label forward; reconciliation removes it.
    if "self-tested" in label_names(labels) and not prior and not bind_label:
        if reconcile and (recent_label(api, repo, number) or active_build(api, repo, number)):
            return False  # The picker may still be processing the new label event.
        if reconcile and not latest_status(rows, "build"):
            request_ordinary(api, repo, number)
        api.request("DELETE", f"repos/{repo}/issues/{number}/labels/self-tested")
        return False
    eligible = requested(labels, branch) and os.environ.get("SELF_TESTED_MERGE_BACKEND") == "bors"

    def read_team(path):
        token = os.environ.get("TAUCETI_TEAM_TOKEN") or os.environ.get("SELF_TESTED_TEAM_TOKEN")
        if not token:
            return api.request("GET", path, read=False)
        import subprocess

        result = subprocess.run(
            ["gh", "api", path], env={**os.environ, "GH_TOKEN": token}, capture_output=True, text=True, timeout=30
        )
        if result.returncode:
            raise RuntimeError("team membership unavailable")
        return json.loads(result.stdout)

    bump = branch.startswith(BUMP_BRANCH)
    if eligible and bump:
        # The bot authors every bump; the trusted assertion is whoever applied the label.
        eligible = automatic_bump(pr, repo) and membership(repo, labeller(api, repo, number), read_team)
    elif eligible:
        eligible = membership(repo, pr.get("user", {}).get("login"), read_team)
    files = []
    if eligible:
        files = api.items(f"repos/{repo}/pulls/{number}/files?per_page=100")
        paths = [f["filename"] for f in files]
        renamed = [f for f in files if f.get("status") == "renamed"]
        paths.extend(f.get("previous_filename", "") for f in renamed)
        # A Mathlib bump rewrites many lines mechanically across the library, so it gets a
        # wider line budget; it already passes review and check-bump.sh before staging builds it.
        lines = 20000 if bump else 2000
        eligible = (
            len(files) == pr.get("changed_files")
            and len(files) <= 2000
            and (allowed_bump_paths(paths) if bump else allowed_paths(paths))
            and pr.get("additions", lines + 1) <= lines
            and pr.get("deletions", lines + 1) <= lines
            and all(
                f.get("status") != "added" or not f["filename"].endswith(".lean") or f.get("additions", 1001) <= 1000
                for f in files
            )
        )
    guard = None
    if eligible and bump:
        guard, output = check_bump(api, repo, head)
        print(output)
        eligible = guard is True
    after = api.request("GET", f"repos/{repo}/pulls/{number}")
    if (
        after["head"]["sha"] != head
        or after["base"]["ref"] != pr["base"]["ref"]
        or (label_names(after.get("labels")) & {"self-tested", "full-lint", "lint-repair"})
        != (names & {"self-tested", "full-lint", "lint-repair"})
    ):
        return False
    target_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    if guard is False:
        api.request(
            "POST",
            f"repos/{repo}/statuses/{head}",
            {
                "state": "failure",
                "context": "bump-guard",
                "description": "self-tested: check-bump.sh rejected the Lake pins; ordinary PR CI required",
                "target_url": target_url,
            },
            idempotent=True,
        )
    if eligible:
        # These statuses describe actual policy results, not a fictional build.
        # The bump-guard result is posted before the assertion that relies on it.
        statuses = (
            (
                ("bump-guard", "self-tested: check-bump.sh validated the Lake pins"),
                ("scope", "self-tested: mathematics plus Lake pins"),
                (assertion_context, DESCRIPTION),
            )
            if bump
            else (
                (assertion_context, DESCRIPTION),
                ("scope", "self-tested: mathematics-only diff"),
                ("bump-guard", "self-tested: dependency and toolchain pins unchanged"),
            )
        )
        for context, description in statuses:
            if latest_status(rows, context).get("state") == "success":
                continue
            api.request(
                "POST",
                f"repos/{repo}/statuses/{head}",
                {
                    "state": "success",
                    "context": context,
                    "description": description,
                    "target_url": target_url,
                },
                idempotent=True,
            )
    elif prior.get("state") == "success":
        if reconcile:
            request_missing_build(api, repo, number, rows)
        api.request(
            "POST",
            f"repos/{repo}/statuses/{head}",
            {
                "state": "failure",
                "context": assertion_context,
                "description": "self-tested no longer applies; ordinary PR CI required",
                "target_url": f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
            },
            idempotent=True,
        )
    if not eligible and bind_label:
        # This picker already returns to ordinary CI; do not leave a stale label
        # for reconciliation to dispatch the same build a second time.
        api.request("DELETE", f"repos/{repo}/issues/{number}/labels/self-tested")
    return eligible


def reconcile(number, repo):
    token = os.environ.get("SELF_TESTED_WRITE_TOKEN")
    if not token:
        return  # Standalone read-only status consumers never acquire write authority.

    class ReconcileAPI(GitHub):
        def _credential(self, read):
            role, env = super()._credential(read)
            if not read:
                env["GH_TOKEN"] = token
            return role, env

    try:
        event = (
            json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()) if os.environ.get("GITHUB_EVENT_PATH") else {}
        )
        decide(ReconcileAPI(max_wait=0), repo, int(number), event, reconcile=True)
    except (APIError, Deferred, OSError, ValueError, KeyError, RuntimeError) as error:
        print(f"self-tested reconciliation deferred: {type(error).__name__}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, required=True)
    args = parser.parse_args(argv)
    api = GitHub(max_wait=0)
    try:
        event = (
            json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()) if os.environ.get("GITHUB_EVENT_PATH") else {}
        )
        skip = decide(api, os.environ.get("GITHUB_REPOSITORY", "TauCetiProject/TauCeti"), args.pr, event)
    except (APIError, Deferred, OSError, ValueError, KeyError, RuntimeError) as error:
        print(f"self-tested: ordinary CI ({type(error).__name__})")
        skip = False
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            out.write(f"skip={'true' if skip else 'false'}\n")
    print(DESCRIPTION if skip else "Ordinary PR CI required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
