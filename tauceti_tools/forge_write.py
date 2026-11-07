"""Single-attempt, identity-checked native Forgejo operations for gh commands.

Multi-request commands validate their inputs before any mutation. A later
failure after a confirmed mutation is an ambiguous command outcome and requires
reconciliation; this layer never retries mutations.
"""

from __future__ import annotations

import re

from .forge import AmbiguousWrite, ForgeError, Incomplete, RepositoryRef, Unsupported


class ForgejoWriter:
    def __init__(self, api, *, expected_user_id=None):
        self.api = api
        self.expected_user_id = expected_user_id
        self._actor = None

    def actor(self):
        if self._actor is None:
            actor = self.api.current_user()
            if self.expected_user_id is not None and str(actor["id"]) != str(self.expected_user_id):
                raise ForgeError("Forgejo account does not match the trusted expected user ID")
            self._actor = actor
        return self._actor

    def _path(self, repository):
        return self.api._repo(repository)

    def _steps(self, operations):
        self.actor()
        completed, result = 0, None
        for method, path, data in operations:
            try:
                result = self.api.request(method, path, data)
            except (ForgeError, ValueError) as error:
                if completed:
                    raise AmbiguousWrite("Forgejo command partially applied; reconcile before retrying") from error
                raise
            completed += 1
        return result

    def label_ids(self, repository, names):
        if not names:
            return []
        labels = self.api.collection(self._path(repository) + "/labels")
        found = {}
        for label in labels:
            if (
                not isinstance(label, dict)
                or not isinstance(label.get("name"), str)
                or type(label.get("id")) is not int
                or label["id"] < 1
            ):
                raise Incomplete("Forgejo label identity is unavailable")
            name = label["name"].casefold()
            if name in found and found[name] != label["id"]:
                raise Incomplete("Forgejo returned ambiguous label names")
            found[name] = label["id"]
        missing = [name for name in names if name.casefold() not in found]
        if missing:
            raise ForgeError("requested label does not exist")
        return list(dict.fromkeys(found[name.casefold()] for name in names))

    def create(self, repository, *, pull, title, body="", base=None, head=None, labels=(), draft=False):
        path = self._path(repository)
        if not isinstance(title, str) or not title:
            raise ValueError("title is required")
        if pull and not head:
            raise ValueError("head branch is required")
        if draft and not pull:
            raise Unsupported("issues do not have draft state")
        data = {"title": "WIP: " + title if draft else title, "body": body}
        if labels:
            data["labels"] = self.label_ids(repository, labels)
        if pull:
            if base is None:
                base = self.api.repository(repository).get("default_branch")
            if not isinstance(base, str) or not base:
                raise Incomplete("Forgejo default branch is unavailable")
            data.update(base=base, head=head)
        row = self._steps([("POST", path + ("/pulls" if pull else "/issues"), data)])
        if not isinstance(row, dict) or type(row.get("number")) is not int or row["number"] < 1:
            raise AmbiguousWrite("Forgejo created an issue/PR without a usable response; reconcile before retrying")
        if draft:
            # Forgejo 17 represents WIP using title prefixes, not a draft input
            # field (unknown JSON fields would otherwise be silently ignored).
            try:
                created = self.api.pull(repository, row["number"])
            except ForgeError as error:
                raise AmbiguousWrite(
                    "Forgejo draft creation could not be verified; reconcile before retrying"
                ) from error
            if created.get("draft") is not True:
                raise AmbiguousWrite("Forgejo did not confirm native WIP state; reconcile before retrying")
        return row["number"]

    def edit(self, repository, number, *, pull, changes=None, add_labels=(), remove_labels=()):
        repository.pull_url(number)
        path = self._path(repository)
        data = dict(changes or {})
        allowed = {"title", "body", "base"} if pull else {"title", "body"}
        if not data.keys() <= allowed:
            raise Unsupported("unsupported edit fields")
        adds, removes = self.label_ids(repository, add_labels), self.label_ids(repository, remove_labels)
        if set(adds) & set(removes):
            raise ValueError("a label cannot be added and removed in the same command")
        operations = []
        if data:
            operations.append(("PATCH", f"{path}/{'pulls' if pull else 'issues'}/{number}", data))
        if adds:
            operations.append(("POST", f"{path}/issues/{number}/labels", {"labels": adds}))
        operations.extend(("DELETE", f"{path}/issues/{number}/labels/{native_id}", None) for native_id in removes)
        if not operations:
            raise ValueError("no changes were requested")
        self._steps(operations)

    def comment(self, repository, number, body):
        repository.pull_url(number)
        path = self._path(repository)
        row = self._steps([("POST", f"{path}/issues/{number}/comments", {"body": body})])
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 1:
            raise AmbiguousWrite("Forgejo comment response was incomplete; reconcile before retrying")
        return row["id"]

    def lifecycle(self, repository, number, *, pull, state, comment=None):
        repository.pull_url(number)
        if state not in {"open", "closed"}:
            raise ValueError("invalid lifecycle state")
        path = self._path(repository)
        current = self.api.pull(repository, number) if pull else self.api.issue(repository, number)
        if current.get("state") not in {"open", "closed"}:
            raise Incomplete("Forgejo lifecycle state is unavailable")
        if pull and current.get("merged") is True and state == "open":
            raise ForgeError("merged pull requests cannot be reopened")
        if current["state"] == state:
            return
        operations = []
        if comment is not None:
            operations.append(("POST", f"{path}/issues/{number}/comments", {"body": comment}))
        operations.append(("PATCH", f"{path}/{'pulls' if pull else 'issues'}/{number}", {"state": state}))
        self._steps(operations)

    def review(self, repository, number, *, event, body=""):
        path = self._path(repository)
        repository.pull_url(number)
        if event not in {"APPROVED", "COMMENT", "REQUEST_CHANGES"}:
            raise ValueError("invalid review event")
        if event != "APPROVED" and not body.strip():
            raise ValueError("comment and change-request reviews require a body")
        self.actor()
        row = self.api.pull(repository, number)
        head = row.get("head")
        revision = head.get("sha") if isinstance(head, dict) else None
        if (
            row.get("state") != "open"
            or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision)
        ):
            raise Incomplete("Forgejo review requires an open PR with a known head")
        if self.api.pull_ref(repository, number) != revision:
            raise Incomplete("Forgejo has not synchronized the requested review head")
        review = self._steps(
            [("POST", f"{path}/pulls/{number}/reviews", {"event": event, "body": body, "commit_id": revision})]
        )
        if not isinstance(review, dict) or review.get("commit_id") != revision:
            raise AmbiguousWrite("Forgejo review did not confirm the intended commit; reconcile before retrying")

    def label(self, repository, name, *, color=None, description=None, edit=False, rename=None, force=False):
        path = self._path(repository)
        if not name:
            raise ValueError("label name is required")
        if color is not None and not re.fullmatch(r"[0-9a-fA-F]{6}", color.removeprefix("#")):
            raise ValueError("label color must be six hexadecimal digits")
        data = {}
        if color is not None:
            data["color"] = color.removeprefix("#")
        if description is not None:
            data["description"] = description
        existing = None
        if edit or force:
            rows = self.api.collection(path + "/labels")
            matches = [
                row for row in rows if isinstance(row, dict) and str(row.get("name", "")).casefold() == name.casefold()
            ]
            if len(matches) > 1:
                raise Incomplete("Forgejo returned ambiguous label names")
            existing = matches[0] if matches else None
        if edit and existing is None:
            raise ForgeError("requested label does not exist")
        if existing is not None:
            native_id = existing.get("id")
            if type(native_id) is not int or native_id < 1:
                raise Incomplete("Forgejo label identity is unavailable")
            if rename is not None:
                data["name"] = rename
            if not data:
                raise ValueError("no label changes were requested")
            self._steps([("PATCH", f"{path}/labels/{native_id}", data)])
        else:
            data.update(name=name, color=data.get("color", "ededed"))
            self._steps([("POST", path + "/labels", data)])

    def fork(self, repository, *, name=None, organization=None):
        path = self._path(repository)
        actor = self.actor()
        target = RepositoryRef(
            repository.forge, (organization or actor["login"]) + "/" + (name or repository.name.split("/")[1])
        )
        try:
            existing = self.api.repository(target)
        except ForgeError as error:
            if error.status != 404:
                raise
        else:
            parent = existing.get("parent")
            if (
                existing.get("fork") is not True
                or not isinstance(parent, dict)
                or str(parent.get("full_name", "")).casefold() != repository.name.casefold()
            ):
                raise ForgeError("fork destination already exists with a different parent")
            return target
        data = {}
        if name is not None:
            data["name"] = name
        if organization is not None:
            data["organization"] = organization
        row = self._steps([("POST", path + "/forks", data)])
        if not isinstance(row, dict) or str(row.get("full_name", "")).casefold() != target.name.casefold():
            raise AmbiguousWrite("Forgejo fork response was incomplete; reconcile before retrying")
        return target
