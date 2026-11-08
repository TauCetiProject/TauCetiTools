"""Cooperative Git leases using the existing claim.sh compare-and-swap protocol.

The bundled shell implementation is shared with TauCetiWorker. Native claims
check their repository identity through REST; the lease itself uses only Git CAS.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

from .ci_candidate import RepositoryIdentity, canonical
from .forge import ForgeError, Incomplete
from .git_credentials import scoped_git_environment


def script_path() -> Path:
    return Path(__file__).parent / "assets/scripts/claim.sh"


def record_review_start(repository, pr, head, nonce, *, git_held):
    """Append one allow-listed receipt to the fleet's existing journal, without network calls."""
    target = os.environ.get("TAUCETI_FLEET_EVENTS")
    if not target:
        return
    event = dict(
        version=1,
        kind="review_coordination",
        id=hashlib.sha256(nonce.encode()).hexdigest(),
        at=time.time(),
        repository=repository,
        pr=pr,
        head_sha=head,
        worker=os.environ.get("TAUCETI_WORKER_ID", "review"),
        machine_id=os.environ.get("TAUCETI_FLEET_MACHINE_ID", "local"),
        git_held=git_held,
    )
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stream.write(json.dumps(event) + "\n")
    except OSError:
        pass  # Reporting must not delay a review.


def review_repository(content: str) -> str | None:
    """Use the operator's namespace, otherwise existing shared access or an existing direct fork."""
    if os.environ.get("CLAIM_REPO"):
        return os.environ["CLAIM_REPO"]
    shared = os.environ.get(
        "TAUCETI_CLAIMS_REPO", "TauCetiProject/tauceti-claims" if content.lower() == "taucetiproject/tauceti" else ""
    )
    if not shared:
        return None

    def query(endpoint):
        try:
            result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, timeout=30)
            return json.loads(result.stdout) if result.returncode == 0 else {}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {}

    if query("repos/" + shared).get("permissions", {}).get("push") is True:
        return shared
    login = query("user").get("login")
    if login:
        fork = query("repos/" + login + "/" + content.split("/")[1])
        if (
            fork.get("permissions", {}).get("push")
            and fork.get("parent", {}).get("full_name", "").lower() == content.lower()
        ):
            return fork["full_name"]
    return None


class GitClaim:
    def __init__(self, repository: str, key: str, owner: str, ttl: int = 1800):
        self.key = key
        self.ttl = ttl
        self.env = {**os.environ, "CLAIM_REPO": repository, "TAUCETI_WORKER_ID": owner}
        self.script = os.environ.get("TAUCETI_CLAIM_SH") or str(script_path())
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.held = False

    def command(self, verb: str) -> int:
        try:
            args = ["bash", self.script, verb, self.key]
            if verb != "release":
                args.append(str(self.ttl))
            return subprocess.run(args, env=self.env, capture_output=True, timeout=30).returncode
        except (OSError, subprocess.TimeoutExpired):
            return 2

    def acquire(self) -> int:
        result = self.command("acquire")
        if result == 0:
            self.held = True
        return result

    def start_heartbeat(self) -> None:
        def renew():
            while not self.stop.wait(max(1, self.ttl // 3)):
                result = self.command("renew")
                if result == 1:
                    # Cooperative deduplication only: do not discard a completed review.
                    self.held = False
                    break

        self.thread = threading.Thread(target=renew, daemon=True)
        self.thread.start()

    def release(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=35)
            self.thread = None
        if self.held:
            self.command("release")
            self.held = False


def qualified_claim_key(source, resource):
    """Encode the source server/native repository ID without illegal ref characters."""
    if not isinstance(source, RepositoryIdentity) or not isinstance(resource, str) or not resource:
        raise ValueError("qualified claims require a source identity and resource")
    try:
        if len(resource.encode("utf-8")) > 4096:
            raise ValueError("claim resource exceeds its bound")
    except UnicodeError:
        raise ValueError("claim resource must be valid UTF-8") from None
    value = {
        "schema": "tauceti-claim-resource/v2",
        "source": source.repository.key,
        "source_id": source.native_id,
        "resource": resource,
    }
    return "v2/" + hashlib.sha256(canonical(value)).hexdigest()


class ForgejoGitClaim(GitClaim):
    """Explicit native Git lease; unavailable observations never mean ownership.

    The cache is private controller/worker state, never a candidate checkout.
    Administrative repository rename/deletion must not overlap lease operations.
    A claim is cooperative coordination and confers no merge/review authority.
    """

    def __init__(
        self, repository, resource, owner, ttl=1800, *, source, client, token, credential_origin, cache_base=None
    ):
        if (
            not isinstance(repository, RepositoryIdentity)
            or repository.repository.forge.kind != "forgejo"
            or getattr(client, "forge", None) != repository.repository.forge
            or credential_origin != repository.repository.forge
        ):
            raise ValueError("native claim repository and credential origins differ")
        if type(ttl) is not int or not -86400 <= ttl <= 86400:
            raise ValueError("claim TTL must be a bounded integer")
        if not isinstance(owner, str) or not owner or len(owner) > 512 or any(c in owner for c in "\0\r\n"):
            raise ValueError("claim owner must be a bounded process identity")
        super().__init__(repository.repository.name, qualified_claim_key(source, resource), owner, ttl)
        self.repository_identity, self.client = repository, client
        self.script = str(script_path())  # Never execute an ambient candidate-selected helper.
        self.env = scoped_git_environment(credential_origin, token)
        base = (
            Path(cache_base)
            if cache_base is not None
            else Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tauceti-claims"
        )
        scope = hashlib.sha256(
            canonical({"repository": repository.repository.key, "id": repository.native_id})
        ).hexdigest()
        self.directory = base.absolute() / (scope + ".git")
        self.git = shutil.which("git")
        if not self.git or not shutil.which("bash") or not shutil.which("jq"):
            raise OSError("native claims require Git, Bash and jq")
        self.env.update(
            CLAIM_REPO=repository.repository.name,
            CLAIM_GIT_URL=repository.repository.clone_url,
            CLAIM_GITDIR=str(self.directory),
            CLAIM_FIXED_REMOTE="1",
            TAUCETI_WORKER_ID=owner,
        )

    def _bound(self):
        row = self.client.repository(self.repository_identity.repository)
        if (
            not isinstance(row, dict)
            or type(row.get("id")) is not int
            or row["id"] != self.repository_identity.native_id
            or not isinstance(row.get("full_name"), str)
            or row["full_name"].casefold() != self.repository_identity.repository.name.casefold()
            or row.get("has_actions") is not False
            or row.get("archived") is not False
            or row.get("mirror") is not False
        ):
            raise Incomplete("native claims require the bound writable repository with Actions disabled")

    def _prepare(self):
        self.directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.directory) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
            ):
                raise OSError("native claim cache lock must be an owned private regular file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.directory.is_symlink():
                raise OSError("native claim cache cannot be a symlink")
            if not self.directory.exists():
                subprocess.run(
                    [self.git, "init", "--quiet", "--bare", "--template=", str(self.directory)],
                    env=self.env,
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
            remote = subprocess.run(
                [self.git, "-C", str(self.directory), "remote", "get-url", "origin"],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if remote.returncode == 2:
                subprocess.run(
                    [self.git, "-C", str(self.directory), "remote", "add", "origin", self.env["CLAIM_GIT_URL"]],
                    env=self.env,
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
            elif remote.returncode != 0 or remote.stdout.strip() != self.env["CLAIM_GIT_URL"]:
                raise OSError("native claim cache belongs to another remote")
        finally:
            os.close(fd)

    def command(self, verb):
        try:
            self._bound()
            self._prepare()
            result = super().command(verb)
            self._bound()
        except (ForgeError, Incomplete, OSError, subprocess.SubprocessError):
            result = 2
        if result not in (0, 1):
            result = 2
        if verb in {"acquire", "renew", "holds"}:
            self.held = result == 0
        return result
