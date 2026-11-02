"""Complete watchdog snapshots, pagination, and fail-closed evidence."""

import copy
import unittest

import stuck_alerts as sa
from stuck_snapshot import Snapshot


def node(number=1):
    return dict(number=number, headRefOid="head", headRefName="feature", isDraft=False,
                updatedAt="2026-11-01T00:00:00Z", author=dict(login="author"),
                headRepository=dict(nameWithOwner="fork/repo"), headRef=dict(target=dict(oid="tip")),
                labels=dict(nodes=[], pageInfo=dict(hasNextPage=False)),
                commits=dict(nodes=[dict(commit=dict(oid="head", status=dict(contexts=[
                    dict(context="build", state="SUCCESS", createdAt="2026-11-01T00:00:00Z")]),
                    statusCheckRollup=None))]))


class Client:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def graphql(self, query, variables):
        self.calls.append(variables)
        return dict(repository=dict(pullRequests=self.pages.pop(0)))


def page(nodes, cursor=None):
    return dict(nodes=nodes, pageInfo=dict(hasNextPage=cursor is not None, endCursor=cursor))


class SnapshotTests(unittest.TestCase):
    def test_snapshot_reuses_pages_for_statuses_and_branch_tips(self):
        client = Client([page([node()], "next"), page([node(2)])])
        snapshot = Snapshot("owner/repo", client)
        self.assertEqual(len(snapshot.pulls()), 2)
        self.assertEqual(snapshot.pulls()[0]["branch_tip"], "tip")
        self.assertEqual(snapshot.status("head", "build"), ("success", "2026-11-01T00:00:00Z"))
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1]["cursor"], "next")

    def test_partial_snapshot_is_never_cached_as_complete(self):
        broken = node(2)
        broken["commits"]["nodes"][0]["commit"]["oid"] = "different-head"
        client = Client([page([node()], "next"), page([broken])])
        snapshot = Snapshot("owner/repo", client)
        with self.assertRaises(RuntimeError):
            snapshot.pulls()
        with self.assertRaises(RuntimeError):
            snapshot.pulls()
        self.assertEqual(len(client.calls), 2)

    def test_truncated_labels_and_nonadvancing_cursor_fail_closed(self):
        broken = node()
        broken["labels"]["pageInfo"]["hasNextPage"] = True
        for pages in ([page([broken])], [page([], "same"), page([], "same")]):
            with self.assertRaises(RuntimeError):
                Snapshot("owner/repo", Client(copy.deepcopy(pages))).pulls()

    def test_cancelled_head_build_is_detected_without_per_pr_run_requests(self):
        from unittest.mock import patch
        record = dict(number=8, head="head", draft=False, checks_truncated=False, checks=[dict(
            __typename="CheckRun", name="sandboxed-build", status="COMPLETED",
            conclusion="CANCELLED", completedAt="2026-10-01T00:00:00Z")])
        with patch.object(sa, "open_prs", return_value=[record]), patch.object(sa, "newest_status", return_value=(None, None)):
            self.assertEqual(sa.detect_missing_required_status()[0]["key"], "missing-status/8")

    def test_review_tracking_state_is_batched_without_assuming_absent_prs_closed(self):
        class ReviewClient:
            def __init__(self):
                self.calls = 0

            def graphql(self, query, variables):
                self.calls += 1
                return dict(repository=dict(pr1=dict(state="CLOSED", headRefOid="head"),
                                            pr2=dict(state="OPEN", headRefOid="head"), pr3=None,
                                            pr4=dict(state="MERGED", headRefOid="head")))

        client = ReviewClient()
        snapshot = Snapshot("owner/repo", client)
        snapshot.load_review_prs([1, 2, 3, 1, 4])
        self.assertEqual(snapshot.review_prs[1]["state"], "closed")
        self.assertEqual(snapshot.review_prs[2]["state"], "open")
        self.assertEqual(snapshot.review_prs[3], {})
        self.assertEqual(snapshot.review_prs[4]["state"], "closed")
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
