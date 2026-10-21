"""Small GitHub client for roadmap labels and advisory comments.

GH_READ_TOKEN selects the workflow token for reads; GH_TOKEN remains the App
token for writes whose events must reach downstream workflows. Local invocations
without GH_READ_TOKEN use the normal gh credential for both.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import json
import math
import os
import re
import random
import subprocess
import sys
import time
import urllib.parse


class APIError(RuntimeError):
    """A failed API request, retaining its HTTP status and parsed response data.

    ``status`` is zero when no HTTP response was received; ``data`` then holds
    a bounded diagnostic with the request's credentials redacted. The exception
    message identifies the method/path and the server's message, if present.
    """

    def __init__(self, method, path, status, data, headers=None):
        self.status, self.data = status, data
        self.headers = headers or {}
        message = data.get("message", "request failed") if isinstance(data, dict) else "request failed"
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_READ_TOKEN"):
            if os.environ.get(name):
                message = str(message).replace(os.environ[name], "[redacted]")
        super().__init__(f"GitHub {method} {path}: HTTP {status}: {message}")


class Deferred(RuntimeError):
    """Stop this credential's requests; a later reconciliation must retry."""

    def __init__(self, credential, until):
        self.until = until
        mark_quota_unavailable()
        reset = dt.datetime.fromtimestamp(until, dt.timezone.utc).isoformat()
        super().__init__(f"GitHub {credential} rate limited; deferred until {reset}")


def response(raw):
    """Split gh api --include's status/headers from its JSON body."""
    raw = raw.replace("\r\n", "\n")
    head, separator, body = raw.partition("\n\n")
    match = re.match(r"HTTP/\S+ (\d+)", head)
    if not separator or not match:
        raise RuntimeError("gh api returned no HTTP response headers")
    headers = {}
    for line in head.splitlines()[1:]:
        name, colon, value = line.partition(":")
        if colon:
            headers[name.lower()] = value.strip()
    try:
        data = json.loads(body) if body.strip() else None
    except json.JSONDecodeError:
        data = {"message": "non-JSON API response"}
    return int(match.group(1)), headers, data


def retry_after(value, now, *, default=0):
    """Parse a finite nonnegative wait; absent/invalid values return ``default``.

    Accept seconds or an HTTP date. Callers distinguishing missing timing from
    an explicit zero can pass ``default=None``.
    """
    if not value:
        return default
    try:
        delay = float(value)
        if delay < 0:
            return default
    except ValueError:
        try:
            delay = email.utils.parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return default
    return max(0, delay) if math.isfinite(delay) else default



def safe_path(path):
    """Log endpoint identity without arbitrary query values or credentials."""
    parsed = urllib.parse.urlsplit(path)
    clean = parsed.path
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_READ_TOKEN"):
        if os.environ.get(name):
            clean = clean.replace(os.environ[name], "[redacted]")
    keys = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if keys:
        clean += "?" + "&".join(
            key + "=" + (value if key in {"page", "per_page"} and value.isdigit() else "[redacted]")
            for key, value in keys)
    return clean


class GitHub:
    """A gh-backed client with separate read/write credentials and bounded retries.

    Reads use GH_READ_TOKEN when set; writes retain GH_TOKEN so App mutations
    can trigger downstream workflows. Without GH_READ_TOKEN, both use normal
    gh authentication. ``max_wait`` caps cumulative retry sleeps in seconds
    across this instance, excluding ordinary request time and write spacing.
    Requests try at most three times; exhausted quota waits raise Deferred and
    block further requests on that credential until reset. Mutations are spaced
    by one second within this instance; separate processes are not coordinated.

    Pagination helpers count every response. ``report`` emits request counts,
    GraphQL costs and observed quota headers to stderr and the Actions summary.
    """

    def __init__(self, *, max_wait=180, jitter=0):
        self.max_wait = max_wait
        self.waited = 0
        self.jitter = jitter
        self.cached = {}
        self.blocked = {}
        self.stats = {}
        self.known_labels = set()
        self.last_write = None
        self.renewed_status_token = None

    def _credential(self, read):
        env = dict(os.environ)
        env.pop("GH_DEBUG", None)  # Request debug logs can contain comment bodies.
        env.pop("CI_STATUS_APP_PRIVATE_KEY", None)
        token = env.pop("GH_READ_TOKEN", None)
        if read and token:
            env["GH_TOKEN"] = token
            env.pop("GITHUB_TOKEN", None)
            return os.environ.get("TAUCETI_GITHUB_READ_ROLE", "workflow-read"), env
        if not read and self.renewed_status_token:
            env["GH_TOKEN"] = self.renewed_status_token
            env.pop("GITHUB_TOKEN", None)
        return os.environ.get("TAUCETI_GITHUB_WRITE_ROLE", "app-write" if token else "gh-default"), env

    def _record(self, credential, method, path, status, headers, read):
        stat = self.stats.setdefault(credential, {"requests": 0, "reads": 0, "writes": 0, "buckets": {}})
        stat["requests"] += 1
        stat["reads" if read else "writes"] += 1
        resource = headers.get("x-ratelimit-resource", "core")
        if resource not in {"core", "search", "graphql", "integration_manifest", "code_search"}:
            resource = "other"
        quota = {k: headers.get(f"x-ratelimit-{k}") if str(headers.get(f"x-ratelimit-{k}", "")).isdigit()
                 else None for k in ("limit", "remaining", "reset")}
        path = safe_path(path)
        stat["buckets"][resource] = quota
        # Only fixed API paths and numeric quota values are logged, never tokens or PR bodies.
        print("github-api: " + json.dumps({"credential": credential, "method": method,
              "path": path, "status": status, "resource": resource, **quota}), file=sys.stderr)

    def request(self, method, path, data=None, *, read=None, conditional=False, idempotent=False):
        """Return parsed JSON using the read/write credential selected by ``read``.

        GET defaults to a read; GraphQL queries explicitly opt in. Apply bounded
        quota waits, read retries and idempotent update retries, but do not repeat
        an ambiguous creation POST. Raise ``Deferred`` when quota waits exceed
        the budget, or ``APIError`` for other unsuccessful responses.
        """
        if path.startswith(("http:", "https:", "//")):
            raise ValueError("GitHub requests must use a relative API path")
        read = method == "GET" if read is None else read
        credential, env = self._credential(read)
        now = time.time()
        if self.blocked.get(credential, 0) > now:
            raise Deferred(credential, self.blocked[credential])
        renewed = False
        for attempt in range(3):
            # GitHub recommends at least a second between mutative requests.
            if not read and self.last_write is not None:
                time.sleep(max(0, self.last_write + 1 - time.monotonic()))
            argv = ["gh", "api", "--include", "--method", method, path]
            cache_key = (credential, path)
            cached = self.cached.get(cache_key) if conditional and method == "GET" else None
            if cached:
                argv += ["--header", "If-None-Match: " + cached[0]]
            if data is not None:
                argv += ["--input", "-"]
            try:
                result = subprocess.run(argv, input=json.dumps(data) if data is not None else None,
                                        env=env, capture_output=True, text=True, timeout=30)
            except (subprocess.TimeoutExpired, OSError):
                result = subprocess.CompletedProcess(argv, 1, "", "GitHub request timed out or could not start")
            if not read:
                self.last_write = time.monotonic()
            # Network/CLI failures are ambiguous for a POST; do not blindly repeat it.
            try:
                status, headers, payload = response(result.stdout)
            except RuntimeError:
                # Missing HTTP headers are transport/CLI failures, not API validation.
                diagnostic = (result.stderr or "no HTTP response").strip()
                for name in ("GH_TOKEN", "GITHUB_TOKEN"):
                    secret = env.get(name)
                    if secret:
                        diagnostic = diagnostic.replace(secret, "[redacted]")
                status, headers, payload = 0, {}, {"message": diagnostic[:300]}
            self.last_headers = headers
            self._record(credential, method, path, status, headers, read)
            if path == "graphql" and isinstance(payload, dict):
                rate = (payload.get("data") or {}).get("rateLimit") or {}
                if "cost" in rate:
                    stat = self.stats[credential]
                    stat["graphql_points"] = stat.get("graphql_points", 0) + rate["cost"]
            # GraphQL can refuse a query with HTTP 200 and an errors array.
            if path == "graphql" and isinstance(payload, dict) and payload.get("errors"):
                message = "; ".join(item.get("message", "GraphQL error") for item in payload["errors"])
                payload = {"message": message, "errors": payload["errors"]}
                kinds = {item.get("type") for item in payload["errors"]}
                if "rate limit" in message.lower() or "RATE_LIMITED" in kinds or headers.get("retry-after") or headers.get("x-ratelimit-remaining") == "0":
                    status = 403
                elif kinds & {"INTERNAL", "SERVICE_UNAVAILABLE", "TIMEOUT"} or any(
                    word in message.lower() for word in ("timed out", "timeout", "something went wrong")
                ):
                    status = 503
                else:
                    status = 422
            if status == 304 and cached:
                return cached[1]
            if 200 <= status < 300 and result.returncode == 0:
                if conditional and method == "GET" and headers.get("etag"):
                    self.cached[cache_key] = (headers["etag"], payload)
                return payload
            status_request = not read and (
                method == "POST" and re.fullmatch(r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/statuses/[0-9a-f]{40}", path)
                or method == "GET" and re.fullmatch(r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/commits/[0-9a-f]{40}/statuses\?per_page=100", path))
            if (status == 401 and not renewed and status_request
                    and os.environ.get("CI_STATUS_APP_ID") and os.environ.get("CI_STATUS_APP_PRIVATE_KEY")
                    and attempt < 2):
                # A definite authentication rejection performed no mutation.
                # Only trusted status-only jobs receive these optional secrets;
                # intents cannot select an App, key, endpoint or permissions.
                from github_app_token import status_token
                self.renewed_status_token = status_token("/".join(path.split("/")[1:3]),
                    os.environ["CI_STATUS_APP_ID"], os.environ["CI_STATUS_APP_PRIVATE_KEY"], record=self._record)
                env["GH_TOKEN"] = self.renewed_status_token
                env.pop("GITHUB_TOKEN", None)
                renewed = True
                continue
            if isinstance(payload, dict) and isinstance(payload.get("message"), str):
                payload = dict(payload)
                for name in ("GH_TOKEN", "GITHUB_TOKEN"):
                    if env.get(name):
                        payload["message"] = payload["message"].replace(env[name], "[redacted]")
            error = APIError(method, safe_path(path), status, payload, headers)
            message = str(error).lower()
            limited = status == 429 or (status == 403 and
                       (headers.get("retry-after") or headers.get("x-ratelimit-remaining") == "0" or
                        any(word in message for word in ("rate limit", "abuse detection", "creating content too quickly", "was submitted too quickly"))))
            if limited:
                print("github-api-limited: " + json.dumps({"credential": credential,
                      "resource": headers.get("x-ratelimit-resource", "core"),
                      "status": status}), file=sys.stderr)
                now = time.time()
                delay = retry_after(headers.get("retry-after"), now)
                primary = headers.get("x-ratelimit-remaining") == "0" or (
                    re.search(r"(?:api )?rate limit (?:already )?exceeded", message) is not None
                    and "secondary" not in message)
                if primary:
                    # Missing reset data must not license a fast retry against an exhausted quota.
                    try:
                        reset = float(headers.get("x-ratelimit-reset") or now + 3600)
                        if not math.isfinite(reset):
                            reset = now + 3600
                    except (ValueError, OverflowError):
                        reset = now + 3600
                    delay = max(5, delay, reset - now + 5)
                elif not delay:
                    delay = 60 * 2 ** attempt
                delay = math.ceil(delay + random.uniform(0, self.jitter))
                until = now + delay
                if attempt == 2 or self.waited + delay > self.max_wait:
                    self.blocked[credential] = until
                    raise Deferred(credential, until)
                print(f"github-api: {credential} waiting {delay}s before retry", file=sys.stderr)
                time.sleep(delay)
                self.waited += delay
                continue
            # Retry reads and idempotent updates after server errors. Comment and label
            # creation POSTs are ambiguous after a server error, so reconciliation rereads state.
            if (status == 0 or status >= 500) and (read or idempotent or (status != 0 and method in {"PATCH", "PUT", "DELETE"})) and attempt < 2:
                delay = 2 ** (attempt + 1)
                if self.waited + delay <= self.max_wait:
                    time.sleep(delay)
                    self.waited += delay
                    continue
            raise error
        raise AssertionError("unreachable")

    def graphql(self, query, variables):
        """Run a read-only query with the read credential and return its data object."""
        return self.request("POST", "graphql", {"query": query, "variables": variables}, read=True)["data"]

    def pages(self, path):
        """Yield REST list pages, recording each request and propagating API errors."""
        # Explicit page numbers keep every response (and its rate-limit headers) visible.
        page = 1
        while True:
            rows = self.request("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
            if not isinstance(rows, list):
                raise RuntimeError(f"GitHub {path}: expected an array")
            yield rows
            if len(rows) < 100:
                return
            page += 1

    def items(self, path):
        """Return every item from a paginated REST list, or propagate its failure."""
        return [row for page in self.pages(path) for row in page]

    def skip_write(self, kind):
        """Count an unchanged label/comment decision, without making a request.

        Counts describe decisions, not hypothetical HTTP requests saved; one
        label decision can otherwise require several mutations.
        """
        credential, _ = self._credential(False)
        stat = self.stats.setdefault(credential, {"requests": 0, "reads": 0, "writes": 0, "buckets": {}})
        skipped = stat.setdefault("skipped_writes", {})
        skipped[kind] = skipped.get(kind, 0) + 1
        print("github-api-skip: " + json.dumps({"credential": credential, "kind": kind}), file=sys.stderr)

    def report(self):
        """Write request/quota counters to stderr and the optional Actions step summary."""
        summary = json.dumps(self.stats, sort_keys=True)
        print("github-api-summary: " + summary, file=sys.stderr)
        target = os.environ.get("GITHUB_STEP_SUMMARY")
        if target:
            with open(target, "a") as stream:
                stream.write("\nGitHub API usage (response-header readings):\n\n```json\n" + summary + "\n```\n")


def fingerprint(info):
    """Return head/base/body/state identity used to reject mixed paginated snapshots."""
    return (info["head"]["sha"], info["base"]["sha"], info.get("body") or "", info["state"])


PR_QUERY = """
query($owner: String!, $name: String!, $pr: Int!, $after: String) {
  rateLimit { cost }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      title body state headRefOid baseRefOid baseRefName changedFiles updatedAt
      labels(first: 100) { nodes { name } pageInfo { hasNextPage } }
      files(first: 100, after: $after) {
        nodes { path changeType }
        totalCount
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


HISTORY_QUERY = """
query($owner: String!, $name: String!, $first: Int!, $after: String) {
  rateLimit { cost }
  repository(owner: $owner, name: $name) {
    pullRequests(first: $first, after: $after, states: [OPEN, CLOSED, MERGED],
                 orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes {
        number title body state headRefOid baseRefOid baseRefName changedFiles
        labels(first: 100) { nodes { name } pageInfo { hasNextPage } }
        files(first: 100) {
          nodes { path changeType }
          totalCount
          pageInfo { hasNextPage endCursor }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def snapshot_info(row):
    """Normalize GraphQL PR metadata to the REST-shaped fields used by the classifier."""
    return {"title": row["title"], "body": row["body"], "state": row["state"].lower(),
            "head": {"sha": row["headRefOid"]},
            "base": {"sha": row["baseRefOid"], "ref": row["baseRefName"]},
            "changed_files": row["changedFiles"], "labels": row["labels"]["nodes"]}


def history_snapshots(api, repo, limit):
    """Bulk historical reads, ordered by creation as gh pr list --state all was.

    Only PRs with paginated labels/files need an individual snapshot query.
    """
    owner, name = repo.split("/")
    count, after = 0, None
    while count < limit:
        result = api.graphql(HISTORY_QUERY, {"owner": owner, "name": name,
                            "first": min(100, limit - count), "after": after})
        connection = result["repository"]["pullRequests"]
        for row in connection["nodes"]:
            count += 1
            pr = row["number"]
            files = row["files"]
            if row["labels"]["pageInfo"]["hasNextPage"] or files["pageInfo"]["hasNextPage"] or len(files["nodes"]) != row["changedFiles"] or len(files["nodes"]) != files["totalCount"]:
                try:
                    yield pr, pr_snapshot(api, repo, pr)
                except Deferred:
                    raise
                except APIError as error:
                    if error.status == 0:
                        raise
                    yield pr, error
                except Exception as error:
                    yield pr, error
            else:
                yield pr, {"info": snapshot_info(row), "files": [
                    {"filename": item["path"], "status": item["changeType"].lower()}
                    for item in files["nodes"]]}
        if not connection["pageInfo"]["hasNextPage"]:
            return
        cursor = connection["pageInfo"]["endCursor"]
        if not cursor or cursor == after:
            raise RuntimeError("Historical PR pagination did not advance")
        after = cursor


def pr_snapshot(api, repo, pr):
    """Fetch metadata, labels and files together, sharing one small query in the common case.

    Paginated diffs must keep the same head, base and body throughout.
    Incomplete/capped file lists are marked for conservative classification. GraphQL
    reads use GH_READ_TOKEN and its GraphQL bucket, leaving REST for label writes.
    """
    owner, name = repo.split("/")
    for _ in range(3):
        files, first, after = [], None, None
        while True:
            result = api.graphql(PR_QUERY, {"owner": owner, "name": name, "pr": pr, "after": after})
            row = result["repository"]["pullRequest"]
            if row is None:
                raise RuntimeError(f"PR #{pr}: not found")
            current = snapshot_info(row)
            if first is None:
                first = fingerprint(current)
            if first != fingerprint(current):
                break  # Discard the mixed diff and restart with the new head/body/base.
            connection = row["files"]
            files.extend({"filename": item["path"], "status": item["changeType"].lower()}
                         for item in connection["nodes"])
            if not connection["pageInfo"]["hasNextPage"] or len(files) >= 3000:
                current["files_capped"] = current["changed_files"] > 3000 and len(files) >= 3000
                current["incomplete_files"] = bool(connection["pageInfo"]["hasNextPage"]) or len(files) != current["changed_files"] or len(files) != connection["totalCount"]
                if row["labels"]["pageInfo"]["hasNextPage"]:
                    current["labels"] = api.items(f"repos/{repo}/issues/{pr}/labels")
                return {"info": current, "files": files}
            next_cursor = connection["pageInfo"]["endCursor"]
            if not next_cursor or next_cursor == after:
                raise RuntimeError(f"PR #{pr}: file pagination did not advance")
            after = next_cursor
    raise RuntimeError(f"PR #{pr}: changed while fetching its diff; retry reconciliation")


def mark_quota_unavailable():
    """Expose a trusted step outcome without parsing candidate-produced logs."""
    target = os.environ.get("GITHUB_ENV")
    if target:
        with open(target, "a") as stream:
            stream.write("TAUCETI_CI_QUOTA_UNAVAILABLE=1\n")


def main(argv=None):
    """Small gh-api-compatible JSON CLI using the same bounded client as CI scripts."""
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("path")
    ap.add_argument("-X", "--method")
    ap.add_argument("--jq", "-q")
    ap.add_argument("-f", "--raw-field", action="append", default=[])
    ap.add_argument("-F", "--field", action="append", default=[])
    ap.add_argument("--input")
    ap.add_argument("--paginate", action="store_true")
    ap.add_argument("--slurp", action="store_true")
    ap.add_argument("--silent", action="store_true")
    args = ap.parse_args(argv)
    fields = {}
    for typed, values in ((False, args.raw_field), (True, args.field)):
        for value in values:
            key, sep, text = value.partition("=")
            if not sep or not key or any(c in key for c in "[]"):
                ap.error("fields require a simple name=value")
            if typed and text.startswith("@"):
                text = sys.stdin.read() if text == "@-" else open(text[1:]).read()
            elif typed and re.fullmatch(r"-?\d+|true|false|null", text):
                text = json.loads(text)
            fields[key] = text
    if args.input and fields:
        ap.error("--input and fields cannot be combined")
    data = (json.load(sys.stdin) if args.input == "-" else json.load(open(args.input))) if args.input else (fields or None)
    method = (args.method or ("POST" if data is not None else "GET")).upper()
    path = args.path
    if method == "GET" and data:
        path += ("&" if "?" in path else "?") + urllib.parse.urlencode(data)
        data = None
    api = GitHub(max_wait=int(os.environ.get("TAUCETI_GITHUB_MAX_WAIT", "180")), jitter=2)
    try:
        if args.paginate:
            if method != "GET":
                ap.error("pagination requires GET")
            pages = []
            while path:
                pages.append(api.request("GET", path))
                next_link = re.search(r'<([^>]+)>;\s*rel="next"', api.last_headers.get("link", ""))
                if next_link:
                    parsed = urllib.parse.urlsplit(next_link[1])
                    if parsed.scheme != "https" or parsed.netloc != "api.github.com":
                        raise ValueError("unsafe GitHub pagination link")
                    path = parsed.path.lstrip("/") + ("?" + parsed.query if parsed.query else "")
                else:
                    path = None
            values = [pages] if args.slurp else pages
        else:
            value = api.request(method, path, data)
            values = [[value]] if args.slurp else [value]
        if not args.silent:
            for value in values:
                if args.jq:
                    # Never run a jq filter against an error response.
                    filtered = subprocess.run(["jq", "-r", args.jq], input=json.dumps(value), text=True)
                    if filtered.returncode:
                        return filtered.returncode
                else:
                    print(json.dumps(value))
        return 0
    except Deferred as error:
        print(f"::error::{error}", file=sys.stderr)
        return 75
    except (APIError, ValueError, OSError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    finally:
        api.report()


if __name__ == "__main__":
    raise SystemExit(main())
