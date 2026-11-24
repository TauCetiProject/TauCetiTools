#!/usr/bin/env python3
"""Check that the root package's built outputs are the bytes their Lake traces name.

Lake records each module output in the module's `.trace` under its content hash, and once the
trace's input hash matches it trusts the files on disk without reading them. An interrupted write
therefore leaves a module that Lake considers built while an output is truncated or empty: leantar
writes the `.trace` before the outputs, and a killed Lean leaves only a `.tmp` file. A corrupt
archive restored from a cache does the same. Importing such a module crashes Lean, and packing it
publishes the damage (batch 506 attempt 339, 24 Nov 2026: a 524,288-byte `.olean.private` and a
0-byte `.ir.sig` under their original trace names).

    lake_output_check.py check PROJECT    report every mismatch; exit 1 if there is one
    lake_output_check.py discard PROJECT  also remove each mismatching module's build outputs,
                                          trace and cache records (exit 3 when it removed any),
                                          so the next `lake build` compiles it from source

Hashes use the toolchain's own Lake hash (CacheHash.lean), as bulk-unpack-cache.py does. Exit 2
means the check itself could not run. Runs inside the build sandbox; it reads only the project's
`.lake` and executes no project code. This guards against accidents, not against candidate code,
which can write any bytes it likes under `.lake` anyway.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

NAME = re.compile(r"[0-9a-f]{16}")
HASHER = Path(__file__).resolve().parent / "CacheHash.lean"
# Lake's module trace output keys and their extensions (`ModuleOutputDescrs.toJson`); "l" is the
# module's packed archive, kept at `Module.ltarFile` (the IR path).
DESCRIPTORS = {"o": ("olean", "olean.server", "olean.private"), "i": ("ilean",), "rs": ("ir.sig",),
               "r": ("ir",), "c": ("c",), "b": ("bc",), "l": ("ltar",)}
IR_EXTENSIONS = ("c", "bc", "ltar")


def regular(path: Path) -> bool:
    """Whether path is a regular file. Absent is False; any other failure stops the check."""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def raise_error(error: OSError) -> None:
    raise error


def cache_dir(project: Path) -> Path:
    configured = os.environ.get("LAKE_CACHE_DIR")
    return Path(configured).resolve() if configured else project / ".lake/cache"


def expected_outputs(project: Path):
    """Yield (module stem, depHash, [(build path, trace name)]) for every root-package trace.

    Every name the trace gives is listed, whether or not the build file exists: the artifact
    cache's file of that name is checked either way. TauCeti does not use postponed compilation,
    whose IR outputs are not in the module trace.
    """
    lib = project / ".lake/build/lib/lean"
    ir = project / ".lake/build/ir"
    if not lib.is_dir():
        return
    for directory, _, files in os.walk(lib, onerror=raise_error):
        for file in files:
            if not file.endswith(".trace"):
                continue
            trace = Path(directory) / file
            stem = trace.relative_to(lib).with_suffix("")
            try:
                value = json.loads(trace.read_text())
            except ValueError:
                continue  # not a trace Lake would trust either; an unreadable one stops the check
            outputs = value.get("outputs") if isinstance(value, dict) else None
            if not isinstance(outputs, dict):
                continue
            pairs = []
            for key, extensions in DESCRIPTORS.items():
                names = outputs.get(key)
                names = names if isinstance(names, list) else [names]
                for extension, name in zip(extensions, names):
                    if not isinstance(name, str) or not re.fullmatch(NAME.pattern + r"\." + re.escape(extension), name):
                        continue
                    root = ir if extension in IR_EXTENSIONS else lib
                    pairs.append((root / (str(stem) + "." + extension), name))
            yield stem, value.get("depHash"), pairs


def published_archives(project: Path) -> list[tuple[str, str]]:
    """The (input hash, archive name) rows of the build's native output map, if it wrote one."""
    try:
        lines = (project / ".lake/outputs.jsonl").read_text().splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines[1:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, list) and len(row) == 2 and all(isinstance(v, str) for v in row)
                and NAME.fullmatch(row[0]) and re.fullmatch(NAME.pattern + r"\.ltar", row[1])):
            rows.append((row[0], row[1]))
    return rows


def lake_hashes(paths: list[Path], scratch: Path, *, jobs: int | None = None) -> dict[str, str]:
    """The Lake content hash of each path, as 16 hex digits, from parallel CacheHash.lean helpers.

    Hard links share one inode, so each file is read once; every helper's output is drained
    concurrently, so no helper stalls on a full pipe.
    """
    if not paths:
        return {}
    inodes: dict[tuple[int, int], Path] = {}
    for path in paths:
        info = path.stat()
        inodes.setdefault((info.st_dev, info.st_ino), path)
    unique = list(inodes.values())
    lean = Path(subprocess.check_output(["lean", "--print-prefix"], text=True).strip()) / "bin/lean"
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    count = max(1, min(jobs or 16, cpus, len(unique)))
    helpers = []
    try:
        for index in range(count):
            request = scratch / f"hash-request-{index}.json"
            request.write_text(json.dumps([str(p) for p in unique[index::count]]))
            helpers.append(subprocess.Popen([str(lean), "--run", str(HASHER), str(request)],
                                            stdout=subprocess.PIPE, text=True))
        with concurrent.futures.ThreadPoolExecutor(len(helpers)) as pool:
            outputs = list(pool.map(lambda helper: helper.communicate(timeout=1800)[0], helpers))
        by_inode = {}
        for helper, output in zip(helpers, outputs):
            if helper.returncode != 0:
                raise subprocess.CalledProcessError(helper.returncode, "CacheHash.lean")
            for line in output.splitlines():
                path, value = json.loads(line)
                info = os.stat(path)
                by_inode[(info.st_dev, info.st_ino)] = f"{value:016x}"
    finally:
        for helper in helpers:
            if helper.poll() is None:
                helper.kill()
                helper.wait()
    if len(by_inode) != len(unique):
        raise ValueError("incomplete output hashes")
    hashes = {}
    for path in paths:
        info = path.stat()
        hashes[str(path)] = by_inode[(info.st_dev, info.st_ino)]
    return hashes


def mismatches(project: Path, hasher=None):
    """Every (stem, depHash, path, trace name, actual hash, size) whose bytes differ from the name.

    Checks each build output and the artifact cache's file of the same name (Lake resolves the
    trace through the cache and hard-links it into the build), and every archive the build's
    output map names, which `lake cache stage` copies for publication without reading it.
    """
    project = project.resolve()
    artifacts = cache_dir(project) / "artifacts"
    modules = list(expected_outputs(project))
    stems = {dep_hash: stem for stem, dep_hash, _ in modules if isinstance(dep_hash, str)}
    checks = []  # (stem, depHash, path, name)
    for stem, dep_hash, pairs in modules:
        for path, name in pairs:
            checks += [(stem, dep_hash, candidate, name) for candidate in (path, artifacts / name)
                       if regular(candidate)]
    for dep_hash, name in published_archives(project):
        if regular(artifacts / name):
            checks.append((stems.get(dep_hash), dep_hash, artifacts / name, name))
    paths = list(dict.fromkeys(path for _, _, path, _ in checks))
    with tempfile.TemporaryDirectory(prefix="lake-output-check-") as scratch:
        hashes = (hasher or lake_hashes)(paths, Path(scratch))
    found, seen = [], set()
    for stem, dep_hash, path, name in checks:
        actual = hashes.get(str(path))
        if actual is not None and actual != name.split(".", 1)[0] and (path, name) not in seen:
            seen.add((path, name))
            found.append((stem, dep_hash, path, name, actual, path.stat().st_size))
    return len(modules), len(paths), found


def discard(project: Path, found) -> int:
    """Remove each mismatching module's build files and the cache records for its input hash.

    A damaged artifact cache file goes too. Without its records the next build compiles the
    module (or restores another record's intact outputs) instead of restoring the damage again.
    """
    project = project.resolve()
    lib, ir = project / ".lake/build/lib/lean", project / ".lake/build/ir"
    cache = cache_dir(project)
    records = cache / "outputs"
    removed = set()
    for stem, dep_hash, path, _, _, _ in found:
        if path.parent == cache / "artifacts":
            path.unlink(missing_ok=True)
        if isinstance(dep_hash, str) and NAME.fullmatch(dep_hash) and records.is_dir():
            for record in records.glob(f"*/{dep_hash}.json"):
                record.unlink(missing_ok=True)
        if stem is None or stem in removed:
            continue
        removed.add(stem)
        for root in (lib, ir):
            directory = (root / stem).parent
            if not directory.is_dir():
                continue
            for entry in directory.iterdir():
                # `Basic.olean.private`, `Basic.trace`, `Basic.c.hash`, but never `Basic.Sub.olean`.
                if entry.name.startswith(stem.name + ".") and not entry.is_dir():
                    rest = entry.name[len(stem.name) + 1:]
                    if rest.split(".", 1)[0] in {"trace", "olean", "ilean", "ir", "c", "bc", "ltar", "setup", "log"}:
                        entry.unlink(missing_ok=True)
    return len(removed)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("check", "discard"))
    parser.add_argument("project", type=Path)
    args = parser.parse_args(argv)
    try:
        modules, files, found = mismatches(args.project)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        print(f"::warning::lake output check could not run: {error}", flush=True)
        return 2
    for stem, _, path, name, actual, size in found:
        print(f"::error::{path} is not the output its trace names: expected {name}, "
              f"found Lake hash {actual} ({size} bytes); module {str(stem).replace(os.sep, '.')}", flush=True)
    print(f"lake output check: {modules} modules, {files} files, {len(found)} mismatches", flush=True)
    if not found:
        return 0
    if args.action == "discard":
        count = discard(args.project, found)
        print(f"lake output check: removed {len(found)} damaged files, {count} modules' outputs and their "
              "cache records; the next build restores or compiles them", flush=True)
        return 3
    return 1


if __name__ == "__main__":
    sys.exit(main())
