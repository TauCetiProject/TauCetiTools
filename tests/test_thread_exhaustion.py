"""thread_exhaustion.py reports a build failed only by the host running out of threads, and nothing else."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tauceti_tools/assets/scripts/thread_exhaustion.py"

LAKE_ABORT = ("libc++abi: terminating due to uncaught exception of type lean::exception: "
              "failed to create thread: Resource temporarily unavailable")
LEANTAR = ("The global thread pool has not been initialized.: ThreadPoolBuildError { kind: IOError(Os { "
           'code: 11, kind: WouldBlock, message: "Resource temporarily unavailable" }) }')
UNPACK_FAILED = [
    "✔ [100/200] Unpacked TauCeti.A",
    "✖ [101/200] Unpacking TauCeti.B",
    "trace: .> leantar -x .lake/build/lib/lean/TauCeti/B.ltar",
    "error: stderr:",
    LEANTAR,
    "error: external command '/toolchain/bin/leantar' exited with code 101",
    "Some required targets logged failures:",
    "- TauCeti.B:unpack",
    "error: build failed",
]
BUILD_FAILED = ["✖ [150/200] Building TauCeti.C", "error: TauCeti/C.lean:3:0: unknown identifier 'x'"]


class ThreadExhaustion(unittest.TestCase):
    def run_build(self, lines, status, *, color=False):
        """Run the wrapper around a fake build printing `lines` and exiting `status`."""
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "out.txt"
            text = "\n".join(lines) + "\n" if lines else ""
            if color:
                text = text.replace("✖", "\x1b[31m✖").replace("\n", "\x1b[0m\n")
            out.write_text(text)
            fake = textwrap.dedent(f"""
                import os, sys
                sys.stdout.buffer.write(open({str(out)!r}, 'rb').read())
                sys.stdout.flush()
                code = {status}
                if code < 0:
                    os.kill(os.getpid(), -code)
                sys.exit(code)
            """)
            result = subprocess.run([sys.executable, str(SCRIPT), sys.executable, "-c", fake],
                                    capture_output=True)
            return result.returncode, result.stdout.decode(), text

    def test_output_and_status_pass_through(self):
        for lines, status in ((["✔ [1/1] Built TauCeti.A"], 0), (BUILD_FAILED, 1), ([], 3)):
            with self.subTest(status=status):
                code, stdout, text = self.run_build(lines, status)
                self.assertEqual((code, stdout), (status, text))

    def test_a_leantar_thread_pool_failure_is_infrastructure(self):
        code, stdout, text = self.run_build(UNPACK_FAILED, 1)
        self.assertEqual(code, 75)
        self.assertTrue(stdout.startswith(text))
        self.assertIn("::error::the CI host ran out of threads", stdout)
        self.assertEqual(self.run_build(UNPACK_FAILED, 1, color=True)[0], 75)

    def test_a_lake_abort_is_infrastructure(self):
        lines = ["✔ [9193/62168] Unpacked TauCeti.A", LAKE_ABORT]
        self.assertEqual(self.run_build(lines, -6)[0], 75)
        self.assertEqual(self.run_build(lines, 134)[0], 75)

    def test_a_killed_build_keeps_its_shell_status(self):
        self.assertEqual(self.run_build(["✔ [1/2] Built TauCeti.A"], -9)[0], 137)

    def test_any_other_failed_job_keeps_the_build_failure(self):
        # Candidate code can print only from a module it compiles, and under --iofail that output
        # fails the module's Building job, so printing the error text never earns a retry.
        # Lake marks a job whose log reached info (ℹ) or warning (⚠) that way, and --iofail fails it.
        for extra in (BUILD_FAILED, ["✖ [5/9] Running TauCeti.D:extraDep"], ["✖ [7/9] Building TauCeti.E", LAKE_ABORT],
                      ["ℹ [7/9] Built TauCeti.E (1.2s)", "info: TauCeti/E.lean:1:0: " + LAKE_ABORT],
                      ["⚠ [7/9] Built TauCeti.E (1.2s)", "warning: TauCeti/E.lean:1:0: " + LEANTAR]):
            with self.subTest(extra=extra[0]):
                self.assertEqual(self.run_build(UNPACK_FAILED + extra, 1)[0], 1)
                self.assertEqual(self.run_build(extra + [LAKE_ABORT], 134)[0], 134)

    def test_a_failure_without_either_error_is_not_infrastructure(self):
        lines = ["✖ [101/200] Unpacking TauCeti.B", "error: external command 'leantar' exited with code 1"]
        self.assertEqual(self.run_build(lines, 1)[0], 1)

    def test_a_cancelled_build_keeps_its_status(self):
        fake = ("import signal, sys, time\n"
                "signal.signal(signal.SIGTERM, lambda *a: sys.exit(143))\n"
                f"print({LAKE_ABORT!r}, flush=True)\n"
                "time.sleep(30)\n")
        wrapper = subprocess.Popen([sys.executable, str(SCRIPT), sys.executable, "-c", fake], stdout=subprocess.PIPE)
        self.assertIn(b"failed to create thread", wrapper.stdout.readline())
        wrapper.terminate()
        wrapper.communicate(timeout=10)
        self.assertEqual(wrapper.returncode, 143)

    def test_a_closed_reader_does_not_change_the_status(self):
        for status, expected in ((0, 0), (1, 75)):
            with self.subTest(status=status):
                fake = (f"import sys\nprint({LAKE_ABORT!r})\nsys.stdout.write('x' * 1000000)\nsys.exit({status})\n")
                read, write = os.pipe()
                os.close(read)
                result = subprocess.run([sys.executable, str(SCRIPT), sys.executable, "-c", fake], stdout=write,
                                        stderr=subprocess.PIPE)
                os.close(write)
                self.assertEqual((result.returncode, result.stderr), (expected, b""))

    def test_output_is_streamed_without_waiting_for_a_newline(self):
        fake = "import sys, time\nsys.stdout.write('partial'); sys.stdout.flush(); time.sleep(30)\n"
        wrapper = subprocess.Popen([sys.executable, str(SCRIPT), sys.executable, "-c", fake], stdout=subprocess.PIPE)
        try:
            self.assertEqual(wrapper.stdout.read(7), b"partial")
        finally:
            wrapper.kill()
            wrapper.communicate()

    def test_a_job_mark_inside_an_over_long_line_is_not_a_job(self):
        lines = UNPACK_FAILED[:-1] + ["x" * (3 << 20) + "✖ [7/9] Building TauCeti.E"]
        self.assertEqual(self.run_build(lines, 1)[0], 75)
        self.assertEqual(self.run_build(lines + BUILD_FAILED, 1)[0], 1)

    def test_a_passing_build_that_mentions_the_error_still_passes(self):
        self.assertEqual(self.run_build([LAKE_ABORT, LEANTAR], 0)[0], 0)


if __name__ == "__main__":
    unittest.main()
