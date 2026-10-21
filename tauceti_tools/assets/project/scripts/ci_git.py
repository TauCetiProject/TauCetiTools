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


class Git:
    def __init__(self, repository, home, *, objects=None, token=None):
        self.executable = shutil.which("git")
        if not self.executable:
            raise OSError("Git is unavailable")
        self.repository = Path(repository)
        self.env = {
            "PATH": os.defpath, "HOME": str(home), "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
        }
        # This credential is used only for fixed GitHub HTTPS URLs. Never put it in
        # process arguments, persistent config, output, or the repository URL.
        if token:
            encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            self.env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                            GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {encoded}")
        subprocess.run([self.executable, "init", "--quiet", "--bare", "--template=", str(self.repository)],
                       env=self.env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if objects is not None:
            self.env["GIT_OBJECT_DIRECTORY"] = str(objects)
        self.command = [self.executable, "--no-replace-objects", f"--git-dir={self.repository}",
                        "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null",
                        "-c", "credential.helper=", "-c", "fetch.fsckObjects=true"]

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


def resolve(repository, head_repository, head, base_ref, store, *, staging_ref=None, token=None):
    """Capture B once and compute the exact merge base, failing closed on missing history.

    A staging ref must already have been selected by the bors authority. It must
    point to H, and B must be a strict ancestor of H (GitHub compare status=ahead).
    Without staging_ref, base_ref names an ordinary branch, or an immutable base
    SHA supplied by a trusted merge-group event. No candidate content is executed.
    """
    upstream, candidate = github_url(repository), github_url(head_repository)
    sha(head)
    store = Path(store)
    if store.exists():
        raise ValueError("identity object store must be new")
    with tempfile.TemporaryDirectory(prefix="ci-git-home-") as home:
        git = Git(store, home, token=token)
        base_source = base_ref if re.fullmatch(r"[0-9a-f]{40}", base_ref) else git.branch(base_ref)
        if len(base_source) == 40:
            sha(base_source)
        head_source = git.branch(staging_ref) if staging_ref is not None else head
        if staging_ref is not None and repository.lower() != head_repository.lower():
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
    entry = commands.add_parser("tree-entry")
    entry.add_argument("--checkout", required=True)
    entry.add_argument("--commit", required=True)
    entry.add_argument("--path", required=True, choices=PINS)
    args = parser.parse_args()
    try:
        if args.command == "resolve":
            print(json.dumps(resolve(args.repository, args.head_repository, args.head, args.base_ref, args.store,
                                     staging_ref=args.staging_ref,
                                     token=os.environ.get("CI_GIT_TOKEN") or os.environ.get("GH_TOKEN"))))
        else:
            print(tree_entry(args.checkout, args.commit, args.path))
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        # Do not print network stderr or the environment: either could contain
        # credentials. Local identity diagnostics above are safe and actionable.
        diagnostic = ("Git operation exceeded its 120-second deadline" if isinstance(error, subprocess.TimeoutExpired)
                      else f"Git operation exited {error.returncode}" if isinstance(error, subprocess.CalledProcessError) else str(error))
        parser.exit(1, f"ci-git: {diagnostic}\n")


if __name__ == "__main__":
    main()
