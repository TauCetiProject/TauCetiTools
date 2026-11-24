"""Run finalizer and publisher contracts against the shipped workflow."""
import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = tempfile.TemporaryDirectory(prefix="installed-finalizer-")
project = Path(FIXTURE.name)
(project / "scripts").mkdir()
(project / ".github/workflows").mkdir(parents=True)
for workflow in ("pr-build.yml", "publish-lake-cache.yml"):
    shutil.copyfile(ROOT / ".github/workflows" / workflow, project / ".github/workflows" / workflow)
source = ROOT / "tauceti_tools/assets/scripts"
shutil.copyfile(source / "test_finalize_batch.py", project / "scripts/test_finalize_batch.py")
sys.path.insert(0, str(source))
spec = importlib.util.spec_from_file_location("installed_finalizer_tests", project / "scripts/test_finalize_batch.py")
contracts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contracts)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([
        loader.loadTestsFromTestCase(contracts.FinalizerTests),
        loader.loadTestsFromTestCase(PublicationGate),
    ])


class PublicationGate(unittest.TestCase):
    def test_staged_cache_publication_survives_check_and_reporting_failure(self):
        # A queue batch publishes whenever it staged its outputs, even when status reporting failed:
        # only `build` lets a candidate land. Staging itself requires every sandboxed phase to pass
        # (test_failed_checks_stage_nothing).
        gate = yaml.safe_load((ROOT / ".github/workflows/pr-build.yml").read_text())["jobs"]["publish-merge-group-cache"]["if"]
        for cancelled, staged, required, expected in (
            (False, "true", "true", True),
            (False, "false", "true", False),
            (False, "", "true", False),
            (False, "true", "false", False),
            (True, "true", "true", False),
        ):
            with self.subTest(cancelled=cancelled, staged=staged, required=required):
                expression = gate.removeprefix("${{").removesuffix("}}")
                for key, value in {
                    "cancelled()": cancelled,
                    "needs.sandboxed-build.outputs.cache-staged": staged,
                    "needs.sandboxed-build.outputs.publication-required": required,
                }.items():
                    expression = expression.replace(key, repr(value))
                expression = expression.replace("always()", "True").replace("&&", " and ").replace("!", " not ")
                self.assertEqual(eval(expression.strip(), {"__builtins__": {}}), expected)

    def test_failed_checks_stage_nothing(self):
        # Batch 506 attempt 339 published two corrupt archives after its audit driver crashed on them:
        # a failed audit or lint can mean a bad tree, so only a run whose every phase passed stages.
        steps = yaml.safe_load((ROOT / ".github/workflows/pr-build.yml").read_text())["jobs"]["sandboxed-build"]["steps"]
        stage = next(step for step in steps if step.get("id") == "cache_stage")
        self.assertIn("steps.build.outcome == 'success'", stage["if"])
        self.assertNotIn("steps.sandbox_build.outcome", stage["if"])
        build = next(step for step in steps if step.get("id") == "build")
        for phase in ("sandbox_build", "sandbox_audits", "sandbox_module_system", "sandbox_lint_env",
                      "sandbox_lint_style", "sandbox_finish"):
            self.assertIn(f"steps.{phase}.outcome == 'success'", build["if"])
        self.assertLess(steps.index(build), steps.index(stage))
