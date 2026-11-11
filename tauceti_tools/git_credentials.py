"""Origin-scoped Git credentials for trusted forge operations."""

from __future__ import annotations

import base64
import os
import shlex
import stat
import sys
from urllib.parse import urlsplit

from .forge import Forge
from .git_inputs import ENV_ALLOW


def read_private_token(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Git credential file must be private and owned")
        token = stream.read(4098).strip()
    if not 0 < len(token) <= 4096 or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("Git credentials require an explicit bearer token")
    return token


def scoped_git_environment(origin, token=None, *, token_file=None, parent=None, purpose="source"):
    """Keep credentials in a clean child environment, never arguments or config files."""
    if purpose not in {"source", "workspace"}:
        raise ValueError("Git environment purpose must be source or workspace")
    if not isinstance(origin, Forge):
        raise ValueError("Git credentials require an explicit forge origin")
    if token_file is not None:
        if token is not None or not os.path.isabs(os.fspath(token_file)):
            raise ValueError("Git credential file must be absolute and replace the inline token")
    elif not isinstance(token, str) or not 0 < len(token) <= 4096 or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("Git credentials require an explicit bearer token")
    parent = os.environ if parent is None else parent
    env = {key: parent[key] for key in (*ENV_ALLOW, "GIT_SSL_CAINFO") if key in parent}
    env.update(
        LC_ALL="C",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_ATTR_NOSYSTEM="1",
    )
    settings = [
        ("credential.helper", ""),
        ("core.hooksPath", "/dev/null"),
        ("core.attributesFile", "/dev/null"),
        ("core.fsmonitor", "false"),
        ("http.followRedirects", "false"),
        ("http.sslVerify", "true"),
        ("fetch.fsckObjects", "true"),
    ]
    if token_file is not None:
        command = shlex.join(
            [
                sys.executable,
                "-I",
                "-m",
                "tauceti_tools.git_credentials",
                "--origin",
                origin.url,
                "--kind",
                origin.kind,
                "--token-file",
                os.fspath(token_file),
            ]
        )
        settings.append((f"credential.{origin.url}.helper", "!" + command))
    else:
        encoded = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
        settings.append((f"http.{origin.url}/.extraheader", "AUTHORIZATION: basic " + encoded))
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


def main():
    """Git credential protocol: disclose only to an exact configured origin on get."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("action", choices=("get", "store", "erase"))
    args = parser.parse_args()
    if args.action != "get":
        return 0
    origin = Forge(args.kind, args.origin)
    raw = sys.stdin.read(16385)
    if len(raw) > 16384:
        return 1
    fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    try:
        requested = Forge(origin.kind, fields.get("protocol", "") + "://" + fields.get("host", ""))
        if requested != origin:
            return 0
        token = read_private_token(args.token_file)
    except (ValueError, OSError):
        return 1
    sys.stdout.write("username=x-access-token\npassword=" + token + "\n\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
