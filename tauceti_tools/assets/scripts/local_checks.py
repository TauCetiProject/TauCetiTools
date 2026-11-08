#!/usr/bin/env python3
"""Shared local content checks for Lake drivers and worker verification.

These checks use local Git state only. The full transitive/ownership audits remain in CI.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

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
    # The new-file limit is relative to the same merge base as the existing local scope.
    added = {
        name.decode(errors="surrogateescape")
        for name in git(
            root,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            "--diff-filter=A",
            "--name-only",
            "-z",
            selected["merge_base"],
            "--",
        ).split(b"\0")
        if name
    } | untracked(root)
    for name in selected["changed"]:
        path = root / name
        if name.endswith(".lean") and path.is_file() and name in added:
            count = len(path.read_bytes().splitlines())
            if count > 1000:
                raise Violation(f"{name}: new Lean files must be at most 1000 lines (found {count})")
    print("local-sources: PASS", flush=True)


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
        choices=("verify", "test", "lint", "sources", "axioms", "module-system", "style", "environment", "shim-expiry"),
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
