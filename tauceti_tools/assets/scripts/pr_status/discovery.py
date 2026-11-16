"""Coalesce existing shared-state changes into the batch's retained PR queue."""

import json
import time

from tauceti_tools.github_state import Unavailable


def seed(state):
    value = state._get('changes', after='0')
    boundary = value.get('revision')
    if type(boundary) is not int or boundary < 0:
        raise Unavailable('invalid_change_boundary')
    inventory = state.query('open_prs')['data']
    if not isinstance(inventory, list):
        raise Unavailable('invalid_open_inventory')
    heads = {}
    for row in inventory:
        if type(row.get('number')) is not int or not isinstance(row.get('headRefOid'), str):
            raise Unavailable('invalid_open_pr')
        heads[str(row['number'])] = row['headRefOid']
    return boundary, heads


def discover(service, checkpoint, policy, *, full=False, repair_limit=50):
    """Publish a boundary only alongside all work discovered through that boundary."""
    previous = checkpoint.get('shared_discovery')
    all_dirty = not previous or previous.get('policy') != policy or full
    if all_dirty:
        boundary, heads = seed(service)
        previous = dict(revision=boundary, heads=heads, policy=policy, repair_after=0)
        changed = []
    else:
        heads = dict(previous['heads'])
        try:
            boundary, changed = service.changes(previous['revision'])
        except Unavailable as error:
            if error.reason != 'change_log_gap':
                raise
            boundary, heads = seed(service)
            changed, all_dirty = [], True
    by_head = {}
    for number, head in heads.items():
        by_head.setdefault(head, []).append(int(number))
    dirty = set(map(int, heads)) if all_dirty else set()
    for row in changed:
        kind, identity, parent = row.get('kind'), row.get('id', ''), row.get('parent')
        if kind == 'pr' and identity.isdigit():
            dirty.add(int(identity))
        elif kind in {'issue_comments', 'review_comments', 'review_threads', 'reviews', 'files'}:
            if not isinstance(parent, str) or not parent.isdigit():
                # A retired row without its parent cannot be silently omitted.
                boundary, heads = seed(service)
                dirty.update(map(int, heads))
                break
            dirty.add(int(parent))
        elif kind == 'status_context':
            dirty.update(by_head.get(identity.split(':', 1)[0], []))
        elif kind in {'collection', 'statuses'}:
            operation, _, encoded = identity.partition(':')
            try:
                args = json.loads(encoded)
            except ValueError:
                raise Unavailable('invalid_change_collection') from None
            if operation in {'pr', 'issue_comments', 'review_comments', 'review_threads', 'reviews', 'files'}:
                if str(args.get('number', '')).isdigit():
                    dirty.add(int(args['number']))
            elif operation == 'statuses':
                dirty.update(by_head.get(args.get('sha'), []))
        elif kind == 'refs' and identity == 'refs/heads/main':
            dirty.update(map(int, heads))
    decisions = checkpoint.get('decisions', {})
    dirty.update(int(number) for number, record in decisions.items()
                 if record.get('valid_until', 0) <= time.time() and number in heads)
    # Rotating repairs bound recovery from missed events without scanning run history.
    numbers = sorted(map(int, heads))
    after = previous.get('repair_after', 0)
    rotating = [n for n in numbers if n > after][:repair_limit]
    if len(rotating) < repair_limit:
        rotating += [n for n in numbers if n <= after][:repair_limit-len(rotating)]
    dirty.update(rotating)
    checkpoint['shared_discovery'] = dict(revision=boundary, heads=heads, policy=policy,
                                        repair_after=rotating[-1] if rotating else 0)
    return sorted(dirty)
