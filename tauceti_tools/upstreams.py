"""Inspect and prepare consistent dependency updates without changing lakefiles."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path

from .project import ConfigError


def api(path):
    return json.loads(subprocess.check_output(["gh", "api", path], text=True))


def file_at(repo, revision, name):
    value = api(f"repos/{repo}/contents/{name}?ref={revision}")
    return base64.b64decode(value["content"]).decode()


def inspect(project, checkout: Path):
    current = json.loads((checkout / "lake-manifest.json").read_text())
    packages = {row["name"]: row for row in current["packages"]}
    proposed = []
    shared = {}
    sources = {}
    toolchains = {}
    for upstream in project.raw.get("upstreams", []):
        if not upstream.get("update", False):
            continue
        package = upstream["package"]
        if package not in packages:
            raise ConfigError(
                f"{package} is not in the pinned manifest; add it to the human-owned Lake dependencies first"
            )
        repo = upstream["repo"]
        branch = upstream.get("branch", "main")
        old = packages[package]
        if old.get("inherited", False):
            raise ConfigError(
                f"{package} is inherited, not a direct dependency. Set update = false in its [[upstreams]] table "
                "and enable updates for its direct parent instead; keep the human-owned lakefile unchanged."
            )
        commit = api(f"repos/{repo}/commits/{branch}")["sha"]
        expected_url = f"https://github.com/{repo}"
        if old.get("url", "").removesuffix(".git").rstrip("/") != expected_url:
            raise ConfigError(
                f"{package}'s existing URL does not match configured {repo}; repository changes require human review"
            )
        relation = api(f"repos/{repo}/compare/{old['rev']}...{commit}")["status"]
        if relation not in ("ahead", "identical"):
            raise ConfigError(f"{repo} is {relation}; refusing a backwards or divergent update")
        toolchain = file_at(repo, commit, "lean-toolchain").strip()
        toolchains.setdefault(toolchain, []).append(f"{repo}@{commit}")
        manifest = json.loads(file_at(repo, commit, "lake-manifest.json"))
        for row in manifest["packages"]:
            binding = (row.get("type"), row.get("url"), row.get("rev"))
            if row["name"] in shared and shared[row["name"]] != binding:
                raise ConfigError(
                    f"Automatic update blocked: {row['name']} differs. "
                    f"{sources[row['name']]} requires {shared[row['name']][1]}@{shared[row['name']][2]}; "
                    f"{repo}@{commit} requires {binding[1]}@{binding[2]}. "
                    "No pins changed. Wait for compatible nominated branch heads and rerun upstreams inspect, "
                    "or set update = false for all enabled [[upstreams]] entries to pause automatic updates. "
                    "This exact-match policy does not establish that your existing Lake project is broken; "
                    "a deliberate dependency override needs human integration and review."
                )
            shared[row["name"]] = binding
            sources[row["name"]] = f"{repo}@{commit}"
        # A direct dependency can also be inherited through another upstream.
        binding = ("git", expected_url, commit)
        if package in shared:
            previous = shared[package]
            if (previous[0], str(previous[1]).removesuffix(".git"), previous[2]) != binding:
                raise ConfigError(
                    f"{package}'s direct and inherited pins disagree; update its downstream upstream first"
                )
        shared[package] = binding
        sources[package] = f"{repo}@{commit}"
        proposed.append({"package": package, "repo": repo, "old": old["rev"], "new": commit})
    if len(toolchains) > 1:
        versions = "; ".join(f"{', '.join(repos)} requires {version}" for version, repos in toolchains.items())
        raise ConfigError(
            "Automatic update blocked: " + versions + ". No pins changed. Wait for upstream branch heads "
            "with a common Lean toolchain and rerun upstreams inspect, or set update = false for all enabled "
            "[[upstreams]] entries. A human must choose and build any deliberate integration."
        )
    return proposed, next(iter(toolchains), None)


def update(project, checkout, *, apply=False):
    proposed, toolchain = inspect(project, checkout)
    print(json.dumps({"dependencies": proposed, "toolchain": toolchain}, indent=2))
    if not apply or not proposed:
        print("No pins changed. Add --apply to prepare the update in this checkout.")
        return
    old_manifest = (checkout / "lake-manifest.json").read_bytes()
    old_toolchain = (checkout / "lean-toolchain").read_bytes()
    try:
        base = json.loads(old_manifest)
        targets = {row["package"]: row["new"] for row in proposed}
        direct = [
            dict(row, rev=targets.get(row["name"], row.get("rev")))
            for row in base["packages"]
            if not row.get("inherited", False)
        ]
        resolved = {}
        for row in direct:
            repo = row["url"].removeprefix("https://github.com/").removesuffix(".git")
            inherited_manifest = json.loads(file_at(repo, row["rev"], "lake-manifest.json"))
            for inherited in inherited_manifest["packages"]:
                value = dict(inherited, inherited=True)
                if inherited["name"] in resolved and resolved[inherited["name"]] != value:
                    raise ConfigError(f"Exact upstream manifests disagree on {inherited['name']}")
                resolved[inherited["name"]] = value
        for row in direct:
            inherited = resolved.get(row["name"])
            if inherited and any(inherited.get(k) != row.get(k) for k in ("url", "rev", "type")):
                raise ConfigError(f"Direct and inherited pins disagree on {row['name']}")
            resolved[row["name"]] = row
        base["packages"] = sorted(resolved.values(), key=lambda row: row["name"])
        (checkout / "lake-manifest.json").write_text(json.dumps(base, indent=2) + "\n")
        (checkout / "lean-toolchain").write_text(toolchain + "\n")
    except BaseException:
        (checkout / "lake-manifest.json").write_bytes(old_manifest)
        (checkout / "lean-toolchain").write_bytes(old_toolchain)
        raise
    print(
        "Updated only the dependency pins/toolchain. Review the diff and open a dependency PR against the default branch; source adaptation is a separate worker phase."
    )


def guard(project, base: Path, merge_base: Path, candidate: Path):
    """Derive candidate pins from exact trusted upstream manifests; execute no candidate code."""
    paths = ("lakefile.toml", "lakefile.lean", "lean-toolchain", "lake-manifest.json")

    def contents(root, name):
        return (root / name).read_bytes() if (root / name).exists() else b""

    if all(contents(merge_base, name) == contents(candidate, name) for name in paths):
        print("BUMP-GUARD: PASS — no pin or Lake configuration change")
        return
    for name in paths[:2]:
        if contents(base, name) != contents(candidate, name):
            raise ConfigError(f"{name} changes are human-owned")
    before = json.loads(contents(base, "lake-manifest.json"))
    after = json.loads(contents(candidate, "lake-manifest.json"))
    if {k: v for k, v in before.items() if k != "packages"} != {k: v for k, v in after.items() if k != "packages"}:
        raise ConfigError("Root manifest metadata changed")
    actual = {row["name"]: row for row in after["packages"]}
    if len(actual) != len(after["packages"]):
        raise ConfigError("Duplicate package names")
    allowed = {u["package"]: u for u in project.raw.get("upstreams", []) if u.get("update")}
    direct = [row for row in before["packages"] if not row.get("inherited", False)]
    expected = {}
    toolchains = set()
    for package in direct:
        name = package["name"]
        target = actual.get(name)
        if not target:
            raise ConfigError(f"Direct dependency {name} removed")
        if {k: v for k, v in package.items() if k != "rev"} != {k: v for k, v in target.items() if k != "rev"}:
            raise ConfigError(f"Direct dependency {name} configuration changed")
        repo = str(package.get("url", "")).removeprefix("https://github.com/").removesuffix(".git").rstrip("/")
        revision = target.get("rev", "")
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ConfigError(f"{name} needs an immutable revision")
        if package["rev"] != revision:
            policy = allowed.get(name)
            if not policy or policy["repo"] != repo:
                raise ConfigError(f"{name} is not configured for automatic updates")
            if api(f"repos/{repo}/compare/{package['rev']}...{revision}")["status"] != "ahead":
                raise ConfigError(f"{name} did not move forward")
            branch = package.get("inputRev")
            if branch != policy.get("branch", "main"):
                raise ConfigError(f"{name} nominated branch differs from protected policy")
            if api(f"repos/{repo}/compare/{revision}...{branch}")["status"] not in ("ahead", "identical"):
                raise ConfigError(f"{name} target is outside its nominated branch")
        upstream = json.loads(file_at(repo, revision, "lake-manifest.json"))
        toolchains.add(file_at(repo, revision, "lean-toolchain").strip())
        for row in upstream["packages"]:
            inherited = dict(row, inherited=True)
            previous = expected.get(row["name"])
            if previous and previous != inherited:
                raise ConfigError(f"Upstream manifests disagree on {row['name']}")
            expected[row["name"]] = inherited
    for package in direct:
        target = actual[package["name"]]
        inherited = expected.get(package["name"])
        if inherited and any(inherited.get(k) != target.get(k) for k in ("url", "rev", "type")):
            raise ConfigError(f"Direct and inherited pins disagree on {package['name']}")
        expected[package["name"]] = target
    if expected != actual:
        raise ConfigError("Candidate manifest is not derived from the exact configured upstreams")
    candidate_toolchain = contents(candidate, "lean-toolchain").decode().strip()
    if len(toolchains) != 1 or candidate_toolchain not in toolchains:
        raise ConfigError("Toolchain differs from the agreed upstream toolchain")

    def version(value):
        match = re.fullmatch(r"leanprover/lean4:v(\d+)\.(\d+)\.(\d+)", value)
        return tuple(map(int, match.groups())) if match else None

    previous = contents(base, "lean-toolchain").decode().strip()
    if previous != candidate_toolchain and (
        not version(previous) or not version(candidate_toolchain) or version(candidate_toolchain) < version(previous)
    ):
        raise ConfigError("Toolchain movement needs human review; only forward stable releases are automatic")
    print("BUMP-GUARD: PASS — consistent forward dependency pins")
