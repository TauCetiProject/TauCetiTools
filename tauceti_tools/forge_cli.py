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
from .project import ConfigError, configuration_source, load


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


def _arguments(argv):
    """Extract wrapper options without interpreting native option values as flags.

    Native arguments are retained verbatim for GitHub delegation. The value
    options cover standard gh payload/filter inputs, including literal text
    beginning with --conf or --repo. Unsupported Forgejo commands still fail.
    """
    options = {"--conf": "conf", "--repo": "repo", "-R": "repo", "--json": "json", "--jq": "jq", "-q": "jq"}
    native_values = {
        "--body",
        "--body-file",
        "--title",
        "--subject",
        "--input",
        "--template",
        "--search",
        "--header",
        "-H",
        "--field",
        "-f",
        "--raw-field",
        "-F",
        "--head",
        "--base",
        "--hostname",
        "--author",
        "--assignee",
        "--label",
        "--milestone",
    }
    args = argparse.Namespace(conf=None, repo=None, json=None, jq=None)
    command, native = [], []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            command.extend(argv[index:])
            native.extend(argv[index:])
            break
        option, equals, value = token.partition("=")
        if option in native_values:
            size = 1 if equals else 2
            if index + size > len(argv):
                raise ValueError(f"{option} requires a value")
            command.extend(argv[index : index + size])
            native.extend(argv[index : index + size])
            index += size
            continue
        if token.startswith("-R") and token != "-R":
            option, equals, value = "-R", "=", token[2:].removeprefix("=")
        if option in options:
            size = 1 if equals else 2
            if index + size > len(argv):
                raise ValueError(f"{option} requires a value")
            setattr(args, options[option], value if equals else argv[index + 1])
            if option != "--conf":
                native.extend(argv[index : index + size])
            index += size
            continue
        command.append(token)
        native.append(token)
        index += 1
    return args, command, native


def run(argv, *, environment=None):
    env = os.environ if environment is None else environment
    parser = argparse.ArgumentParser(prog="tauceti-gh", description=__doc__, allow_abbrev=False)
    parser.add_argument("--conf")
    parser.add_argument("-R", "--repo")
    parser.add_argument("--json")
    parser.add_argument("--jq", "-q")
    if argv in (["--help"], ["-h"]):
        parser.print_help()
        return 0
    args, command, native = _arguments(argv)
    source = configuration_source(args.conf or env.get("TAUCETI_CONF"))
    if not source:
        return _delegate(native, env)
    project = load(source)
    selected = args.repo or env.get("GH_REPO")
    refs = [project.repository_ref(role) for role in project.raw["repositories"]]
    if not selected:
        if all(ref.forge == GITHUB for ref in refs):
            return _delegate(native, env)
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
