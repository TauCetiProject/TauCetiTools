#!/usr/bin/env python3
"""Run the shared CI sweeper with the queue host's existing GitHub App identity."""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path


def main():
    checkout = Path(os.environ.get("TAUCETI_QUEUE_CHECKOUT", Path.home() / "TauCetiQueue"))
    root = Path(__file__).resolve().parents[2]
    # Queue imports can themselves import shared Tools. Select this immutable
    # release before importing the queue, not after an older installed copy loads.
    sys.path[:0] = [str(root), str(checkout)]
    from tauceti_queue.github_app import GitHubApp

    # The queue App is repository-scoped; its installation cannot read the org
    # team. Reuse the host's existing read:org credential only for that lookup.
    if not os.environ.get("SELF_TESTED_TEAM_TOKEN"):
        os.environ["SELF_TESTED_TEAM_TOKEN"] = subprocess.check_output(
            ["gh", "auth", "token"], text=True, timeout=30
        ).strip()
    token = GitHubApp.load().tokens.token()
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
