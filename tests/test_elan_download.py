"""Execute the verified installer block against transient HTTP failures and corrupt bytes."""
import hashlib
import importlib.util
import io
import os
import subprocess
import tarfile
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("elan_pin", ROOT / "tauceti_tools/assets/scripts/test_elan_pin.py")
PIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PIN)


class ElanDownload(unittest.TestCase):
    def test_pin_contract_for_shared_and_generated_workflows(self):
        for directory in (ROOT / ".github/workflows", ROOT / "tauceti_tools/assets/project/.github/workflows"):
            PIN.WORKFLOW_DIR = directory
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(PIN.ElanPin)
            result = unittest.TestResult()
            suite.run(result)
            self.assertTrue(result.wasSuccessful(), result.errors + result.failures)

    def install(self, primary_failures, fallback_bad=False, fallback_fail=False):
        archive = io.BytesIO()
        script = b'#!/bin/sh\nprintf installed > "$MARKER"\n'
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            info = tarfile.TarInfo("elan-init")
            info.mode, info.size = 0o755, len(script)
            tar.addfile(info, io.BytesIO(script))
        payload = archive.getvalue()
        hits = {"primary": 0, "fallback": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                endpoint = self.path.lstrip("/")
                hits[endpoint] += 1
                fail = ((endpoint == "primary" and hits[endpoint] <= primary_failures)
                        or (endpoint == "fallback" and fallback_fail))
                body = b"gateway timeout" if fail else (b"corrupt" if endpoint == "fallback" and fallback_bad else payload)
                self.send_response(504 if fail else 200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                workflow = ROOT / "tauceti_tools/assets/project/.github/workflows/publish-lake-cache.yml"
                block = textwrap.dedent(PIN.INSTALL.search(PIN.code(workflow)).group())
                base = f"http://127.0.0.1:{server.server_port}"
                block = block.replace('https://github.com/leanprover/elan/releases/download/${ELAN_VER}/'
                                      'elan-x86_64-unknown-linux-gnu.tar.gz', base + "/primary")
                block = block.replace('https://api.github.com/repos/leanprover/elan/releases/assets/${ELAN_ASSET_ID}',
                                      base + "/fallback")
                with tempfile.TemporaryDirectory() as temporary:
                    marker = Path(temporary) / "installed"
                    env = dict(os.environ, ELAN_VER="v-test", ELAN_ASSET_ID="123", MARKER=str(marker),
                               ELAN_SHA256=hashlib.sha256(payload).hexdigest())
                    result = subprocess.run(["bash", "-euc", block], cwd=temporary, env=env,
                                            capture_output=True, text=True, timeout=20)
                    return result, marker.exists(), hits
            finally:
                server.shutdown()
                thread.join()

    def test_transient_gateway_failure_retries_without_executing_error_page(self):
        result, installed, hits = self.install(1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(installed)
        self.assertEqual(hits, {"primary": 2, "fallback": 0})

    def test_official_asset_api_recovers_persistent_release_url_failure(self):
        result, installed, hits = self.install(99)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(installed)
        self.assertEqual(hits, {"primary": 3, "fallback": 1})

    def test_fallback_still_requires_the_pinned_checksum(self):
        result, installed, _ = self.install(99, fallback_bad=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(installed)
        self.assertIn("checksum did NOT match", result.stderr)

    def test_exhausted_downloads_never_run_an_installer(self):
        result, installed, hits = self.install(99, fallback_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(installed)
        self.assertEqual(hits, {"primary": 3, "fallback": 3})


if __name__ == "__main__":
    unittest.main()
