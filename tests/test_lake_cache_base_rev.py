"""lake-cache-get.sh with an explicit base revision (LAKE_CACHE_BASE_REV).

The properties that matter:

* with a base revision set, the base cache is first asked for exactly that revision (`--rev`),
  and a hit stops there without walking ancestors;
* a miss for the base revision, in Lake's `--rev` wording, falls back to the bounded walk;
* without it, or with a value that is not a full SHA, only the walk runs;
* the log says which path supplied the cache.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tauceti_tools.project import parse
from tauceti_tools.scaffold import initial_configuration
from tauceti_tools.templates import materialize

BASE = "0123456789abcdef0123456789abcdef01234567"

# A stand-in `lake`: records its argv; `--rev=<sha>` hits only for $FAKE_PUBLISHED, and the walk
# hits only when FAKE_WALK=hit.
FAKE_LAKE = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LAKE_ARGV"
case "${*: -1}" in
  --rev=*)
    rev="${*: -1}"; rev="${rev#--rev=}"
    [ "$rev" = "${FAKE_PUBLISHED:-}" ] && exit 0
    echo "error: one/Project: outputs not found for revision $rev"; exit 1 ;;
  --max-revs=*)
    [ "${FAKE_WALK:-}" = hit ] && exit 0
    echo "error: one/Project: no outputs found in 1000 revisions from HEAD"; exit 1 ;;
esac
exit 2
"""


class BaseRevisionLookup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        config = parse(initial_configuration("one/Project", "human").encode())
        materialize(config, self.root / "scripts")
        (self.root / "pr/.lake/cache").mkdir(parents=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "lake").write_text(FAKE_LAKE)
        (bin_dir / "lake").chmod(0o755)
        (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "sleep").chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", RUNNER_TEMP=str(self.root),
                        FAKE_LAKE_ARGV=str(self.root / "argv"), GITHUB_ENV=str(self.root / "env"),
                        LAKE_CACHE_MAX_REVS="1000")
        self.env.pop("LAKE_CACHE_BASE_REV", None)

    def tearDown(self):
        self._tmp.cleanup()

    def run_get(self, **env):
        result = subprocess.run(["bash", str(self.root / "scripts/lake-cache-get.sh"), "pr"], cwd=self.root,
                                env=dict(self.env, **env), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        github_env = (self.root / "env").read_text() if (self.root / "env").exists() else ""
        return result.stdout, (self.root / "argv").read_text().splitlines(), github_env

    def test_base_revision_hit_skips_the_walk(self):
        out, argv, github_env = self.run_get(LAKE_CACHE_BASE_REV=BASE, FAKE_PUBLISHED=BASE)
        self.assertEqual(argv, [f"cache get --service tauceti-public --repo one/Project --rev={BASE}"])
        self.assertIn(f"fetched the outputs of base revision {BASE}", out)
        self.assertIn("incremental build enabled", out)
        self.assertNotIn("LAKE_ARTIFACT_CACHE=false", github_env)

    def test_base_revision_miss_falls_back_to_the_walk(self):
        out, argv, github_env = self.run_get(LAKE_CACHE_BASE_REV=BASE, FAKE_WALK="hit")
        self.assertEqual(argv, [f"cache get --service tauceti-public --repo one/Project --rev={BASE}",
                                "cache get --service tauceti-public --repo one/Project --max-revs=1000"])
        self.assertIn(f"no outputs for base revision {BASE}; walking up to 1000 revisions", out)
        self.assertIn("fetched outputs found by walking", out)
        self.assertNotIn("retrying", out)
        self.assertNotIn("LAKE_ARTIFACT_CACHE=false", github_env)
        self.assertTrue((self.root / "cache-get-1.log").exists())
        self.assertTrue((self.root / "cache-get-2.log").exists())

    def test_both_miss_discards_the_cache(self):
        out, argv, github_env = self.run_get(LAKE_CACHE_BASE_REV=BASE)
        self.assertEqual(len(argv), 2)
        self.assertIn("LAKE_ARTIFACT_CACHE=false", github_env)
        self.assertFalse((self.root / "pr/.lake/cache").exists())

    def test_unset_or_invalid_base_revision_only_walks(self):
        for value in (None, "main", BASE[:12]):
            with self.subTest(value=value):
                (self.root / "argv").unlink(missing_ok=True)
                env = {"FAKE_WALK": "hit"}
                if value is not None:
                    env["LAKE_CACHE_BASE_REV"] = value
                out, argv, _ = self.run_get(**env)
                self.assertEqual(argv, ["cache get --service tauceti-public --repo one/Project --max-revs=1000"])
                if value is not None:
                    self.assertIn("::warning::ignoring LAKE_CACHE_BASE_REV", out)


if __name__ == "__main__":
    unittest.main()
