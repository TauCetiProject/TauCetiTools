import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tauceti_tools.assets.scripts import ci_report_assets as reports


class PublishedReportsTests(unittest.TestCase):
    def manifest(self):
        assets, responses = {}, {}
        for names in reports.GROUPS.values():
            for name in names:
                data = f'<svg><title>{name}</title></svg>'.encode()
                revision = hashlib.sha256(data).hexdigest()
                path = f'/v1/ci/assets/{revision}/{name}'
                assets[name] = {'url': path, 'revision': revision}
                responses[path] = data
        value = {'schema': 'tauceti-ci.manifest/v1', 'mode': 'live',
                 'history': {'status': 'ready'}, 'assets': assets,
                 'groups': {group: {'status': 'updated'} for group in reports.GROUPS}}
        responses['/v1/ci/manifest.json'] = json.dumps(value).encode()
        return value, responses

    def test_fetches_only_published_assets_without_database_or_github(self):
        _, responses = self.manifest()
        with tempfile.TemporaryDirectory() as directory, patch.object(
                reports, 'fetch', side_effect=lambda path, limit: responses[path]) as fetch:
            out = Path(directory)
            self.assertEqual(reports.cache(out), [])
            self.assertEqual(len(list(out.glob('*.svg'))), 11)
            self.assertEqual(len(fetch.call_args_list), 12)
            self.assertTrue(all(call.args[0].startswith('/v1/ci/') for call in fetch.call_args_list))
            self.assertEqual(json.loads((out / 'ci-freshness.json').read_text())['source'],
                             'published-live-reports')

    def test_bad_digest_preserves_the_entire_previous_group(self):
        manifest, responses = self.manifest()
        responses[manifest['assets']['ci-fleet-30d.svg']['url']] = b'<svg>corrupt</svg>'
        with tempfile.TemporaryDirectory() as directory, patch.object(
                reports, 'fetch', side_effect=lambda path, limit: responses[path]):
            out = Path(directory)
            for name in reports.GROUPS['fleet']:
                (out / name).write_text('previous chart')
            self.assertEqual(reports.cache(out), ['fleet'])
            for name in reports.GROUPS['fleet']:
                self.assertEqual((out / name).read_text(), 'previous chart')
            self.assertTrue((out / 'ci-build-duration.svg').read_text().startswith('<svg'))

    def test_unready_manifest_preserves_cached_reports(self):
        manifest, responses = self.manifest()
        manifest['history']['status'] = 'importing'
        responses['/v1/ci/manifest.json'] = json.dumps(manifest).encode()
        with tempfile.TemporaryDirectory() as directory, patch.object(
                reports, 'fetch', side_effect=lambda path, limit: responses[path]):
            out = Path(directory)
            self.assertEqual(reports.cache(out), list(reports.GROUPS))
            self.assertEqual(list(out.glob('*.svg')), [])
            self.assertEqual(json.loads((out / 'ci-freshness.json').read_text())['groups']['fleet']['status'], 'error')
