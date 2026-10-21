#!/usr/bin/env python3
"""Post one terminal batch status, with bounded isolated-publisher acquisition.

Executed only from the immutable workflow checkout. The finalizer starts beside
the publisher after the full sandbox audit; it never consumes candidate files.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from github_api import APIError, Deferred, GitHub

from bors_observation import MAX_BYTES, REPO, observation, sha
from bors_train_identity import candidate_ref

PUBLISHER = "publish-merge-group-cache / publish"
CONFIRM = "Confirm the exact revision map on the public endpoint"
ACQUISITION_SECONDS = 300
PUBLICATION_SECONDS = 1200
POST_RETRY_SECONDS = 600
MACHINE_LABEL = "nscloud-ubuntu-24.04-amd64-4x8-with-features;job.priority=0"
API_ROOT = "https://api.github.com/repos/" + REPO


_client = None


def post_retry_seconds():
    return max(0, min(3700, int(os.environ.get("TAUCETI_GITHUB_MAX_WAIT", POST_RETRY_SECONDS))))


def api(path, payload=None):
    """Use the shared credential-aware quota client; never repeat ambiguous reruns."""
    global _client
    if _client is None:
        # Publication owns waits so it can revalidate the run after sleeping.
        _client = GitHub(max_wait=0, jitter=2)
    return _client.request("GET" if payload is None else "POST", "repos/" + REPO + path,
                           payload, conditional=payload is None,
                           idempotent=path.startswith("/statuses/"))


def publication_job(data):
    """Use only this attempt's unique trusted publisher, never an older attempt."""
    jobs = data["jobs"]
    if data["total_count"] != len(jobs) or len(jobs) > 100:
        raise ValueError("incomplete jobs response")
    matches = [job for job in jobs if job["name"] == PUBLISHER]
    if len(matches) > 1:
        raise ValueError("ambiguous publisher")
    return matches[0] if matches else None


def isolated_runner(data, job, run_id):
    """Require the selected nonprivileged labels and a different sandbox runner."""
    labels = {MACHINE_LABEL, f"namespace-features:github.run-id={run_id}"}
    sandbox = [item for item in data["jobs"] if item["name"] == "sandboxed-build"]
    return (set(job.get("labels", [])) == labels and bool(job.get("runner_name"))
            and len(sandbox) == 1 and bool(sandbox[0].get("runner_name"))
            and job["runner_name"] != sandbox[0]["runner_name"])


def wait_publication(run_id, attempt, *, fetch=api, clock=time.monotonic, sleep=time.sleep):
    """Return success only after the publisher and its exact-map check complete."""
    start = clock()
    previous = None
    acquired = False
    while True:
        elapsed = clock() - start
        try:
            data = fetch(f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
            job = publication_job(data)
        except Deferred:
            raise
        except (OSError, APIError, ValueError, KeyError, TypeError) as error:
            # A transient API failure gets a bounded retry, never a green result.
            print(f"::warning::publisher observation unavailable: {type(error).__name__}", flush=True)
            job = None
        if job is not None:
            current = (job["status"], job.get("conclusion"), job.get("runner_name"))
            if current != previous:
                print("Publisher evidence: " + json.dumps({key: job.get(key) for key in (
                    "id", "status", "conclusion", "created_at", "started_at",
                    "completed_at", "runner_name", "labels")}), flush=True)
                previous = current
            if job["status"] == "completed" and not job.get("runner_name"):
                return "failure", "publisher completed without acquiring a runner; inspect run jobs and Namespace routing"
            if job.get("runner_name") and job["status"] in {"in_progress", "completed"}:
                if not isolated_runner(data, job, run_id):
                    return "failure", "publisher runner/labels are not isolated; inspect Namespace routing"
            if job["status"] == "completed":
                confirmed = any(step.get("name") == CONFIRM and step.get("conclusion") == "success"
                                for step in job.get("steps", []))
                if job.get("conclusion") == "success" and confirmed:
                    return "success", "sandboxed build passed; exact revision cache is public"
                return "failure", "build passed, but isolated publication/confirmation failed"
        if job is not None and job["status"] == "in_progress" and job.get("runner_name"):
            acquired = True
        if not acquired and elapsed >= ACQUISITION_SECONDS:
            return "failure", "publisher not acquired/observable in 5m; inspect run jobs and Namespace routing"
        if elapsed >= PUBLICATION_SECONDS:
            return "failure", "isolated publication exceeded 20m; inspect run jobs and Namespace capacity"
        sleep(30)


def terminal_state(env, *, wait=wait_publication, observe=observation):
    """Keep failed/canceled audits red and revalidate bors before a green status."""
    if env.get("BUILD_JOB_RESULT") != "success" or env.get("BUILD_STATE") != "success":
        return "failure", env.get("BUILD_DESCRIPTION") or "sandboxed merge-group build did not complete"
    if env.get("PUBLICATION_REQUIRED") == "true":
        state, description = wait(int(env["GITHUB_RUN_ID"]), int(env["GITHUB_RUN_ATTEMPT"]))
    else:
        state, description = "success", "sandboxed build passed (cache upload is not configured)"
    if state == "success" and env.get("BORS_STAGING") == "true":
        try:
            batch_id = int(env["BORS_BATCH_ID"])
            branch = env["BORS_BASE_REF"]
            for attempt in range(3):
                try:
                    data = observe(batch_id, branch)
                    break
                except OSError:
                    if attempt == 2:
                        raise
                    time.sleep(5 * (attempt + 1))
            candidate_ref(data, env["STATUS_SHA"], env["BORS_BASE_SHA"],
                          batch_id=batch_id, branch=branch)
        except (OSError, ValueError, KeyError, TypeError):
            return "failure", "bors generation is stale or its authority is unavailable; redispatch current batch"
    return state, description


def status_intent(env, payload):
    """A data-only record lets another trusted job publish without rebuilding."""
    intent = dict(schema="tauceti-ci.status-intent/v1", repo=REPO, revision=env["STATUS_SHA"],
                  run_id=int(env["GITHUB_RUN_ID"]), attempt=int(env["GITHUB_RUN_ATTEMPT"]), payload=payload)
    if env.get("BORS_STAGING") == "true":
        intent["bors"] = dict(batch_id=int(env["BORS_BATCH_ID"]), base_ref=env["BORS_BASE_REF"],
                              base_sha=env["BORS_BASE_SHA"])
    return intent


EVIDENCE_FIELDS = {"BUILD_JOB_RESULT", "BUILD_STATE", "BUILD_DESCRIPTION", "PUBLICATION_REQUIRED",
                   "BORS_STAGING", "BORS_BATCH_ID", "BORS_BASE_REF", "BORS_BASE_SHA"}


def finalization_intent(env):
    """Persist completed build evidence before publisher observation can exhaust quota."""
    evidence = {key: env.get(key, "") for key in EVIDENCE_FIELDS}
    evidence["BUILD_DESCRIPTION"] = evidence["BUILD_DESCRIPTION"][:140]
    return dict(schema="tauceti-ci.finalization-intent/v1", repo=REPO, revision=env["STATUS_SHA"],
                run_id=int(env["GITHUB_RUN_ID"]), attempt=int(env["GITHUB_RUN_ATTEMPT"]), evidence=evidence)


def validate_intent(intent):
    if (not isinstance(intent, dict) or intent.get("schema") not in {
            "tauceti-ci.status-intent/v1", "tauceti-ci.finalization-intent/v1"}
            or intent.get("repo") != REPO or not sha(intent.get("revision"))):
        raise ValueError("invalid status intent identity")
    for key in ("run_id", "attempt"):
        if type(intent.get(key)) is not int or intent[key] <= 0:
            raise ValueError("invalid status intent run identity")
    if intent["schema"] == "tauceti-ci.finalization-intent/v1":
        evidence = intent.get("evidence")
        if (set(intent) != {"schema", "repo", "revision", "run_id", "attempt", "evidence"}
                or not isinstance(evidence, dict) or set(evidence) != EVIDENCE_FIELDS
                or not all(isinstance(value, str) for value in evidence.values())
                or evidence["BUILD_JOB_RESULT"] not in {"success", "failure", "cancelled", "skipped"}
                or evidence["BUILD_STATE"] not in {"success", "failure", ""}
                or len(evidence["BUILD_DESCRIPTION"]) > 140
                or evidence["PUBLICATION_REQUIRED"] not in {"true", "false", ""}
                or evidence["BORS_STAGING"] not in {"true", "false", ""}):
            raise ValueError("invalid finalization evidence")
        if evidence["BORS_STAGING"] == "true":
            branch = evidence["BORS_BASE_REF"]
            if (not evidence["BORS_BATCH_ID"].isdigit() or int(evidence["BORS_BATCH_ID"]) <= 0
                    or not sha(evidence["BORS_BASE_SHA"]) or not 0 < len(branch) <= 200
                    or any(char.isspace() or ord(char) < 32 for char in branch)):
                raise ValueError("invalid bors finalization evidence")
        return intent
    payload = intent.get("payload", {})
    expected = {"schema", "repo", "revision", "run_id", "attempt", "payload"}
    if "bors" in intent:
        expected.add("bors")
        bors = intent["bors"]
        if (not isinstance(bors, dict) or set(bors) != {"batch_id", "base_ref", "base_sha"}
                or type(bors["batch_id"]) is not int or bors["batch_id"] <= 0
                or not sha(bors["base_sha"]) or not isinstance(bors["base_ref"], str)
                or not 0 < len(bors["base_ref"]) <= 200
                or any(char.isspace() or ord(char) < 32 for char in bors["base_ref"])):
            raise ValueError("invalid status intent bors identity")
    if (set(intent) != expected
            or not isinstance(payload, dict)
            or set(payload) != {"state", "context", "description", "target_url"}
            or payload.get("state") not in {"success", "failure"} or payload.get("context") != "build"
            or not isinstance(payload.get("description"), str) or len(payload["description"]) > 140
            or payload.get("target_url") != f"https://github.com/{REPO}/actions/runs/{intent['run_id']}"):
        raise ValueError("invalid status intent payload")
    return intent


def save_intent(intent, target):
    validate_intent(intent)
    if target:
        destination = Path(target)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(intent) + "\n")
        temporary.replace(destination)


def finalize(env, target, *, resume=False):
    save_intent(finalization_intent(env), target)
    deadline = time.monotonic() + post_retry_seconds()
    while True:
        try:
            state, description = terminal_state(env)
            break
        except Deferred as error:
            delay = max(1, error.until - time.time())
            if time.monotonic() + delay > deadline:
                print(f"::error::batch evidence unavailable: {error}; retained finalization intent can be resumed", file=sys.stderr)
                return 75
            print(f"::warning::{error}; retrying finalization evidence without rebuilding", flush=True)
            time.sleep(delay)
        except (OSError, APIError, ValueError, KeyError, TypeError) as error:
            state, description = "failure", "batch finalization failed: " + type(error).__name__
            break
    payload = dict(state=state, context="build", description=description[:140],
                   target_url=f"https://github.com/{REPO}/actions/runs/{env['GITHUB_RUN_ID']}")
    intent = status_intent(env, payload)
    save_intent(intent, target)
    return publish(intent, target=target, resume=resume)


def newer_build_status(intent):
    """One bounded newest-first read fences verdicts from later workflow runs."""
    rows = api(f"/commits/{intent['revision']}/statuses?per_page=100")
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("invalid commit status observation")
    pattern = re.compile(r"https://github\.com/" + re.escape(REPO) + r"/actions/runs/([1-9][0-9]*)")
    for row in rows:
        if not isinstance(row, dict) or row.get("context") != "build":
            continue
        target = row.get("target_url")
        match = pattern.fullmatch(target) if isinstance(target, str) else None
        if match and int(match.group(1)) > intent["run_id"]:
            return True
    return False


def discard_intent(target):
    if target:
        Path(target).unlink(missing_ok=True)


def publish(intent, *, target=None, resume=False):
    """Retry only publication, preserving the completed result and attempt fence."""
    validate_intent(intent)
    if intent["schema"] != "tauceti-ci.status-intent/v1":
        raise ValueError("pending finalization evidence must be checked before publication")
    payload = dict(intent["payload"])
    revision = intent["revision"]
    deadline = time.monotonic() + post_retry_seconds()
    while time.monotonic() < deadline:
        try:
            # This read remains authoritative: a previous attempt cannot overwrite
            # a successor. Conditional requests require server revalidation.
            latest = api(f"/actions/runs/{intent['run_id']}")
            if latest["run_attempt"] != intent["attempt"]:
                print("::notice::superseded run attempt will not overwrite its successor's status", flush=True)
                discard_intent(target)
                return 0 if resume else 1
            if newer_build_status(intent):
                print("::notice::newer run already published build status; obsolete intent discarded", flush=True)
                discard_intent(target)
                return 0 if resume else 1
            if latest.get("conclusion") == "cancelled":
                payload.update(state="failure", description="batch workflow was canceled; redispatch current batch")
            if payload["state"] == "success" and intent.get("bors"):
                bors = intent["bors"]
                # A staging generation can become obsolete without this run
                # acquiring a new attempt. Revalidate after every quota wait.
                try:
                    observed = observation(bors["batch_id"], bors["base_ref"])
                    candidate_ref(observed, revision, bors["base_sha"],
                                  batch_id=bors["batch_id"], branch=bors["base_ref"])
                except (ValueError, KeyError, TypeError):
                    print("::notice::obsolete or unconfirmed bors generation will not publish success", flush=True)
                    discard_intent(target)
                    return 0 if resume else 1
            api(f"/statuses/{revision}", payload)
            discard_intent(target)
            print(f"Posted build={payload['state']} on {revision}: {payload['description']}", flush=True)
            return 0 if resume or payload["state"] == "success" else 1
        except Deferred as error:
            # Wait outside POST, then refresh the run fence: an attempt may
            # have been canceled or superseded during a long quota wait.
            delay = max(1, error.until - time.time())
            if time.monotonic() + delay > deadline:
                print(f"::error::terminal status pending: {error}; retained status intent can be resumed", file=sys.stderr)
                return 75
            print(f"::warning::{error}; will refresh run identity before retry", flush=True)
            time.sleep(delay)
            continue
        except APIError as error:
            if error.status and error.status < 500:
                print(f"::error::terminal status rejected: {error}", file=sys.stderr)
                return 1
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            # Token renewal can be temporarily unavailable. Preserve the intent
            # and retry publication within the same finite deadline, not build.
            pass
        print("::warning::terminal status publication unavailable; retrying without rebuilding", flush=True)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(10, remaining))
    print("::error::terminal status could not be posted; retained intent can be resumed", file=sys.stderr)
    return 1


def main(argv=()):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", help="publish an existing trusted status intent without repeating the build")
    args = parser.parse_args(argv)
    if args.resume:
        path = Path(args.resume)
        if path.stat().st_size > 16384:
            raise ValueError("status intent exceeds bound")
        intent = validate_intent(json.loads(path.read_text()))
        for variable, field in (("CI_STATUS_SOURCE_RUN_ID", "run_id"), ("CI_STATUS_SOURCE_ATTEMPT", "attempt")):
            expected = os.environ.get(variable)
            if expected is not None and str(intent[field]) != expected:
                raise ValueError("status intent does not match trusted source run/attempt")
        if intent["schema"] == "tauceti-ci.finalization-intent/v1":
            env = dict(intent["evidence"], STATUS_SHA=intent["revision"],
                       GITHUB_RUN_ID=str(intent["run_id"]), GITHUB_RUN_ATTEMPT=str(intent["attempt"]))
            return finalize(env, str(path), resume=True)
        return publish(intent, target=str(path), resume=True)
    env = os.environ
    revision = env.get("STATUS_SHA")
    if not sha(revision):
        print("::error::cannot post a batch status without an exact commit SHA", file=sys.stderr)
        return 1
    target = env.get("TAUCETI_STATUS_INTENT")
    if not target and env.get("RUNNER_TEMP"):
        target = str(Path(env["RUNNER_TEMP"]) / "tauceti-status-intent.json")
    return finalize(env, target)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
