"""Exercise the native staging boundary without a candidate checkout or credentials."""

import importlib.util
import sys
import unittest
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).parents[1] / "tauceti_tools/assets/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("native_lake_cache_tests", SCRIPTS / "test_lake_cache_publish.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
PublicationTests = module.PublicationTests


class WorkflowStructure(unittest.TestCase):
    def test_steps_have_an_action_and_environment_is_a_mapping(self):
        for path in (Path(__file__).parents[1] / ".github/workflows").glob("*.yml"):
            for job in yaml.safe_load(path.read_text()).get("jobs", {}).values():
                if not isinstance(job, dict):
                    continue
                for step in job.get("steps", []):
                    with self.subTest(workflow=path.name, step=step.get("name")):
                        self.assertTrue("run" in step or "uses" in step)
                        if "env" in step:
                            self.assertIsInstance(step["env"], dict)
