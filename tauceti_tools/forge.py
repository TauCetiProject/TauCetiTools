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

    Complete pagination succeeds as a unit or raises. No shared state service,
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
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method, path, data=None):
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
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    error = Incomplete if method == "GET" else AmbiguousWrite
                    raise error("Forgejo response exceeded the configured byte limit")
                if not raw:
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

    def collection(self, path):
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError("collection path must be relative to the configured Forgejo API")
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if any(k in {"page", "limit"} for k, _ in query):
            raise ValueError("collection owns page and limit parameters")
        rows = []
        for page in range(1, self.max_pages + 1):
            parameters = urllib.parse.urlencode(query + [("limit", "50"), ("page", str(page))])
            batch = self.request("GET", parsed.path + "?" + parameters)
            if not isinstance(batch, list):
                raise Incomplete("Forgejo collection response was not an array")
            rows.extend(batch)
            # Only an empty page proves completion: server-side limits may be below 50.
            if not batch:
                return rows
        raise Incomplete("Forgejo collection exceeded the configured page limit")

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

    def pulls(self, repository, *, state="open"):
        if state not in {"open", "closed", "all"}:
            raise ValueError("invalid pull request state")
        return self.collection(f"{self._repo(repository)}/pulls?state={state}")

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
