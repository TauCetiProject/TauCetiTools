"""Shared run normalization, enrichment and atomic append-only record writers.

The bounded collector entry point is ``python -m collector.incremental``.
Detailed snapshots and independently collected resource observations use these
same provenance, attempt, failure and runner classifiers.
"""

from __future__ import annotations

import copy
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import tempfile
import uuid
import zipfile
from pathlib import Path

from . import classify, resources
from .merge_identity import apply_telemetry, apply_dispatch_title
from .observations import batch_members
from .gh import GitHub

ORG = os.environ.get("TAUCETI_REPO", "TauCetiProject/TauCeti").split("/")[0]
DETAIL_REPOS = {os.environ.get("TAUCETI_REPO", "TauCetiProject/TauCeti").split("/")[1]}
SCHEMA = "tauceti-ci.run/v1"
ROOT = Path(os.environ.get("TAUCETI_CI_ARCHIVE_DIR", str(Path.cwd())))
RECORDS = ROOT / "records"
MAX_LOGS_PER_COLLECTION = 300
# Longer than any workflow's timeout (nightly-verify's is 4 hours), so a run created before the
# cursor but completed after it is still listed.
OVERLAP = dt.timedelta(hours=6)

# Steps, commits tested and failure excerpts are kept only for these workflows. The label,
# notification and merge-bot workflows fire thousands of times a day and need only their jobs.
BUILD_WORKFLOWS = {".github/workflows/pr-build.yml", ".github/workflows/ci.yml",
                   ".github/workflows/nightly-verify.yml", ".github/workflows/pr-profile.yml",
                   ".github/workflows/lint-full.yml", ".github/workflows/pages.yml"}

# Of the high-volume workflows' runs, fetch jobs for one in SAMPLE_SHORT (chosen by run id, so
# deterministically) and record the rest at run level. That keeps an unbiased sample of their
# queue waits while cutting the collector's API cost by about three quarters.
SAMPLE_SHORT = 4

UTC = dt.timezone.utc


def iso(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str) -> dt.datetime:
    now = dt.datetime.now(UTC)
    m = re.fullmatch(r"(\d+)([hd])", s)
    if m:
        n = int(m.group(1))
        return now - (dt.timedelta(hours=n) if m.group(2) == "h" else dt.timedelta(days=n))
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def runner_kind(labels: list[str]) -> str:
    if any(l.startswith("nscloud-") for l in labels):
        return "namespace"
    if not labels:
        return "none"
    if any(l.startswith(("ubuntu-", "windows-", "macos-")) for l in labels):
        return "github"
    return "self-hosted"


def trigger_class(run: dict) -> str:
    ev = run["event"]
    if ev in ("pull_request", "pull_request_target"):
        return "pr"
    if ev == "merge_group":
        return "merge_queue"
    if ev == "push":
        return "main" if run.get("head_branch") == "main" else "push"
    if ev in ("issue_comment", "pull_request_review", "pull_request_review_comment"):
        return "comment"
    return ev  # schedule, workflow_dispatch, workflow_run, repository_dispatch, ...


# --- existing records --------------------------------------------------------------------------


def day_dirs(kind: str, repo: str | None, since: dt.datetime, until: dt.datetime):
    base = RECORDS / kind / repo if repo else RECORDS / kind
    d = since.date()
    while d <= until.date():
        yield base / f"{d:%Y/%m/%d}"
        d += dt.timedelta(days=1)


def seen_keys(kind: str, repo: str | None, since: dt.datetime, until: dt.datetime, key) -> set:
    out = set()
    for d in day_dirs(kind, repo, since - dt.timedelta(days=1), until + dt.timedelta(days=1)):
        for f in d.glob("*.ndjson.gz") if d.is_dir() else []:
            for line in gzip.open(f, "rt"):
                out.add(key(json.loads(line)))
    return out


def write_records(kind: str, repo: str | None, records: list[dict], date_field: str, stamp: str):
    by_day: dict[str, list[dict]] = {}
    for r in records:
        by_day.setdefault(r[date_field][:10], []).append(r)
    written = []
    for day, rs in by_day.items():
        d = (RECORDS / kind / repo if repo else RECORDS / kind) / day.replace("-", "/")
        d.mkdir(parents=True, exist_ok=True)
        # Each batch is an immutable gzip member/file. A killed writer cannot
        # truncate a file containing previously checkpointed records.
        target = d / f"{stamp}-{time.time_ns():020d}-{uuid.uuid4().hex[:12]}.ndjson.gz"
        with tempfile.NamedTemporaryFile(dir=d, suffix='.tmp', delete=False) as tmp:
            temporary = Path(tmp.name)
        try:
            with gzip.open(temporary, 'wt') as f:
                for r in sorted(rs, key=lambda r: r[date_field]):
                    f.write(json.dumps(r, separators=(",", ":"), sort_keys=True, ensure_ascii=False) + "\n")
            os.replace(temporary, target)
            written.append(target)
        finally:
            temporary.unlink(missing_ok=True)
    return written


# --- enrichment (build workflows of DETAIL_REPOS) -----------------------------------------------


class Enricher:
    def __init__(self, gh: GitHub, repo: str):
        self.gh, self.repo = gh, repo
        self.pr_by_head: dict[tuple[str, str], list[dict]] = {}
        self.main_at_cache: dict[str, str | None] = {}
        self.logs_fetched = 0
        self._lock = threading.Lock()

    def pr_number(self, run: dict) -> int | None:
        if run.get("pull_requests"):
            return run["pull_requests"][0]["number"]
        head_repo = (run.get("head_repository") or {}).get("full_name")
        if not head_repo or not run.get("head_branch"):
            return None
        owner = head_repo.split("/")[0]
        key = (owner, run["head_branch"])
        if key not in self.pr_by_head:
            try:
                self.pr_by_head[key] = self.gh.get(f"repos/{ORG}/{self.repo}/pulls",
                                                   head=f"{owner}:{run['head_branch']}", state="all",
                                                   per_page=10)
            except Exception:
                self.pr_by_head[key] = []
        prs = self.pr_by_head[key]
        for pr in prs:
            if pr["head"]["sha"] == run["head_sha"]:
                return pr["number"]
        return prs[0]["number"] if prs else None

    def main_at(self, when: str) -> str | None:
        minute = when[:16]
        if minute not in self.main_at_cache:
            commits = self.gh.get(f"repos/{ORG}/{self.repo}/commits", sha="main", until=when, per_page=1)
            self.main_at_cache[minute] = commits[0]["sha"] if commits else None
        return self.main_at_cache[minute]

    def compare(self, base: str, head: str) -> dict | None:
        try:
            return self.gh.get(f"repos/{ORG}/{self.repo}/compare/{base}...{head}", per_page=100)
        except Exception as e:
            print(f"compare {base[:8]}...{head[:8]} failed: {e}", file=sys.stderr)
            return None

    @staticmethod
    def diff_summary(cmp: dict) -> dict:
        files = cmp.get("files") or []
        # A content fingerprint of the change, stable across a pure rebase when the base's changes
        # don't touch the same hunks: the sorted (path, patch) pairs, falling back to the new blob.
        h = hashlib.sha256()
        for f in sorted(files, key=lambda f: f["filename"]):
            h.update(f["filename"].encode())
            h.update((f.get("patch") or f.get("sha") or "").encode())
        return {
            "files": len(files),
            "files_truncated": len(files) >= 300,
            "additions": sum(f.get("additions", 0) for f in files),
            "deletions": sum(f.get("deletions", 0) for f in files),
            "paths": sorted(f["filename"] for f in files)[:300],
            "patch_fingerprint": h.hexdigest()[:16],
        }

    def tested(self, run: dict) -> dict:
        """What a build run tested."""
        tc = trigger_class(run)
        out: dict = {"head_sha": run["head_sha"]}
        if tc == "merge_queue":
            # gh-readonly-queue/main/pr-<N>-<sha of main the group was formed on>
            m = re.fullmatch(r"gh-readonly-queue/main/pr-(\d+)-([0-9a-f]{40})", run.get("head_branch") or "")
            if m:
                out["queue_pr"] = int(m.group(1))
                out["base_sha"] = m.group(2)
                cmp = self.compare(m.group(2), run["head_sha"])
                if cmp:
                    prs = []
                    for c in cmp.get("commits", []):
                        subj = c["commit"]["message"].splitlines()[0]
                        n = re.search(r"\(#(\d+)\)\s*$", subj)
                        prs.append(int(n.group(1)) if n else None)
                    out["group_prs"] = prs              # oldest first; this entry's PR is last
                    out["group_position"] = len(prs)   # entry k tests entries 1..k together
                    out["diff"] = self.diff_summary(cmp)
        elif tc == "pr" or run["event"] == "workflow_dispatch":
            if tc == "pr":
                out["pr"] = self.pr_number(run)
            base = self.main_at(run["created_at"])
            out["main_at_created"] = base
            if base:
                head = run["head_sha"]
                cmp = self.compare(base, head)
                if cmp:
                    out["merge_base"] = cmp["merge_base_commit"]["sha"]
                    out["behind_by"] = cmp["behind_by"]
                    out["ahead_by"] = cmp["ahead_by"]
                    # Diff against the merge base: what the PR itself changes.
                    mb = self.compare(cmp["merge_base_commit"]["sha"], head) if cmp["behind_by"] else cmp
                    if mb:
                        out["diff"] = self.diff_summary(mb)
        return out

    def telemetry(self, run: dict) -> dict | None:
        """The `ci-telemetry` artifact pr-build uploads (TauCeti's scripts/ci_telemetry.py): phase
        timings and module counts from inside the sandbox. Statistics only; kept for 30 days."""
        try:
            arts = self.gh.get(f"repos/{ORG}/{self.repo}/actions/runs/{run['id']}/artifacts",
                               name="ci-telemetry")
            for a in arts.get("artifacts", []):
                if a.get("expired"):
                    continue
                # The fixed artifact name is shared across retries. Only bind
                # telemetry to an attempt when its API timestamps establish the
                # artifact was uploaded during that execution.
                started, finished, created = run.get('run_started_at'), run.get('updated_at'), a.get('created_at')
                if run.get('run_attempt',1) > 1 or created:
                    if not started or not finished or not created:
                        continue
                    if not parse_time(started) <= parse_time(created) <= parse_time(finished):
                        continue
                data = self.gh.raw(f"repos/{ORG}/{self.repo}/actions/artifacts/{a['id']}/zip")
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    record = json.loads(z.read("telemetry.json"))
                if record.get("schema", "").startswith("tauceti-ci.telemetry/"):
                    return record
        except Exception as e:
            print(f"telemetry for run {run['id']} unavailable: {e}", file=sys.stderr)
        return None

    def failure(self, job: dict) -> dict | None:
        if job.get("conclusion") in (None, "success", "skipped", "neutral"):
            return None
        failed = next((s for s in job.get("steps", []) if s.get("conclusion") == "failure"), None)
        lines = None
        with self._lock:
            fetch = job["conclusion"] == "failure" and self.logs_fetched < MAX_LOGS_PER_COLLECTION
            if fetch:
                self.logs_fetched += 1
        if fetch:
            try:
                log = self.gh.text(f"repos/{ORG}/{self.repo}/actions/jobs/{job['id']}/logs")
                lines = classify.excerpt(log, failed and failed.get("started_at"),
                                         failed and failed.get("completed_at"))
            except Exception as e:  # logs expire after 90 days
                print(f"log for job {job['id']} unavailable: {e}", file=sys.stderr)
        return {
            "failed_step": failed and failed["name"],
            "class": classify.classify(job["conclusion"], failed and failed["name"], lines),
            "classifier": classify.CLASSIFIER_VERSION,
            "excerpt": lines,
        }


def job_record(job: dict, detail: bool) -> dict:
    """A job. Durations are left to the database (wait = started - created, run = completed -
    started); the high-volume workflows get only what occupancy and wait analyses need."""
    labels = job.get("labels") or []
    rec = {
        "id": job["id"],
        "name": job["name"],
        "run_attempt": job.get("run_attempt"),
        "conclusion": job.get("conclusion"),
        "status": job.get("status"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "labels": labels,
        "runner_kind": runner_kind(labels),
    }
    if detail:
        rec["runner_name"] = job.get("runner_name") or None
        rec["steps"] = [{
            "n": s["number"], "name": s["name"], "conclusion": s.get("conclusion"),
            "started_at": s.get("started_at"), "completed_at": s.get("completed_at"),
        } for s in job.get("steps", [])]
    return rec


def needs_jobs(run: dict, jobs_for: str) -> bool:
    if (run.get("path") or run.get("name")) in BUILD_WORKFLOWS:
        return True
    if jobs_for != "all":
        return False
    return jobs_for == "all" and run["id"] % SAMPLE_SHORT == 0


def run_record(gh: GitHub, repo: str, run: dict, enricher: Enricher | None, jobs_for: str,
               *, batch_fallback: bool = True, current_attempt_only: bool = False,
               preloaded_record: dict | None = None, keep_steps: bool = False) -> dict:
    workflow = run.get("path") or run.get("name")
    # A run skipped by its `if:` never reached a runner; its jobs carry nothing worth a call. With
    # `jobs_for="build"` (backfills of long periods) the high-volume workflows are recorded at run
    # level only, and otherwise a sample of them is (SAMPLE_SHORT); either way marked
    # `jobs_fetched: false`.
    fetch = preloaded_record is None and run.get("conclusion") != "skipped" and needs_jobs(run, jobs_for)
    # Bind jobs to attempts using the API path; historical all-attempt listings
    # can omit run_attempt, which must never be guessed from the latest attempt.
    jobs = []
    if fetch:
        current = run.get('run_attempt', 1)
        for attempt in ([current] if current_attempt_only else range(1, current + 1)):
            jobs.extend({**job, "run_attempt": attempt} for job in gh.paginate(
                f"repos/{ORG}/{repo}/actions/runs/{run['id']}/attempts/{attempt}/jobs", key="jobs"))
    detail = enricher is not None and workflow in BUILD_WORKFLOWS
    rec = {
        "schema": SCHEMA,
        "repo": repo,
        "run_id": run["id"],
        "run_attempt": run.get("run_attempt", 1),
        "workflow": workflow,
        "event": run["event"],
        "trigger": trigger_class(run),
        "conclusion": run.get("conclusion"),
        "head_sha": run["head_sha"],
        "created_at": run["created_at"],
        "updated_at": run.get("updated_at"),
        "jobs": [job_record(j, detail or keep_steps) for j in jobs],
    }
    if not fetch and run.get("conclusion") != "skipped":
        rec["jobs_fetched"] = False
    if preloaded_record is not None:
        rec = copy.deepcopy(preloaded_record)
    if detail:
        rec.update({
            "head_branch": run.get("head_branch"),
            "head_repo": (run.get("head_repository") or {}).get("full_name"),
            "workflow_repository": (run.get("repository") or {}).get("full_name"),
            "referenced_workflows": run.get("referenced_workflows") or [],
            "title": (run.get("display_title") or "")[:200],
            "run_started_at": run.get("run_started_at"),
        })
        try:
            rec["tested"] = enricher.tested(run)
        except Exception as e:
            print(f"enrich run {run['id']} failed: {e}", file=sys.stderr)
        apply_dispatch_title(rec, run.get("display_title"))
        if workflow == ".github/workflows/pr-build.yml" and run.get("conclusion") != "skipped":
            t = enricher.telemetry(run)
            if t:
                rec["telemetry"] = t
                apply_telemetry(rec, t)
            if batch_fallback and rec.get("merge_metadata_pending") and rec["tested"].get("batch_id"):
                try:
                    tested = rec["tested"]
                    members = batch_members(tested["batch_id"], tested["head_sha"])
                    apply_telemetry(rec, {"meta": {"merge_engine": "bors", "head_sha": tested["head_sha"],
                        "base_sha": tested["base_sha"], "batch_id": str(tested["batch_id"]),
                        "batch_members": json.dumps(members)}})
                except Exception as e:
                    print(f"batch metadata for run {run['id']} unavailable: {e}", file=sys.stderr)
        if workflow in (".github/workflows/pr-build.yml", ".github/workflows/ci.yml",
                        ".github/workflows/publish-lake-cache.yml"):
            rec["observations"] = resources.observations(gh, repo, run)
            # Immutable compare metadata distinguishes dependency bumps in historical
            # staging cohorts even when no resource artifact exists yet.
            tested = rec.get("tested") or {}
            if tested.get("base_sha") and tested.get("head_sha") and "diff" not in tested:
                comparison = enricher.compare(tested["base_sha"], tested["head_sha"])
                if comparison:
                    tested["diff"] = enricher.diff_summary(comparison)
        for jr in rec["jobs"]:
            f = enricher.failure(jr)
            if f:
                jr["failure"] = f
    return rec


def collect_main(gh: GitHub, repo: str, since, until, stamp: str, dry_run: bool) -> int:
    """First-parent history of main: which commits landed when, and from which PR. The merge queue
    fast-forwards main to a group's head, so a merge-queue run landed iff its head_sha is here."""
    have = seen_keys("main", repo, since, until, lambda r: r["sha"])
    out, count = [], 0
    def flush():
        nonlocal count
        if not dry_run and out:
            write_records("main", repo, out, "committed_at", stamp)
        count += len(out)
        out.clear()
    try:
        for c in gh.paginate(f"repos/{ORG}/{repo}/commits", sha="main", since=iso(since), until=iso(until)):
            if c["sha"] in have:
                continue
            subj = c["commit"]["message"].splitlines()[0]
            n = re.search(r"\(#(\d+)\)\s*$", subj)
            out.append({
                "schema": "tauceti-ci.main-commit/v1",
                "sha": c["sha"],
                "parents": [p["sha"] for p in c["parents"]],
                "committed_at": c["commit"]["committer"]["date"],
                "pr": int(n.group(1)) if n else None,
                "subject": subj[:200],
            })
            if len(out) >= 100:
                flush()
    finally:
        flush()
    return count
