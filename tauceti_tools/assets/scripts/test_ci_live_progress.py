import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("ci_live_progress.py")
spec = importlib.util.spec_from_file_location("progress", SCRIPT)
progress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(progress)


class Progress(unittest.TestCase):
    def test_bounded_counters_do_not_publish_log_text_or_raw_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "phases.tsv").write_text("PRIVATE_COMMAND\t1\nbuild\t2\n")
            (path / "lake-build.log").write_text(
                "PRIVATE_TOKEN=/private/path\n✔ [1/2] Built TauCeti.Example (1s)\n✖ [2/2] Building TauCeti.Failed\n"
            )
            value = progress.Counters(path).sample(clock=lambda: 100)
            self.assertEqual(value["phase"], "build")
            self.assertEqual(value["completed_modules"], 1)
            self.assertEqual(value["failed_modules"], 1)
            self.assertNotIn("PRIVATE", str(value))
            self.assertEqual(value["last_progress_at"], "1970-01-01T00:01:40Z")

    def test_candidate_symlink_and_fifo_are_never_followed_or_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry"
            path.symlink_to("/etc/passwd")
            with self.assertRaises(OSError):
                progress.bounded_file(path)
            path.unlink()
            os.mkfifo(path)
            with self.assertRaises(ValueError):
                progress.bounded_file(path)

    def test_telemetry_unavailable_preserves_child_exit_code(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--telemetry", "/missing", "--", sys.executable, "-c", "raise SystemExit(7)"],
            env={k: v for k, v in os.environ.items() if not k.startswith("ACTIONS_ID_TOKEN")},
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 7)
        self.assertNotIn("Traceback", result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
