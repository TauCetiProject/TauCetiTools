#!/usr/bin/env python3
"""Summarise recognized Lake noise; preserve all other output and command status."""
from __future__ import annotations

import argparse
import re
import sys

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[0-9:.]+Z ")
ARTIFACT = re.compile(r"info: [^\r\n]+: downloaded artifact [0-9a-f]{16,64}$")
REVISION = re.compile(r"info: [^\r\n]+: downloading build outputs for revision ([0-9a-f]{40})$")
BAD_IMPORT = re.compile(r"error: [A-Za-z0-9_/.\-']+\.lean: bad import '[A-Za-z0-9_.']+'$")
TARGET = re.compile(r"- [A-Za-z_][A-Za-z0-9_'.]*(?::[A-Za-z0-9_'.]+)?$")
RUNNING = re.compile(r"✖ \[\d+/\d+\] Running ([A-Za-z_][A-Za-z0-9_.']*)$")
CANCELED = re.compile(r"⊘ \[\d+/\d+\] Canceled [A-Za-z_][A-Za-z0-9_.']*$")
HEADER = "Some required targets logged failures:"
SAMPLED = "TAUCETI-DIAGNOSTICS-SAMPLED"
SAMPLES = 5


def filter_log(source, out, mode):
    artifacts = revisions = imports = targets = 0
    continuation = 0
    in_targets = partial = False
    last_revision = None
    pending = None
    canceled = 0
    while True:
        raw = source.readline(65536)
        if not raw:
            break
        # Long/unfamiliar records pass through in bounded chunks unchanged.
        if partial or not raw.endswith("\n") and len(raw) == 65536:
            if pending:
                out.write(pending[0])
                pending = None
            out.write(raw)
            partial = not raw.endswith("\n")
            continuation = 0
            continue
        line = STAMP.sub("", ANSI.sub("", raw.rstrip("\r\n").lstrip("\ufeff")))
        if pending:
            previous, module = pending
            if not (BAD_IMPORT.fullmatch(line) and line.startswith("error: " + module.replace(".", "/") + ".lean:")) or imports < SAMPLES:
                out.write(previous)
            pending = None
        if mode == "cache":
            if ARTIFACT.fullmatch(line):
                artifacts += 1
                continuation = 2
                continue
            revision = REVISION.fullmatch(line)
            if revision:
                revisions += 1
                last_revision = revision[1]
                continuation = 2
                continue
            if continuation and line.startswith(("  local path: ", "  remote URL: ")):
                continuation -= 1
                continue
            continuation = 0
        else:
            running = RUNNING.fullmatch(line)
            if running:
                pending = (raw, running[1])
                continue
            if CANCELED.fullmatch(line):
                canceled += 1
                if canceled <= SAMPLES:
                    out.write(raw)
                continue
            if BAD_IMPORT.fullmatch(line):
                imports += 1
                if imports <= SAMPLES:
                    out.write(raw)
                continue
            if line == HEADER:
                in_targets = True
                out.write(raw)
                continue
            if in_targets and TARGET.fullmatch(line):
                targets += 1
                if targets <= SAMPLES:
                    out.write(raw)
                continue
            if in_targets:
                if targets > SAMPLES:
                    out.write(f"{SAMPLED} failed-targets total={targets} shown={SAMPLES}; full list in raw build artifact\n")
                targets = 0
                in_targets = False
        out.write(raw)
    if pending:
        out.write(pending[0])
    if mode == "cache":
        out.write(f"Lake cache: downloaded artifacts={artifacts}, revision lookups={revisions}")
        if last_revision:
            out.write(f", last revision lookup={last_revision}")
        out.write("\n")
    else:
        if canceled > SAMPLES:
            out.write(f"Lake canceled targets: total={canceled}, shown={SAMPLES}\n")
        if targets > SAMPLES:
            out.write(f"{SAMPLED} failed-targets total={targets} shown={SAMPLES}; full list in raw build artifact\n")
        if imports > SAMPLES:
            out.write(f"Lake dependency failures: bad imports={imports}, shown={SAMPLES}; full log in raw build artifact\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["cache", "build"])
    parser.add_argument("file", nargs="?")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True, errors="surrogateescape")
    sys.stdin.reconfigure(errors="surrogateescape")
    if args.file:
        with open(args.file, encoding="utf-8", errors="surrogateescape") as source:
            filter_log(source, sys.stdout, args.mode)
    else:
        filter_log(sys.stdin, sys.stdout, args.mode)


if __name__ == "__main__":
    main()
