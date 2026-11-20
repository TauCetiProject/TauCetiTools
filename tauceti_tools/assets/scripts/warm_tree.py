#!/usr/bin/env python3
"""Reuse the head batch's build tree between staging attempts on one Fleet slot.

TauCetiFleet attaches one persistent disk (virtio serial `tauceti-warm`) to every guest of a
reserved staging slot; the guests themselves stay disposable. This trusted helper owns the
disk. It holds at most one batch's tree, named by a key over the batch, its accepted member
heads, the toolchain pin and the Lake manifest, so a withdrawn member or a new pin starts afresh.

Nothing a previous sandbox wrote is visible to trusted setup. Lake puts the root package's
`build/bin` on the PATH of `lake exe cache get`, and Lake trusts local cache mappings and
artifacts, so a kept tree could otherwise plant programs or outputs that run unsandboxed.

- `prepare`, right after the candidate's `.lake` is normalised: mount the disk; keep the
  previous attempt's `lake/build` and `lake/cache` only for the same key and only if they
  hold nothing but directories and regular files, otherwise reformat; bind a fresh, empty
  `setup/` on the disk as `pr/.lake`. Trusted setup then runs exactly as on a cold runner.
- `activate`, immediately before the sandboxed build: move the kept `build/` into `pr/.lake`
  and merge the kept `cache/` under the fresh one (fresh files win). Renames on one
  filesystem, so this takes seconds. From here on only the sandbox and the post-sandbox
  steps, which already treat `pr/.lake` as candidate-written, see the tree.
- `retain`, at the end of an uncancelled job whose sandboxed build ran to an outcome: keep
  `setup/` as the next attempt's `lake/` and record the key as complete.

`state.json`, outside the sandbox's bind, says `complete` only between `retain` and the
next `prepare`, which first marks it incomplete. A crash, cancellation or VM loss
therefore leaves no reusable tree. Anything unexpected takes the normal cold path; no
subcommand ever fails the job.
"""

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

DEVICE = Path("/dev/disk/by-id/virtio-tauceti-warm")
MOUNT = Path("/mnt/tauceti-warm")
SCHEMA = 1
KEPT = ("build", "cache")
MIN_FREE_BYTES = 64 * 1024**3
MIN_FREE_INODES = 0.1


def sudo(*args, check=True, timeout=120):
    return subprocess.run(["sudo", "-n", *map(str, args)], check=check, timeout=timeout,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def file_digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_key(project, request_file, members_file):
    """The batch's identity from the trusted request receipt and verified member manifest."""
    request = json.loads(Path(request_file).read_text())["request"]
    members = request["members"]
    if isinstance(members, dict):
        # A large batch carries its members in the verified retained manifest.
        members = json.loads(Path(members_file).read_text())["members"]
    if not isinstance(members, list) or not members or not request.get("batch_id"):
        raise ValueError("request has no batch membership")
    value = dict(
        schema=SCHEMA,
        source=request["source"],
        batch=request["batch_id"],
        members=sorted(json.dumps(member, sort_keys=True) for member in members),
        toolchain=(Path(project) / "lean-toolchain").read_text().strip(),
        manifest=file_digest(Path(project) / "lake-manifest.json"),
    )
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def raise_error(error):
    raise error


# Outputs Lake itself makes read-only and replaces by unlinking. Lake rewrites every other
# file it keeps (traces, hashes, setup and output mappings) in place.
READ_ONLY_OUTPUTS = (".olean", ".olean.server", ".olean.private", ".ilean", ".ir", ".c", ".bc", ".ltar",
                     ".o", ".so", ".a")


def ordinary_tree(root):
    """Only usable directories and regular files, checked without following links.

    A previous sandbox can also leave modes Lake cannot use, so those are refused too.
    """
    if not stat.S_ISDIR(os.lstat(root).st_mode) or not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        return False
    for directory, names, files in os.walk(root, onerror=raise_error):
        relative = Path(directory).relative_to(root).parts
        artifacts = Path(root).name == "cache" and relative[:1] == ("artifacts",)
        for name in names + files:
            path = os.path.join(directory, name)
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                    return False
            elif not stat.S_ISREG(mode) or not os.access(path, os.R_OK):
                return False
            elif not (artifacts or name.endswith(READ_ONLY_OUTPUTS)) and not os.access(path, os.W_OK):
                return False
    return True


def keep_only_outputs(lake):
    """Delete everything but build/ and cache/, then refuse anything but a plain tree."""
    if not stat.S_ISDIR(os.lstat(lake).st_mode):
        return False
    for entry in lake.iterdir():
        mode = os.lstat(entry).st_mode
        if entry.name in KEPT and stat.S_ISDIR(mode):
            continue
        if stat.S_ISDIR(mode):
            shutil.rmtree(entry)  # removes links, never follows them
        else:
            entry.unlink()
    return all(ordinary_tree(lake / name) for name in KEPT if os.path.lexists(lake / name))


def room_left(path):
    usage = os.statvfs(path)
    return (usage.f_bavail * usage.f_frsize >= MIN_FREE_BYTES
            and usage.f_favail >= MIN_FREE_INODES * usage.f_files)


def completed_key():
    path = MOUNT / "state.json"
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("complete") is not True:
        return None
    return value.get("key")


def write_state(key, *, complete):
    """Replace the marker durably: the file and its directory reach the disk."""
    temporary = MOUNT / "state.json.new"
    with open(temporary, "w") as stream:
        stream.write(json.dumps(dict(schema=SCHEMA, key=key, complete=complete)) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, MOUNT / "state.json")
    directory = os.open(MOUNT, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def mount_volume():
    """Mount the disk for this job's (trusted) runner user; the sandbox never sees its root."""
    sudo("mkdir", "-p", MOUNT)
    if sudo("mount", "-o", "nosuid,nodev,noatime", DEVICE, MOUNT, check=False).returncode:
        return False
    sudo("chown", f"{os.getuid()}:{os.getgid()}", MOUNT)
    return True


def reformat(mounted):
    if mounted:
        sudo("umount", MOUNT)
    # mkfs discards the whole device, which returns its space to the host (discard=unmap).
    sudo("mkfs.ext4", "-q", "-F", "-L", "tauceti-warm", DEVICE, timeout=300)
    if not mount_volume():
        raise OSError("the freshly formatted disk does not mount")


def bound_to_setup(lake):
    """True if the candidate's .lake is the bind of this disk's setup/ directory."""
    try:
        here, there = os.stat(lake), os.stat(MOUNT / "setup")
    except OSError:
        return False
    return os.path.ismount(lake) and (here.st_dev, here.st_ino) == (there.st_dev, there.st_ino)


def prepare(workspace, project, request_file, members_file):
    project = Path(workspace) / project
    lake = project / ".lake"
    if not DEVICE.exists():
        print("warm tree: no persistent staging disk on this runner; cold path")
        return {}
    if not stat.S_ISDIR(os.lstat(lake).st_mode) or any(lake.iterdir()):
        print("warm tree: the candidate's .lake is not a fresh empty directory; cold path")
        return {}
    key = tree_key(project, request_file, members_file)
    mounted = mount_volume()
    warm = mounted and completed_key() == key
    if mounted:
        write_state(key, complete=False)
    if warm:
        try:
            if os.path.lexists(MOUNT / "setup"):
                shutil.rmtree(MOUNT / "setup")
            warm = keep_only_outputs(MOUNT / "lake") and room_left(MOUNT)
        except OSError as error:
            print(f"warm tree: the kept tree could not be cleaned ({error})")
            warm = False
    if warm:
        sudo("fstrim", MOUNT, check=False)
    else:
        print("warm tree: no completed tree for this batch; reformatting the disk (cold path)")
        reformat(mounted)
        write_state(key, complete=False)
    (MOUNT / "setup").mkdir()
    sudo("mount", "--bind", MOUNT / "setup", lake)
    print("warm tree: " + ("this batch's previous tree is kept for the sandboxed build"
                           if warm else "this attempt starts a fresh tree for this batch"))
    return dict(WARM_VOLUME="1", WARM_TREE="1" if warm else "0", WARM_TREE_KEY=key)


def merge(source, target):
    """Move source's entries under target; existing target entries win."""
    for directory, names, files in os.walk(source, onerror=raise_error):
        relative = Path(directory).relative_to(source)
        for name in list(names):
            destination = target / relative / name
            if not os.path.lexists(destination):
                os.rename(Path(directory) / name, destination)
                names.remove(name)
            elif not stat.S_ISDIR(os.lstat(destination).st_mode):
                names.remove(name)  # a fresh file wins over a kept directory
        for name in files:
            if not os.path.lexists(target / relative / name):
                os.rename(Path(directory) / name, target / relative / name)


def activate(project, warm):
    project = Path(project)
    lake = project / ".lake"
    if not bound_to_setup(lake):
        raise OSError("the candidate's .lake is no longer this disk's setup directory")
    kept = MOUNT / "lake"
    if warm:
        moved = []
        for name in KEPT:
            if not os.path.lexists(kept / name):
                continue
            if not os.path.lexists(lake / name):
                os.rename(kept / name, lake / name)
                moved.append(name)
            elif name == "cache" and stat.S_ISDIR(os.lstat(lake / name).st_mode):
                merge(kept / name, lake / name)
                moved.append(name)
        print(f"warm tree: reusing the previous attempt's {' and '.join(moved) or 'nothing'}")
    if os.path.lexists(kept):
        shutil.rmtree(kept)
    return dict(WARM_ACTIVE="1")


def retain(project, key):
    lake = Path(project) / ".lake"
    if not bound_to_setup(lake):
        raise OSError("the candidate's .lake is no longer this disk's setup directory")
    subprocess.run(["sync"], check=False, timeout=60)
    sudo("umount", lake, timeout=60)
    if os.path.lexists(MOUNT / "lake"):
        shutil.rmtree(MOUNT / "lake")
    os.rename(MOUNT / "setup", MOUNT / "lake")
    write_state(key, complete=True)
    sudo("umount", MOUNT, check=False, timeout=60)
    print("warm tree: kept for this batch's next attempt")
    return {}


def undo_prepare(workspace, project):
    """Leave nothing mounted over the candidate's tree, so the job continues exactly cold."""
    subprocess.run(["sudo", "-n", "umount", str(Path(workspace) / project / ".lake")],
                   check=False, capture_output=True)
    subprocess.run(["sudo", "-n", "umount", str(MOUNT)], check=False, capture_output=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare")
    command.add_argument("--workspace", required=True)
    command.add_argument("--project", required=True)
    command.add_argument("--request", required=True)
    command.add_argument("--members", required=True)
    command = commands.add_parser("activate")
    command.add_argument("--project", required=True)
    command.add_argument("--warm", choices=("0", "1"), required=True)
    command = commands.add_parser("retain")
    command.add_argument("--project", required=True)
    command.add_argument("--key", required=True)
    for command in commands.choices.values():
        command.add_argument("--github-env")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.workspace, args.project, args.request, args.members)
        elif args.command == "activate":
            result = activate(args.project, args.warm == "1")
        else:
            result = retain(args.project, args.key)
    except Exception as error:  # noqa: BLE001 - the cold path is always available
        print(f"::warning::warm tree {args.command}: {error}; this attempt's tree will not be reused")
        if args.command == "prepare":
            undo_prepare(args.workspace, args.project)
        return 0
    if args.github_env and result:
        with open(args.github_env, "a") as output:
            output.writelines(f"{name}={value}\n" for name, value in result.items())
    return 0


if __name__ == "__main__":
    sys.exit(main())
