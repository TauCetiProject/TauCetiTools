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
import bors_scope_approval

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
    if not isinstance(record, dict) or set(record) not in (FIELDS, FIELDS | {"approval"}) or record.get("schema") != SCHEMA:
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
    if "approval" in record:
        approval = record["approval"]
        if payload["context"] != "scope" or payload["state"] != "success" or not isinstance(approval, dict):
            raise ValueError("scope approval applies only to a successful scope status")
        if (approval.get("head") != record["sha"] or not isinstance(approval.get("base"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", approval["base"]) or approval["base"] == "0" * 40):
            raise ValueError("invalid scope approval revision")
        if record["pr"] is not None:
            if set(approval) != {"head", "base", "pr"} or approval["pr"] != record["pr"] or type(approval["pr"]) is not int:
                raise ValueError("scope approval PR differs from status identity")
        else:
            if (set(approval) != {"head", "base", "batch_id", "paths"}
                    or type(approval["batch_id"]) is not int or positive(approval["batch_id"]) != approval["batch_id"]):
                raise ValueError("invalid scope approval batch")
            paths = approval["paths"]
            if not isinstance(paths, list) or not 0 < len(paths) <= 4096:
                raise ValueError("invalid scope approval paths")
            for path in paths:
                if (not isinstance(path, str) or not path or len(path) > 4096 or path.startswith("/")
                        or any(c in path for c in "\\\n\r\0") or any(part in ("", ".", "..") for part in path.split("/"))):
                    raise ValueError("invalid relative scope approval path")
            if paths != sorted(set(paths)) or sum(len(path.encode()) for path in paths) > 512 * 1024:
                raise ValueError("scope approval paths must be bounded, unique and sorted")
    return record


def record_intent(sha, context, state, description, pr=None, base_ref=None, *, approval=None):
    repo, run, attempt = source()
    record = dict(schema=SCHEMA, repo=repo, sha=sha, run_id=run, attempt=attempt,
                           pr=positive(pr) if pr is not None else None, base_ref=base_ref,
                           payload=dict(context=context, state=state, description=description[:140],
                                        target_url=f"https://github.com/{repo}/actions/runs/{run}"))
    if approval is not None:
        record["approval"] = approval
    validate(record)
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
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
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
    return current


def scope_approved(api, record):
    approval = record.get("approval")
    if approval is None:
        return True
    if record["repo"] != bors_scope_approval.REPO:
        return False
    def read(path):
        endpoint = path if path.startswith("/") else f"repos/{record['repo']}/{path}"
        return api.request("GET", endpoint)
    try:
        return bors_scope_approval.verify(
            bors_scope_approval.observation(approval.get("batch_id")), approval["head"], approval["base"],
            pr=approval.get("pr"), batch_id=approval.get("batch_id"), combined_paths=approval.get("paths"), api=read)
    except (KeyError, TypeError, ValueError) as error:
        print(f"ci-status: scope approval unavailable: {type(error).__name__}", file=sys.stderr)
        return False


def status_snapshot(api, record):
    path = f"repos/{record['repo']}/commits/{record['sha']}/statuses?per_page=100"
    rows = api.request("GET", path, conditional=True, read=False)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise APIError("GET", path, 503, {"message": "invalid commit status history"})
    latest = {}
    # GitHub returns newest first. Only the newest status for each exact context
    # governs it; an older row from a newer run must not override that ordering.
    for row in rows:
        context = row.get("context")
        if isinstance(context, str) and context in CONTEXTS and context not in latest:
            latest[context] = row
    return latest, len(rows) < 100


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing status attempt timestamp")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed


def newer_status(record, snapshot, started_at):
    latest, complete = snapshot
    row = latest.get(record["payload"]["context"])
    if row is None:
        return False if complete else None
    url = row.get("target_url")
    pattern = rf"https://github\.com/{re.escape(record['repo'])}/actions/runs/([1-9][0-9]*)"
    match = re.fullmatch(pattern, url) if isinstance(url, str) else None
    if not match or int(match[1]) <= record["run_id"]:
        return False
    try:
        # A lower-numbered run can have a later rerun. Its current attempt is
        # allowed to replace statuses published before that attempt began.
        return timestamp(row.get("created_at")) > timestamp(started_at)
    except ValueError:
        return None


def binding(record):
    return tuple(record[key] for key in ("repo", "run_id", "attempt", "sha", "pr", "base_ref"))


def publish(records, *, max_wait=600):
    # The client must not wait inside a status POST: after a quota reset the PR
    # or run attempt may have changed. Wait here, then refresh the identity fence.
    api = GitHub(max_wait=0)
    deadline = time.monotonic() + max_wait
    fenced = {}
    statuses = {}
    failures = 0
    try:
        for path, record in records:
            key = binding(record)
            for attempt in range(4):
                try:
                    if key not in fenced:
                        current = fence(api, record)
                        if not current:
                            print(f"ci-status: obsolete {record['payload']['context']} intent skipped")
                            path.unlink(missing_ok=True)
                            break
                        fenced[key] = current.get("run_started_at")
                    if key not in statuses:
                        statuses[key] = status_snapshot(api, record)
                    superseded = newer_status(record, statuses[key], fenced[key])
                    if superseded is None:
                        print(f"ci-status: recent history cannot establish {record['payload']['context']} ordering; retaining intent", file=sys.stderr)
                        failures += 1
                        break
                    if superseded:
                        print(f"ci-status: newer run already published {record['payload']['context']}; obsolete intent skipped")
                        path.unlink(missing_ok=True)
                        break
                    payload = record["payload"]
                    approval_ok = scope_approved(api, record)
                    if not approval_ok:
                        # Clear an older green waiver on the same SHA. Revocation
                        # cannot be represented by silently skipping publication.
                        payload = dict(payload, state="failure", description="no valid current bors scope approval")
                    api.request("POST", f"repos/{record['repo']}/statuses/{record['sha']}",
                                payload, idempotent=True)
                    path.unlink(missing_ok=True)
                    if not approval_ok:
                        failures += 1
                    print(f"ci-status: posted {payload['context']}={payload['state']}")
                    break
                except Deferred as error:
                    delay = max(1, error.until - time.time())
                except APIError as error:
                    if error.status != 0 and error.status < 500:
                        print(f"ci-status: {error}", file=sys.stderr)
                        failures += 1
                        break
                    delay = 2 ** (attempt + 1)
                except (OSError, RuntimeError) as error:
                    print(f"ci-status: publication dependency unavailable: {type(error).__name__}", file=sys.stderr)
                    delay = 2 ** (attempt + 1)
                if attempt == 3 or time.monotonic() + delay > deadline:
                    print(f"ci-status: publication deferred; retaining {path.name}", file=sys.stderr)
                    failures += 1
                    break
                reset = dt.datetime.fromtimestamp(time.time() + delay, dt.timezone.utc).isoformat()
                print(f"ci-status: waiting until {reset}; will refresh run/PR identity before retry", flush=True)
                time.sleep(delay)
                fenced.clear()
                statuses.clear()
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
    post.add_argument("--scope-approval-base")
    approval_kind = post.add_mutually_exclusive_group()
    approval_kind.add_argument("--scope-approval-pr", type=int)
    approval_kind.add_argument("--scope-approval-batch", type=int)
    post.add_argument("--scope-paths-file", type=Path)
    resume = commands.add_parser("resume")
    resume.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "post":
            approval = None
            if args.scope_approval_base is not None:
                approval = {"head": args.sha, "base": args.scope_approval_base}
                if args.scope_approval_pr is not None:
                    approval["pr"] = args.scope_approval_pr
                elif args.scope_approval_batch is not None and args.scope_paths_file is not None:
                    if args.scope_paths_file.stat().st_size > 512 * 1024:
                        raise ValueError("scope paths file exceeds bound")
                    approval.update(batch_id=args.scope_approval_batch,
                                    paths=sorted(set(args.scope_paths_file.read_text().splitlines())))
                else:
                    raise ValueError("scope approval requires a PR or batch with paths")
            elif any(value is not None for value in (args.scope_approval_pr, args.scope_approval_batch, args.scope_paths_file)):
                raise ValueError("scope approval requires an exact base")
            path, record = record_intent(args.sha, args.context, args.state, args.description, args.pr, args.base_ref,
                                         approval=approval)
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
