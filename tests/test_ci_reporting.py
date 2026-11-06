"""Run the shared CI chart regression suite in package CI."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts"))
import test_ci_graphs


def load_tests(loader, tests, pattern):
    tests.addTests(loader.loadTestsFromModule(test_ci_graphs))
    return tests


class RunnerIdentityTests(unittest.TestCase):
    def test_older_runner_names_and_conflicting_evidence(self):
        from tauceti_tools.assets.scripts.ci_runners import reporting_kind
        name = "fleet-11e2756fc110-5b75de8785749239"
        self.assertEqual(reporting_kind("self-hosted", ["tauceti-fleet"], name), "leanfro")
        self.assertEqual(reporting_kind("self-hosted", ["fleet-host-chris-server"], name), "self-hosted")
        self.assertEqual(reporting_kind("github", [], name), "github")
        self.assertEqual(reporting_kind("self-hosted", [], "not-a-fleet-runner"), "self-hosted")

    def test_database_retains_raw_category_and_archived_name(self):
        import sqlite3
        import tempfile
        from unittest.mock import patch
        from tauceti_tools.collector import build_db
        record = {"repo": "TauCeti", "run_id": 1, "run_attempt": 1,
                  "workflow": ".github/workflows/pr-build.yml", "event": "pull_request", "trigger": "pr",
                  "head_sha": "a" * 40, "conclusion": "success",
                  "updated_at": "2026-10-22T10:00:00Z",
                  "created_at": "2026-10-22T10:00:00Z", "jobs": [
                      {"id": 10, "name": "sandboxed-build", "runner_kind": "self-hosted",
                       "labels": ["self-hosted", "tauceti-fleet"],
                       "runner_name": "fleet-11e2756fc110-0123456789abcdef"}]}
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "ci.sqlite"
            with patch.object(build_db, "records", side_effect=lambda kind: iter([record] if kind == "jobs" else [])):
                build_db.build(out)
            with sqlite3.connect(out) as db:
                self.assertEqual(db.execute("SELECT runner_kind,runner_name FROM jobs").fetchone(),
                                 ("self-hosted", "fleet-11e2756fc110-0123456789abcdef"))
