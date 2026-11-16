"""List immutable Git tree changes without consulting candidate Git configuration.

The caller supplies exact base/head SHAs, using the local merge base for raw PRs
or the captured integration endpoints. Read only the checkout's object database; never enter its working tree or
load its config, attributes, replacement refs, hooks, or executable helpers.
Missing objects cause the caller to fall back to full lint.

In `--policy` mode a change to `.tauceti/ci-source.json` or a workflow is also refused unless
every workflow bound to the shared-harness revision pins exactly that revision
(`tauceti_tools.harness_pins`). The guard then reports an incomplete scope, a non-waivable
scope failure.
"""

import argparse
import contextlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


@contextlib.contextmanager
def _object_reader(checkout):
    objects = (Path(checkout) / ".git" / "objects").resolve(strict=True)
    # Locate the runner's Git once (also supports Nix installations); subprocesses
    # get only the minimal PATH below and cannot search the candidate checkout.
    git = shutil.which("git")
    if git is None:
        raise OSError("Git is unavailable")
    with tempfile.TemporaryDirectory(prefix="lint-scope-git-") as directory:
        # No inherited token, Git configuration, shell startup, or candidate PATH.
        env = {"PATH": os.defpath, "HOME": directory, "LC_ALL": "C",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
        repository = str(Path(directory) / "repo.git")
        subprocess.run([git, "init", "--quiet", "--bare", "--template=", repository],
                       env=env, check=True, stdout=subprocess.DEVNULL)
        env["GIT_OBJECT_DIRECTORY"] = str(objects)
        command = [git, "--no-replace-objects", f"--git-dir={repository}",
                   "-c", "core.attributesFile=/dev/null"]
        yield command, env


def changed_files(checkout, base, head, *, include_deleted=False):
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
        raise ValueError("expected immutable commit SHAs")
    with _object_reader(checkout) as (command, env):
        for sha in (base, head):
            kind = subprocess.check_output(command + ["cat-file", "-t", sha], env=env)
            if kind != b"commit\n":
                raise ValueError("scope endpoint is not a commit")
        # Disable rename detection: a rename is a deletion plus an addition, so
        # the destination is included and the absent old module is excluded.
        # NUL separation preserves tabs, newlines and non-ASCII filenames.
        return subprocess.check_output(
            command + ["diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                       "--name-only", "-z"] + ([] if include_deleted else ["--diff-filter=d"])
            + [base, head, "--"], env=env)


HARNESS_PATHS = (".tauceti/ci-source.json", ".github/workflows")


def harness_files(checkout, commit):
    """The policy and workflow files at one commit, read only as regular blobs."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("expected an immutable commit SHA")
    files = {}
    with _object_reader(checkout) as (command, env):
        listing = subprocess.check_output(
            command + ["ls-tree", "-r", "-z", "--full-tree", commit, "--", *HARNESS_PATHS], env=env)
        for row in listing.split(b"\0")[:-1]:
            meta, path = row.split(b"\t", 1)
            mode, kind, oid = meta.split(b" ")
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                raise ValueError("a CI policy or workflow path is not a regular file")
            files[path.decode()] = subprocess.check_output(command + ["cat-file", "blob", oid.decode()], env=env)
    return files


def harness_errors(checkout, base, head):
    """Shared-harness literal errors at the head, binding at both endpoints."""
    from tauceti_tools import harness_pins

    def snapshot(commit):
        files = harness_files(checkout, commit)
        source = files.get(HARNESS_PATHS[0])
        workflows = {path.rsplit("/", 1)[1]: data.decode() for path, data in files.items()
                     if path.startswith(".github/workflows/") and path.count("/") == 2
                     and path.endswith((".yml", ".yaml"))}
        return (None if source is None else source.decode()), workflows

    head_source, head_workflows = snapshot(head)
    if head_source is None:
        return []
    base_source, base_workflows = snapshot(base)
    try:
        return harness_pins.check(head_source, head_workflows, base_source, base_workflows)
    except harness_pins.PinError as error:
        return [str(error)]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout")
    parser.add_argument("base")
    parser.add_argument("head")
    parser.add_argument("--policy", action="store_true",
                        help="include deletions and emit lines for the merge-policy scope guard")
    args = parser.parse_args()
    try:
        files = changed_files(args.checkout, args.base, args.head, include_deleted=args.policy)
        if args.policy:
            paths = files.split(b"\0")[:-1]
            if any(b"\n" in path or b"\r" in path for path in paths):
                raise ValueError("filename cannot be represented by the scope guard's line format")
            if any(p == b".tauceti/ci-source.json" or p.startswith(b".github/workflows/") for p in paths):
                errors = harness_errors(args.checkout, args.base, args.head)
                for error in errors:
                    print(f"::error::shared harness pin: {error}", file=sys.stderr)
                if errors:
                    sys.exit(1)
            files = b"".join(path + b"\n" for path in paths)
        sys.stdout.buffer.write(files)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"lint-scope: cannot read the immutable local diff: {error}", file=sys.stderr)
        sys.exit(1)
