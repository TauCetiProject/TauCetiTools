"""Exercise the temporary stock-Lake cache extraction workaround."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "tauceti_tools/assets/scripts/test_bulk_unpack_cache.py"
spec = importlib.util.spec_from_file_location("bulk_cache_tests", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
BulkUnpackTests = module.BulkUnpackTests
