"""Explicit REST read contracts for gh api on a selected Forgejo origin.

Native numeric IDs are local to that origin. Added node_id values are opaque,
origin-qualified identities. Unsupported endpoints, methods and query options
are rejected before credentials are opened; this is not a raw HTTP tunnel.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import urllib.parse
from dataclasses import dataclass

from .forge import ForgeError, Incomplete, RepositoryRef, Unsupported


@dataclass(frozen=True)
class ApiPlan:
    path: str
    kind: str
    repository_name: str | None
    parameters: tuple[tuple[str, str], ...]
    options: dict
    raw: bool = False


def plan(command, *, context=None):
    opts = command.options
    method = opts.get("method", "POST" if opts.get("field") or opts.get("raw_field") or "input" in opts else "GET")
    if method.upper() != "GET" or "input" in opts:
        raise Unsupported("this Forgejo gh api contract supports explicit GET reads; API writes require an adapter")
    endpoint = command.args[0]
    if context is not None:
        owner, name = context.name.split("/")
        endpoint = endpoint.replace("{owner}", owner).replace("{repo}", name)
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme or parsed.netloc or parsed.fragment or "\\" in endpoint or any(c.isspace() for c in parsed.path):
        raise Unsupported("API endpoint must be a relative path on the selected origin")
    path = parsed.path.lstrip("/")
    if "{" in endpoint or "}" in endpoint:
        raise Unsupported("unresolved API placeholders; provide an explicit branch/ref")
    parts = urllib.parse.unquote(path).split("/")
    if path.endswith("/") and "/git/matching-refs/" in path:
        parts = parts[:-1]
    if any(part in {"", ".", ".."} or any(ord(c) < 32 for c in part) for part in parts):
        raise Unsupported("invalid API endpoint path")
    parameters = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    for typed, values in [(False, opts.get("raw_field", [])), (True, opts.get("field", []))]:
        for value in values:
            key, sep, value = value.partition("=")
            if not sep or not key or "[" in key or (typed and value.startswith("@")):
                raise Unsupported("GET fields require simple key=value parameters; file/array fields need an adapter")
            if typed and ("{" in value or "}" in value):
                raise Unsupported("typed field placeholders require a dedicated context adapter")
            if typed and key == "ref" and value in {"null", "true", "false"}:
                raise Unsupported("ref requires a string; use --raw-field for literal null/true/false branch names")
            parameters.append((key, value))
    if len({key for key, _ in parameters}) != len(parameters):
        raise Unsupported("duplicate API query parameters are unsupported")
    raw = False
    accept = None
    for header in opts.get("header", []):
        key, sep, value = header.partition(":")
        value = value.strip()
        if (
            not sep
            or key.casefold() != "accept"
            or value
            not in {
                "application/json",
                "application/vnd.github+json",
                "application/vnd.github.v3+json",
                "application/vnd.github.raw",
                "application/vnd.github.raw+json",
                "application/vnd.github.v3.raw+json",
            }
        ):
            raise Unsupported("unsupported API header; credentials and destination headers cannot be overridden")
        if accept is not None and accept != value:
            raise Unsupported("conflicting Accept headers")
        accept, raw = value, ".raw" in value
    repository_name, kind, allowed = None, None, set()
    if path == "user" or re.fullmatch(r"users/[^/]+", path):
        kind = "user"
    match = re.fullmatch(r"repos/([^/]+/[^/]+)(?:/(.*))?", path)
    if match:
        repository_name, suffix = match.group(1), match.group(2) or ""
        # Validate the name without requiring a selected server yet.
        if any(c in repository_name for c in "%{}"):
            raise Unsupported("repository placeholders must resolve to a configured context")
        if not suffix:
            kind = "repository"
        elif re.fullmatch(r"collaborators/[^/]+/permission", suffix):
            kind = "permission"
        elif suffix == "collaborators":
            kind, allowed = "collaborators", {"page", "per_page"}
        elif suffix == "labels":
            kind, allowed = "labels", {"page", "per_page"}
        elif suffix.startswith("git/matching-refs/"):
            kind, allowed = "matching_refs", {"page", "per_page"}
        elif suffix.startswith(("git/ref/", "git/refs/")):
            kind = "ref"
        elif suffix.startswith("contents/"):
            kind, allowed = "contents", {"ref"}
        elif re.fullmatch(r"git/trees/[^/]+", suffix):
            kind, allowed = "tree", {"recursive"}
        elif re.fullmatch(r"git/commits/[0-9a-f]{40}|git/commits/[0-9a-f]{64}", suffix):
            kind = "git_commit"
        elif re.fullmatch(r"commits/[^/]+", suffix):
            kind = "repo_commit"
        elif re.fullmatch(r"git/blobs/[^/]+", suffix):
            kind = "blob"
    if kind is None:
        raise Unsupported("unsupported Forgejo gh api endpoint; no delegation or request was attempted")
    if set(dict(parameters)) - allowed:
        raise Unsupported("unsupported query parameters for this API read contract")
    for key, value in parameters:
        if key in {"page", "per_page"} and (not value.isascii() or not value.isdigit() or int(value) < 1):
            raise Unsupported("API page sizes and numbers must be positive ASCII integers")
        if key == "per_page" and int(value) > 100:
            raise Unsupported("per_page must be at most 100")
    if opts.get("paginate") and kind not in {"labels", "collaborators", "matching_refs"}:
        raise Unsupported("pagination is supported only for collection read contracts")
    if opts.get("include") and kind in {"labels", "collaborators", "matching_refs"}:
        raise Unsupported("collection response headers require a paginated HTTP adapter")
    if raw and opts.get("jq"):
        raise Unsupported("jq cannot be applied to a raw file/blob response")
    if raw and kind not in {"contents", "blob"}:
        raise Unsupported("raw Accept is supported only for file/blob contents")
    path = urllib.parse.quote(path, safe="/%:@!$&'()*+,;=-._~")
    return ApiPlan(path, kind, repository_name, tuple(parameters), opts, raw)


def _nodes(value, forge, kind):
    if isinstance(value, list):
        return [_nodes(item, forge, kind) for item in value]
    if not isinstance(value, dict):
        return value
    result = dict(value)
    native_id = result.get("id")
    if type(native_id) is int:
        result["node_id"] = f"{forge.key}/{kind}/{native_id}" if native_id > 0 else None
    for key in ["owner", "user", "author", "committer"]:
        if isinstance(result.get(key), dict) and "login" in result[key]:
            result[key] = _nodes(result[key], forge, "users")
    if isinstance(result.get("parent"), dict):
        result["parent"] = _nodes(result["parent"], forge, "repositories")
    return result


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value)


def _content(value):
    if not isinstance(value, dict) or value.get("encoding") != "base64" or not isinstance(value.get("content"), str):
        raise Incomplete("Forgejo file/blob content is unavailable")
    try:
        content = base64.b64decode(value["content"], validate=True)
    except ValueError:
        raise Incomplete("Forgejo file/blob content is not base64") from None
    sha = value.get("sha")
    if type(value.get("size")) is not int or value["size"] != len(content) or not _sha(sha):
        raise Incomplete("Forgejo content size or Git blob identity is unavailable")
    digest = hashlib.new("sha1" if len(sha) == 40 else "sha256")
    digest.update(f"blob {len(content)}\0".encode() + content)
    if digest.hexdigest() != sha:
        raise Incomplete("Forgejo content does not match its Git blob identity")
    return content


def _git_commit(value):
    if not isinstance(value, dict) or not _sha(value.get("sha")) or not isinstance(value.get("commit"), dict):
        raise Incomplete("Forgejo commit metadata is unavailable")
    commit = value["commit"]
    if (
        not isinstance(commit.get("tree"), dict)
        or not _sha(commit["tree"].get("sha"))
        or not isinstance(commit.get("message"), str)
    ):
        raise Incomplete("Forgejo commit tree/message is unavailable")
    for key in ["author", "committer"]:
        if not isinstance(commit.get(key), dict) or any(
            not isinstance(commit[key].get(field), str) for field in ["name", "email", "date"]
        ):
            raise Incomplete("Forgejo commit author metadata is unavailable")
    if not isinstance(value.get("parents"), list) or any(
        not isinstance(parent, dict) or not _sha(parent.get("sha")) for parent in value["parents"]
    ):
        raise Incomplete("Forgejo commit parents are unavailable")
    return {
        "sha": value["sha"],
        "url": value.get("url"),
        **{key: commit[key] for key in ["message", "tree", "author", "committer"]},
        "parents": value["parents"],
        **({"verification": commit["verification"]} if "verification" in commit else {}),
    }


def _resolve_tree(api, path):
    base, selector = path.rsplit("/git/trees/", 1)
    try:
        commit = api.request("GET", base + "/git/commits/" + selector + "?stat=false&files=false")
    except ForgeError as error:
        if error.status != 404:
            raise
        # A tree object is not a commit. Verify its own object hash before
        # using it as the immutable selector for the requested traversal.
        tree = _tree(api, path, [], {}, {})
        entries = sorted(
            tree["tree"], key=lambda entry: entry["path"].encode() + (b"/" if entry["type"] == "tree" else b"\0")
        )
        if any("/" in entry["path"] or "\0" in entry["path"] for entry in entries):
            raise Incomplete("Forgejo root tree entries are invalid") from None
        body = b"".join(
            (entry["mode"].lstrip("0") or "0").encode()
            + b" "
            + entry["path"].encode()
            + b"\0"
            + bytes.fromhex(entry["sha"])
            for entry in entries
        )
        sha = tree["sha"]
        digest = hashlib.new("sha1" if len(sha) == 40 else "sha256", f"tree {len(body)}\0".encode() + body).hexdigest()
        if digest != sha:
            raise Incomplete("Forgejo root tree does not match its immutable identity") from None
        return base + "/git/trees/" + digest
    commit = _git_commit(commit)
    return base + "/git/trees/" + commit["tree"]["sha"]


def _collection(api, path, *, stop_after=None):
    rows, size = [], 0
    for batch in api.pages(path):
        size += len(json.dumps(batch, ensure_ascii=False).encode("utf-8"))
        if size > api.max_bytes:
            raise Incomplete("API collection exceeds the configured aggregate byte bound")
        rows.extend(batch)
        if stop_after is not None and len(rows) >= stop_after:
            return rows[:stop_after]
    return rows


def _tree(api, path, parameters, headers, meta):
    entries, seen, first, size = [], set(), None, 0
    for page in range(1, api.max_pages + 1):
        query = urllib.parse.urlencode([*parameters, ("page", str(page)), ("per_page", "1000")])
        current = api.request("GET", path + "?" + query, response_headers=headers, response_meta=meta)
        if isinstance(current, dict) and current.get("total_count") == 0 and current.get("tree") is None:
            current = {**current, "tree": []}
        if (
            not isinstance(current, dict)
            or type(current.get("total_count")) is not int
            or current["total_count"] < 0
            or not isinstance(current.get("tree"), list)
            or current.get("page") != page
        ):
            raise Incomplete("Forgejo tree page completeness is unavailable")
        if not _sha(current.get("sha")) or current["sha"] != path.rsplit("/", 1)[1]:
            raise Incomplete("Forgejo tree did not identify the requested immutable tree")
        if first is None:
            first = current
        elif any(current.get(key) != first.get(key) for key in ["sha", "total_count"]):
            raise Incomplete("Forgejo tree identity changed during pagination")
        batch = current["tree"]
        size += len(json.dumps(batch, ensure_ascii=False).encode("utf-8"))
        if size > api.max_bytes:
            raise Incomplete("Forgejo tree exceeds the configured aggregate byte bound")
        for entry in batch:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or entry["path"] in seen:
                raise Incomplete("Forgejo tree page repeated or omitted an entry identity")
            if (
                not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(entry.get("sha", "")))
                or entry.get("type") not in {"blob", "tree", "commit"}
                or not re.fullmatch(r"[0-7]{6}", str(entry.get("mode", "")))
            ):
                raise Incomplete("Forgejo tree entry object metadata is unavailable")
            seen.add(entry["path"])
            entries.append(entry)
        if len(entries) == first["total_count"]:
            return {**first, "tree": entries, "truncated": False}
        if not batch or len(entries) > first["total_count"]:
            raise Incomplete("Forgejo tree pages did not account for every entry")
    raise Incomplete("Forgejo tree exceeds the configured page bound")


def execute(request, api):
    """Return rendered gh output and exit status; all I/O stays on api.forge."""
    opts, query = request.options, dict(request.parameters)
    path, kind = request.path, request.kind
    headers, meta = {}, {}
    if request.repository_name:
        RepositoryRef(api.forge, request.repository_name)
    if kind in {"labels", "collaborators"}:
        target = int(query.get("page", "1")) * int(query.get("per_page", "30"))
        rows = _collection(api, path, stop_after=None if opts.get("paginate") else target)
    elif kind in {"matching_refs", "ref"}:
        native_path = path.replace("/git/matching-refs/", "/git/refs/", 1).replace("/git/ref/", "/git/refs/", 1)
        try:
            rows = api.request("GET", native_path, response_headers=headers, response_meta=meta)
        except ForgeError as error:
            if error.status != 404 or kind != "matching_refs":
                raise
            repository = api.repository(RepositoryRef(api.forge, request.repository_name))
            if not isinstance(repository.get("permissions"), dict) or repository["permissions"].get("pull") is not True:
                raise Incomplete("Forgejo code-read permission could not establish an empty ref namespace") from None
            rows = []
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise Incomplete("Forgejo refs were not an array")
        prefix = urllib.parse.unquote(native_path.split("/git/refs/", 1)[1])
        if any(
            not isinstance(row, dict)
            or not isinstance(row.get("ref"), str)
            or not row["ref"].startswith("refs/" + prefix)
            or not isinstance(row.get("object"), dict)
            or not _sha(row["object"].get("sha"))
            for row in rows
        ):
            raise Incomplete("Forgejo returned unrelated or incomplete ref metadata")
        if kind == "ref":
            rows = [row for row in rows if row["ref"] == "refs/" + prefix]
            if not rows:
                raise ForgeError("Forgejo requested ref returned HTTP 404", status=404)
            if len(rows) != 1:
                raise Incomplete("Forgejo did not identify exactly the requested ref")
            value = rows[0]
    elif kind == "tree":
        parameters = [(k, "1") for k, _ in request.parameters]
        native_path = _resolve_tree(api, path)
        value = _tree(api, native_path, parameters, headers, meta)
    else:
        native_path = path
        if kind == "repo_commit":
            native_path = path.replace("/commits/", "/git/commits/", 1)
        if kind == "git_commit":
            native_path += "?stat=false&files=false"
        elif request.parameters:
            native_path += "?" + urllib.parse.urlencode(request.parameters)
        value = api.request("GET", native_path, response_headers=headers, response_meta=meta)
        if kind == "git_commit":
            value = _git_commit(value)
        if kind == "blob" or (kind == "contents" and isinstance(value, dict) and value.get("type") == "file"):
            _content(value)
    collection = kind in {"labels", "collaborators", "matching_refs"}
    if collection:
        if not isinstance(rows, list):
            raise Incomplete("Forgejo collection was not an array")
        per_page, page = int(query.get("per_page", "30")), int(query.get("page", "1"))
        if len(rows) > api.max_pages * per_page:
            raise Incomplete("API collection exceeds the configured page bound")
        start = (page - 1) * per_page
        pages = [rows[start : start + per_page]]
        if opts.get("paginate"):
            pages = [rows[i : i + per_page] for i in range(start, len(rows), per_page)] or [[]]
    else:
        if not isinstance(value, dict):
            raise Incomplete("Forgejo API read was not an object")
        if kind == "tree" and value.get("truncated") is not False:
            raise Incomplete("Forgejo tree completeness is unavailable")
        if kind == "user" and (
            not isinstance(value.get("login"), str) or type(value.get("id")) is not int or value["id"] < 1
        ):
            raise Incomplete("Forgejo user identity is unavailable")
        if kind == "repository" and str(value.get("full_name", "")).casefold() != request.repository_name.casefold():
            raise Incomplete("Forgejo response did not identify the repository")
        if (
            kind == "user"
            and path.startswith("users/")
            and value["login"].casefold() != urllib.parse.unquote(path[6:]).casefold()
        ):
            raise Incomplete("Forgejo response did not identify the requested user")
        if kind == "permission":
            user = value.get("user")
            requested_login = urllib.parse.unquote(path.rsplit("/", 2)[1])
            if (
                value.get("permission") not in {"owner", "admin", "write", "read", "none"}
                or not isinstance(user, dict)
                or str(user.get("login", "")).casefold() != requested_login.casefold()
                or type(user.get("id")) is not int
                or user["id"] < 1
            ):
                raise Incomplete("Forgejo collaborator permission identity is unavailable")
            if value["permission"] == "owner":
                value = {**value, "permission": "admin"}
        if kind == "repository" and (type(value.get("id")) is not int or value["id"] < 1):
            raise Incomplete("Forgejo repository identity is unavailable")
        if kind in {"git_commit", "repo_commit", "blob", "tree"} and not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", str(value.get("sha", ""))
        ):
            raise Incomplete("Forgejo Git object identity is unavailable")
        pages = [value]
    if kind in {"ref", "matching_refs"}:
        for ref in [value] if kind == "ref" else rows:
            obj = ref.get("object")
            if not isinstance(obj, dict) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(obj.get("sha", ""))):
                raise Incomplete("Forgejo ref object identity is unavailable")
    if kind in {"labels", "collaborators"}:
        for batch in pages:
            for row in batch:
                if (
                    not isinstance(row, dict)
                    or type(row.get("id")) is not int
                    or row["id"] < 1
                    or not isinstance(row.get("name" if kind == "labels" else "login"), str)
                ):
                    raise Incomplete("Forgejo collection entry identity is unavailable")
    identity_kind = {"user": "users", "repository": "repositories", "labels": "labels", "collaborators": "users"}.get(
        kind, kind
    )
    pages = [_nodes(value, api.forge, identity_kind) for value in pages]
    if request.raw:
        value = pages[0]
        raw_content = _content(value)
        try:
            rendered = raw_content.decode("utf-8")
        except UnicodeError:
            raise Incomplete("raw file/blob content is not UTF-8 text") from None
        if opts.get("jq"):
            raise Unsupported("jq cannot be applied to a raw file/blob response")
    else:
        output = [pages] if opts.get("slurp") else pages
        rendered = "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in output)
        response_length = len(rendered.encode("utf-8"))
        if response_length > api.max_bytes:
            raise Incomplete("normalized API output exceeds the configured aggregate byte bound")
        if opts.get("jq"):
            result = subprocess.run(["jq", "-r", opts["jq"]], input=rendered, text=True, capture_output=True)
            if result.returncode:
                return result.stdout, result.stderr, result.returncode
            rendered = result.stdout
    if request.raw:
        response_length = len(rendered.encode("utf-8"))
    if opts.get("silent"):
        rendered = ""
    if opts.get("include"):
        if collection:
            raise Unsupported("collection response headers require a paginated HTTP adapter")
        if not meta:
            raise Incomplete("Forgejo HTTP response metadata is unavailable")
        headers = {
            k: v
            for k, v in headers.items()
            if k
            in {"content-type", "date", "etag", "last-modified", "cache-control", "expires", "vary", "x-total-count"}
        }
        headers["content-length"] = str(response_length)
        if request.raw:
            headers["content-type"] = "text/plain; charset=utf-8"
        prefix = f"HTTP/{'1.0' if meta['version'] == 10 else '1.1'} {meta['status']} {meta['reason']}\n"
        rendered = prefix + "".join(f"{k}: {v}\n" for k, v in headers.items()) + "\n" + rendered
    return rendered, "", 0
