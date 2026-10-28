#!/usr/bin/env python3
"""Cache published CI charts for the website without downloading the CI database."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ORIGIN = 'https://ci-data.taucetiproject.org'
GROUPS = {
    'fleet': ['ci-fleet-72h.svg', 'ci-fleet-30d.svg'],
    'daily': ['ci-build-duration.svg', 'ci-build-wait.svg', 'ci-runner-minutes.svg',
              'ci-merge-queue.svg', 'ci-picker.svg'],
    'details': ['ci-build-phases.svg', 'ci-failures.svg'],
    'queue': ['ci-queue-size.svg'],
    'staging': ['ci-bors-staging.svg'],
}


def fetch(path, limit):
    request = urllib.request.Request(ORIGIN + path, headers={'User-Agent': 'TauCetiCI/1.0'})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Published report exceeds size limit')
    return data


def cache(out):
    out.mkdir(parents=True, exist_ok=True)
    try:
        manifest = json.loads(fetch('/v1/ci/manifest.json', 1024 * 1024))
        if (manifest.get('schema') != 'tauceti-ci.manifest/v1' or
                manifest.get('mode') != 'live' or manifest.get('history', {}).get('status') != 'ready'):
            raise ValueError('Published CI charts are not ready')
    except Exception as error:
        (out / 'ci-freshness.json').write_text(json.dumps({
            'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'source': 'published-live-reports',
            'groups': {group: {'status': 'error', 'reason': str(error), 'retained_previous': True}
                       for group in GROUPS},
        }, indent=2) + '\n')
        return list(GROUPS)
    freshness, errors = {}, []

    def asset(name):
        value = manifest.get('assets', {}).get(name, {})
        path, revision = value.get('url', ''), value.get('revision', '')
        if not re.fullmatch('/v1/ci/assets/[a-f0-9]{64}/' + re.escape(name), path):
            raise ValueError('Invalid published chart URL')
        data = fetch(path, 4 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != revision or path.split('/')[-2] != revision:
            raise ValueError('Published chart digest mismatch')
        if not data.startswith(b'<svg') or not data.rstrip().endswith(b'</svg>'):
            raise ValueError('Invalid published SVG')
        return name, data

    with ThreadPoolExecutor(max_workers=4) as pool:
        for group, names in GROUPS.items():
            metadata = dict(manifest.get('groups', {}).get(group, {}))
            try:
                if metadata.get('status') in ('stale', 'error'):
                    raise ValueError('Published chart group is stale or unavailable')
                # Validate the entire group before replacing any cached member.
                values = list(pool.map(asset, names))
                for name, data in values:
                    target = out / name
                    temporary = target.with_suffix('.svg.tmp')
                    temporary.write_bytes(data)
                    temporary.replace(target)
            except Exception as error:
                metadata.update(status='error', reason=str(error), retained_previous=True)
                errors.append(group)
            freshness[group] = metadata
    (out / 'ci-freshness.json').write_text(json.dumps({
        'generated_at': manifest.get('generated_at'), 'source': 'published-live-reports',
        'groups': freshness,
    }, indent=2) + '\n')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', required=True, type=Path)
    args = parser.parse_args()
    errors = cache(args.out_dir)
    if errors:
        raise SystemExit('Retained previous CI charts: ' + ', '.join(errors))


if __name__ == '__main__':
    main()
