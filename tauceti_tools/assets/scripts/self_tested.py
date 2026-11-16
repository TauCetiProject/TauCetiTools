#!/usr/bin/env python3
"""Recognise local testing inside the existing PR assessment. Never runs candidate code."""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from pathlib import Path

from github_api import APIError, Deferred, GitHub

from tauceti_tools.self_tested import PIN_FILES

# Only these files feed check-bump.sh; candidate code is never fetched or run.
GUARD_FILES = ("lakefile.toml", "lakefile.lean", *PIN_FILES)


def fetch_guard_files(api, repo, sha, directory):
    for name in GUARD_FILES:
        try:
            data = api.request("GET", f"repos/{repo}/contents/{name}?ref={sha}")
        except RuntimeError as error:
            if getattr(error, "status", None) == 404:
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
    except Deferred:
        raise
    except (
        APIError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        return None, f"bump-guard inputs unavailable: {type(error).__name__}"
    return result.returncode == 0, result.stdout + result.stderr


def decide(api, repo, number, *, pr=None, rows=None, paths=None):
    """Persistent self-testing is evaluated directly; never remove labels or post receipts."""
    from tauceti_tools.self_tested import eligible

    pr = pr or api.request("GET", f"repos/{repo}/pulls/{number}")
    return eligible(pr, repo, paths=paths, read=lambda path: api.request("GET", path))


def reconcile(number, repo, *, pr=None, statuses=None, paths=None):
    return decide(GitHub(max_wait=0), repo, int(number), pr=pr, paths=paths)
