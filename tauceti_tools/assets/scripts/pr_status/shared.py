"""Readiness evidence from the existing shared service, never direct fallback."""

import json
import hashlib
from pathlib import Path
import re
import threading
import time

from tauceti_tools.github_state import State, Unavailable


def policy_identity(runner):
    """Bind cached decisions to the deployed reducer and pinned review sources."""
    digest = hashlib.sha256()
    roots = [Path(runner), Path(__file__).parent]
    import tauceti_tools.self_tested as declarations
    sources = [Path(declarations.__file__)]
    for root in roots:
        sources += [source for source in sorted(root.glob('*.py')) if not source.name.startswith('test_')]
    for source in sources:
        digest.update(source.name.encode() + b'\0' + source.read_bytes() + b'\0')
    return digest.hexdigest()


def decision_key(pr, comments, statuses, merge_base, policy, lifecycle, *, repo):
    header = dict(pr, labels=sorted(label['name'].lower() for label in pr['labels']
                                   if label['name'] not in lifecycle))
    # Base advancement that leaves the merge base unchanged leaves the reviewed
    # diff unchanged. Retargeting still changes the base name in this signature.
    header['base'] = dict(ref=pr['base']['ref'], merge_base=merge_base)
    payload = ['github.com', repo, policy, header, comments, statuses]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def valid_until(pr, comments, repo):
    deadline = time.time() + 3600
    from tauceti_tools import self_tested
    if self_tested.requested(pr.get('labels'), pr['head'].get('ref', '')):
        if pr['head'].get('ref', '').startswith(self_tested.BUMP_BRANCH):
            return time.time()  # The inline pin guard is rechecked against main's tip.
    for comment in comments:
        for match in re.finditer(r'"expires_at"\s*:\s*(\d+(?:\.\d+)?)', comment.get('body') or ''):
            expires = float(match[1])
            if expires > time.time():
                deadline = min(deadline, expires)
    return deadline


def normalized_pr(row):
    try:
        owner = (row.get('headRepositoryOwner') or {}).get('login', '')
        name = (row.get('headRepository') or {}).get('name', '')
        author = row.get('author') or {}
        login = author.get('login', '')
        if author.get('is_bot') and login.startswith('app/'):
            login = login[4:] + '[bot]'
        return dict(number=row['number'], state=row['state'].lower(), draft=row['isDraft'],
                    head=dict(sha=row['headRefOid'], ref=row['headRefName'],
                              repo=dict(full_name=owner+'/'+name if owner and name else '')),
                    base=dict(ref=row['baseRefName'], sha=row['baseRefOid']),
                    user=dict(login=login), labels=row['labels'],
                    mergeable={'MERGEABLE': True, 'CONFLICTING': False}.get(row.get('mergeable')))
    except (KeyError, TypeError, AttributeError):
        raise Unavailable('incomplete_pr_header') from None


def affects(row, number, head):
    """Conservative invalidation from the service's retained resource journal."""
    kind, identity = row.get('kind'), row.get('id', '')
    if kind == 'pr':
        return identity == str(number)
    if kind in {'issue_comments', 'review_comments', 'reviews', 'files'}:
        if not isinstance(row.get('parent'), str) or not row['parent'].isdigit():
            raise Unavailable('change_parent_contract')
        return row['parent'] == str(number)
    if kind == 'status_context':
        return identity.startswith(head + ':')
    if kind == 'collection' or kind == 'statuses':
        operation, _, args = identity.partition(':')
        try:
            args = json.loads(args)
        except ValueError:
            return True
        return ((operation in {'pr', 'issue_comments', 'review_comments', 'reviews', 'files'}
                 and str(args.get('number')) == str(number))
                or operation == 'statuses' and args.get('sha') == head)
    return kind == 'refs' and identity == 'refs/heads/main'


def resource(row):
    kind = row.get('kind')
    if kind == 'collection':
        return row.get('id', '').partition(':')[0]
    return 'statuses' if kind == 'status_context' else kind


class Evidence:
    def __init__(self, repository, *, state=None):
        self.state = state or State(repository)
        self.main = None
        self.main_lock = threading.Lock()
        self.headers = {}
        self.fences = {}

    def main_ref(self):
        with self.main_lock:
            return self._main_ref()

    def _main_ref(self):
        if self.main is None:
            result = self.state.query('refs', name='main')
            rows = [row for row in result['data'] if row.get('ref') == 'refs/heads/main']
            if len(rows) != 1 or not re.fullmatch('[0-9a-f]{40}', (rows[0].get('object') or {}).get('sha', '')):
                raise Unavailable('main_ref_coverage')
            self.main = result
        return self.main

    def read(self, number, *, comments=True, preliminary=None):
        header = self.state.query('pr', number=number)
        pr = normalized_pr(header['data'])
        if pr['number'] != number:
            raise Unavailable('wrong_pr_identity')
        if preliminary is not None and preliminary(pr) is not None:
            self.headers[number] = pr
            return pr, [], {}
        revisions = dict(pr=header.get('checked_revision', header['revision']))
        if pr['base']['ref'] == 'main':
            main = self.main_ref()
            pr['base']['sha'] = next(row['object']['sha'] for row in main['data'] if row['ref'] == 'refs/heads/main')
            revisions['refs'] = main.get('checked_revision', main['revision'])
        history, status = {'data': []}, {'data': []}
        if comments:
            history = self.state.query('issue_comments', number=number)
            status = self.state.query('statuses', sha=pr['head']['sha'])
            if not isinstance(history['data'], list) or not isinstance(status['data'], list):
                raise Unavailable('invalid_readiness_histories')
            revisions.update(issue_comments=history.get('checked_revision', history['revision']),
                             statuses=status.get('checked_revision', status['revision']))
        prior = self.fences.get(number) if not comments else None
        checked = prior or revisions
        before, after = min(checked.values()), max(revisions.values())
        if before != after:
            _, changed = self.state.changes(before, through=after)
            if any(row.get('kind') == 'refs' and row.get('id') == 'refs/heads/main'
                   and row.get('revision', after) > checked.get('refs', before) for row in changed):
                with self.main_lock:
                    self.main = None
            if any(row.get('revision', after) > checked.get(resource(row), before)
                   and affects(row, number, pr['head']['sha']) for row in changed):
                raise Unavailable('concurrent_readiness_input')
        if comments:
            self.fences[number] = revisions
        self.headers[number] = pr
        return pr, history['data'], {row['context']: row['state'].lower() for row in status['data']}
