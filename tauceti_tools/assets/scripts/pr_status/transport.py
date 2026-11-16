"""Per-batch GitHub clients sharing quota holds, without sharing credentials.

All responses go through the existing structured client. A refusal stops new
assessments immediately; no per-PR quota probe or retry sleep is needed.
"""

import json
import os
import re
import subprocess
import threading
import tempfile
import time

from github_api import APIError, GitHub, QuotaState


class ReconciliationAPI:
    def __init__(self, holds=(), *, decisions=None, comparisons=None):
        self.quota = QuotaState()
        for hold in holds:
            self.quota.blocked[(hold['credential'], hold['resource'])] = hold['until']
        self.local = threading.local()
        self.clients = []
        self.lock = threading.Lock()
        self.label_lock = threading.Lock()
        self.decision_lock = threading.RLock()
        self.decisions = decisions if decisions is not None else {}
        self.policy = None
        self.cache_hits = 0
        self.comparisons = comparisons if comparisons is not None else {}
        self.git_stats = dict(fetches=0, prepare_seconds=0, cache_bytes=0, reused_comparisons=0, reused_paths=0)
        self.known_labels = set()
        self.shared_evidence = None
        self.git_cache = None
        if os.environ.get('TAUCETI_PR_STATUS_SHARED_EVIDENCE') == 'true':
            from shared import Evidence
            self.shared_evidence = Evidence(os.environ.get('GH_REPO', 'TauCetiProject/TauCeti'))
            self.git_directory = tempfile.TemporaryDirectory(prefix='pr-status-git-inputs-')
            self.git_cache = self.git_directory.name
            from shared import policy_identity
            self.policy = policy_identity(os.environ.get('TAUCETI_REVIEW_RUNNER', '.tauceti-review/runner'))

    def merge_base(self, repository, number, base, head):
        from tauceti_tools import git_inputs
        identity = dict(repository=repository, base=base, head=head)
        with self.decision_lock:
            cached = self.comparisons.get(str(number), {})
            if (all(cached.get(k) == v for k, v in identity.items())
                    and re.fullmatch('[0-9a-f]{40}', cached.get('merge_base', ''))):
                self.git_stats['reused_comparisons'] += 1
                return cached['merge_base']
        def run_git(directory, args, *a, **kw):
            if args[0] == 'fetch':
                with self.decision_lock:
                    self.git_stats['fetches'] += 1
            return git_inputs._git(directory, args, *a, **kw)
        started = time.monotonic()
        value = git_inputs.merge_base('https://github.com/'+repository, base, head,
                                     cache_dir=self.git_cache, run_git=run_git)
        with self.decision_lock:
            self.git_stats['prepare_seconds'] += time.monotonic()-started
            self.git_stats['cache_bytes'] = git_inputs._du(self.git_cache)
            record = dict(identity, merge_base=value)
            if (cached.get('repository') == repository and cached.get('head') == head
                    and cached.get('merge_base') == value and isinstance(cached.get('paths'), list)):
                record['paths'] = cached['paths']
            self.comparisons[str(number)] = record
        return value

    def changed_paths(self, repository, number, merge_base, head):
        from readiness import diff_engine
        with self.decision_lock:
            record = self.comparisons.get(str(number), {})
            if (record.get('repository') == repository and record.get('head') == head
                    and record.get('merge_base') == merge_base and isinstance(record.get('paths'), list)
                    and all(isinstance(path, str) for path in record['paths'])):
                self.git_stats['reused_paths'] += 1
                return list(record['paths'])
        paths = diff_engine().git_diff('https://github.com/'+repository, merge_base, head)
        with self.decision_lock:
            record = self.comparisons.get(str(number), {})
            if (record.get('repository') == repository and record.get('head') == head
                    and record.get('merge_base') == merge_base):
                record['paths'] = list(paths)
        return paths

    def client(self):
        if not hasattr(self.local, 'client'):
            self.local.client = GitHub(max_wait=0, quota_state=self.quota)
            with self.lock:
                self.clients.append(self.local.client)
        return self.local.client

    def holds(self):
        with self.quota.lock:
            return [dict(credential=c, resource=r, until=t)
                    for (c, r), t in sorted(self.quota.blocked.items())]

    def read(self, path, *, jq=None, paginate=False):
        client, bodies = self.client(), []
        while True:
            bodies.append(json.dumps(client.request('GET', path)))
            link = client.last_headers.get('link', '')
            next_page = re.search(r'<https://api\.github\.com/([^>]+)>; rel="next"', link)
            if not paginate or not next_page:
                break
            path = next_page[1]
        text = '\n'.join(bodies)
        if jq is None:
            return text
        out = subprocess.run(['jq', '-rc', jq], input=text, capture_output=True, text=True)
        if out.returncode:
            raise RuntimeError('Could not filter GitHub JSON response')
        return out.stdout

    def run(self, args):
        """Compatibility for label operations; quota refusals remain exceptions."""
        method, path, data = 'GET', None, {}
        it = iter(args)
        for arg in it:
            if arg == '--method':
                method = next(it)
            elif arg in {'-f', '-F'}:
                key, value = next(it).split('=', 1)
                if key.endswith('[]'):
                    data.setdefault(key[:-2], []).append(value)
                else:
                    data[key] = value
            elif path is None:
                path = arg
            else:
                raise ValueError('Unsupported label request argument')
        try:
            result = self.client().request(method, path, data or None)
            return subprocess.CompletedProcess(args, 0, json.dumps(result), '')
        except APIError as exc:
            return subprocess.CompletedProcess(args, 1, '', str(exc))

    def report(self):
        for client in self.clients:
            client.report()
        if self.shared_evidence is not None:
            print('github-state-summary: ' + json.dumps(dict(self.shared_evidence.state.stats,
                                                           unchanged_decisions=self.cache_hits), sort_keys=True))
            print('pr-status-git-inputs: ' + json.dumps(self.git_stats, sort_keys=True))
