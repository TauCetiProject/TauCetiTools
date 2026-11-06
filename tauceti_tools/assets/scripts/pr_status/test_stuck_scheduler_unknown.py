"""Keep unreadable scheduler alerts active while observed siblings reconcile."""
import unittest
from unittest.mock import patch

import stuck_alerts as sa


class PartialSchedulerTests(unittest.TestCase):
    def test_unreadable_workflow_keeps_its_alert_while_healthy_workflow_clears(self):
        from test_stuck_alerts import FakeZulip, active_content, msg
        for prefix, detector, helper in (
                ("dead-scheduler", sa.detect_dead_schedulers, "_check_scheduler"),
                ("failing-scheduler", sa.detect_failing_schedulers, "_check_scheduler_health")):
            def check(wf, *_args):
                if wf == "unknown.yml":
                    raise RuntimeError("API unavailable")
                return []
            with (self.subTest(prefix=prefix), patch.object(sa, "SCHEDULERS", {"unknown.yml": ("U", 1), "healthy.yml": ("H", 1)}),
                  patch.object(sa, helper, side_effect=check), patch.object(sa, "DETECTORS", [(prefix, detector)])):
                alerts, failed = sa.collect_alerts()
            self.assertEqual(failed, {prefix + "/unknown.yml"})
            z = FakeZulip([msg(1, active_content(prefix + "/unknown.yml")),
                           msg(2, active_content(prefix + "/healthy.yml"))])
            sa.reconcile(z, alerts, failed, False)
            self.assertEqual([number for number, _content in z.updated], [2])
            with patch.object(sa, "DETECTORS", []):
                self.assertEqual(sa.collect_alerts(), ([], set()))

    def test_one_unknown_scheduler_does_not_hide_another_observed_failure(self):
        def check(wf, *_args):
            if wf == "unknown.yml":
                raise RuntimeError("API unavailable")
            return [dict(key="dead-scheduler/broken.yml", title="Broken", body="Observed failure")]
        with (patch.object(sa, "SCHEDULERS", {"unknown.yml": ("U", 1), "broken.yml": ("B", 1)}),
              patch.object(sa, "_check_scheduler", side_effect=check),
              patch.object(sa, "DETECTORS", [("dead-scheduler", sa.detect_dead_schedulers)])):
            alerts, failed = sa.collect_alerts()
        self.assertEqual([a["key"] for a in alerts], ["dead-scheduler/broken.yml"])
        self.assertEqual(failed, {"dead-scheduler/unknown.yml"})


if __name__ == "__main__":
    unittest.main()
