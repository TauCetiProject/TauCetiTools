"""Maintenance control jobs must stay runnable without requiring Namespace downstream."""

import tempfile
import unittest
from pathlib import Path

import yaml

from tauceti_tools.project import load
from tauceti_tools.scaffold import initial_configuration
from tauceti_tools.templates import write_project


class MaintenanceControlRunnersTests(unittest.TestCase):
    def test_canonical_control_jobs_use_priority_capacity_and_downstream_keeps_github(self):
        for repository in ("TauCetiProject/TauCeti", "other/Project"):
            with self.subTest(repository=repository), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / "tauceti.toml"
                text = initial_configuration(repository, "human").replace("zulip = false", "zulip = true")
                text += '\n[tools.tools]\nrepo="TauCetiProject/TauCetiTools"\nref="' + "a" * 40 + '"\n'
                text += '[tools.review]\nrepo="TauCetiProject/TauCetiReview"\nref="' + "b" * 40 + '"\n'
                config.write_text(text)
                write_project(load(str(config)), root)
                alerts = (root / ".github/workflows/stuck-alerts.yml").read_text()
                self.assertNotIn("__MAINTENANCE_RUNNER__", alerts)
                alert_runner = yaml.safe_load(alerts)["jobs"]["stuck-alerts"]["runs-on"]
                status_text = (root / ".github/workflows/pr-status.yml").read_text()
                self.assertNotIn("__MAINTENANCE_RUNNER__", status_text)
                status = yaml.safe_load(status_text)
                condition = status["jobs"]["reconcile"]["if"]
                self.assertIn('"workflow_dispatch"', condition)
                self.assertIn('"repository_dispatch"', condition)
                status_runners = [job["runs-on"] for job in status["jobs"].values()]
                merge_text = (root / ".github/workflows/auto-merge.yml").read_text()
                merge = yaml.safe_load(merge_text)
                events = merge.get("on", merge.get(True))
                self.assertIn("labeled", events["pull_request_target"]["types"])
                self.assertIn("github.event.label.name == 'ready-to-merge'", merge_text)
                if repository == "TauCetiProject/TauCeti":
                    update = yaml.safe_load((root / ".github/workflows/update.yml").read_text())
                    runners = [alert_runner] + status_runners + [job["runs-on"] for job in update["jobs"].values()]
                    for runner in runners:
                        self.assertIn("2x4-with-features;job.priority=0", runner)
                        self.assertIn("github.run_id", runner)
                else:
                    self.assertEqual(alert_runner, "ubuntu-latest")
                    self.assertTrue(all(runner == "ubuntu-latest" for runner in status_runners))
                    self.assertFalse((root / ".github/workflows/update.yml").exists())


if __name__ == "__main__":
    unittest.main()
