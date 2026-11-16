#!/usr/bin/env python3
"""Recognise local testing inside the existing PR assessment. Never runs candidate code."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from github_api import APIError, Deferred, GitHub

from tauceti_tools.self_tested import PIN_FILES

# Only these files feed check-bump.sh; candidate code is never fetched or run.
GUARD_FILES = ("lakefile.toml", "lakefile.lean", *PIN_FILES)


def run_mathlib_guard(api, command, directory, env):
    """Run the unchanged trusted guard over centrally observed API responses.

    A cache miss stops the local guard, makes one read through the caller's
    transport, and restarts the pure check with that observation. This keeps its
    policy intact without giving child processes an independent GitHub client.
    """
    bridge = Path(directory) / 'observations'
    bridge.mkdir()
    executable = bridge / 'gh'
    executable.write_text('#!/usr/bin/env python3\n' + Path(__file__).with_name('bump_api_replay.py').read_text())
    executable.chmod(0o700)
    env = dict(env, PATH=str(bridge) + os.pathsep + env.get('PATH', ''),
               TAUCETI_BUMP_OBSERVATIONS=str(bridge))
    for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_READ_TOKEN'):
        env.pop(name, None)  # Only the parent transport needs credentials.
    observations = {}
    request = bridge / 'request.json'
    deadline = time.monotonic() + 120
    # The existing guard needs at most five distinct upstream resources.
    for _ in range(9):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('bump-guard observation budget expired')
        request.unlink(missing_ok=True)
        (bridge / 'observations.json').write_text(json.dumps(observations))
        result = subprocess.run(command, cwd=directory, env=env, capture_output=True,
                                text=True, timeout=remaining)
        if not request.exists():
            return result
        missing = json.loads(request.read_text())
        path = missing.get('path')
        if (not isinstance(path, str) or not path.startswith('repos/')
                or len(path) > 8192 or path in observations or len(observations) >= 8):
            raise RuntimeError('bump-guard observation unavailable')
        observations[path] = api.request('GET', path)
    raise RuntimeError('bump-guard observation limit reached')


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
            command = ["bash", str(Path(__file__).resolve().with_name("check-bump.sh")), *dirs]
            # The alternate multi-upstream resolver owns its transport. Preserve
            # that separate path; this adapter covers the Mathlib-only guard.
            delegated = env.get('TAUCETI_CONF_SNAPSHOT') and (
                env.get('TAUCETI_SOURCE_CANDIDATE') == 'true'
                or env.get('TAUCETI_REPO', 'TauCetiProject/TauCeti') != 'TauCetiProject/TauCeti')
            if delegated:
                result = subprocess.run(command, cwd=tmp, env=env, capture_output=True, text=True, timeout=120)
            else:
                result = run_mathlib_guard(api, command, tmp, env)
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
