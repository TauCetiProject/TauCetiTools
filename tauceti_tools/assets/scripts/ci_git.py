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
import sys
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


@contextmanager
def repository_reader(repository):
    """Read the object database of a local repository, worktree or bare clone.

    Only the object directory is borrowed, as in `object_reader`; the repository's
    configuration, attributes and hooks never reach the diff.
    """
    objects = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--path-format=absolute", "--git-path", "objects"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
    ).stdout.decode().strip()
    with tempfile.TemporaryDirectory(prefix="ci-git-read-") as directory:
        yield Git(Path(directory) / "repo.git", directory, objects=Path(objects).resolve(strict=True))


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


# Per-PR review caps. PR CI and the staging candidate's retained-member policy
# both apply them through `review_size`, so the two checks cannot disagree.
REVIEW_ADDED_LINE_CAP = 3000
REVIEW_REMOVED_LINE_CAP = 2000
REVIEW_FILE_CAP = 2000
NEW_FILE_LINE_CAP = 1250


def _diff_records(raw, stat_field):
    """Split `git diff -z` output into (stat, paths) records.

    With `-z`, an ordinary record is `stat<path>\0` and a rename is
    `stat\0<old>\0<new>\0`, where stat ends in a tab for --numstat and in NUL
    for --name-status. `stat_field` parses the leading field into (stat, path),
    returning an empty path when the two-path rename form follows.
    """
    fields = raw.split(b"\0")
    if fields.pop() != b"":
        raise ValueError("malformed Git diff output")
    records = []
    index = 0
    while index < len(fields):
        stat, name, index = stat_field(fields, index)
        if name:
            records.append((stat, (name,)))
            continue
        if index + 2 > len(fields) or not fields[index] or not fields[index + 1]:
            raise ValueError("truncated Git rename record")
        records.append((stat, (fields[index], fields[index + 1])))
        index += 2
    return records


def _numstat_field(fields, index):
    parts = fields[index].split(b"\t", 2)
    if len(parts) != 3:
        raise ValueError("malformed Git line statistics")
    return (parts[0], parts[1]), parts[2], index + 1


def _status_field(fields, index):
    if index + 1 >= len(fields):
        raise ValueError("truncated Git status record")
    status = fields[index]
    # Copies and renames carry two paths; every other status carries one.
    if status[:1] in (b"R", b"C"):
        return status, b"", index + 1
    return status, fields[index + 1], index + 2


def review_size(git, base, head, *, large_refactor=False):
    """Measure one PR's review size between exact commits from local objects.

    Use the merge base as `base`, as GitHub does for a PR. Renames are detected
    at Git's default similarity, so a moved file is one changed file whose lines
    are only its edits, and its destination is not a new file. Binary files
    count as changed files with zero lines, as on GitHub. Unreadable statistics
    raise ValueError so that every caller fails closed. `large_refactor` is the caller's
    verdict from `large_refactor_approved`: it lifts the file and line caps, never the
    new-file cap.
    """
    git.commit(base)
    git.commit(head)
    diff = ("diff", "--no-ext-diff", "--no-textconv", "--find-renames", "-z")
    numstat = _diff_records(git.run(*diff, "--numstat", base, head, "--").stdout, _numstat_field)
    additions = deletions = 0
    added_lines = {}
    pins = {name.encode() for name in PINS}
    pins_changed = False
    for (adds, dels), names in numstat:
        if adds == dels == b"-":
            adds = dels = b"0"
        if not (adds.isdigit() and dels.isdigit()):
            raise ValueError("unreadable Git line statistics")
        additions += int(adds)
        deletions += int(dels)
        added_lines[names[-1]] = int(adds)
        pins_changed = pins_changed or bool(pins.intersection(names))
    statuses = _diff_records(
        git.run(*diff, "--name-status", "--diff-filter=A", base, head, "--").stdout, _status_field
    )
    new_lean_files = []
    for status, names in statuses:
        if status != b"A" or len(names) != 1 or names[0] not in added_lines:
            raise ValueError("inconsistent Git added-file listing")
        if names[0].endswith(b".lean"):
            new_lean_files.append((os.fsdecode(names[0]), added_lines[names[0]]))
    lines_capped = additions > REVIEW_ADDED_LINE_CAP or deletions > REVIEW_REMOVED_LINE_CAP
    return {
        "additions": additions,
        "deletions": deletions,
        "files": len(numstat),
        "pins_changed": pins_changed,
        "large_refactor": bool(large_refactor),
        "new_lean_files": sorted(new_lean_files),
        # A Lake-pin bump's diff is forced upstream, so only the file cap applies.
        # An admin-approved large refactor skips all three caps.
        "too_large": not large_refactor
        and (len(numstat) > REVIEW_FILE_CAP or (lines_capped and not pins_changed)),
        "new_files_too_long": sorted(
            (name, lines) for name, lines in new_lean_files if lines > NEW_FILE_LINE_CAP
        ),
    }


# A repository admin's `large-refactor` label lifts the three review-size caps for one PR,
# for a mechanical change that cannot sensibly be split. Every caller of `pr_guards` asks
# `large_refactor_approved` with its own GitHub read credential.
LARGE_REFACTOR_LABEL = "large-refactor"


def _github_time(value):
    import datetime as dt

    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()


def large_refactor_approved(read, repo, number, labels, committed_at):
    """Whether PR `number` of the GitHub repository `repo` is an admin-approved large refactor.

    `read(path)` returns parsed JSON for a GitHub REST GET; `labels` are the PR's current label
    names; `committed_at` is the head commit's committer time (Unix seconds, from local objects).
    It holds when the label is present, its latest `labeled` event is by a user who is a
    repository admin now, that event is later than the head's committer time and no force push
    followed it. So removing the label ends the exemption, and a push after labelling ends it
    too unless the pushed commit claims an older committer time. Unreadable evidence raises.
    """
    if LARGE_REFACTOR_LABEL not in {str(name).lower() for name in labels}:
        return False
    events, page = [], 1
    while True:
        rows = read(f"repos/{repo}/issues/{int(number)}/events?per_page=100&page={page}")
        if not isinstance(rows, list):
            raise ValueError("unreadable PR events")
        events += rows
        if len(rows) < 100:
            break
        page += 1
    labelled = [e for e in events if e.get("event") == "labeled"
                and str((e.get("label") or {}).get("name", "")).lower() == LARGE_REFACTOR_LABEL]
    if not labelled:
        return False
    latest = max(labelled, key=lambda e: e["id"])
    if any(e.get("event") == "head_ref_force_pushed" and e["id"] > latest["id"] for e in events):
        return False
    if _github_time(latest["created_at"]) <= committed_at:
        return False
    actor = latest.get("actor") or {}
    login, actor_id = actor.get("login"), actor.get("id")
    if not isinstance(login, str) or not login or type(actor_id) is not int:
        return False
    from urllib.parse import quote

    permission = read(f"repos/{repo}/collaborators/{quote(login, safe='')}/permission")
    if not isinstance(permission, dict):
        raise ValueError("unreadable collaborator permission")
    return (permission.get("role_name") or permission.get("permission")) == "admin" and (
        permission.get("user") or {}).get("id") == actor_id


def large_refactor_head(read, repo, number, git, head):
    """`large_refactor_approved` for a PR whose current head must be exactly `head`."""
    pull = read(f"repos/{repo}/pulls/{int(number)}")
    if not isinstance(pull, dict) or not isinstance(pull.get("labels"), list):
        raise ValueError("unreadable pull request")
    if (pull.get("head") or {}).get("sha") != head:
        return False
    labels = [label.get("name", "") for label in pull["labels"] if isinstance(label, dict)]
    return large_refactor_approved(read, repo, number, labels, committer_time(git, head))


def committer_time(git, commit):
    """A commit's committer time in Unix seconds, from local objects."""
    return int(git.run("show", "-s", "--format=%ct", git.commit(commit)).stdout.decode().strip())


# Root files a PR may change without a human merge, beside the library directory and its
# root module. Lakefiles are deliberately absent. The queue's admission allow-list is
# TauCetiReview `default_allow_files`; the two must agree.
SCOPE_ROOT_FILES = ("lake-manifest.json", "lean-toolchain", "docbuild/docs/references.bib")


def out_of_scope(paths, lean_root="TauCeti"):
    """Changed paths outside the library and the allowed root files, which need a human merge."""
    directory = lean_root.replace(".", "/")
    allowed = {directory + ".lean", *SCOPE_ROOT_FILES}
    return sorted({p for p in paths if not (p.startswith(directory + "/") or p in allowed)})


def changed_paths(git, base, head):
    """Every path a diff touches, both sides of a rename included."""
    git.commit(base)
    git.commit(head)
    raw = git.run("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z",
                  base, head, "--").stdout
    fields = raw.split(b"\0")
    if fields.pop() != b"":
        raise ValueError("malformed Git name listing")
    return [os.fsdecode(name) for name in fields]


def guard_violations(result):
    """One readable sentence per failed PR-level diff guard in a `pr_guards` result."""
    messages = []
    if result["too_large"]:
        messages.append(
            f"too large to review: +{result['additions']} / -{result['deletions']} lines across "
            f"{result['files']} files (cap: {REVIEW_ADDED_LINE_CAP} added lines, {REVIEW_REMOVED_LINE_CAP} removed "
            f"lines, {REVIEW_FILE_CAP} files; Lake-pin bumps are exempt from the line caps). "
            "Split it into smaller PRs, one topic each."
        )
    for name, lines in result["new_files_too_long"]:
        messages.append(
            f"{name}: new Lean files must be at most {NEW_FILE_LINE_CAP} lines (found {lines}). "
            "Split it, or grow an existing file (allowed up to 1500)."
        )
    if result.get("out_of_scope"):
        messages.append(
            "changes paths outside the library and the Lake pins, which need a human merge: "
            + ", ".join(result["out_of_scope"])
        )
    return messages


def pr_guards(git, base, head, *, lean_root="TauCeti", scope=True, large_refactor=False):
    """The PR-level diff guards, from local Git objects only.

    PR CI, `lake lint`, queue admission and the staging member policy all call this,
    so no forge's PR totals are consulted and the four cannot disagree. `base` is the
    exact merge base. `scope=False` leaves path scope to a caller with its own
    human-merge route. Unreadable statistics raise ValueError, so callers fail closed.
    """
    result = review_size(git, base, head, large_refactor=large_refactor)
    result["out_of_scope"] = out_of_scope(changed_paths(git, base, head), lean_root) if scope else []
    result["violations"] = guard_violations(result)
    return result


def _cli_large_refactor(pr, git, head):
    """The CLI's `--large-refactor-pr OWNER/NAME#NUMBER` verdict, read with GH_READ_TOKEN."""
    if not pr:
        return False
    repo, _, number = pr.rpartition("#")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not number.isdigit():
        raise ValueError("--large-refactor-pr must be OWNER/NAME#NUMBER")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from github_api import GitHub

    api = GitHub(max_wait=60)
    try:
        return large_refactor_head(lambda path: api.request("GET", path), repo, int(number), git, head)
    except (RuntimeError, ValueError, KeyError, TypeError) as error:
        # Unknown evidence never lifts a cap; the ordinary caps still apply.
        print(f"ci-git: large-refactor label unverified: {type(error).__name__}", file=sys.stderr)
        return False


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
    size = commands.add_parser("review-size", help="measure a PR's rename-aware review size as JSON")
    size.add_argument("--checkout", required=True)
    size.add_argument("--base", required=True, help="exact merge base")
    size.add_argument("--head", required=True)
    size.add_argument("--large-refactor-pr", metavar="OWNER/NAME#NUMBER",
                      help="lift the size caps if this GitHub PR, at --head, has an admin's large-refactor "
                           "label (reads with GH_READ_TOKEN)")
    guards = commands.add_parser("pr-guards", help="run the PR-level diff guards as JSON")
    guards.add_argument("--checkout", required=True)
    guards.add_argument("--base", required=True, help="exact merge base")
    guards.add_argument("--head", required=True)
    guards.add_argument("--lean-root", default="TauCeti")
    guards.add_argument("--no-scope", action="store_true", help="leave path scope to the caller")
    guards.add_argument("--large-refactor-pr", metavar="OWNER/NAME#NUMBER",
                        help="lift the size caps if this GitHub PR, at --head, has an admin's large-refactor "
                             "label (reads with GH_READ_TOKEN)")
    scope = commands.add_parser("scope", help="print changed paths (one per line on stdin) needing a human merge")
    scope.add_argument("--lean-root", default="TauCeti")
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
        elif args.command == "pr-guards":
            with object_reader(args.checkout) as git:
                exempt = _cli_large_refactor(args.large_refactor_pr, git, args.head)
                print(json.dumps(pr_guards(git, args.base, args.head, lean_root=args.lean_root,
                                           scope=not args.no_scope, large_refactor=exempt), sort_keys=True))
        elif args.command == "scope":
            for path in out_of_scope([line for line in sys.stdin.read().split("\n") if line], args.lean_root):
                print(path)
        elif args.command == "review-size":
            with object_reader(args.checkout) as git:
                exempt = _cli_large_refactor(args.large_refactor_pr, git, args.head)
                print(json.dumps(review_size(git, args.base, args.head, large_refactor=exempt), sort_keys=True))
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
