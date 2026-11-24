#!/usr/bin/env python3
"""Copy each changed module's base `.olean` out of the restored Lake cache.

`declaration-names.sh save` calls this before `lake build`, inside the offline PR sandbox and
before any candidate code runs. The declaration-names record compares each changed module's
tested header with its header at the base revision. Stock Lake restores only outputs whose
inputs still match, never the base version of a changed module, so read that version straight
from the cache that `lake cache get` fetched.

The revision maps (`<cache>/revisions/TauCeti/<rev>.jsonl`) list `[input hash, archive]` for
every module of a revision, without module names. Each archive begins with its trace's file name,
`<last name component>.trace`, uncompressed, so this reads only those first bytes to keep the
archives that could hold a changed module, extracts just those with one `leantar` invocation,
and takes the module name from each extracted `.ilean`. The base revision's map is read first;
the first archive found for a module wins.

Usage: base-headers.py MODULES DEST [--cache DIR] [--base REV]
Exit 0 after copying every base header the cache holds (a changed module with none is new).
Exit 1 when the cache cannot answer: no cache, no revision map, an unknown archive format or a
failed extraction. The caller then publishes no record, rather than a record that counts every
name of an existing module as added.
"""

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HASH = re.compile(r"[0-9a-f]{16}")
ARCHIVE = re.compile(r"[0-9a-f]{16}\.ltar")
REVISION = re.compile(r"[0-9a-f]{40}")
MODULE = re.compile(r"TauCeti(?:\.[A-Za-z_][A-Za-z0-9_']*)+")
# leantar's archive header: this magic and a flag byte, then the trace's NUL-terminated file name.
MAGIC = b"LTR4"
HEADER_BYTES = 1024
# The oldest ilean format whose "module" field this script relies on; v4.37.0-rc1 writes 6.
ILEAN_VERSION = 5


class Unavailable(Exception):
    """The cache cannot say what the base headers are."""


def regular(path):
    """Return a regular file path; reject symlinks and special files via lstat."""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise Unavailable("expected an ordinary file: " + str(path))
    return path


def changed_modules(path):
    modules = []
    for line in Path(path).read_text().splitlines():
        module = "".join(line.split())
        if not module:
            continue
        if not MODULE.fullmatch(module):
            raise Unavailable("unexpected module name " + repr(module))
        modules.append(module)
    return modules


def revision_rows(cache, base):
    """`(input hash, archive, from the base map)` rows of every revision map, the base revision's first."""
    directory = cache / "revisions/TauCeti"
    maps = sorted(directory.glob("*.jsonl")) if directory.is_dir() else []
    maps = [path for path in maps if REVISION.fullmatch(path.stem)]
    if not maps:
        raise Unavailable("no revision map in " + str(directory))
    maps.sort(key=lambda path: path.stem != base)
    rows = []
    for path in maps:
        for line in regular(path).read_text().splitlines()[1:]:
            row = json.loads(line)
            if (not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str)
                    or not HASH.fullmatch(row[0]) or not isinstance(row[1], str)
                    or not ARCHIVE.fullmatch(row[1])):
                raise Unavailable("unexpected revision map row in " + path.name)
            rows.append((row[0], row[1], path.stem == base))
    return rows


def trace_name(archive):
    """The file name of an archive's first entry, its `.trace`."""
    descriptor = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise Unavailable("expected an ordinary file: " + str(archive))
        head = os.read(descriptor, HEADER_BYTES)
    finally:
        os.close(descriptor)
    end = head.find(b"\0", len(MAGIC) + 1)
    if not head.startswith(MAGIC) or end < 0:
        raise Unavailable("unknown archive format: " + os.path.basename(archive))
    name = head[len(MAGIC) + 1:end].decode("utf-8", "replace")
    if not name.endswith(".trace") or "/" in name:
        raise Unavailable("unknown archive format: " + os.path.basename(archive))
    return name


def raise_stack_limit():
    """leantar 0.1.21 overflows the default 8 MiB stack on a large olean; lift it to the hard limit."""
    try:
        import resource
        _, hard = resource.getrlimit(resource.RLIMIT_STACK)
        resource.setrlimit(resource.RLIMIT_STACK, (hard, hard))
    except (ImportError, OSError, ValueError):
        pass


def extract(candidates, staging):
    """Extract every candidate archive into its own directory with one leantar invocation."""
    requests = [dict(file=str(archive), hash=digest, base=[str(staging / str(index) / "lib"),
                                                          str(staging / str(index) / "ir")])
                for index, (digest, archive) in enumerate(candidates)]
    sysroot = subprocess.check_output(["lean", "--print-prefix"], text=True).strip()
    env = dict(os.environ)
    # One process extracts every candidate, so give it the pool of up to 16 threads that the
    # whole-cache bulk unpack used; Lake's own restore later runs many leantars at 4 each.
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    env["RAYON_NUM_THREADS"] = str(min(16, cpus))
    # A `-j` batch unpacks on Rust worker threads, whose 2 MiB default stack leantar 0.1.21
    # overflows on a large olean whatever the rlimit; RUST_MIN_STACK sizes them.
    env.setdefault("RUST_MIN_STACK", str(256 * 1024 * 1024))
    subprocess.run([str(Path(sysroot) / "bin/leantar"), "-x", "-j", "-"], input=json.dumps(requests),
                   text=True, check=True, env=env, preexec_fn=raise_stack_limit)
    return [staging / str(index) / "lib" for index in range(len(candidates))]


def archive_module(lib, modules):
    """The changed module an extracted archive holds, read from its `.ilean`, or None.

    Several thousand archives can share a common last name such as `Basic`, so only an ilean
    whose text names one of `modules` is parsed; any ilean that is parsed must be well formed.
    """
    ileans = list(lib.glob("*.ilean")) if lib.is_dir() and not lib.is_symlink() else []
    if len(ileans) != 1:
        raise Unavailable("unexpected archive contents")
    text = regular(ileans[0]).read_bytes()
    if not any(json.dumps(module).encode() in text for module in modules):
        return None
    info = json.loads(text)
    module, version = info.get("module"), info.get("version")
    if (not isinstance(module, str) or not MODULE.fullmatch(module)
            or not isinstance(version, int) or isinstance(version, bool) or version < ILEAN_VERSION
            or ileans[0].name != module.split(".")[-1] + ".ilean"):
        raise Unavailable("unknown archive module layout")
    return module if module in modules else None


def scan(cache, rows, wanted):
    """The rows whose archive can hold a wanted trace, and how many archives were read."""
    unique, seen = [], set()
    for digest, name, from_base in rows:
        if name not in seen:
            seen.add(name)
            unique.append((digest, str(cache / "artifacts" / name), from_base))

    def read(row):
        try:
            return trace_name(row[1])
        except FileNotFoundError:
            # `lake cache get` installs every archive of the base map or discards the whole
            # cache. A failed repair fetch can leave another revision's map without its archives.
            if row[2]:
                raise Unavailable("base archive missing: " + os.path.basename(row[1]))
            return None

    # Reading one header is a file open and a small read; overlap them, as a just-downloaded
    # cache need not be in memory.
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        names = list(pool.map(read, unique, chunksize=256))
    return [(digest, Path(path)) for (digest, path, _), name in zip(unique, names)
            if name in wanted], len(unique)


def base_headers(modules_file, destination, cache, base=""):
    started = time.monotonic()
    destination = Path(destination)
    modules = {m for m in changed_modules(modules_file)
               if not (destination / (m.replace(".", "/") + ".olean")).exists()}
    if not modules:
        return 0
    if not cache:
        raise Unavailable("no Lake cache")
    cache = Path(cache)
    wanted = {module.split(".")[-1] + ".trace" for module in modules}
    candidates, archives = scan(cache, revision_rows(cache, base), wanted)
    found = set()
    if candidates:
        scratch = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        with tempfile.TemporaryDirectory(prefix="base-headers-", dir=scratch) as staging:
            for lib in extract(candidates, Path(staging)):
                module = archive_module(lib, modules)
                # The rows are in map order, the base revision's first; its version wins.
                if module is None or module in found:
                    continue
                found.add(module)
                target = destination / (module.replace(".", "/") + ".olean")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(regular(lib / (module.split(".")[-1] + ".olean")), target)
    print(f"base-headers: {len(found)} of {len(modules)} changed modules have a base header "
          f"({len(candidates)} of {archives} archives extracted, "
          f"{time.monotonic() - started:.1f}s)", file=sys.stderr, flush=True)
    return len(found)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("modules")
    parser.add_argument("destination")
    parser.add_argument("--cache", default=os.environ.get("LAKE_CACHE_DIR", ""))
    parser.add_argument("--base", default="")
    args = parser.parse_args(argv)
    try:
        base_headers(args.modules, args.destination, args.cache, args.base)
    except (Unavailable, OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        print(f"base-headers: {error}; no base headers", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
