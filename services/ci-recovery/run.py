#!/usr/bin/env python3
"""Run the shared CI sweeper with the queue host's existing GitHub App identity."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def main():
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
    script = Path(__file__).resolve().parents[2] / "tauceti_tools/assets/scripts/ci_recovery.py"
    sys.path.insert(0, str(script.parent))
    sys.argv[0] = str(script)
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
