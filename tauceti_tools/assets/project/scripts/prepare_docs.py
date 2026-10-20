#!/usr/bin/env python3
"""Compatibility entrypoint; implementation is pinned through TauCetiTools."""
import sys
from tauceti_tools.templates import run_script

if __name__ == "__main__":
    raise SystemExit(run_script("prepare_docs.py", sys.argv[1:]))
