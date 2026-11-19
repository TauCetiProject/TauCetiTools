"""Priority discovery never authorizes a bypass; the normal assessment does."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import batch
import priority


def row(number, head="a"*40, contexts=()):
    return dict(number=number, headRefOid=head, commits=dict(nodes=[dict(commit=dict(
        oid=head, status=dict(contexts=list(contexts))))]))


def page(rows, cursor=None):
    return dict(repository=dict(pullRequests=dict(nodes=rows,
                pageInfo=dict(hasNextPage=cursor is not None, endCursor=cursor))))


class PriorityTests(unittest.TestCase):
    def test_complete_pagination_and_pr_bound_status(self):
        api = Mock()
        api.graphql.side_effect = [page([row(1), row(2, contexts=[dict(context="self-tested/2", state="SUCCESS")])], "next"),
                                   page([row(3, contexts=[dict(context="self-tested/999", state="SUCCESS")])])]
        old = {"1": dict(head="a"*40, first_seen_at="2026-11-15T01:00:00Z"),
               "3": dict(head="b"*40, first_seen_at="2026-11-15T00:00:00Z"),
               "4": dict(head="a"*40, first_seen_at="2026-11-14T00:00:00Z")}
        pending = priority.scan("org/repo", old, now="2026-11-15T02:00:00Z", api=api)
        self.assertEqual(set(pending), {"1", "3"})
        self.assertEqual(pending["1"]["first_seen_at"], old["1"]["first_seen_at"])
        self.assertEqual(pending["3"]["first_seen_at"], "2026-11-15T02:00:00Z")
        self.assertEqual(api.graphql.call_args_list[1].args[1]["cursor"], "next")

    def test_incomplete_inventory_is_not_reported_as_empty(self):
        api = Mock()
        api.graphql.side_effect = [page([row(1)], "next"), RuntimeError("unavailable")]
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            priority.scan("org/repo", {}, now="2026-11-15T02:00:00Z", api=api)

    def test_summary_distinguishes_retained_count_and_persists_wait_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.md"
            with patch.dict(os.environ, GITHUB_STEP_SUMMARY=str(path)):
                priority.report({"1": dict(first_seen_at="2026-11-15T01:00:00Z")},
                                observed=False, now="2026-11-15T02:00:00Z")
            self.assertIn("3600s", path.read_text())
            self.assertIn("current count unverified", path.read_text())

    def test_assessment_precedes_run_discovery_and_retains_unfinished_work(self):
        pending = {"1": dict(head="a"*40, first_seen_at="2026-11-15T01:00:00Z"),
                   "2": dict(head="b"*40, first_seen_at="2026-11-15T02:00:00Z")}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            with patch.object(batch.priority, "scan", side_effect=[pending, {"2": pending["2"]}]), \
                    patch.object(batch.labels, "reconcile") as assess, \
                    patch.object(batch, "open_heads", side_effect=AssertionError("expensive discovery")), \
                    patch.object(batch.core, "_BLOCKED_UNTIL", 0):
                self.assertEqual(batch.main(["--since", "2026-11-15T00:00:00Z", "--state-out", str(state),
                                             "--prioritize-self-tested", "--self-tested-limit", "1"]), 75)
                assess.assert_called_once_with("1")
            saved = json.loads(state.read_text())
            self.assertEqual(saved["self_tested_pending"], {"2": pending["2"]})
            self.assertEqual(saved["watermark"], "2026-11-15T00:00:00Z")
            self.assertIsNone(saved["active"])

    def test_old_checkpoint_gains_inventory_but_malformed_age_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = dict(version=1, watermark="2026-11-15T00:00:00Z", active=None, retry={}, blocked_until=0)
            path.write_text(json.dumps(state))
            loaded = batch.load_progress(path, "unused")
            self.assertEqual(loaded["self_tested_pending"], {})
            state["self_tested_pending"] = {"1": dict(head="a"*40, first_seen_at="not a timestamp")}
            path.write_text(json.dumps(state))
            with self.assertRaises(ValueError):
                batch.load_progress(path, "unused")


if __name__ == "__main__":
    unittest.main()
