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
                if Path(argv[0]).name == "git":
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

    def test_lint_runs_the_shared_pr_diff_guards_on_the_working_tree(self):
        selected = checks.scope(self.root, self.base, "ordinary")
        with patch.object(checks, "run"), contextlib.redirect_stdout(io.StringIO()):
            # Untracked scratch outside the library is not PR content.
            (self.root / "NOTES.md").write_text("scratch\n")
            checks.sources(self.root, selected)
            # A tracked edit outside the library is.
            (self.root / "scripts").mkdir()
            (self.root / "scripts/tool.py").write_text("print()\n")
            self.git("add", "scripts/tool.py")
            with self.assertRaisesRegex(checks.Violation, "human merge: scripts/tool.py"):
                checks.sources(self.root, selected)
            self.git("rm", "-q", "--cached", "scripts/tool.py")
            (self.root / "TauCeti/A.lean").write_text("module\n" + "".join(f"-- {i}\n" for i in range(2001)))
            with self.assertRaisesRegex(checks.Violation, "too large to review"):
                checks.sources(self.root, selected)
            # The Lake-pin bump exemption from the line caps applies locally too.
            (self.root / "lean-toolchain").write_text("leanprover/lean4:v4.36.0\n")
            self.git("add", "lean-toolchain")
            checks.sources(self.root, selected)
        # Nothing was staged or committed by the measurement.
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "lean-toolchain")

    def test_base_override_measures_from_the_given_revision(self):
        (self.root / "TauCeti/B.lean").write_text("".join(f"-- {i}\n" for i in range(1500)))
        self.git("add", ".")
        self.git("commit", "-qm", "batch parent adds a long file")
        parent = self.git("rev-parse", "HEAD")
        with patch.object(checks, "run"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(checks.Violation, "1000 lines"):
                checks.sources(self.root, checks.scope(self.root, self.base, "ordinary"))
            checks.sources(self.root, checks.scope(self.root, parent, "ordinary"))

    def test_lint_runs_ci_dot_notation_lint_scoped_like_ci(self):
        calls = []

        def record(name, *args, **kwargs):
            if name == "lint-dot-notation.py":
                base = Path(args[args.index("--base-source-root") + 1]) if "--base-source-root" in args else None
                calls.append((args, base is not None and (base / "A.lean").is_file()))

        (self.root / "TauCeti/A.lean").write_text("module\n-- edited\n")
        with patch.object(checks, "run", side_effect=record), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checks.main(["dot-notation"]), 0)
            (self.root / "lean-toolchain").write_text("leanprover/lean4:v4.36.0\n")
            self.git("add", "lean-toolchain")
            self.assertEqual(checks.main(["dot-notation"]), 0)
        (scoped, base_extracted), (full, _) = calls
        self.assertTrue(base_extracted)
        self.assertEqual(scoped[scoped.index("--source-root") + 1], self.root / "TauCeti")
        self.assertEqual(Path(scoped[scoped.index("--only-modules") + 1]).name, "modules.txt")
        self.assertEqual(scoped[scoped.index("--baseline") + 1], checks.SCRIPTS / "lint-dot-notation-baseline.txt")
        # A Lake-pin change checks the whole library, as CI does.
        self.assertNotIn("--only-modules", full)

    def test_unknown_scope_never_runs_checks(self):
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        with patch.object(checks, "run") as run:
            with self.assertRaisesRegex(ValueError, "no local canonical"):
                checks.main(["test"])
        run.assert_not_called()

    def test_moving_an_existing_long_file_is_not_a_new_file(self):
        (self.root / "TauCeti/A.lean").write_text("module\n" + "-- existing content\n" * 1200)
        self.git("add", ".")
        self.git("commit", "-qm", "existing long module")
        base = self.git("rev-parse", "HEAD")
        self.git("mv", "TauCeti/A.lean", "TauCeti/Moved.lean")
        with patch.object(checks, "run"):
            checks.sources(self.root, checks.scope(self.root, base, "ordinary"))

    def test_broad_policies_keep_whole_library_source_style(self):
        for policy, changed_only in (("ordinary", "1"), ("bump", "0"), ("full-lint", "0")):
            with patch.object(checks, "run") as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checks.main(["style", "--policy", policy]), 0)
                self.assertEqual(run.call_args.kwargs["env"]["LINT_CHANGED_ONLY"], changed_only)

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

        def lake(*args, cwd=None):
            return subprocess.run(["lake", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120)

        built = lake("build", "--iofail")
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        tested = lake("test")
        self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)
        outside = lake("-d", str(self.root), "test", cwd=self.root.parent)
        self.assertEqual(outside.returncode, 0, outside.stdout + outside.stderr)
        (self.root / "TauCeti/A.lean").write_text("module\npublic axiom bad : False\n")
        self.assertEqual(lake("build", "--iofail").returncode, 0)
        bad = lake("test")
        self.assertNotEqual(bad.returncode, 0, bad.stdout + bad.stderr)
        self.assertIn("axiom declaration", bad.stdout + bad.stderr)
        (self.root / "TauCeti/A.lean").write_text("module\nset_option maxHeartbeats 0\n")
        lint = lake("-d", str(self.root), "lint", cwd=self.root.parent)
        self.assertNotEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        self.assertIn("set_option", lint.stdout + lint.stderr)
        (self.root / "TauCeti/A.lean").write_text("module\n#check Nat\n")
        self.assertNotEqual(lake("build", "--iofail").returncode, 0)


if __name__ == "__main__":
    unittest.main()
