#!/usr/bin/env python3
"""Copy a native Lake staging directory across the candidate/credential boundary.

Lake owns cache validity and uploads. This helper only accepts a complete flat
archive staging directory and copies ordinary files into a fresh trusted directory.
It performs no network requests, digest bookkeeping or incremental publication.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from lake_cache_probe import parse_map

ARCHIVE = re.compile(r"[0-9a-f]{16}\.ltar")
MAX_MAP = 16 * 1024 * 1024


@contextmanager
def regular(parent: int, name: str):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("expected an ordinary staged file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            yield handle
    finally:
        os.close(fd)


def mapping(raw: bytes) -> set[str]:
    if len(raw) > MAX_MAP:
        raise ValueError("oversized Lake map")
    version, rows = parse_map(raw)
    if version != "2026-03-17":
        raise ValueError("unsupported Lake map schema")
    if len({key for key, _ in rows}) != len(rows):
        raise ValueError("duplicate input mapping")
    return {name for _, name in rows}


def stage(source: Path, destination: Path) -> dict:
    """Freeze native staging data without following candidate-supplied paths."""
    started = time.monotonic()
    parent = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with regular(parent, "outputs.jsonl") as handle:
            raw = handle.read(MAX_MAP + 1)
        names = mapping(raw)
        if set(os.listdir(parent)) != names | {"outputs.jsonl"}:
            raise ValueError("staging files do not match the native Lake map")
        destination.mkdir(parents=True, exist_ok=False)
        try:
            (destination / "outputs.jsonl").write_bytes(raw)
            size = 0
            for name in sorted(names):
                # parse_map accepts only flat content-hash archive names.
                if not ARCHIVE.fullmatch(name):
                    raise ValueError("unsafe archive name")
                with regular(parent, name) as source_file, (destination / name).open("xb") as target:
                    shutil.copyfileobj(source_file, target)
                    size += target.tell()
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    finally:
        os.close(parent)
    stats = {
        "objects": len(names),
        "inherited": 0,
        "uploaded_objects": len(names),
        "upload_bytes": size,
        "full_bytes": size,
        "verified_download_bytes": 0,
        "seconds": round(time.monotonic() - started, 3),
    }
    print("Lake native publication: " + json.dumps(stats), flush=True)
    return stats


def main():
    if any(os.environ.get(key) for key in ("LAKE_CACHE_KEY", "LAKE_CACHE_KEY_RAW")):
        raise ValueError("staging helper must never receive upload credentials")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "prepare"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    stats = stage(args.source, args.destination)
    if args.action == "prepare":
        # Observation metadata is host-generated and is not part of the cache protocol.
        (args.destination / "metrics.json").write_text(json.dumps(stats) + "\n")


if __name__ == "__main__":
    main()
