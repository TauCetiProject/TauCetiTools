"""Cooperative Git leases using the existing claim.sh compare-and-swap protocol.

The bundled shell implementation is shared with TauCetiWorker. No REST requests
are used to acquire, renew, or release a lease.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path


def script_path() -> Path:
    return Path(__file__).parent / "assets/scripts/claim.sh"


def record_review_start(repository, pr, head, nonce, *, marker_posted, git_held):
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
        marker_posted=marker_posted,
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
