"""Execute parsed native Forgejo commands using the typed read/write layers."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .forge import Incomplete, RepositoryRef, Unsupported
from .forge_read import ForgejoReader, project, validate_fields
from .forge_write import ForgejoWriter


class CheckResults(list):
    """Keep the command status when --json selects fields without bucket/state."""

    def __init__(self, rows, status):
        super().__init__(rows)
        self.status = status


def names(groups):
    return [name for group in groups for name in group.split(",")]


def body(options):
    if "body_file" in options:
        path = options["body_file"]
        return sys.stdin.read() if path == "-" else Path(path).read_text()
    return options.get("body", "")


def execute(command, repository, api, *, token=None, expected_user_id=None):
    """Return (output value, JSON output requested), without retries."""
    group, action, opts = command.group, command.action, command.options
    reader = ForgejoReader(api)
    fields = opts["json"].split(",") if "json" in opts else None
    limit = int(opts.get("limit", "30"))
    if command.key == ("pr", "diff"):
        number = int(command.args[0])
        before = reader.pull(repository, number, ["headRefOid", "baseRefOid"])
        if opts.get("name_only"):
            # Historical PR stats/files on stock Forgejo are not always bound
            # to the retained comparison. Exact Git endpoints also handle file
            # limits, renames, and quoted filenames consistently with reviews.
            from .git_credentials import scoped_git_environment
            from .git_inputs import changed_paths

            row = api.pull(repository, number)
            base = row.get("merge_base")
            if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}", base):
                raise Incomplete("PR merge base unavailable for exact file listing")
            environment = scoped_git_environment(repository.forge, token) if token else None
            paths = changed_paths(repository.clone_url, base, before["headRefOid"], environment=environment)
            output = "".join(path + "\n" for path in paths)
        else:
            output = api.pull_diff(repository, number)
        if reader.pull(repository, number, ["headRefOid", "baseRefOid"]) != before:
            raise Incomplete("PR comparison changed while reading its diff")
        return output, False
    if command.key == ("pr", "checks"):
        supported = {"name", "state", "bucket", "description", "link", "startedAt", "completedAt", "workflow", "event"}
        validate_fields(fields, supported)
        statuses = reader.pull(repository, int(command.args[0]), ["statusCheckRollup"])["statusCheckRollup"]
        values = [dict(name=row["context"], state=row["state"],
                       bucket={"SUCCESS": "pass", "FAILURE": "fail", "ERROR": "fail"}.get(row["state"], "pending"),
                       description=row.get("description", ""), link=row.get("targetUrl", ""),
                       startedAt=row.get("startedAt", ""), completedAt="", workflow="", event="native-status")
                  for row in statuses]
        # Native status names alone cannot attest to the configured execution
        # backend's exact-input build. Keep that missing evidence visible even
        # when an ordinary contributor posted a status named "build".
        values.append(dict(name="execution evidence", state="PENDING", bucket="pending",
                           description="Verified execution evidence unavailable", link="", startedAt="",
                           completedAt="", workflow="", event="execution"))
        status = 1 if any(row["bucket"] == "fail" for row in values) else 8
        return CheckResults([project(row, fields) for row in values], status), True
    if action == "view":
        if group == "repo":
            value = reader.repository(repository, fields)
        else:
            selected = fields
            if opts.get("comments"):
                selected = list(dict.fromkeys([*(fields or ["number", "title", "body", "url"]), "comments", "reviews"]))
            value = getattr(reader, "pull" if group == "pr" else "issue")(repository, int(command.args[0]), selected)
            if opts.get("comments") and fields is None:
                sections = [f"{value['title']}\n{value['url']}\n\n{value['body'] or ''}"]
                for row in [*value["comments"], *value["reviews"]]:
                    sections.append(f"{(row.get('author') or {}).get('login', 'unknown')}:\n{row.get('body') or ''}")
                return "\n\n".join(sections) + "\n", False
        return value, True
    if action == "list" and group in {"pr", "issue"}:
        author = opts.get("author")
        if author == "@me":
            author = api.current_user()["login"]
        filters = {
            "state": opts.get("state", "open"),
            "limit": limit,
            "author": author,
            "labels": names(opts.get("label", [])),
        }
        if group == "pr":
            filters.update(base=opts.get("base"), head=opts.get("head"))
        return getattr(reader, "pulls" if group == "pr" else "issues")(repository, fields, **filters), True
    if command.key == ("label", "list"):
        supported = {"id", "name", "description", "color"}
        validate_fields(fields, supported)
        rows = api.collection(api._repo(repository) + "/labels")
        values = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise Incomplete("Forgejo label response was not an object")
            value = {k: row[k] for k in supported - {"id"} if k in row}
            if type(row.get("id")) is int and row["id"] > 0:
                value["id"] = f"{repository.forge.key}/labels/{row['id']}"
            values.append(project(value, fields))
        return values, True
    if command.key == ("repo", "list"):
        supported = {"name", "nameWithOwner", "parent", "owner", "url", "isFork", "isPrivate", "description", "id"}
        validate_fields(fields, supported)
        actor = api.current_user()
        owner = command.args[0] if command.args else actor["login"]
        from urllib.parse import quote

        rows = api.collection(
            "user/repos"
            if owner.casefold() == actor["login"].casefold()
            else "users/" + quote(owner, safe="") + "/repos"
        )
        result = []
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("owner"), dict)
                or not isinstance(row["owner"].get("login"), str)
            ):
                raise Incomplete("Forgejo repository owner is unavailable")
            if row["owner"]["login"].casefold() != owner.casefold():
                continue
            if opts.get("fork"):
                if not isinstance(row.get("fork"), bool):
                    raise Incomplete("Forgejo repository fork state is unavailable")
                if not row["fork"]:
                    continue
            full_name = row.get("full_name")
            if not isinstance(full_name, str):
                raise Incomplete("Forgejo repository identity is unavailable")
            if isinstance(row.get("updated_at"), str):
                result.append((row["updated_at"], row))
            else:
                raise Incomplete("Forgejo repository update time is unavailable")
        values = []
        for _, row in sorted(result, key=lambda pair: pair[0], reverse=True)[:limit]:
            ref = RepositoryRef(repository.forge, row["full_name"])
            # Only hydrate the selected prefix, rather than every owned repo.
            values.append(reader.repository(ref, fields))
        return values, True
    writer = ForgejoWriter(api, expected_user_id=expected_user_id)
    if command.key == ("auth", "status"):
        actor = writer.actor()
        return f"Logged in to {repository.forge.url} as {actor['login']}\n", False
    if command.key == ("auth", "token"):
        writer.actor()
        if not token:
            raise Unsupported("no origin-bound Forgejo token is configured")
        return token + "\n", False
    if command.key == ("repo", "fork"):
        ref = writer.fork(repository, name=opts.get("fork_name"), organization=opts.get("org"))
        return ref.url + "\n", False
    if group == "label":
        writer.label(
            repository,
            command.args[0],
            color=opts.get("color"),
            description=opts.get("description"),
            edit=action == "edit",
            rename=opts.get("name"),
            force=opts.get("force", False),
        )
        return "", False
    pull = group == "pr"
    if action == "create":
        number = writer.create(
            repository,
            pull=pull,
            title=opts["title"],
            body=body(opts),
            base=opts.get("base"),
            head=opts.get("head"),
            labels=names(opts.get("label", [])),
            draft=opts.get("draft", False),
        )
        return (repository.pull_url(number) if pull else f"{repository.url}/issues/{number}") + "\n", False
    number = int(command.args[0])
    if action == "edit":
        changes = {key: opts[key] for key in ["title", "base"] if key in opts}
        if "body" in opts or "body_file" in opts:
            changes["body"] = body(opts)
        writer.edit(
            repository,
            number,
            pull=pull,
            changes=changes,
            add_labels=names(opts.get("add_label", [])),
            remove_labels=names(opts.get("remove_label", [])),
        )
    elif action == "comment":
        native_id = writer.comment(repository, number, body(opts), pull=pull)
        url = repository.pull_url(number) if pull else f"{repository.url}/issues/{number}"
        return f"{url}#issuecomment-{native_id}\n", False
    elif action in {"close", "reopen"}:
        writer.lifecycle(
            repository, number, pull=pull, state="closed" if action == "close" else "open", comment=opts.get("comment")
        )
    elif action == "review":
        event = "APPROVED" if opts.get("approve") else "REQUEST_CHANGES" if opts.get("request_changes") else "COMMENT"
        review = writer.review(repository, number, event=event, body=body(opts))
        return (
            f"Submitted {event} on {repository.pull_url(number)}; official={review.get('official', 'unknown')}, stale={review.get('stale', 'unknown')}\n",
            False,
        )
    else:
        raise Unsupported("command has no Forgejo implementation")
    return "", False


def render(value, json_output):
    return json.dumps(value, ensure_ascii=False) + "\n" if json_output else value


def exit_status(command, value):
    if command.key == ("pr", "checks"):
        return value.status
    return 0
