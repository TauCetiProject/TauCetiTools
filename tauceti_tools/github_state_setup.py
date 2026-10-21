"""Small, explicit setup commands for GitHub-state clients and operators."""

import hashlib
import json
import os
import secrets
import shlex
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .project import ConfigError, repository


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def client_token_file(project):
    return Path(os.environ.get(
        "TAUCETI_GITHUB_STATE_TOKEN_FILE",
        str(Path.home() / ".config/tauceti/projects" / project.key / "github_state/client-token"),
    )).expanduser()


def public_configuration(project):
    return (project.source if project.source.startswith("https://") else
            f"https://raw.githubusercontent.com/{project.content}/{project.branch}/tauceti.toml")


def request_file(project):
    return client_token_file(project).with_name("access-request.md")


def request_title(project):
    return f"Infrastructure request: {project.content} (github_state)"


def _private(path, text, *, overwrite=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    with os.fdopen(os.open(path, flags, 0o600), "w") as output:
        output.write(text)
    path.chmod(0o600)


def _grant(path, repository):
    token = path.read_text().strip()
    if not token:
        raise ConfigError(f"Token file is empty: {path}. Restore the token or remove the empty file and retry.")
    return {"hash": hashlib.sha256(token.encode()).hexdigest(), "repositories": [repository]}


def run(project, args):
    if args.action == "request":
        if project.service("github_state").get("mode") != "hosted":
            raise ConfigError("Access requests apply to services.github_state.mode = 'hosted'.")
        if args.installation <= 0:
            raise ConfigError("--installation must be the positive number from the App installation URL.")
        token = client_token_file(project)
        if not token.exists():
            _private(token, secrets.token_urlsafe(48) + "\n")
        token.chmod(0o600)
        grant = _grant(token, project.content)
        _private(token.with_name("client-grant.json"), json.dumps(grant) + "\n", overwrite=True)
        body = (f"Repository: {project.content}\nConfiguration: {public_configuration(project)}\n"
                f"App installation ID: {args.installation}\nClient grant: {json.dumps(grant)}\n\n"
                "Please approve GitHub-state access. This request contains no raw credentials.\n")
        _private(request_file(project), body, overwrite=True)
        print(f"Client token saved privately: {token}")
        print(f"Access request saved: {request_file(project)}")
        print("Submit the request (only the token hash is shared):")
        print("  " + shlex.join(["gh", "issue", "create", "--repo", "TauCetiProject/TauCetiTools",
                                  "--title", request_title(project), "--body-file", str(request_file(project))]))
    elif args.action == "initial-grants":
        repository(args.repository, "--repository")
        # This prepares a local file only. Uploading it is an explicit operator step.
        _private(args.output, json.dumps([_grant(args.token_file, args.repository)], indent=2) + "\n")
        print(f"Saved the initial grant to {args.output}. Add any other existing grants before uploading the full array.")
    else:
        # The operator chooses this endpoint; downstream configuration cannot choose
        # where the service's admin credential is sent.
        url = args.url
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ConfigError("Pass --url with the operator's HTTPS service URL without credentials.")
        token = args.admin_token_file.read_text().strip()
        if not token:
            raise ConfigError(f"Admin token file is empty: {args.admin_token_file}")
        request = urllib.request.Request(
            url.rstrip("/") + "/admin/bootstrap",
            data=json.dumps({"repository": project.content}).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                     "User-Agent": "TauCetiWorker/1.0"},
        )
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
                print(f"Initialization started for {project.content} (HTTP {response.status}).")
        except urllib.error.HTTPError as error:
            raise ConfigError(f"Initialization failed (HTTP {error.code}); check the operator credential and repository grant.") from None
        except OSError:
            raise ConfigError("Could not reach the GitHub-state service; check its URL and retry.") from None
