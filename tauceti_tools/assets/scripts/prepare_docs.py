#!/usr/bin/env python3
"""Prepare a nested doc-gen4 project using the project's exact dependency pins."""

import json
import urllib.request
from pathlib import Path

from tauceti_tools.project import bootstrap

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def main():
    project = bootstrap([])
    pin = project.tool("docgen")
    if not pin.get("ref"):
        raise SystemExit("Select a doc-gen4 revision compatible with lean-toolchain in [tools.docgen] repo and ref")
    root = Path.cwd()
    package = tomllib.loads((root / "lakefile.toml").read_text())["name"]
    url = f"https://raw.githubusercontent.com/{pin['repo']}/{pin['ref']}/lake-manifest.json"
    with urllib.request.urlopen(url, timeout=30) as response:
        docs = json.load(response)
    # Inherit doc-gen4's locked dependencies; project dependencies take precedence.
    packages = {p["name"]: {**p, "inherited": True} for p in docs["packages"]}
    for p in json.loads((root / "lake-manifest.json").read_text())["packages"]:
        packages[p["name"]] = {**p, "inherited": True}
    packages["«doc-gen4»"] = {
        "name": "«doc-gen4»", "type": "git", "url": f"https://github.com/{pin['repo']}",
        "rev": pin["ref"], "inputRev": pin["ref"], "subDir": None, "scope": "",
        "manifestFile": "lake-manifest.json", "configFile": "lakefile.lean", "inherited": False,
    }
    packages[package] = {
        "name": package, "type": "path", "dir": "../", "scope": "",
        "manifestFile": "lake-manifest.json", "configFile": "lakefile.toml", "inherited": False,
    }
    directory = root / "docbuild"
    directory.mkdir(exist_ok=True)
    (directory / "lakefile.toml").write_text(
        'name = "docbuild"\nreservoir = false\npackagesDir = "../.lake/packages"\n\n'
        '[[require]]\nname = "doc-gen4"\n'
        f'git = "https://github.com/{pin["repo"]}"\nrev = "{pin["ref"]}"\n\n'
        f'[[require]]\nname = {json.dumps(package)}\npath = "../"\n\n'
        '[[lean_lib]]\nname = "ProjectDocs"\n'
    )
    docs.update(name="docbuild", packagesDir="../.lake/packages", packages=list(packages.values()))
    (directory / "lake-manifest.json").write_text(json.dumps(docs, indent=2) + "\n")
    (directory / "lean-toolchain").write_bytes((root / "lean-toolchain").read_bytes())
    modules = sorted({
        ".".join(path.relative_to(root).with_suffix("").parts)
        for name in project.roots
        for path in (root / name.replace(".", "/")).rglob("*.lean")
    } | set(project.roots))
    (directory / "ProjectDocs.lean").write_text(
        "".join(f"import {name}\n" for name in modules)
    )
    print(f"Prepared docbuild with {len(modules)} library modules and doc-gen4 @ {pin['ref']}")


if __name__ == "__main__":
    main()
