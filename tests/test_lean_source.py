"""Shared parser regressions and the generated downstream callers that consume it."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tauceti_tools.scaffold import initial_configuration
from tauceti_tools.templates import ASSETS, python_wrapper


ROOT = Path(__file__).resolve().parents[1]


class LeanSourceTests(unittest.TestCase):
    def test_shared_parser_regressions(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(ASSETS / "scripts/test_lint_dot_notation.py")],
                cwd=directory, capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_generated_callers_keep_bundled_returns_and_detect_receivers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tauceti.toml").write_text(initial_configuration("one/Project", "human"))
            scripts = root / "scripts"
            scripts.mkdir()
            for name in ("lean_source.py", "lint-dot-notation.py"):
                (scripts / name).write_text(python_wrapper(name))
            (scripts / "lint-dot-notation-baseline.txt").write_text("")
            (root / "Mathlib").mkdir()
            (root / "Mathlib/Types.lean").write_text(
                "namespace Foo\nend Foo\nnamespace AlgHom\nend AlgHom\n"
            )
            (root / "Project").mkdir()
            (root / "Project/Test.lean").write_text(
                "namespace Project.Foo\n"
                "def returned : Foo →ₐ[R] B := f\n"
                "end Project.Foo\n"
                "namespace Project.AlgHom\n"
                "def misplaced (f : A →ₐ[R] B) := f\n"
                "end Project.AlgHom\n"
            )
            environment = dict(os.environ)
            for name in ("TAUCETI_CONF", "TAUCETI_CONF_SOURCE", "TAUCETI_CONF_SNAPSHOT",
                         "GIT_DIR", "GIT_WORK_TREE"):
                environment.pop(name, None)
            environment["PYTHONPATH"] = str(ROOT)
            arguments = ["--mathlib-root", "Mathlib", "--source-root", "Project"]
            for name in ("lean_source.py", "lint-dot-notation.py"):
                with self.subTest(caller=name):
                    result = subprocess.run(
                        [sys.executable, str(scripts / name), *arguments],
                        cwd=root, env=environment, capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("1 total, 0 grandfathered, 1 new", result.stdout)
                    self.assertIn("Project.AlgHom.misplaced", result.stdout)
                    self.assertNotIn("Project.Foo.returned", result.stdout)
