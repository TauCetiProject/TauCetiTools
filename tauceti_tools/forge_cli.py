"""Experimental gh-compatible direct Forgejo reads, with explicit GitHub delegation.

Invoked as tauceti-gh while the worker integration is prepared. This does not
install a global gh replacement or activate Forgejo in legacy worker bindings.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.parse

from .forge import GITHUB, ForgeError, Forgejo, RepositoryRef, Unsupported
from .project import ConfigError, configuration_source, load


def _emit(value, fields, query):
    if fields:

        def project(row):
            missing = set(fields) - set(row)
            if missing:
                raise Unsupported("unsupported JSON fields: " + ", ".join(sorted(missing)))
            return {field: row[field] for field in fields}

        value = [project(row) for row in value] if isinstance(value, list) else project(value)
    rendered = json.dumps(value, ensure_ascii=False) + "\n"
    if query:
        result = subprocess.run(["jq", "-r", query], input=rendered, text=True, capture_output=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    sys.stdout.write(rendered)
    return 0


def _pull(row):
    result = {key: row.get(key) for key in ("number", "title", "body", "labels", "created_at", "updated_at")}
    result.update(
        url=row.get("html_url"),
        state="MERGED" if row.get("merged") else str(row.get("state", "")).upper(),
        author={"login": (row.get("user") or {}).get("login")},
        headRefOid=(row.get("head") or {}).get("sha"),
        headRefName=(row.get("head") or {}).get("ref"),
        baseRefName=(row.get("base") or {}).get("ref"),
        isDraft=bool(row.get("draft")),
        mergedAt=row.get("merged_at"),
        createdAt=row.get("created_at"),
        updatedAt=row.get("updated_at"),
    )
    return result


def run(argv, *, environment=None):
    env = os.environ if environment is None else environment
    parser = argparse.ArgumentParser(prog="tauceti-gh", description=__doc__)
    parser.add_argument("--conf")
    parser.add_argument("-R", "--repo")
    parser.add_argument("--json")
    parser.add_argument("--jq", "-q")
    args, command = parser.parse_known_args(argv)
    source = configuration_source(args.conf or env.get("TAUCETI_CONF"))
    if not source:
        return _delegate(argv, env)
    project = load(source)
    selected = args.repo or env.get("GH_REPO")
    refs = [project.repository_ref(role) for role in project.raw["repositories"]]
    if not selected:
        repository = project.repository_ref("content")
    else:
        matches = [
            ref
            for ref in refs
            if selected in {ref.name, ref.url, urllib.parse.urlsplit(ref.url).netloc + "/" + ref.name}
        ]
        if len(set(matches)) > 1:
            raise Unsupported("ambiguous repository; specify HOST/OWNER/REPO")
        if matches:
            repository = matches[0]
        elif selected.startswith(("github.com/", "https://github.com/")):
            repository = RepositoryRef(GITHUB, selected.removeprefix("https://").removeprefix("github.com/"))
        else:
            raise Unsupported(
                "repository is not configured; specify an explicit GitHub host or add its forge reference"
            )
    if repository.forge.kind == "github":
        # Fully qualify the chosen target: the checkout may have a Forgejo origin.
        host = urllib.parse.urlsplit(repository.forge.url).netloc
        return _delegate(argv, {**env, "GH_REPO": host + "/" + repository.name, "GH_HOST": host}, remove_conf=True)
    if not command:
        parser.error("a command is required")
    token_file = env.get("TAUCETI_FORGEJO_TOKEN_FILE")
    token = None
    if token_file:
        from pathlib import Path

        with Path(token_file).open() as credential:
            if os.fstat(credential.fileno()).st_mode & 0o077:
                raise ForgeError("Forgejo token file must be private (chmod 600)")
            token = credential.read().strip()
        if not token:
            raise ForgeError("Forgejo token file is empty")
    api = Forgejo(repository.forge, token=token)
    fields = args.json.split(",") if args.json else None
    if command[:2] == ["pr", "view"] and len(command) == 3:
        value = _pull(api.pull(repository, int(command[2])))
        if fields and "comments" in fields:
            value["comments"] = [
                {
                    "id": row["id"],
                    "body": row.get("body"),
                    "author": {"login": (row.get("user") or {}).get("login")},
                    "url": row.get("html_url"),
                }
                for row in api.comments(repository, int(command[2]))
            ]
    elif command[:2] == ["pr", "list"]:
        listing = argparse.ArgumentParser(prog="gh pr list")
        listing.add_argument("--state", choices=["open", "closed", "merged", "all"], default="open")
        listing.add_argument("--limit", type=int, default=30)
        options = listing.parse_args(command[2:])
        if options.limit < 1:
            raise ValueError("limit must be positive")
        rows = api.pulls(repository, state="closed" if options.state == "merged" else options.state)
        value = [_pull(row) for row in rows if options.state != "merged" or row.get("merged")][: options.limit]
    else:
        raise Unsupported(
            "Forgejo command is not implemented yet; supported: pr view, pr list. No GitHub fallback was attempted."
        )
    return _emit(value, fields, args.jq)


def _delegate(argv, env, *, remove_conf=False):
    executable = env.get("TAUCETI_REAL_GH") or shutil.which("gh", path=env.get("PATH"))
    if not executable or os.path.realpath(executable) == os.path.realpath(sys.argv[0]):
        raise Unsupported("real GitHub CLI not found; set TAUCETI_REAL_GH")
    args = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if remove_conf and arg in {"--conf", "--repo", "-R"}:
            skip = True
        elif not (remove_conf and arg.startswith(("--conf=", "--repo=", "-R"))):
            args.append(arg)
    return subprocess.run([executable, *args], env=env).returncode


def main():
    try:
        return run(sys.argv[1:])
    except (ForgeError, ConfigError, ValueError, OSError) as error:
        print(f"gh: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
