#!/usr/bin/env python3
"""Resolve CI identities over Git and read immutable metadata without GitHub REST.

The repository/ref authority remains with the trusted workflow and bors validator.
Network reads use a fresh bare repository; local reads borrow only object databases,
never candidate configuration, refs, attributes, hooks, or executable helpers.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


PINS = ("lake-manifest.json", "lean-toolchain")


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value) or value == "0" * 40:
        raise ValueError("expected an immutable commit SHA")
    return value


def github_url(repository):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("expected a GitHub owner/repository")
    if any(part in (".", "..") for part in repository.split("/")):
        raise ValueError("invalid GitHub repository")
    return f"https://github.com/{repository}.git"


def source_urls(repository, head_repository, *, token=None, credential_origin=None, legacy_url=None):
    """Resolve explicit source identities before any Git or credential use.

    Legacy strings retain their fixed GitHub meaning. Qualified identities never
    inherit GitHub credentials, and a supplied credential must independently name
    its trusted origin; request-controlled repository metadata cannot move it.
    """
    if isinstance(repository, str) and isinstance(head_repository, str):
        if credential_origin is not None:
            from tauceti_tools.forge import GITHUB
            if credential_origin != GITHUB:
                raise ValueError("legacy source credentials must belong to GitHub")
        url = legacy_url or github_url
        return url(repository), url(head_repository), None
    from tauceti_tools.forge import Forge, RepositoryRef
    if not isinstance(repository, RepositoryRef) or not isinstance(head_repository, RepositoryRef):
        raise ValueError("source and head repositories require the same explicit identity format")
    if not isinstance(repository.forge, Forge) or not isinstance(head_repository.forge, Forge):
        raise ValueError("source repositories require validated forge origins")
    if repository.forge != head_repository.forge:
        raise ValueError("candidate and base repositories must belong to the same source forge")
    if credential_origin is not None and (
        not isinstance(credential_origin, Forge) or credential_origin != repository.forge
    ):
        raise ValueError("source credential belongs to a different forge origin")
    if token and credential_origin is None:
        raise ValueError("qualified source credentials require an independently configured origin")
    return repository.url + ".git", head_repository.url + ".git", repository.forge


def repository_record(repository):
    return {"forge": {"kind": repository.forge.kind, "url": repository.forge.url}, "name": repository.name}


def repository_from_record(value):
    from tauceti_tools.forge import Forge, RepositoryRef
    if not isinstance(value, dict) or set(value) != {"forge", "name"}:
        raise ValueError("invalid qualified source repository record")
    forge = value["forge"]
    if (not isinstance(forge, dict) or set(forge) != {"kind", "url"}
            or not isinstance(forge["kind"], str) or not isinstance(forge["url"], str)):
        raise ValueError("invalid source forge record")
    return RepositoryRef(Forge(forge["kind"], forge["url"]), value["name"])


def cli_source_arguments(parser):
    parser.add_argument("--source-kind", choices=("github", "forgejo"))
    parser.add_argument("--source-origin")
    parser.add_argument("--credential-origin")


def cli_source_repositories(repository, head_repository, *, source_kind, source_origin, credential_origin):
    if source_kind is None and source_origin is None and credential_origin is None:
        return repository, head_repository, None
    if source_kind is None or source_origin is None:
        raise ValueError("qualified sources require both source kind and origin")
    from tauceti_tools.forge import Forge, RepositoryRef
    forge = Forge(source_kind, source_origin)
    credential = Forge(source_kind, credential_origin) if credential_origin is not None else None
    return RepositoryRef(forge, repository), RepositoryRef(forge, head_repository), credential


class Git:
    def __init__(self, repository, home, *, objects=None, token=None, token_origin=None, follow_redirects=True):
        self.executable = shutil.which("git")
        if not self.executable:
            raise OSError("Git is unavailable")
        self.repository = Path(repository)
        self.env = {
            "PATH": os.defpath, "HOME": str(home), "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
        }
        # Credentials stay in this clean child's environment, scoped to one
        # canonical origin. Neither arguments nor persistent config carry them.
        if token:
            origin = "https://github.com"
            if token_origin is not None:
                from tauceti_tools.forge import Forge
                if not isinstance(token_origin, Forge):
                    raise ValueError("Git credential origin requires a qualified forge")
                origin = token_origin.url
            encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            self.env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"http.{origin}/.extraheader",
                            GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {encoded}")
        subprocess.run([self.executable, "init", "--quiet", "--bare", "--template=", str(self.repository)],
                       env=self.env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if objects is not None:
            self.env["GIT_OBJECT_DIRECTORY"] = str(objects)
        self.command = [self.executable, "--no-replace-objects", f"--git-dir={self.repository}",
                        "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null",
                        "-c", "credential.helper=", "-c", "fetch.fsckObjects=true"]
        if not follow_redirects:
            self.command += ["-c", "http.followRedirects=false"]

    def run(self, *args, check=True):
        return subprocess.run(self.command + list(args), env=self.env, check=check,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    def branch(self, value):
        # Branches must be full ordinary branch names, never revisions, refspecs,
        # options, HEAD, tags, or object paths. check-ref-format handles edge cases.
        if not value or value.startswith("-") or value == "HEAD":
            raise ValueError("expected a branch name")
        ref = "refs/heads/" + value
        if self.run("check-ref-format", ref, check=False).returncode:
            raise ValueError("invalid branch name")
        return ref

    def fetch(self, url, references):
        self.run("fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--filter=tree:0",
                 url, *(f"+{source}:{destination}" for source, destination in references))

    def commit(self, value):
        sha(value)
        if self.run("cat-file", "-t", value).stdout != b"commit\n":
            raise ValueError("identity endpoint is not a commit")
        return value


def resolve(repository, head_repository, head, base_ref, store, *, staging_ref=None, token=None, credential_origin=None):
    """Capture B once and compute the exact merge base, failing closed on missing history.

    A staging ref must already have been selected by the bors authority. It must
    point to H, and B must be a strict ancestor of H (GitHub compare status=ahead).
    Without staging_ref, base_ref names an ordinary branch, or an immutable base
    SHA supplied by a trusted merge-group event. No candidate content is executed.
    """
    upstream, candidate, forge = source_urls(
        repository, head_repository, token=token, credential_origin=credential_origin, legacy_url=github_url
    )
    sha(head)
    store = Path(store)
    if store.exists():
        raise ValueError("identity object store must be new")
    with tempfile.TemporaryDirectory(prefix="ci-git-home-") as home:
        git = Git(store, home, token=token, token_origin=forge, follow_redirects=forge is None)
        base_source = base_ref if re.fullmatch(r"[0-9a-f]{40}", base_ref) else git.branch(base_ref)
        if len(base_source) == 40:
            sha(base_source)
        head_source = git.branch(staging_ref) if staging_ref is not None else head
        if staging_ref is not None and upstream.lower() != candidate.lower():
            raise ValueError("staging candidate must be in the upstream repository")
        base_destination, head_destination = "refs/ci/base", "refs/ci/head"
        if upstream.lower() == candidate.lower():
            git.fetch(upstream, [(base_source, base_destination), (head_source, head_destination)])
        else:
            git.fetch(upstream, [(base_source, base_destination)])
            git.fetch(candidate, [(head_source, head_destination)])
        base = git.commit(git.run("rev-parse", base_destination).stdout.decode().strip())
        actual_head = git.commit(git.run("rev-parse", head_destination).stdout.decode().strip())
        if actual_head != head:
            raise ValueError("candidate ref differs from this dispatch")
        if staging_ref is not None:
            ancestry = git.run("merge-base", "--is-ancestor", base, head, check=False)
            if base == head or ancestry.returncode != 0:
                raise ValueError("bors base is not a strict ancestor of its staging commit")
        mergebase = git.run("merge-base", "--all", base, head).stdout.decode().splitlines()
        if len(mergebase) != 1:
            raise ValueError("expected one exact merge base")
        return {"base_sha": base, "mergebase": git.commit(mergebase[0]),
                "staging_sha": actual_head if staging_ref is not None else ""}


@contextmanager
def object_reader(checkout):
    objects = (Path(checkout) / ".git/objects").resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="ci-git-read-") as directory:
        yield Git(Path(directory) / "repo.git", directory, objects=objects)


def tree_entry(checkout, commit, path):
    """Return mode/type/OID for a pin, including symlinks so the caller rejects them."""
    sha(commit)
    if path not in PINS:
        raise ValueError("expected a Lake pin path")
    with object_reader(checkout) as git:
        git.commit(commit)
        result = git.run("ls-tree", "-z", commit, "--", path).stdout
        if not result:
            return ""
        fields = result.split(b"\0")
        if len(fields) != 2 or fields[-1] or b"\t" not in fields[0]:
            raise ValueError("malformed tree entry")
        metadata, name = fields[0].split(b"\t", 1)
        if name != path.encode():
            raise ValueError("unexpected tree path")
        mode, kind, oid = metadata.decode().split(" ")
        sha(oid)
        return f"{mode} {kind} {oid}"


def lint_metadata(checkout, base, head):
    """Read the exact merge base and batch commit titles without a REST compare.

    Read the complete immutable batch, including every source citation. Batch
    size alone must not select full-library lint. PR scope only needs the merge base.
    """
    with object_reader(checkout) as git:
        git.commit(base)
        git.commit(head)
        bases = git.run("merge-base", "--all", base, head).stdout.decode().splitlines()
        if len(bases) != 1:
            raise ValueError("expected one exact merge base")
        history = f"{base}..{head}"
        count = int(git.run("rev-list", "--count", history).stdout)
        messages = git.run("log", "--no-show-signature", "--format=%B", "-z",
                           history).stdout.decode(errors="replace").split("\0")
        if messages[-1] or len(messages) - 1 != count:
            raise ValueError("malformed local commit metadata")
        # %s folds a multiline first paragraph; policy uses the exact first line.
        # Keep explicit source citations separately so a qualified native PR
        # cannot disappear when this compact metadata drops the body.
        commits = []
        for message in messages[:-1]:
            row = {"commit": {"message": message.split("\n", 1)[0]}}
            citations = re.findall(r"^Source-PR:[ \t]*(.*)$", message, re.M)
            if citations:
                row["source_prs"] = citations
            commits.append(row)
        return {"merge_base_commit": {"sha": git.commit(bases[0])},
                "total_commits": count,
                "commits": commits}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    identity = commands.add_parser("resolve")
    identity.add_argument("--repository", required=True)
    identity.add_argument("--head-repository", required=True)
    identity.add_argument("--head", required=True)
    identity.add_argument("--base-ref", required=True)
    identity.add_argument("--store", type=Path, required=True)
    identity.add_argument("--staging-ref")
    cli_source_arguments(identity)
    entry = commands.add_parser("tree-entry")
    entry.add_argument("--checkout", required=True)
    entry.add_argument("--commit", required=True)
    entry.add_argument("--path", required=True, choices=PINS)
    lint = commands.add_parser("lint-metadata")
    lint.add_argument("--checkout", required=True)
    lint.add_argument("--base", required=True)
    lint.add_argument("--head", required=True)
    args = parser.parse_args()
    try:
        if args.command == "resolve":
            repository, head_repository, credential = cli_source_repositories(
                args.repository, args.head_repository, source_kind=args.source_kind,
                source_origin=args.source_origin, credential_origin=args.credential_origin,
            )
            token = (os.environ.get("CI_SOURCE_GIT_TOKEN") if args.source_kind is not None
                     else os.environ.get("CI_GIT_TOKEN") or os.environ.get("GH_TOKEN"))
            print(json.dumps(resolve(repository, head_repository, args.head, args.base_ref, args.store,
                                     staging_ref=args.staging_ref, token=token, credential_origin=credential)))
        elif args.command == "tree-entry":
            print(tree_entry(args.checkout, args.commit, args.path))
        else:
            print(json.dumps(lint_metadata(args.checkout, args.base, args.head)))
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        # Do not print network stderr or the environment: either could contain
        # credentials. Local identity diagnostics above are safe and actionable.
        diagnostic = ("Git operation exceeded its 120-second deadline" if isinstance(error, subprocess.TimeoutExpired)
                      else f"Git operation exited {error.returncode}" if isinstance(error, subprocess.CalledProcessError) else str(error))
        parser.exit(1, f"ci-git: {diagnostic}\n")


if __name__ == "__main__":
    main()
