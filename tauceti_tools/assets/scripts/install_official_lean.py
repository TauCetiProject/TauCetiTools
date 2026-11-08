#!/usr/bin/env python3
"""Install stock Linux Lean; recover release redirects through verified official asset metadata."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

CURL = ["curl", "--fail", "--silent", "--show-error", "--location", "--retry", "2",
        "--retry-all-errors", "--retry-delay", "1", "--connect-timeout", "10"]


def install(toolchain, elan):
    if not re.fullmatch(r"leanprover/lean4:v[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?", toolchain):
        raise ValueError("only official Lean release toolchains are supported")
    for attempt in range(3):
        if subprocess.run([elan, "toolchain", "install", toolchain]).returncode == 0:
            return
        if attempt < 2:
            time.sleep(5 * (attempt + 1))
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "amd64"):
        raise RuntimeError("official asset fallback supports Linux x86_64 only")
    version = toolchain.split(":", 1)[1]
    asset_name = f"lean-{version.removeprefix('v')}-linux.tar.zst"
    with tempfile.TemporaryDirectory(prefix="lean-release-download-") as scratch:
        scratch = Path(scratch)
        metadata = scratch / "release.json"
        subprocess.run(CURL + ["--max-time", "60", "--retry-max-time", "120", "-H",
                               "Accept: application/vnd.github+json",
                               f"https://api.github.com/repos/leanprover/lean4/releases/tags/{version}",
                               "-o", str(metadata)], check=True)
        release = json.loads(metadata.read_text())
        if release.get("tag_name") != version or release.get("draft"):
            raise ValueError("official release metadata does not match the requested toolchain")
        asset = next((a for a in release.get("assets", []) if a.get("name") == asset_name), None)
        if not asset or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""):
            raise ValueError("the exact official release asset has no SHA-256 digest")
        url = asset.get("url", "")
        if not re.fullmatch(r"https://api\.github\.com/repos/leanprover/lean4/releases/assets/[0-9]+", url):
            raise ValueError("asset URL is outside the official Lean release API")
        archive = scratch / "lean.tar.zst"
        subprocess.run(CURL + ["--max-time", "600", "--retry-max-time", "1200", "-H",
                               "Accept: application/octet-stream", url, "-o", str(archive)], check=True)
        digest = hashlib.sha256()
        with archive.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        if "sha256:" + digest.hexdigest() != asset["digest"]:
            raise ValueError("official Lean archive failed SHA-256 verification")
        target = Path(tempfile.mkdtemp(prefix=f"official-lean-{version}-", dir=os.environ.get("RUNNER_TEMP")))
        try:
            subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", str(target),
                            "--strip-components=1"], check=True)
            subprocess.run([elan, "toolchain", "link", toolchain, str(target)], check=True)
            subprocess.run([elan, "run", toolchain, "lean", "--version"], check=True)
        except BaseException:
            shutil.rmtree(target)
            raise
        print(f"Installed verified official {toolchain} through the release asset API", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("toolchain")
    parser.add_argument("--elan", default=shutil.which("elan") or "elan")
    args = parser.parse_args()
    try:
        install(args.toolchain, args.elan)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"official Lean installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
