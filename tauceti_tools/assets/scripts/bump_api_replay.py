"""Offline gh-api reader for the trusted Mathlib guard, never a network client."""

import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    directory = Path(os.environ['TAUCETI_BUMP_OBSERVATIONS'])
    args = sys.argv[1:]
    if len(args) != 4 or args[0] != 'api' or args[2] != '--jq':
        (directory / 'request.json').write_text(json.dumps({'error': 'unsupported guard API arguments'}))
        return 1
    path, expression = args[1], args[3]
    observations = json.loads((directory / 'observations.json').read_text())
    if path not in observations:
        (directory / 'request.json').write_text(json.dumps({'path': path}))
        return 1
    try:
        result = subprocess.run(['jq', '-r', expression], input=json.dumps(observations[path]),
                                capture_output=True, text=True, timeout=10)
        if result.returncode or result.stdout.strip() == 'null':
            (directory / 'request.json').write_text(json.dumps({'error': 'guard response filter failed'}))
            return 1
        sys.stdout.write(result.stdout)
        return 0
    except (OSError, subprocess.SubprocessError):
        (directory / 'request.json').write_text(json.dumps({'error': 'guard response filter unavailable'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
