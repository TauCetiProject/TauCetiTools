"""Source discussion reads in GitHub REST shape, with native authority retained.

No author association or approval authority is inferred. Historical PR metadata
uses the same retained-head and diff-stat checks as explicit familiar commands.
"""

from __future__ import annotations

import json
import re
import urllib.parse

from .forge import Incomplete, RepositoryRef, Unsupported
from .forge_read import ForgejoReader


def _positive(value, what):
    if type(value) is not int or value < 1:
        raise Incomplete(f"Forgejo {what} identity is unavailable")
    return value


def _actor(row):
    if row is None:
        return
    if not isinstance(row, dict) or not isinstance(row.get("login"), str) or not row["login"]:
        raise Incomplete("Forgejo actor identity is unavailable")
    # Deleted/imported/actions actors may have nonpositive native sentinel IDs.
    if type(row.get("id")) is not int:
        raise Incomplete("Forgejo actor native ID is unavailable")


def _bound_url(value, repo, number):
    if not isinstance(value, str):
        raise Incomplete("Forgejo source discussion URL is unavailable")
    parsed = urllib.parse.urlsplit(value)
    expected = urllib.parse.urlsplit(repo.forge.url)
    if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc) or parsed.query:
        raise Incomplete("Forgejo source discussion URL belongs to another origin")
    if parsed.path.casefold() not in {
        f"/{repo.name}/issues/{number}".casefold(), f"/{repo.name}/pulls/{number}".casefold()
    }:
        raise Incomplete("Forgejo source discussion URL belongs to another issue or PR")


def _common(row, repo, number):
    if not isinstance(row, dict):
        raise Incomplete("Forgejo source discussion response was not an object")
    _positive(row.get("id"), "source discussion")
    if type(row.get("number")) is not int or row["number"] != number:
        raise Incomplete("Forgejo source discussion did not identify the requested number")
    _bound_url(row.get("html_url"), repo, number)
    for key in ("title", "body", "created_at", "updated_at"):
        if not isinstance(row.get(key), str):
            raise Incomplete(f"Forgejo source discussion {key} is unavailable")
    if row.get("state") not in {"open", "closed"}:
        raise Incomplete("Forgejo source discussion state is unavailable")
    if type(row.get("is_locked")) is not bool:
        raise Incomplete("Forgejo source discussion lock state is unavailable")
    if "user" not in row:
        raise Incomplete("Forgejo source discussion actor is unavailable")
    _actor(row["user"])
    for key in ("labels", "assignees"):
        if key not in row or (row[key] is not None and not isinstance(row[key], list)):
            raise Incomplete(f"Forgejo source discussion {key} are unavailable")
    for label in row["labels"] or []:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            raise Incomplete("Forgejo label identity is unavailable")
        _positive(label.get("id"), "label")
    for actor in row["assignees"] or []:
        _actor(actor)


def _bounded(rows, api):
    if len(json.dumps(rows, ensure_ascii=False).encode()) > api.max_bytes:
        raise Incomplete("source discussion exceeds the configured aggregate byte bound")
    return rows


def source_read(request, api):
    repo = RepositoryRef(api.forge, request.repository_name)
    suffix = request.path[len("repos/" + repo.name) + 1 :]
    number = int(suffix.split("/")[1])
    base = api.forge.api_url + "/repos/" + repo.name
    if request.kind == "issue_comments":
        # Native Forgejo17 comments are unpaginated; synthetic gh pages below
        # slice this single bounded response instead of repeating its contents.
        rows = api.comments(repo, number)
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise Incomplete("Forgejo issue comment was not an object")
            identity = _positive(row.get("id"), "comment")
            if identity in seen:
                raise Incomplete("Forgejo issue comments repeated an identity")
            seen.add(identity)
            _bound_url(row.get("html_url"), repo, number)
            if any(not isinstance(row.get(k), str) for k in ("body", "created_at", "updated_at")) or "user" not in row:
                raise Incomplete("Forgejo issue comment metadata is unavailable")
            _actor(row["user"])
        return _bounded([{**row, "url": base + "/issues/comments/" + str(row["id"]),
                          "issue_url": base + "/issues/" + str(number)} for row in rows], api)
    reader = ForgejoReader(api)
    if request.kind == "pull_reviews":
        # review_records exhausts counted pages across hidden drafts, filters other
        # actors' pending reviews, and excludes reviewer-request placeholders.
        rows = reader.review_records(repo, number)
        result, seen = [], set()
        for row in rows:
            native_id = _positive(row.get("id"), "review")
            if native_id in seen:
                raise Incomplete("Forgejo reviews repeated an identity")
            seen.add(native_id)
            states = {"PENDING": "PENDING", "COMMENT": "COMMENTED", "APPROVED": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}
            if row.get("state") not in states or any(type(row.get(key)) is not bool for key in ("dismissed", "official", "stale")):
                raise Incomplete("Forgejo review state/authority metadata is unavailable")
            if any(not isinstance(row.get(key), str) for key in ("body", "submitted_at", "updated_at", "commit_id")) or "user" not in row:
                raise Incomplete("Forgejo review metadata is unavailable")
            # Web-started own drafts have no summary comment yet, hence no URL.
            # review_records has already restricted PENDING rows to this actor.
            if row["state"] != "PENDING" or row.get("html_url") != "":
                _bound_url(row.get("html_url"), repo, number)
            _actor(row["user"])
            result.append({**row, "state": "DISMISSED" if row["dismissed"] else states[row["state"]],
                           "submitted_at": None if row["state"] == "PENDING" else row["submitted_at"],
                           "pull_request_url": base + "/pulls/" + str(number)})
        return _bounded(result, api)
    row = api.pull(repo, number) if request.kind == "pull" else api.issue(repo, number)
    _common(row, repo, number)
    value = {**row, "labels": row["labels"] or [], "assignees": row["assignees"] or []}
    if request.kind == "pull":
        if type(row.get("flow")) is not int:
            raise Incomplete("Forgejo PR flow is unavailable")
        if row["flow"] != 0:
            raise Unsupported("AGit PR metadata requires a retained-head/diff adapter")
        if any(type(row.get(key)) is not bool for key in ("draft", "merged", "mergeable")):
            raise Incomplete("Forgejo PR lifecycle/mergeability metadata is unavailable")
        if row["merged"] and row["state"] != "closed":
            raise Incomplete("Forgejo merged PR lifecycle is inconsistent")
        for key in ("merged_at", "closed_at", "merge_commit_sha", "merged_by", "requested_reviewers"):
            if key not in row:
                raise Incomplete(f"Forgejo PR {key} is unavailable")
        if ((row["merged"] and not isinstance(row["merged_at"], str))
                or (not row["merged"] and row["merged_at"] is not None)):
            raise Incomplete("Forgejo PR merged timestamp disagrees with its lifecycle")
        if row["closed_at"] is not None and not isinstance(row["closed_at"], str):
            raise Incomplete("Forgejo PR closed timestamp is unavailable")
        if row["merge_commit_sha"] is not None and (not isinstance(row["merge_commit_sha"], str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", row["merge_commit_sha"])):
            raise Incomplete("Forgejo merged commit identity is unavailable")
        if row["merged"] and row["merge_commit_sha"] is None:
            raise Incomplete("Forgejo merged PR has no merged commit identity")
        _actor(row["merged_by"])
        if row["requested_reviewers"] is not None and not isinstance(row["requested_reviewers"], list):
            raise Incomplete("Forgejo requested reviewers are unavailable")
        value["requested_reviewers"] = row["requested_reviewers"] or []
        for actor in value["requested_reviewers"]:
            _actor(actor)
        for side in ("head", "base"):
            branch = row.get(side)
            if not isinstance(branch, dict) or any(not isinstance(branch.get(key), str) or not branch[key] for key in ("ref", "label")):
                raise Incomplete("Forgejo PR branch name is unavailable")
        branch = row.get("base")
        if not isinstance(branch, dict) or not isinstance(branch.get("sha"), str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", branch["sha"]):
            raise Incomplete("Forgejo PR base identity is unavailable")
        fields = ["headRefOid", "changedFiles", "additions", "deletions"]
        verified = reader._hydrate(row, repo, fields, pull=True)
        if not isinstance(verified["headRefOid"], str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", verified["headRefOid"]):
            raise Incomplete("Forgejo PR head identity is unavailable")
        if any(type(verified[key]) is not int or verified[key] < 0 for key in ("changedFiles", "additions", "deletions")):
            raise Incomplete("Forgejo PR diff statistics are unavailable")
        # Never expose current source-branch metadata as historical proposal data.
        value.update(changed_files=verified["changedFiles"], additions=verified["additions"], deletions=verified["deletions"])
        value["head"] = {**row["head"], "sha": verified["headRefOid"], "ref": row["head"]["label"], "forgejo_ref": row["head"]["ref"]}
        # Forgejo false also covers checking/error/draft; GitHub REST uses null
        # for unknown. Do not turn uncertainty into a claimed merge conflict.
        value["mergeable"] = True if row["mergeable"] else None
        value["locked"] = value.pop("is_locked")
        value["url"] = base + "/pulls/" + str(number)
    else:
        value.pop("pull_request", None)  # GitHub omits the PR marker for plain issues.
        value["locked"] = value.pop("is_locked")
        value["url"] = base + "/issues/" + str(number)
    return _bounded(value, api)
