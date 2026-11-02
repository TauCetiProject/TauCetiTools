"""Generated maintenance jobs must execute the configured immutable tool release."""

import tempfile
import unittest
from pathlib import Path

import yaml

from tauceti_tools.project import load
from tauceti_tools.scaffold import initial_configuration
from tauceti_tools.templates import write_project


class MaintenanceWorkflowPinsTests(unittest.TestCase):
    def test_all_updater_jobs_use_the_selected_tools_release(self):
        for repository, tools_repository in (
            ("TauCetiProject/TauCeti", "TauCetiProject/TauCetiTools"),
            ("TauCetiProject/TauCeti", "other/SharedTools"),
        ):
            with self.subTest(repository=repository), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                ref = "a" * 40
                configuration = root / "tauceti.toml"
                configuration.write_text(
                    initial_configuration(repository, "human")
                    + f'\n[tools.tools]\nrepo="{tools_repository}"\nref="{ref}"\n'
                    + '[tools.review]\nrepo="TauCetiProject/TauCetiReview"\nref="' + "b" * 40 + '"\n'
                )
                write_project(load(str(configuration)), root)
                text = (root / ".github/workflows/update.yml").read_text()
                self.assertNotIn("__TOOLS_", text)
                jobs = yaml.safe_load(text)["jobs"]
                for name in ("recover-maintenance", "select", "publish"):
                    with self.subTest(job=name):
                        setup = next(step for step in jobs[name]["steps"]
                                     if step.get("name") == "Prepare pinned shared tools")
                        self.assertEqual(setup["uses"], f"{tools_repository}/.github/actions/setup@{ref}")
                        self.assertEqual(setup["with"]["tools-repository"], tools_repository)
                        self.assertEqual(setup["with"]["tools-ref"], ref)


if __name__ == "__main__":
    unittest.main()
