"""Host-qualified forge identities and a bounded direct Forgejo REST client.

This module has no worker dependency and never selects credentials implicitly.
GitHub callers continue using their existing implementation.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


class ForgeError(RuntimeError):
    def __init__(self, message, *, status=0):
        super().__init__(message)
        self.status = status


class Unsupported(ForgeError):
    pass


class Incomplete(ForgeError):
    pass


class AmbiguousWrite(ForgeError):
    """The server may have accepted a mutation; reconcile before retrying."""


@dataclass(frozen=True)
class Forge:
    kind: str
    url: str

    def __post_init__(self):
        parsed = urllib.parse.urlsplit(self.url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            self.kind not in {"github", "forgejo"}
            or not parsed.hostname
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and local))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(c.isspace() for c in self.url)
        ):
            raise ValueError("forge must be github or forgejo with an HTTPS origin (HTTP allowed on loopback)")
        # Accessing port also validates its syntax/range.
        port = parsed.port
        host = parsed.hostname.lower()
        host = f"[{host}]" if ":" in host else host
        authority = host + (f":{port}" if port and port != (443 if parsed.scheme == "https" else 80) else "")
        object.__setattr__(self, "url", f"{parsed.scheme}://{authority}")

    @property
    def key(self):
        return self.kind + ":" + self.url

    @property
    def api_url(self):
        if self.kind == "forgejo":
            return self.url + "/api/v1"
        return "https://api.github.com" if self.url == "https://github.com" else self.url + "/api/v3"


GITHUB = Forge("github", "https://github.com")


def resolve_repository(value, connections=None):
    """Resolve a v1 GitHub name or v2 {forge, name} reference without ambient defaults."""
    if isinstance(value, str):
        return RepositoryRef(GITHUB, value)
    if not isinstance(value, dict) or set(value) != {"forge", "name"}:
        raise ValueError("repository reference requires exactly forge and name")
    name = value["forge"]
    if not isinstance(name, str) or name not in (connections or {}):
        raise ValueError("repository references an unknown forge connection")
    spec = connections[name]
    if not isinstance(spec, dict) or set(spec) != {"kind", "url"}:
        raise ValueError("forge connection requires exactly kind and url")
    return RepositoryRef(Forge(spec["kind"], spec["url"]), value["name"])


@dataclass(frozen=True)
class RepositoryRef:
    forge: Forge
    name: str

    def __post_init__(self):
        if (
            not isinstance(self.name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.name)
            or any(p in {".", ".."} for p in self.name.split("/"))
            or self.name.endswith(".git")
        ):
            raise ValueError("repository must be owner/name without a URL or .git suffix")

    @property
    def key(self):
        return self.forge.key + "/" + self.name.lower()

    @property
    def url(self):
        return self.forge.url + "/" + self.name

    @property
    def clone_url(self):
        return self.url + ".git"

    def pull_url(self, number):
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("pull request number must be a positive integer")
        return f"{self.url}/{'pulls' if self.forge.kind == 'forgejo' else 'pull'}/{number}"

    def source_url(self, revision, path):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("source links require an immutable commit SHA")
        if path.startswith("/") or any(p in {".", ".."} for p in path.split("/")):
            raise ValueError("source path must stay inside the repository")
        prefix = "src/commit" if self.forge.kind == "forgejo" else "blob"
        return f"{self.url}/{prefix}/{revision}/{urllib.parse.quote(path, safe='/')}"


@dataclass(frozen=True)
class PrincipalRef:
    forge: Forge
    native_id: str

    def __post_init__(self):
        if not isinstance(self.native_id, str) or not self.native_id:
            raise ValueError("principal requires a nonempty native ID")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Forgejo:
    """Direct reads and single-attempt mutations; never follows credential-bearing redirects.

    Page failures raise rather than returning a partial collection. Page-number
    listings are observations, not atomic snapshots of a changing repository.
    No shared state service,
    implicit GitHub fallback, or mutation retry is involved.
    """

    def __init__(self, forge, *, token=None, timeout=30, max_pages=1000, max_bytes=16 * 1024 * 1024):
        if forge.kind != "forgejo":
            raise ValueError("Forgejo client requires a Forgejo origin")
        if timeout <= 0 or max_pages < 1 or max_bytes < 1:
            raise ValueError("request bounds must be positive")
        if token is not None and (
            not isinstance(token, str) or not token or not all(33 <= ord(c) <= 126 for c in token)
        ):
            raise ValueError("Forgejo token must be nonempty printable ASCII without whitespace")
        self.forge, self.timeout, self.max_pages, self.max_bytes = forge, timeout, max_pages, max_bytes
        self._token = token
        if urllib.parse.urlsplit(forge.url).hostname in {"localhost", "127.0.0.1", "::1"}:
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        else:
            self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method, path, data=None, *, response_headers=None, response_meta=None):
        parsed = urllib.parse.urlsplit(path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or "\\" in path
            or any(part in {".", ".."} for part in urllib.parse.unquote(parsed.path).split("/"))
        ):
            raise ValueError("API path must be relative to the configured Forgejo API")
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise Unsupported(f"unsupported HTTP method {method}")
        headers = {"Accept": "application/json", "User-Agent": "tauceti-tools"}
        if self._token:
            headers["Authorization"] = "token " + self._token
        body = None if data is None else json.dumps(data).encode()
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.forge.api_url + "/" + path.lstrip("/"), body, headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                if response_meta is not None:
                    response_meta.update(
                        status=response.status, reason=response.reason, version=getattr(response, "version", 11)
                    )
                if response_headers is not None:
                    response_headers.update({key.lower(): value for key, value in response.headers.items()})
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    error = Incomplete if method == "GET" else AmbiguousWrite
                    raise error("Forgejo response exceeded the configured byte limit")
                if not raw:
                    if method == "GET":
                        raise Incomplete("Forgejo returned an empty read response")
                    return None
                try:
                    return json.loads(raw)
                except (ValueError, UnicodeError):
                    error = Incomplete if method == "GET" else AmbiguousWrite
                    raise error("Forgejo returned an invalid JSON response") from None
        except urllib.error.HTTPError as exc:
            # Neither response bodies nor credential-bearing request URLs enter errors.
            error = AmbiguousWrite if method != "GET" and exc.code >= 500 else ForgeError
            exc.close()
            raise error(f"Forgejo {method} returned HTTP {exc.code}", status=exc.code) from None
        except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
            error = ForgeError if method == "GET" else AmbiguousWrite
            raise error(f"Forgejo {method} transport failed") from None

    def pages(self, path, *, empty_null=False, counted=False, expected_headers=None):
        """Yield bounded pages, without claiming the caller consumed a complete collection.

        A consumer may stop after its requested prefix. Consumers requiring the
        entire history must exhaust the iterator; hitting the page bound raises.
        """
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError("collection path must be relative to the configured Forgejo API")
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if any(k in {"page", "limit"} for k, _ in query):
            raise ValueError("collection owns page and limit parameters")
        page_size = 50
        expected_total = None
        if counted:
            settings = self.request("GET", "settings/api")
            cap = settings.get("max_response_items") if isinstance(settings, dict) else None
            if type(cap) is not int or cap < 1:
                raise Incomplete("Forgejo pagination settings are unavailable")
            page_size = min(page_size, cap)
        for page in range(1, self.max_pages + 1):
            parameters = urllib.parse.urlencode(query + [("limit", str(page_size)), ("page", str(page))])
            headers = {}
            batch = self.request("GET", parsed.path + "?" + parameters, response_headers=headers)
            if expected_headers and any(headers.get(key.lower()) != value for key, value in expected_headers.items()):
                raise Incomplete("Forgejo collection did not bind the requested comparison commits")
            if batch is None and empty_null:
                return  # Explicit native nil slice; only endpoints known to use it.
            if not isinstance(batch, list):
                raise Incomplete("Forgejo collection response was not an array")
            if counted:
                try:
                    total = int(headers["x-total-count"])
                    if total < 0:
                        raise ValueError
                except (KeyError, ValueError):
                    raise Incomplete("Forgejo collection count is unavailable") from None
                if expected_total is not None and total != expected_total:
                    raise Incomplete("Forgejo collection count changed during pagination")
                expected_total = total
                yield batch
                if page * page_size >= total:
                    return
                continue
            # Only an empty page proves completion: server-side limits may be below 50.
            if not batch:
                return
            yield batch
        raise Incomplete("Forgejo collection exceeded the configured page limit")

    def collection(self, path, *, empty_null=False, counted=False, expected_headers=None):
        return [
            row
            for batch in self.pages(path, empty_null=empty_null, counted=counted, expected_headers=expected_headers)
            for row in batch
        ]

    def _numbered(self, path, *, limit=None, predicate=None):
        """Newest distinct issues/PRs, filtered before applying an optional limit.

        Native listings must use creation-descending order. De-duplication handles
        overlap when concurrent inserts shift page boundaries, not snapshot isolation.
        """
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("limit must be a positive integer")
        rows, seen = [], set()
        for batch in self.pages(path):
            for row in batch:
                if not isinstance(row, dict) or type(row.get("number")) is not int or row["number"] < 1:
                    raise Incomplete("Forgejo listing did not identify an issue or PR")
                number = row["number"]
                if number in seen:
                    continue
                seen.add(number)
                if predicate is None or predicate(row):
                    rows.append(row)
                    if limit is not None and len(rows) == limit:
                        return rows
        return rows

    def _listing(self, repository, path, *, author=None, **options):
        try:
            return self._numbered(path, **options)
        except ForgeError as error:
            if error.status not in {400, 404} or author is None:
                raise
            # Do not turn an inaccessible repository into an empty result.
            self.repository(repository)
            try:
                self.request("GET", "users/" + urllib.parse.quote(author, safe=""))
            except ForgeError as user_error:
                if user_error.status == 404:
                    return []
                raise
            raise error

    def _repo(self, repository):
        if repository.forge != self.forge:
            raise ValueError("repository belongs to a different forge")
        return "repos/" + repository.name

    def pull(self, repository, number):
        repository.pull_url(number)  # validate before constructing an API path
        row = self.request("GET", f"{self._repo(repository)}/pulls/{number}")
        if not isinstance(row, dict) or row.get("number") != number:
            raise Incomplete("Forgejo pull response did not identify the requested PR")
        return row

    def pull_ref(self, repository, number):
        repository.pull_url(number)
        ref = f"refs/pull/{number}/head"
        rows = self.request("GET", f"{self._repo(repository)}/git/refs/pull/{number}/head")
        matches = (
            [row for row in rows if isinstance(row, dict) and row.get("ref") == ref] if isinstance(rows, list) else []
        )
        obj = matches[0].get("object") if len(matches) == 1 else None
        sha = obj.get("sha") if isinstance(obj, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise Incomplete("Forgejo did not identify the retained pull head")
        return sha

    def pulls(self, repository, *, state="open", limit=None, author=None, base=None, head=None, labels=()):
        if state not in {"open", "closed", "merged", "all"}:
            raise ValueError("invalid pull request state")
        query = {"state": "closed" if state == "merged" else state}
        for key, value in [("poster", author), ("base", base), ("head", head)]:
            if value is not None:
                query[key] = value

        def matches(row):
            if state in {"closed", "merged"}:
                if not isinstance(row.get("merged"), bool):
                    raise Incomplete("Forgejo listing did not identify merge state")
                if row["merged"] != (state == "merged"):
                    return False
            for side, name in [("base", base), ("head", head)]:
                if name is not None:
                    branch = row.get(side)
                    if not isinstance(branch, dict) or not isinstance(branch.get("label"), str):
                        raise Incomplete("Forgejo listing did not identify its branch name")
                    if branch["label"] != name:
                        return False
            return self._labels_match(row, labels) and self._author_match(row, author)

        # Omitting sort uses Forgejo 17's creation-descending default. 'oldest'
        # cannot serve a bounded newest-first prefix. Preserve server creation
        # order: imported issues can have timestamps out of index order.
        return self._listing(
            repository,
            f"{self._repo(repository)}/pulls?{urllib.parse.urlencode(query)}",
            author=author,
            limit=limit,
            predicate=matches,
        )

    @staticmethod
    def _labels_match(row, labels):
        if not labels:
            return True
        if "labels" not in row:
            raise Incomplete("Forgejo listing did not identify labels")
        values = row["labels"] if row["labels"] is not None else []
        if not isinstance(values, list) or any(
            not isinstance(v, dict) or not isinstance(v.get("name"), str) for v in values
        ):
            raise Incomplete("Forgejo listing did not identify labels")
        # Native issue label filtering is OR and ignores missing labels; gh's
        # repeated --label flags require AND. Apply the requested contract here.
        return {label.casefold() for label in labels} <= {v["name"].casefold() for v in values}

    @staticmethod
    def _author_match(row, author):
        if author is None:
            return True
        user = row.get("user")
        if not isinstance(user, dict) or not isinstance(user.get("login"), str):
            raise Incomplete("Forgejo listing did not identify its author")
        return user["login"].casefold() == author.casefold()

    def issues(self, repository, *, state="open", limit=None, author=None, labels=()):
        if state not in {"open", "closed", "all"}:
            raise ValueError("invalid issue state")
        query = {"state": state, "type": "issues", "sort": "latest"}
        if author is not None:
            query["created_by"] = author
        return self._listing(
            repository,
            f"{self._repo(repository)}/issues?{urllib.parse.urlencode(query)}",
            author=author,
            limit=limit,
            predicate=lambda row: (
                row.get("pull_request") is None and self._labels_match(row, labels) and self._author_match(row, author)
            ),
        )

    def issue(self, repository, number):
        repository.pull_url(number)
        row = self.request("GET", f"{self._repo(repository)}/issues/{number}")
        if not isinstance(row, dict) or row.get("number") != number or row.get("pull_request") is not None:
            raise Incomplete("Forgejo response did not identify the requested issue")
        return row

    def repository(self, repository):
        row = self.request("GET", self._repo(repository))
        if not isinstance(row, dict) or str(row.get("full_name", "")).lower() != repository.name.lower():
            raise Incomplete("Forgejo response did not identify the requested repository")
        return row

    def current_user(self):
        row = self.request("GET", "user")
        if (
            not isinstance(row, dict)
            or type(row.get("id")) is not int
            or row["id"] < 1
            or not isinstance(row.get("login"), str)
            or not row["login"]
        ):
            raise Incomplete("Forgejo response did not identify the authenticated account")
        return row

    def reviews(self, repository, number):
        repository.pull_url(number)
        # Visibility filtering happens after native pagination: an empty page
        # can hide only other users' drafts while later submitted reviews exist.
        rows, size = [], 0
        for batch in self.pages(f"{self._repo(repository)}/pulls/{number}/reviews", counted=True):
            size += len(json.dumps(batch, ensure_ascii=False).encode("utf-8"))
            if size > self.max_bytes:
                raise Incomplete("Forgejo reviews exceeded the configured aggregate byte limit")
            rows.extend(batch)
        return rows

    def review_comments(self, repository, number, review_id):
        repository.pull_url(number)
        repository.pull_url(review_id)
        # Like issue comments, this endpoint is unpaginated in Forgejo 17.
        rows = self.request("GET", f"{self._repo(repository)}/pulls/{number}/reviews/{review_id}/comments")
        if not isinstance(rows, list):
            raise Incomplete("Forgejo review comments response was not an array")
        return rows

    def files(self, repository, number, *, comparison_base=None, revision=None):
        repository.pull_url(number)
        expected = None
        if comparison_base is not None or revision is not None:
            if any(
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value)
                for value in [comparison_base, revision]
            ):
                raise Incomplete("file comparison commit identities are unavailable")
            expected = {
                "x-forgejo-diff-base": comparison_base,
                "x-forgejo-diff-head": revision,
                "x-forgejo-diff-format": "1",
            }
        # Forgejo 17 serializes apiFiles' nil slice as JSON null beyond the end.
        return self.collection(
            f"{self._repo(repository)}/pulls/{number}/files", empty_null=True, expected_headers=expected
        )

    def statuses(self, repository, revision):
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
            raise ValueError("commit statuses require an immutable object ID")
        return self.collection(f"{self._repo(repository)}/commits/{revision}/statuses?sort=highestindex")

    def comments(self, repository, number):
        repository.pull_url(number)
        # Forgejo 17's issue-comment endpoint is unpaginated (only since/before
        # are supported). Sending page=N repeats the same history indefinitely.
        rows = self.request("GET", f"{self._repo(repository)}/issues/{number}/comments")
        if not isinstance(rows, list):
            raise Incomplete("Forgejo comments response was not an array")
        return rows

    def post_comment(self, repository, number, body):
        repository.pull_url(number)
        return self.request("POST", f"{self._repo(repository)}/issues/{number}/comments", {"body": body})

    def edit_comment(self, repository, comment_id, body):
        repository.pull_url(comment_id)  # identical positive-ID validation
        # The generic Forgejo issue-comment edit route also edits code comments;
        # native server permissions and ContentVersion handling remain authoritative.
        return self.request("PATCH", f"{self._repo(repository)}/issues/comments/{comment_id}", {"body": body})
