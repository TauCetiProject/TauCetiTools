#!/usr/bin/env python3
"""Run the trusted sandbox launcher while reporting bounded, untrusted counters.

This wrapper stays outside bwrap. Its OIDC credentials are never mounted or passed
into the sandbox. Telemetry cannot change the command's exit status. No raw logs,
process arguments, environment values or candidate paths are published.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import signal
import stat
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ORIGIN = "https://ci-data.taucetiproject.org"
PHASES = {
    "build",
    "audit-setup",
    "audit-module-system",
    "lint-env",
    "lint-style",
    "declaration-names",
    "stage-cache",
    "end",
}


def stamp(value):
    import datetime as dt

    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def bounded_file(path, limit=65536, offset=0):
    """Candidate can replace telemetry paths: reject links, pipes and oversized reads."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular telemetry file")
        os.lseek(fd, offset if info.st_size >= offset else 0, os.SEEK_SET)
        return os.read(fd, limit), info.st_ino, os.lseek(fd, 0, os.SEEK_CUR)
    finally:
        os.close(fd)


class Counters:
    def __init__(self, root):
        self.root, self.offset, self.inode = Path(root), 0, None
        self.completed, self.failed, self.last_module, self.phase = 0, 0, None, "starting"
        self.last_progress = None
        self.pending = b""

    def sample(self, clock=time.time):
        before = (self.completed, self.failed, self.phase)
        try:
            data, _, _ = bounded_file(self.root / "phases.tsv")
            names = [line.split("\t", 1)[0] for line in data.decode(errors="replace").splitlines()]
            self.phase = next((v for v in reversed(names) if v in PHASES), "unknown")
        except (OSError, ValueError):
            pass
        try:
            data, inode, offset = bounded_file(self.root / "lake-build.log", 262144, self.offset)
            if self.inode != inode or offset < self.offset:
                self.offset, self.inode, self.pending = 0, inode, b""
                self.completed, self.failed = 0, 0
                data, _, offset = bounded_file(self.root / "lake-build.log", 262144)
            self.offset = offset
            lines = (self.pending + data).split(b"\n")
            self.pending = lines.pop()[-4096:]
            for raw in lines:
                line = re.sub(r"\x1b\[[0-9;]*m", "", raw[:4096].decode(errors="replace"))
                found = re.search(r"(?:Built|Replayed|Building)\s+(TauCeti(?:\.[A-Za-z0-9_\']+){0,30})(?:\s|$)", line)
                if found and len(found[1]) <= 300:
                    self.last_module = found[1]
                    if re.search(r"Built|Replayed", line):
                        self.completed += 1
                if re.match(r"^✖\s", line):
                    self.failed += 1
            self.completed, self.failed = min(10000000, self.completed), min(10000000, self.failed)
        except (OSError, ValueError):
            pass
        if before != (self.completed, self.failed, self.phase):
            self.last_progress = stamp(clock())
        return {
            "phase": self.phase,
            "completed_modules": self.completed,
            "failed_modules": self.failed,
            "last_module": self.last_module,
            "last_progress_at": self.last_progress,
        }


def process_sample(parent):
    """Read CPU counters and comm only for descendants; never retain cmdline/env."""
    rows = {}
    for path in list(Path("/proc").iterdir())[:100000]:
        if not path.name.isdigit():
            continue
        try:
            raw = (path / "stat").read_text()
            end = raw.rindex(")")
            fields = raw[end + 2 :].split()
            rows[int(path.name)] = (
                int(fields[1]),
                raw[raw.index("(") + 1 : end],
                int(fields[11]) + int(fields[12]),
                fields[19],
            )
        except (OSError, ValueError, IndexError):
            continue
    descendants, frontier = set(), {parent}
    for _ in range(32):
        next_ids = {pid for pid, row in rows.items() if row[0] in frontier} - descendants
        if not next_ids:
            break
        descendants.update(next_ids)
        frontier = next_ids
        if len(descendants) > 4096:
            return None, {}
    return sum(rows[p][1] == "lean" for p in descendants), {
        (p, rows[p][3]): rows[p][2] / os.sysconf("SC_CLK_TCK") for p in descendants
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(url, token, value=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(value).encode() if value is not None else None,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "TauCeti/live-progress",
        },
    )
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=5) as response:
        return json.loads(response.read(16384))


def report(child, args, stop):
    session, sequence, counters, cpu = str(uuid.uuid4()), 0, Counters(args.telemetry), {}
    token, refresh = None, 0
    while True:
        try:
            now = time.time()
            if now >= refresh:
                url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
                parsed = urllib.parse.urlsplit(url)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or not parsed.hostname.endswith(".actions.githubusercontent.com")
                ):
                    raise ValueError("unexpected OIDC request origin")
                token = request(
                    url + "&audience=tauceti-operations-progress", os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
                )["value"]
                refresh = now + 120
            sequence += 1
            active, sample = process_sample(child.pid)
            cpu.update(sample)
            # Bound process receipt history. Missing CPU evidence remains unknown.
            total = round(sum(cpu.values()), 2) if len(cpu) <= 100000 else None
            if total is None:
                cpu.clear()
            value = dict(
                schema="tauceti.operations.progress/v1",
                session=session,
                sequence=sequence,
                observed_at=stamp(now),
                source_revision=os.environ.get("CI_RESOURCE_WORKFLOW_SHA", "")[:40],
                active_compilers=active,
                cpu_seconds=total,
                **counters.sample(),
            )
            request(ORIGIN + "/ingest/operations/progress", token, value)
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            pass
        if stop.wait(15):
            return


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a sandbox launcher command is required")
    child = subprocess.Popen(command)
    stop = threading.Event()
    # Forward cancellation to the existing launcher; reporter failure never changes its result.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda received, _frame: child.send_signal(received) if child.poll() is None else None)
    try:
        try:
            threading.Thread(target=report, args=(child, args, stop), daemon=True).start()
        except RuntimeError:
            pass
        return child.wait()
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
