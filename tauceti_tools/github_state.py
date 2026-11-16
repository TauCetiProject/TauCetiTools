"""Shared GitHub-state reads for Tools consumers, with explicit coverage.

An unavailable resource requests repair through the existing authenticated query
path. It never triggers a direct GitHub fallback in this client.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_URL = 'https://github-state.taucetiproject.org'
MAX_BYTES = 32 << 20


class Unavailable(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('shared GitHub evidence unavailable: ' + reason)


class State:
    def __init__(self, repository, *, url=None, token=None):
        self.repository = repository
        self.url = (url or os.environ.get('TAUCETI_GITHUB_STATE_URL', DEFAULT_URL)).rstrip('/')
        parsed = urllib.parse.urlsplit(self.url)
        if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1'})
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Invalid shared-state URL')
        self.token = token if token is not None else os.environ.get('TAUCETI_GITHUB_STATE_TOKEN', '')
        if not self.token and os.environ.get('TAUCETI_GITHUB_STATE_TOKEN_FILE'):
            self.token = Path(os.environ['TAUCETI_GITHUB_STATE_TOKEN_FILE']).expanduser().read_text().strip()
        self.lock = threading.Lock()
        self.stats = {'requests': 0, 'complete': 0, 'unavailable': 0}
        self.transport_hold = 0

    def _get(self, endpoint, **params):
        with self.lock:
            if self.transport_hold > time.monotonic():
                raise Unavailable('transport_hold')
        headers = {'User-Agent': 'TauCetiTools/shared-evidence', 'Accept': 'application/json',
                   'Cache-Control': 'no-cache'}
        request_id = uuid.uuid4().hex
        headers['X-Request-ID'] = request_id
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        request = urllib.request.Request(self.url + '/v1/' + endpoint + '?' + urllib.parse.urlencode(
            dict(repository=self.repository, request_id=request_id, **params)), headers=headers)
        with self.lock:
            self.stats['requests'] += 1
            operation = params.get('operation', endpoint)
            operations = self.stats.setdefault('operations', {})
            operations[operation] = operations.get(operation, 0) + 1
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read(MAX_BYTES + 1)
                checked_header = getattr(response, 'headers', {}).get('X-TauCeti-Checked-Revision')
            if len(body) > MAX_BYTES:
                raise Unavailable('response_too_large')
            value = json.loads(body)
        except (OSError, ValueError) as error:
            # Transport diagnostics may contain URLs or credentials; retain only the class.
            with self.lock:
                self.transport_hold = max(self.transport_hold, time.monotonic() + 30)
            raise Unavailable('transport_' + type(error).__name__) from None
        if not isinstance(value, dict):
            raise Unavailable('invalid_response')
        if checked_header is not None:
            if not checked_header.isdigit():
                raise Unavailable('invalid_checked_revision')
            value['checked_revision'] = int(checked_header)
        return value

    def query(self, operation, **args):
        pages, cursor, seen, revision, total_bytes, checked = [], None, set(), None, 0, None
        while True:
            params = dict(operation=operation, args=json.dumps(args, sort_keys=True))
            if cursor:
                params['cursor'] = cursor
            value = self._get('query', **params)
            if (value.get('version') != 1 or value.get('repository') != self.repository
                    or value.get('operation') != operation or value.get('status') != 'ok'
                    or value.get('complete') is not True or value.get('coverage') != 'complete'
                    or type(value.get('revision')) is not int or 'data' not in value):
                with self.lock:
                    self.stats['unavailable'] += 1
                raise Unavailable('coverage_' + operation)
            if revision is not None and value['revision'] != revision:
                raise Unavailable('mixed_pagination_revision')
            revision = value['revision']
            if checked is None:
                checked = value.get('checked_revision', revision)
                if type(checked) is not int or checked < revision:
                    raise Unavailable('invalid_checked_revision')
            total_bytes += len(json.dumps(value['data']).encode())
            if total_bytes > 2*MAX_BYTES or len(pages) >= 1000:
                raise Unavailable('snapshot_too_large')
            pages.append(value['data'])
            cursor = value.get('cursor')
            if not cursor:
                break
            if not isinstance(cursor, str) or len(cursor) > 512 or cursor in seen or not isinstance(value['data'], list):
                raise Unavailable('invalid_cursor')
            seen.add(cursor)
        if len(pages) > 1:
            if not all(isinstance(page, list) for page in pages):
                raise Unavailable('invalid_pagination')
            value['data'] = [row for page in pages for row in page]
        with self.lock:
            self.stats['complete'] += 1
        value['checked_revision'] = checked
        return value

    def changes(self, after, *, through=None):
        """Finish one observed change boundary before exposing its watermark."""
        rows, boundary = [], through
        while True:
            value = self._get('changes', after=str(after))
            current, page = value.get('revision'), value.get('changes')
            if (type(current) is not int or not isinstance(page, list)
                    or value.get('reset') or current < after):
                raise Unavailable('change_log_gap')
            if boundary is None:
                boundary = current
            for row in page:
                revision = row.get('revision') if isinstance(row, dict) else None
                if type(revision) is not int or revision <= after:
                    raise Unavailable('invalid_change_order')
                after = revision
                if revision <= boundary:
                    rows.append(row)
            if after >= boundary:
                return boundary, rows
            if not page:
                # Revision gaps may be generated by non-resource changes. A complete
                # empty tail proves the boundary; a truncated page never does.
                if current >= boundary:
                    return boundary, rows
                raise Unavailable('change_log_incomplete')

