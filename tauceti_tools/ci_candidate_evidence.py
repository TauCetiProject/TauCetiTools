"""Bounded authenticated GitHub reads for source-candidate execution evidence."""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .ci_candidate import (
    MAX_ARTIFACT_BYTES,
    EvidenceUnavailable,
    artifact_name,
    check_run_metadata,
    classify_verdicts,
    receipt_artifact_name,
    verify_artifact,
    verify_receipt_artifact,
)
from .forge import GITHUB


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubEvidence:
    def __init__(self, policy, *, token, token_origin, timeout=30, max_pages=10, opener=None):
        if token_origin != GITHUB or policy.execution.repository.forge != GITHUB:
            raise ValueError("execution credential belongs to a different forge origin")
        if not isinstance(token, str) or not token or not all(33 <= ord(c) <= 126 for c in token):
            raise ValueError("invalid execution read credential")
        if timeout <= 0 or type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValueError("invalid execution read bounds")
        self.policy, self.timeout, self.max_pages = policy, timeout, max_pages
        self._token = token
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)

    def _url(self, path):
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or "\\" in path
            or any(p in {".", ".."} for p in urllib.parse.unquote(urllib.parse.urlsplit(path).path).split("/"))
        ):
            raise ValueError("execution API path must stay in the configured repository")
        return GITHUB.api_url + "/repos/" + self.policy.execution.repository.name + path

    def _read(self, request, limit):
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(limit + 1)
                if response.status != 200 or len(raw) > limit:
                    raise EvidenceUnavailable("execution evidence exceeded its bounds")
                return raw
        except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            raise EvidenceUnavailable("execution evidence transport failed") from None

    def get(self, path):
        request = urllib.request.Request(
            self._url(path),
            headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Cache-Control": "no-cache",
                "User-Agent": "tauceti-source-ci",
            },
        )
        raw = self._read(request, 4 * 1024 * 1024)
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise EvidenceUnavailable("execution evidence is not valid JSON") from None

    def artifact_bytes(self, artifact_id):
        if type(artifact_id) is not int or artifact_id <= 0:
            raise ValueError("invalid execution artifact ID")
        # GitHub issues a short-lived signed storage URL. The GitHub credential
        # is sent only to the API; no authorization header follows the redirect.
        request = urllib.request.Request(
            self._url(f"/actions/artifacts/{artifact_id}/zip"),
            headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "tauceti-source-ci",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise EvidenceUnavailable("unexpected execution artifact response")
                raw = response.read(MAX_ARTIFACT_BYTES + 1)
                if len(raw) > MAX_ARTIFACT_BYTES:
                    raise EvidenceUnavailable("execution artifact exceeded its byte bound")
                return raw
        except urllib.error.HTTPError as error:
            status, location = error.code, error.headers.get("Location")
            error.close()
            if status != 302 or not isinstance(location, str):
                raise EvidenceUnavailable("execution artifact is unavailable") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise EvidenceUnavailable("execution artifact transport failed") from None
        parsed = urllib.parse.urlsplit(location)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(c.isspace() for c in location)
        ):
            raise EvidenceUnavailable("invalid execution artifact download location")
        return self._read(
            urllib.request.Request(location, headers={"User-Agent": "tauceti-source-ci"}), MAX_ARTIFACT_BYTES
        )

    def result(self, request, *, run_id, attempt):
        return self._candidate_artifact(request, run_id=run_id, attempt=attempt, receipt=False)

    def receipt(self, request, *, run_id, observed_attempt):
        """Request ownership only; this method returns no build verdict or merge approval."""
        return self._candidate_artifact(request, run_id=run_id, attempt=observed_attempt, receipt=True)

    def _candidate_artifact(self, request, *, run_id, attempt, receipt):
        request.check_policy(self.policy)
        name = (receipt_artifact_name if receipt else artifact_name)(request, run_id, attempt)
        harness_request = urllib.request.Request(
            GITHUB.api_url + "/repos/" + self.policy.harness.repository.name,
            headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Cache-Control": "no-cache",
                "User-Agent": "tauceti-source-ci",
            },
        )
        try:
            harness = json.loads(self._read(harness_request, 1024 * 1024))
        except (ValueError, UnicodeError):
            raise EvidenceUnavailable("execution harness metadata is malformed") from None
        if (
            not isinstance(harness, dict)
            or type(harness.get("id")) is not int
            or harness["id"] != self.policy.harness.native_id
            or harness.get("full_name") != self.policy.harness.repository.name
        ):
            raise EvidenceUnavailable("execution harness repository identity changed")
        run = self.get(f"/actions/runs/{run_id}")
        check_run_metadata(request, self.policy, run, run_id=run_id, attempt=attempt, require_completed=not receipt)
        rows, total = [], None
        for page in range(1, self.max_pages + 1):
            value = self.get(f"/actions/runs/{run_id}/artifacts?per_page=100&page={page}")
            if (
                not isinstance(value, dict)
                or type(value.get("total_count")) is not int
                or not isinstance(value.get("artifacts"), list)
            ):
                raise EvidenceUnavailable("incomplete execution artifact listing")
            if total is None:
                total = value["total_count"]
            if value["total_count"] != total or total < 0 or len(value["artifacts"]) > 100:
                raise EvidenceUnavailable("execution artifact listing changed")
            rows.extend(value["artifacts"])
            if len(rows) == total:
                break
            if not value["artifacts"] or len(rows) > total:
                raise EvidenceUnavailable("execution artifact listing is truncated")
        else:
            raise EvidenceUnavailable("execution artifact listing exceeded its page bound")
        if any(
            not isinstance(row, dict)
            or type(row.get("id")) is not int
            or row["id"] <= 0
            or not isinstance(row.get("name"), str)
            for row in rows
        ) or len({row["id"] for row in rows}) != len(rows):
            raise EvidenceUnavailable("execution artifact listing contains invalid or duplicate identities")
        receipt_attempt = None
        if receipt:
            pattern = re.compile(rf"source-candidate-request-([0-9a-f]{{32}})-{run_id}-([1-9][0-9]{{0,18}})")
            owned = []
            attempts = set()
            for row in rows:
                matched = pattern.fullmatch(row["name"])
                if matched is None:
                    continue
                previous = int(matched[2])
                if previous > attempt:
                    raise EvidenceUnavailable("request receipt belongs to a newer attempt")
                if previous in attempts or matched[1] != request.request_id:
                    raise EvidenceUnavailable("workflow request receipts are ambiguous")
                attempts.add(previous)
                owned.append((previous, row))
            if not owned:
                raise EvidenceUnavailable("workflow request receipt is unavailable")
            available = [(previous, row) for previous, row in owned if row.get("expired") is False]
            if not available:
                raise EvidenceUnavailable("workflow request receipts have expired or are unavailable")
            receipt_attempt, selected = max(available, key=lambda item: item[0])
            matches = [selected]
        else:
            matches = [row for row in rows if row["name"] == name]
        if len(matches) != 1:
            raise EvidenceUnavailable("execution artifact is missing or ambiguous")
        metadata = self.get(f"/actions/artifacts/{matches[0]['id']}")
        if not isinstance(metadata, dict) or metadata.get("id") != matches[0]["id"]:
            raise EvidenceUnavailable("execution artifact identity changed")
        if metadata.get("expired") is True:
            raise EvidenceUnavailable("execution artifact has expired")
        raw = self.artifact_bytes(metadata.get("id"))
        if receipt:
            verified = verify_receipt_artifact(
                request,
                self.policy,
                metadata,
                raw,
                run,
                run_id=run_id,
                observed_attempt=attempt,
                receipt_attempt=receipt_attempt,
            )
        else:
            verified = verify_artifact(request, self.policy, metadata, raw, run, run_id=run_id, attempt=attempt)
        # A changed observed attempt requires a fresh read. An older receipt can
        # still prove run ownership then; a result must match the latest attempt.
        current = self.get(f"/actions/runs/{run_id}")
        check_run_metadata(request, self.policy, current, run_id=run_id, attempt=attempt, require_completed=not receipt)
        if not receipt and current.get("conclusion") != run.get("conclusion"):
            raise EvidenceUnavailable("execution result changed during verification")
        proof = {
            "run_id": run_id,
            "artifact_id": metadata["id"],
            "artifact_digest": metadata["digest"],
            "request_digest": request.digest,
        }
        if receipt:
            proof.update(kind="request", **verified)
        else:
            proof.update(kind="result", attempt=attempt, verdicts=verified, classification=classify_verdicts(verified))
        return proof
