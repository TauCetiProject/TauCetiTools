#!/usr/bin/env python3
"""Run the shared CI sweeper with the queue host's existing GitHub App identity."""

from __future__ import annotations

import fcntl
import os
import runpy
import sys
from pathlib import Path


def main():
    directory = Path(os.environ.get("XDG_RUNTIME_DIR") or Path.home() / ".local/state/tauceti-ci-recovery")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (directory / "tauceti-ci-recovery.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("ci-recovery: another cleanup/recovery pass owns the lock; next timer will retry")
        return
    checkout = Path(os.environ.get("TAUCETI_QUEUE_CHECKOUT", Path.home() / "TauCetiQueue"))
    sys.path.insert(0, str(checkout))
    from tauceti_queue.github_app import GitHubApp

    token = GitHubApp.load().tokens.token()
    os.environ.update(
        GH_TOKEN=token,
        GH_READ_TOKEN=token,
        TAUCETI_GITHUB_READ_ROLE="ci-recovery-read",
        TAUCETI_GITHUB_WRITE_ROLE="ci-recovery-dispatch",
    )
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    script = root / "tauceti_tools/assets/scripts/ci_recovery.py"
    sys.path.insert(0, str(script.parent))
    sys.argv[0] = str(script)
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
