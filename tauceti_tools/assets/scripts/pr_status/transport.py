"""Per-batch GitHub clients sharing quota holds, without sharing credentials.

All responses go through the existing structured client. A refusal stops new
assessments immediately; no per-PR quota probe or retry sleep is needed.
"""

import json
import re
import subprocess
import threading

from github_api import APIError, GitHub, QuotaState


class ReconciliationAPI:
    def __init__(self, holds=()):
        self.quota = QuotaState()
        for hold in holds:
            self.quota.blocked[(hold['credential'], hold['resource'])] = hold['until']
        self.local = threading.local()
        self.clients = []
        self.lock = threading.Lock()
        self.label_lock = threading.Lock()
        self.known_labels = set()

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
