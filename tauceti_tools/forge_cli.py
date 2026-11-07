"""Experimental gh-compatible direct Forgejo reads, with explicit GitHub delegation.

Invoked as tauceti-gh while the worker integration is prepared. This does not
install a global gh replacement or activate Forgejo in legacy worker bindings.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.parse

from .forge import GITHUB, Forge, ForgeError, Forgejo, RepositoryRef, Unsupported
from .forge_command import parse_command
from .forge_commands import execute, render
from .project import ConfigError, load


def _checkout_repository(refs):
    result = subprocess.run(["git", "config", "--get", "remote.origin.url"], capture_output=True, text=True)
    remote = result.stdout.strip() if result.returncode == 0 else ""
    if remote.startswith("git@") and ":" in remote:
        host, path = remote[4:].split(":", 1)
    else:
        parsed = urllib.parse.urlsplit(remote)
        host, path = parsed.netloc.rsplit("@", 1)[-1], parsed.path.lstrip("/")
    path = path.removesuffix(".git").rstrip("/")
    matches = {
        ref
        for ref in refs
        if host.lower() == urllib.parse.urlsplit(ref.forge.url).netloc and path.lower() == ref.name.lower()
    }
    if len(matches) != 1:
        raise Unsupported("checkout origin is not uniquely configured; specify -R HOST/OWNER/REPO")
    return matches.pop()


def _require_origin(env, name, forge):
    value = env.get(name)
    if not value or Forge(forge.kind, value) != forge:
        raise ForgeError(f"credential origin must match the destination: set {name} explicitly")


def _known_forges(refs):
    return {ref.forge for ref in refs} | {GITHUB}


def _host_forge(host, refs):
    matches = [
        forge for forge in _known_forges(refs) if urllib.parse.urlsplit(forge.url).netloc.casefold() == host.casefold()
    ]
    if len(matches) != 1:
        raise Unsupported("hostname is not uniquely configured")
    return matches[0]


def _selected_repository(selected, refs, primary):
    matches = {
        ref
        for ref in refs
        if selected.casefold().rstrip("/")
        in {
            ref.name.casefold(),
            ref.url.casefold(),
            urllib.parse.urlsplit(ref.url).netloc.casefold() + "/" + ref.name.casefold(),
        }
    }
    if len(matches) > 1:
        raise Unsupported("ambiguous repository; specify HOST/OWNER/REPO")
    if matches:
        return matches.pop()
    if selected.startswith(("https://", "http://")):
        parsed = urllib.parse.urlsplit(selected)
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise Unsupported("repository URL must not contain credentials, queries or fragments")
        origin = parsed.scheme + "://" + parsed.netloc
        candidates = [forge for forge in _known_forges(refs) if forge.url.casefold() == origin.casefold()]
        if len(candidates) != 1:
            raise Unsupported("repository origin is not uniquely configured")
        return RepositoryRef(candidates[0], parsed.path.strip("/"))
    parts = selected.split("/")
    if len(parts) == 3:
        return RepositoryRef(_host_forge(parts[0], refs), "/".join(parts[1:]))
    if len(parts) == 2:
        return RepositoryRef(primary.forge, selected)
    raise Unsupported("repository requires OWNER/REPO or HOST/OWNER/REPO")


def run(argv, *, environment=None):
    env = os.environ if environment is None else environment
    if argv in (["--help"], ["-h"]):
        print("tauceti-gh [--conf PATH] COMMAND: direct Forgejo operations with native GitHub delegation")
        return 0
    native = list(argv)
    source = env.get("TAUCETI_CONF")
    if native and native[0] == "--conf":
        if len(native) < 2 or not native[1]:
            raise Unsupported("--conf requires a nonempty value")
        source, native = native[1], native[2:]
    elif native and native[0].startswith("--conf="):
        source, native = native[0].partition("=")[2], native[1:]
        if not source:
            raise Unsupported("--conf requires a nonempty value")
    if not source:
        return _delegate(native, env)
    snapshot = env.get("TAUCETI_CONF_SNAPSHOT") if env.get("TAUCETI_CONF_SOURCE") == source else None
    project = load(snapshot or source)
    refs = [project.repository_ref(role) for role in project.raw["repositories"]]
    if all(ref.forge == GITHUB for ref in refs):
        return _delegate(native, env)
    command = parse_command(native)
    opts = command.options
    primary = project.repository_ref("content")
    selected = opts.get("repo") or env.get("GH_REPO")
    if command.group == "repo" and command.action in {"view", "fork"} and command.args:
        if opts.get("repo"):
            raise Unsupported("specify the repository once")
        selected = command.args[0]
    if command.group == "auth" or command.key == ("repo", "list"):
        host = opts.get("hostname") or env.get("GH_HOST")
        repository = RepositoryRef(_host_forge(host, refs), primary.name) if host else primary
    elif selected:
        repository = _selected_repository(selected, refs, primary)
    else:
        repository = _checkout_repository(refs)
    if repository.forge.kind == "github":
        if repository.forge != GITHUB and any(
            env.get(key) for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
        ):
            _require_origin(env, "TAUCETI_GITHUB_ORIGIN", repository.forge)
        host = urllib.parse.urlsplit(repository.forge.url).netloc
        return _delegate(native, {**env, "GH_REPO": host + "/" + repository.name, "GH_HOST": host})
    token_file = env.get("TAUCETI_FORGEJO_TOKEN_FILE")
    token = None
    if token_file:
        _require_origin(env, "TAUCETI_FORGEJO_ORIGIN", repository.forge)
        from pathlib import Path

        with Path(token_file).open() as credential:
            if os.fstat(credential.fileno()).st_mode & 0o077:
                raise ForgeError("Forgejo token file must be private (chmod 600)")
            token = credential.read().strip()
        if not token:
            raise ForgeError("Forgejo token file is empty")
    api = Forgejo(repository.forge, token=token)
    value, json_output = execute(
        command, repository, api, token=token, expected_user_id=env.get("TAUCETI_FORGEJO_USER_ID")
    )
    rendered = render(value, json_output)
    if opts.get("jq"):
        result = subprocess.run(["jq", "-r", opts["jq"]], input=rendered, text=True, capture_output=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    sys.stdout.write(rendered)
    return 0


def _delegate(argv, env):
    executable = env.get("TAUCETI_REAL_GH") or shutil.which("gh", path=env.get("PATH"))
    if not executable or os.path.realpath(executable) == os.path.realpath(sys.argv[0]):
        raise Unsupported("real GitHub CLI not found; set TAUCETI_REAL_GH")
    return subprocess.run([executable, *argv], env=env).returncode


def main():
    try:
        return run(sys.argv[1:])
    except (ForgeError, ConfigError, ValueError, OSError) as error:
        print(f"gh: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
