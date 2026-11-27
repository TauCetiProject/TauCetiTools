#!/usr/bin/env python3
"""Copy a native Lake staging directory across the candidate/credential boundary.

Lake owns cache validity and uploads. This helper only accepts a complete flat
archive staging directory and copies ordinary files into a fresh trusted directory.
It performs no network requests, digest bookkeeping or incremental publication.

`split` divides such a trusted copy into a fixed number of part directories, so the
build job can hand it to the publisher as several GitHub artifacts. The publisher
downloads up to five artifacts at once (actions/download-artifact), and one
artifact of a whole-library map (about 1.3 GB) took 7 to 15 minutes to download
as a single stream. The publisher merges the parts into one directory and `prepare`
checks it exactly as before: every file named by the map and nothing else.

`check` and `select` let the publisher skip artifacts the bucket already holds. Both run
without the upload key. `check` writes a curl configuration naming every artifact's
signed upload URL and records each archive's MD5. The key-bearing workflow step sends
those HEAD requests (R2 Class B operations) and writes one line per answer. `select`
keeps an archive out of the upload only when the bucket answered 200 with an ETag equal
to the MD5 of the publisher's own copy, so the bucket's object already has exactly these
bytes. Every other answer, and a missing or unreadable answer file, uploads the archive,
as `lake cache put-staged` would. The full map is always uploaded last.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from lake_cache_probe import REPOSITORY, exact_map_url, parse_map, plain_https_endpoint

ARCHIVE = re.compile(r"[0-9a-f]{16}\.ltar")
MAX_MAP = 16 * 1024 * 1024
MAX_ANSWERS = 64 * 1024 * 1024


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


def artifact_url(endpoint: str, repository: str, name: str) -> str:
    """Lake's ``s3ArtifactUrl`` under ``--repo``: ``<endpoint>/<owner>/<repo>/<hash>.art``."""
    if not ARCHIVE.fullmatch(name):
        raise ValueError("unsafe archive name")
    if not REPOSITORY.fullmatch(repository) or any(c in {".", ".."} for c in repository.split("/")):
        raise ValueError("the repository is not an owner/name pair")
    return f"{plain_https_endpoint(endpoint)}/{repository}/{name[:-5]}.art"


def check(plan: Path, upload: Path, endpoint: str, repository: str) -> int:
    """Write the HEAD requests for every archive of a frozen plan, and each archive's MD5."""
    names = mapping((plan / "outputs.jsonl").read_bytes())
    upload.mkdir(parents=True, exist_ok=False)
    expected, config = {}, []
    parent = os.open(plan, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in sorted(names):
            url = artifact_url(endpoint, repository, name)
            digest = hashlib.md5(usedforsecurity=False)
            with regular(parent, name) as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            expected[url] = [name, digest.hexdigest()]
            config.append(f"url = {json.dumps(url)}\noutput = \"/dev/null\"\n")
    finally:
        os.close(parent)
    (upload / "expected.json").write_text(json.dumps(expected) + "\n")
    (upload / "heads.curl").write_text("".join(config))
    return len(expected)


def present(upload: Path) -> set[str]:
    """URLs whose HEAD answered 200 with the MD5 ETag of the publisher's copy.

    Each answer line is ``<status>\\t<ETag header>\\t<URL>``. A missing, oversized or
    malformed answer file, or any other line, proves nothing, so those archives upload.
    """
    expected = json.loads((upload / "expected.json").read_text())
    try:
        with (upload / "heads.out").open("rb") as handle:
            raw = handle.read(MAX_ANSWERS + 1)
    except FileNotFoundError:
        return set()
    if len(raw) > MAX_ANSWERS:
        return set()
    found = set()
    for line in raw.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        status, etag, url = parts
        entry = expected.get(url)
        if entry is not None and status == "200" and etag.strip() == f'"{entry[1]}"':
            found.add(url)
    return found


def select(plan: Path, upload: Path, revision_endpoint: str, repository: str,
           toolchain: str, revision: str) -> dict:
    """Write the curl upload list of the archives the bucket lacks, the map URL and statistics."""
    map_url = exact_map_url(revision_endpoint, toolchain, revision, repository)
    scope = toolchain.replace("/", "--").replace(":", "---")
    if quote(scope, safe="-_.~") != scope:
        # Lake percent-encodes this path component; refuse rather than publish a map it never reads.
        raise ValueError("the toolchain needs URL encoding in Lake's revision path")
    expected = json.loads((upload / "expected.json").read_text())
    if {name for name, _ in expected.values()} != mapping((plan / "outputs.jsonl").read_bytes()):
        raise ValueError("the HEAD request list does not match the plan's map")
    found = present(upload)
    missing = sorted((name, url) for url, (name, _) in expected.items() if url not in found)
    with (upload / "artifacts.curl").open("w") as config:
        for name, url in missing:
            config.write(f"upload-file = {json.dumps(str(plan / name))}\nurl = {json.dumps(url)}\n")
    (upload / "map-url").write_text(map_url + "\n")
    stats = json.loads((plan / "metrics.json").read_text())
    stats.update(
        inherited=len(found),
        uploaded_objects=len(missing),
        upload_bytes=sum((plan / name).stat().st_size for name, _ in missing),
    )
    (plan / "metrics.json").write_text(json.dumps(stats) + "\n")
    print("Lake publication upload: " + json.dumps(stats), flush=True)
    return stats


def split(staged: Path, parts: Path, count: int) -> list[int]:
    """Move a trusted staged copy's files into ``part-0`` .. ``part-<count-1>`` under ``parts``.

    The map goes to ``part-0``; the archives are dealt out in name order, so the parts differ in
    size by at most one archive. Returns the number of files in each part. ``staged`` is left empty.
    """
    if not 1 <= count <= 16:
        raise ValueError("part count must be between 1 and 16")
    names = sorted(os.listdir(staged))
    if "outputs.jsonl" not in names:
        raise ValueError("staged copy has no Lake map")
    names.remove("outputs.jsonl")
    parts.mkdir(parents=True, exist_ok=False)
    directories = [parts / f"part-{index}" for index in range(count)]
    for directory in directories:
        directory.mkdir()
    os.rename(staged / "outputs.jsonl", directories[0] / "outputs.jsonl")
    sizes = [1] + [0] * (count - 1)
    for index, name in enumerate(names):
        if not ARCHIVE.fullmatch(name):
            raise ValueError("unsafe archive name")
        os.rename(staged / name, directories[index % count] / name)
        sizes[index % count] += 1
    return sizes


def main():
    if any(os.environ.get(key) for key in ("LAKE_CACHE_KEY", "LAKE_CACHE_KEY_RAW")):
        raise ValueError("staging helper must never receive upload credentials")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "prepare", "split", "check", "select"))
    parser.add_argument("source", type=Path, help="check and select: the frozen publication plan")
    parser.add_argument("destination", type=Path, help="check and select: the upload directory")
    parser.add_argument("--parts", type=int, default=5, help="split: the number of part directories")
    parser.add_argument("--repo", help="check and select: the GitHub owner/name cache scope")
    parser.add_argument("--toolchain", help="select: the revision's Lean toolchain")
    parser.add_argument("--revision", help="select: the revision being published")
    args = parser.parse_args()
    if args.action == "check":
        count = check(args.source, args.destination, os.environ["S3_ARTIFACT_ENDPOINT"], args.repo)
        print(f"Lake publication: checking {count} artifacts in the bucket", flush=True)
        return
    if args.action == "select":
        select(args.source, args.destination, os.environ["S3_REVISION_ENDPOINT"], args.repo,
               args.toolchain, args.revision)
        return
    if args.action == "split":
        sizes = split(args.source, args.destination, args.parts)
        print("Lake staging parts (files): " + json.dumps(sizes), flush=True)
        return
    stats = stage(args.source, args.destination)
    if args.action == "prepare":
        # Observation metadata is host-generated and is not part of the cache protocol.
        (args.destination / "metrics.json").write_text(json.dumps(stats) + "\n")


if __name__ == "__main__":
    main()
