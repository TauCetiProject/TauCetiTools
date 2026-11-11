"""Exercise the real sandbox bootstrap with Ubuntu downloads unavailable."""

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from tauceti_tools.project import parse
from tauceti_tools.scaffold import initial_configuration
from tauceti_tools.templates import materialize

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github/workflows/pr-build.yml",
    ROOT / "tauceti_tools/assets/project/.github/workflows/nightly-verify.yml",
    ROOT / "tauceti_tools/assets/project/.github/workflows/update-upstreams.yml",
)


def installer(path):
    workflow = yaml.safe_load(path.read_text())
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name", "").startswith("Install bubblewrap"):
                values = {**job.get("env", {}), **step.get("env", {})}
                return step["run"], {k: str(v) for k, v in values.items() if k.startswith("BWRAP_")}
    raise AssertionError(f"No bubblewrap installer in {path}")


class BwrapBundle(unittest.TestCase):
    def prepare(self, root):
        scripts = root / "runner/tauceti-shared-scripts"
        project = parse(initial_configuration("one/Project", "human").encode())
        materialize(project, scripts)
        return scripts

    def test_materialized_package_matches_every_installer_pin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = self.prepare(root)
            for path in WORKFLOWS:
                with self.subTest(workflow=path.name):
                    _, env = installer(path)
                    package = scripts / "vendor" / f"bubblewrap_{env['BWRAP_VER']}_amd64.deb"
                    self.assertEqual(hashlib.sha256(package.read_bytes()).hexdigest(), env["BWRAP_DEB_SHA256"])

    def test_bootstrap_works_offline_and_rejects_corrupted_packages(self):
        for path in WORKFLOWS:
            for corrupt in (False, True):
                with self.subTest(workflow=path.name, corrupt=corrupt), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    scripts = self.prepare(root)
                    shell, values = installer(path)
                    package = scripts / "vendor" / f"bubblewrap_{values['BWRAP_VER']}_amd64.deb"
                    if corrupt:
                        package.write_bytes(package.read_bytes() + b"corrupted")
                    commands = root / "bin"
                    commands.mkdir()
                    curl = commands / "curl"
                    curl.write_text("#!/bin/sh\necho 'Unexpected network download' >&2\nexit 88\n")
                    curl.chmod(0o755)
                    env = dict(
                        os.environ,
                        **values,
                        RUNNER_TEMP=str(root / "runner"),
                        PATH=str(commands) + os.pathsep + os.environ["PATH"],
                    )
                    result = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", shell.split("sudo dpkg -i", 1)[0]],
                        cwd=root,
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, int(corrupt), result.stdout + result.stderr)
                    self.assertNotIn("Unexpected network download", result.stderr)

    @unittest.skipUnless(os.environ.get("BWRAP_LIVE_INSTALL") == "1", "requires disposable Ubuntu runner")
    def test_installer_and_isolation_self_test_on_ubuntu(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            shell, values = installer(WORKFLOWS[0])
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", shell],
                cwd=root,
                env=dict(os.environ, **values, RUNNER_TEMP=str(root / "runner")),
                capture_output=True,
                text=True,
                timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("self-test: run=0", result.stdout)
            self.assertIn("/usr/bin/bwrap: OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
