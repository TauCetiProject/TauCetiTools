"""Run the style wrapper and inspect exactly what reaches the two real lint interfaces."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts"


class StyleScopeTests(unittest.TestCase):
    def run_style(self, fast, selected):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "TauCeti").mkdir()
            (root / ".lake").mkdir()
            (root / "bin").mkdir()
            (root / "TauCeti.lean").write_text("")
            for name in ("Changed", "Unchanged", "Renamed"):
                (root / f"TauCeti/{name}.lean").write_text("")
            (root / "selected").write_text(selected)
            lake = root / "bin/lake"
            lake.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
record = {"args": sys.argv[1:]}
if sys.argv[1:3] == ["exe", "lint-style"]:
    source = pathlib.Path(os.environ["LEAN_SRC_PATH"].split(":")[0])
    record["imports"] = (source / "TauCetiLint/All.lean").read_text().splitlines()
with open("calls", "a") as f:
    f.write(json.dumps(record) + "\\n")
''')
            lake.chmod(0o755)
            result = subprocess.run(["bash", str(SCRIPTS / "lint-style.sh")],
                text=True, capture_output=True, cwd=root, env={**os.environ,
                "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
                "TAUCETI_PROJECT_ROOT": str(root), "TAUCETI_TRUSTED_SCRIPTS": str(SCRIPTS),
                "LINT_CHANGED_ONLY": "1" if fast else "0",
                "LINT_ONLY_MODULES": str(root / "selected")})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return [json.loads(line) for line in (root / "calls").read_text().splitlines()]

    def test_pr_checks_changed_and_renamed_files_but_not_deleted_or_unchanged(self):
        header, style = self.run_style(True,
            "TauCeti.Changed\nTauCeti.Renamed\nTauCeti.Deleted\n")
        self.assertEqual(header["args"][4:], ["TauCeti/Changed.lean", "TauCeti/Renamed.lean"])
        self.assertEqual(style["imports"],
            ["import TauCeti", "import TauCeti.Changed", "import TauCeti.Renamed"])

    def test_staging_checks_all_files_even_if_a_selection_is_present(self):
        header, style = self.run_style(False, "TauCeti.Changed\n")
        self.assertEqual(len(header["args"][4:]), 3)
        self.assertIn("import TauCeti.Unchanged", style["imports"])

    def test_deleted_only_scope_keeps_root_style_check(self):
        (style,) = self.run_style(True, "TauCeti.Deleted\n")
        self.assertEqual(style["imports"], ["import TauCeti"])
