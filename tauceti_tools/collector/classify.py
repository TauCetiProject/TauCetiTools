"""Classify why a TauCeti CI job failed, from its first failing step and (when fetched) its log.

The classes are deliberately coarse and stable, because analyses group by them over months.
Add a rule rather than renaming a class; `CLASSIFIER_VERSION` records which rule set produced a label.
"""

from __future__ import annotations

import os

import re

CLASSIFIER_VERSION = 3

# (class, regex on the first failing step's name). First match wins.
STEP_RULES: list[tuple[str, str]] = [
    ("infra-report", r"^Report|^Post "),
    ("policy", r"^Reject option overrides and heartbeat instrumentation$"),
    ("policy", r"guard|Scope|shim|Attest|Validate the Lake-pin|Normalise|bump"),
    ("dot-notation", r"Dot-notation"),
    ("toolchain-incompat", r"Build the trusted environment-lint driver|watchdog toolchain"),
    ("infra-cache", r"Mathlib|Lake cache|oleans|Lake artifact|cache"),
    ("infra-setup", r"Install|Raise vm|Checkout|Set up|Resolve PR head"),
]

# (class, regex on log lines) for failures inside the sandboxed build, where build, audits and lints
# may share one step. Checked in order against the failing step's extracted error lines.
LOG_RULES: list[tuple[str, str]] = [
    # Exit 137 also means an external SIGKILL (for example, OOM). The historical
    # Lean wrapper calls it a deadline expiry even when the process ran for seconds.
    ("process-killed", r"(?:^|\s)Killed(?:\s+timeout\b|\s*$)|\bLean exited with code 137\b"),
    ("timeout", r"\btimed out\b|\bexceeded .* (?:deadline|wall-clock limit)\b"),
    ("infra-cache", r"failed to download artifact|Transferred a partial file"),
    ("lint-env", r"LINT-ENV: FAIL"),
    ("audit-axioms", r"[Aa]xiom"),
    ("audit-duplicates", r"[Dd]uplicate declaration"),
    ("audit-module-system", r"module system|isModule"),
    ("lint-style", r"ERR_[A-Z]+|style lint|lint-style"),
    ("lean-error", r"\.lean:\d+:\d+: error|^error: .*\.lean"),
]

ANSI = re.compile(r"\x1b\[[0-9;]*m")
TIMESTAMP = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ")
INTERESTING = re.compile(r"error|FAIL|✖|timed out|Killed|##\[error\]", re.IGNORECASE)
NON_DIAGNOSTIC = re.compile(r"^(?:✔(?:\s|$)|\+\+?\s)")


def excerpt(log: str, start: str | None = None, end: str | None = None, limit: int = 30) -> list[str]:
    """The error lines of a job log, restricted to lines timestamped within [start, end] (the failing
    step; ISO strings compare correctly as text at second precision). Lines GitHub echoes from the
    step's own script (rendered in cyan, `ESC[36;1m`) are skipped: they quote `echo "::error::..."`
    source, not actual errors."""
    out: list[str] = []
    lo = start[:19] if start else None
    hi = end[:19] if end else None
    for raw in log.splitlines():
        if "\x1b[36;1m" in raw:
            continue
        raw = raw.lstrip("﻿")
        if lo and hi and TIMESTAMP.match(raw) and not (lo <= raw[:19] <= hi):
            continue
        line = ANSI.sub("", TIMESTAMP.sub("", raw)).strip()
        if (line and not NON_DIAGNOSTIC.match(line) and INTERESTING.search(line)
                and "Process completed with exit code" not in line):
            out.append(line[:300])
            if len(out) >= limit:
                break
    return out


def classify(conclusion: str | None, failed_step: str | None, lines: list[str] | None) -> str | None:
    if conclusion in (None, "success", "skipped", "neutral"):
        return None
    if conclusion == "cancelled":
        return "cancelled"
    if conclusion == "timed_out":
        return "timeout"
    step = failed_step or ""
    # The sandboxed build: one step, or (pr-build's phases) one step per phase "... under bwrap".
    if re.search(r"Build exact candidate|under bwrap|lake build|^Build$", step):
        # Old records contain successful module names such as KilledByRank and
        # ErrorBound. Filter them here too when rebuilding historical classifications.
        diagnostics = [ANSI.sub("", line).strip() for line in (lines or [])]
        diagnostics = [line for line in diagnostics if not NON_DIAGNOSTIC.match(line)]
        for cls, rx in LOG_RULES:
            if any(re.search(rx, line) for line in diagnostics):
                return cls
        return "build-unknown" if lines is None else "lean-error"
    for cls, rx in STEP_RULES:
        if re.search(rx, step):
            return cls
    return "other"
