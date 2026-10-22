"""Build a SQLite database from the NDJSON records. The database is derived and never committed.

    python -m collector.build_db [--out db/ci.sqlite]

Tables
  runs(repo, run_id, run_attempt, workflow, event, trigger, conclusion, head_sha, created_at,
       updated_at, pr, queue_pr, base_sha, group_position, group_prs, merge_base, behind_by,
       ahead_by, diff_files, diff_additions, diff_deletions, patch_fingerprint, landed,
       queue_position, queue_chain, ahead_failed: a merge-queue build's place in the queue, the PRs
       built together (front first), and whether an entry ahead of it failed; see
       derive_queue_chains. (group_prs/group_position from the records are superseded: they list
       only the entry's own PR.)
       jobs_fetched: 0 when a backfill recorded the run without its jobs)
  Runner kind retains its raw category; runner_name preserves archived fleet identity
  so reporting aliases do not change the categories used by other database readers.
  Failure labels are recomputed from archived excerpts with the current classifier.
  Raw records retain their original label/version; records without excerpts keep that label.
  process-killed establishes neither a timeout nor an OOM cause.

  jobs(repo, run_id, job_id, run_attempt, name, conclusion, created_at, started_at, completed_at,
       wait_s, run_s, runner_kind, runner_size, labels, failure_class, failed_step, excerpt)
  steps(job_id, n, name, conclusion, started_at, completed_at, run_s)
  main_commits(sha, committed_at, pr, subject)
  main_landings(repo, head_sha, landed_at, source, push_run_id): earliest recorded
       main-push workflow event proving a commit was on main. This is a notification
       upper bound, never the staging commit's committer timestamp.
  post_merge_runs(repo, run_id, engine): main-push jobs attributed once by tested SHA;
       engine is NULL when merge identity is absent or ambiguous.
  settings(repo, observed_at, rulesets_json)
  telemetry(repo, run_id, phases_json, build_json, meta_json)   pr-build's in-sandbox phase timings
       and module counts (statistics only: written where candidate code runs)
  replay_features(repo, run_id, run_attempt, pr, head_sha, available_at, payload_json)
       completed exact-head PR snapshots for advisory replay; untrusted work hints
  coverage(repo, complete_to)   every run of `repo` created before `complete_to` is recorded
  annotations(at, text)   hand-kept notes (records/annotations.json) for changes the API cannot see,
                          such as repository variables

`runs` holds the latest attempt seen for each run; `jobs` holds every attempt's jobs, deduplicated.
Times are ISO-8601 UTC text; durations are seconds.
"""

from __future__ import annotations

import os

import argparse
import datetime as dt
import gzip
import json
import re
import sqlite3
from itertools import chain as record_chain
from pathlib import Path
from . import classify
from .merge_identity import identity, apply_dispatch_title
from .resources import cohort, phase, verified_observation

ROOT = Path(os.environ.get("TAUCETI_CI_ARCHIVE_DIR", str(Path.cwd())))
RECORDS = ROOT / "records"

SCHEMA = """
CREATE TABLE runs (
  repo TEXT, run_id INTEGER, run_attempt INTEGER, workflow TEXT, event TEXT, trigger TEXT,
  conclusion TEXT, head_sha TEXT, created_at TEXT, updated_at TEXT,
  pr INTEGER, queue_pr INTEGER, base_sha TEXT, group_position INTEGER, group_prs TEXT,
  merge_base TEXT, behind_by INTEGER, ahead_by INTEGER,
  diff_files INTEGER, diff_additions INTEGER, diff_deletions INTEGER, patch_fingerprint TEXT,
  landed INTEGER, jobs_fetched INTEGER,
  queue_position INTEGER, queue_chain TEXT, ahead_failed INTEGER,
  PRIMARY KEY (repo, run_id)
);
CREATE TABLE jobs (
  repo TEXT, run_id INTEGER, job_id INTEGER PRIMARY KEY, run_attempt INTEGER, name TEXT,
  conclusion TEXT, created_at TEXT, started_at TEXT, completed_at TEXT, wait_s REAL, run_s REAL,
  runner_kind TEXT, runner_size TEXT, labels TEXT,
  failure_class TEXT, failed_step TEXT, excerpt TEXT, api_status TEXT, runner_name TEXT
);
CREATE TABLE steps (
  job_id INTEGER, n INTEGER, name TEXT, conclusion TEXT, started_at TEXT, completed_at TEXT,
  run_s REAL, PRIMARY KEY (job_id, n)
);
CREATE TABLE main_commits (sha TEXT PRIMARY KEY, committed_at TEXT, pr INTEGER, subject TEXT);
CREATE TABLE main_landings (repo TEXT, head_sha TEXT, landed_at TEXT, source TEXT,
  push_run_id INTEGER, PRIMARY KEY(repo, head_sha));
CREATE TABLE post_merge_runs (repo TEXT, run_id INTEGER, engine TEXT, PRIMARY KEY(repo, run_id));
CREATE TABLE merge_experiments (id TEXT PRIMARY KEY, observed_at TEXT, payload_json TEXT);
CREATE TABLE settings (repo TEXT, observed_at TEXT, rulesets_json TEXT);
CREATE TABLE annotations (at TEXT, text TEXT);
CREATE TABLE coverage (repo TEXT PRIMARY KEY, complete_to TEXT);
CREATE TABLE stream_coverage (repo TEXT, stream TEXT, since TEXT, complete_to TEXT,
  PRIMARY KEY(repo,stream,since,complete_to));
CREATE TABLE collection_gaps (repo TEXT,run_id INTEGER,run_attempt INTEGER,stream TEXT,created_at TEXT,reason TEXT,
  PRIMARY KEY(repo,run_id,run_attempt,stream));
CREATE TABLE enrichment_state (repo TEXT,run_id INTEGER,run_attempt INTEGER,stage TEXT,
  PRIMARY KEY(repo,run_id,run_attempt));
CREATE TABLE queue_observation_metadata (observed_at TEXT PRIMARY KEY, eligible_count INTEGER,
  candidate_scan_complete INTEGER, candidate_snapshot_at TEXT);
CREATE TABLE telemetry (repo TEXT, run_id INTEGER, phases_json TEXT, build_json TEXT, meta_json TEXT,
  PRIMARY KEY (repo, run_id));
CREATE TABLE replay_features (repo TEXT, run_id INTEGER, run_attempt INTEGER, pr INTEGER,
  head_sha TEXT, available_at TEXT, payload_json TEXT, PRIMARY KEY(repo,run_id,run_attempt));
CREATE INDEX replay_features_head ON replay_features(repo,pr,head_sha,available_at);
CREATE TABLE resource_observations (repo TEXT, run_id INTEGER, run_attempt INTEGER,
  kind TEXT, coverage TEXT, artifact_id INTEGER, payload_json TEXT,
  PRIMARY KEY(repo, run_id, run_attempt, kind));
CREATE TABLE run_attempts (repo TEXT, run_id INTEGER, run_attempt INTEGER, conclusion TEXT,
  cohort TEXT, workflow_sha TEXT, PRIMARY KEY(repo, run_id, run_attempt));
CREATE TABLE phase_boundaries (job_id INTEGER, n INTEGER, phase TEXT, trust TEXT,
  started_at TEXT, completed_at TEXT, run_s REAL, PRIMARY KEY(job_id,n));
CREATE TABLE merge_builds (repo TEXT, run_id INTEGER, engine TEXT, head_sha TEXT, base_sha TEXT,
  batch_id INTEGER, members_json TEXT, landed INTEGER, PRIMARY KEY(repo, run_id));
CREATE TABLE merge_observations (observed_at TEXT PRIMARY KEY, backend TEXT, setting_updated_at TEXT,
  github_count INTEGER, bors_count INTEGER, pending INTEGER, overlap INTEGER, eligible_json TEXT, reason TEXT);
CREATE INDEX jobs_run ON jobs(repo, run_id);
CREATE INDEX jobs_created ON jobs(created_at);
CREATE INDEX runs_created ON runs(created_at);
CREATE INDEX runs_workflow ON runs(workflow, trigger);
CREATE INDEX merge_builds_head ON merge_builds(repo,head_sha);
"""


def seconds(a, b):
    if not a or not b:
        return None
    f = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return max(0, (f(b) - f(a)).total_seconds())


def runner_size(labels):
    for l in labels:
        m = re.search(r"-(\d+x\d+)", l)
        if l.startswith("nscloud-") and m:
            return m.group(1)
    return None


def records(kind: str):
    for f in sorted((RECORDS / kind).rglob("*.ndjson.gz")):
        with gzip.open(f, "rt") as fh:
            for line in fh:
                yield json.loads(line)


def derive_queue_chains(db: sqlite3.Connection):
    """Reconstruct each merge-queue build's place in the queue.

    GitHub names a merge-queue branch gh-readonly-queue/main/pr-<N>-<sha>, where <sha> is the commit
    the entry was built on: main's tip for the front entry, and otherwise the head of the entry
    ahead of it, which itself contains every entry ahead. Walking those links back while the entry
    ahead was still being built gives the entry's position (1 at the front), the PRs it
    was built together with (`queue_chain`, front first, this PR last), and whether any entry ahead
    of it failed, which fails this one too whatever its own PR does (`ahead_failed`)."""
    mq = {}
    for repo, run_id, head, base, pr, created, updated, concl in db.execute("""
            SELECT repo, run_id, head_sha, base_sha, queue_pr, created_at, updated_at, conclusion FROM runs
            WHERE trigger = 'merge_queue' AND base_sha IS NOT NULL"""):
        mq[(repo, head)] = (run_id, base, pr, created, updated, concl)
    for (repo, head), (run_id, base, pr, created, _, concl) in mq.items():
        chain, failed, cur, seen = [pr], 0, base, {head}
        # An entry is ahead only while its own build is still running: once it has finished it has
        # either landed (and is simply main) or been removed. A merge-queue commit's committer date
        # is when GitHub made it, not when main moved to it, so it cannot answer this.
        while (repo, cur) in mq and cur not in seen and mq[(repo, cur)][4] > created:
            seen.add(cur)
            _, cur_base, cur_pr, _, _, cur_concl = mq[(repo, cur)]
            chain.insert(0, cur_pr)
            failed |= cur_concl == "failure"
            cur = cur_base
        db.execute("UPDATE runs SET queue_position = ?, queue_chain = ?, ahead_failed = ? WHERE repo = ? AND run_id = ?",
                   (len(chain), json.dumps(chain), int(failed), repo, run_id))


def derive_main_landings(db, main):
    """Use main-push evidence, including coalesced pushes containing tested prefixes.

    A cancelled post-merge run still proves a push occurred. Earliest events are
    retained across reruns. Parent links propagate that proof to ancestors, with
    an explicit source identifying the weaker ancestor upper bound. Missing
    parent/history data stays missing; commit dates are never a timing fallback.
    """
    pushes = db.execute("""SELECT repo,run_id,head_sha,created_at FROM runs
        WHERE repo='TauCeti' AND event='push' AND trigger='main'
        ORDER BY created_at,run_id""").fetchall()
    seen = set()
    for repo, run_id, head, at in pushes:
        pending = [head]
        while pending:
            sha = pending.pop()
            if (repo, sha) in seen:
                continue
            seen.add((repo, sha))
            source = 'main-push-workflow' if sha == head else 'main-push-ancestor-bound'
            db.execute('INSERT INTO main_landings VALUES (?,?,?,?,?)',
                       (repo, sha, at, source, run_id))
            pending.extend(main.get(sha, {}).get('parents', []))
    db.execute("""UPDATE merge_builds SET landed=1 WHERE EXISTS
        (SELECT 1 FROM main_landings l WHERE l.repo=merge_builds.repo
         AND l.head_sha=merge_builds.head_sha)""")
    db.execute("""INSERT INTO post_merge_runs
        SELECT r.repo,r.run_id,CASE WHEN COUNT(DISTINCT m.engine)=1 THEN MIN(m.engine) END
        FROM runs r LEFT JOIN merge_builds m ON m.repo=r.repo AND m.head_sha=r.head_sha
        WHERE r.repo='TauCeti' AND r.event='push' AND r.trigger='main'
        GROUP BY r.repo,r.run_id""")


def build(out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)

    main = {}
    for c in records("main"):
        main[c["sha"]] = c
    db.executemany("INSERT OR REPLACE INTO main_commits VALUES (?,?,?,?)",
                   [(c["sha"], c["committed_at"], c.get("pr"), c.get("subject")) for c in main.values()])

    # Inventory cannot erase jobs or enriched metadata. For the same attempt,
    # consume progressively richer snapshots in this order. Older attempts
    # still contribute their jobs, without overwriting the latest run's identity.
    for r in record_chain(records('inventory'), records('jobs'), records("runs")):
        # Old raw records remain append-only; derived trusted statistics must
        # independently verify producer provenance on every rebuild.
        r = {**r, "observations": {
            kind: verified_observation(r["repo"], r, kind, observation)
            for kind, observation in (r.get("observations") or {}).items()}}
        # Historical bors runs without an artifact still contribute their
        # cost. Unknown tested heads never count as landed workflow-source SHAs.
        if r.get("trigger") == "repository_dispatch":
            apply_dispatch_title(r, r.get("title"))
        t = r.get("tested") or {}
        d = t.get("diff") or {}
        if r["repo"] == "TauCeti" and t.get("pr") and t.get("head_sha"):
            # Use completion availability, never the run's start, to
            # avoid lending a replay features which arrived in the future.
            db.execute("INSERT OR REPLACE INTO replay_features VALUES (?,?,?,?,?,?,?)", (
                r["repo"], r["run_id"], r["run_attempt"], t["pr"], t["head_sha"],
                r.get("updated_at"), json.dumps({"diff": d, "behind_by": t.get("behind_by"),
                    "build": (r.get("telemetry") or {}).get("build") or {}})))
        stage = r.get('collection_stage', 'runs')
        db.execute('INSERT OR REPLACE INTO enrichment_state VALUES (?,?,?,?)',
                   (r['repo'],r['run_id'],r['run_attempt'],stage))
        for stream in ('jobs','details'):
            if r.get(stream+'_gap'):
                db.execute('INSERT OR REPLACE INTO collection_gaps VALUES (?,?,?,?,?,?)',
                    (r['repo'],r['run_id'],r['run_attempt'],stream,r['created_at'],r[stream+'_gap']['reason']))
        prev = db.execute("SELECT run_attempt,conclusion,created_at,updated_at FROM runs WHERE repo=? AND run_id=?",
                          (r["repo"], r["run_id"])).fetchone()
        if not prev or prev[0] <= r["run_attempt"]:
            conclusion, created, updated = r.get('conclusion'),r['created_at'],r.get('updated_at')
            if prev and prev[0] == r['run_attempt'] and (updated or '') < (prev[3] or ''):
                conclusion,created,updated = prev[1:]
            db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)", (
                r["repo"], r["run_id"], r["run_attempt"], r["workflow"], r["event"], r["trigger"],
                conclusion, r["head_sha"], created, updated,
                t.get("pr"), t.get("queue_pr"), t.get("base_sha"), t.get("group_position"),
                json.dumps(t["group_prs"]) if "group_prs" in t else None,
                t.get("merge_base"), t.get("behind_by"), t.get("ahead_by"),
                d.get("files"), d.get("additions"), d.get("deletions"), d.get("patch_fingerprint"),
                (1 if r["head_sha"] in main else 0) if r["trigger"] == "merge_queue" else None,
                0 if r.get("jobs_fetched") is False else 1,
            ))
        merge = identity(r)
        if merge and (not prev or prev[0] <= r['run_attempt']):
            engine, head, base, batch, members = merge
            db.execute("INSERT OR REPLACE INTO merge_builds VALUES (?,?,?,?,?,?,?,?)", (
                r["repo"], r["run_id"], engine, head, base, batch, json.dumps(members), int(head in main)))
        t = r.get("telemetry")
        if t and (not prev or prev[0] <= r['run_attempt']):
            db.execute("INSERT OR REPLACE INTO telemetry VALUES (?,?,?,?,?)", (
                r["repo"], r["run_id"], json.dumps(t.get("phases") or []),
                json.dumps(t.get("build") or {}), json.dumps(t.get("meta") or {})))
        if r["repo"] == "TauCeti" and r["workflow"] in (
                ".github/workflows/pr-build.yml", ".github/workflows/ci.yml",
                ".github/workflows/publish-lake-cache.yml"):
            db.execute("INSERT OR REPLACE INTO run_attempts VALUES (?,?,?,?,?,?)", (
                r["repo"], r["run_id"], r["run_attempt"], r.get("conclusion"), cohort(r),
                (((r.get("observations") or {}).get("resources") or {}).get("payload") or {}).get("workflow_sha")))
            if r.get('details_gap'):
                default_coverage = 'enrichment-unavailable'
            elif stage == 'runs':
                default_coverage = 'historical-missing'
            elif r['workflow'] in ('.github/workflows/pr-build.yml','.github/workflows/ci.yml') and r.get('conclusion') != 'skipped':
                default_coverage = 'pending-enrichment'
            else:
                default_coverage = 'not-collected'
            for kind in ("resources", "publication"):
                observation = (r.get("observations") or {}).get(kind) or {"coverage": default_coverage}
                db.execute("INSERT OR REPLACE INTO resource_observations VALUES (?,?,?,?,?,?,?)", (
                    r["repo"], r["run_id"], r["run_attempt"], kind, observation["coverage"],
                    observation.get("artifact_id"),
                    json.dumps(observation["payload"]) if observation.get("payload") else None))
            for attempt in {j.get("run_attempt") for j in r["jobs"]} - {None, r["run_attempt"]}:
                db.execute("INSERT OR IGNORE INTO run_attempts VALUES (?,?,?,?,?,?)", (
                    r["repo"], r["run_id"], attempt, None, "unknown", None))
                for kind in ("resources", "publication"):
                    db.execute("INSERT OR IGNORE INTO resource_observations VALUES (?,?,?,?,?,?,?)", (
                        r["repo"], r["run_id"], attempt, kind, "attempt-not-collected", None, None))
        for j in r["jobs"]:
            f = j.get("failure") or {}
            # Labels are derived. Reapply current rules to archived evidence so
            # classifier repairs also correct historical charts without rewriting records.
            failure_class = f.get("class")
            if f:
                derived = classify.classify(j.get("conclusion"), f.get("failed_step"),
                                            f.get("excerpt"))
                # Step names can establish a cause even when the log was unavailable.
                # Otherwise keep archived evidence rather than replacing it with a fallback.
                if f.get("excerpt") is not None or derived not in ("build-unknown", "other"):
                    failure_class = derived
            db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                r["repo"], r["run_id"], j["id"], j.get("run_attempt"), j["name"], j.get("conclusion"),
                j.get("created_at"), j.get("started_at"), j.get("completed_at"),
                seconds(j.get("created_at"), j.get("started_at")) if j.get("status") != "queued" else None,
                seconds(j.get("started_at"), j.get("completed_at")) if j.get("status") != "queued" else None,
                j.get("runner_kind"), runner_size(j.get("labels") or []), json.dumps(j.get("labels") or []),
                failure_class, f.get("failed_step"),
                "\n".join(f["excerpt"]) if f.get("excerpt") else None, j.get("status"), j.get("runner_name"),
            ))
            for s in j.get("steps") or []:
                db.execute("INSERT OR REPLACE INTO steps VALUES (?,?,?,?,?,?,?)", (
                    j["id"], s["n"], s["name"], s.get("conclusion"), s.get("started_at"),
                    s.get("completed_at"), seconds(s.get("started_at"), s.get("completed_at"))))
                db.execute("INSERT OR REPLACE INTO phase_boundaries VALUES (?,?,?,?,?,?,?)", (
                    j["id"], s["n"], phase(s["name"]), "github-api-step",
                    s.get("started_at"), s.get("completed_at"),
                    seconds(s.get("started_at"), s.get("completed_at"))))

    for r in records('attempt-gaps'):
        db.execute('INSERT OR REPLACE INTO collection_gaps VALUES (?,?,?,?,?,?)',
                   (r['repo'],r['run_id'],r['run_attempt'],'attempt',r['created_at'],r['reason']))
        db.execute('INSERT OR IGNORE INTO run_attempts VALUES (?,?,?,?,?,?)',
                   (r['repo'],r['run_id'],r['run_attempt'],None,'unknown',None))
        for kind in ('resources','publication'):
            db.execute('''INSERT INTO resource_observations VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(repo,run_id,run_attempt,kind) DO UPDATE SET coverage='attempt-unavailable'
                WHERE coverage IN ('pending-enrichment','historical-missing','attempt-not-collected')''',
                       (r['repo'],r['run_id'],r['run_attempt'],kind,'attempt-unavailable',None,None))
    # Batch membership is cheap, independently collected metadata. It remains
    # usable when job/artifact enrichment has not caught up.
    for r in records('merge-members'):
        db.execute('''UPDATE merge_builds SET members_json=?
            WHERE repo=? AND batch_id=? AND head_sha=? AND engine='bors' ''',
                   (json.dumps(r['members']), r['repo'], r['batch_id'], r['head_sha']))
    derive_queue_chains(db)

    # Queue chains are reconstructed after all runs are read. Never sum their
    # overlapping prefixes to count merged PRs.
    for repo, run_id, chain in db.execute("SELECT repo,run_id,queue_chain FROM runs WHERE trigger='merge_queue'").fetchall():
        members = [{"pr": n} for n in json.loads(chain or "[]") if n is not None]
        db.execute("UPDATE merge_builds SET members_json=? WHERE repo=? AND run_id=?",
                   (json.dumps(members), repo, run_id))
    derive_main_landings(db, main)
    for obs in records("observations"):
        db.execute("INSERT OR REPLACE INTO merge_observations VALUES (?,?,?,?,?,?,?,?,?)", (
            obs["observed_at"], obs.get("backend"), obs.get("updated_at"), obs.get("github_count"),
            obs.get("bors_count"), obs.get("pending"), int(bool(obs.get("overlap"))),
            json.dumps(obs.get("eligible") or []), obs.get("reason")))
        count = len({p['pr'] for p in obs.get('eligible', [])
                     if type(p.get('pr')) is int and p['pr'] > 0})
        complete = obs.get('candidate_scan_complete')
        db.execute('INSERT OR REPLACE INTO queue_observation_metadata VALUES (?,?,?,?)',
                   (obs['observed_at'], count, None if complete is None else int(complete),
                    obs.get('candidate_snapshot_at')))
        experiment = obs.get("experiment")
        if isinstance(experiment, dict) and experiment.get("id"):
            db.execute("""INSERT INTO merge_experiments VALUES (?,?,?)
                ON CONFLICT(id) DO UPDATE SET observed_at=excluded.observed_at,
                payload_json=excluded.payload_json WHERE excluded.observed_at>observed_at""",
                       (experiment["id"], obs["observed_at"], json.dumps(experiment)))

    for f in sorted((RECORDS / "settings").rglob("*.json")):
        s = json.loads(f.read_text())
        db.execute("INSERT INTO settings VALUES (?,?,?)",
                   (s["repo"], s["observed_at"], json.dumps(s["rulesets"])))
    cursor = ROOT / "state" / "cursor.json"
    if cursor.exists():
        catalog = ROOT / 'state/repositories.json'
        active = set(json.loads(catalog.read_text())['active']) if catalog.exists() else None
        db.executemany("INSERT INTO coverage VALUES (?,?)", [(repo,end) for repo,end in json.loads(cursor.read_text()).items()
                                                           if active is None or repo in active])
    # Preserve each proven inventory interval, including recent windows ahead
    # of a historical backlog. Consumers can shade gaps instead of calling them
    # empty queues or missing staging activity.
    for repo, end in db.execute('SELECT repo,complete_to FROM coverage').fetchall():
        first = db.execute('SELECT MIN(created_at) FROM runs WHERE repo=?', (repo,)).fetchone()[0]
        if first and first < end:
            db.execute('INSERT OR IGNORE INTO stream_coverage VALUES (?,?,?,?)',
                       (repo, 'inventory', first, end))
    for r in records('inventory-coverage'):
        db.execute('INSERT OR IGNORE INTO stream_coverage VALUES (?,?,?,?)',
                   (r['repo'], 'inventory', r['created_at'], r['until']))
    build_cursor = ROOT / 'state/build-cursor.json'
    if build_cursor.exists():
        for repo, end in json.loads(build_cursor.read_text()).items():
            first = db.execute('SELECT MIN(created_at) FROM runs WHERE repo=?', (repo,)).fetchone()[0]
            if first and first < end:
                db.execute('INSERT OR IGNORE INTO stream_coverage VALUES (?,?,?,?)',
                           (repo, 'build-jobs', first, end))
    details_cursor = ROOT / 'state/details-cursor.json'
    if details_cursor.exists():
        for repo, end in json.loads(details_cursor.read_text()).items():
            first = db.execute('SELECT MIN(created_at) FROM runs WHERE repo=?', (repo,)).fetchone()[0]
            if first and first < end:
                db.execute('INSERT OR IGNORE INTO stream_coverage VALUES (?,?,?,?)',
                           (repo, 'details', first, end))
    notes = RECORDS / "annotations.json"
    if notes.exists():
        db.executemany("INSERT INTO annotations VALUES (?,?)",
                       [(a["at"], a["text"]) for a in json.loads(notes.read_text())])
    db.commit()
    db.execute("VACUUM")
    db.close()
    tmp.replace(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "db" / "ci.sqlite"))
    build(Path(ap.parse_args(argv).out))


if __name__ == "__main__":
    main()
