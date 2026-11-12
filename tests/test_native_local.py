"""Configured local verification uses the same checks on either source forge."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.forge import GITHUB, Forge, RepositoryRef
from tauceti_tools.local_scope import local_main

ROOT = Path(__file__).resolve().parents[1]
CONFIG = '''schema_version=2
[forges.source]
kind="forgejo"
url="https://git.example.org"
[repositories]
content={forge="source",name="one/Probe"}
roadmap="one/Roadmap"
[lean]
roots=["Probe"]
'''
HEADER = '''/-
Copyright (c) 2026 Contributors. All rights reserved.
Released under Apache 2.0 license as described in the file LICENSE.
Authors: Contributors
-/

module

/-! # Local verification fixture -/

public section

'''


class NativeLocal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conf = self.root / "tauceti.toml"
        self.conf.write_text(CONFIG)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("TAUCETI_")}
        self.env.update(TAUCETI_CONF=str(self.conf), PYTHONPATH=str(ROOT), XDG_STATE_HOME=str(self.root / "state"))
        self.git("init", "-qb", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "Probe").mkdir()
        (self.root / "Probe.lean").write_text(HEADER)
        (self.root / "Probe/Small.lean").write_text(HEADER + "/-- A fixture fact. -/\ntheorem fixture_fact : True := .intro\n")
        (self.root / ".gitignore").write_text("state/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", "https://git.example.org/one/Probe.git")
        self.git("update-ref", "refs/remotes/origin/main", self.base)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "tauceti_tools", *args], cwd=self.root,
                              env=self.env, capture_output=True, text=True, timeout=30)

    def test_native_remote_and_original_snapshot_define_the_local_base(self):
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(local_main(self.root), self.base)
            snapshot = self.root / "snapshot.toml"
            snapshot.write_text(CONFIG)
            self.conf.write_text(CONFIG.replace("git.example.org", "wrong.example.org"))
            with patch.dict(os.environ, TAUCETI_CONF_SOURCE=str(self.conf), TAUCETI_CONF_SNAPSHOT=str(snapshot)):
                self.assertEqual(local_main(self.root), self.base)
            with self.assertRaisesRegex(ValueError, "no local canonical"):
                local_main(self.root)

    def test_same_repository_name_on_github_does_not_supply_native_base(self):
        self.git("remote", "set-url", "origin", "https://github.com/one/Probe.git")
        with patch.dict(os.environ, self.env, clear=True), self.assertRaisesRegex(ValueError, "no local canonical"):
            local_main(self.root)

    def test_cli_materializes_and_runs_the_same_inherited_snapshot(self):
        snapshot = self.root / "snapshot.toml"
        snapshot.write_text(CONFIG)
        # Even an invalid live file must not be loaded before the inherited
        # immutable snapshot is selected.
        self.conf.write_text('schema_version="invalid live file"')
        self.env.update(TAUCETI_CONF_SOURCE=str(self.conf), TAUCETI_CONF_SNAPSHOT=str(snapshot))
        result = self.run_cli("script", "local_checks.py", "sources")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("local-sources: PASS", result.stdout)

    def test_local_scope_never_fetches_an_unbootstrapped_url(self):
        with patch.dict(os.environ, {**self.env, "TAUCETI_CONF": "https://config.example.org/project.toml"}, clear=True):
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")), self.assertRaisesRegex(ValueError, "saved project snapshot"):
                local_main(self.root)

    def test_real_native_sources_entrypoint_and_all_local_wrapper_bootstraps(self):
        result = self.run_cli("script", "local_checks.py", "sources")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("local-sources: PASS", result.stdout)
        for script in ("local_checks.py", "local_axioms.py", "local_lint.py", "check-expired-mathlib-shims.py"):
            with self.subTest(script=script):
                result = self.run_cli("script", script, "--help")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                result = subprocess.run([sys.executable, "-c", f"from tauceti_tools.templates import import_shared; import_shared({script!r}, 'fixture')"],
                                        cwd=self.root, env=self.env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unported_scripts_still_refuse_native_configuration(self):
        result = self.run_cli("script", "fleet_runner.py", "--help")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GitHub-only", result.stderr)


class RemoteIdentity(unittest.TestCase):
    def test_all_supported_transports_match_the_same_configured_identity(self):
        for forge in (GITHUB, Forge("forgejo", "https://git.example.org")):
            repo = RepositoryRef(forge, "one/Probe")
            host = forge.url.removeprefix("https://")
            for remote in (repo.url, repo.url + ".git", repo.url + ".git/", f"git@{host}:one/Probe.git", f"ssh://git@{host}/one/Probe.git"):
                with self.subTest(remote=remote):
                    self.assertTrue(repo.matches_remote(remote))
            self.assertFalse(repo.matches_remote("https://other.example.org/one/Probe.git"))
            self.assertFalse(repo.matches_remote(f"https://{host}/one/Other.git"))
            self.assertFalse(repo.matches_remote(f"http://{host}/one/Probe.git"))
