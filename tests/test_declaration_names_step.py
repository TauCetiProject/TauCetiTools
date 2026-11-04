"""The advisory declaration-names step never changes a build's verdict and prints one record."""
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts/declaration-names.sh"

FAKE_LAKE = """#!/usr/bin/env bash
# fake lake: `lake env <lean> --run <script> HEAD BASE TESTED MODULES LIB BASE-DIR`
[ "$1" = env ] || { echo "unexpected: $*" >&2; exit 9; }
shift; lean=$1; shift; [ "$1" = --run ] || exit 9; shift; script=$1; shift
printf '%s\\n' "args: $*" > "$FAKE_LOG"
echo "declaration-names: cannot read something" >&2
echo "DECLARATION-NAMES {\\"version\\":1,\\"head\\":\\"$1\\",\\"complete\\":true,\\"added\\":{}}"
echo "DECLARATION-NAMES {\\"version\\":1,\\"head\\":\\"second\\",\\"complete\\":true,\\"added\\":{}}"
exit "${FAKE_STATUS:-0}"
"""


class DeclarationNamesStepTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".lake/build/lib/lean/TauCeti").mkdir(parents=True)
        (self.root / ".lake/build/lib/lean/TauCeti/A.olean").write_bytes(b"base A")
        (self.root / "modules").write_text("TauCeti.A\n\nTauCeti.New\n")
        trusted = self.root / "trusted"
        trusted.mkdir()
        (trusted / "DeclarationNames.lean").write_text("-- script\n")
        fake = self.root / "bin"
        fake.mkdir()
        (fake / "lake").write_text(FAKE_LAKE)
        (fake / "lake").chmod((fake / "lake").stat().st_mode | stat.S_IEXEC)
        self.log = self.root / "fake.log"
        self.env = {
            **os.environ,
            "PATH": f"{fake}:{os.environ['PATH']}",
            "LINT_ONLY_MODULES": str(self.root / "modules"),
            "TAUCETI_TRUSTED_SCRIPTS": str(trusted),
            "CI_RESOURCE_TESTED_HEAD": "h" * 40,
            "CI_RESOURCE_TESTED_BASE": "b" * 40,
            "CI_RESOURCE_TESTED_COMMIT": "t" * 40,
            "FAKE_LOG": str(self.log),
        }
        self.env.pop("IS_BATCH", None)
        self.env.pop("WATCHDOG_TOOLCHAIN", None)

    def tearDown(self):
        self.temp.cleanup()

    def run_helper(self, mode, **overrides):
        env = {**self.env, **{k: v for k, v in overrides.items() if v is not None}}
        for k, v in overrides.items():
            if v is None:
                env.pop(k, None)
        return subprocess.run(["bash", str(HELPER), mode], cwd=self.root, env=env,
                              text=True, capture_output=True)

    def test_save_copies_existing_headers_only(self):
        result = self.run_helper("save")
        self.assertEqual(result.returncode, 0, result.stderr)
        base = self.root / ".lake/telemetry/base-oleans/TauCeti"
        self.assertEqual((base / "A.olean").read_bytes(), b"base A")
        self.assertFalse((base / "New.olean").exists())

    def test_record_prints_exactly_one_line_and_routes_diagnostics(self):
        self.run_helper("save")
        result = self.run_helper("record")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1, result.stdout)
        self.assertTrue(lines[0].startswith('DECLARATION-NAMES {"version":1,"head":"' + "h" * 40))
        self.assertIn("cannot read something", result.stderr)
        args = self.log.read_text()
        self.assertIn("h" * 40, args)
        self.assertIn(".lake/build/lib/lean", args)
        self.assertIn("base-oleans", args)

    def test_record_failure_is_silent_and_exits_zero(self):
        result = self.run_helper("record", FAKE_STATUS="1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.count("DECLARATION-NAMES"), 1)
        result = self.run_helper("record", TAUCETI_TRUSTED_SCRIPTS=str(self.root / "absent"))
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        result = self.run_helper("record", CI_RESOURCE_TESTED_COMMIT="")
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_staging_and_missing_module_list_do_nothing(self):
        for mode in ("save", "record"):
            result = self.run_helper(mode, IS_BATCH="true")
            self.assertEqual((result.returncode, result.stdout), (0, ""))
            result = self.run_helper(mode, LINT_ONLY_MODULES=None)
            self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertFalse((self.root / ".lake/telemetry/base-oleans").exists())


if __name__ == "__main__":
    unittest.main()
