"""Disposable index of append-only records. Git records, never this cache, establish coverage."""
import os

import gzip
import json
import sqlite3
import time
from pathlib import Path

from . import collect


class Index:
    kinds = ('inventory', 'jobs', 'runs', 'merge-members', 'collection-errors', 'attempt-gaps')

    def __init__(self, path, records):
        self.records = Path(records)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, size INTEGER);
            CREATE TABLE IF NOT EXISTS runs (
              repo TEXT, id INTEGER, attempt INTEGER, created TEXT, workflow TEXT,
              status TEXT, updated TEXT, source TEXT, jobs TEXT, rank INTEGER DEFAULT -1,
              jobs_done INTEGER DEFAULT 0, detail_done INTEGER DEFAULT 0,
              retry_after REAL DEFAULT 0,
              required INTEGER, build INTEGER, batch INTEGER, head TEXT,
              PRIMARY KEY(repo,id,attempt));
            CREATE INDEX IF NOT EXISTS pending_jobs ON runs(jobs_done,build,created);
            CREATE INDEX IF NOT EXISTS pending_details ON runs(detail_done,build,created);
            CREATE TABLE IF NOT EXISTS active_refresh (repo TEXT,id INTEGER,next_refresh REAL,PRIMARY KEY(repo,id));
            CREATE TABLE IF NOT EXISTS attempt_jobs (repo TEXT,id INTEGER,attempt INTEGER,
              PRIMARY KEY(repo,id,attempt));
            CREATE TABLE IF NOT EXISTS missing_attempts (repo TEXT,id INTEGER,attempt INTEGER,created TEXT,
              PRIMARY KEY(repo,id,attempt));
            CREATE TABLE IF NOT EXISTS errors (repo TEXT, id INTEGER, attempt INTEGER, stream TEXT,
              failures INTEGER, retry_after REAL, PRIMARY KEY(repo,id,attempt,stream));
            CREATE TABLE IF NOT EXISTS batches (
              repo TEXT, batch INTEGER, head TEXT, members TEXT,
              PRIMARY KEY(repo,batch,head));
        ''')

    def close(self):
        self.db.close()

    @staticmethod
    def source(record):
        """Enough API provenance for deferred enrichment, without a second run lookup."""
        if record.get('_listing'):
            return record['_listing']
        return {'id': record['run_id'], 'run_attempt': record['run_attempt'],
                'path': record['workflow'], 'event': record['event'],
                'head_sha': record['head_sha'], 'head_branch': record.get('head_branch'),
                'head_repository': {'full_name': record.get('head_repo')},
                'repository': {'full_name': record.get('workflow_repository')},
                'referenced_workflows': record.get('referenced_workflows', []),
                'display_title': record.get('title'), 'created_at': record['created_at'],
                'updated_at': record.get('updated_at'), 'conclusion': record.get('conclusion'),
                'status': 'completed',
                'run_started_at': record.get('run_started_at')}

    def ingest(self, kind, r):
        if kind == 'attempt-gaps':
            key = (r['repo'],r['run_id'],r['run_attempt'])
            self.db.execute('INSERT OR IGNORE INTO attempt_jobs VALUES (?,?,?)', key)
            self.db.execute('DELETE FROM missing_attempts WHERE repo=? AND id=? AND attempt=?', key)
            self.db.execute("UPDATE runs SET jobs_done=1,detail_done=1,status='completed' WHERE repo=? AND id=? AND attempt=?", key)
            return
        if kind == 'collection-errors':
            self.db.execute('''INSERT INTO errors VALUES (?,?,?,?,?,?)
                ON CONFLICT(repo,id,attempt,stream) DO UPDATE SET
                failures=MAX(errors.failures,excluded.failures),retry_after=MAX(errors.retry_after,excluded.retry_after)''',
                (r['repo'],r['run_id'],r['run_attempt'],r['stream'],r['failures'],r['retry_after']))
            self.db.execute('UPDATE runs SET retry_after=MAX(retry_after,?) WHERE repo=? AND id=? AND attempt=?',
                            (r['retry_after'],r['repo'],r['run_id'],r['run_attempt']))
            return
        if kind == 'merge-members':
            self.db.execute('INSERT OR REPLACE INTO batches VALUES (?,?,?,?)',
                            (r['repo'], r['batch_id'], r['head_sha'], json.dumps(r['members'])))
            return
        source = self.source(r)
        workflow = r['workflow']
        build = workflow in collect.BUILD_WORKFLOWS
        required = r.get('conclusion') != 'skipped' and collect.needs_jobs(source, r.get('jobs_for', 'all'))
        # Historical backfill/sampling decisions remain valid. Newly deferred
        # jobs are explicit inventory records, not silently abandoned samples.
        jobs_done = kind != 'inventory' or not required
        detail_done = (kind == 'runs' or r.get('conclusion') == 'skipped'
                       or not (build and r['repo'] in collect.DETAIL_REPOS))
        tested = r.get('tested') or {}
        self.db.execute('''INSERT INTO runs
            (repo,id,attempt,created,workflow,status,updated,source,required,build,batch,head)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(repo,id,attempt) DO UPDATE SET
            source=CASE WHEN excluded.updated >= COALESCE(runs.updated,'') THEN excluded.source ELSE runs.source END,
            status=CASE WHEN excluded.updated >= COALESCE(runs.updated,'') THEN excluded.status ELSE runs.status END,
            updated=MAX(COALESCE(runs.updated,''),COALESCE(excluded.updated,'')),
            batch=COALESCE(excluded.batch,runs.batch),head=COALESCE(excluded.head,runs.head),
            jobs_done=CASE WHEN excluded.required=1 AND runs.required=0 AND runs.rank=0 THEN 0 ELSE runs.jobs_done END,
            required=MAX(runs.required,excluded.required)
            ''', (r['repo'], r['run_id'], r['run_attempt'], r['created_at'], workflow,
                  source.get('status', 'completed'), r.get('updated_at') or r['created_at'],
                  json.dumps(source), int(required), int(build), tested.get('batch_id'),
                  tested.get('head_sha') if tested.get('batch_id') else None))
        key = (r['repo'], r['run_id'], r['run_attempt'])
        rank = {'inventory': 0, 'jobs': 1, 'runs': 2}[kind]
        self.db.execute('''UPDATE runs SET jobs=?,rank=?
            WHERE repo=? AND id=? AND attempt=? AND rank <= ?''',
                        (json.dumps(r), rank, *key, rank))
        self.db.execute('''UPDATE runs SET jobs_done=MAX(jobs_done,?),detail_done=MAX(detail_done,?)
            WHERE repo=? AND id=? AND attempt=?''', (int(jobs_done), int(detail_done), *key))
        known = {j.get('run_attempt') for j in r.get('jobs', [])} - {None}
        if jobs_done:
            known.add(r['run_attempt'])
        for attempt in known:
            self.db.execute('INSERT OR IGNORE INTO attempt_jobs VALUES (?,?,?)', (r['repo'],r['run_id'],attempt))
            self.db.execute('DELETE FROM missing_attempts WHERE repo=? AND id=? AND attempt=?', (r['repo'],r['run_id'],attempt))
        if collect.needs_jobs(source, r.get('jobs_for', 'all')):
            for attempt in range(1,r['run_attempt']):
                exists = self.db.execute("""SELECT 1 FROM attempt_jobs WHERE repo=? AND id=? AND attempt=?
                    UNION ALL SELECT 1 FROM runs WHERE repo=? AND id=? AND attempt=? AND status='completed'""",
                    (r['repo'],r['run_id'],attempt,r['repo'],r['run_id'],attempt)).fetchone()
                if not exists:
                    self.db.execute('INSERT OR IGNORE INTO missing_attempts VALUES (?,?,?,?)',
                                    (r['repo'],r['run_id'],attempt,r['created_at']))
        if source.get('status') == 'completed':
            self.db.execute('DELETE FROM missing_attempts WHERE repo=? AND id=? AND attempt=?', key)
        retry = self.db.execute('SELECT MAX(retry_after) FROM errors WHERE repo=? AND id=? AND attempt=?', key).fetchone()[0]
        if retry:
            self.db.execute('UPDATE runs SET retry_after=MAX(retry_after,?) WHERE repo=? AND id=? AND attempt=?', (retry,*key))
        self.db.execute('''UPDATE runs SET jobs=NULL,source=NULL
            WHERE repo=? AND id=? AND attempt=? AND jobs_done=1 AND detail_done=1 AND status='completed' ''', key)
        if tested.get('batch_members') and tested.get('batch_id'):
            self.ingest('merge-members', {'repo': r['repo'], 'batch_id': tested['batch_id'],
                        'head_sha': tested['head_sha'], 'members': tested['batch_members']})

    def sync(self):
        """Read only new/grown files. Reject cache entries absent from this checkout."""
        kinds = self.kinds
        current = {str(p.relative_to(self.records)): (p, p.stat().st_size)
                   for kind in kinds for p in (self.records/kind).rglob('*.ndjson.gz')}
        old = dict(self.db.execute('SELECT path,size FROM files'))
        if any(path not in current or current[path][1] < size for path, size in old.items()):
            # A cache from a newer/different checkout cannot lend it records.
            for table in ('files', 'runs', 'batches', 'errors', 'attempt_jobs', 'missing_attempts', 'active_refresh'):
                self.db.execute(f'DELETE FROM {table}')
            old = {}
        read = 0
        for name, (path, size) in sorted(current.items(), key=lambda item: (kinds.index(item[0].split('/')[0]), item[0])):
            if old.get(name) == size:
                continue
            with gzip.open(path, 'rt') as source:
                for line in source:
                    self.ingest(name.split('/')[0], json.loads(line))
            self.db.execute('INSERT OR REPLACE INTO files VALUES (?,?)', (name, size))
            read += 1
        # Old optional source/job payloads cannot recover expired logs/artifacts.
        # Keep the small coverage row explicit rather than claiming completed details.
        cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 90 * 86400))
        self.db.execute("UPDATE runs SET jobs=NULL,source=NULL WHERE jobs_done=1 AND status='completed' AND created<?", (cutoff,))
        self.db.commit()
        return read

    def attempts(self, limit=50):
        return self.db.execute("""SELECT m.repo,m.id,m.attempt,m.created FROM missing_attempts m
            LEFT JOIN errors e ON e.repo=m.repo AND e.id=m.id AND e.attempt=m.attempt AND e.stream='attempt'
            WHERE COALESCE(e.retry_after,0) <= ? ORDER BY m.created,m.id,m.attempt LIMIT ?""",
            (time.time(),limit)).fetchall()

    def failures(self, repo, run, stream):
        row = self.db.execute('SELECT failures FROM errors WHERE repo=? AND id=? AND attempt=? AND stream=?',
                              (repo,run['id'],run.get('run_attempt',1),stream)).fetchone()
        return row[0] if row else 0

    def pending(self, *, build, detail=False, recent=None, limit=32):
        done = 'detail_done' if detail else 'jobs_done'
        condition = 'AND jobs_done=1 AND source IS NOT NULL' if detail else ''
        args = [int(build)]
        if recent:
            condition += ' AND created >= ?'
            args.append(recent)
        # Every attempt has its own work item. Never refetch previous attempts'
        # job pages when a new attempt appears.
        rows = self.db.execute(f'''SELECT repo,source,jobs FROM runs
            WHERE {done}=0 AND build=? AND status='completed' {condition}
              AND retry_after <= ?
            ORDER BY created {'DESC' if detail else 'ASC'},id,attempt LIMIT ?''', (*args, time.time(), limit)).fetchall()
        return [(repo, json.loads(source), json.loads(jobs or '{}')) for repo, source, jobs in rows]

    def coverage(self, repo, through, *, build=False, detail=False):
        condition = 'AND build=1' if build else ''
        # Active workflows are not finished-run coverage. A historical stuck
        # workflow remains an explicit inventory item, but cannot pin forever.
        stuck = collect.iso(collect.parse_time(through) - collect.OVERLAP)
        done = 'detail_done' if detail else 'jobs_done'
        row = self.db.execute(f'''SELECT MIN(created) FROM runs WHERE repo=?
            AND (status='completed' AND {done}=0 AND required=1
                 OR status!='completed' AND created >= ?)
            {condition}''', (repo, stuck)).fetchone()[0]
        missing = self.db.execute(f"""SELECT MIN(m.created) FROM missing_attempts m
            JOIN runs r ON r.repo=m.repo AND r.id=m.id WHERE m.repo=? {condition}""", (repo,)).fetchone()[0]
        return min(t for t in (through,row,missing) if t is not None)
