"""Small source boundary for the common review workflow.

The engine consumes the same metadata and native numeric comment IDs on both
backends. Qualified repository/principal identities remain separate from API
selectors; neither a same-number PR nor a same-login actor grants authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess

from .ci_candidate import RepositoryIdentity
from .forge import GITHUB, Forgejo, Incomplete, RepositoryRef
from .forge_read import ForgejoReader
from .git_credentials import scoped_git_environment


def validate_comments(rows):
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0
        and "body" in row and (row["body"] is None or isinstance(row["body"], str))
        and (row.get("in_reply_to_id") is None or type(row["in_reply_to_id"]) is int)
        and (row.get("user") is None or isinstance(row["user"], dict))
        for row in rows
    ):
        raise Incomplete("review discussion history is incomplete or malformed")
    return rows


def review_comments(api, repository, number, *, conversation=None):
    """Normalize native findings and replies while retaining reviewed-head identity."""
    comments = []
    for review in api.reviews(repository, number):
        head = review.get("commit_id")
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise Incomplete("native review commit identity unavailable")
        for row in validate_comments(api.review_comments(repository, number, review["id"])):
            comments.append(
                dict(row, pull_request_review_id=review["id"], line_commit_id=row.get("commit_id"), commit_id=head)
            )
    for row in validate_comments(api.comments(repository, number) if conversation is None else conversation):
        head = re.search(r"<!--tauceti-source-head:([0-9a-f]{40})-->", row.get("body", ""))
        reply = re.search(r"<!--tauceti-source-reply:root:([1-9][0-9]*)-->", row.get("body", ""))
        if head or reply:
            value = dict(row, thread_kind="issue-comment", path="")
            if head:
                value["commit_id"] = head[1]
            if reply:
                value["in_reply_to_id"] = int(reply[1])
            comments.append(value)
    roots = [row for row in comments if "<!--tauceti-rubric:" in row.get("body", "")]
    for row in comments:
        reply = re.search(r"<!--tauceti-source-reply:root:([1-9][0-9]*)-->", row.get("body", ""))
        if reply:
            row["in_reply_to_id"] = int(reply[1])
        if row in roots or row.get("in_reply_to_id") or row.get("thread_kind") == "issue-comment":
            continue
        parents = [
            root
            for root in roots
            if root["id"] < row["id"]
            and all(
                root.get(key) == row.get(key)
                for key in ("pull_request_review_id", "path", "position", "original_position")
            )
        ]
        if len(parents) == 1:
            row["in_reply_to_id"] = parents[0]["id"]
        elif parents:
            raise Incomplete("ambiguous native review reply parent")
    return comments


def reply_to_finding(api, repository, number, parent, body, *, comments=None):
    comments = review_comments(api, repository, number) if comments is None else comments
    root = next((row for row in comments if row["id"] == parent), None)
    if root is None or "<!--tauceti-rubric:" not in root.get("body", ""):
        raise Incomplete("native finding reply parent unavailable")
    body = f"<!--tauceti-source-reply:root:{parent}-->\n" + body
    prefix = "repos/" + repository.name
    if root.get("thread_kind") == "issue-comment":
        return api.request("POST", f"{prefix}/issues/{number}/comments", {"body": body})
    return api.request(
        "POST",
        f"{prefix}/pulls/{number}/reviews/{root['pull_request_review_id']}/comments",
        dict(
            body=body,
            path=root["path"],
            new_position=root.get("position", 0),
            old_position=root.get("original_position", 0),
        ),
    )


class GitHubReviewSource:
    """GitHub transport; review policy lives with the common engine."""

    def __init__(self, repository):
        self.repository = repository
        self.key = repository.name  # Preserve existing GitHub ledger/archive keys.

    claim_required = False

    def claim(self, number, head, owner, ttl, cache_base=None):
        from .claims import GitClaim, review_repository

        repository = review_repository(self.repository.name)
        if not repository:
            return None
        return GitClaim(repository, f"review/{self.repository.name.lower()}/{number}/{head}", owner, ttl)

    def publisher_key(self):
        return os.environ.get("GITHUB_ACTOR") or self.request("user")["login"]

    def request(self, endpoint, *, collection=False):
        result = subprocess.run(["gh", "api", *(["--paginate"] if collection else []), endpoint], capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise Incomplete("GitHub review source unavailable")
        pages = []
        decoder = json.JSONDecoder()
        raw = result.stdout
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            raise Incomplete("GitHub review source returned invalid text")
        rest = raw.lstrip()
        try:
            while rest:
                page, end = decoder.raw_decode(rest)
                pages.append(page)
                rest = rest[end:].lstrip()
        except ValueError:
            raise Incomplete("GitHub review source returned invalid JSON") from None
        if not pages:
            raise Incomplete("GitHub review source returned no response")
        if collection:
            if not all(isinstance(page, list) for page in pages):
                raise Incomplete("GitHub review collection returned malformed pages")
            return [row for page in pages for row in page]
        if len(pages) != 1 or not isinstance(pages[0], dict):
            raise Incomplete("GitHub review object returned malformed JSON")
        return pages[0]

    def pull(self, number):
        return self.request(f"repos/{self.repository.name}/pulls/{number}")

    def comments(self, number):
        return validate_comments(self.request(f"repos/{self.repository.name}/issues/{number}/comments", collection=True))

    def findings(self, number):
        return validate_comments(self.request(f"repos/{self.repository.name}/pulls/{number}/comments", collection=True))

    def merge_base(self, number, base, head):
        return self.request(f"repos/{self.repository.name}/compare/{base}...{head}")["merge_base_commit"]["sha"]

    def git_environment(self):
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        return scoped_git_environment(self.repository.forge, token) if token else None

    def projection(self, number, fields):
        result = subprocess.run(
            ["gh", "pr", "view", str(number), "--repo", self.repository.name, "--json", ",".join(fields)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode:
            raise Incomplete("GitHub review projection unavailable")
        return json.loads(result.stdout)

    def execution_metadata(self, number):
        return self.projection(number, ["headRefOid", "statusCheckRollup"])

    @property
    def cache_key(self):
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        return hashlib.sha256((self.repository.key + "\0" + token).encode()).hexdigest()


class ForgejoReviewSource:
    claim_required = True

    def __init__(self, identity, api, *, expected_actor=None):
        self.identity, self.repository, self.api = identity, identity.repository, api
        self.key = self.repository.key
        row = api.repository(self.repository)
        if (
            row.get("id") != identity.native_id
            or row.get("full_name", "").casefold() != self.repository.name.casefold()
        ):
            raise Incomplete("review source repository identity changed")
        self.actor = api.current_user()
        if expected_actor is not None and str(self.actor.get("id")) != str(expected_actor):
            raise Incomplete("review source actor changed")

    def claim(self, number, head, owner, ttl, cache_base=None):
        from .claims import ForgejoGitClaim

        return ForgejoGitClaim(
            self.identity,
            f"review/{number}/{head}",
            owner,
            ttl,
            source=self.identity,
            client=self.api,
            token=self.api._token,
            credential_origin=self.repository.forge,
            cache_base=cache_base,
        )

    def publisher_key(self):
        return self.repository.forge.key + "/users/" + str(self.actor["id"])

    def pull(self, number):
        return self.api.pull(self.repository, number)

    def comments(self, number):
        return validate_comments(self.api.comments(self.repository, number))

    def findings(self, number):
        return review_comments(self.api, self.repository, number)

    def merge_base(self, number, base, head):
        row = self.pull(number)
        if row["base"]["sha"] != base or row["head"]["sha"] != head:
            raise Incomplete("source comparison changed before preparation")
        value = row.get("merge_base")
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            raise Incomplete("native merge base unavailable")
        return value

    def git_environment(self):
        return scoped_git_environment(self.repository.forge, self.api._token)

    def projection(self, number, fields):
        return ForgejoReader(self.api).pull(self.repository, number, fields)

    def execution_metadata(self, number):
        return {"headRefOid": self.pull(number)["head"]["sha"], "statusCheckRollup": []}

    @property
    def cache_key(self):
        identity = json.dumps(self.identity.record(), sort_keys=True)
        return hashlib.sha256((identity + "\0" + self.api._token).encode()).hexdigest()


def source_for(repository, environment=None):
    """Select the transport once at the source boundary, never by PR number alone."""
    env = os.environ if environment is None else environment
    binding = env.get("TAUCETI_SOURCE_BINDING")
    if not binding:
        return GitHubReviewSource(RepositoryRef(GITHUB, repository))
    identity = RepositoryIdentity.parse(json.loads(binding))
    if repository not in {identity.repository.key, identity.repository.name, identity.repository.url}:
        raise ValueError("review repository differs from its source binding")
    if env.get("TAUCETI_FORGEJO_ORIGIN") != identity.repository.forge.url:
        raise ValueError("review credential origin differs from its source")
    fd = os.open(env["TAUCETI_FORGEJO_TOKEN_FILE"], os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("review credential file must be private and owned")
        token = stream.read().strip()
    return ForgejoReviewSource(
        identity, Forgejo(identity.repository.forge, token=token), expected_actor=env.get("TAUCETI_FORGEJO_USER_ID")
    )
