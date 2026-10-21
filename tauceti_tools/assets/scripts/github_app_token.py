"""Renew a repository-scoped CI status token after expiry, only in trusted jobs.

The private key is supplied by the workflow environment, never by an intent.
Signing uses the runner's OpenSSL; neither the key nor JWT reaches diagnostics.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def encoded(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")


def signed_jwt(app_id, private_key):
    if not re.fullmatch(r"[1-9][0-9]*", str(app_id)):
        raise ValueError("CI status App ID must be a positive number")
    now = int(time.time())
    unsigned = encoded({"alg": "RS256", "typ": "JWT"}) + b"." + encoded({"iat": now - 60, "exp": now + 540, "iss": str(app_id)})
    # NamedTemporaryFile is mode0600, and is removed even if OpenSSL fails.
    with tempfile.NamedTemporaryFile(mode="w", prefix="tauceti-ci-app-key-") as key:
        key.write(private_key)
        key.flush()
        env = {name: value for name, value in os.environ.items()
               if name not in {"CI_STATUS_APP_PRIVATE_KEY", "GH_TOKEN", "GITHUB_TOKEN", "GH_READ_TOKEN", "GH_DEBUG"}}
        try:
            result = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key.name],
                                    input=unsigned, capture_output=True, timeout=15, env=env)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("CI status App signing unavailable") from None
        if result.returncode or not result.stdout:
            raise RuntimeError("CI status App signing failed")
    return (unsigned + b"." + base64.urlsafe_b64encode(result.stdout).rstrip(b"=")).decode("ascii")


def status_token(repository, app_id, private_key, *, record=None):
    """Mint only statuses:write for one repository; never follow authenticated redirects."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid CI status repository")
    jwt = signed_jwt(app_id, private_key)
    opener = urllib.request.build_opener(NoRedirect)

    def request(method, path, payload=None):
        req = urllib.request.Request("https://api.github.com/" + path, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "Bearer " + jwt, "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "TauCetiCI-status-renewal/1.0"})
        try:
            with opener.open(req, timeout=30) as response:
                if record:
                    record("ci-status-app-auth", method, path, response.status,
                           {key.lower(): value for key, value in response.headers.items()}, method == "GET")
                raw = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            if record:
                record("ci-status-app-auth", method, path, error.code,
                       {key.lower(): value for key, value in error.headers.items()}, method == "GET")
            code = error.code
            error.close()
            raise RuntimeError(f"CI status App renewal rejected (HTTP {code})") from None
        except OSError:
            raise RuntimeError("CI status App renewal unavailable") from None
        if len(raw) > 1024 * 1024:
            raise RuntimeError("CI status App renewal response exceeds bound")
        try:
            return json.loads(raw)
        except ValueError:
            raise RuntimeError("CI status App renewal returned invalid JSON") from None

    installation = request("GET", f"repos/{repository}/installation")
    installation_id = installation.get("id") if isinstance(installation, dict) else None
    if type(installation_id) is not int or installation_id <= 0:
        raise RuntimeError("CI status App repository installation unavailable")
    data = request("POST", f"app/installations/{installation_id}/access_tokens",
                   {"repositories": [repository.split("/", 1)[1]], "permissions": {"statuses": "write"}})
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token or any(char.isspace() for char in token):
        raise RuntimeError("CI status App renewal returned no token")
    # Register a mask before any subsequent gh process can include this token in
    # an Actions diagnostic. Never emit a masking command outside Actions.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::add-mask::" + token, flush=True)
    return token
