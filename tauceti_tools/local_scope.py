"""Offline actual-source scope shared by local Lake commands and worker verification."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .forge import GITHUB, RepositoryRef
from .project import load

BASE_BRANCH = os.environ.get("TAUCETI_BASE_BRANCH", "main")
LEAN_ROOT = os.environ.get("TAUCETI_LEAN_ROOT", "TauCeti")
TAUCETI = os.environ.get("TAUCETI_REPO", "TauCetiProject/TauCeti")

CANONICAL = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    + re.escape(TAUCETI)
    + r"(?:\.git)?/?$",
    re.I,
)
MODULE = re.compile(r"^" + re.escape(LEAN_ROOT.replace(".", "/")) + r"(/[A-Za-z_][A-Za-z0-9_']*)+\.lean$")
PINS = {"lake-manifest.json", "lean-toolchain", "lakefile.toml", "lakefile.lean"}
POLICIES = {"ordinary", "full-lint", "lint-repair", "bump"}


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *args], check=True, capture_output=True
    ).stdout


def local_main(root: Path) -> str:
    """Resolve the configured repository's local base ref without network access."""
    source = os.environ.get("TAUCETI_CONF")
    if not source and (root / "tauceti.toml").is_file():
        source = str(root / "tauceti.toml")
    if source:
        snapshot = os.environ.get("TAUCETI_CONF_SNAPSHOT") if os.environ.get("TAUCETI_CONF_SOURCE") == source else None
        if not snapshot and source.startswith(("http://", "https://")):
            raise ValueError("local verification needs a saved project snapshot; bootstrap the configured consumer first")
        project = load(snapshot or source)
        repository, branch = project.repository_ref("content"), project.branch
    else:
        repository, branch = RepositoryRef(GITHUB, TAUCETI), BASE_BRANCH
    for remote in git(root, "remote").decode().splitlines():
        if repository.matches_remote(git(root, "config", "--get", f"remote.{remote}.url").decode().strip()):
            try:
                return (
                    git(root, "rev-parse", "--verify", f"refs/remotes/{remote}/{branch}^{{commit}}")
                    .decode()
                    .strip()
                )
            except subprocess.CalledProcessError:
                continue
    raise ValueError(f"no local canonical {repository.key} {branch} ref")


def untracked(root: Path) -> set[str]:
    raw = git(root, "ls-files", "-z", "--others", "--exclude-standard", "--", ".")
    raw += git(root, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--", LEAN_ROOT.replace(".", "/"))
    return {p.decode(errors="surrogateescape") for p in raw.split(b"\0") if p}


def scope(root: Path, base: str, policy: str) -> dict:
    if policy not in POLICIES:
        raise ValueError(f"unknown local verification scope policy {policy!r}")
    result = dict(mode="unknown", reason="", modules=[], changed=[], base=base, merge_base=None, policy=policy)
    try:
        if git(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
            raise ValueError("shallow history")
        merge = git(root, "merge-base", base, "HEAD").decode().strip()
        result["merge_base"] = merge
        raw = git(
            root,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--no-relative",
            "--name-only",
            "-z",
            merge,
            "--",
        )
        changed = {p.decode(errors="surrogateescape") for p in raw.split(b"\0") if p} | untracked(root)
        result["changed"] = sorted(changed)
    except (subprocess.CalledProcessError, ValueError) as error:
        result["reason"] = str(error)
        return result
    reasons = []
    modules = []
    for name in sorted(changed):
        if name in PINS:
            reasons.append(f"dependency, toolchain or lakefile change: {name}")
        elif name.endswith(".lean"):
            if not MODULE.fullmatch(name):
                reasons.append(f"Lean file outside the module inventory: {name}")
            elif not (root / name).is_file():
                reasons.append(f"deleted module: {name}")
            else:
                modules.append(name[:-5].replace("/", "."))
    if policy != "ordinary":
        reasons.append(f"{policy} policy")
    result.update(
        mode="full" if reasons else "scoped",
        reason=reasons[0] if reasons else f"against {merge}",
        modules=sorted(set(modules)),
    )
    return result
