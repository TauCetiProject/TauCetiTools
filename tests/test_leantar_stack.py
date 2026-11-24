"""Every place a TauCeti build runs leantar raises its stack limit first.

leantar 0.1.21 (Lean v4.37.0-rc1) recurses once per object in an olean's longest chain. For
TauCeti.Algebra.Lie.F4.ModularLattice that needs about 11 MiB of stack to unpack and 14 MiB to
pack, so at Linux's default 8 MiB it exits 139. A workflow step that missed the raise broke every
API docs run for days (Nov 2026). The static checks below find any `lake build` that may restore or
pack artifacts without the raise; the integration check (LEANTAR_STACK_INTEGRATION=1, with the
toolchain installed) unpacks the real archive through the raise and shows it still needs one.

The scan is a heuristic, not a proof: it sees the cache variables anywhere in a job's YAML (env,
`$GITHUB_ENV` writes) or the workflow's env, and literal `lake build` in a step's script. A build
inside a wrapper script or a composite action, or cache settings made there, needs its own check,
as sandbox-build.sh has below.
"""

import hashlib
import json
import os
import re
import resource
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SANDBOX_BUILD = ROOT / "tauceti_tools" / "assets" / "scripts" / "sandbox-build.sh"
FIXTURE = ROOT / "tests" / "fixtures" / "leantar" / "9dc0e2c61f2c8dc3.ltar"
FIXTURE_OLEAN_SHA256 = "774331ee38d09b184eff691d9b5c27801b06dc8c18c3ca731554310d6e99d1fc"
RAISE = 'ulimit -s unlimited 2>/dev/null || ulimit -s "$(ulimit -H -s)" 2>/dev/null || true'
LAKE_BUILD = re.compile(r"\blake build\b")
DEFAULT_STACK = 8 * 1024 * 1024


def lake_build_steps():
    """(workflow, job name, job, step) for every step that runs `lake build`."""
    import yaml  # only the static checks need it; the integration job has no PyYAML

    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for job_name, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if LAKE_BUILD.search(step.get("run") or ""):
                    yield path.name, job_name, dict(job, workflow_env=workflow.get("env") or {}), step


def may_use_artifacts(job, step):
    """Whether Lake may pack or restore artifacts with leantar in this step."""
    import yaml

    env = {**job["workflow_env"], **(job.get("env") or {}), **(step.get("env") or {})}
    if str(env.get("LAKE_ARTIFACT_CACHE", "")).strip().lower() == "false":
        return False
    text = yaml.safe_dump(job)
    return "LAKE_ARTIFACT_CACHE" in text or "LAKE_RESTORE_ARTIFACTS" in text


class StackRaiseCoverage(unittest.TestCase):
    def test_every_artifact_lake_build_raises_the_stack_first(self):
        checked = []
        for workflow, job_name, job, step in lake_build_steps():
            if not may_use_artifacts(job, step):
                continue
            run = step["run"]
            where = f"{workflow} {job_name} / {step.get('name')}"
            checked.append(where)
            with self.subTest(step=where):
                self.assertIn(RAISE, run, f"{where} runs `lake build` with the artifact cache but never raises the stack")
                self.assertLess(run.index(RAISE), LAKE_BUILD.search(run).start(),
                                f"{where} raises the stack only after `lake build`")
        # The scan must keep finding the steps this was written for.
        for expected in ("api-docs.yml build-docs", "ci.yml build", "nightly-verify.yml verify",
                         "nightly-verify.yml reference"):
            self.assertTrue(any(where.startswith(expected) for where in checked), expected)

    def test_sandbox_build_raises_the_stack_before_any_lake_command(self):
        text = SANDBOX_BUILD.read_text()
        self.assertIn(RAISE, text)
        before = text[:text.index(RAISE)]
        self.assertIsNone(re.search(r"^\s*[^#\n]*\blake\b", before, re.M),
                          "sandbox-build.sh runs Lake before raising the stack")


def leantar():
    """The toolchain's leantar, or None."""
    try:
        lean = subprocess.run(["elan", "which", "lean"], check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    candidate = Path(lean).parent / "leantar"
    return candidate if candidate.exists() else None


def default_stack():
    resource.setrlimit(resource.RLIMIT_STACK, (DEFAULT_STACK, resource.getrlimit(resource.RLIMIT_STACK)[1]))


@unittest.skipUnless(os.environ.get("LEANTAR_STACK_INTEGRATION") == "1", "set LEANTAR_STACK_INTEGRATION=1")
class RealArchive(unittest.TestCase):
    """Lake's own invocation (`leantar -C lib -C ir -x -j -`) on the archive that overflows."""

    def setUp(self):
        hard = resource.getrlimit(resource.RLIMIT_STACK)[1]
        if hard != resource.RLIM_INFINITY and hard < 16 * 1024 * 1024:
            self.fail(f"this runner's hard stack limit ({hard} bytes) is below what leantar needs; the raise cannot work here")

    def unpack(self, prelude):
        tool = leantar()
        if tool is None:
            self.fail("no leantar on PATH's toolchain; install TauCeti's toolchain first")
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        (work / "lib").mkdir()
        (work / "ir").mkdir()
        # Archives in TauCeti's cache are packed without their input hash; Lake supplies one.
        request = json.dumps([{"file": str(FIXTURE), "hash": "0000000000000001"}])
        script = f'{prelude}\nexec "$0" -C lib -C ir -x -j -'
        result = subprocess.run(["bash", "-c", script, str(tool)], cwd=work, input=request, text=True,
                                capture_output=True, preexec_fn=default_stack, env=dict(os.environ, RAYON_NUM_THREADS="4"))
        return result, work / "lib" / "ModularLattice.olean"

    def test_the_raise_lets_leantar_unpack_the_archive(self):
        result, olean = self.unpack(RAISE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(hashlib.sha256(olean.read_bytes()).hexdigest(), FIXTURE_OLEAN_SHA256)

    def test_the_archive_still_needs_the_raise(self):
        result, _ = self.unpack(":")
        if result.returncode == 0:
            self.skipTest("this leantar unpacks the archive at 8 MiB; the raise is no longer needed")
        # A segfault: -11 when leantar is the process itself, 139 when a shell reports it.
        self.assertIn(result.returncode, (-11, 139), result.stderr)


if __name__ == "__main__":
    unittest.main()
