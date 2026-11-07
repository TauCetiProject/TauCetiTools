"""Single-attempt, identity-checked native Forgejo operations for gh commands.

Multi-request commands validate their inputs before any mutation. A later
failure after a confirmed mutation is an ambiguous command outcome and requires
reconciliation; this layer never retries mutations.
"""

from __future__ import annotations

import re
from urllib.parse import quote

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
        completed, result = [], None
        for method, path, data in operations:
            try:
                result = self.api.request(method, path, data)
            except (ForgeError, ValueError) as error:
                if completed:
                    raise AmbiguousWrite(
                        f"Forgejo command partially applied; completed {completed}; failed {method} {path}; reconcile before retrying"
                    ) from error
                raise
            completed.append(f"{method} {path}")
        return result

    def labels(self, repository, *, organization=False):
        if organization:
            try:
                labels = self.api.collection("orgs/" + quote(repository.name.split("/")[0], safe="") + "/labels")
            except ForgeError as error:
                if error.status == 404:
                    return []
                if error.status == 403:
                    raise ForgeError(
                        "organization label lookup requires organization read scope", status=403
                    ) from error
                raise
        else:
            labels = self.api.collection(self._path(repository) + "/labels")
        for label in labels:
            if (
                not isinstance(label, dict)
                or not isinstance(label.get("name"), str)
                or type(label.get("id")) is not int
                or label["id"] < 1
            ):
                raise Incomplete("Forgejo label identity is unavailable")
        return labels

    def resolve_labels(self, repository, names):
        if not names:
            return []
        labels = self.labels(repository)
        result, org_labels = [], None
        for name in names:
            matches = [row for row in labels if row["name"] == name]
            if not matches:
                matches = [row for row in labels if row["name"].casefold() == name.casefold()]
            if not matches:
                if org_labels is None:
                    org_labels = self.labels(repository, organization=True)
                matches = [row for row in org_labels if row["name"] == name] or [
                    row for row in org_labels if row["name"].casefold() == name.casefold()
                ]
            if not matches:
                raise ForgeError("requested label does not exist")
            if len({row["id"] for row in matches}) != 1:
                raise Incomplete("requested label name is ambiguous")
            if matches[0]["id"] not in {row["id"] for row in result}:
                result.append(matches[0])
        return result

    def label_ids(self, repository, names):
        return [row["id"] for row in self.resolve_labels(repository, names)]

    @staticmethod
    def wip_prefix(title):
        if not isinstance(title, str):
            raise Incomplete("Forgejo title is unavailable")
        return next((title[: len(prefix)] for prefix in ["WIP:", "[WIP]"] if title.upper().startswith(prefix)), "")

    def create(self, repository, *, pull, title, body="", base=None, head=None, labels=(), draft=False):
        path = self._path(repository)
        if not isinstance(title, str) or not title:
            raise ValueError("title is required")
        if pull and not head:
            raise ValueError("head branch is required")
        if draft and not pull:
            raise Unsupported("issues do not have draft state")
        if pull and self.wip_prefix(title) and not draft:
            raise Unsupported("WIP titles require --draft; Forgejo derives draft state from its title")
        data = {"title": "WIP: " + title if draft and not self.wip_prefix(title) else title, "body": body}
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
        if pull:
            # Forgejo 17 represents WIP using title prefixes, not a draft input
            # field (unknown JSON fields would otherwise be silently ignored).
            try:
                created = self.api.pull(repository, row["number"])
            except ForgeError as error:
                raise AmbiguousWrite(
                    f"Forgejo creation {repository.pull_url(row['number'])} draft state could not be verified; reconcile before retrying"
                ) from error
            if created.get("draft") is not draft:
                raise AmbiguousWrite(
                    f"Forgejo creation {repository.pull_url(row['number'])} did not confirm requested draft state; reconcile before retrying"
                )
        return row["number"]

    def edit(self, repository, number, *, pull, changes=None, add_labels=(), remove_labels=()):
        repository.pull_url(number)
        path = self._path(repository)
        data = dict(changes or {})
        allowed = {"title", "body", "base"} if pull else {"title", "body"}
        if not data.keys() <= allowed:
            raise Unsupported("unsupported edit fields")
        current = self.api.pull(repository, number) if pull else self.api.issue(repository, number)
        if pull and "title" in data:
            draft = current.get("draft")
            if not isinstance(draft, bool):
                raise Incomplete("Forgejo draft state is unavailable")
            prefix = self.wip_prefix(current.get("title", ""))
            if draft and not prefix:
                raise Unsupported("cannot preserve this server's custom WIP prefix on a title edit")
            if draft and not self.wip_prefix(data["title"]):
                data["title"] = prefix + " " + data["title"]
            if not draft and self.wip_prefix(data["title"]):
                raise Unsupported("title edits cannot change draft state; use an explicit draft operation")
        adds = self.label_ids(repository, add_labels)
        remove_rows = self.resolve_labels(repository, remove_labels)
        removes = [row["id"] for row in remove_rows]
        if set(adds) & set(removes):
            raise ValueError("a label cannot be added and removed in the same command")
        operations = []
        if adds:
            operations.append(("POST", f"{path}/issues/{number}/labels", {"labels": adds}))
        # Forgejo resolves the route parameter by name before ID. Use the exact
        # resolved name, avoiding collisions with another label named "3".
        operations.extend(
            ("DELETE", f"{path}/issues/{number}/labels/{quote(row['name'], safe='')}", None) for row in remove_rows
        )
        # Labels require issue/PR write access, while an author may edit their
        # body without it. Test label authority before applying body/title edits.
        if data:
            operations.append(("PATCH", f"{path}/{'pulls' if pull else 'issues'}/{number}", data))
        if not operations:
            raise ValueError("no changes were requested")
        self._steps(operations)
        if pull and "title" in data:
            try:
                after = self.api.pull(repository, number)
            except ForgeError as error:
                raise AmbiguousWrite(
                    f"title edit on {repository.pull_url(number)} could not verify draft state; reconcile before retrying"
                ) from error
            if after.get("draft") is not draft:
                raise AmbiguousWrite(
                    f"title edit on {repository.pull_url(number)} changed draft state; reconcile before retrying"
                )

    def comment(self, repository, number, body, *, pull=False):
        repository.pull_url(number)
        path = self._path(repository)
        if pull:
            self.api.pull(repository, number)
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
        if pull and current.get("merged") is True:
            raise ForgeError("merged pull requests cannot be closed or reopened")
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
        # Native submission takes over a pending review, including its inline
        # drafts. Do not publish unrelated draft work through a simple gh review.
        pending = self.api.reviews(repository, number)
        if any(
            isinstance(item, dict)
            and item.get("state") == "PENDING"
            and isinstance(item.get("user"), dict)
            and item["user"].get("id") == self.actor()["id"]
            for item in pending
        ):
            raise Unsupported("a pending review exists; submit or discard it explicitly before gh pr review")
        review = self._steps(
            [("POST", f"{path}/pulls/{number}/reviews", {"event": event, "body": body, "commit_id": revision})]
        )
        if not isinstance(review, dict) or review.get("commit_id") != revision:
            raise AmbiguousWrite(
                f"Forgejo review on {repository.pull_url(number)} did not confirm the intended commit; reconcile before retrying"
            )
        return review

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
        rows = self.labels(repository)
        matches = [row for row in rows if row["name"].casefold() == name.casefold()]
        if len(matches) > 1:
            raise Incomplete("Forgejo returned ambiguous label names")
        existing = matches[0] if matches else None
        if existing is not None and not edit and not force:
            raise ForgeError("label already exists; use --force to update it")
        if rename is not None and any(
            row["name"].casefold() == rename.casefold() and (existing is None or row["id"] != existing["id"])
            for row in rows
        ):
            raise ForgeError("new label name already exists")
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
