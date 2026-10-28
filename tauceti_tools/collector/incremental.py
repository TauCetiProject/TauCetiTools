"""Bounded, checkpointed run inventory followed by independently resumable enrichment.

API pages are durable before job requests start. A disposable SQLite index avoids
re-reading historical gzip files and is rebuilt from committed records on a cache
miss. Coverage for inventory, build jobs, all sampled jobs and bors observations
is independent. No request waits for a rate-limit reset.
"""
import argparse
import copy
import datetime as dt
import json
import hashlib
import gzip
import os
import shutil
import tempfile
import uuid
import signal
import subprocess
import threading
import time
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import collect
from .gh import GitHub, RateLimitUnavailable
from .index import Index
from .observations import batch_members, read_days
from .merge_identity import apply_dispatch_title


class CollectionStopped(BaseException):
    """Cooperative stop, like KeyboardInterrupt: optional enrichment must not swallow it."""


class CheckpointFailed(BaseException):
    """Stop collection when publication fails; the workflow may retry the saved batch."""


class BoundedGitHub(GitHub):
    def __init__(self, max_calls, max_seconds):
        super().__init__(wait_for_rate_limit=False)
        self.max_calls = max_calls
        self.deadline = time.monotonic() + max_seconds
        self.phase_limit = max_calls
        self.phase_deadline = self.deadline
        self.started = 0
        self.stop_requested = False
        self.cache = {}
        self.cache_lock = threading.Lock()

    def check(self):
        if self.stop_requested or time.monotonic() >= min(self.deadline, self.phase_deadline):
            raise CollectionStopped('collection time budget reached; checkpointing')

    def before_request(self):
        self.check()
        with self._lock:
            if self.started >= min(self.max_calls, self.phase_limit):
                raise CollectionStopped('collection API budget reached; checkpointing')
            self.started += 1

    def _request(self, *args, **kwargs):
        try:
            return super()._request(*args, **kwargs)
        except RateLimitUnavailable as e:
            raise CollectionStopped(str(e)) from e

    def raw(self, *args, **kwargs):
        try:
            return super().raw(*args, **kwargs)
        except RateLimitUnavailable as e:
            raise CollectionStopped(str(e)) from e

    def get(self, path, **params):
        # Completed-run artifacts are queried by telemetry and both resource
        # readers. Immutable comparisons are also shared within an invocation.
        cacheable = '/compare/' in path or path.endswith('/artifacts')
        key = (path, tuple(sorted(params.items())))
        with self.cache_lock:
            if cacheable and key in self.cache:
                return copy.deepcopy(self.cache[key])
        result = super().get(path, **params)
        if cacheable:
            with self.cache_lock:
                if len(self.cache) >= 256:
                    self.cache.clear()
                self.cache[key] = copy.deepcopy(result)
        return result

    def fit_quota(self, reserve=200):
        self.phase(1)
        try:
            remaining = self.get('rate_limit')['resources']['core']['remaining']
            self.max_calls = min(self.max_calls, self.started+max(0, remaining-reserve))
        except (CollectionStopped, Exception) as e:
            self.max_calls = self.started
            print(f'GitHub quota unavailable: {e}', flush=True)

    def phase(self, calls, seconds=None):
        self.phase_limit = min(self.max_calls, self.started + calls)
        self.phase_deadline = min(self.deadline, time.monotonic()+min(seconds,max(0,(self.deadline-time.monotonic())/2))) if seconds else self.deadline


def read_state(name):
    path = collect.ROOT / 'state' / name
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(name, value):
    path = collect.ROOT / 'state' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(value, indent=1, sort_keys=True) + '\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Checkpoint:
    def __init__(self, push=False, interval=120):
        self.push, self.interval = push, interval
        self.last = time.monotonic()
        self.prepare = None

    def __call__(self, force=False):
        if not self.push or not force and time.monotonic()-self.last < self.interval:
            return
        # Fail rather than silently collecting after a failed publication.
        # The workflow's final step retries this same completed batch.
        try:
            if self.prepare:
                self.prepare()
            subprocess.run(['python3', '-m', 'collector.checkpoint'], check=True, timeout=300)
        except Exception as e:
            raise CheckpointFailed(str(e)) from e
        self.last = time.monotonic()


def listing_source(run):
    keys = ('id', 'run_attempt', 'path', 'name', 'event', 'status', 'conclusion',
            'head_sha', 'head_branch', 'created_at', 'updated_at', 'run_started_at',
            'display_title', 'referenced_workflows')
    out = {k: run[k] for k in keys if k in run}
    for k in ('repository', 'head_repository'):
        out[k] = {'full_name': (run.get(k) or {}).get('full_name')}
    out['pull_requests'] = [{'number': p['number']} for p in run.get('pull_requests', [])]
    return out


def inventory_record(repo, run, jobs_for='all'):
    r = {'schema': collect.SCHEMA, 'collection_stage': 'inventory', 'repo': repo, 'run_id': run['id'],
         'run_attempt': run.get('run_attempt', 1), 'workflow': run.get('path') or run.get('name'),
         'event': run['event'], 'trigger': collect.trigger_class(run),
         'conclusion': run.get('conclusion'), 'head_sha': run['head_sha'],
         'head_branch': run.get('head_branch'), 'head_repo': (run.get('head_repository') or {}).get('full_name'),
         'created_at': run['created_at'], 'updated_at': run.get('updated_at'),
         'title': (run.get('display_title') or '')[:200],
         'jobs': [], '_listing': listing_source(run), 'jobs_for': jobs_for}
    if run.get('conclusion') != 'skipped':
        r['jobs_fetched'] = False
    apply_dispatch_title(r, r['title'])
    return r


def persist(index, kind, repo, records, stamp):
    if not records:
        return
    for r in records:
        r['collection_stage'] = kind
    written = collect.write_records(kind, repo, records, 'created_at', stamp)
    for path in written:
        index.db.execute('INSERT OR REPLACE INTO files VALUES (?,?)',
                         (str(path.relative_to(index.records)), path.stat().st_size))
    for r in records:
        index.ingest(kind, r)
    index.db.commit()


def coalesce(index, stamp):
    """Pack only uncommitted files before publication; committed archives stay immutable."""
    names = subprocess.check_output(['git','-C',str(collect.ROOT),'ls-files','--others',
                                     '--exclude-standard','--','records'],timeout=30).decode().splitlines()
    groups = {}
    for name in names:
        if name.endswith('.ndjson.gz'):
            path = collect.ROOT/name
            groups.setdefault(path.parent,[]).append(path)
    for parent, paths in groups.items():
        if len(paths) < 2:
            continue
        target = parent/f'{stamp}-{time.time_ns():020d}-{uuid.uuid4().hex[:12]}.ndjson.gz'
        with tempfile.NamedTemporaryFile(dir=parent,suffix='.tmp',delete=False) as f:
            temporary=Path(f.name)
        try:
            with gzip.open(temporary,'wb') as merged:
                for path in sorted(paths):
                    with gzip.open(path,'rb') as source:
                        shutil.copyfileobj(source,merged)
            os.replace(temporary,target)
            for path in paths:
                path.unlink()
                index.db.execute('DELETE FROM files WHERE path=?',(str(path.relative_to(index.records)),))
            if target.relative_to(index.records).parts[0] in index.kinds:
                index.db.execute('INSERT OR REPLACE INTO files VALUES (?,?)',
                                 (str(target.relative_to(index.records)),target.stat().st_size))
            index.db.commit()
        finally:
            temporary.unlink(missing_ok=True)


def list_window(gh, repo, since, until, page_done):
    """List all statuses in [since,until), persisting pages before retrying gaps.

    Reuse the first page's count instead of a separate count request. Split down
    to seconds, not five-minute windows that can exceed GitHub's 1,000-result cap.
    Never certify an unenumerable single-second burst as complete.
    """
    if since >= until:
        return
    query = f'{collect.iso(since)}..{collect.iso(until-dt.timedelta(seconds=1))}'
    path = f'repos/{collect.ORG}/{repo}/actions/runs'
    first = gh.get(path, created=query, per_page=100, page=1)
    total = first['total_count']
    def divide(reason, *, report=True):
        seconds = int((until-since).total_seconds())
        if seconds <= 1:
            raise RuntimeError(f'{repo} {query}: {reason} in one second; inventory coverage left open')
        if report:
            print(f'{repo} {query}: {reason}; retrying smaller inventory windows', flush=True)
        middle = since + dt.timedelta(seconds=seconds//2)
        list_window(gh, repo, since, middle, page_done)
        list_window(gh, repo, middle, until, page_done)
    if total >= 1000:
        divide('>=1000 runs', report=False)
        return
    count, seen = 0, set()
    pages = (total+99)//100
    if first['workflow_runs']:
        pages = max(1, pages)  # Retain rows even if the count incorrectly says zero.
    last_full = False
    for page in range(1, pages + 1):
        value = first if page == 1 else gh.get(path, created=query, per_page=100, page=page)
        items = value['workflow_runs']
        if not items:
            # GitHub's count and paginated listing can disagree even for a
            # historical range. Retain completed pages, then enumerate smaller
            # ranges instead of pinning this cursor on the same bad page.
            divide(f'listing page {page} disappeared (reported {total} runs)')
            return
        page_done(items)
        count += len(items)
        seen.update(r['id'] for r in items)
        last_full = len(items) == 100
    if count != total or len(seen) != total:
        divide(f'incomplete listing ({len(seen)} distinct runs, reported {total})')
    elif last_full:
        # An undercount that is a multiple of 100 otherwise looks complete.
        extra = gh.get(path, created=query, per_page=100, page=pages+1)['workflow_runs']
        if extra:
            page_done(extra)
            divide(f'listing continues past reported total {total}')


def scan(gh, index, repo, since, until, stamp, checkpoint, cursor=None, cursor_name='inventory-cursor.json', jobs_for='all'):
    """Small chronological windows; interrupted windows are replayed and deduplicated."""
    def page_done(items):
        records = []
        for run in items:
            old = index.db.execute('SELECT updated,status,required FROM runs WHERE repo=? AND id=? AND attempt=?',
                                   (repo, run['id'], run.get('run_attempt', 1))).fetchone()
            required = run.get('conclusion') != 'skipped' and collect.needs_jobs(run, jobs_for)
            if old and old[:2] == (run.get('updated_at'), run.get('status')) and old[2] >= required:
                continue
            records.append(inventory_record(repo, run, jobs_for))
        persist(index, 'inventory', repo, records, stamp)
        checkpoint()

    # A receipt covers a contiguous sequence of completed windows. Group the
    # windows into one immutable file instead of creating dozens of tiny files
    # for every overlapping scan. Even a cooperative stop preserves the prefix.
    covered_from, covered_to = since, since
    try:
        while since < until:
            gh.check()
            end = min(until, since+dt.timedelta(minutes=30))
            list_window(gh, repo, since, end, page_done)
            covered_to = end
            if cursor is not None:
                cursor[repo] = collect.iso(end)
                save_state(cursor_name, cursor)
            since = end
            checkpoint()
    finally:
        if covered_to > covered_from:
            collect.write_records('inventory-coverage', repo, [{
                'repo': repo, 'created_at': collect.iso(covered_from), 'until': collect.iso(covered_to),
                'collected_at': stamp}], 'created_at', stamp)
        checkpoint()


def observations(gh, stamp, checkpoint, until):
    cursor = read_state('observations-cursor.json')
    if 'TauCeti' in cursor and until <= collect.parse_time(cursor['TauCeti']):
        return
    since = collect.parse_time(cursor['TauCeti'])-dt.timedelta(minutes=2) if 'TauCeti' in cursor else until-dt.timedelta(days=1)
    batch = []
    def page_done(position):
        nonlocal batch
        if batch:
            collect.write_records('observations', None, batch, 'observed_at', stamp)
            batch = []
        # Records precede pagination state in every checkpoint. Replaying the
        # final page catches new observations without re-downloading the day.
        save_state('observations-pages.json', position)
        checkpoint()
    def day_done(covered):
        nonlocal batch
        if batch:
            collect.write_records('observations', None, batch, 'observed_at', stamp)
            batch = []
        cursor['TauCeti'] = collect.iso(covered)
        save_state('observations-cursor.json', cursor)
        checkpoint()
    # Records are durable in small batches; the cursor advances only at a
    # completed archive day. Interrupted pages are replayed safely.
    for observation in read_days(since, until, before_request=gh.check, day_done=day_done,
                                 resume=read_state('observations-pages.json') or None, page_done=page_done):
        gh.check()
        batch.append(observation)
        if len(batch) >= 200:
            collect.write_records('observations', None, batch, 'observed_at', stamp)
            batch = []
            checkpoint()
    if batch:
        collect.write_records('observations', None, batch, 'observed_at', stamp)
    cursor['TauCeti'] = collect.iso(until)
    save_state('observations-cursor.json', cursor)
    checkpoint()


def merge_members(gh, index, stamp, checkpoint, limit=80):
    rows = index.db.execute('''SELECT r.repo,r.id,r.attempt,r.created,r.batch,r.head
        FROM runs r LEFT JOIN batches b ON b.repo=r.repo AND b.batch=r.batch AND b.head=r.head
        WHERE r.status='completed' AND r.batch IS NOT NULL AND r.head IS NOT NULL AND b.members IS NULL
        ORDER BY r.created DESC LIMIT ?''', (limit,)).fetchall()
    tried = set()
    for repo, run_id, attempt, created, batch, head in rows:
        gh.check()
        if (repo, batch, head) in tried:
            continue
        tried.add((repo, batch, head))
        try:
            members = batch_members(batch, head)
        except Exception as e:
            print(f'batch {batch}: membership deferred ({type(e).__name__})', flush=True)
            continue
        r = {'repo': repo, 'run_id': run_id, 'run_attempt': attempt,
             'created_at': created, 'batch_id': batch, 'head_sha': head, 'members': members}
        persist(index, 'merge-members', repo, [r], stamp)
        checkpoint()


def refresh_active(gh,index,stamp,checkpoint,until):
    rows = index.db.execute("""SELECT r.repo,r.id,MIN(r.created) FROM runs r
        LEFT JOIN active_refresh a ON a.repo=r.repo AND a.id=r.id
        WHERE r.status!='completed' AND r.created<? AND COALESCE(a.next_refresh,0)<=?
        GROUP BY r.repo,r.id ORDER BY COALESCE(a.next_refresh,0),r.created LIMIT 50""",
        (collect.iso(until-collect.OVERLAP),time.time())).fetchall()
    for repo,number,created in rows:
        gh.check()
        try:
            source = gh.get(f'repos/{collect.ORG}/{repo}/actions/runs/{number}')
            if source['id'] != number:
                raise ValueError('run endpoint returned mismatched identity')
            persist(index,'inventory',repo,[inventory_record(repo,source)],stamp)
        except CollectionStopped:
            raise
        except Exception as e:
            print(f'{repo} active run {number}: deferred {type(e).__name__}: {e}',flush=True)
        index.db.execute('INSERT OR REPLACE INTO active_refresh VALUES (?,?,?)',(repo,number,time.time()+3600))
        index.db.commit()
        checkpoint()


def run_specific_error(error):
    """Shared API/network outages must never retire otherwise recoverable runs."""
    if isinstance(error,urllib.error.HTTPError):
        return 400 <= error.code < 500 and error.code not in (401,403,408,429)
    return isinstance(error,(KeyError,ValueError,TypeError))


def earlier_attempts(gh, index, stamp, checkpoint):
    while True:
        pending = index.attempts()
        if not pending:
            return
        for repo, number, attempt, created in pending:
            gh.check()
            run = {'id': number, 'run_attempt': attempt}
            try:
                source = gh.get(f'repos/{collect.ORG}/{repo}/actions/runs/{number}/attempts/{attempt}')
                if source['id'] != number or source.get('run_attempt',1) != attempt:
                    raise ValueError('attempt endpoint returned mismatched identity')
                persist(index, 'inventory', repo, [inventory_record(repo, source)], stamp)
            except CollectionStopped:
                raise
            except Exception as e:
                failures = index.failures(repo,run,'attempt')+int(run_specific_error(e))
                permanent = isinstance(e,urllib.error.HTTPError) and e.code in (404,410)
                record = {'repo': repo, 'run_id': number, 'run_attempt': attempt,
                          'created_at': created, 'stream': 'attempt', 'failures': failures,
                          'reason': f'{type(e).__name__}: {e}'[:500], 'retry_after': time.time()+3600}
                persist(index, 'attempt-gaps' if permanent or failures >= 3 else 'collection-errors', repo, [record], stamp)
            checkpoint()


def enrich(gh, index, stamp, checkpoint, *, build, detail=False, recent=None):
    enrichers = {}
    completed = {}
    last_flush = time.monotonic()
    publication_failure = None
    def flush():
        nonlocal last_flush
        for repo, records in completed.items():
            persist(index, 'runs' if detail else 'jobs', repo, records, stamp)
        completed.clear()
        last_flush = time.monotonic()
        if publication_failure is None:
            checkpoint()
    while True:
        gh.check()
        pending = index.pending(build=build, detail=detail, recent=recent)
        if not pending:
            return
        def one(item):
            repo, run, previous = item
            if detail:
                enricher = enrichers.setdefault(repo, collect.Enricher(gh, repo))
                record = collect.run_record(gh, repo, run, enricher, 'all',
                                            preloaded_record=previous, batch_fallback=False)
            else:
                record = collect.run_record(gh, repo, run, None, 'all',
                                            current_attempt_only=True, keep_steps=build)
                record['_listing'] = run
                # Preserve cheap dispatch identity for job-only snapshots.
                record['title'] = (run.get('display_title') or '')[:200]
                apply_dispatch_title(record, record['title'])
            return repo, record
        stopped = None
        # Only a bounded batch is queued. Persist small completed batches even
        # when another future reaches a limit; avoid a gzip/git object per run.
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(one, item) for item in pending]
            for future in as_completed(futures):
                try:
                    repo, record = future.result()
                except CollectionStopped as e:
                    stopped = e
                    continue
                except Exception as e:
                    item = pending[futures.index(future)]
                    repo, run, _ = item
                    stream = 'details' if detail else 'jobs'
                    failures = index.failures(repo, run, stream)+int(run_specific_error(e))
                    message = f'{type(e).__name__}: {e}'[:500]
                    permanent = isinstance(e, urllib.error.HTTPError) and e.code in (404,410)
                    if permanent or failures >= 3:
                        record = copy.deepcopy(item[2])
                        record[stream+'_gap'] = {'reason': message, 'failures': failures}
                        if not detail:
                            record['jobs_fetched'] = False
                        persist(index, 'runs' if detail else 'jobs', repo, [record], stamp)
                        print(f'{repo} run {run["id"]}: {stream} unavailable ({message})', flush=True)
                    else:
                        receipt = {'repo': repo, 'run_id': run['id'], 'run_attempt': run.get('run_attempt',1),
                                   'stream': stream, 'created_at': run['created_at'], 'failures': failures,
                                   'retry_after': time.time()+3600, 'reason': message}
                        persist(index, 'collection-errors', repo, [receipt], stamp)
                        print(f'{repo} run {run["id"]}: deferred {message}', flush=True)
                    continue
                completed.setdefault(repo, []).append(record)
                if sum(map(len, completed.values())) >= 16 or time.monotonic()-last_flush >= 5:
                    try:
                        flush()
                    except CheckpointFailed as e:
                        publication_failure = e
                        gh.stop_requested = True
        flush()
        if publication_failure:
            raise publication_failure
        if stopped:
            raise stopped


def run(gh, index, repos, until, stamp, checkpoint, since=None, discover=False, jobs_for='all', requested_until=None, analysis=False):
    index.sync()
    legacy = read_state('cursor.json')
    inventory = read_state('inventory-cursor.json') or dict(legacy)
    errors = []
    backfill_name = 'backfills/'+hashlib.sha256((collect.iso(since)+'..'+collect.iso(requested_until or until)+'..'+jobs_for).encode()).hexdigest()[:32]+'.json' if since else None
    backfill = read_state(backfill_name) if since else None
    def coverage():
        if not since:
            full, builds = dict(legacy), read_state('build-cursor.json')
            details = read_state('details-cursor.json')
            for repo, end in inventory.items():
                full[repo] = index.coverage(repo, end)
                builds[repo] = index.coverage(repo, end, build=True)
                details[repo] = index.coverage(repo, end, build=True, detail=True)
            save_state('cursor.json', full)
            save_state('build-cursor.json', builds)
            save_state('details-cursor.json', details)
    if isinstance(checkpoint, Checkpoint):
        def prepare():
            coalesce(index,stamp)
            coverage()
        checkpoint.prepare = prepare
    def phase(label, calls, work, seconds=None):
        gh.phase(calls, seconds=seconds)
        try:
            work()
        except (CollectionStopped, RateLimitUnavailable) as e:
            print(f'{label}: {e}', flush=True)
        except Exception as e:
            errors.append(f'{label}: {type(e).__name__}: {e}')
            print(errors[-1], flush=True)
        finally:
            checkpoint()

    if not since:
        phase('observations', 0, lambda: observations(gh, stamp, checkpoint, until), seconds=120)
    if hasattr(gh, 'fit_quota'):
        gh.fit_quota()
    if discover:
        def discover_repositories():
            active = [r['name'] for r in gh.paginate(f'orgs/{collect.ORG}/repos') if not r.get('archived')]
            repos[:] = active
            save_state('repositories.json', {'observed_at': stamp, 'active': sorted(active)})
        # Repository discovery is optional when GitHub's quota is exhausted;
        # saved repositories and the independent Bors stream remain collectable.
        phase('repository discovery', 20, discover_repositories, seconds=30)
    # Main history is needed for landing dates and throughput, so reserve its
    # small budget before optional artifact/log enrichment.
    def main_history():
        main_cursor = read_state('main-cursor.json')
        for repo in set(repos) & collect.DETAIL_REPOS:
            start = since or collect.parse_time(main_cursor.get(repo, legacy.get(repo, collect.iso(until)))) - collect.OVERLAP
            collect.collect_main(gh, repo, start, until, stamp, False)
            if not since:
                previous = collect.parse_time(main_cursor.get(repo,legacy.get(repo,collect.iso(until))))
                main_cursor[repo] = collect.iso(max(previous,until))
                save_state('main-cursor.json', main_cursor)
    phase('main history', max(1, gh.max_calls//20), main_history, seconds=120)
    phase('older active metadata',max(1,gh.max_calls//50),
          lambda: refresh_active(gh,index,stamp,checkpoint,until),seconds=60)
    # Give every repository a recent scan before spending the discovery budget
    # on an older backlog. This never advances the historical cursor across a gap.
    inventory_budget = max(1, gh.max_calls*2//5)
    gh.phase(inventory_budget, seconds=600)
    for repo in sorted(repos, key=lambda r: r not in collect.DETAIL_REPOS) if not since else []:
        try:
            recent_start = until-collect.OVERLAP
            scan(gh, index, repo, recent_start, until, stamp, checkpoint)
            if repo in inventory and recent_start <= collect.parse_time(inventory[repo]) <= until:
                # The recent scan overlaps the contiguous prefix: reuse its
                # proof rather than listing the same tail a second time.
                inventory[repo] = collect.iso(until)
                save_state('inventory-cursor.json',inventory)
        except CollectionStopped as e:
            print(f'recent inventory: {e}', flush=True)
            break
        except Exception as e:
            errors.append(f'{repo} recent inventory: {type(e).__name__}: {e}')
            print(errors[-1], flush=True)
    for repo in repos:
        start = collect.parse_time(backfill.get(repo, collect.iso(since))) if since else collect.parse_time(inventory.get(repo, collect.iso(until-dt.timedelta(days=1))))
        try:
            scan(gh, index, repo, start, until, stamp, checkpoint, backfill if since else inventory,
                 backfill_name if since else 'inventory-cursor.json', jobs_for=jobs_for)
        except CollectionStopped as e:
            print(f'historical inventory: {e}', flush=True)
            break
        except Exception as e:
            errors.append(f'{repo} historical inventory: {type(e).__name__}: {e}')
            print(errors[-1], flush=True)
    phase('merge membership', 0, lambda: merge_members(gh, index, stamp, checkpoint), seconds=60)
    phase('earlier attempt metadata', max(1, gh.max_calls//20),
          lambda: earlier_attempts(gh,index,stamp,checkpoint), seconds=120)
    remaining = max(0, gh.max_calls-gh.started)
    phase('recent build jobs', remaining//4, lambda: enrich(
        gh, index, stamp, checkpoint, build=True, recent=collect.iso(until-collect.OVERLAP)))
    phase('historical build jobs', remaining//4,
          lambda: enrich(gh, index, stamp, checkpoint, build=True))
    if jobs_for == 'all':
        phase('sampled auxiliary jobs', max(0, gh.max_calls-gh.started-min(200, remaining//10)),
          lambda: enrich(gh, index, stamp, checkpoint, build=False))
    if analysis:
        phase('optional details', max(0, gh.max_calls-gh.started),
              lambda: enrich(gh, index, stamp, checkpoint, build=True, detail=True,
                             recent=collect.iso(until-dt.timedelta(days=1))))
    coverage()
    checkpoint(force=True)
    index.sync()
    print(f'GitHub request attempts: {gh.started}; pending jobs: '
          f'{index.db.execute("SELECT COUNT(*) FROM runs WHERE jobs_done=0").fetchone()[0]}', flush=True)
    if errors:
        raise RuntimeError('; '.join(errors))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--max-calls', type=int, default=4500)
    ap.add_argument('--max-seconds', type=int, default=1800)
    ap.add_argument('--index', default='.cache/collector.sqlite')
    ap.add_argument('--push', action='store_true', help='publish checkpoints at most two minutes apart')
    ap.add_argument('--repos', default=None)
    ap.add_argument('--jobs-for', choices=['build', 'all'], default='all')
    ap.add_argument('--analysis', action='store_true',
                    help='explicitly collect optional artifacts, logs and commit comparisons')
    ap.add_argument('--since')
    ap.add_argument('--until')
    from tauceti_tools.project import conf_argument
    args = ap.parse_args(conf_argument(list(__import__('sys').argv[1:] if argv is None else argv))[1])
    if os.environ.get('TAUCETI_CONF') and not args.repos:
        args.repos = os.environ['TAUCETI_REPO'].split('/')[1]
    gh = BoundedGitHub(args.max_calls, args.max_seconds)
    # Signals request a cooperative stop; no signal handler performs I/O or
    # commits half a record. Leave time for checkpoint/push before the hard timeout.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: setattr(gh, 'stop_requested', True))
    index = Index(args.index, collect.RECORDS)
    checkpoint = Checkpoint(args.push)
    try:
        repos = args.repos.split(',') if args.repos else sorted(
            set(read_state('repositories.json').get('active') or
                (set(read_state('cursor.json')) | set(read_state('inventory-cursor.json')) | {'TauCeti'})))
        now = dt.datetime.now(collect.UTC).replace(microsecond=0)
        requested_until = collect.parse_time(args.until).replace(microsecond=0) if args.until else now
        until = min(now,requested_until)
        since = collect.parse_time(args.since).replace(microsecond=0) if args.since else None
        if since is not None and since >= until:
            ap.error('--since must precede the effective end time')
        stamp = dt.datetime.now(collect.UTC).strftime('%Y%m%dT%H%M%S%fZ')
        run(gh, index, repos, until, stamp, checkpoint,
            since,
            discover=not args.repos, jobs_for=args.jobs_for, requested_until=requested_until, analysis=args.analysis)
    finally:
        try:
            checkpoint(force=True)
        finally:
            index.close()


if __name__ == '__main__':
    main()
