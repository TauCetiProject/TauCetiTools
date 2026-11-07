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

from .forge import GITHUB, Forge, ForgeError, Forgejo, RepositoryRef, Unsupported
from .forge_read import ForgejoReader
from .project import ConfigError, load


def _emit(value, query):
    rendered = json.dumps(value, ensure_ascii=False) + "\n"
    if query:
        result = subprocess.run(["jq", "-r", query], input=rendered, text=True, capture_output=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    sys.stdout.write(rendered)
    return 0


def _checkout_repository(refs):
    result = subprocess.run(["git", "config", "--get", "remote.origin.url"], capture_output=True, text=True)
    remote = result.stdout.strip() if result.returncode == 0 else ""
    if remote.startswith("git@") and ":" in remote:
        host, path = remote[4:].split(":", 1)
    else:
        parsed = urllib.parse.urlsplit(remote)
        host, path = parsed.netloc.rsplit("@", 1)[-1], parsed.path.lstrip("/")
    path = path.removesuffix(".git").rstrip("/")
    matches = {
        ref
        for ref in refs
        if host.lower() == urllib.parse.urlsplit(ref.forge.url).netloc and path.lower() == ref.name.lower()
    }
    if len(matches) != 1:
        raise Unsupported("checkout origin is not uniquely configured; specify -R HOST/OWNER/REPO")
    return matches.pop()


def _require_origin(env, name, forge):
    value = env.get(name)
    if not value or Forge(forge.kind, value) != forge:
        raise ForgeError(f"credential origin must match the destination: set {name} explicitly")


def run(argv, *, environment=None):
    env = os.environ if environment is None else environment
    parser = argparse.ArgumentParser(
        prog="tauceti-gh",
        description=__doc__,
        allow_abbrev=False,
        add_help=False,
        epilog="Place --conf PATH before the native command, or set TAUCETI_CONF.",
    )
    parser.add_argument("-R", "--repo")
    parser.add_argument("--json")
    parser.add_argument("--jq", "-q")
    if argv in (["--help"], ["-h"]):
        parser.print_help()
        return 0
    native = list(argv)
    source = env.get("TAUCETI_CONF")
    if native and native[0] == "--conf":
        if len(native) < 2:
            parser.error("--conf requires a value")
        source, native = native[1], native[2:]
    elif native and native[0].startswith("--conf="):
        source, native = native[0].partition("=")[2], native[1:]
    if not source:
        return _delegate(native, env)
    snapshot = env.get("TAUCETI_CONF_SNAPSHOT") if env.get("TAUCETI_CONF_SOURCE") == source else None
    project = load(snapshot or source)
    refs = [project.repository_ref(role) for role in project.raw["repositories"]]
    if all(ref.forge == GITHUB for ref in refs):
        return _delegate(native, env)
    # Read commands share a complete parser before routing or credentials.
    # Other commands still fail closed; their flags have different meanings.
    args, command = parser.parse_known_args(native)
    options = None
    if command[:2] in (["pr", "view"], ["issue", "view"]) and len(command) == 3:
        number = int(command[2])
        if number < 1:
            raise ValueError("issue/PR number must be positive")
    elif command[:2] in (["pr", "list"], ["issue", "list"]):
        listing = argparse.ArgumentParser(prog="gh " + " ".join(command[:2]), allow_abbrev=False)
        listing.add_argument(
            "--state",
            "-s",
            choices=["open", "closed", "all"] + (["merged"] if command[0] == "pr" else []),
            default="open",
        )
        listing.add_argument("--limit", "-L", type=int, default=30)
        listing.add_argument("--author", "-A")
        listing.add_argument("--label", "-l", action="append", default=[])
        if command[0] == "pr":
            listing.add_argument("--base", "-B")
            listing.add_argument("--head", "-H")
        options = listing.parse_args(command[2:])
        if options.limit < 1:
            raise ValueError("limit must be positive")
    elif command[:2] == ["repo", "view"] and len(command) in {2, 3}:
        if len(command) == 3:
            if args.repo:
                raise ValueError("specify the repository once")
            args.repo = command[2]
    else:
        raise Unsupported(
            "mixed-project commands currently support pr/issue view/list and repo view; no delegation was attempted"
        )
    selected = args.repo or env.get("GH_REPO")
    if not selected:
        repository = _checkout_repository(refs)
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
        if repository.forge != GITHUB and any(
            env.get(key) for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
        ):
            _require_origin(env, "TAUCETI_GITHUB_ORIGIN", repository.forge)
        # Fully qualify the chosen target: the checkout may have a Forgejo origin.
        host = urllib.parse.urlsplit(repository.forge.url).netloc
        return _delegate(native, {**env, "GH_REPO": host + "/" + repository.name, "GH_HOST": host})
    if not command:
        parser.error("a command is required")
    token_file = env.get("TAUCETI_FORGEJO_TOKEN_FILE")
    token = None
    if token_file:
        _require_origin(env, "TAUCETI_FORGEJO_ORIGIN", repository.forge)
        from pathlib import Path

        with Path(token_file).open() as credential:
            if os.fstat(credential.fileno()).st_mode & 0o077:
                raise ForgeError("Forgejo token file must be private (chmod 600)")
            token = credential.read().strip()
        if not token:
            raise ForgeError("Forgejo token file is empty")
    api = Forgejo(repository.forge, token=token)
    fields = args.json.split(",") if args.json else None
    reader = ForgejoReader(api)
    if command[:2] == ["pr", "view"]:
        value = reader.pull(repository, number, fields)
    elif command[:2] == ["issue", "view"]:
        value = reader.issue(repository, number, fields)
    elif command[:2] == ["repo", "view"]:
        value = reader.repository(repository, fields)
    else:
        author = api.current_user()["login"] if options.author == "@me" else options.author
        filters = {
            "state": options.state,
            "limit": options.limit,
            "author": author,
            "labels": [name for group in options.label for name in group.split(",")],
        }
        if command[0] == "pr":
            filters.update(base=options.base, head=options.head)
            value = reader.pulls(repository, fields, **filters)
        else:
            value = reader.issues(repository, fields, **filters)
    return _emit(value, args.jq)


def _delegate(argv, env):
    executable = env.get("TAUCETI_REAL_GH") or shutil.which("gh", path=env.get("PATH"))
    if not executable or os.path.realpath(executable) == os.path.realpath(sys.argv[0]):
        raise Unsupported("real GitHub CLI not found; set TAUCETI_REAL_GH")
    return subprocess.run([executable, *argv], env=env).returncode


def main():
    try:
        return run(sys.argv[1:])
    except (ForgeError, ConfigError, ValueError, OSError) as error:
        print(f"gh: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
