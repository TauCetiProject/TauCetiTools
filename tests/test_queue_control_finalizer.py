import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).parents[1] / "tauceti_tools/assets/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("queue_control_finalizer", SCRIPTS / "finalize_batch.py")
finalizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(finalizer)


class QueueControlFinalizer(unittest.TestCase):
    def test_reserved_fresh_publisher_accepts_only_the_exact_machine_and_class(self):
        labels = ["self-hosted", "linux", "x64", "tauceti-fleet-queue-control", "fleet-" + "a" * 32]
        sandbox = dict(name="sandboxed-build", runner_name="build-guest")
        job = dict(name=finalizer.PUBLISHER, runner_name="fleet-" + "a" * 12 + "-" + "b" * 16, labels=labels)
        data = dict(jobs=[sandbox, job])
        with patch.dict(os.environ, CI_PUBLISHER_RUNNER_LABELS=json.dumps(labels)):
            self.assertTrue(finalizer.isolated_runner(data, job, 12))
            for bad in (
                dict(job, runner_name="build-guest"),
                dict(job, runner_name="fleet-" + "c" * 12 + "-" + "b" * 16),
                dict(job, labels=job["labels"] + ["tauceti-fleet-pr"]),
                dict(job, labels=[x.replace("a" * 32, "c" * 32) for x in job["labels"]]),
                dict(job, labels=[finalizer.MACHINE_LABEL]),
            ):
                self.assertFalse(finalizer.isolated_runner(data, bad, 12))

    def test_invalid_trusted_configuration_fails_closed_and_legacy_remains_supported(self):
        job = dict(
            name=finalizer.PUBLISHER,
            runner_name="publisher",
            labels=[finalizer.MACHINE_LABEL],
        )
        data = dict(jobs=[dict(name="sandboxed-build", runner_name="build"), job])
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(finalizer.isolated_runner(data, job, 12))
        for value in ("invalid", "{}", "[]", "[1]", '["self-hosted"]'):
            with patch.dict(os.environ, CI_PUBLISHER_RUNNER_LABELS=value):
                self.assertFalse(finalizer.isolated_runner(data, job, 12))
