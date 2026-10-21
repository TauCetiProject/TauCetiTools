#!/usr/bin/env python3
"""Durable commit-status publication; retries never rebuild or execute PR content."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from github_api import APIError, Deferred, GitHub

SCHEMA = "tauceti-ci.pr-status-intent/v1"
CONTEXTS = {"build", "bump-guard", "scope"}
STATES = {"error", "failure", "pending", "success"}
FIELDS = {"schema", "repo", "sha", "run_id", "attempt", "pr", "base_ref", "payload"}
PAYLOAD_FIELDS = {"state", "context", "description", "target_url"}


def positive(value):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
        raise ValueError("expected a positive numeric identity")
    return int(value)


def source():
    repository = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid workflow repository")
    run = positive(os.environ.get("CI_STATUS_SOURCE_RUN_ID") or os.environ["GITHUB_RUN_ID"])
    attempt = positive(os.environ.get("CI_STATUS_SOURCE_ATTEMPT") or os.environ["GITHUB_RUN_ATTEMPT"])
    return repository, run, attempt


def validate(record):
    repo, run, attempt = source()
    if not isinstance(record, dict) or set(record) != FIELDS or record.get("schema") != SCHEMA:
        raise ValueError("unsupported status intent schema")
    if record["repo"] != repo or record["run_id"] != run or record["attempt"] != attempt:
        raise ValueError("status intent differs from the selected source run")
    if type(record["run_id"]) is not int or type(record["attempt"]) is not int:
        raise ValueError("invalid status run identity")
    if not isinstance(record["sha"], str) or not re.fullmatch(r"[0-9a-f]{40}", record["sha"]) or record["sha"] == "0" * 40:
        raise ValueError("invalid status commit SHA")
    if (record["pr"] is None) != (record["base_ref"] is None):
        raise ValueError("PR and captured base ref must be supplied together")
    if record["pr"] is not None:
        if type(record["pr"]) is not int or positive(record["pr"]) != record["pr"]:
            raise ValueError("invalid PR identity")
        ref = record["base_ref"]
        if not isinstance(ref, str) or not ref or len(ref) > 1024 or any(ord(c) < 32 for c in ref):
            raise ValueError("invalid captured PR base ref")
    payload = record["payload"]
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_FIELDS:
        raise ValueError("invalid status payload")
    if payload["context"] not in CONTEXTS or payload["state"] not in STATES:
        raise ValueError("unsupported status context/state")
    description = payload["description"]
    if not isinstance(description, str) or len(description) > 140 or any(ord(c) < 32 for c in description):
        raise ValueError("invalid status description")
    if payload["target_url"] != f"https://github.com/{repo}/actions/runs/{run}":
        raise ValueError("status target does not identify the selected source run")
    return record


def record_intent(sha, context, state, description, pr=None, base_ref=None):
    repo, run, attempt = source()
    record = validate(dict(schema=SCHEMA, repo=repo, sha=sha, run_id=run, attempt=attempt,
                           pr=positive(pr) if pr is not None else None, base_ref=base_ref,
                           payload=dict(context=context, state=state, description=description,
                                        target_url=f"https://github.com/{repo}/actions/runs/{run}")))
    directory = Path(os.environ["RUNNER_TEMP"]) / "tauceti-status-intents"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{context}.json"
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, prefix=".intent-", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(record, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    return path, record


def load_intents(directory):
    directory = Path(directory)
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("status intent directory must be a real directory")
    paths = list(directory.iterdir())
    if len(paths) > 16:
        raise ValueError("too many status intent files")
    records = []
    contexts = set()
    # Validate every file before any write. An artifact cannot smuggle unrelated
    # statuses or bind one context to two competing payloads.
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            raise ValueError("invalid status intent file")
        if path.suffix != ".json":
            raise ValueError("unexpected status intent file")
        record = validate(json.loads(path.read_text()))
        context = record["payload"]["context"]
        if path.name != f"{context}.json" or context in contexts:
            raise ValueError("duplicate or mismatched status intent context")
        contexts.add(context)
        records.append((path, record))
    return records


def fence(api, record):
    repo, run, attempt = record["repo"], record["run_id"], record["attempt"]
    current = api.request("GET", f"repos/{repo}/actions/runs/{run}")
    if (not isinstance(current, dict) or current.get("id") != run
            or current.get("run_attempt") != attempt or current.get("conclusion") == "cancelled"):
        return False
    if record["pr"] is not None:
        pull = api.request("GET", f"repos/{repo}/pulls/{record['pr']}")
        if (not isinstance(pull, dict) or (pull.get("head") or {}).get("sha") != record["sha"]
                or (pull.get("base") or {}).get("ref") != record["base_ref"]):
            return False
    return True


def binding(record):
    return tuple(record[key] for key in ("repo", "run_id", "attempt", "sha", "pr", "base_ref"))


def publish(records, *, max_wait=600):
    # The client must not wait inside a status POST: after a quota reset the PR
    # or run attempt may have changed. Wait here, then refresh the identity fence.
    api = GitHub(max_wait=0)
    deadline = time.monotonic() + max_wait
    fenced = set()
    failures = 0
    try:
        for path, record in records:
            key = binding(record)
            for attempt in range(4):
                try:
                    if key not in fenced:
                        if not fence(api, record):
                            print(f"ci-status: obsolete {record['payload']['context']} intent skipped")
                            path.unlink(missing_ok=True)
                            break
                        fenced.add(key)
                    api.request("POST", f"repos/{record['repo']}/statuses/{record['sha']}",
                                record["payload"], idempotent=True)
                    path.unlink(missing_ok=True)
                    print(f"ci-status: posted {record['payload']['context']}={record['payload']['state']}")
                    break
                except Deferred as error:
                    delay = max(1, error.until - time.time())
                except APIError as error:
                    if error.status != 0 and error.status < 500:
                        print(f"ci-status: {error}", file=sys.stderr)
                        failures += 1
                        break
                    delay = 2 ** (attempt + 1)
                if attempt == 3 or time.monotonic() + delay > deadline:
                    print(f"ci-status: publication deferred; retaining {path.name}", file=sys.stderr)
                    failures += 1
                    break
                reset = dt.datetime.fromtimestamp(time.time() + delay, dt.timezone.utc).isoformat()
                print(f"ci-status: waiting until {reset}; will refresh run/PR identity before retry", flush=True)
                time.sleep(delay)
                fenced.clear()
        return failures == 0
    finally:
        api.report()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    post = commands.add_parser("post")
    post.add_argument("--sha", required=True)
    post.add_argument("--context", required=True, choices=sorted(CONTEXTS))
    post.add_argument("--state", required=True, choices=sorted(STATES))
    post.add_argument("--description", required=True)
    post.add_argument("--pr")
    post.add_argument("--base-ref")
    post.add_argument("--record-only", action="store_true")
    resume = commands.add_parser("resume")
    resume.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "post":
            path, record = record_intent(args.sha, args.context, args.state, args.description, args.pr, args.base_ref)
            if args.record_only:
                return 0
            records = [(path, record)]
        else:
            records = load_intents(args.directory)
        if not records:
            print("ci-status: no pending status intents")
            return 0
        max_wait = float(os.environ.get("CI_STATUS_MAX_WAIT", "3700" if args.command == "resume" else "600"))
        if not 0 <= max_wait <= 3700:
            raise ValueError("CI_STATUS_MAX_WAIT must be between 0 and 3700 seconds")
        return 0 if publish(records, max_wait=max_wait) else 1
    except (KeyError, OSError, ValueError, RuntimeError) as error:
        print(f"ci-status: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
