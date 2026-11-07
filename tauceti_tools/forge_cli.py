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

from .forge import GITHUB, Forge, ForgeError, Forgejo, Incomplete, RepositoryRef, Unsupported
from .project import ConfigError, load


def _emit(value, fields, query):
    if fields:

        def project(row):
            missing = set(fields) - set(row)
            if missing:
                known = {
                    "number",
                    "title",
                    "body",
                    "labels",
                    "created_at",
                    "updated_at",
                    "url",
                    "state",
                    "author",
                    "headRefOid",
                    "headRefName",
                    "baseRefName",
                    "isDraft",
                    "mergedAt",
                    "createdAt",
                    "updatedAt",
                    "comments",
                }
                error = Incomplete if missing <= known else Unsupported
                raise error("unavailable JSON fields: " + ", ".join(sorted(missing)))
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
    if not isinstance(row, dict):
        raise Incomplete("Forgejo PR response was not an object")
    result = {key: row[key] for key in ("number", "title", "body", "labels", "created_at", "updated_at") if key in row}
    for target, source in {
        "url": "html_url",
        "mergedAt": "merged_at",
        "createdAt": "created_at",
        "updatedAt": "updated_at",
    }.items():
        if source in row:
            result[target] = row[source]
    if "state" in row and "merged" in row:
        result["state"] = "MERGED" if row["merged"] else str(row["state"]).upper()
    if isinstance(row.get("draft"), bool):
        result["isDraft"] = row["draft"]
    if isinstance(row.get("user"), dict) and "login" in row["user"]:
        result["author"] = {"login": row["user"]["login"]}
    for target, parent, field in [
        ("headRefOid", "head", "sha"),
        ("headRefName", "head", "ref"),
        ("baseRefName", "base", "ref"),
    ]:
        if isinstance(row.get(parent), dict) and field in row[parent]:
            result[target] = row[parent][field]
    return result


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
    # The mixed-host foundation supports only these read commands. Validate the
    # entire command before selecting a server or credential; native options on
    # other subcommands have incompatible meanings (e.g. -f can be boolean).
    args, command = parser.parse_known_args(native)
    options = None
    if command[:2] == ["pr", "view"] and len(command) == 3:
        int(command[2])
    elif command[:2] == ["pr", "list"]:
        listing = argparse.ArgumentParser(prog="gh pr list", allow_abbrev=False)
        listing.add_argument("--state", "-s", choices=["open", "closed", "merged", "all"], default="open")
        listing.add_argument("--limit", "-L", type=int, default=30)
        options = listing.parse_args(command[2:])
        if options.limit < 1:
            raise ValueError("limit must be positive")
    else:
        raise Unsupported("mixed-project commands currently support pr view and pr list; no delegation was attempted")
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
    if command[:2] == ["pr", "view"] and len(command) == 3:
        value = _pull(api.pull(repository, int(command[2])))
        if fields and "comments" in fields:
            comments = api.comments(repository, int(command[2]))
            if any(
                not isinstance(row, dict)
                or not {"id", "body", "user", "html_url"} <= row.keys()
                or not isinstance(row["user"], dict)
                or "login" not in row["user"]
                for row in comments
            ):
                raise Incomplete("Forgejo returned an incomplete comment")
            value["comments"] = [
                {
                    "id": row["id"],
                    "body": row.get("body"),
                    "author": {"login": (row.get("user") or {}).get("login")},
                    "url": row.get("html_url"),
                }
                for row in comments
            ]
    elif command[:2] == ["pr", "list"]:
        rows = api.pulls(repository, state="closed" if options.state == "merged" else options.state)
        by_number = {}
        for row in rows:
            if not isinstance(row, dict) or type(row.get("number")) is not int:
                raise Incomplete("Forgejo list response did not identify a PR")
            if options.state == "merged" and "merged" not in row:
                raise Incomplete("Forgejo list response did not identify merge state")
            if options.state != "merged" or row["merged"]:
                by_number[row["number"]] = row
        value = [_pull(by_number[number]) for number in sorted(by_number, reverse=True)[: options.limit]]
    else:
        raise Unsupported(
            "Forgejo command is not implemented yet; supported: pr view, pr list. No GitHub fallback was attempted."
        )
    return _emit(value, fields, args.jq)


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
