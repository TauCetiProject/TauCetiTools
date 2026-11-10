"""Exercise the native staging boundary without a candidate checkout or credentials."""

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / "tauceti_tools/assets/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("native_lake_cache_tests", SCRIPTS / "test_lake_cache_publish.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
PublicationTests = module.PublicationTests
