"""Complete watchdog snapshots, pagination, and fail-closed evidence."""

import copy
import datetime
import json
import re
import unittest
from unittest.mock import patch

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


def connection(nodes, cursor=None):
    return dict(nodes=nodes, pageInfo=dict(hasNextPage=cursor is not None, endCursor=cursor))


def timeline(kind="LabeledEvent", at="2026-11-01T00:00:00Z"):
    return dict(__typename=kind, createdAt=at, **(
        {"label": {"name": "ready-to-merge"}} if kind != "RemovedFromMergeQueueEvent" else {}))


def review_comment(state="green", updated="2026-11-02T00:00:00Z"):
    meta = json.dumps(dict(head_sha="head", states={"correctness": state}))
    return dict(body="<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 " + meta + "-->", updatedAt=updated)


def evidence(number=1, **fields):
    return dict(number=number, state="OPEN", headRefOid="head", mergeable="MERGEABLE", **fields)


class EvidenceClient:
    def __init__(self, replies=None):
        self.replies = list(replies) if replies is not None else None
        self.calls = []

    def graphql(self, query, variables):
        self.calls.append(query)
        if self.replies is not None:
            return {"repository": self.replies.pop(0)}
        rows = {}
        for match in re.finditer(r"pr(\d+):pullRequest", query):
            number = int(match.group(1))
            fields = {}
            if "timelineItems(" in query:
                fields["timelineItems"] = connection([timeline()] + [
                    timeline("RemovedFromMergeQueueEvent", f"2026-11-02T0{hour}:00:00Z") for hour in (6, 7, 8)])
            if "comments(" in query:
                fields["comments"] = connection([review_comment()])
            if "files(" in query:
                fields["files"] = connection([{"path": "TauCeti/Test.lean"}])
            rows[f"pr{number}"] = evidence(number, **fields)
        return {"repository": rows}


def prepared(client, count=1):
    snapshot = Snapshot("owner/repo", client)
    snapshot._rows = [dict(number=number, head="head", draft=False, labels=["ready-to-merge"],
                           statuses=[dict(context="build", state="SUCCESS", createdAt="2026-11-01T00:00:00Z")])
                      for number in range(1, count + 1)]
    return snapshot


class BatchedEvidenceTests(unittest.TestCase):
    def test_hundreds_of_ready_prs_do_not_make_per_pr_rest_requests(self):
        client = EvidenceClient()
        snapshot = prepared(client, 730)
        now = datetime.datetime(2026, 11, 2, 12, tzinfo=datetime.timezone.utc)
        with (patch.object(sa, "_snapshot", snapshot), patch.object(sa, "now_utc", return_value=now),
              patch.object(sa.core, "gh_api", side_effect=AssertionError("per-PR REST request"))):
            stranded = sa.detect_stranded_prs()
            evictions = sa.detect_eviction_loops()
        self.assertEqual(len(stranded), 730)
        self.assertEqual(len(evictions), 730)
        self.assertEqual({a["key"] for a in stranded}, {f"stranded-pr/{n}" for n in range(1, 731)})
        self.assertLess(len(client.calls), 100)

    def test_timeline_pages_preserve_current_cycle_and_are_shared(self):
        client = EvidenceClient([
            {"pr1": evidence(timelineItems=connection([timeline(), timeline("RemovedFromMergeQueueEvent", "2026-11-02T06:00:00Z")], "next"))},
            {"pr1": evidence(timelineItems=connection([timeline("UnlabeledEvent", "2026-11-02T07:00:00Z"),
                                                      timeline(at="2026-11-02T08:00:00Z"),
                                                      timeline("RemovedFromMergeQueueEvent", "2026-11-02T09:00:00Z")]))},
        ])
        snapshot = prepared(client)
        with patch.object(sa, "_snapshot", snapshot):
            self.assertEqual(sa.ready_label_applied_at(1), "2026-11-02T08:00:00Z")
            self.assertEqual(sa.detect_eviction_loops(), [])
        self.assertEqual(len(client.calls), 2)
        self.assertIn('after:"next"', client.calls[1])

    def test_independent_comment_and_file_cursors_do_not_repeat_finished_connections(self):
        client = EvidenceClient([
            {"pr1": evidence(comments=connection([review_comment()], "c2"), files=connection([{"path": "TauCeti/A.lean"}]))},
            {"pr1": evidence(comments=connection([review_comment("blocking_request")]))},
        ])
        snapshot = prepared(client)
        snapshot.load_readiness([1])
        self.assertEqual(len(snapshot.readiness_prs[1]["comments"]), 2)
        self.assertNotIn("files(", client.calls[1])
        self.assertIn('after:"c2"', client.calls[1])

    def test_a_human_owned_path_on_later_page_is_not_mistaken_for_auto_merge_scope(self):
        client = EvidenceClient([
            {"pr1": evidence(timelineItems=connection([timeline()]))},
            {"pr1": evidence(comments=connection([review_comment()]), files=connection([{"path": "TauCeti/A.lean"}], "f2"))},
            {"pr1": evidence(files=connection([{"path": "scripts/check.py"}]))},
        ])
        snapshot = prepared(client)
        now = datetime.datetime(2026, 11, 2, 12, tzinfo=datetime.timezone.utc)
        with patch.object(sa, "_snapshot", snapshot), patch.object(sa, "now_utc", return_value=now):
            self.assertEqual(sa.detect_stranded_prs(), [])
        self.assertNotIn("comments(", client.calls[2])

    def test_missing_alias_wrong_identity_head_changes_and_partial_pages_fail_closed(self):
        variants = [{}, {"pr1": None}, {"pr1": evidence(number=2)},
                    {"pr1": {**evidence(timelineItems=connection([])), "headRefOid": "changed"}},
                    {"pr1": evidence(timelineItems={"nodes": []})},
                    {"pr1": evidence(timelineItems=connection([{"__typename": "LabeledEvent"}]))}]
        for response in variants:
            with self.subTest(response=response):
                client = EvidenceClient([response])
                snapshot = prepared(client)
                with self.assertRaises((RuntimeError, KeyError)):
                    snapshot.timeline_events(1)
                with self.assertRaises(RuntimeError):
                    snapshot.timeline_events(1)
                self.assertEqual(len(client.calls), 1)

    def test_repeated_cursor_fails_instead_of_treating_partial_history_as_complete(self):
        response = {"pr1": evidence(timelineItems=connection([timeline()], "same"))}
        snapshot = prepared(EvidenceClient([response, response]))
        with self.assertRaises(RuntimeError):
            snapshot.timeline_events(1)

    def test_pr_closed_during_collection_has_no_active_readiness(self):
        client = EvidenceClient([{"pr1": {"number": 1, "state": "MERGED"}}])
        snapshot = prepared(client)
        self.assertEqual(snapshot.timeline_events(1), [])

    def test_newest_updated_scoreboard_still_wins_across_pages(self):
        client = EvidenceClient([
            {"pr1": evidence(timelineItems=connection([timeline()]))},
            {"pr1": evidence(comments=connection([review_comment("blocking_request", "2026-11-02T11:00:00Z")], "c2"),
                             files=connection([{"path": "TauCeti/A.lean"}]))},
            {"pr1": evidence(comments=connection([review_comment("green", "2026-11-02T10:00:00Z")]))},
        ])
        snapshot = prepared(client)
        now = datetime.datetime(2026, 11, 2, 12, tzinfo=datetime.timezone.utc)
        with patch.object(sa, "_snapshot", snapshot), patch.object(sa, "now_utc", return_value=now):
            self.assertEqual(sa.detect_stranded_prs(), [])


if __name__ == "__main__":
    unittest.main()
