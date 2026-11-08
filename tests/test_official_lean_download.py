"""Official Lean API fallback verifies release identity and digest before extraction."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tauceti_tools/assets/scripts/install_official_lean.py"
SPEC = importlib.util.spec_from_file_location("official_lean", SOURCE)
LEAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LEAN)
TOOLCHAIN = "leanprover/lean4:v4.36.0-rc2"


class OfficialLean(unittest.TestCase):
    def fixture(self, mutate=lambda r: None, corrupt=False, success=False):
        content = b"official release fixture"
        release = {"tag_name": "v4.36.0-rc2", "draft": False, "assets": [{
            "name": "lean-v4.36.0-rc2-linux.tar.zst", "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "url": "https://api.github.com/repos/leanprover/lean4/releases/assets/123"}]}
        mutate(release)
        calls = []
        targets = []

        def run(args, **kwargs):
            calls.append(args)
            if args[:3] == ["elan", "toolchain", "install"]:
                return subprocess.CompletedProcess(args, 0 if success else 1)
            if args[0] == "curl":
                Path(args[-1]).write_bytes(json.dumps(release).encode() if args[-1].endswith("release.json")
                                          else (b"corrupt" if corrupt else content))
            if args[:3] == ["elan", "toolchain", "link"]:
                targets.append(Path(args[-1]))
            return subprocess.CompletedProcess(args, 0)

        return calls, targets, run

    def execute(self, run):
        with patch.object(LEAN.subprocess, "run", side_effect=run), patch.object(LEAN.time, "sleep"), \
                patch.object(LEAN.sys, "platform", "linux"), patch.object(LEAN.platform, "machine", return_value="x86_64"):
            LEAN.install(TOOLCHAIN, "elan")

    def test_normal_elan_success_needs_no_fallback(self):
        calls, _, run = self.fixture(success=True)
        self.execute(run)
        self.assertEqual(calls, [["elan", "toolchain", "install", TOOLCHAIN]])

    def test_official_asset_is_verified_extracted_and_linked_under_the_exact_name(self):
        calls, targets, run = self.fixture()
        try:
            self.execute(run)
            self.assertEqual(sum(c[:3] == ["elan", "toolchain", "install"] for c in calls), 3)
            self.assertEqual(calls[-2][:4], ["elan", "toolchain", "link", TOOLCHAIN])
            self.assertEqual(calls[-1], ["elan", "run", TOOLCHAIN, "lean", "--version"])
            self.assertTrue(targets[0].is_dir(), "the linked release must survive download scratch cleanup")
        finally:
            for target in targets:
                shutil.rmtree(target)

    def test_wrong_release_missing_digest_untrusted_url_and_corrupt_bytes_never_extract(self):
        cases = [
            (lambda r: r.update(tag_name="v0.0.0"), False),
            (lambda r: r["assets"][0].pop("digest"), False),
            (lambda r: r["assets"][0].update(url="https://example.com/asset"), False),
            (lambda r: None, True),
        ]
        for mutate, corrupt in cases:
            with self.subTest(corrupt=corrupt):
                calls, _, run = self.fixture(mutate, corrupt)
                with self.assertRaises(ValueError):
                    self.execute(run)
                self.assertFalse(any(c[0] == "tar" or c[:3] == ["elan", "toolchain", "link"] for c in calls))

    def test_unofficial_toolchains_are_refused(self):
        with patch.object(LEAN.subprocess, "run") as run, self.assertRaises(ValueError):
            LEAN.install("attacker/lean4:nightly", "elan")
        run.assert_not_called()

    def test_both_publishers_execute_the_installer_from_the_pinned_tools_checkout(self):
        for publisher in (ROOT / ".github/workflows/publish-lake-cache.yml",
                          ROOT / "tauceti_tools/assets/project/.github/workflows/publish-lake-cache.yml"):
            workflow = yaml.safe_load(publisher.read_text())
            steps = workflow["jobs"]["publish"]["steps"]
            install = next(s for s in steps if s.get("id") == "toolchain")
            self.assertIn("python3 publisher-tools/tauceti_tools/assets/scripts/install_official_lean.py", install["run"])
            checkout = next(s for s in steps if s.get("with", {}).get("path") == "publisher-tools")
            self.assertEqual(checkout["with"]["repository"], "${{ inputs.tools_repository }}")
            self.assertEqual(checkout["with"]["ref"], "${{ inputs.tools_ref }}")
            self.assertIn("tauceti_tools", checkout["with"]["sparse-checkout"])
