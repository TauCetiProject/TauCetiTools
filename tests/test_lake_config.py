"""Exercise shipped config attestation and integration against installed scripts."""

import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = tempfile.TemporaryDirectory(prefix="installed-lake-config-")
project = Path(FIXTURE.name)
shutil.copytree(ROOT / "tauceti_tools/assets/scripts", project / "scripts")
(project / ".github/workflows").mkdir(parents=True)
shutil.copyfile(ROOT / ".github/workflows/pr-build.yml", project / ".github/workflows/pr-build.yml")
sys.path.insert(0, str(project / "scripts"))


def load_contract(name):
    spec = importlib.util.spec_from_file_location("installed_" + name, project / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


attestation = load_contract("test_attest_pr_config")
integration = load_contract("test_prepare_pr_integration")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([
        loader.loadTestsFromTestCase(attestation.ConfigAttestation),
        loader.loadTestsFromTestCase(integration.IntegrationTests),
    ])
