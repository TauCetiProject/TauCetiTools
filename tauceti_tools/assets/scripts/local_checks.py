#!/usr/bin/env python3
"""Shared local content checks for Lake drivers and worker verification.

These checks use local Git state only. The full transitive/ownership audits remain in CI.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tauceti_tools.assets.scripts import ci_git
from tauceti_tools.local_scope import LEAN_ROOT, POLICIES, git, local_main, scope, untracked

SCRIPTS = Path(__file__).resolve().parent


class Violation(ValueError):
    pass


def run(name, *args, env=None):
    command = [sys.executable] if name.endswith(".py") else ["bash", "-o", "pipefail"]
    subprocess.run([*command, str(SCRIPTS / name), *map(str, args)], env=env, check=True)


def sources(root, selected):
    # Use the same source inventory and header parser as CI. It rejects symlinks too.
    run("check-imports.py", "--root", root)
    directory = root / LEAN_ROOT.replace(".", "/")
    root_module = directory.with_suffix(".lean")
    run("check_source_options.py", directory, root_module)
    boundary = re.compile(r"(?m)^\s*(?:public\s+)?import\s+(?:TauCetiRoadmap|TauCetiReview)\b")
    for path in [root_module, *directory.rglob("*.lean")]:
        if boundary.search(path.read_text()):
            raise Violation(f"{path}: library sources must not import roadmap/review modules")
    diff_guards(root, selected)
    print("local-sources: PASS", flush=True)


def working_tree_commit(root):
    """A commit of the tree a push would carry: HEAD plus every tracked edit and untracked library file.

    It lives only in the object database. Untracked files outside the library are scratch,
    not PR content, so they are left out, as the local scope's untracked inventory does.
    """
    with tempfile.TemporaryDirectory(prefix="local-guard-index-") as name:
        index = Path(name) / "index"
        current = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip())
        if current.is_file():
            shutil.copyfile(current, index)  # keeps the stat cache, so unchanged files are not rehashed
        env = dict(
            os.environ,
            GIT_INDEX_FILE=str(index),
            GIT_AUTHOR_NAME="lake lint",
            GIT_AUTHOR_EMAIL="lake-lint@localhost",
            GIT_COMMITTER_NAME="lake lint",
            GIT_COMMITTER_EMAIL="lake-lint@localhost",
        )

        def command(*args, stdin=None):
            return subprocess.run(
                ["git", "--no-optional-locks", "-C", str(root), *args],
                env=env,
                input=stdin,
                check=True,
                capture_output=True,
            ).stdout

        if not current.is_file():
            command("read-tree", "HEAD")
        command("add", "--update", "--", ".")
        library = LEAN_ROOT.replace(".", "/")
        extra = sorted(p for p in untracked(root) if p == library + ".lean" or p.startswith(library + "/"))
        if extra:
            command(
                "add",
                "--force",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                stdin=b"".join(os.fsencode(p) + b"\0" for p in extra),
            )
        tree = command("write-tree").decode().strip()
        return command("commit-tree", tree, "-p", "HEAD", "-m", "lake lint working tree").decode().strip()


def diff_guards(root, selected):
    """PR CI's diff guards (review size, new-file length, scope) on the working tree.

    The same shared function as PR CI, queue admission and the staging member policy,
    measured from the same merge base as the rest of the local scope (`--base` overrides it).
    """
    head = working_tree_commit(root)
    with ci_git.repository_reader(root) as reader:
        result = ci_git.pr_guards(reader, selected["merge_base"], head, lean_root=LEAN_ROOT)
    print(
        f"diff size: +{result['additions']} / -{result['deletions']} across {result['files']} files"
        f" against {selected['merge_base']}",
        flush=True,
    )
    if result["violations"]:
        raise Violation("PR diff guards failed:\n" + "\n".join("  " + m for m in result["violations"]))


def dot_notation(root, selected, modules):
    """PR CI's dot-notation namespace lint, scoped to changed modules exactly as CI scopes it."""
    library = LEAN_ROOT.replace(".", "/")
    baseline = root / "scripts/lint-dot-notation-baseline.txt"  # the project's policy file wins
    if not baseline.is_file():
        baseline = SCRIPTS / "lint-dot-notation-baseline.txt"
    args = [
        "--baseline",
        baseline,
        "--mathlib-root",
        root / ".lake/packages/mathlib/Mathlib",
        "--source-root",
        root / library,
        "--context-cache",
        root / ".lake/dot-notation-context/context.json",
    ]
    with tempfile.TemporaryDirectory(prefix="local-dot-notation-") as name:
        if selected["mode"] == "scoped":
            directory = Path(name)
            archive = directory / "base.tar"
            archive.write_bytes(git(root, "archive", selected["merge_base"], library))
            subprocess.run(["tar", "-xf", str(archive), "-C", str(directory)], check=True)
            args += ["--only-modules", modules, "--base-source-root", directory / library]
        run("lint-dot-notation.py", *args)


def shims(root, selected, policy):
    manifest = root / LEAN_ROOT.replace(".", "/") / "mathlib-shims.json"
    merge = selected["merge_base"]
    relative = manifest.relative_to(root).as_posix()
    try:
        baseline = git(root, "show", f"{merge}:{relative}")
    except subprocess.CalledProcessError:
        baseline = None
    if not manifest.exists() and baseline is None:
        return  # Checkouts before the shim registry have no obligations.
    with tempfile.TemporaryDirectory(prefix="local-shims-") as name:
        directory = Path(name)
        args = ["--repo-root", root, "--manifest", manifest, "--fail-on-available"]
        if baseline is not None:
            (directory / "mathlib-shims.json").write_bytes(baseline)
            archive = directory / "base.tar"
            archive.write_bytes(git(root, "archive", merge, LEAN_ROOT.replace(".", "/")))
            subprocess.run(["tar", "-xf", str(archive), "-C", str(directory)], check=True)
            args += ["--base-manifest", directory / "mathlib-shims.json", "--base-root", directory]
            pins = git(
                root, "diff", "--no-ext-diff", "--name-only", merge, "--", "lake-manifest.json", "lean-toolchain"
            )
            if not pins and policy != "bump":
                args.append("--only-new")
        run("check-expired-mathlib-shims.py", *args)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase",
        choices=(
            "verify",
            "test",
            "lint",
            "sources",
            "axioms",
            "module-system",
            "style",
            "dot-notation",
            "environment",
            "shim-expiry",
        ),
    )
    parser.add_argument("--base", default=os.environ.get("TAUCETI_VERIFY_BASE"))
    parser.add_argument(
        "--policy", choices=sorted(POLICIES), default=os.environ.get("TAUCETI_VERIFY_POLICY", "ordinary")
    )
    parser.add_argument("--modules", type=Path, help="Existing worker-selected module list")
    parser.add_argument("--lint-modules", type=Path, help="Additional defining modules named by CI lint findings")
    args = parser.parse_args(argv)
    root = Path(os.environ.get("TAUCETI_PROJECT_ROOT", ".")).resolve()
    os.chdir(root)
    # Ambient CI filters must not accidentally narrow the local contract.
    env = dict(os.environ)
    for key in ("AXIOMS_ONLY_MODULES", "LINT_ONLY_MODULES", "LINT_CHANGED_ONLY"):
        env.pop(key, None)
    env.update(TAUCETI_PROJECT_ROOT=str(root), TAUCETI_TRUSTED_SCRIPTS=str(SCRIPTS))
    if args.phase == "module-system":
        return subprocess.call(["lake", "env", "lean", "--run", str(SCRIPTS / "ModuleSystem.lean")], env=env)
    selected = scope(root, args.base or local_main(root), args.policy)
    print(f"scope: {selected['mode']}: {selected['reason']}", flush=True)
    if selected["mode"] == "unknown":
        raise ValueError(f"cannot determine local check scope: {selected['reason']}")
    with tempfile.TemporaryDirectory(prefix="local-checks-") as name:
        modules = Path(name) / "modules.txt"
        modules.write_text("".join(module + "\n" for module in selected["modules"]))
        if args.modules:
            modules = args.modules.resolve()
        if args.phase in ("sources", "lint", "verify"):
            sources(root, selected)
        if args.phase == "verify":
            subprocess.run(["lake", "build", "--iofail"], env=env, check=True)
        if args.phase in ("shim-expiry", "lint", "verify"):
            shims(root, selected, args.policy)
        if args.phase in ("axioms", "test", "verify"):
            run("local_axioms.py", "--modules", modules, "--root", LEAN_ROOT, env=env)
        if args.phase in ("test", "verify"):
            subprocess.run(["lake", "env", "lean", "--run", str(SCRIPTS / "ModuleSystem.lean")], env=env, check=True)
        if args.lint_modules:
            lint_modules = set(modules.read_text().splitlines()) | set(args.lint_modules.read_text().splitlines())
            modules = Path(name) / "lint-modules.txt"
            modules.write_text("".join(m + "\n" for m in sorted(lint_modules) if m))
        if args.phase in ("style", "lint", "verify"):
            env.update(LINT_CHANGED_ONLY="1" if selected["mode"] == "scoped" else "0", LINT_ONLY_MODULES=str(modules))
            run("lint-style.sh", env=env)
        if args.phase in ("dot-notation", "lint", "verify"):
            dot_notation(root, selected, modules)
        if args.phase in ("environment", "lint", "verify"):
            run("local_lint.py", "--modules", modules, env=env)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None
    except Violation as error:
        print(f"local-checks: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError) as error:
        print(f"local-checks: {error}", file=sys.stderr)
        raise SystemExit(75) from None
