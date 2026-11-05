"""The staging fetch of the repair worker's outputs: its configuration and its fallbacks.

The properties that matter:

* `[services.repair_cache]` renders into the Lake cache services file every cache script reads,
  with its name and public URL, and an installation without it renders no repair service;
* the fetch asks Lake for exactly the staging head (`--rev`), from the repair service, for the
  content repository;
* a head with no published outputs, or a fetch that never completes, leaves the cache's output
  records exactly as the base cache left them and does not fail the job;
* a completed fetch reports how many module outputs it contributed.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tauceti_tools.project import ConfigError, parse
from tauceti_tools.scaffold import initial_configuration
from tauceti_tools.templates import materialize

HEAD = "0123456789abcdef0123456789abcdef01234567"

# A stand-in `lake`: records its argv, then behaves as FAKE_LAKE_MODE says. `used` adds one new
# output record and replaces one; `partial` writes a record and then fails as a transfer would.
FAKE_LAKE = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LAKE_ARGV"
printf '%s\n' "$LAKE_CONFIG" >> "$FAKE_LAKE_ARGV.config"
out="$LAKE_CACHE_DIR/outputs/Project"
case "$FAKE_LAKE_MODE" in
  absent) echo 'error: no outputs found for revision'; exit 1 ;;
  used)
    mkdir -p "$out"
    echo '{"repair": 1}' > "$out/1111111111111111.json"
    echo '{"repair": 2}' > "$out/0000000000000000.json"
    echo 'info: Project: downloaded artifact 2222222222222222'
    exit 0 ;;
  partial)
    mkdir -p "$out"
    echo '{"partial": true}' > "$out/3333333333333333.json"
    echo '{"partial": true}' > "$out/0000000000000000.json"
    echo 'error: failed to download artifact'
    exit 1 ;;
esac
"""


def project(repair: dict | None):
    config = parse(initial_configuration("one/Project", "human").encode())
    if repair is not None:
        config.raw["services"]["repair_cache"] = repair
    return config


class RepairCacheConfig(unittest.TestCase):
    def services(self, repair):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            materialize(project(repair), root / "scripts")
            cfg = root / "lake-cache.toml"
            subprocess.run(
                ["bash", "-c", '. "$1"; write_lake_cache_config "$2" https://c.invalid/a https://c.invalid/r',
                 "-", str(root / "scripts/lake-cache-services.sh"), str(cfg)],
                check=True)
            return cfg.read_text(), (root / "scripts/repair-cache-get.sh").read_text()

    def test_configured_service_renders_name_and_url(self):
        cfg, script = self.services({"mode": "custom", "name": "project-repair", "url": "https://repair.example.org/"})
        self.assertIn('cache.defaultService = "tauceti-public"', cfg)
        self.assertIn('name = "project-repair"', cfg)
        self.assertIn('artifactEndpoint = "https://repair.example.org/artifacts"', cfg)
        self.assertIn('revisionEndpoint = "https://repair.example.org/revisions"', cfg)
        self.assertIn("--repo one/Project", script)

    def test_default_name_and_absent_service(self):
        cfg, _ = self.services({"mode": "hosted", "url": "https://repair.example.org"})
        self.assertIn('name = "tauceti-repair"', cfg)
        for repair in (None, {"mode": "off", "url": "https://repair.example.org"}):
            with self.subTest(repair=repair):
                cfg, _ = self.services(repair)
                self.assertEqual(cfg.count("[[cache.service]]"), 1)
                self.assertNotIn("repair", cfg)

    def test_invalid_settings_are_rejected(self):
        base = initial_configuration("one/Project", "human")
        for table in ('mode = "custom"\nurl = "http://repair.example.org"\n',
                      'mode = "custom"\nname = "bad name"\nurl = "https://repair.example.org"\n'):
            with self.subTest(table=table), self.assertRaises(ConfigError):
                parse((base + "\n[services.repair_cache]\n" + table).encode())


class RepairCacheFetch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        materialize(project({"mode": "custom", "url": "https://repair.example.org"}), self.root / "scripts")
        self.outputs = self.root / "pr/.lake/cache/outputs/Project"
        self.outputs.mkdir(parents=True)
        (self.outputs / "0000000000000000.json").write_text('{"base": true}\n')
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "lake").write_text(FAKE_LAKE)
        (bin_dir / "lake").chmod(0o755)
        (self.root / "tmp").mkdir()
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                        RUNNER_TEMP=str(self.root / "tmp"), FAKE_LAKE_ARGV=str(self.root / "argv"),
                        GITHUB_ENV=str(self.root / "env"), GITHUB_STEP_SUMMARY=str(self.root / "summary"))
        self.env.pop("LAKE_ARTIFACT_CACHE", None)

    def tearDown(self):
        self._tmp.cleanup()

    def fetch(self, mode, *, script="scripts/repair-cache-get.sh", **env):
        result = subprocess.run(["bash", str(self.root / script), "pr", HEAD], cwd=self.root,
                                env=dict(self.env, FAKE_LAKE_MODE=mode, **env),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        github_env = dict(line.split("=", 1) for line in (self.root / "env").read_text().splitlines())
        return result.stdout, github_env

    def records(self):
        return {p.name: p.read_text() for p in self.outputs.iterdir()}

    def test_absent_outputs_leave_the_base_cache(self):
        before = self.records()
        out, env = self.fetch("absent")
        self.assertIn(f"no repair outputs for {HEAD}, building from the base cache", out)
        self.assertEqual(env, {"REPAIR_OUTPUTS_STATE": "absent", "REPAIR_OUTPUTS_REUSED": "0"})
        self.assertEqual(self.records(), before)
        argv = (self.root / "argv").read_text().splitlines()
        self.assertEqual(argv, [f"cache get --service tauceti-repair --repo one/Project --rev={HEAD}"])
        config = Path((self.root / "argv.config").read_text().strip()).read_text()
        self.assertIn('artifactEndpoint = "https://repair.example.org/artifacts"', config)
        self.assertIn("no repair outputs", (self.root / "summary").read_text())

    def test_incomplete_fetch_restores_the_base_records(self):
        before = self.records()
        out, env = self.fetch("partial")
        self.assertEqual(env["REPAIR_OUTPUTS_STATE"], "failed")
        self.assertEqual(self.records(), before)
        self.assertEqual(len((self.root / "argv").read_text().splitlines()), 3)

    def test_completed_fetch_counts_contributed_outputs(self):
        out, env = self.fetch("used")
        self.assertEqual(env, {"REPAIR_OUTPUTS_STATE": "used", "REPAIR_OUTPUTS_REUSED": "2"})
        self.assertIn("reused 2 module outputs", out)
        self.assertIn("1 artifacts downloaded", out)

    def test_discarded_base_cache_or_no_service_skips_lake(self):
        out, env = self.fetch("used", LAKE_ARTIFACT_CACHE="false")
        self.assertEqual(env["REPAIR_OUTPUTS_STATE"], "off")
        self.assertFalse((self.root / "argv").exists())
        materialize(project(None), self.root / "off")
        out, env = self.fetch("used", script="off/repair-cache-get.sh")
        self.assertIn("no repair cache configured", out)
        self.assertFalse((self.root / "argv").exists())

    def test_rejects_a_non_sha_head(self):
        result = subprocess.run(["bash", str(self.root / "scripts/repair-cache-get.sh"), "pr", "main"],
                                cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
