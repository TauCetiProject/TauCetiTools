#!/usr/bin/env python3
"""Fetch complete immutable CI histories once, then materialize private checkouts.

Identity resolution remains ci_git.py's separate fail-closed operation. This
source store is fresh, unfiltered, job-owned, and outside every candidate tree.
Only its objects directory is exposed read-only to candidate sandboxes.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import random
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if __package__:
    from .ci_git import (
        Git, cli_source_arguments, cli_source_repositories, github_url, repository_from_record,
        repository_record, sha, source_urls,
    )
else:
    from ci_git import (
        Git, cli_source_arguments, cli_source_repositories, github_url, repository_from_record,
        repository_record, sha, source_urls,
    )

STORE = ".ci-source.git"
ROLES = {"head": "pr", "base": "base", "mergebase": "mergebase"}
FETCH_TIMEOUT = 600
FETCH_ATTEMPTS = 3


def workspace_path(value):
    path = Path(value).resolve(strict=True)
    if not path.is_dir() or any(c in str(path) for c in "\n\r\0"):
        raise ValueError("expected an ordinary workspace directory")
    return path


class SourceGit(Git):
    """Keep local Git limits; source transfers have their own process lifetime."""

    def __init__(self, *args, fetch_timeout=FETCH_TIMEOUT, **kwargs):
        if fetch_timeout <= 0:
            raise ValueError("fetch timeout must be positive")
        super().__init__(*args, **kwargs)
        self.fetch_timeout = fetch_timeout
        token = kwargs.get("token")
        self.redactions = (token, base64.b64encode(f"x-access-token:{token}".encode()).decode()) if token else ()

    def transfer(self, url, references):
        command = self.command + [
            "fetch",
            # The store lives for one job. Detached automatic repacking can
            # remove packs while the following fsck or offline readers use them.
            "--no-auto-maintenance",
            "--no-tags",
            "--no-recurse-submodules",
            url,
            *(f"+{source}:{target}" for source, target in references),
        ]
        # A timeout must stop git-remote-https/index-pack as well as their parent
        # before retrying against this same store. Credentials remain in env only.
        process = subprocess.Popen(
            command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        try:
            output, error = process.communicate(timeout=self.fetch_timeout)
        except subprocess.TimeoutExpired:
            output, error = self.stop_transfer(process)
            raise subprocess.TimeoutExpired(command, self.fetch_timeout, output=output, stderr=error) from None
        except BaseException:
            self.stop_transfer(process)
            raise
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, output, error)

    @staticmethod
    def stop_transfer(process):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            return process.communicate()

    def fetch_sources(self, url, references, *, attempts=FETCH_ATTEMPTS):
        if attempts < 1:
            raise ValueError("fetch attempts must be positive")
        # Match actions/checkout v4's three tries and 10–20 second jitter. Retain
        # verified packs between attempts; never change the requested identities.
        for attempt in range(1, attempts + 1):
            print(f"CI source fetch: attempt {attempt}/{attempts}", file=sys.stderr, flush=True)
            try:
                self.transfer(url, references)
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                diagnostic = (error.stderr or b"").decode(errors="replace")
                for secret in self.redactions:
                    diagnostic = diagnostic.replace(secret, "[redacted]")
                # Escape newlines/control characters, including workflow command
                # syntax, rather than replaying remote text as runner commands.
                print("CI source fetch failed: " + json.dumps(diagnostic[-4096:]), file=sys.stderr, flush=True)
                if attempt == attempts:
                    raise
                time.sleep(random.randint(10, 20))


def fetch_sources(
    repository,
    head_repository,
    head,
    base,
    mergebase,
    workspace,
    *,
    token=None,
    credential_origin=None,
    fetch_timeout=FETCH_TIMEOUT,
    attempts=FETCH_ATTEMPTS,
):
    upstream, candidate, forge = source_urls(
        repository, head_repository, token=token, credential_origin=credential_origin, legacy_url=github_url
    )
    head, base, mergebase = map(sha, (head, base, mergebase))
    workspace = workspace_path(workspace)
    store = workspace / STORE
    # Reserve only a new path. An existing or dangling-symlink store belongs to
    # someone else and must never be reinitialized or removed on failure.
    store.mkdir()
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="ci-source-home-") as home:
            git = SourceGit(store, home, token=token, token_origin=forge,
                            follow_redirects=forge is None, fetch_timeout=fetch_timeout)
            if upstream.lower() == candidate.lower():
                git.fetch_sources(upstream, [(base, "refs/ci/base"), (head, "refs/ci/head")], attempts=attempts)
            else:
                git.fetch_sources(upstream, [(base, "refs/ci/base")], attempts=attempts)
                git.fetch_sources(candidate, [(head, "refs/ci/head")], attempts=attempts)
            for value in (head, base, mergebase):
                git.commit(value)
            actual = git.run("merge-base", "--all", base, head).stdout.decode().splitlines()
            if actual != [mergebase]:
                raise ValueError("source history differs from the resolved merge base")
            if (store / "shallow").exists() or list((store / "objects/pack").glob("*.promisor")):
                raise ValueError("source store must contain complete unfiltered history")
            # Complete-history validation has a separate generous deadline too;
            # it must not inherit the identity resolver's short local limit.
            subprocess.run(
                git.command + ["fsck", "--connectivity-only", "--no-dangling", "--no-reflogs", head, base],
                env=git.env,
                check=True,
                capture_output=True,
                timeout=FETCH_TIMEOUT,
            )
            identity = dict(
                schema=2 if forge is not None else 1,
                repository=repository_record(repository) if forge is not None else repository,
                head_repository=repository_record(head_repository) if forge is not None else head_repository,
                head=head,
                base=base,
                mergebase=mergebase,
            )
            (store / "identity.json").write_text(json.dumps(identity) + "\n")
        return dict(
            store=str(store),
            seconds=round(time.monotonic() - started, 3),
            pack_bytes=sum(p.stat().st_size for p in (store / "objects/pack").glob("*.pack")),
        )
    except BaseException:
        # Preserve the original failure if cleanup itself fails. This directory
        # was reserved by us and remains owned by normal job cleanup.
        with contextlib.suppress(OSError):
            shutil.rmtree(store)
        raise


def materialize(workspace, role, commit):
    workspace, commit = workspace_path(workspace), sha(commit)
    if role not in ROLES:
        raise ValueError("unknown checkout role")
    store = workspace / STORE
    identity_file = store / "identity.json"
    if store.is_symlink() or not stat.S_ISREG(identity_file.lstat().st_mode) or identity_file.stat().st_size > 4096:
        raise ValueError("missing ordinary source identity")
    identity = json.loads(identity_file.read_text())
    if (
        not isinstance(identity, dict)
        or type(identity.get("schema")) is not int
        or identity["schema"] not in (1, 2)
        or identity.get(role) != commit
    ):
        raise ValueError("checkout differs from the fetched immutable identity")
    if identity["schema"] == 2:
        repository = repository_from_record(identity["repository"])
        head_repository = repository_from_record(identity["head_repository"])
        upstream, candidate, _forge = source_urls(repository, head_repository)
    else:
        upstream = github_url(identity["repository"])
        candidate = github_url(identity["head_repository"])
    objects = store / "objects"
    if objects.is_symlink() or not objects.is_dir():
        raise ValueError("missing ordinary source objects")
    destination = workspace / ROLES[role]
    if destination.exists() or destination.is_symlink():
        raise ValueError("checkout destination must be new")
    started = time.monotonic()
    with (
        tempfile.TemporaryDirectory(prefix=f".ci-checkout-{role}-", dir=workspace) as temporary,
        tempfile.TemporaryDirectory(prefix="ci-checkout-home-") as home,
    ):
        stage = Path(temporary)
        # A clean metadata repository never loads the candidate's hooks/config.
        # Its alternate points to a stable path, surviving pr -> pr-raw-head.
        git = Git(stage / ".git", home)
        (stage / ".git/objects/info/alternates").write_text(str(objects) + "\n")
        git.commit(commit)
        git.run(
            "-c",
            "core.bare=false",
            "-c",
            "submodule.recurse=false",
            f"--work-tree={stage}",
            "checkout",
            "--quiet",
            "--force",
            "--detach",
            commit,
        )
        git.run("config", "core.bare", "false")
        git.run("remote", "add", "origin", candidate if role == "head" else upstream)
        if git.run("rev-parse", "HEAD").stdout.decode().strip() != commit:
            raise ValueError("materialized head differs from its immutable identity")
        stage.rename(destination)
    return dict(role=role, commit=commit, seconds=round(time.monotonic() - started, 3))


def main():
    # Detached transfer groups must receive cancellation before GitHub escalates
    # from SIGINT/SIGTERM to killing the action's original process group.
    def cancel(signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, cancel)
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    fetch = actions.add_parser("fetch")
    fetch.add_argument("--repository", required=True)
    fetch.add_argument("--head-repository", required=True)
    fetch.add_argument("--head", required=True)
    fetch.add_argument("--base", required=True)
    fetch.add_argument("--mergebase", required=True)
    fetch.add_argument("--workspace", default=".")
    fetch.add_argument("--fetch-timeout", type=float, default=FETCH_TIMEOUT)
    fetch.add_argument("--github-env", type=Path)
    cli_source_arguments(fetch)
    checkout = actions.add_parser("materialize")
    checkout.add_argument("--workspace", default=".")
    checkout.add_argument("--role", choices=ROLES, required=True)
    checkout.add_argument("--commit", required=True)
    args = vars(parser.parse_args())
    action = args.pop("action")
    try:
        if action == "fetch":
            environment = args.pop("github_env")
            source_kind = args.pop("source_kind")
            repository, head_repository, credential = cli_source_repositories(
                args["repository"], args["head_repository"], source_kind=source_kind,
                source_origin=args.pop("source_origin"), credential_origin=args.pop("credential_origin"),
            )
            args.update(repository=repository, head_repository=head_repository)
            token = os.environ.get("CI_SOURCE_GIT_TOKEN" if source_kind is not None else "CI_GIT_TOKEN")
            result = fetch_sources(**args, token=token, credential_origin=credential)
            if environment is not None:
                # Let the workflow exec Python directly, so runner cancellation
                # reaches this process rather than a command-substitution shell.
                with environment.open("a") as stream:
                    stream.write("CI_SOURCE_OBJECTS=" + result["store"] + "/objects\n")
        else:
            result = materialize(**args)
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        # Do not print the credential-bearing environment. The workflow keeps
        # resolved H/B for its existing always-run checkout-failure reporting.
        print(f"::error::CI source checkout failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
