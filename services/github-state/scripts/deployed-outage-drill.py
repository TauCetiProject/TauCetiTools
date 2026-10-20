#!/usr/bin/env python3
"""Exercise only an explicitly provisioned disposable deployed-outage fixture.

STATE_DIR contains a private state.json and admin-token/client-token/webhook-token.
No provisioning, production fault injection, GitHub access or resource deletion.
"""
import argparse
import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('state_dir', type=Path)
    parser.add_argument('case', choices=['seed', 'sqlite-full', 'postgres-unavailable', 'postgres-slow', 'r2-unavailable', 'frozen-source', 'inspect'])
    args = parser.parse_args()
    root = args.state_dir
    state = json.loads((root / 'state.json').read_text())
    run, url, repo = state['name'], state['service_url'], state['repository']['name']
    parsed = urllib.parse.urlsplit(url)
    if not (re.fullmatch(r'issue317-drill-[a-f0-9]{8}', run)
            and state.get('production_actions') is False
            and parsed.scheme == 'https' and parsed.hostname
            and parsed.hostname.startswith(run + '.') and parsed.hostname.endswith('.workers.dev')
            and parsed.path in ['', '/'] and not parsed.query and not parsed.fragment
            and not parsed.username and not parsed.password and parsed.port in [None, 443]
            and re.fullmatch(r'issue317/[a-z]+', repo)):
        raise RuntimeError('Disposable fixture identity required')
    result = {'case': args.case, 'run': run, 'started_at': time.time(), 'production_actions': False, 'verified': False}
    delivery_suffix = args.case + '-' + uuid.uuid4().hex[:8]

    def call(path, body=None, token='admin', headers=None):
        request_id = run + '-' + uuid.uuid4().hex
        data = None if body is None else json.dumps(body).encode()
        values = {'Content-Type': 'application/json', 'User-Agent': 'TauCetiWorker/1.0 (+https://github.com/TauCetiProject/TauCetiWorker)', 'X-Request-ID': request_id}
        if token:
            values['Authorization'] = 'Bearer ' + (root / (token + '-token')).read_text().strip()
        values.update(headers or {})
        request = urllib.request.Request(url.rstrip('/') + path, data=data, headers=values, method='GET' if data is None else 'POST')
        # Reject redirects so operator credentials never follow another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *unused):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        try:
            value = json.loads(raw)
        except ValueError:
            raise RuntimeError('Malformed fixture response; request ' + request_id) from None
        if status >= 300:
            result.setdefault('errors', []).append({'status': status, 'error': value.get('error'), 'reason': value.get('reason'), 'request_id': value.get('request_id', request_id)})
        return status, value

    def ok(path, body=None, token='admin'):
        status, value = call(path, body, token)
        if status != 200:
            raise RuntimeError('Fixture request failed: ' + str(status) + ' ' + path)
        return value

    def query(operation='issue_comments', arguments=None):
        params = urllib.parse.urlencode({'repository': repo, 'operation': operation, 'args': json.dumps(arguments if arguments is not None else {'number': 1})})
        return ok('/v1/query?' + params, token='client')

    def event(suffix, number):
        delivery = run + '-' + suffix
        payload = {'repository': {'id': int(state['repository']['id'])}, 'installation': {'id': int(state['repository']['installation'])}, 'action': 'created', 'issue': {'number': 1, 'pull_request': {'url': 'https://example.invalid/pulls/1'}}, 'comment': {'id': number, 'body': 'drill ' + suffix, 'user': {'login': 'fixture', 'type': 'User'}, 'created_at': '2026-10-20T00:00:00Z', 'updated_at': '2026-10-20T00:00:00Z'}}
        signature = hmac.new((root / 'webhook-token').read_text().strip().encode(), json.dumps(payload).encode(), hashlib.sha256).hexdigest()
        status, _ = call('/webhook/github', payload, token=None, headers={'X-Hub-Signature-256': 'sha256=' + signature, 'X-GitHub-Delivery': delivery, 'X-GitHub-Event': 'issue_comment'})
        return {'delivery': delivery, 'status': status, 'comment_id': str(number)}

    def recovered(number):
        for _ in range(36):
            value = query()
            if sum(str(row['id']) == str(number) for row in value.get('data', [])) == 1:
                return
            time.sleep(5)
        raise RuntimeError('Ordinary recovery timed out; preserve fixture and parked input')

    try:
        if args.case == 'seed':
            for after in range(0, 128, 8):
                ok('/fixture/seed', {'after': after})
            first, objects = ok('/fixture/state'), ok('/fixture/resources')
            ok('/fixture/seed', {'after': 128})
            second, again = ok('/fixture/state'), ok('/fixture/resources')
            assert first['revision'] == second['revision'] and objects == again
            inventory = query('open_prs', {})
            assert inventory['status'] == 'ok' and inventory.get('cursor')
            assert any(row['id'] == '9223372036854775807' for row in query()['data'])
            result['spill_reuse'] = {'revision': second['revision'], 'objects': len(again['objects'])}
        elif args.case == 'frozen-source':
            probe = ok('/fixture/frozen-source', {})
            assert probe == {'readable': True, 'result_status': 'ok', 'header': 'retained frozen header', 'blocked_write': True, 'activated_source_rejected': True, 'blocked_external_fetches': 0}
            result['probe'] = probe
        elif args.case == 'inspect':
            result['health'] = ok('/v1/health?' + urllib.parse.urlencode({'repository': repo}), token='client')
        else:
            sqlite = args.case == 'sqlite-full'
            identity = ok('/fixture/state')
            assert identity['backend'] == ('sqlite' if sqlite else 'postgres')
            warm = query()
            assert warm['status'] == 'ok'
            fault = {'postgres-unavailable': {'database': True}, 'postgres-slow': {'slow_ms': 5000}, 'r2-unavailable': {'r2': True}}.get(args.case)
            # Every repetition must apply fresh input rather than pass by seeing
            # an earlier run's already committed comment or delivery receipt.
            number = time.time_ns() // 1000
            try:
                if sqlite:
                    ok('/admin/storage/fault', {'repository': repo, 'mode': 'full'})
                else:
                    ok('/fixture/faults', fault)
                cached = query()
                assert cached['status'] == 'ok' and cached['data'] == warm['data']
                result['safe_cached_read'] = True
                start = time.monotonic()
                ok('/v1/health?' + urllib.parse.urlencode({'repository': repo}), token='client')
                result['health_ms'] = (time.monotonic() - start) * 1000
                assert result['health_ms'] < 3000
                if sqlite:
                    missing = query('files', {'number': 1})
                    assert missing['status'] == 'unavailable' and missing['complete'] is False and not missing.get('data')
                    result['missing_coverage'] = missing
                accepted = event(delivery_suffix, number)
                assert accepted['status'] == (503 if args.case == 'r2-unavailable' else 202)
                result['ingress'] = accepted
                time.sleep(12)
            finally:
                if sqlite:
                    ok('/admin/storage/fault', {'repository': repo, 'mode': 'none'})
                else:
                    ok('/fixture/faults', {})
            if args.case == 'r2-unavailable':
                result['ingress_after_restore'] = event(delivery_suffix, number)
                assert result['ingress_after_restore']['status'] == 202
            recovered(number)
            result['ordinary_recovery'] = True
        result['identity'] = ok('/fixture/state')
        assert result['identity']['blocked_external_fetches'] == 0
        result['verified'] = True
    finally:
        result['finished_at'] = time.time()
        destination = root / ('receipt-' + args.case + '.json')
        # Receipts may contain synthetic health details; keep operator files private.
        import os
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(result, output, indent=2)
            output.write('\n')
        print(json.dumps({'case': args.case, 'verified': result['verified'], 'receipt': str(destination)}))


if __name__ == '__main__':
    main()
