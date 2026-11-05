#!/usr/bin/env python3
"""Retain raw diagnostic logs as data, without following candidate-planted paths."""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import stat
import sys
from contextlib import ExitStack
from pathlib import Path


def copy_log(root, parts, destination):
    with ExitStack() as stack:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, fd)
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            stack.callback(os.close, fd)
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        stack.callback(os.close, source)
        metadata = os.fstat(source)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("log is not a regular file")
        if metadata.st_size > 1048576 and metadata.st_blocks * 512 < metadata.st_size // 2:
            raise ValueError("sparse diagnostic log")
        with os.fdopen(os.dup(source), "rb") as input_file, gzip.open(destination, "wb") as output:
            shutil.copyfileobj(input_file, output, length=65536)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--runner-temp", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    logs = [(".", [args.candidate, ".lake", "telemetry", "lake-build.log"], "lake-build.log.gz")]
    logs.extend((args.runner_temp, [f"cache-get-{attempt}.log"], f"cache-get-{attempt}.log.gz")
                for attempt in range(1, 4))
    # lake-cache-get.sh numbers its logs across an explicit base-revision lookup and the walk
    # after it, so a run may leave up to six; the extra ones are collected only when present.
    logs.extend((args.runner_temp, [f"cache-get-{attempt}.log"], f"cache-get-{attempt}.log.gz")
                for attempt in range(4, 7) if (Path(args.runner_temp) / f"cache-get-{attempt}.log").exists())
    for root, parts, name in logs:
        try:
            copy_log(root, parts, out / name)
        except (OSError, ValueError):
            (out / name).unlink(missing_ok=True)
            print(f"::warning::raw diagnostic log unavailable: {name}", file=sys.stderr)


if __name__ == "__main__":
    main()
