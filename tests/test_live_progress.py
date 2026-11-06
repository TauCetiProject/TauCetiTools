"""Trusted reporter isolation and its actual bounded-reader/exit-status regressions."""

import importlib.util
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "tauceti_tools/assets/scripts/test_ci_live_progress.py"
spec = importlib.util.spec_from_file_location("progress_regressions", ASSET)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Progress = module.Progress


class WorkflowIsolation(unittest.TestCase):
    def test_reporter_wraps_the_existing_clean_environment_and_sandbox(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/pr-build.yml").read_text())
        job = workflow["jobs"]["sandboxed-build"]
        self.assertEqual(job["permissions"]["id-token"], "write")
        caller = yaml.safe_load((ROOT / "tauceti_tools/assets/project/.github/workflows/pr-build.yml").read_text())
        self.assertEqual(caller["permissions"]["id-token"], "write")
        build = next(s["run"] for s in job["steps"] if s.get("id") == "build")
        self.assertIn('"${progress_observer[@]}" "${resource_observer[@]}" env -i /usr/bin/bwrap', build)
        self.assertNotIn("--setenv ACTIONS_ID_TOKEN", build)
        self.assertIn("--unshare-pid --unshare-net", build)
        self.assertIn("--remount-ro /", build)

    def test_post_build_shim_probe_cannot_reach_the_job_oidc_or_host_processes(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/pr-build.yml").read_text())
        step = next(s for s in workflow["jobs"]["sandboxed-build"]["steps"] if s.get("id") == "shim_expiry")
        self.assertIn("env -i /usr/bin/bwrap --tmpfs /", step["run"])
        self.assertIn("--unshare-pid --unshare-net", step["run"])
        self.assertIn("--remount-ro /", step["run"])
        self.assertNotIn("--setenv ACTIONS_ID_TOKEN", step["run"])
        self.assertIn('"${probe_mounts[@]}"', step["run"])


if __name__ == "__main__":
    unittest.main()
