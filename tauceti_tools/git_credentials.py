"""Origin-scoped Git credentials for trusted forge operations."""

from __future__ import annotations

import base64
import os
from urllib.parse import urlsplit

from .forge import Forge


def scoped_git_environment(origin, token, *, parent=None, purpose="source"):
    """Keep credentials in a clean child environment, never arguments or config files."""
    if purpose not in {"source", "workspace"}:
        raise ValueError("Git environment purpose must be source or workspace")
    if not isinstance(origin, Forge):
        raise ValueError("Git credentials require an explicit forge origin")
    if not isinstance(token, str) or not 0 < len(token) <= 4096 or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("Git credentials require an explicit bearer token")
    parent = os.environ if parent is None else parent
    env = {key: parent[key] for key in ("PATH", "HOME", "TMPDIR", "SSL_CERT_FILE", "GIT_SSL_CAINFO") if key in parent}
    env.update(
        LC_ALL="C",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_ATTR_NOSYSTEM="1",
    )
    encoded = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
    settings = [
        ("credential.helper", ""),
        ("core.hooksPath", "/dev/null"),
        ("core.attributesFile", "/dev/null"),
        ("core.fsmonitor", "false"),
        ("http.followRedirects", "false"),
        ("http.sslVerify", "true"),
        ("fetch.fsckObjects", "true"),
        (f"http.{origin.url}/.extraheader", "AUTHORIZATION: basic " + encoded),
    ]
    if purpose == "source":
        env["GIT_NO_LAZY_FETCH"] = "1"
        settings.extend((("protocol.allow", "never"), (f"protocol.{urlsplit(origin.url).scheme}.allow", "always")))
    else:
        # Workspace tools (Lake and uv) materialize partial dependencies and
        # clone their trusted local package caches. These are not immutable
        # source reads. Authentication remains bound to its exact origin.
        env["GIT_NO_LAZY_FETCH"] = "0"
    env["GIT_CONFIG_COUNT"] = str(len(settings))
    for index, (key, value) in enumerate(settings):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env
