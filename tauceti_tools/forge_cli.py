"""Experimental gh-compatible direct Forgejo operations, with explicit GitHub delegation.

Invoked as tauceti-gh while the worker integration is prepared. This does not
install a global gh replacement or activate Forgejo in legacy worker bindings.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.parse

from . import forge_api
from .forge import GITHUB, AmbiguousWrite, Forge, ForgeError, Forgejo, RepositoryRef, Unsupported
from .forge_command import CI_COMMANDS, parse_command
from .forge_commands import execute, render
from .project import ConfigError, load, resolve_repository


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
        raise Unsupported("unconfigured bare repository; specify HOST/OWNER/REPO")
    raise Unsupported("repository requires OWNER/REPO or HOST/OWNER/REPO")


def _api_repository(command, refs, primary, env):
    host = command.options.get("hostname") or env.get("GH_HOST")
    forge = _host_forge(host, refs) if host else None
    endpoint = command.args[0]
    parsed_endpoint = urllib.parse.urlsplit(endpoint)
    if parsed_endpoint.scheme or parsed_endpoint.netloc or parsed_endpoint.fragment or "\\" in endpoint:
        raise Unsupported("API endpoint must be relative; select its configured origin with --hostname")
    context = primary
    from_context = "{owner}" in endpoint or "{repo}" in endpoint
    if from_context:
        selected = env.get("GH_REPO")
        if selected and forge and len(selected.split("/")) == 2:
            context = RepositoryRef(forge, selected)
        elif selected:
            context = _selected_repository(selected, refs, primary)
        else:
            context = _checkout_repository(refs)
        owner, name = context.name.split("/")
        endpoint = endpoint.replace("{owner}", owner).replace("{repo}", name)
    path = urllib.parse.urlsplit(endpoint).path.lstrip("/")
    match = re.match(r"repos/([^/]+/[^/]+)(?:/|$)", path)
    name = match.group(1) if match else None
    if forge:
        repository = RepositoryRef(forge, name or context.name)
    elif name and from_context:
        repository = RepositoryRef(context.forge, name)
    elif name:
        repository = _selected_repository(name, refs, primary)
    else:
        repository = primary
    return repository, context


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
    refs += [
        resolve_repository(value["repo"], project.raw.get("forges")) for value in project.raw.get("tools", {}).values()
    ]
    refs += [resolve_repository(value["repo"], project.raw.get("forges")) for value in project.raw.get("upstreams", [])]
    if "ci" in project.raw:
        refs.append(project.ci_repository_ref())
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
    api_plan = None
    if command.group == "api":
        repository, api_context = _api_repository(command, refs, primary, env)
    elif command.group == "auth" or command.key == ("repo", "list"):
        host = opts.get("hostname") or env.get("GH_HOST")
        repository = RepositoryRef(_host_forge(host, refs), primary.name) if host else primary
    elif selected:
        repository = _selected_repository(selected, refs, primary)
    elif command.key in CI_COMMANDS:
        repository = project.ci_repository_ref()
    else:
        repository = _checkout_repository(refs)
    if command.key in CI_COMMANDS:
        if repository.forge.kind != "github":
            raise Unsupported("GitHub Actions commands require a GitHub repository; select -R HOST/OWNER/REPO")
        if repository.key not in {ref.key for ref in refs if ref.forge.kind == "github"}:
            raise Unsupported("GitHub Actions commands require a configured GitHub repository")
    if repository.forge.kind == "github":
        if repository.forge != GITHUB and any(
            env.get(key) for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
        ):
            _require_origin(env, "TAUCETI_GITHUB_ORIGIN", repository.forge)
        host = urllib.parse.urlsplit(repository.forge.url).netloc
        if command.group == "api":
            return _delegate(native, {**env, "GH_HOST": host})
        return _delegate(native, {**env, "GH_REPO": host + "/" + repository.name, "GH_HOST": host})
    if command.group == "api":
        api_plan = forge_api.plan(command, context=api_context)
    if opts.get("jq", "").startswith("-"):
        raise Unsupported("jq filters beginning with a dash are unsupported; wrap the expression in parentheses")
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
    if api_plan is not None:
        rendered, diagnostic, status = forge_api.execute(api_plan, api)
        sys.stdout.write(rendered)
        sys.stderr.write(diagnostic)
        return status
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
    try:
        status = subprocess.run([executable, *argv], env=env).returncode
    except KeyboardInterrupt:
        return 130
    return status if status >= 0 else 128 - status


def main():
    try:
        return run(sys.argv[1:])
    except AmbiguousWrite as error:
        print(f"gh: ambiguous-write: {error}", file=sys.stderr)
        return 2
    except (ForgeError, ConfigError, ValueError, OSError) as error:
        print(f"gh: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
