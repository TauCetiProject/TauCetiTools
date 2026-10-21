"""Prepare and verify forward dependency updates using Lake's own resolver."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .project import ConfigError


def api(path):
    return json.loads(subprocess.check_output(["gh", "api", path], text=True))


def file_at(repo, revision, name):
    value = api(f"repos/{repo}/contents/{name}?ref={revision}")
    return base64.b64decode(value["content"]).decode()


def release(value):
    match = re.fullmatch(r"leanprover/lean4:v(\d+)\.(\d+)\.(\d+)(?:-rc(\d+))?", value)
    if not match:
        raise ConfigError(f"{value!r} is not an official Lean release or release candidate; select one in lean-toolchain before enabling automatic dependency updates")
    major, minor, patch, rc = match.groups()
    return int(major), int(minor), int(patch), rc is None, int(rc or 0)


def packages(manifest):
    result = {row["name"]: row for row in manifest["packages"]}
    if len(result) != len(manifest["packages"]):
        raise ConfigError("Duplicate package names in lake-manifest.json; run lake update and review the manifest")
    return result


def repository(package):
    url = str(package.get("url", "")).rstrip("/").removesuffix(".git")
    match = re.fullmatch(r"https://github.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", url)
    if package.get("type") != "git" or not match or not re.fullmatch(r"[0-9a-f]{40}", package.get("rev", "")):
        raise ConfigError(f"{package['name']} needs a public GitHub Git dependency pinned to a full SHA for automatic updates; review the human-owned Lake requirements")
    return match[1]


def toolchain_for(checkout, direct):
    choices = [(checkout / "lean-toolchain").read_text().strip()]
    for row in direct:
        choices.append(file_at(repository(row), row["rev"], "lean-toolchain").strip())
    # Older dependencies can work with a newer Lean. The normal build decides compatibility.
    return max(choices, key=release)


def require_toml(checkout):
    if not (checkout / "lakefile.toml").is_file() or (checkout / "lakefile.lean").exists():
        raise ConfigError(
            "Tau Ceti projects require lakefile.toml at the root, without lakefile.lean. "
            "If only the Lean file exists, run lake translate-config toml in your content checkout and review "
            "the generated lakefile.toml and retained lakefile.lean.bak. If both formats exist, compare them, "
            "keep the reviewed TOML and move the Lean file to an unused backup name. "
            "Run lake update and lake build and commit the conversion before installing the guard. "
            "Upstream dependencies can still use lakefile.lean."
        )


def inspect(project, checkout: Path):
    require_toml(checkout)
    current = packages(json.loads((checkout / "lake-manifest.json").read_text()))
    proposed = []
    for upstream in project.raw.get("upstreams", []):
        if not upstream.get("update", False):
            continue
        name, repo, branch = upstream["package"], upstream["repo"], upstream.get("branch", "main")
        old = current.get(name)
        if not old or old.get("inherited", False):
            raise ConfigError(f"{name} is not a pinned direct dependency. Set update = false in its [[upstreams]] table and enable its direct parent instead; keep the human-owned lakefile unchanged")
        if repository(old) != repo or old.get("inputRev") != branch:
            raise ConfigError(f"{name}'s URL or nominated branch differs from [[upstreams]]; set repo and branch to the existing Lake requirement, or have a human review a lakefile change")
        commit = api(f"repos/{repo}/commits/{branch}")["sha"]
        if api(f"repos/{repo}/compare/{old['rev']}...{commit}")["status"] not in ("ahead", "identical"):
            raise ConfigError(f"{repo} is backwards or divergent; no update prepared. Review its nominated branch before retrying")
        if commit != old["rev"]:
            proposed.append({"package": name, "repo": repo, "old": old["rev"], "new": commit})
        current[name] = dict(old, rev=commit)
    direct = [row for row in current.values() if not row.get("inherited", False)]
    return proposed, toolchain_for(checkout, direct) if proposed else None


def sandbox_command(directory, sysroot, driver):
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise ConfigError("Lake resolution requires bubblewrap on Linux. On Ubuntu run sudo apt-get install bubblewrap; alternatively open the project's Actions page → Update configured upstreams → Run workflow")
    command = [bwrap, "--tmpfs", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/nix", "/run/current-system/sw",
                 "/etc/ssl", "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
        if Path(path).exists():
            command += ["--ro-bind", path, path]
    certificate = os.environ.get("SSL_CERT_FILE") or os.environ.get("GIT_SSL_CAINFO") or "/etc/ssl/certs/ca-certificates.crt"
    if certificate and Path(certificate).is_file():
        command += ["--ro-bind", str(Path(certificate).resolve()), "/ca-certificates.crt",
                    "--setenv", "SSL_CERT_FILE", "/ca-certificates.crt",
                    "--setenv", "GIT_SSL_CAINFO", "/ca-certificates.crt"]
    command += ["--ro-bind", str(sysroot), "/toolchain", "--ro-bind", str(driver), "/resolve.lean",
                "--bind", str(directory), "/work", "--remount-ro", "/",
                "--setenv", "PATH", "/toolchain/bin:/usr/bin:/bin:/run/current-system/sw/bin",
                "--setenv", "HOME", "/tmp", "--setenv", "GIT_CONFIG_NOSYSTEM", "1",
                "--setenv", "GIT_TERMINAL_PROMPT", "0", "--setenv", "LAKE_NO_CACHE", "true",
                "--chdir", "/work", "--unshare-user", "--unshare-ipc", "--unshare-pid",
                "--unshare-uts", "--unshare-cgroup", "--disable-userns", "--die-with-parent", "--new-session",
                "--", "/toolchain/bin/lean", "--load-dynlib=/toolchain/lib/lean/libLake_shared.so", "--run", "/resolve.lean"]
    return command


def resolve(checkout, direct, toolchain):
    """Resolve the committed root TOML plus authorized direct pins; no PR files or secrets."""
    release(toolchain)
    require_toml(checkout)
    if not shutil.which("elan"):
        raise ConfigError("Lake resolution needs elan. Install it using https://lean-lang.org/install/manual/ or run Actions → Update configured upstreams → Run workflow on GitHub")
    subprocess.run(["elan", "toolchain", "install", toolchain], check=True)
    compiler = Path(subprocess.check_output(["elan", "which", "lean"], env={**os.environ, "ELAN_TOOLCHAIN": toolchain}, text=True).strip())
    sysroot = compiler.resolve().parent.parent
    driver = Path(__file__).parent / "assets/scripts/ResolveDependencies.lean"
    configuration = subprocess.check_output(["git", "-C", str(checkout), "show", "HEAD:lakefile.toml"])
    with tempfile.TemporaryDirectory(prefix="tauceti-resolve-") as tmp:
        directory = Path(tmp)
        (directory / "lakefile.toml").write_bytes(configuration)
        manifest = json.loads((checkout / "lake-manifest.json").read_text())
        manifest["packages"] = direct
        (directory / "lake-manifest.json").write_text(json.dumps(manifest))
        (directory / "lean-toolchain").write_text(toolchain + "\n")
        try:
            # Empty launcher environment also hides secrets from bwrap's PID 1.
            subprocess.run(sandbox_command(directory, sysroot, driver), env={}, check=True, timeout=1200)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise ConfigError("Lake resolution failed; no project pins changed. Read the Lake error above, check the direct requirements with your Lean maintainer, then rerun upstreams update --apply. If the log reports missing Lake APIs or driver compilation errors, this Tools resolver may need updating for the selected Lean version. Resolution uses the committed root TOML and does not run post-update hooks") from error
        return json.loads((directory / "lake-manifest.json").read_text())


def update(project, checkout, *, apply=False):
    proposed, toolchain = inspect(project, checkout)
    print(json.dumps({"dependencies": proposed, "toolchain": toolchain}, indent=2))
    if not apply or not proposed:
        print("No upstream pins moved; no resolution needed." if not proposed else
              "No pins changed. Add --apply to resolve the update with Lake in isolation.")
        return
    if subprocess.run(["git", "-C", str(checkout), "diff", "--quiet", "HEAD", "--", "lakefile.toml", "lakefile.lean", "lake-manifest.json", "lean-toolchain"]).returncode:
        raise ConfigError("Commit or restore the Lake configuration, manifest and toolchain before preparing an update; resolution uses committed base source")
    before = json.loads((checkout / "lake-manifest.json").read_text())
    targets = {row["package"]: row["new"] for row in proposed}
    direct = [dict(row, rev=targets.get(row["name"], row["rev"])) for row in before["packages"] if not row.get("inherited", False)]
    resolved = resolve(checkout, direct, toolchain)
    old_manifest = (checkout / "lake-manifest.json").read_bytes()
    old_toolchain = (checkout / "lean-toolchain").read_bytes()
    try:
        (checkout / "lake-manifest.json").write_text(json.dumps(resolved, indent=2) + "\n")
        (checkout / "lean-toolchain").write_text(toolchain + "\n")
    except BaseException:
        (checkout / "lake-manifest.json").write_bytes(old_manifest)
        (checkout / "lean-toolchain").write_bytes(old_toolchain)
        raise
    print("Lake resolved the dependency pins. Open a dependency PR; the required build and reviews decide whether it can merge. Source adaptation remains separate.")


def guard(project, base: Path, merge_base: Path, candidate: Path):
    paths = ("lakefile.toml", "lakefile.lean", "lean-toolchain", "lake-manifest.json")

    def contents(root, name):
        return (root / name).read_bytes() if (root / name).exists() else b""

    if all(contents(merge_base, name) == contents(candidate, name) for name in paths):
        print("BUMP-GUARD: PASS — no pin or Lake configuration change")
        return
    require_toml(base)
    require_toml(candidate)
    for name in paths[:2]:
        if contents(base, name) != contents(candidate, name):
            raise ConfigError(f"{name} changes are human-owned")
    before = json.loads(contents(base, "lake-manifest.json"))
    after = json.loads(contents(candidate, "lake-manifest.json"))
    actual = packages(after)
    allowed = {u["package"]: u for u in project.raw.get("upstreams", []) if u.get("update")}
    direct = []
    for package in packages(before).values():
        if package.get("inherited", False):
            continue
        name, repo = package["name"], repository(package)
        target = actual.get(name)
        if not target or target.get("inherited", False) or not re.fullmatch(r"[0-9a-f]{40}", target.get("rev", "")):
            raise ConfigError(f"Direct dependency {name} removed or not pinned to an immutable Git revision")
        revision = target["rev"]
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
        direct.append(dict(package, rev=revision))
    toolchain = toolchain_for(base, direct)
    if contents(candidate, "lean-toolchain").decode().strip() != toolchain:
        raise ConfigError(f"Expected {toolchain}, chosen from the base and pinned direct dependencies; candidate toolchain differs")
    expected = resolve(base, direct, toolchain)
    if {**expected, "packages": packages(expected)} != {**after, "packages": actual}:
        raise ConfigError("Candidate manifest differs from Lake's resolution of the trusted base and authorized direct pins")
    print("BUMP-GUARD: PASS — forward dependency pins resolved by Lake")
