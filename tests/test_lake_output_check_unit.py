"""Run the built-output check's unit tests under the package test discovery."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "tauceti_tools/assets/scripts/test_lake_output_check.py"
spec = importlib.util.spec_from_file_location("lake_output_check_unit_tests", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
OutputCheckTests = module.OutputCheckTests
