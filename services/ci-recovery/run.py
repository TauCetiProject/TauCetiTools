#!/usr/bin/env python3
"""Run the shared CI sweeper with the queue host's existing GitHub App identity."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def main():
    checkout = Path(os.environ.get("TAUCETI_QUEUE_CHECKOUT", Path.home() / "TauCetiQueue"))
    root = Path(__file__).resolve().parents[2]
    # Queue imports can themselves import shared Tools. Select this immutable
    # release before importing the queue, not after an older installed copy loads.
    sys.path[:0] = [str(root), str(checkout)]
    from tauceti_queue.github_app import GitHubApp

    # Every request uses the queue App's installation; never the host's personal `gh` login,
    # whose quota every agent on that account shares.
    token = GitHubApp.load().tokens.token()
    # The merge queue on this host reads this report for its status and its stuck-head alert.
    os.environ.setdefault("TAUCETI_CI_STUCK_REPORT", str(Path.home() / ".local/state/tauceti-ci-recovery/stuck-heads.json"))
    os.environ.update(
        GH_TOKEN=token,
        GH_READ_TOKEN=token,
        TAUCETI_GITHUB_READ_ROLE="ci-recovery-read",
        TAUCETI_GITHUB_WRITE_ROLE="ci-recovery-dispatch",
    )
    script = root / "tauceti_tools/assets/scripts/ci_recovery.py"
    sys.path.insert(0, str(script.parent))
    sys.argv[0] = str(script)
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
