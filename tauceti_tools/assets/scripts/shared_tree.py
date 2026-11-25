#!/usr/bin/env python3
"""Mount the host's shared read-only CI build tree under a PR build's `pr/.lake`, or decline.

TauCetiWorker decisions 211 and 216. On a Fleet native guest whose slot leased an entry, Fleet
binds that one immutable entry read-only at /opt/tauceti-shared-tree: `lake/` is a complete
`.lake` for a published `main` commit, restored from CI's published outputs and verified by
Fleet, and `ready.json` names its key. This script, run by pr-build.yml's trusted phase before
anything writes to `pr/.lake`:

1. declines (a `miss`, leaving today's path untouched) unless the bind is there, read-only, and
   its key matches this job's, as computed by Fleet's own `tauceti_ci_tree.py key`;
2. mounts `overlay -o userxattr,volatile,lowerdir=<entry>/lake,upperdir=U,workdir=W` on
   `pr/.lake`, with U and W in a fresh directory the sandbox never mounts. Writes, including
   the sandbox's, go only to U: the entry is never written;
3. deletes, through the overlay, every root-package output whose module has no source in the
   candidate, so audits that read `build/lib/lean` wholesale never see a module the candidate
   deleted or that `main` deleted since the entry was made.

Everything else is Lake's ordinary job: the later restore steps fetch only what is missing,
and the build checks every trace and restores or rebuilds what differs, exactly as when the
restored base is older than the merge base.

    shared_tree.py mount --project DIR --watchdog DIR --overlay DIR --record FILE
    shared_tree.py record --overlay DIR --record FILE [--field NAME=VALUE ...]

`mount` exits 0 on a hit or a miss, and nonzero only when it cannot leave `pr/.lake` as either
a verified overlay or the plain empty directory it found. `record` adds the upper layer's size
and the job's verdict to the record at the end of the job. Neither ever fails the build for the
tree's sake otherwise.
"""

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

TREE = Path("/opt/tauceti-shared-tree")
KEY_TOOL = Path("/usr/local/bin/tauceti_ci_tree.py")
ROOT_LIBRARY = "TauCeti"
# Where Lake keeps the root package's per-module outputs.
OUTPUT_DIRECTORIES = ("build/lib/lean", "build/ir")
KEY_TIMEOUT = 300
# Characters that would end or split an overlay mount option.
UNSAFE_OPTION = re.compile(r"[,:\\=\s]")


class Miss(Exception):
    """The tree does not serve this job: take today's path."""


def run(args, timeout=60, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout, **kwargs)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".next")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    temporary.replace(path)


def read_ready(tree):
    try:
        ready = json.loads((tree / "ready.json").read_text())
    except FileNotFoundError:
        raise Miss("no-ready-record") from None
    except (OSError, ValueError):
        raise Miss("unreadable-ready-record") from None
    if (not isinstance(ready, dict) or ready.get("version") != 1
            or not isinstance(ready.get("key_digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", ready["key_digest"])
            or not isinstance(ready.get("id"), str) or not isinstance(ready.get("commit"), str)):
        raise Miss("malformed-ready-record")
    return ready


def job_key(project, watchdog, key_tool=KEY_TOOL):
    if not key_tool.is_file():
        raise Miss("no-key-tool")
    try:
        output = run([sys.executable, str(key_tool), "key", "--project", str(project), "--watchdog", str(watchdog)],
                     timeout=KEY_TIMEOUT).stdout
        value = json.loads(output)
        return value["key"], value["key_digest"]
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
        raise Miss("key-failed") from None


def key_difference(entry_key, job):
    """The names of the key fields that differ, for the record: why a job missed."""
    if not isinstance(entry_key, dict) or not isinstance(job, dict):
        return ["unknown"]
    return sorted(name for name in set(entry_key) | set(job) if entry_key.get(name) != job.get(name))


def read_only(path):
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def is_mount(path):
    return os.path.ismount(path)


def mount_type(path):
    try:
        return run(["findmnt", "-n", "-o", "FSTYPE", "--mountpoint", str(path)]).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def module_stem(relative):
    """`TauCeti/A/B` for `TauCeti/A/B.olean.server.hash`: the path up to the name's first dot."""
    parent, _, name = relative.rpartition("/")
    stem = name.split(".", 1)[0]
    return f"{parent}/{stem}" if parent else stem


def has_source(project, stem):
    if not stem or stem.startswith(".") or "/." in stem:
        return False
    if stem != ROOT_LIBRARY and not stem.startswith(ROOT_LIBRARY + "/"):
        return False
    try:
        return stat.S_ISREG(os.lstat(project / (stem + ".lean")).st_mode)
    except OSError:
        return False


def prune(project, lake):
    """Delete root-package outputs of modules the candidate has no source for; return the count.

    Every file under the root package's output directories belongs to the module named by its
    path up to the first dot (outputs, `.trace`, `.hash`, `.log.json` and the like), so a module
    without a regular `.lean` source in the candidate loses all of them.
    """
    removed = 0
    known = {}  # stem -> has a source; a module has about ten output files

    def fail(error):
        raise error

    for top in OUTPUT_DIRECTORIES:
        base = lake / top
        if not base.is_dir() or base.is_symlink():
            continue
        for directory, directories, files in os.walk(base, onerror=fail):
            for name in files:
                path = Path(directory) / name
                stem = module_stem(str(path.relative_to(base)))
                if stem not in known:
                    known[stem] = has_source(project, stem)
                if not known[stem]:
                    path.unlink()
                    removed += 1
            for name in directories:
                if (Path(directory) / name).is_symlink():
                    raise OSError(f"unexpected symlink in the shared tree: {name}")
    return removed


def mount(project, watchdog, overlay, record, *, tree=TREE, key_tool=KEY_TOOL, sudo=("sudo", "-n")):
    started = time.monotonic()
    project = Path(project).absolute()
    lake = project / ".lake"
    overlay = Path(overlay).absolute()
    value = dict(version=1, shared_tree="unavailable", at=time.time())
    if not tree.is_dir():
        # No lease in this slot: the control group, or a host with the tree off.
        write_json(record, value)
        print("Shared CI tree: none offered to this guest; today's path")
        return 0
    mounted = False
    try:
        ready = read_ready(tree)
        value.update(entry=ready["id"], entry_commit=ready["commit"])
        if not read_only(tree) or not read_only(tree / "lake"):
            raise Miss("not-read-only")
        if not (tree / "lake").is_dir() or (tree / "lake").is_symlink():
            raise Miss("no-entry-tree")
        key, digest = job_key(project, watchdog, key_tool)
        if digest != ready["key_digest"]:
            value["key_difference"] = key_difference(ready.get("key"), key)
            raise Miss("key")
        info = os.lstat(lake)
        if not stat.S_ISDIR(info.st_mode) or any(lake.iterdir()) or is_mount(lake):
            raise Miss("lake-not-empty")
        for path in (tree / "lake", overlay, lake):
            if UNSAFE_OPTION.search(str(path)):
                raise Miss("unsafe-path")
        # Fresh every job; an existing directory is somebody else's state.
        overlay.mkdir(mode=0o700)
        (overlay / "upper").mkdir(mode=0o755)
        (overlay / "work").mkdir(mode=0o700)
        options = (f"userxattr,volatile,nosuid,nodev,lowerdir={tree / 'lake'},"
                   f"upperdir={overlay / 'upper'},workdir={overlay / 'work'}")
        try:
            run([*sudo, "mount", "-t", "overlay", "overlay", "-o", options, str(lake)])
        except (subprocess.SubprocessError, OSError):
            raise Miss("mount-failed") from None
        mounted = True
        if not is_mount(lake) or mount_type(lake) != "overlay":
            raise RuntimeError("pr/.lake is not the overlay after mounting")
        value["pruned"] = prune(project, lake)
        (lake / "tmp").mkdir(exist_ok=True)
        value.update(shared_tree="hit", mount_seconds=round(time.monotonic() - started, 1))
        write_json(record, value)
        print(f"Shared CI tree: hit; entry {ready['id']} (main {ready['commit'][:12]}), "
              f"{value['pruned']} outputs of modules without a source removed")
        return 0
    except Miss as miss:
        reason = str(miss)
    except Exception as error:  # noqa: BLE001 - any doubt takes today's path
        reason = "error:" + type(error).__name__
        print(f"::warning::shared CI tree setup failed ({type(error).__name__}: {error}); taking today's path")
    if mounted:
        # Back to the plain empty directory the step found, or stop: a half-prepared .lake must
        # never reach the build.
        try:
            run([*sudo, "umount", str(lake)])
        except (subprocess.SubprocessError, OSError):
            pass
        if is_mount(lake) or any(lake.iterdir()):
            value.update(shared_tree="miss:" + reason, unrestored=True)
            write_json(record, value)
            print("::error::the shared CI tree overlay could not be removed from pr/.lake")
            return 1
    value.update(shared_tree="miss:" + reason, mount_seconds=round(time.monotonic() - started, 1))
    write_json(record, value)
    print(f"Shared CI tree: miss ({reason}); today's path")
    return 0


def measure(path, timeout=600):
    """(bytes, inodes) under path on its own filesystem, as du counts them; None if unknown."""
    result = []
    for flag in ("--block-size=1", "--inodes"):
        try:
            output = subprocess.run(["du", "-sx", flag, str(path)], capture_output=True, text=True, timeout=timeout)
            result.append(int(output.stdout.split()[0]) if output.stdout.strip() else None)
        except (subprocess.SubprocessError, OSError, ValueError, IndexError):
            result.append(None)
    return tuple(result)


def record(overlay, path, fields):
    """Add the verdict and the upper layer's size; Fleet's own evidence counts the instance."""
    try:
        value = json.loads(Path(path).read_text())
        if not isinstance(value, dict):
            raise ValueError
    except (OSError, ValueError):
        value = dict(version=1, shared_tree="unrecorded")
    value.update(fields)
    upper = Path(overlay) / "upper"
    if value.get("shared_tree") == "hit" and upper.is_dir():
        value["upper_bytes"], value["upper_inodes"] = measure(upper)
    value["recorded_at"] = time.time()
    write_json(path, value)
    print("Shared CI tree record: " + json.dumps(value, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    m = sub.add_parser("mount")
    m.add_argument("--project", required=True)
    m.add_argument("--watchdog", required=True)
    m.add_argument("--overlay", required=True)
    m.add_argument("--record", required=True)
    r = sub.add_parser("record")
    r.add_argument("--overlay", required=True)
    r.add_argument("--record", required=True)
    r.add_argument("--field", action="append", default=[], help="NAME=VALUE to add to the record")
    args = parser.parse_args(argv)
    if args.action == "mount":
        return mount(args.project, args.watchdog, args.overlay, args.record)
    fields = {}
    for item in args.field:
        name, separator, text = item.partition("=")
        if separator and re.fullmatch(r"[a-z_]+", name):
            fields[name] = text
    try:
        return record(args.overlay, args.record, fields)
    except OSError as error:
        print(f"::warning::could not write the shared CI tree record: {error}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
