#!/usr/bin/env python3
"""Run the sandboxed `lake build`, passing its output through, and report thread exhaustion.

Usage: thread_exhaustion.py COMMAND [ARGS...]

The command's combined output is copied to stdout unchanged and its exit status is returned,
except that a failed build whose only cause is the host running out of threads exits with
`EXIT_THREADS` (75, EX_TEMPFAIL). pr-build.yml turns that status into an infrastructure `build`
description, which the CI recovery pass retries once (ci_recovery.py).

On 23 and 24 Nov 2026 the native CI guests on one host shared a task limit that three overlapping
cache restores exhausted. Lake then aborted ("failed to create thread: Resource temporarily
unavailable", exit 134), or leantar could not start its thread pool (rayon `ThreadPoolBuildError`
with OS error 11, exit 101) and Lake marked those modules' `Unpacking` jobs failed.

The match is narrow. The build must fail without this process having been asked to stop, its
output must contain one of those two exact errors, and every Lake job reported with anything but
`✔` (failed `✖`, or with a warning `⚠` or information `ℹ` log, which `--iofail` also fails) must be
an `Unpacking` job. The verdict comes from this trusted process's own reading of the stream, not
from a file in the candidate-writable tree. Candidate code can print only from a module it
compiles, and Lake reports that output under the module's `Building` job with a non-`✔` mark, so
printing the error text never turns a candidate's failure into an infrastructure one. For the
same reason a Lean process that aborts with the thread error is an ordinary build failure.
"""
import os
import re
import signal
import subprocess
import sys

EXIT_THREADS = 75
CHUNK = 65536
# A line longer than this is classified by its first `LINE_LIMIT` bytes only: a signature cut
# there is missed, which errs towards an ordinary failure.
LINE_LIMIT = 1 << 20

# Lake (a Lean runtime abort) and leantar (rayon), word for word as they appeared in PR CI logs.
SIGNATURES = (
    re.compile(rb"lean::exception: failed to create thread: Resource temporarily unavailable"),
    re.compile(rb"ThreadPoolBuildError \{ kind: IOError\(Os \{ code: 11, kind: WouldBlock"),
)
ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
# A Lake job line: its status mark, `[done/total]`, then the job's verb and target.
JOB = re.compile(rb"^(\S+) \[[0-9]+/[0-9]+\] (\S+)")
PASSED = "✔".encode()


class Verdict:
    """What the build's output has shown so far."""

    def __init__(self):
        self.signature = False
        self.other_failure = False

    def read(self, line, *, start=True):
        """Classify one line, or (`start` False) a later piece of an over-long line."""
        line = ANSI.sub(b"", line)
        if not self.signature and any(pattern.search(line) for pattern in SIGNATURES):
            self.signature = True
        job = JOB.match(line) if start else None
        if job and job[1] != PASSED and job[2] != b"Unpacking":
            self.other_failure = True

    def status(self, code, cancelled=False):
        """The exit status to report for the command's own status `code`."""
        if code != 0 and not cancelled and self.signature and not self.other_failure:
            return EXIT_THREADS
        return code


def exit_status(returncode):
    """A subprocess return code as a shell reports it (a signal N is 128 + N)."""
    return 128 - returncode if returncode < 0 else returncode


class Output:
    """Unbuffered writes to stdout; after one fails, the rest are dropped, so a closed reader
    never changes the build's status."""

    def __init__(self, fd=1):
        self.fd = fd

    def write(self, data):
        while data and self.fd is not None:
            try:
                data = data[os.write(self.fd, data):]
            except InterruptedError:
                continue
            except OSError:
                self.fd = None


def main(argv=None):
    command = sys.argv[1:] if argv is None else argv
    if not command:
        print("usage: thread_exhaustion.py COMMAND [ARGS...]", file=sys.stderr)
        return 2
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    cancelled = []

    # A cancelled run signals this process; the build sees the signal as before, and a cancelled
    # build is never an infrastructure failure.
    def forward(received, _frame):
        cancelled.append(received)
        if child.poll() is None:
            child.send_signal(received)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, forward)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    verdict, out = Verdict(), Output()
    source = child.stdout.fileno()
    pending, start = b"", True
    while True:
        try:
            data = os.read(source, CHUNK)
        except InterruptedError:
            continue
        if not data:
            break
        out.write(data)
        pending += data
        *lines, pending = pending.split(b"\n")
        for line in lines:
            verdict.read(line, start=start)
            start = True
        if len(pending) > LINE_LIMIT:
            verdict.read(pending[:LINE_LIMIT], start=start)
            pending, start = b"", False
    if pending:
        verdict.read(pending, start=start)
    status = verdict.status(exit_status(child.wait()), cancelled=bool(cancelled))
    if status == EXIT_THREADS:
        out.write(b"::error::the CI host ran out of threads during the build; this is an infrastructure failure\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
