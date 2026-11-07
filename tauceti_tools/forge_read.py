"""Explicit gh-shaped read contracts over the direct Forgejo API.

Only requested expensive collections are hydrated. Missing source metadata is
unavailable, never a negative approval or a successful empty observation. IDs are
opaque, forge-qualified strings; numeric IDs remain native to their own server.
"""

from __future__ import annotations

from .forge import Incomplete, Unsupported

COMMON_FIELDS = {
    "id",
    "number",
    "title",
    "body",
    "labels",
    "created_at",
    "updated_at",
    "url",
    "state",
    "author",
    "createdAt",
    "updatedAt",
    "closedAt",
    "assignees",
    "milestone",
    "comments",
    "isLocked",
}
PR_FIELDS = COMMON_FIELDS | {
    "headRefOid",
    "headRefName",
    "baseRefOid",
    "baseRefName",
    "isDraft",
    "mergedAt",
    "mergedBy",
    "headRepository",
    "headRepositoryOwner",
    "baseRepository",
    "isCrossRepository",
    "mergeable",
    "additions",
    "deletions",
    "changedFiles",
    "reviews",
    "files",
    "statusCheckRollup",
    "mergeCommit",
}
ISSUE_FIELDS = COMMON_FIELDS
REPO_FIELDS = {
    "id",
    "name",
    "nameWithOwner",
    "owner",
    "url",
    "description",
    "isPrivate",
    "isFork",
    "isArchived",
    "defaultBranchRef",
    "parent",
    "sshUrl",
    "viewerPermission",
    "createdAt",
    "updatedAt",
}


def validate_fields(fields, supported):
    unknown = set(fields or ()) - supported
    if unknown:
        raise Unsupported("unsupported JSON fields: " + ", ".join(sorted(unknown)))


def project(row, fields):
    if fields is None:
        return row
    missing = set(fields) - row.keys()
    if missing:
        raise Incomplete("unavailable JSON fields: " + ", ".join(sorted(missing)))
    return {field: row[field] for field in fields}


def _object(row, name):
    if not isinstance(row, dict):
        raise Incomplete(f"Forgejo {name} response was not an object")
    return row


def _positive(value, name):
    if type(value) is not int or value < 1:
        raise Incomplete(f"Forgejo did not identify {name}")
    return value


def _identity(repository, kind, native_id):
    return f"{repository.forge.key}/{kind.lower()}/{_positive(native_id, kind)}"


def _user(row, repository):
    if row is None:
        return None  # A deleted or absent actor is not an invented bot/human.
    _object(row, "user")
    if not isinstance(row.get("login"), str) or not row["login"]:
        raise Incomplete("Forgejo user has no login")
    result = {"login": row["login"]}
    if "id" in row:
        result["id"] = _identity(repository, "users", row["id"])
    return result


def _copy(row, result, mappings):
    for target, source in mappings.items():
        if source in row:
            result[target] = row[source]


def _labels(rows, repository):
    if rows is None:
        return []  # Forgejo serializes an explicitly empty native slice as null.
    if not isinstance(rows, list):
        raise Incomplete("Forgejo labels were not an array")
    result = []
    for row in rows:
        _object(row, "label")
        if not isinstance(row.get("name"), str):
            raise Incomplete("Forgejo label has no name")
        value = {k: row[k] for k in ("name", "color", "description") if k in row}
        if "id" in row:
            value["id"] = _identity(repository, f"repos/{repository.name}/labels", row["id"])
        result.append(value)
    return result


def _common(row, repository, *, pull):
    _object(row, "PR" if pull else "issue")
    number = _positive(row.get("number"), "issue/PR")
    result = {"number": number, "url": repository.pull_url(number) if pull else f"{repository.url}/issues/{number}"}
    _copy(
        row,
        result,
        {
            "title": "title",
            "body": "body",
            "created_at": "created_at",
            "updated_at": "updated_at",
            "createdAt": "created_at",
            "updatedAt": "updated_at",
            "closedAt": "closed_at",
        },
    )
    if "id" in row:
        result["id"] = _identity(repository, f"repos/{repository.name}/{'pulls' if pull else 'issues'}", row["id"])
    if "labels" in row:
        result["labels"] = _labels(row["labels"], repository)
    if "user" in row:
        result["author"] = _user(row["user"], repository)
    if "assignees" in row:
        if row["assignees"] is not None and not isinstance(row["assignees"], list):
            raise Incomplete("Forgejo assignees were not an array")
        result["assignees"] = [_user(user, repository) for user in row["assignees"] or []]
    if isinstance(row.get("is_locked"), bool):
        result["isLocked"] = row["is_locked"]
    if "milestone" in row:
        milestone = row["milestone"]
        if milestone is None:
            result["milestone"] = None
        else:
            _object(milestone, "milestone")
            result["milestone"] = {k: milestone[k] for k in ("title", "description") if k in milestone}
            if "id" in milestone:
                result["milestone"]["id"] = _identity(
                    repository, f"repos/{repository.name}/milestones", milestone["id"]
                )
    return result


def pull_record(row, repository):
    result = _common(row, repository, pull=True)
    _copy(
        row,
        result,
        {
            "mergedAt": "merged_at",
            "additions": "additions",
            "deletions": "deletions",
            "changedFiles": "changed_files",
        },
    )
    if isinstance(row.get("merged"), bool) and row.get("state") in {"open", "closed"}:
        result["state"] = "MERGED" if row["merged"] else row["state"].upper()
    if isinstance(row.get("draft"), bool):
        result["isDraft"] = row["draft"]
    if isinstance(row.get("mergeable"), bool):
        # Forgejo false also means checking/error/draft. It does not prove a conflict.
        result["mergeable"] = "MERGEABLE" if row["mergeable"] else "UNKNOWN"
    if "merged_by" in row:
        result["mergedBy"] = _user(row["merged_by"], repository)
    if "merge_commit_sha" in row:
        result["mergeCommit"] = {"oid": row["merge_commit_sha"]} if row["merge_commit_sha"] else None
    for side in ("head", "base"):
        branch = row.get(side)
        if not isinstance(branch, dict):
            continue
        _copy(branch, result, {side + "RefOid": "sha", side + "RefName": "ref"})
        if "repo" in branch:
            repo = branch["repo"]
            if repo is None:
                result[side + "Repository"] = None
                if side == "head":
                    result["headRepositoryOwner"] = None
            else:
                _object(repo, "branch repository")
                result[side + "Repository"] = {k: repo[k] for k in ("name",) if k in repo}
                if "full_name" in repo:
                    result[side + "Repository"]["nameWithOwner"] = repo["full_name"]
                if side == "head" and "owner" in repo:
                    result["headRepositoryOwner"] = _user(repo["owner"], repository)
    head, base = row.get("head"), row.get("base")
    if isinstance(head, dict) and isinstance(base, dict):
        hr, br = head.get("repo"), base.get("repo")
        if isinstance(hr, dict) and isinstance(br, dict) and "id" in hr and "id" in br:
            result["isCrossRepository"] = hr["id"] != br["id"]
    return result


def issue_record(row, repository):
    result = _common(row, repository, pull=False)
    if row.get("state") in {"open", "closed"}:
        result["state"] = row["state"].upper()
    if row.get("pull_request") is not None:
        raise Incomplete("Forgejo returned a PR where an issue was requested")
    return result


class ForgejoReader:
    def __init__(self, api):
        self.api = api

    def _comments(self, repository, number):
        result = []
        for row in self.api.comments(repository, number):
            _object(row, "comment")
            value = {"id": _identity(repository, f"repos/{repository.name}/comments", row.get("id"))}
            _copy(row, value, {"body": "body", "url": "html_url", "createdAt": "created_at", "updatedAt": "updated_at"})
            if "user" in row:
                value["author"] = _user(row["user"], repository)
            result.append(project(value, ["id", "body", "url", "author", "createdAt", "updatedAt"]))
        return result

    def _reviews(self, repository, number):
        result = []
        states = {
            "PENDING": "PENDING",
            "COMMENT": "COMMENTED",
            "APPROVED": "APPROVED",
            "REQUEST_CHANGES": "CHANGES_REQUESTED",
        }
        for row in self.api.reviews(repository, number):
            _object(row, "review")
            if row.get("state") == "REQUEST_REVIEW":
                continue  # A reviewer request is not a submitted review.
            if row.get("state") not in states or not isinstance(row.get("dismissed"), bool):
                raise Incomplete("Forgejo review state is unavailable")
            value = {
                "id": _identity(repository, f"repos/{repository.name}/reviews", row.get("id")),
                "state": "DISMISSED" if row["dismissed"] else states[row["state"]],
            }
            _copy(row, value, {"body": "body", "submittedAt": "submitted_at", "url": "html_url"})
            if "commit_id" in row:
                value["commit"] = {"oid": row["commit_id"]}
            if "user" in row:
                value["author"] = _user(row["user"], repository)
            result.append(project(value, ["id", "state", "body", "submittedAt", "url", "commit", "author"]))
        return result

    def _files(self, repository, number):
        result = []
        for row in self.api.files(repository, number):
            _object(row, "changed file")
            result.append(
                project(
                    {
                        target: row[source]
                        for target, source in {
                            "path": "filename",
                            "additions": "additions",
                            "deletions": "deletions",
                        }.items()
                        if source in row
                    },
                    ["path", "additions", "deletions"],
                )
            )
        return result

    def _statuses(self, repository, revision):
        latest = {}
        for row in self.api.statuses(repository, revision):
            _object(row, "commit status")
            _positive(row.get("id"), "commit status")
            if not isinstance(row.get("context"), str) or row.get("status") not in {
                "pending",
                "success",
                "error",
                "failure",
            }:
                raise Incomplete("Forgejo status context/state is unavailable")
            old = latest.get(row["context"])
            if old is None or old["id"] < row["id"]:
                latest[row["context"]] = row
        return [
            project(
                {
                    "context": row["context"],
                    "state": row["status"].upper(),
                    **{
                        target: row[source]
                        for target, source in {
                            "targetUrl": "target_url",
                            "startedAt": "created_at",
                            "description": "description",
                        }.items()
                        if source in row
                    },
                },
                ["context", "state", "targetUrl", "startedAt", "description"],
            )
            for row in latest.values()
        ]

    def _hydrate(self, row, repository, fields, *, pull):
        value = pull_record(row, repository) if pull else issue_record(row, repository)
        wanted = set(fields or ())
        if "comments" in wanted:
            value["comments"] = self._comments(repository, value["number"])
        if "reviews" in wanted:
            value["reviews"] = self._reviews(repository, value["number"])
        if "files" in wanted:
            value["files"] = self._files(repository, value["number"])
            count = row.get("changed_files")
            if type(count) is not int or count < 0 or len(value["files"]) != count:
                raise Incomplete("Forgejo file listing did not match the PR's changed-file count")
            # Unlike status requests, the native file endpoint follows a mutable
            # PR. Refuse to combine files from a new head/base with old metadata.
            after = self.api.pull(repository, value["number"])
            for side in ("head", "base"):
                before_ref, after_ref = row.get(side), after.get(side)
                if (
                    not isinstance(before_ref, dict)
                    or not isinstance(after_ref, dict)
                    or not before_ref.get("sha")
                    or before_ref["sha"] != after_ref.get("sha")
                ):
                    raise Incomplete("Forgejo PR head/base changed while reading files")
        if "statusCheckRollup" in wanted:
            if "headRefOid" not in value:
                raise Incomplete("Forgejo PR head is unavailable for statuses")
            value["statusCheckRollup"] = self._statuses(repository, value["headRefOid"])
        return project(value, fields)

    def pull(self, repository, number, fields=None):
        validate_fields(fields, PR_FIELDS)
        return self._hydrate(self.api.pull(repository, number), repository, fields, pull=True)

    def pulls(self, repository, fields=None, **filters):
        validate_fields(fields, PR_FIELDS)
        return [self._hydrate(row, repository, fields, pull=True) for row in self.api.pulls(repository, **filters)]

    def issue(self, repository, number, fields=None):
        validate_fields(fields, ISSUE_FIELDS)
        return self._hydrate(self.api.issue(repository, number), repository, fields, pull=False)

    def issues(self, repository, fields=None, **filters):
        validate_fields(fields, ISSUE_FIELDS)
        return [self._hydrate(row, repository, fields, pull=False) for row in self.api.issues(repository, **filters)]

    def repository(self, repository, fields=None):
        validate_fields(fields, REPO_FIELDS)
        row = self.api.repository(repository)
        value = {}
        _copy(
            row,
            value,
            {
                "name": "name",
                "nameWithOwner": "full_name",
                "description": "description",
                "isPrivate": "private",
                "isFork": "fork",
                "isArchived": "archived",
                "sshUrl": "ssh_url",
                "createdAt": "created_at",
                "updatedAt": "updated_at",
            },
        )
        value["url"] = repository.url
        if "id" in row:
            value["id"] = _identity(repository, "repositories", row["id"])
        if "owner" in row:
            value["owner"] = _user(row["owner"], repository)
        if "default_branch" in row:
            value["defaultBranchRef"] = {"name": row["default_branch"]} if row["default_branch"] else None
        if "parent" in row:
            parent = row["parent"]
            if parent is None:
                value["parent"] = None
            elif isinstance(parent, dict) and "full_name" in parent and "name" in parent:
                value["parent"] = {"nameWithOwner": parent["full_name"], "name": parent["name"]}
        permissions = row.get("permissions")
        if isinstance(permissions, dict) and all(
            isinstance(permissions.get(k), bool) for k in ("admin", "push", "pull")
        ):
            value["viewerPermission"] = next(
                (v for k, v in [("admin", "ADMIN"), ("push", "WRITE"), ("pull", "READ")] if permissions[k]), "NONE"
            )
        return project(value, fields)
