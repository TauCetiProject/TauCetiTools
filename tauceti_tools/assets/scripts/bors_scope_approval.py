"""Verify bors's human approval before waiving the restricted-path rule.

Run only from workflow-pinned tooling. The bors HTTPS observation, rather than
dispatch payloads or contributor comments, supplies approval and batch membership.
No candidate code is executed. Missing/incomplete observations fail closed.
"""

import argparse
import json
import pathlib
import re
import subprocess

from bors_observation import (FROZEN_ADMISSION, REPO, observation_with_admission,
                              requested_batch, sha, valid_observation)
ALLOWED_ROOT = {"TauCeti.lean", "lake-manifest.json", "lean-toolchain",
                "docbuild/docs/references.bib"}


def in_scope(path):
    return isinstance(path, str) and (path.startswith("TauCeti/") or path in ALLOWED_ROOT)


def github(path):
    endpoint = path if path.startswith("/") else f"repos/{REPO}/{path}"
    return json.loads(subprocess.check_output(["gh", "api", endpoint],
                                             text=True, timeout=45))


def current_pr(pr, head, api):
    data = api(f"pulls/{pr}")
    if (data.get("number") != pr or data.get("state") != "open" or data.get("draft") is not False
            or data.get("head", {}).get("sha") != head
            or data.get("base", {}).get("ref") != "main"
            or not sha(data.get("base", {}).get("sha"))):
        raise ValueError("PR revision or target changed")
    return data


def merge_base(data, head, api):
    base = api(f"compare/{data['base']['sha']}...{head}?per_page=1")["merge_base_commit"]["sha"]
    if not sha(base):
        raise ValueError("merge base unavailable")
    return base


def administrator(actor_id, api):
    """Check effective permissions, including admin access inherited from a team."""
    if type(actor_id) is not int or actor_id <= 0:
        return False
    user = api(f"/user/{actor_id}")
    if not isinstance(user, dict):
        raise ValueError("approval actor unavailable")
    login = user.get("login")
    if (user.get("id") != actor_id or not isinstance(login, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", login) is None):
        raise ValueError("approval actor unavailable")
    permission = api(f"collaborators/{login}/permission")
    if (not isinstance(permission, dict) or not isinstance(permission.get("user"), dict)
            or permission["user"].get("id") != actor_id):
        raise ValueError("approval actor changed during permission lookup")
    return permission.get("permission") == "admin"


def approved(approvals, pr, head, base, api):
    matches = [a for a in approvals if isinstance(a, dict) and a.get("pr") == pr]
    if len(matches) != 1:
        return False
    actor_id = matches[0].get("actor_id")
    return matches[0] == {
        "pr": pr, "actor_id": actor_id, "head_sha": head, "merge_base_sha": base
    } and administrator(actor_id, api)


def paths(data, api):
    # GitHub caps PR file listings at 3000. Never treat a truncated list as a
    # complete proof; include both sides of a rename and all deleted paths.
    count = data.get("changed_files")
    if type(count) is not int or not 0 < count <= 3000:
        raise ValueError("complete PR file count unavailable")
    files = []
    for page in range(1, (count + 99) // 100 + 1):
        result = api(f"pulls/{data['number']}/files?per_page=100&page={page}")
        if not isinstance(result, list):
            raise ValueError("invalid PR files")
        files.extend(result)
    names = [entry["filename"] for entry in files]
    if len(files) != count or len(set(names)) != count or not all(isinstance(n, str) and n for n in names):
        raise ValueError("incomplete PR files")
    for entry in files:
        if entry.get("status") == "renamed":
            previous = entry.get("previous_filename")
            if not isinstance(previous, str) or not previous:
                raise ValueError("rename source unavailable")
            names.append(previous)
    return names


class ImmutablePaths:
    """Complete restricted-path diff of immutable trees, without compare's file cap.

    Tree responses are nonrecursive and must be complete. Unchanged subtrees and
    TauCeti/ are pruned; additions, deletions, modes and file/tree changes are
    treated as git diff --no-renames would treat them. Caches span the batch.
    """
    def __init__(self, api):
        self.api = api
        self.commits = {}
        self.trees = {}

    def read(self, path):
        try:
            return self.api(path)
        except RuntimeError as error:
            # Shared API clients carry the HTTP status; quota/network failures
            # must propagate so status publication can retry without revocation.
            if getattr(error, "status", None) in (404, 422):
                raise ValueError("immutable scope object unavailable") from error
            raise

    def root(self, commit):
        if commit not in self.commits:
            data = self.read(f"git/commits/{commit}")
            if not isinstance(data, dict) or not isinstance(data.get("tree"), dict):
                raise ValueError("invalid immutable commit")
            tree = data["tree"].get("sha")
            if data.get("sha") != commit or not sha(tree):
                raise ValueError("invalid immutable commit")
            self.commits[commit] = tree
        return self.commits[commit]

    def tree(self, tree):
        if tree is None:
            return {}
        if tree not in self.trees:
            data = self.read(f"git/trees/{tree}")
            if (not isinstance(data, dict) or data.get("sha") != tree or data.get("truncated") is not False
                    or not isinstance(data.get("tree"), list)):
                raise ValueError("incomplete immutable tree")
            entries = {}
            for entry in data["tree"]:
                if not isinstance(entry, dict):
                    raise ValueError("invalid immutable tree entry")
                name, mode, kind = entry.get("path"), entry.get("mode"), entry.get("type")
                if (not isinstance(name, str) or not name or name in (".", "..")
                        or "/" in name or "\x00" in name or name in entries
                        or not sha(entry.get("sha"))
                        or (mode, kind) not in {("040000", "tree"), ("100644", "blob"),
                            ("100755", "blob"), ("120000", "blob"), ("160000", "commit")}):
                    raise ValueError("invalid immutable tree entry")
                entries[name] = (mode, kind, entry["sha"])
            self.trees[tree] = entries
        return self.trees[tree]

    def changed(self, base, head):
        pending = [("", self.root(base), self.root(head))]
        paths = set()
        visited = 0
        while pending:
            prefix, before, after = pending.pop()
            if before == after or prefix.startswith("TauCeti/"):
                continue
            left, right = self.tree(before), self.tree(after)
            for name in left.keys() | right.keys():
                visited += 1
                if visited > 100_000:
                    raise ValueError("immutable scope proof exceeds metadata bound")
                old, new = left.get(name), right.get(name)
                if old == new:
                    continue
                path = prefix + name
                for item in (old, new):
                    if item is not None and item[1] != "tree" and not in_scope(path):
                        paths.add(path)
                old_tree = old[2] if old and old[1] == "tree" else None
                new_tree = new[2] if new and new[1] == "tree" else None
                if old_tree != new_tree and path != "TauCeti":
                    pending.append((path + "/", old_tree, new_tree))
        return paths


def frozen_batch(data, batch, combined_paths, api, refresh):
    if not callable(refresh):
        return False
    immutable = ImmutablePaths(api)
    covered, seen, used = set(), set(), []
    for member in batch["members"]:
        if not isinstance(member, dict):
            return False
        number, head = member.get("pr"), member.get("head_sha")
        if type(number) is not int or number <= 0 or number in seen or not sha(head):
            return False
        seen.add(number)
        comparison = immutable.read(f"compare/{batch['base_sha']}...{head}?per_page=1")
        if not isinstance(comparison, dict) or not isinstance(comparison.get("merge_base_commit"), dict):
            raise ValueError("immutable merge base unavailable")
        base = comparison["merge_base_commit"].get("sha")
        if not sha(base):
            raise ValueError("immutable merge base unavailable")
        changed = immutable.changed(base, head)
        matches = [a for a in data["scope_approvals"]
                   if isinstance(a, dict) and a.get("pr") == number]
        if matches:
            if not approved(data["scope_approvals"], number, head, base, api):
                return False
            used.append(matches[0])
            covered.update(changed)
        elif changed:
            return False
    if not all(in_scope(path) or path in covered for path in combined_paths):
        return False
    # Membership, exact approval and current effective admin role are rechecked
    # after all immutable reads. Mutable PR heads are never admission authority.
    after, admission = refresh()
    if admission != FROZEN_ADMISSION or not valid_observation(after, "main"):
        return False
    if after.get("requested_batch") != batch or not isinstance(after.get("scope_approvals"), list):
        return False
    return all(approved(after["scope_approvals"], a["pr"], a["head_sha"],
                        a["merge_base_sha"], api) for a in used)


def verify(data, head, base, *, pr=None, batch_id=None, combined_paths=None, api=github,
           admission=None, refresh=None):
    if (not sha(head) or not sha(base) or not valid_observation(data,"main")
            or not isinstance(data.get("scope_approvals"), list)):
        return False
    approvals = data["scope_approvals"]
    if pr is not None:
        live = current_pr(pr, head, api)
        return merge_base(live, head, api) == base and approved(approvals, pr, head, base, api)

    try:
        batch = requested_batch(data, head, base, batch_id=batch_id)
    except ValueError:
        return False
    members = batch.get("members")
    # The queue seals batches of any size; every member is still verified below.
    if not isinstance(members, list) or not members:
        return False
    if not isinstance(combined_paths, list) or not combined_paths or not all(isinstance(p, str) and p for p in combined_paths):
        return False
    if admission == FROZEN_ADMISSION:
        return frozen_batch(data, batch, combined_paths, api, refresh)
    covered = set()
    seen = set()
    for member in members:
        number, member_head = member.get("pr"), member.get("head_sha")
        if type(number) is not int or number <= 0 or number in seen or not sha(member_head):
            return False
        seen.add(number)
        live = current_pr(number, member_head, api)
        member_base = merge_base(live, member_head, api)
        changed = paths(live, api)
        is_approved = approved(approvals, number, member_head, member_base, api)
        if any(not in_scope(path) for path in changed) and not is_approved:
            return False
        if is_approved:
            covered.update(changed)
        # PR file listings follow a mutable head. Confirm their revision and diff
        # again after pagination, including force pushes with an unchanged count.
        after = current_pr(number, member_head, api)
        if after.get("changed_files") != live.get("changed_files") or merge_base(after, member_head, api) != member_base:
            return False
    return all(in_scope(path) or path in covered for path in combined_paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--paths-file", help="complete workflow-pinned combined diff paths")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", type=int)
    group.add_argument("--batch-id", type=int)
    args = parser.parse_args()
    try:
        combined_paths = pathlib.Path(args.paths_file).read_text().splitlines() if args.paths_file else None
        data, admission = observation_with_admission(args.batch_id)
        ok = verify(data, args.head, args.base, pr=args.pr, batch_id=args.batch_id,
                    combined_paths=combined_paths, admission=admission,
                    refresh=lambda: observation_with_admission(args.batch_id))
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"bors scope approval unavailable: {type(error).__name__}")
        ok = False
    print("restricted paths approved by a repository admin through bors" if ok else "no valid bors scope approval")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
