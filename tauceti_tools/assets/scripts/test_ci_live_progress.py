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

    def test_a_later_phase_session_reads_the_finished_build_log_at_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "phases.tsv").write_text("build\t1\nlint-env\t2\n")
            (path / "lake-build.log").write_text(
                "".join(f"✔ [{i}/20000] Built TauCeti.Module{i} (1s)\n" for i in range(20000)))
            self.assertGreater((path / "lake-build.log").stat().st_size, 2 * 262144)
            counters = progress.Counters(path)
            first = counters.sample(clock=lambda: 100)
            self.assertEqual(first["completed_modules"], 20000)
            self.assertEqual(first["phase"], "lint-env")
            # Nothing new: no progress is reported by later samples.
            self.assertEqual(counters.sample(clock=lambda: 200), first)

    def test_lake_job_counter_and_rate_during_the_build(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "phases.tsv").write_text("cache-unpack\t1\nbuild\t2\n")
            log = path / "lake-build.log"
            # Lake numbers a finished job's line by the jobs finished before it.
            log.write_text("✔ [99/1000] Replayed Mathlib.Foo\n")
            counters = progress.Counters(path)
            first = counters.sample(clock=lambda: 1000)
            self.assertEqual((first["completed_tasks"], first["total_tasks"]), (100, 1000))
            self.assertIsNone(first["tasks_per_minute"])
            with log.open("a") as stream:
                stream.write("  info: [999/1000] candidate text indented under a job is ignored\n")
                stream.write("⚠ [399/1000] Built TauCeti.Example (1s)\n")
            second = counters.sample(clock=lambda: 1120)
            self.assertEqual(second["completed_tasks"], 400)
            self.assertEqual(second["tasks_per_minute"], 150.0)
            with log.open("a") as stream:
                stream.write("✔ [1000/1000] Built TauCeti.Impossible\nBuild completed successfully (1000 jobs).\n")
            last = counters.sample(clock=lambda: 1240)
            self.assertEqual((last["completed_tasks"], last["total_tasks"]), (1000, 1000))
            # Only samples from the last ten minutes count towards the rate: none after a gap.
            self.assertIsNone(counters.sample(clock=lambda: 1240 + 900)["tasks_per_minute"])
            self.assertEqual(counters.sample(clock=lambda: 1240 + 990)["tasks_per_minute"], 0.0)

    def test_a_replaced_log_starts_a_new_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "phases.tsv").write_text("build\t1\n")
            log = path / "lake-build.log"
            log.write_text("✔ [9/1000] Built TauCeti.A\n")
            counters = progress.Counters(path)
            counters.sample(clock=lambda: 100)
            replacement = path / "replacement.log"
            replacement.write_text("✔ [499/1000] Built TauCeti.B\n")
            os.replace(replacement, log)
            value = counters.sample(clock=lambda: 200)
            self.assertEqual(value["completed_tasks"], 500)
            self.assertIsNone(value["tasks_per_minute"])

    def test_a_log_backlog_is_read_before_any_rate_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "phases.tsv").write_text("build\t1\n")
            log = path / "lake-build.log"
            log.write_text("✔ [0/200000] Built TauCeti.A\n")
            counters = progress.Counters(path)
            counters.sample(clock=lambda: 100)
            line = "✔ [{}/200000] Built TauCeti.Module{} (1s)" + " " * 60 + "\n"
            with log.open("a") as stream:
                stream.write("".join(line.format(i, i) for i in range(1, 200000)))
            self.assertGreater(log.stat().st_size, progress.SAMPLE_BUDGET)
            behind = counters.sample(clock=lambda: 200)
            self.assertLess(behind["completed_tasks"], 200000)
            self.assertIsNone(behind["tasks_per_minute"])
            caught_up = counters.sample(clock=lambda: 215)
            self.assertEqual(caught_up["completed_tasks"], 200000)
            self.assertIsNotNone(caught_up["tasks_per_minute"])

    def test_each_sandboxed_step_continues_the_job_session(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "session.json"
            session, sequence = progress.load_session(state)
            self.assertEqual(sequence, 0)
            progress.save_session(state, session, 7)
            self.assertEqual(progress.load_session(state), (session, 7))
            for bad in ("{", '{"session": "x", "sequence": 1}', '{"session": "%s", "sequence": -1}' % session):
                state.write_text(bad)
                self.assertEqual(progress.load_session(state)[1], 0)
            self.assertEqual(progress.load_session(None)[1], 0)

    def test_no_counter_and_no_rate_outside_the_build(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "phases.tsv").write_text("build\t1\naudit-axioms\t2\n")
            (path / "lake-build.log").write_text("✔ [4/9] Built TauCeti.Example (1s)\n")
            counters = progress.Counters(path)
            counters.sample(clock=lambda: 100)
            value = counters.sample(clock=lambda: 400)
            self.assertEqual(value["phase"], "audit-axioms")
            self.assertEqual((value["completed_tasks"], value["total_tasks"]), (5, 9))
            self.assertIsNone(value["tasks_per_minute"])
            (path / "lake-build.log").unlink()
            self.assertIsNone(progress.Counters(path).sample(clock=lambda: 100)["completed_tasks"])

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
