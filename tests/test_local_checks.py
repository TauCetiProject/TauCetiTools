"""Local entry points use the shared checks without GitHub or a second build."""

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("local_checks", SCRIPTS / "local_checks.py")
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class LocalChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.previous)
        self.env = patch.dict(os.environ, TAUCETI_PROJECT_ROOT=str(self.root))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.org")
        (self.root / "TauCeti").mkdir()
        (self.root / "TauCeti.lean").write_text("module\n")
        (self.root / "TauCeti/A.lean").write_text("module\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", "https://github.com/TauCetiProject/TauCeti.git")
        self.git("update-ref", "refs/remotes/origin/main", self.base)

    def git(self, *args):
        return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL, text=True).strip()

    def test_verify_orders_sources_before_one_build_and_reuses_ci_helpers(self):
        calls = []
        actual_run = subprocess.run
        with (
            patch.object(checks, "run", side_effect=lambda name, *a, **kw: calls.append(name)),
            patch.object(checks.subprocess, "run", wraps=subprocess.run) as execute,
            contextlib.redirect_stdout(io.StringIO()),
        ):

            def invoke(argv, **kw):
                if argv[0] == "git":
                    return actual_run(argv, **kw)
                calls.append(argv[:2])
                return subprocess.CompletedProcess(argv, 0)

            execute.side_effect = invoke
            self.assertEqual(checks.main(["verify"]), 0)
        self.assertLess(calls.index("check_source_options.py"), calls.index(["lake", "build"]))
        self.assertEqual(calls.count(["lake", "build"]), 1)
        self.assertEqual(calls.count("local_axioms.py"), 1)
        self.assertEqual(calls.count("lint-style.sh"), 1)
        self.assertEqual(calls.count("local_lint.py"), 1)

    def test_new_file_length_and_import_boundary(self):
        selected = checks.scope(self.root, self.base, "ordinary")
        with patch.object(checks, "run"):
            (self.root / "TauCeti/New.lean").write_text("\n" * 1001)
            selected = checks.scope(self.root, self.base, "ordinary")
            with self.assertRaisesRegex(checks.Violation, "1000 lines"):
                checks.sources(self.root, selected)
            (self.root / "TauCeti/New.lean").write_text("public import TauCetiReview\n")
            with self.assertRaisesRegex(checks.Violation, "roadmap/review"):
                checks.sources(self.root, selected)

    def test_unknown_scope_never_runs_checks(self):
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        with patch.object(checks, "run") as run:
            with self.assertRaisesRegex(ValueError, "no local canonical"):
                checks.main(["test"])
        run.assert_not_called()

    @unittest.skipUnless(os.environ.get("TAUCETI_LAKE_FIXTURE"), "set TAUCETI_LAKE_FIXTURE for real Lake drivers")
    def test_real_lake_drivers_and_failure_exits(self):
        toolchain = os.environ.get("ELAN_TOOLCHAIN", "leanprover/lean4:v4.36.0-rc2")
        (self.root / "lean-toolchain").write_text(toolchain + "\n")
        (self.root / ".gitignore").write_text(".lake/\n")
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("LocalTest.lean", "LocalLintMain.lean"):
            shutil.copyfile(SCRIPTS / name, scripts / name)
        (scripts / "local-checks.sh").write_text(f'exec "{sys.executable}" "{SCRIPTS / "local_checks.py"}" "$@"\n')
        (self.root / "lakefile.toml").write_text(
            'name = "fixture"\ndefaultTargets = ["TauCeti"]\ntestDriver = "localTest"\nlintDriver = "localLint"\n'
            '[[lean_lib]]\nname = "TauCeti"\nglobs = ["TauCeti.*"]\n'
            '[[lean_exe]]\nname = "localTest"\nroot = "scripts.LocalTest"\n'
            '[[lean_exe]]\nname = "localLint"\nroot = "scripts.LocalLintMain"\n'
        )
        env = dict(os.environ, LAKE_ARTIFACT_CACHE="false", LAKE_NO_CACHE="true")

        def lake(*args):
            return subprocess.run(["lake", *args], env=env, capture_output=True, text=True, timeout=120)

        built = lake("build", "--iofail")
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        tested = lake("test")
        self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)
        (self.root / "TauCeti/A.lean").write_text("module\npublic axiom bad : False\n")
        self.assertEqual(lake("build", "--iofail").returncode, 0)
        bad = lake("test")
        self.assertNotEqual(bad.returncode, 0, bad.stdout + bad.stderr)
        self.assertIn("axiom declaration", bad.stdout + bad.stderr)
        (self.root / "TauCeti/A.lean").write_text("module\nset_option maxHeartbeats 0\n")
        lint = lake("lint")
        self.assertNotEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        self.assertIn("set_option", lint.stdout + lint.stderr)
        (self.root / "TauCeti/A.lean").write_text("module\n#check Nat\n")
        self.assertNotEqual(lake("build", "--iofail").returncode, 0)


if __name__ == "__main__":
    unittest.main()
