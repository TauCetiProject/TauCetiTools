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
    def test_audited_cache_publication_survives_reporting_failure(self):
        gate = yaml.safe_load((ROOT / ".github/workflows/pr-build.yml").read_text())["jobs"]["publish-merge-group-cache"]["if"]
        for cancelled, result, state, required, expected in (
            (False, "success", "success", "true", True),
            (False, "failure", "success", "true", True),
            (False, "failure", "failure", "true", False),
            (False, "failure", "", "true", False),
            (False, "success", "success", "false", False),
            (True, "cancelled", "success", "true", False),
        ):
            with self.subTest(cancelled=cancelled, result=result, state=state, required=required):
                expression = gate.removeprefix("${{").removesuffix("}}")
                for key, value in {
                    "cancelled()": cancelled,
                    "needs.sandboxed-build.result": result,
                    "needs.sandboxed-build.outputs.build-state": state,
                    "needs.sandboxed-build.outputs.publication-required": required,
                }.items():
                    expression = expression.replace(key, repr(value))
                expression = expression.replace("always()", "True").replace("&&", " and ").replace("!", " not ")
                self.assertEqual(eval(expression.strip(), {"__builtins__": {}}), expected)
