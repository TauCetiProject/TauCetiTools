#!/usr/bin/env python3
"""Hermetic tests for scripts/pr_stats_graphs.py."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import chart_style
import pr_stats_graphs as stats


# Helvetica's ascender and descender as fractions of the size: the box a line of text sits in.
# Chromium's ink for these headers stays inside it (two subtitle lines' ink spans 0.94 of the size).
ASCENT, DESCENT = 0.77, 0.23


def header_gaps(root):
    """The clear space between each pair of consecutive header lines (the title, then each subtitle
    line), as a fraction of the subtitle's rendered size."""
    width = float(root.attrib["viewBox"].split()[2])
    scale = width / chart_style.REFERENCE_WIDTH
    sizes = {"title": chart_style.TITLE_SIZE * scale, "subtitle": chart_style.SUBTITLE_SIZE * scale}
    lines = [(float(node.attrib["y"]), sizes[node.attrib["class"]])
             for node in root.iter("{http://www.w3.org/2000/svg}text")
             if node.attrib.get("class") in sizes]
    return [((lower - ASCENT * lower_size) - (upper + DESCENT * upper_size)) / sizes["subtitle"]
            for (upper, upper_size), (lower, lower_size) in zip(lines, lines[1:])]


UTC = timezone.utc


def timestamp(day: int, hour: int = 0) -> str:
    return datetime(2026, 1, day, hour, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def pr(number, created_day, *, state="CLOSED", merged_day=None, author="alice",
       labels=(), cycles=0, is_draft=False):
    events = []
    for index in range(cycles):
        day = created_day + index
        if index:
            # The author's turn between rounds; without it the next label continues one cycle.
            events.append({"created_at": timestamp(day, 6), "label": "awaiting-author"})
        events.append({"created_at": timestamp(day, 8), "label": "awaiting-review"})
        # Claiming the round and having the label restored is churn inside the same cycle.
        events.append({"created_at": timestamp(day, 9), "label": "review-in-progress"})
        events.append({"created_at": timestamp(day, 10), "label": "awaiting-review"})
    if "awaiting-author" in labels:
        events.append({"created_at": timestamp(13, 12), "label": "awaiting-author"})
    if "review-in-progress" in labels and not cycles:
        events.append({"created_at": timestamp(13, 10), "label": "review-in-progress"})
    return {
        "number": number,
        "created_at": timestamp(created_day),
        "updated_at": timestamp(merged_day or 14, 12),
        "merged_at": timestamp(merged_day, 12) if merged_day else None,
        "closed_at": timestamp(merged_day, 12) if merged_day else None,
        "state": state,
        "is_draft": is_draft,
        "author": author,
        "labels": list(labels),
        "labeled_events": events,
    }


class MetricsTest(unittest.TestCase):
    def test_outer_pr_page_reads_no_label_timeline(self):
        self.assertIn("pullRequests(first:100", stats.PR_PAGE_QUERY)
        self.assertEqual(stats.PR_PAGE_QUERY.count("timelineItems"), 1)
        self.assertIn("itemTypes:[RENAMED_TITLE_EVENT]", stats.PR_PAGE_QUERY)
        self.assertNotIn("LABELED_EVENT", stats.PR_PAGE_QUERY)

    def test_direct_label_timeline_pagination_is_not_truncated(self):
        first_page = {
            "repository": {"pullRequests": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{
                    "number": 42, "createdAt": timestamp(1), "updatedAt": timestamp(3),
                    "mergedAt": None,
                    "closedAt": None, "state": "OPEN", "isDraft": False,
                    "author": {"login": "alice"}, "labels": {"nodes": []},
                }],
            }},
        }
        timeline_page = {
            "repository": {"pullRequest": {"timelineItems": {
                "pageInfo": {"hasNextPage": True, "endCursor": "events-100"},
                "nodes": [{"createdAt": timestamp(2),
                           "label": {"name": "awaiting-review"}}],
            }, "mergedAt": None, "closedAt": None, "state": "OPEN",
                "isDraft": False, "labels": {"nodes": []}}},
        }
        timeline_extra = {
            "repository": {"pullRequest": {"timelineItems": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"createdAt": timestamp(3),
                           "label": {"name": "awaiting-review"}}],
            }, "mergedAt": None, "closedAt": None, "state": "OPEN",
                "isDraft": False,
                "labels": {"nodes": [{"name": "awaiting-review"}]}}},
        }
        with patch.object(
            stats, "graphql",
            side_effect=[first_page, timeline_page, timeline_extra],
        ):
            prs = stats.fetch_prs("example/project")
        self.assertEqual(
            [event["label"] for event in prs[0]["labeled_events"]],
            ["awaiting-review", "awaiting-review"],
        )
        self.assertEqual(prs[0]["labels"], ["awaiting-review"])

    def test_closed_pr_uses_complete_direct_timeline(self):
        first_page = {
            "repository": {"pullRequests": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{
                    "number": 42, "createdAt": timestamp(1), "updatedAt": timestamp(3),
                    "mergedAt": None,
                    "closedAt": timestamp(14), "state": "CLOSED", "isDraft": False,
                    "author": {"login": "alice"},
                    "labels": {"nodes": [{"name": "roadmap/PDE"}]},
                }],
            }},
        }
        direct = {
            "repository": {"pullRequest": {"timelineItems": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {"createdAt": timestamp(min(index + 1, 14)),
                     "label": {"name": "awaiting-review"}}
                    for index in range(9)
                ],
            }, "mergedAt": None, "closedAt": timestamp(14), "state": "CLOSED",
                "isDraft": False,
                "labels": {"nodes": [{"name": "roadmap/PDE"}]}}},
        }
        with patch.object(stats, "LIFECYCLE_EPOCH", datetime(2026, 1, 1, tzinfo=UTC)):
            with patch.object(stats, "graphql", side_effect=[first_page, direct]):
                prs = stats.fetch_prs("example/project")
        self.assertEqual(len(prs[0]["labeled_events"]), 9)

    def test_pr_closed_before_lifecycle_epoch_skips_timeline(self):
        first_page = {
            "repository": {"pullRequests": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{
                    "number": 42, "createdAt": timestamp(1), "updatedAt": timestamp(2),
                    "mergedAt": timestamp(2),
                    "closedAt": timestamp(2), "state": "MERGED", "isDraft": False,
                    "author": {"login": "alice"}, "labels": {"nodes": []},
                }],
            }},
        }
        with patch.object(stats, "graphql", return_value=first_page) as graphql:
            prs = stats.fetch_prs("example/project")
        self.assertEqual(prs[0]["labeled_events"], [])
        self.assertEqual(graphql.call_count, 1)

    # --- timelines are fetched one at a time, on purpose --------------------------------------
    #
    # Aliasing several pull requests into one GraphQL request is much cheaper and silently
    # returns incomplete timelines. Measured on 2026-09-06: in a 50-alias batch PR #1556 came
    # back with 2 label events and `hasNextPage: false` where the paginating query returns 4,
    # the missing one being its current `ci-failed`. Smaller batches were complete for that
    # sample, so the limit follows total requested nodes and cannot be pinned to a size, and
    # `hasNextPage` is computed against the truncated slice so nothing in the response reveals
    # the loss. This test exists so the optimisation cannot be reintroduced without meeting it.

    def test_each_pull_request_gets_its_own_query(self):
        numbers = [1, 2, 3]
        reply = {"repository": {"pullRequest": {
            "timelineItems": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                              "nodes": [{"createdAt": timestamp(2),
                                         "label": {"name": "awaiting-review"}}]},
            "mergedAt": None, "closedAt": None, "state": "OPEN", "isDraft": False,
            "labels": {"nodes": [{"name": "awaiting-review"}]}}}}
        with patch.object(stats, "graphql", return_value=reply) as graphql:
            result, fetched = stats.fetch_timelines("o", "n", numbers)
        self.assertEqual(graphql.call_count, 3)
        self.assertEqual(fetched, 3)
        self.assertEqual(len(result), 3)
        # Each call names exactly one pull request.
        for call in graphql.call_args_list:
            self.assertIn("number", call.kwargs)

    def test_the_module_defines_no_alias_batching_helper(self):
        for name in ("timeline_batch_query", "TIMELINE_BATCH"):
            self.assertFalse(
                hasattr(stats, name),
                f"{name} is back. Aliased batching returns silently truncated timelines with "
                "hasNextPage false; read the note above fetch_timeline before reinstating it.")

    def test_reused_pull_requests_are_not_fetched(self):
        numbers = [1, 2, 3]
        reusable = {1: {"merged_at": None, "closed_at": None, "state": "OPEN",
                        "is_draft": False, "labels": [], "labeled_events": []}}
        reply = {"repository": {"pullRequest": {
            "timelineItems": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []},
            "mergedAt": None, "closedAt": None, "state": "OPEN", "isDraft": False,
            "labels": {"nodes": []}}}}
        with patch.object(stats, "graphql", return_value=reply) as graphql:
            result, fetched = stats.fetch_timelines("o", "n", numbers, reusable)
        self.assertEqual(graphql.call_count, 2)
        self.assertEqual(fetched, 2)
        self.assertEqual(len(result), 3)

    # --- incremental snapshots -------------------------------------------------------------
    #
    # One GraphQL request per pull request, against an hourly budget of 5000 points, is a wall
    # this repository walked into: by September 2026 a full pass needed about 4600 requests and
    # the charts ahead of it in the Pages job spent the rest, so the snapshot stopped being
    # written at all and the published pipeline-health.json simply never appeared. Reuse is what
    # keeps a run proportional to what changed rather than to how big the project has become, so
    # these tests pin down both that it happens and that it cannot serve stale events.

    OPEN_PAGE = {
        "repository": {"pullRequests": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{
                "number": 42, "createdAt": timestamp(1), "updatedAt": timestamp(3),
                "mergedAt": None, "closedAt": None, "state": "OPEN", "isDraft": False,
                "author": {"login": "alice"},
                "labels": {"nodes": [{"name": "awaiting-review"}]},
            }],
        }},
    }

    def snapshot_with(self, updated_at, events):
        return {"repo": "example/project", "fetched_at": timestamp(3), "prs": [{
            "number": 42, "updated_at": updated_at, "merged_at": None, "closed_at": None,
            "state": "OPEN", "is_draft": False, "labels": ["awaiting-review"],
            "labeled_events": events,
        }]}

    def test_unchanged_pr_reuses_its_recorded_timeline(self):
        events = [{"created_at": timestamp(2), "label": "awaiting-review"}]
        previous = self.snapshot_with(timestamp(3), events)
        with patch.object(stats, "graphql", side_effect=[self.OPEN_PAGE]) as graphql:
            prs = stats.fetch_prs("example/project", previous)
        # One call: the page query. The timeline query never ran.
        self.assertEqual(graphql.call_count, 1)
        self.assertEqual(prs[0]["labeled_events"], events)

    def test_touched_pr_is_refetched(self):
        # The snapshot was taken when the PR last changed on day 2; it has changed since.
        previous = self.snapshot_with(
            timestamp(2), [{"created_at": timestamp(2), "label": "awaiting-review"}])
        direct = {"repository": {"pullRequest": {
            "timelineItems": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                              "nodes": [{"createdAt": timestamp(3),
                                         "label": {"name": "awaiting-review"}}]},
            "mergedAt": None, "closedAt": None, "state": "OPEN", "isDraft": False,
            "labels": {"nodes": [{"name": "awaiting-review"}]}}}}
        with patch.object(stats, "graphql", side_effect=[self.OPEN_PAGE, direct]) as graphql:
            prs = stats.fetch_prs("example/project", previous)
        self.assertEqual(graphql.call_count, 2)
        self.assertEqual(prs[0]["labeled_events"],
                         [{"created_at": timestamp(3), "label": "awaiting-review"}])

    def test_reuse_keeps_the_current_labels_not_the_recorded_ones(self):
        # A reused entry supplies only the event list. Its state fields come from this run's
        # page query, which was read later. Here the recorded copy disagrees, and must lose.
        previous = self.snapshot_with(
            timestamp(3), [{"created_at": timestamp(2), "label": "awaiting-review"}])
        previous["prs"][0]["labels"] = ["ready-to-merge"]
        previous["prs"][0]["state"] = "MERGED"
        with patch.object(stats, "graphql", side_effect=[self.OPEN_PAGE]):
            prs = stats.fetch_prs("example/project", previous)
        self.assertEqual(prs[0]["labels"], ["awaiting-review"])
        self.assertEqual(prs[0]["state"], "OPEN")

    def test_snapshot_without_update_times_is_not_reused(self):
        # A snapshot written before updated_at was recorded carries no certificate that its
        # events are current, so it must fall back to a full walk rather than be trusted.
        previous = self.snapshot_with(timestamp(3), [])
        del previous["prs"][0]["updated_at"]
        self.assertEqual(stats.reusable_timelines(previous, [
            {"number": 42, "updated_at": timestamp(3), "merged_at": None, "closed_at": None,
             "state": "OPEN", "is_draft": False, "labels": []}]), {})

    def test_snapshot_from_another_repository_is_ignored(self):
        previous = self.snapshot_with(timestamp(3), [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(previous))
            self.assertIsNone(stats.load_previous(path, "someone/else"))
            self.assertIsNotNone(stats.load_previous(path, "example/project"))

    def test_a_missing_or_corrupt_snapshot_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "absent.json"
            self.assertIsNone(stats.load_previous(missing, "example/project"))
            corrupt = Path(directory) / "corrupt.json"
            corrupt.write_text("{not json")
            self.assertIsNone(stats.load_previous(corrupt, "example/project"))

    def test_run_gh_waits_for_the_budget_instead_of_failing(self):
        # Three retries one, two and four seconds apart cannot refill an hourly budget, so the
        # original behaviour turned "wait a while" into "this run produces nothing".
        results = [
            SimpleNamespace(returncode=1, stdout="",
                            stderr="gh: API rate limit already exceeded for site ID installation."),
            SimpleNamespace(returncode=0, stdout="done", stderr=""),
        ]
        with patch.object(stats.subprocess, "run", side_effect=results) as run:
            with patch.object(stats, "rate_limit_reset_wait", return_value=42.0):
                with patch.object(stats.time, "sleep") as sleep:
                    self.assertEqual(stats.run_gh(["api", "graphql"]), "done")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(42.0)

    def test_rate_limit_wait_uses_the_exhausted_api_budget(self):
        limited = SimpleNamespace(returncode=1, stdout="", stderr="API rate limit exceeded")
        success = SimpleNamespace(returncode=0, stdout="done", stderr="")
        for arguments, resource in [(["api", "graphql"], "graphql"),
                                    (["api", "--paginate", "repos/o/r/issues/comments"], "core")]:
            with self.subTest(resource=resource):
                with (patch.object(stats.subprocess, "run", side_effect=[limited, success]),
                      patch.object(stats, "rate_limit_reset_wait", return_value=42.0) as reset,
                      patch.object(stats.time, "sleep")):
                    self.assertEqual(stats.run_gh(arguments), "done")
                reset.assert_called_once_with(resource)

    def test_run_gh_gives_up_when_the_budget_will_not_refill(self):
        limited = SimpleNamespace(
            returncode=1, stdout="", stderr="gh: API rate limit already exceeded")
        with patch.object(stats.subprocess, "run", return_value=limited):
            with patch.object(stats, "rate_limit_reset_wait", return_value=None):
                with self.assertRaises(RuntimeError) as caught:
                    stats.run_gh(["api", "graphql"])
        self.assertIn("--since-data", str(caught.exception))

    def test_review_cycles_use_label_transitions_and_reach_seven(self):
        prs = [
            pr(1, 1, cycles=0),
            pr(2, 2, cycles=1),
            pr(3, 3, cycles=2),
            pr(4, 4, cycles=7),
        ]
        result = stats.review_cycle_metrics(prs)
        self.assertEqual(result["total_cycles"], 10)
        self.assertEqual(result["reviewed_prs"], 3)
        self.assertEqual(
            [item["prs"] for item in result["reach"]],
            [3, 2, 1, 1, 1, 1, 1],
        )

    def test_restored_awaiting_review_label_stays_in_the_same_cycle(self):
        item = pr(1, 1)
        item["labeled_events"] = [
            {"created_at": timestamp(2, 1), "label": "awaiting-review"},
            {"created_at": timestamp(2, 2), "label": "review-in-progress"},
            # Reconciliation restores the label without the author having acted.
            {"created_at": timestamp(2, 3), "label": "awaiting-review"},
            {"created_at": timestamp(2, 4), "label": "awaiting-author"},
            {"created_at": timestamp(3, 1), "label": "awaiting-CI"},
            {"created_at": timestamp(3, 2), "label": "awaiting-review"},
        ]
        result = stats.review_cycle_metrics([item])
        self.assertEqual(result["total_cycles"], 2)
        self.assertEqual(result["cycles_by_pr"], {"1": 2})
        self.assertEqual(result["label_epoch"], "2026-01-02")

    def test_in_review_clock_survives_the_review_label_swap(self):
        item = pr(1, 1, state="OPEN", labels=("awaiting-review",))
        item["labeled_events"] = [
            {"created_at": timestamp(10, 0), "label": "awaiting-review"},
            {"created_at": timestamp(12, 0), "label": "review-in-progress"},
            {"created_at": timestamp(14, 0), "label": "awaiting-review"},
        ]
        metrics = stats.queue_age_metrics([item], datetime(2026, 1, 15, tzinfo=UTC))
        self.assertEqual(metrics["in_review_hours"], [120.0])
        self.assertEqual(metrics["missing_transition_fallbacks"], 0)

    def test_queue_age_order_and_state_clocks(self):
        snapshot = datetime(2026, 1, 15, tzinfo=UTC)
        prs = [
            pr(1, 10, state="OPEN", labels=("awaiting-author",)),
            pr(2, 11, state="OPEN", labels=("review-in-progress",), cycles=2),
            pr(3, 12, state="OPEN", labels=("awaiting-CI",)),
            pr(4, 12, state="OPEN", labels=("awaiting-author",), is_draft=True),
        ]
        metrics = stats.queue_age_metrics(prs, snapshot)
        self.assertEqual(len(metrics["total_open_hours"]), 4)
        self.assertEqual(len(metrics["awaiting_author_hours"]), 1)
        self.assertEqual(len(metrics["in_review_hours"]), 1)
        self.assertEqual(metrics["other_open_prs"], 2)

    def test_author_clock_survives_the_awaiting_author_to_ci_failed_split(self):
        # The migration that introduced ci-failed relabels PRs that were already waiting on their
        # author. That is the same wait continuing, not a new one, so the clock must still run from
        # the original awaiting-author and not reset to the relabelling instant.
        item = pr(1, 1, state="OPEN", labels=("ci-failed",))
        item["labeled_events"] = [
            {"created_at": timestamp(1, 0), "label": "awaiting-author"},
            {"created_at": timestamp(4, 0), "label": "ci-failed"},
        ]
        metrics = stats.queue_age_metrics([item], datetime(2026, 1, 5, tzinfo=UTC))
        self.assertEqual(metrics["awaiting_author_hours"], [96.0])
        self.assertEqual(metrics["missing_transition_fallbacks"], 0)

    def test_author_clock_restarts_when_the_pr_goes_back_through_ci(self):
        # A push is a real fresh wait: the author acted, CI judged it, and it failed again.
        item = pr(1, 1, state="OPEN", labels=("ci-failed",))
        item["labeled_events"] = [
            {"created_at": timestamp(1, 0), "label": "awaiting-author"},
            {"created_at": timestamp(2, 0), "label": "awaiting-CI"},
            {"created_at": timestamp(4, 0), "label": "ci-failed"},
        ]
        metrics = stats.queue_age_metrics([item], datetime(2026, 1, 5, tzinfo=UTC))
        self.assertEqual(metrics["awaiting_author_hours"], [24.0])
        self.assertEqual(metrics["missing_transition_fallbacks"], 0)

    def test_current_state_clock_rejects_stale_historical_transition(self):
        item = pr(1, 1, state="OPEN", labels=("awaiting-review",), cycles=1)
        item["labeled_events"].append({
            "created_at": timestamp(3), "label": "ready-to-merge",
        })
        metrics = stats.queue_age_metrics(
            [item], datetime(2026, 1, 5, tzinfo=UTC),
        )
        self.assertEqual(metrics["missing_transition_fallbacks"], 1)
        self.assertEqual(metrics["in_review_hours"], [96.0])

    def test_generation_rejects_excessive_missing_state_transitions(self):
        prs = [
            pr(number, number, state="OPEN", labels=("awaiting-author",))
            for number in range(1, 4)
        ]
        for item in prs:
            item["labeled_events"] = []
        data = {
            "repo": "example/project", "fetched_at": timestamp(15),
            "prs": prs, "scoreboards": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "matching label transitions"):
                stats.generate(data, Path(temporary))

    def test_scoreboards_need_trust_pull_request_and_matching_meta(self):
        def comment(identifier, number, user, canonical=True):
            return {"id": identifier, "number": str(number), "created_at": timestamp(2),
                    "updated_at": timestamp(2), "user": user, "canonical": canonical}

        store = {item["id"]: item for item in [
            comment("1", 7, "reviewer-a"),
            comment("2", 8, "issue-commenter"),       # an ordinary issue, not a PR
            comment("3", 9, "marker-quoter", False),  # the public marker without engine meta
            comment("4", 7, "forger"),
            comment("5", 7, "reviewer-b"),
            comment("6", None, "reviewer-a"),
        ]}
        scoreboards, rejected = stats.scoreboard_verdicts(
            store, {7, 9}, {"reviewer-a", "reviewer-b", "marker-quoter"})
        self.assertEqual([(item["pr"], item["user"]) for item in scoreboards],
                         [(7, "reviewer-a"), (7, "reviewer-b")])
        self.assertEqual(rejected, {"2": "not_a_pull_request", "3": "no_canonical_scoreboard_meta",
                                    "4": "untrusted_author", "6": "unparsable_issue_number"})
        # Verdicts are derived each run: trusting the forger needs no comment to be read again.
        scoreboards, _ = stats.scoreboard_verdicts(store, {7, 9}, {"reviewer-a", "forger"})
        self.assertEqual([item["user"] for item in scoreboards], ["reviewer-a", "forger"])

    # --- the comment walk ---------------------------------------------------------------------
    #
    # GitHub serves at most 300 pages of the repository comment listing from any starting point,
    # and `gh api --paginate` stops there silently. A descending scan of the whole history so
    # reached back only about a week, and its weekly full rescan dropped everything older.

    class Listing:
        """The comment listing: `sort=updated&direction=asc`, an inclusive `since`, pages of two."""

        def __init__(self, rows):
            self.rows, self.requests = rows, []

        def __call__(self, repo, since, page):
            self.requests.append((since, page))
            matching = sorted((row for row in self.rows
                               if since is None or stats.parse_dt(row["updated_at"]) >= since),
                              key=lambda row: row["updated_at"])
            return matching[(page - 1) * 2:page * 2]

    @staticmethod
    def row(identifier, day, hour=0, number=7, user="trusted", marker=True):
        return {"id": identifier, "updated_at": timestamp(day, hour), "marker": marker,
                "number": str(number), "created_at": timestamp(day, hour), "user": user,
                "canonical": True}

    def walk(self, rows, since=None, pages=10, store=None):
        listing = self.Listing(rows)
        store = {} if store is None else store
        with (patch.object(stats, "COMMENT_PAGE_SIZE", 2),
              patch.object(stats, "fetch_comment_page", side_effect=listing)):
            result = stats.walk_comments("o/r", since, pages, store)
        return result, store, listing.requests

    def test_the_walk_follows_its_cursor_past_any_page_cap(self):
        rows = [self.row(i, 2, i) for i in range(1, 8)]
        (cursor, spent, complete), store, requests = self.walk(rows)
        self.assertEqual(sorted(store, key=int), [str(i) for i in range(1, 8)])
        self.assertTrue(complete)
        self.assertEqual(cursor, stats.parse_dt(timestamp(2, 7)))
        # Every request is a first page; the cursor, not the page number, moves the walk.
        self.assertEqual({page for _, page in requests}, {1})
        self.assertEqual(spent, len(requests))

    def test_comments_sharing_a_timestamp_are_stepped_through_by_page(self):
        rows = [self.row(i, 2, 1) for i in range(1, 6)] + [self.row(6, 2, 2)]
        (_, _, complete), store, requests = self.walk(rows)
        self.assertTrue(complete)
        self.assertEqual(len(store), 6)
        self.assertIn((stats.parse_dt(timestamp(2, 1)), 2), requests)

    def test_a_walk_out_of_budget_says_where_to_resume(self):
        rows = [self.row(i, 2, i) for i in range(1, 8)]
        (cursor, spent, complete), store, _ = self.walk(rows, pages=2)
        self.assertEqual((spent, complete), (2, False))
        (_, _, complete), store, _ = self.walk(rows, since=cursor, store=store)
        self.assertTrue(complete)
        self.assertEqual(len(store), 7)

    def test_an_unbounded_walk_runs_to_the_end(self):
        rows = [self.row(i, 2, i) for i in range(1, 8)]
        (_, spent, complete), store, _ = self.walk(rows, pages=None)
        self.assertTrue(complete)
        self.assertEqual((len(store), spent), (7, 7))

    def test_an_edit_that_drops_the_marker_leaves_the_store(self):
        store = {"1": {"id": "1"}, "2": {"id": "2"}}
        _, store, _ = self.walk([self.row(1, 3, marker=False), self.row(2, 3, user="editor")],
                                store=store)
        self.assertEqual(list(store), ["2"])
        self.assertEqual(store["2"]["user"], "editor")

    def scan(self, rows, previous=None, now_day=10, pages=stats.SCOREBOARD_PAGES_PER_RUN):
        listing = self.Listing(rows)
        with (patch.object(stats, "COMMENT_PAGE_SIZE", 2),
              patch.object(stats, "fetch_comment_page", side_effect=listing)):
            boards, rejected, state = stats.fetch_scoreboards(
                "o/r", {7}, {"trusted"}, previous, datetime(2026, 1, now_day, tzinfo=UTC), pages)
        return [item["id"] for item in boards], state, listing.requests

    def test_a_cold_start_publishes_only_a_finished_full_walk(self):
        rows = [self.row(str(i), 2, i) for i in range(1, 8)]
        boards, state, requests = self.scan(rows, pages=2)
        self.assertEqual(boards, [])
        self.assertIsNotNone(state["scoreboard_rescan"])
        self.assertEqual(requests[0], (None, 1))
        boards, state, _ = self.scan(rows, previous=state)
        self.assertEqual(sorted(boards, key=int), [str(i) for i in range(1, 8)])
        self.assertIsNone(state["scoreboard_rescan"])
        self.assertEqual(state["scoreboards_rescanned_at"], timestamp(10))
        self.assertEqual(state["scoreboards_scanned_at"], timestamp(2, 7))

    def run_main(self, folder, rows, *extra):
        """`main()` against a listing of `rows`, two comments a page and three requests a run."""
        prs = [pr(7, 1, merged_day=2, author="trusted")]
        argv = ["pr_stats_graphs.py", "--repo", "o/r", "--out-dir", str(folder / "out"), *extra]
        with (patch.object(stats, "COMMENT_PAGE_SIZE", 2),
              patch.object(stats, "SCOREBOARD_PAGES_PER_RUN", 3),
              patch.object(stats, "fetch_comment_page", side_effect=self.Listing(rows)),
              patch.object(stats, "fetch_prs", side_effect=lambda repo, previous: [dict(p) for p in prs]),
              patch("sys.argv", argv)):
            return stats.main()

    def test_an_unfinished_first_walk_keeps_the_published_assets(self):
        rows = [self.row(str(i), 2, i) for i in range(1, 8)]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "out").mkdir()
            published = folder / "out" / "pr-stats.json"
            published.write_text('{"review_totals_by_contributor": {"trusted": 1}}')
            snapshot = folder / "snapshot.json"
            resumable = ("--dump-data", str(snapshot), "--since-data", str(snapshot))
            self.assertEqual(self.run_main(folder, rows, *resumable), 3)
            self.assertEqual(published.read_text(), '{"review_totals_by_contributor": {"trusted": 1}}')
            checkpoint = json.loads(snapshot.read_text())["scoreboard_rescan"]
            self.assertEqual(checkpoint["cursor"], timestamp(2, 4))
            # Replaying the unfinished checkpoint is refused the same way.
            self.assertEqual(self.run_main(folder, rows, "--data", str(snapshot)), 3)
            self.assertEqual(self.run_main(folder, rows, *resumable), 3)
            self.assertEqual(self.run_main(folder, rows, *resumable), 0)
            totals = json.loads(published.read_text())["review_totals_by_contributor"]
            self.assertEqual(totals, {"trusted": 7})

    def test_a_run_that_cannot_resume_walks_to_the_end(self):
        rows = [self.row(str(i), 2, i) for i in range(1, 8)]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.assertEqual(self.run_main(folder, rows), 0)
            totals = json.loads((folder / "out" / "pr-stats.json").read_text())
            self.assertEqual(totals["review_totals_by_contributor"], {"trusted": 7})

    def test_a_recent_full_walk_is_kept_up_from_an_hour_before_its_end(self):
        _, state, _ = self.scan([self.row("1", 2)])
        boards, state, requests = self.scan([self.row("1", 2), self.row("2", 9)], previous=state)
        self.assertEqual(requests[0], (stats.parse_dt(timestamp(1, 23)), 1))
        self.assertEqual(boards, ["1", "2"])
        self.assertIsNone(state["scoreboard_rescan"])

    def test_the_weekly_full_walk_replaces_the_published_set_only_when_done(self):
        _, state, _ = self.scan([self.row("1", 2), self.row("2", 2, 1)], now_day=1)
        live = [self.row("2", 2, 1), self.row("3", 9), self.row("4", 9, 1), self.row("5", 9, 2)]
        boards, state, _ = self.scan(live, previous=state, pages=2)
        # Deleted comment 1 is still published while the full walk is unfinished.
        self.assertEqual(boards, ["1", "2", "3", "4"])
        self.assertIsNotNone(state["scoreboard_rescan"])
        boards, state, _ = self.scan(live, previous=state)
        self.assertEqual(boards, ["2", "3", "4", "5"])
        self.assertIsNone(state["scoreboard_rescan"])

    def test_a_capped_scan_snapshot_keeps_its_scoreboards_while_a_full_walk_runs(self):
        old = {"scoreboards_scanned_at": timestamp(9), "scoreboards": [
            {"id": "1", "pr": 7, "created_at": timestamp(2), "updated_at": timestamp(2),
             "user": "trusted"},
            {"pr": 7, "created_at": timestamp(2), "updated_at": timestamp(2), "user": "trusted"},
        ], "rejected_scoreboard_comments_by_id": {"9": "untrusted_author"}}
        rows = [self.row(str(i), 1, i) for i in range(1, 8)]
        boards, state, requests = self.scan(rows + [self.row("8", 9, 12)], previous=old, pages=3)
        self.assertEqual(requests[0], (stats.parse_dt(timestamp(8, 23)), 1))
        self.assertEqual(boards, ["1", "8"])
        self.assertIsNotNone(state["scoreboard_rescan"])

    def test_scoreboard_meta_pr_number_is_matched_exactly(self):
        # #185's scoreboard pasted onto #18 shares the prefix `"pr":18`, and a string "18"
        # is not the integer the engine writes; neither may be read as #18's own scoreboard.
        for meta in ({"kind": "scoreboard", "pr": 185}, {"kind": "scoreboard", "pr": "18"}):
            self.assertFalse(stats.names_scoreboard_for([json.dumps(meta)], 18), meta)
        self.assertTrue(
            stats.names_scoreboard_for(
                ["not json", json.dumps({"kind": "scoreboard", "pr": 18, "round": 2})], 18,
            )
        )

    @unittest.skipUnless(shutil.which("jq"), "jq is not installed")
    def test_scoreboard_jq_program_executes_and_validates_metadata(self):
        def fixture(number, user, association, meta):
            body = f'<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 {json.dumps(meta)} -->'
            return {
                "id": 5000 + number,
                "issue_url": f"https://api.github.com/repos/example/project/issues/{number}",
                "created_at": timestamp(2), "updated_at": timestamp(3),
                "user": {"login": user}, "author_association": association,
                "body": body,
            }

        comments = [
            fixture(7, "trusted", "CONTRIBUTOR",
                    {"kind": "scoreboard", "pr": 7, "states": {"api": "green"}}),
            fixture(7, "wrong-pr", "MEMBER", {"kind": "scoreboard", "pr": 8}),
            fixture(7, "string-pr", "MEMBER", {"kind": "scoreboard", "pr": "7"}),
            fixture(7, "wrong-kind", "MEMBER", {"kind": "claim", "pr": 7}),
            {
                "id": 5099,
                "issue_url": "https://api.github.com/repos/example/project/issues/7",
                "created_at": timestamp(2), "updated_at": timestamp(3),
                "user": {"login": "missing-meta"}, "author_association": "MEMBER",
                "body": "<!--tauceti-scoreboard-->",
            },
        ]
        comments.append({"id": 5100, "updated_at": timestamp(4), "body": "LGTM",
                         "issue_url": "https://api.github.com/repos/example/project/issues/7",
                         "created_at": timestamp(4), "user": {"login": "commenter"}})
        result = subprocess.run(
            ["jq", "-c", stats.SCOREBOARD_JQ], input=json.dumps(comments), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            [row.get("canonical") for row in rows], [True, False, False, False, False, None],
        )
        self.assertEqual(rows[0]["user"], "trusted")
        self.assertEqual(rows[-1], {"id": 5100, "updated_at": timestamp(4), "marker": False})

    def test_snapshot_trusts_only_merged_pr_authors(self):
        prs = [
            pr(1, 1, merged_day=2, author="merged-author"),
            pr(2, 2, state="OPEN", author="open-author"),
            pr(3, 3, merged_day=4, author="unknown"),
        ]
        with (
            patch.object(stats, "fetch_prs", return_value=prs),
            patch.object(stats, "fetch_scoreboards",
                         return_value=([], {}, {})) as fetch,
        ):
            stats.fetch_snapshot("example/project")
        # Besides merged PR authors, only the review posting App's bot, which never authors a PR.
        fetch.assert_called_once_with(
            "example/project", {1, 2, 3}, {"merged-author", "tauceti-review-poster[bot]"}, None,
            pages=stats.SCOREBOARD_PAGES_PER_RUN,
        )

    def test_thousands_of_contributors_are_bounded(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        events = []
        for index in range(2_500):
            # Deterministic unequal totals make the selected top contributors stable.
            events.extend((start + timedelta(days=index % 7), f"user-{index:04d}")
                          for _ in range(1 + index % 3))
        dates, names, series, totals = stats.cumulative_chart_series(
            events, start.date(), date(2026, 1, 14), limit=12,
        )
        self.assertEqual(len(totals), 2_500)
        self.assertEqual(len(names), 13)  # top 12 + one bounded aggregate
        self.assertEqual(len(series), 13)
        self.assertTrue(names[-1].startswith("Other (2,488 contributors)"))
        self.assertTrue(all(len(values) == len(dates) for values in series.values()))

    def test_the_cutoff_excludes_events_after_the_snapshot_instant(self):
        # Independent of the end-date cut: both events fall on a day that has finished, so only
        # the cutoff can separate them. Kept as its own test because generate() now ends the
        # series on the last full day, which would hide a broken cutoff behind the date filter.
        start = datetime(2026, 1, 1, tzinfo=UTC)
        events = [
            (datetime(2026, 1, 3, 9, tzinfo=UTC), "before"),
            (datetime(2026, 1, 3, 18, tzinfo=UTC), "after"),
        ]
        _, _, _, totals = stats.cumulative_chart_series(
            events, start.date(), date(2026, 1, 5), limit=10,
            cutoff=datetime(2026, 1, 3, 12, tzinfo=UTC),
        )
        self.assertEqual(dict(totals), {"before": 1})


def rename(day, actor="tau-ceti-bors", kind="Bot", title="[Merged by Bors] - feat: x"):
    return {"createdAt": timestamp(day, 2), "currentTitle": title,
            "actor": {"__typename": kind, "login": actor}}


def comment(day, body, author="tauceti-queue", kind="Bot"):
    return {"createdAt": timestamp(day, 3), "body": body,
            "author": {"__typename": kind, "login": author}}


def comments_page(nodes, previous_cursor=None):
    return {"repository": {"pullRequest": {"comments": {
        "pageInfo": {"hasPreviousPage": previous_cursor is not None,
                     "startCursor": previous_cursor},
        "nodes": nodes,
    }}}}


def closed(number, closed_day, *, updated_day=None, rename_event=None, author="alice"):
    record = pr(number, 1, author=author)
    record.update(closed_at=timestamp(closed_day, 1),
                  updated_at=timestamp(updated_day or closed_day, 4),
                  merge_rename=stats.merge_app_rename(
                      {"mergeRenames": {"nodes": [rename_event] if rename_event else []}}))
    return record


class MergeAppLandingsTest(unittest.TestCase):
    def test_only_a_merge_apps_rename_to_a_merged_title_is_a_landing(self):
        def landing(*events):
            return stats.merge_app_rename({"mergeRenames": {"nodes": list(events)}})

        self.assertEqual(landing({}, rename(10), rename(9, "tauceti-queue")),
                         {"merged_at": timestamp(9, 2), "merge_source": "tauceti-queue"})
        for rejected in (rename(9, kind="User"), rename(9, "other-app"),
                         rename(9, title="feat: x"), {}):
            self.assertIsNone(landing(rejected))
        self.assertIsNone(stats.merge_app_rename({}))

    def test_receipt_check_reads_every_page_and_keeps_the_first_receipt(self):
        receipt = stats.MERGE_RECEIPT + "Build succeeded"
        pages = [
            comments_page([comment(10, receipt, "tau-ceti-bors"),
                           comment(10, receipt, kind="User"),
                           comment(10, receipt, "other-app"),
                           comment(10, "Pull request rejected during staging repair.\n\n"),
                           comment(10, "Pull request successfully merged into staging.\n\n")],
                          previous_cursor="older"),
            comments_page([comment(9, receipt)]),
        ]
        with patch.object(stats, "graphql", side_effect=pages) as query:
            landing = stats.fetch_merge_receipt("o", "r", 42)
        self.assertEqual(landing, {"merged_at": timestamp(9, 3), "merge_source": "tauceti-queue"})
        self.assertEqual(query.call_args_list[1].kwargs["cursor"], "older")
        with patch.object(stats, "graphql", return_value=comments_page([comment(9, "Build failed")])):
            self.assertIsNone(stats.fetch_merge_receipt("o", "r", 42))

    def test_renames_count_without_requests_and_receipts_are_checked_newest_first(self):
        native = pr(1, 1, merged_day=8)
        native["merge_rename"] = None
        open_pr = pr(2, 1, state="OPEN")
        open_pr["merge_rename"] = None
        prs = [native, open_pr, closed(3, 6, rename_event=rename(6)),
               closed(4, 5), closed(5, 8), closed(6, 7)]
        receipts = {5: {"merged_at": timestamp(9, 3), "merge_source": "tauceti-queue"}, 6: None}
        with patch.object(stats, "fetch_merge_receipt",
                          side_effect=lambda owner, name, number: receipts[number]) as check:
            self.assertFalse(stats.apply_app_merges("o/r", prs, max_checks=2))
        self.assertEqual([call.args[2] for call in check.call_args_list], [5, 6])
        by_number = {record["number"]: record for record in prs}
        self.assertEqual(by_number[1]["merged_at"], timestamp(8, 12))
        self.assertNotIn("merge_source", by_number[1])
        self.assertIsNone(by_number[2]["merged_at"])
        self.assertEqual((by_number[3]["state"], by_number[3]["merged_at"], by_number[3]["merge_source"]),
                         ("MERGED", timestamp(6, 2), "tau-ceti-bors"))
        self.assertEqual((by_number[5]["state"], by_number[5]["merged_at"]), ("MERGED", timestamp(9, 3)))
        self.assertEqual((by_number[6]["state"], by_number[6]["merged_at"]), ("CLOSED", None))
        self.assertEqual({number for number, record in by_number.items() if "receipt_checked" in record},
                         {5, 6})
        self.assertNotIn("receipt_checked", by_number[4])  # Left for the next run.
        self.assertFalse(any("merge_rename" in record for record in prs))

    def test_receipt_verdicts_are_reused_until_updated_at_moves(self):
        found, absent, stale = closed(5, 8), closed(6, 7), closed(7, 7, updated_day=9)
        found.update(state="MERGED", merged_at=timestamp(9, 3), merge_source="tauceti-queue",
                     receipt_checked=found["updated_at"])
        absent["receipt_checked"] = absent["updated_at"]
        stale["receipt_checked"] = timestamp(7, 4)
        previous = {"prs": [found, absent, stale], "merge_evidence": stats.MERGE_EVIDENCE}
        prs = [closed(5, 8), closed(6, 7), closed(7, 7, updated_day=9)]
        with patch.object(stats, "fetch_merge_receipt", return_value=None) as check:
            stats.apply_app_merges("o/r", prs, previous)
        self.assertEqual([call.args[2] for call in check.call_args_list], [7])
        self.assertEqual((prs[0]["state"], prs[0]["merged_at"], prs[0]["merge_source"]),
                         ("MERGED", timestamp(9, 3), "tauceti-queue"))
        self.assertEqual((prs[1]["state"], prs[1]["receipt_checked"]), ("CLOSED", prs[1]["updated_at"]))
        self.assertEqual(prs[2]["receipt_checked"], timestamp(9, 4))

    def test_a_recorded_landing_survives_a_missed_rename_and_a_full_batch(self):
        # The page query drops #3's rename this run and #3 has moved since, while a newer
        # closure takes the only receipt check: the landing the last run recorded must stand.
        renamed = closed(3, 6, rename_event=rename(6))
        stats.apply_app_merges("o/r", [renamed])
        previous = {"prs": [renamed], "merge_evidence": stats.MERGE_EVIDENCE}
        prs = [closed(3, 6, updated_day=9), closed(5, 8)]
        with patch.object(stats, "fetch_merge_receipt", return_value=None) as check:
            complete = stats.apply_app_merges("o/r", prs, previous, max_checks=1)
        self.assertEqual([call.args[2] for call in check.call_args_list], [5])
        self.assertTrue(complete)
        self.assertEqual((prs[0]["state"], prs[0]["merged_at"], prs[0]["merge_source"]),
                         ("MERGED", timestamp(6, 2), "tau-ceti-bors"))
        # A snapshot written under another rule vouches for nothing.
        prs = [closed(3, 6, updated_day=9)]
        with patch.object(stats, "fetch_merge_receipt", return_value=None) as check:
            stats.apply_app_merges("o/r", prs, {**previous, "merge_evidence": {"rule": "old"}})
        self.assertEqual([call.args[2] for call in check.call_args_list], [3])
        self.assertEqual(prs[0]["state"], "CLOSED")

    def test_completeness_and_an_uncapped_check(self):
        def run(max_checks):
            prs = [closed(4, 5), closed(5, 8), closed(6, 7)]
            with patch.object(stats, "fetch_merge_receipt", return_value=None) as check:
                complete = stats.apply_app_merges("o/r", prs, max_checks=max_checks)
            return complete, check.call_count

        self.assertEqual(run(2), (False, 2))
        self.assertEqual(run(3), (True, 3))
        self.assertEqual(run(None), (True, 3))

    def test_the_cli_caps_receipt_checks_only_when_it_can_resume(self):
        # 501 closed, unrenamed PRs, where only the oldest closure has a receipt.
        def run(*extra):
            prs = [closed(number, 5) for number in range(1, 501)] + [closed(501, 2)]
            for record in prs:
                record.pop("merge_rename")
            landing = {"merged_at": timestamp(9, 3), "merge_source": "tauceti-queue"}
            with tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                argv = ["pr_stats_graphs.py", "--repo", "o/r", "--out-dir", str(folder), *extra]
                if extra:
                    argv[-1] = str(folder / argv[-1])
                with (patch.object(stats, "fetch_prs", return_value=prs),
                      patch.object(stats, "fetch_scoreboards", return_value=([], {}, {})),
                      patch.object(stats, "fetch_merge_receipt",
                                   side_effect=lambda owner, name, number:
                                   landing if number == 501 else None) as check,
                      patch("sys.argv", argv)):
                    self.assertEqual(stats.main(), 0)
                totals = json.loads((folder / "pr-stats.json").read_text())
                snapshot = json.loads((folder / "snapshot.json").read_text()) if extra else None
            return check.call_count, totals["merge_totals_by_contributor"], snapshot

        # Without --dump-data nothing resumes, so every candidate is checked now.
        self.assertEqual(run()[:2], (501, {"alice": 1}))
        # A run that saves its snapshot keeps the cap and leaves the oldest for the next run.
        checks, totals, snapshot = run("--dump-data", "snapshot.json")
        self.assertEqual((checks, totals), (500, {}))
        self.assertFalse(snapshot["merge_evidence_complete"])

    def test_a_landing_before_creation_is_an_error(self):
        early = closed(3, 6, rename_event={**rename(6), "createdAt": "2025-12-31T00:00:00Z"})
        with self.assertRaisesRegex(ValueError, "predates creation of PR #3"):
            stats.apply_app_merges("o/r", [early])

    def test_app_and_native_merges_count_once_in_all_histories(self):
        prs = [closed(1, 9, rename_event=rename(9)), pr(2, 1, merged_day=8),
               closed(3, 9), pr(4, 1, state="OPEN")]
        with patch.object(stats, "fetch_merge_receipt", return_value=None):
            stats.apply_app_merges("o/r", prs)
        with tempfile.TemporaryDirectory() as folder:
            metrics = stats.generate({"prs": prs, "fetched_at": timestamp(10),
                                      "scoreboards": []}, Path(folder))
        self.assertEqual(metrics["rolling_seven_day"][-1]["merges"], 2)
        self.assertEqual(metrics["merge_totals_by_contributor"], {"alice": 2})
        self.assertEqual(metrics["cumulative_merges_plotted"]["alice"][-1], 2)

    def test_unchanged_cached_timeline_still_gets_its_landing_from_the_page(self):
        old_pr = pr(42, 1, author="app-only-author")
        old_pr.update(closed_at=timestamp(9), updated_at=timestamp(9), labeled_events=[])
        previous = {"repo": "o/r", "prs": [old_pr]}
        page = {"repository": {"pullRequests": {
            "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{
                "number": 42, "createdAt": timestamp(1), "updatedAt": timestamp(9),
                "mergedAt": None, "closedAt": timestamp(9), "state": "CLOSED",
                "isDraft": False, "author": {"login": "app-only-author"},
                "labels": {"nodes": []}, "mergeRenames": {"nodes": [rename(9)]},
            }],
        }}}
        with (patch.object(stats, "graphql", return_value=page) as query,
              patch.object(stats, "fetch_scoreboards", return_value=([], {}, {}))):
            snapshot = stats.fetch_snapshot("o/r", previous)
        self.assertEqual(query.call_count, 1)  # Only the page: label history reused, no receipt check.
        self.assertEqual(snapshot["prs"][0]["merged_at"], timestamp(9, 2))
        self.assertEqual(snapshot["merge_evidence"], stats.MERGE_EVIDENCE)

    def test_an_app_landing_makes_its_author_a_trusted_reviewer(self):
        prs = [closed(42, 9, rename_event=rename(9), author="app-only-author")]
        old = {"repo": "o/r", "scoreboards_scanned_at": timestamp(10)}
        with (patch.object(stats, "fetch_prs", return_value=prs),
              patch.object(stats, "fetch_scoreboards", return_value=([], {}, {})) as scan):
            snapshot = stats.fetch_snapshot("o/r", old)
        # The walk state carries over: verdicts are derived each run, so new trust needs no rescan.
        scan.assert_called_once_with("o/r", {42}, {"app-only-author"} | stats.REVIEW_APP_LOGINS, old,
                                     pages=stats.SCOREBOARD_PAGES_PER_RUN)
        self.assertEqual(snapshot["prs"][0]["merge_source"], "tau-ceti-bors")
        self.assertTrue(snapshot["merge_evidence_complete"])


def forgejo_row(number, created_day, *, closed_day=None, title="feat: x", state=None,
                author="fj", merged=False, updated_hour=13):
    return {
        "number": number, "title": title,
        "state": state or ("closed" if closed_day else "open"),
        "created_at": timestamp(created_day),
        "updated_at": timestamp(closed_day or created_day, updated_hour),
        "closed_at": timestamp(closed_day, 12) if closed_day else None,
        "user": {"login": author}, "labels": [{"name": "self-tested"}],
        "pull_request": {"merged": merged,
                         "merged_at": timestamp(closed_day, 11) if merged else None},
    }


FORGEJO = "https://forge.example/o/r"
SOURCE = {"url": FORGEJO, "merge_logins": ["queue"]}
LANDED = stats.MERGED_TITLE_PREFIX + "feat: x"


def forgejo_comment(number, day, hour=12, *, login="queue", body=None):
    return {"id": number * 100 + hour, "user": {"login": login},
            "body": stats.MERGE_RECEIPT + "Build succeeded." if body is None else body,
            "created_at": timestamp(day, hour), "updated_at": timestamp(day, hour),
            "issue_url": f"{FORGEJO}/issues/{number}",
            "pull_request_url": f"{FORGEJO}/pulls/{number}"}


class FakeForgejo:
    """Forgejo's listings, paged by offset, with every request recorded."""

    def __init__(self, pulls, comments=(), per_pull=None, page_size=2):
        self.pulls, self.comments = list(pulls), list(comments)
        self.per_pull = per_pull or {}
        self.page_size, self.urls = page_size, []

    def __call__(self, url):
        self.urls.append(url)
        path, _, query = url.partition("?")
        options = dict(item.split("=", 1) for item in query.split("&") if item)
        match = re.search(r"/issues/(\d+)/comments$", path)
        if match:
            # Forgejo's per-issue comment endpoint is unpaginated: every request is the whole history.
            return list(self.per_pull.get(int(match.group(1)), []))
        page = int(options["page"])
        if path.endswith("/issues/comments"):
            rows = self.comments
        else:
            rows = self.pulls
        return rows[(page - 1) * self.page_size:page * self.page_size]


class ForgejoLandingsTest(unittest.TestCase):
    def walk(self, fake, previous=None, source=SOURCE):
        with patch.object(stats, "forgejo_get", fake):
            return stats.fetch_forgejo_prs(source, previous)

    def test_only_a_merge_logins_receipt_is_a_landing(self):
        fake = FakeForgejo(
            [forgejo_row(1, 2, closed_day=5, title=LANDED), forgejo_row(2, 2, closed_day=5),
             forgejo_row(3, 2, closed_day=5, title=LANDED), forgejo_row(4, 2, closed_day=5, merged=True),
             forgejo_row(5, 2, closed_day=5)],
            [forgejo_comment(1, 5, 11), forgejo_comment(1, 5, 12),
             forgejo_comment(2, 5, login="someone"), forgejo_comment(5, 5, body="LGTM")])
        walk = self.walk(fake)
        landed = {pr["number"]: pr["merged_at"] for pr in walk["prs"] if pr["merged_at"]}
        # 1 by its first receipt; 3's title alone and 2's receipt by another login are not
        # landings; 4 is Forgejo's own merge.
        self.assertEqual(landed, {1: timestamp(5, 11), 4: timestamp(5, 11)})
        self.assertEqual(walk["receipts"], {"1": timestamp(5, 11)})
        self.assertEqual(next(pr for pr in walk["prs"] if pr["number"] == 1)["state"], "MERGED")
        urls = [url for url in fake.urls if "/issues?" in url]
        self.assertTrue(all("type=pulls" in url and "state=all" in url for url in urls))
        self.assertNotIn("since=", urls[0])

    def test_the_walk_reads_to_an_empty_page_and_skips_issues(self):
        fake = FakeForgejo([forgejo_row(1, 2), {**forgejo_row(2, 2), "pull_request": None},
                            forgejo_row(3, 2)])
        walk = self.walk(fake)
        self.assertEqual([pr["number"] for pr in walk["prs"]], [1, 3])
        pages = [re.search(r"page=(\d+)", url).group(1) for url in fake.urls if "/issues?" in url]
        self.assertEqual(pages, ["1", "2", "3"])

    def test_a_merged_title_without_a_receipt_reads_its_own_comments_once(self):
        row = forgejo_row(3, 2, closed_day=5, title=LANDED)
        fake = FakeForgejo([row], per_pull={3: [forgejo_comment(3, 5)]})
        self.assertEqual(self.walk(fake)["receipts"], {"3": timestamp(5, 12)})
        unproven = FakeForgejo([row])
        first = self.walk(unproven)
        self.assertEqual(sum("/issues/3/comments" in url for url in unproven.urls), 1)
        self.assertIsNone(first["prs"][0]["merged_at"])
        for previous in (first, {**first, "full_walk_at": timestamp(1)}):
            again = FakeForgejo([row])  # Listed again, unchanged: in the overlap, or a full walk.
            self.walk(again, previous)
            self.assertFalse(any("/issues/3/comments" in url for url in again.urls))
        touched = FakeForgejo([forgejo_row(3, 2, closed_day=5, title=LANDED, updated_hour=14)])
        self.walk(touched, first)
        self.assertEqual(sum("/issues/3/comments" in url for url in touched.urls), 1)

    def test_a_later_walk_reads_only_what_changed_since_the_last_one_began(self):
        first = self.walk(FakeForgejo([forgejo_row(1, 2), forgejo_row(2, 2)]))
        first["walk_started_at"] = timestamp(5, 12)
        fake = FakeForgejo([forgejo_row(2, 2, closed_day=6, title=LANDED)], [forgejo_comment(2, 6)])
        walk = self.walk(fake, first)
        self.assertTrue(all("since=2026-01-05T11%3A50%3A00Z" in url
                            for url in fake.urls if "/comments?" in url or "/issues?" in url))
        self.assertEqual([(pr["number"], pr["merged_at"]) for pr in walk["prs"]],
                         [(1, None), (2, timestamp(6, 12))])
        self.assertEqual(walk["full_walk_at"], first["full_walk_at"])

    def test_a_landing_survives_a_later_edit_and_a_full_walk(self):
        first = self.walk(FakeForgejo([forgejo_row(1, 2, closed_day=5, title=LANDED)],
                                      [forgejo_comment(1, 5)]))
        edited = FakeForgejo([forgejo_row(1, 2, closed_day=7, title="feat: x")])
        self.assertEqual(self.walk(edited, first)["prs"][0]["merged_at"], timestamp(5, 12))
        stale = {**first, "full_walk_at": timestamp(1)}
        full = FakeForgejo([forgejo_row(1, 2, closed_day=7, title="feat: x")])
        walk = self.walk(full, stale)
        self.assertTrue(all("since=" not in url for url in full.urls))
        self.assertEqual(walk["prs"][0]["merged_at"], timestamp(5, 12))

    def test_a_weekly_full_walk_drops_what_a_shifted_page_or_deletion_left_behind(self):
        first = self.walk(FakeForgejo([forgejo_row(1, 2), forgejo_row(9, 2)]))
        stale = {**first, "full_walk_at": stats.iso_z(datetime.now(timezone.utc) - timedelta(days=8))}
        walk = self.walk(FakeForgejo([forgejo_row(1, 2)]), stale)
        self.assertEqual([pr["number"] for pr in walk["prs"]], [1])
        recent = {**first, "full_walk_at": stats.iso_z(datetime.now(timezone.utc) - timedelta(days=1))}
        self.assertEqual([pr["number"] for pr in self.walk(FakeForgejo([]), recent)["prs"]], [1, 9])

    def test_another_repository_or_login_set_starts_over(self):
        first = self.walk(FakeForgejo([forgejo_row(1, 2, closed_day=5, title=LANDED)],
                                      [forgejo_comment(1, 5)]))
        for source in ({"url": "https://forge.example/o/other", "merge_logins": ["queue"]},
                       {"url": FORGEJO, "merge_logins": ["other"]}):
            fake = FakeForgejo([])
            walk = self.walk(fake, first, source)
            self.assertTrue(all("since=" not in url for url in fake.urls))
            self.assertEqual(walk["receipts"], {})

    def test_the_snapshot_carries_forgejo_and_the_next_run_resumes_from_it(self):
        previous = {"repo": "o/r", "forgejo": {"repo": FORGEJO}}
        with (patch.object(stats, "fetch_forgejo_prs", return_value={"repo": FORGEJO}) as walk,
              patch.object(stats, "fetch_prs", return_value=[]),
              patch.object(stats, "fetch_scoreboards", return_value=([], {}, {}))):
            snapshot = stats.fetch_snapshot("o/r", previous, forgejo_repo=SOURCE)
            walk.assert_called_once_with(SOURCE, {"repo": FORGEJO})
            self.assertEqual(snapshot["forgejo"], {"repo": FORGEJO})
            self.assertNotIn("forgejo", stats.fetch_snapshot("o/r", previous))

    def test_a_forgejo_outage_fails_before_the_github_walk(self):
        with (patch.object(stats, "fetch_forgejo_prs", side_effect=RuntimeError("outage")),
              patch.object(stats, "fetch_prs") as github):
            with self.assertRaisesRegex(RuntimeError, "outage"):
                stats.fetch_snapshot("o/r", None, forgejo_repo=SOURCE)
        github.assert_not_called()

    def test_forgejo_landings_and_authors_join_the_seven_day_series_only(self):
        github = [pr(1, 1, merged_day=8), pr(2, 4, state="OPEN", author="bob")]
        # Forgejo numbers overlap GitHub's; number 1 here is a different pull request.
        forgejo = self.walk(FakeForgejo(
            [forgejo_row(1, 3, closed_day=9, title=LANDED), forgejo_row(2, 4, author="carol")],
            [forgejo_comment(1, 9)]))
        data = {"repo": "o/r", "prs": github, "fetched_at": timestamp(10), "scoreboards": [],
                "forgejo": forgejo}
        with tempfile.TemporaryDirectory() as folder:
            metrics = stats.generate(data, Path(folder))
            without = stats.generate({**data, "forgejo": None}, Path(folder))
        last = metrics["rolling_seven_day"][-1]
        self.assertEqual((last["merges"], last["active_authors"]), (2, 3))
        self.assertEqual(last["median_merge_hours"], (180 + 156) / 2)
        self.assertEqual(metrics["forgejo_repo"], FORGEJO)
        for key in ("review_cycles", "by_roadmap_and_contributor",
                    "merge_totals_by_contributor", "cumulative_merges_plotted"):
            self.assertEqual(metrics[key], without[key])
        self.assertEqual(without["rolling_seven_day"][-1]["merges"], 1)
        # The open Forgejo pull request's age is published beside GitHub's, not mixed into it.
        forgejo_ages = metrics["queue"].pop("forgejo_open_hours")
        open_forgejo = next(p for p in forgejo["prs"] if p["state"] == "OPEN")
        self.assertEqual(forgejo_ages, [
            (stats.parse_dt(timestamp(10)) - stats.parse_dt(open_forgejo["created_at"])).total_seconds() / 3600])
        self.assertEqual(without["queue"].pop("forgejo_open_hours"), [])
        self.assertEqual(metrics["queue"], without["queue"])

    def test_the_command_line_pairs_the_project_with_its_forgejo_repository(self):
        seen = []

        def snapshot(repo, previous, **options):
            seen.append(options["forgejo_repo"])
            return {"repo": repo, "prs": [pr(1, 1, merged_day=8)], "fetched_at": timestamp(10),
                    "scoreboards": []}

        runs = ([], ["--forgejo-repo", ""],
                ["--forgejo-repo", FORGEJO, "--forgejo-merge-login", "queue"],
                ["--repo", "o/r"])
        with tempfile.TemporaryDirectory() as folder, patch.object(stats, "fetch_snapshot", snapshot):
            for extra in runs:
                with patch("sys.argv", ["pr_stats_graphs.py", "--out-dir", folder, *extra]):
                    self.assertEqual(stats.main(), 0)
            with patch("sys.argv", ["pr_stats_graphs.py", "--out-dir", folder,
                                    "--forgejo-repo", FORGEJO]):
                with self.assertRaises(SystemExit):
                    stats.main()
        self.assertEqual(seen, [stats.FORGEJO_REPOS["TauCetiProject/TauCeti"], None, SOURCE, None])


class RoadmapMatrixTest(unittest.TestCase):
    """Who merged and who reviewed, per roadmap, over the trailing window."""

    def matrix(self, prs, boards, **kw):
        return stats.roadmap_matrix(prs, boards, date(2026, 1, 31), **kw)

    def labelled(self, number, author, area, day, state="MERGED"):
        return {
            "number": number, "author": author,
            "labels": [f"roadmap/{area}"] if area else ["roadmap/none"],
            "created_at": f"2026-01-{day:02d}T00:00:00Z",
            "merged_at": f"2026-01-{day:02d}T12:00:00Z" if state == "MERGED" else None,
            "closed_at": None, "state": state, "is_draft": False, "events": [],
        }

    def test_it_counts_merges_and_reviews_per_roadmap(self):
        prs = [self.labelled(1, "alice", "PDE", 10),
               self.labelled(2, "alice", "PDE", 11),
               self.labelled(3, "bob", "Topology", 12)]
        boards = [{"pr": 1, "user": "carol", "created_at": "2026-01-10T13:00:00Z"},
                  {"pr": 3, "user": "carol", "created_at": "2026-01-12T13:00:00Z"}]

        matrix = self.matrix(prs, boards)

        self.assertEqual(matrix["merges"]["counts"]["alice\troadmap/PDE"], 2)
        self.assertEqual(matrix["merges"]["counts"]["bob\troadmap/Topology"], 1)
        self.assertEqual(matrix["reviews"]["counts"]["carol\troadmap/PDE"], 1)
        self.assertEqual(matrix["reviews"]["counts"]["carol\troadmap/Topology"], 1)

    def test_a_pr_with_two_area_labels_is_counted_for_neither(self):
        """Splitting it would put a made-up number in a chart about who works where."""
        pr = self.labelled(1, "alice", "PDE", 10)
        pr["labels"] = ["roadmap/PDE", "roadmap/Topology"]

        self.assertEqual(self.matrix([pr], [])["merges"]["counts"], {})

    def test_maintenance_titles_still_count_here(self):
        """Unlike the lines-per-roadmap chart: somebody who keeps the PDE build honest is a
        person that roadmap depends on, which is the question this one is asking."""
        pr = self.labelled(1, "alice", "PDE", 10)

        self.assertEqual(self.matrix([pr], [])["merges"]["counts"]["alice\troadmap/PDE"], 1)

    def test_work_outside_the_window_is_left_out(self):
        old = self.labelled(1, "alice", "PDE", 10)
        old["merged_at"] = "2025-01-10T12:00:00Z"

        self.assertEqual(self.matrix([old], [])["merges"]["counts"], {})

    def test_roadmaps_past_the_limit_become_one_column(self):
        prs = []
        for index in range(stats.ROADMAP_LIMIT + 4):
            for copy in range(stats.ROADMAP_LIMIT + 4 - index):
                prs.append(self.labelled(len(prs) + 1, "alice", f"Area{index:02d}", 10))

        matrix = self.matrix(prs, [])

        self.assertEqual(len(matrix["columns"]), stats.ROADMAP_LIMIT)
        self.assertEqual(len(matrix["bundled"]), 4)
        self.assertEqual(matrix["merges"]["axis"][-1], stats.OTHER_ROADMAP)
        # Nothing is lost by bundling.
        self.assertEqual(sum(matrix["merges"]["counts"].values()), len(prs))

    def test_contributors_past_the_limit_become_one_row(self):
        prs = [self.labelled(index + 1, f"person-{index:02d}", "PDE", 10)
               for index in range(stats.ROADMAP_CONTRIBUTOR_LIMIT + 6)]

        rows = self.matrix(prs, [])["merges"]

        self.assertEqual(rows["omitted_contributors"], 6)
        self.assertEqual(rows["rows"][-1], stats.OTHER_CONTRIBUTOR)
        self.assertEqual(rows["counts"][f"{stats.OTHER_CONTRIBUTOR}\troadmap/PDE"], 6)
        self.assertEqual(sum(rows["counts"].values()), len(prs))

    def test_both_charts_share_the_same_columns(self):
        """Reviews follow the work rather than defining their own areas, so the two grids can
        be read against each other."""
        prs = [self.labelled(1, "alice", "PDE", 10)]
        boards = [{"pr": 1, "user": "carol", "created_at": "2026-01-10T13:00:00Z"}]

        matrix = self.matrix(prs, boards)

        self.assertEqual(matrix["merges"]["axis"], matrix["reviews"]["axis"])


class CategoryGroupingTest(unittest.TestCase):
    """With a TauCetiRoadmap checkout, the who-works-where grids count each PR under the arXiv
    category of the roadmap it advances, as declared in that roadmap's metadata.toml."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for base, name, meta in (("TauCetiRoadmap", "Primes", 'topic = "math.NT"\n'),
                                 ("TauCetiRoadmap", "Forms", 'topic = "math.NT"\n'),
                                 ("TauCetiRoadmap", "Curves", 'topic = "math.AG"\n'),
                                 ("TauCetiRoadmap", "Bare", None),
                                 ("TauCetiRoadmap", "Odd", 'topic = "<script>"\n'),
                                 ("TauCetiRoadmap", "Broken", "topic =\n"),
                                 ("Completed", "Finished", 'topic = "math.CO"\n'),
                                 ("Completed", "Curves", 'topic = "math.NT"\n'),
                                 # Active with no usable category, archived with one: still unsorted.
                                 ("TauCetiRoadmap", "Revived", None),
                                 ("Completed", "Revived", 'topic = "math.CO"\n'),
                                 ("TauCetiRoadmap", "Rewritten", "topic = \"math.nt\"\n"),
                                 ("Completed", "Rewritten", 'topic = "math.CO"\n'),
                                 ("TauCetiRoadmap", "Garbled", "topic =\n"),
                                 ("Completed", "Garbled", 'topic = "math.CO"\n')):
            d = root / base / name
            d.mkdir(parents=True)
            (d / "README.md").write_text(f"# {name}\n")
            if meta is not None:
                (d / "metadata.toml").write_text(meta)
        # Not a roadmap (no README): its metadata is never read.
        (root / "TauCetiRoadmap" / "references").mkdir()
        (root / "TauCetiRoadmap" / "references" / "metadata.toml").write_text('topic = "math.GT"\n')
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def labelled(self, number, author, area):
        return {
            "number": number, "author": author, "labels": [f"roadmap/{area}"],
            "created_at": "2026-01-10T00:00:00Z", "merged_at": "2026-01-10T12:00:00Z",
            "closed_at": None, "state": "MERGED", "is_draft": False, "events": [],
        }

    def test_categories_come_from_each_roadmap_s_metadata(self):
        self.assertEqual(stats.roadmap_categories(self.root), {
            "roadmap/Primes": "math.NT", "roadmap/Forms": "math.NT", "roadmap/Curves": "math.AG",
            "roadmap/Finished": "math.CO",
        })
        # The active roadmap decides: its archived namesake's category never stands in for a
        # missing, invalid or unreadable one of its own (review on TauCetiProject/TauCeti#10413).
        for name in ("Revived", "Rewritten", "Garbled"):
            self.assertNotIn(f"roadmap/{name}", stats.roadmap_categories(self.root))

    def test_no_checkout_means_no_categories(self):
        self.assertEqual(stats.roadmap_categories(self.root / "missing"), {})

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0,
                     "needs POSIX permissions, which root bypasses")
    def test_an_unreadable_checkout_raises_rather_than_reading_as_empty(self):
        """An empty map would publish every PR as unsorted; failing keeps the committed charts."""
        active = self.root / "TauCetiRoadmap"
        active.chmod(0o300)  # searchable, not listable
        try:
            with self.assertRaises(OSError):
                stats.roadmap_categories(self.root)
        finally:
            active.chmod(0o755)

    def test_prs_count_under_their_roadmap_s_category(self):
        prs = [self.labelled(1, "alice", "Primes"), self.labelled(2, "alice", "Forms"),
               self.labelled(3, "bob", "Curves"), self.labelled(4, "bob", "Bare")]
        boards = [{"pr": 2, "user": "carol", "created_at": "2026-01-10T13:00:00Z"}]
        matrix = stats.roadmap_matrix(prs, boards, date(2026, 1, 31),
                                      column_of=stats.roadmap_categories(self.root))
        self.assertEqual(matrix["grouping"], "category")
        self.assertEqual(matrix["merges"]["counts"]["alice\tmath.NT"], 2)
        self.assertEqual(matrix["merges"]["counts"]["bob\tmath.AG"], 1)
        self.assertEqual(matrix["merges"]["counts"][f"bob\t{stats.UNSORTED_CATEGORY}"], 1)
        self.assertEqual(matrix["reviews"]["counts"]["carol\tmath.NT"], 1)
        self.assertEqual(matrix["columns"][0], "math.NT")
        self.assertIn({"contributor": "alice", "category": "math.NT", "count": 2},
                      matrix["exact"]["merges"])

    def test_the_grid_is_labelled_by_category(self):
        prs = [self.labelled(1, "alice", "Primes"), self.labelled(2, "bob", "Bare")]
        matrix = stats.roadmap_matrix(prs, [], date(2026, 1, 31),
                                      column_of=stats.roadmap_categories(self.root))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "heat.svg"
            stats.render_roadmap_heatmap(path, "Grid", "merged PRs", matrix, "merges")
            svg = path.read_text(encoding="utf-8")
        ET.fromstring(svg)
        self.assertIn(">Number Theory</text>", svg)  # the name, never the code
        self.assertNotIn(">math.NT</text>", svg)
        self.assertIn(">Unsorted</text>", svg)
        self.assertNotIn(stats.UNSORTED_CATEGORY, svg)
        self.assertIn("per contributor per arXiv category", svg)

    def test_subtitles_and_headings_fit_the_card(self):
        """Each subtitle line is one unwrapped <text>. Its right edge is estimated at 0.5 em per
        character; Chromium measures these lines in the site's font stack at 0.46 em, and the first
        category wording of this chart (168 characters) overflowed by 87-96 units at every width.
        The column headings are category names up to 27 characters, rotated 45 degrees: the last
        one must end inside the card too (estimated at HEADING_ASPECT, the chart's own allowance).
        Vertically, each header line clears the one above by at least 0.15 of the subtitle's size. A
        wide grid scales the type up, and with the old fixed baselines the two subtitle lines touched
        at 1,939 units (review on TauCetiProject/TauCeti#10413)."""
        def subtitles_fit(matrix):
            for key in ("merges", "reviews"):
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "heat.svg"
                    stats.render_roadmap_heatmap(path, "Merged PRs by arXiv category and contributor",
                                                 "merged PRs", matrix, key)
                    root = ET.parse(path).getroot()
                width = float(root.attrib["viewBox"].split()[2])
                em = 13 * width / chart_style.REFERENCE_WIDTH
                heading_em = 12 * width / chart_style.REFERENCE_WIDTH
                for text in root.iter("{http://www.w3.org/2000/svg}text"):
                    if text.attrib.get("class") == "subtitle":
                        right = float(text.attrib["x"]) + len(text.text) * em * 0.5
                        self.assertLessEqual(right, width, f"{len(text.text)} characters: {text.text}")
                    if text.attrib.get("class") == "collab":
                        right = float(text.attrib["x"]) + len(text.text) * heading_em * stats.HEADING_ASPECT / 1.414
                        self.assertLessEqual(right, width, f"heading {text.text!r}")
                gaps = header_gaps(root)
                self.assertEqual(len(gaps), 2)
                for gap in gaps:
                    self.assertGreaterEqual(gap, 0.15, f"header lines crowd at width {width:g}")

        by_length = sorted(stats.ARXIV_MATH, key=lambda code: (-len(stats.ARXIV_MATH[code]), code))
        for n in (1, 2, 17, 20):
            codes = by_length[:n]
            # Columns are ordered by merges, so give the longest name the fewest: its heading is
            # then the last one, the one that runs toward the card's right edge.
            prs, column_of = [], {}
            for i, code in enumerate(codes):
                column_of[f"roadmap/Area{i:02d}"] = code
                for _ in range(1 if i == 0 else 2):
                    prs.append(self.labelled(len(prs) + 1, f"person-{i:02d}", f"Area{i:02d}"))
            matrix = stats.roadmap_matrix(prs, [], date(2026, 1, 31), column_of=column_of,
                                          roadmap_limit=stats.CATEGORY_LIMIT)
            self.assertEqual(matrix["merges"]["axis"][-1], codes[0])
            subtitles_fit(matrix)
        prs = [self.labelled(i + 1, "alice", f"Area{i:02d}") for i in range(15)]
        subtitles_fit(stats.roadmap_matrix(prs, [], date(2026, 1, 31)))

    def test_generate_groups_by_category_given_a_checkout(self):
        data = {"repo": "TauCetiProject/TauCeti", "fetched_at": "2026-01-31T23:00:00Z",
                "prs": [self.labelled(1, "alice", "Primes")], "scoreboards": []}
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            metrics = stats.generate(data, out, history_days=30, roadmap_dir=self.root)
            svg = (out / "merges-by-roadmap-and-contributor.svg").read_text(encoding="utf-8")
        self.assertEqual(metrics["by_roadmap_and_contributor"]["grouping"], "category")
        self.assertEqual(metrics["by_roadmap_and_contributor"]["roadmap_categories"]["roadmap/Primes"], "math.NT")
        self.assertIn("Merged PRs by arXiv category and contributor", svg)


class HeatBucketTest(unittest.TestCase):
    def test_the_top_bucket_covers_a_range(self):
        # Spacing the edges across the closed interval would land the last one exactly on the
        # maximum, spending a whole ramp step on the single busiest cell.
        edges = stats.heat_buckets(105)

        self.assertLess(edges[-1], 105)
        self.assertEqual(stats.heat_step(105, edges), len(edges) - 1)
        self.assertEqual(stats.heat_step(1, edges), 0)

    def test_edges_are_strictly_increasing(self):
        for maximum in (1, 2, 3, 5, 40, 105, 5000):
            edges = stats.heat_buckets(maximum)
            self.assertEqual(edges, sorted(set(edges)), maximum)
            self.assertLessEqual(len(edges), len(stats.ROADMAP_RAMP))

    def test_every_ramp_step_has_an_ink(self):
        self.assertEqual(len(stats.ROADMAP_INK), len(stats.ROADMAP_RAMP))

    def test_a_long_name_is_elided_in_the_middle(self):
        """Two roadmaps sharing a long prefix must stay distinguishable in the headings."""
        prs = [
            {"number": 1, "author": "alice", "labels": ["roadmap/AlgebraicNumberTheory"],
             "created_at": "2026-01-10T00:00:00Z", "merged_at": "2026-01-10T12:00:00Z",
             "closed_at": None, "state": "MERGED", "is_draft": False, "events": []},
            {"number": 2, "author": "bob", "labels": ["roadmap/AlgebraicTopologySeminar"],
             "created_at": "2026-01-10T00:00:00Z", "merged_at": "2026-01-10T12:00:00Z",
             "closed_at": None, "state": "MERGED", "is_draft": False, "events": []},
        ]
        matrix = stats.roadmap_matrix(prs, [], date(2026, 1, 31))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "heat.svg"
            stats.render_roadmap_heatmap(path, "Grid", "merged PRs", matrix, "merges")
            svg = path.read_text(encoding="utf-8")
        headings = re.findall(r'class="collab"[^>]*>([^<]+)<', svg)
        self.assertEqual(len(set(headings)), len(headings), headings)

    def test_a_forbidden_control_character_is_stripped(self):
        self.assertNotIn("\x00", stats.XML_FORBIDDEN.sub("", "PD\x00E"))
        self.assertEqual(stats.XML_FORBIDDEN.sub("", "Number&Theory"), "Number&Theory")

    def test_the_ramp_gets_lighter_all_the_way_up(self):
        """The one property a sequential scale actually needs. The categorical CVD validator
        does not apply to a ramp and would fail this by construction."""
        def luminance(colour):
            def channel(value):
                value /= 255
                return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            raw = colour.lstrip("#")
            red, green, blue = (int(raw[index:index + 2], 16) for index in (0, 2, 4))
            return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)

        levels = [luminance(step) for step in stats.ROADMAP_RAMP]
        self.assertEqual(levels, sorted(levels))

        def contrast(one, two):
            high, low = max(luminance(one), luminance(two)), min(luminance(one), luminance(two))
            return (high + 0.05) / (low + 0.05)

        for step, ink in zip(stats.ROADMAP_RAMP, stats.ROADMAP_INK):
            self.assertGreaterEqual(contrast(step, ink), 4.0, step)


class HeatmapRenderTest(unittest.TestCase):
    """Degenerate inputs must produce a valid SVG rather than a crash or unparsable XML."""

    def matrix_for(self, prs, boards=()):
        return stats.roadmap_matrix(prs, list(boards), date(2026, 1, 31))

    def merged(self, number, author, area):
        return {
            "number": number, "author": author,
            "labels": [f"roadmap/{area}"] if area else ["roadmap/none"],
            "created_at": "2026-01-10T00:00:00Z", "merged_at": "2026-01-10T12:00:00Z",
            "closed_at": None, "state": "MERGED", "is_draft": False, "events": [],
        }

    def render(self, matrix, key="merges"):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "heat.svg"
            stats.render_roadmap_heatmap(path, "Grid", "merged PRs", matrix, key)
            svg = path.read_text(encoding="utf-8")
        ET.fromstring(svg)  # raises if the sentinels or a label produced invalid XML
        return svg

    def test_nothing_labelled_still_renders(self):
        self.render(self.matrix_for([self.merged(1, "alice", None)]))

    def test_a_single_cell_renders(self):
        svg = self.render(self.matrix_for([self.merged(1, "alice", "PDE")]))
        self.assertIn("alice", svg)
        self.assertIn("PDE", svg)

    def test_a_login_needing_escaping_is_escaped(self):
        svg = self.render(self.matrix_for([self.merged(1, "a&b", "PDE")]))
        self.assertIn("a&amp;b", svg)
        self.assertNotIn(">a&b<", svg)

    def test_a_long_roadmap_name_is_clipped_out_of_the_header(self):
        svg = self.render(self.matrix_for([self.merged(1, "alice", "A" * 60)]))
        self.assertNotIn("A" * 40, svg)
        self.assertIn("…", svg)

    def headings_clear_the_subtitle(self, svg):
        """Recompute how far each rotated heading reaches and compare with the subtitle.

        A heading is drawn at `y` and rotated -45 degrees about that point, so it runs up and
        to the right by its own rendered length over root two. The first render against real
        roadmap names put `RepresentationTheory` straight through the subtitle, because the
        band was a fixed 232 units and `css_px` scales the 12 to about 18 at this width.
        """
        root = ET.fromstring(svg)
        width = int(root.attrib["viewBox"].split()[2])
        font = 12 * width / stats.REFERENCE_WIDTH
        worst = None
        for node in root.iter("{http://www.w3.org/2000/svg}text"):
            if node.attrib.get("class") != "collab":
                continue
            reach = float(node.attrib["y"]) - (
                len(node.text or "") * font * stats.HEADING_ASPECT / 1.414)
            worst = reach if worst is None else min(worst, reach)
        return worst

    def test_long_headings_do_not_run_into_the_subtitle(self):
        prs = [self.merged(index + 1, f"person-{index:02d}", name) for index, name in enumerate(
            ["RepresentationTheory", "OneParameterSemigroups", "StandardDistributions",
             "GeometricTopology", "ConformalMapping"])]
        svg = self.render(self.matrix_for(prs))

        worst = self.headings_clear_the_subtitle(svg)

        self.assertIsNotNone(worst)
        lowest = max(float(node.attrib["y"]) for node in ET.fromstring(svg).iter(
            "{http://www.w3.org/2000/svg}text") if node.attrib.get("class") == "subtitle")
        self.assertGreater(worst, lowest, "a heading crosses the subtitle")

    def test_short_headings_do_not_pay_for_the_long_ones(self):
        """The band is sized from the labels, so a grid of short names stays compact."""
        def height_for(name):
            prs = [self.merged(1, "alice", name)]
            svg = self.render(self.matrix_for(prs))
            return int(ET.fromstring(svg).attrib["viewBox"].split()[3])

        self.assertLess(height_for("PDE"), height_for("RepresentationTheory"))

    def test_neither_sentinel_reaches_the_output(self):
        prs = [self.merged(index + 1, f"person-{index:02d}", "PDE")
               for index in range(stats.ROADMAP_CONTRIBUTOR_LIMIT + 3)]
        svg = self.render(self.matrix_for(prs))
        self.assertNotIn(stats.OTHER_CONTRIBUTOR, svg)
        self.assertNotIn(stats.OTHER_ROADMAP, svg)


class HeaderSpacingTest(unittest.TestCase):
    def test_cards_up_to_1500_wide_keep_the_design_positions(self):
        self.assertEqual(stats.subtitle_baselines(chart_style.REFERENCE_WIDTH, 2), [72, 94])
        for width in (1250, 1500):
            self.assertEqual(stats.subtitle_baselines(width, 1), [72])

    def test_the_queue_age_header_clears_its_panels(self):
        """The one two-line header besides the grids, on a 1,710-unit card: its lines are spread
        apart, and the panel headings beneath stay where they were, so they must still clear it."""
        metrics = {"total_open_hours": [5, 30, 200], "awaiting_author_hours": [3, 50],
                   "in_review_hours": [7], "other_open_prs": 12, "missing_transition_fallbacks": 3}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.svg"
            stats.render_queue_age(path, metrics, datetime(2026, 1, 31, 12, tzinfo=timezone.utc))
            root = ET.parse(path).getroot()
        gaps = header_gaps(root)
        self.assertEqual(len(gaps), 2)
        for gap in gaps:
            self.assertGreaterEqual(gap, 0.15)
        scale = float(root.attrib["viewBox"].split()[2]) / chart_style.REFERENCE_WIDTH
        texts = list(root.iter("{http://www.w3.org/2000/svg}text"))
        lowest = max(float(t.attrib["y"]) for t in texts if t.attrib.get("class") == "subtitle")
        panel = min(float(t.attrib["y"]) for t in texts if t.attrib.get("class") == "panel")
        clear = (panel - ASCENT * 15 * scale) - (lowest + DESCENT * chart_style.SUBTITLE_SIZE * scale)
        self.assertGreaterEqual(clear / (chart_style.SUBTITLE_SIZE * scale), 0.15)


class RenderingTest(unittest.TestCase):
    def test_generate_writes_every_declared_svg(self):
        prs = [
            pr(1, 1, merged_day=2, author="alice", cycles=1),
            pr(2, 2, merged_day=5, author="bob", cycles=2),
            pr(3, 3, merged_day=9, author="alice", cycles=7),
            pr(4, 10, state="OPEN", labels=("awaiting-author",), author="carol"),
            pr(5, 11, state="OPEN", labels=("review-in-progress",), cycles=2,
               author="dave"),
            pr(6, 12, state="OPEN", labels=("awaiting-CI",), author="erin"),
            pr(7, 14, merged_day=15, author="frank", cycles=1),
        ]
        future_merge = pr(8, 14, author="future-author", cycles=1)
        future_merge.update({
            "merged_at": timestamp(15, 21), "closed_at": timestamp(15, 21),
            "state": "MERGED",
        })
        prs.append(future_merge)
        data = {
            "schema_version": 1,
            "repo": "example/project",
            "fetched_at": timestamp(15, 20),
            "prs": prs,
            "scoreboards": [
                {"pr": 1, "created_at": timestamp(2), "updated_at": timestamp(2),
                 "user": "reviewer-a"},
                {"pr": 2, "created_at": timestamp(5), "updated_at": timestamp(5),
                 "user": "reviewer-b"},
                {"pr": 7, "created_at": timestamp(15, 12),
                 "updated_at": timestamp(15, 12), "user": "reviewer-c"},
                {"pr": 8, "created_at": timestamp(15, 21),
                 "updated_at": timestamp(15, 21), "user": "future-reviewer"},
            ],
        }
        # Derived from ASSET_NAMES rather than restated, so adding a chart cannot leave this
        # test quietly checking the old set.
        expected = [name for name in stats.ASSET_NAMES if name.endswith(".svg")]
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            metrics = stats.generate(data, out, contributor_limit=2, history_days=30)
            for name in expected:
                root = ET.parse(out / name).getroot()
                svg = (out / name).read_text(encoding="utf-8")
                card = root.find("{http://www.w3.org/2000/svg}rect")
                self.assertIsNotNone(card)
                self.assertEqual(card.attrib["fill"], chart_style.BG)
                self.assertEqual(card.attrib["stroke"], chart_style.PANEL)
                self.assertGreater(float(card.attrib["rx"]), 0)
                width = int(root.attrib["viewBox"].split()[2])
                self.assertIn(chart_style.base_css(width), svg)
            self.assertTrue((out / "pr-stats.json").is_file())
            queue_svg = (out / "pr-queue-age.svg").read_text(encoding="utf-8")
            self.assertLess(queue_svg.index("Total time open"),
                            queue_svg.index("Awaiting author"))
            self.assertLess(queue_svg.index("Awaiting author"),
                            queue_svg.index("In review"))
            cycle_svg = (out / "review-cycles-reached.svg").read_text(encoding="utf-8")
            self.assertIn("Review cycle 7", cycle_svg)
            review_svg = (
                out / "cumulative-reviews-by-contributor.svg"
            ).read_text(encoding="utf-8")
            self.assertIn("Reviews by contributor", review_svg)
            self.assertNotIn("Trusted v1 review scoreboards", review_svg)
            self.assertEqual(metrics["review_cycles"]["max_cycle"], 7)
            self.assertEqual(metrics["merge_totals_by_contributor"]["alice"], 2)
            self.assertEqual(metrics["review_totals_by_contributor"]["reviewer-a"], 1)
            # frank merged and reviewer-c reviewed on day 15 at 12:00, eight hours before the
            # 20:00 snapshot. Both are real and both are COUNTED -- the totals are documented
            # as exact through the snapshot instant, and a contributor must not vanish from
            # them for a few hours.
            self.assertEqual(metrics["merge_totals_by_contributor"]["frank"], 1)
            self.assertEqual(metrics["review_totals_by_contributor"]["reviewer-c"], 1)
            # They are not PLOTTED, because day 15 is not over: drawing eight hours of it as
            # though it were a whole day reads as a downturn, and the next run three hours
            # later would redraw the same point higher.
            self.assertEqual(metrics["last_full_day"], "2026-01-14")
            self.assertEqual(metrics["cumulative_dates"][-1], "2026-01-14")
            plotted = sum(values[-1] for values
                          in metrics["cumulative_merges_plotted"].values())
            counted = sum(metrics["merge_totals_by_contributor"].values())
            self.assertEqual(counted - plotted, 1)  # frank's, held back for the day
            # And anything after the snapshot instant stays out of both.
            self.assertNotIn("future-author", metrics["merge_totals_by_contributor"])
            self.assertNotIn("future-reviewer", metrics["review_totals_by_contributor"])

    def test_render_failure_keeps_previous_asset_set(self):
        data = {
            "repo": "example/project", "fetched_at": timestamp(15),
            "prs": [pr(1, 1, merged_day=2, cycles=1)], "scoreboards": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "assets"
            out.mkdir()
            existing = out / "pr-queue-age.svg"
            existing.write_text("previous", encoding="utf-8")
            with patch.object(
                stats, "render_review_cycles", side_effect=RuntimeError("boom"),
            ):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    stats.generate(data, out)
            self.assertEqual(existing.read_text(encoding="utf-8"), "previous")
            self.assertFalse((out / "review-cycles-reached.svg").exists())


class SiteStatsPageTest(unittest.TestCase):
    """The page keeps the user-requested narrative and chart order."""

    def test_participation_precedes_contributor_histories(self):
        source = (
            Path(__file__).parents[1] / "web" / "Site" / "Stats.lean"
        ).read_text(encoding="utf-8")
        self.assertLess(
            source.index('src="static/rolling-seven-day-history.svg"'),
            source.index('src="static/review-cycles-reached.svg"'),
        )
        self.assertLess(
            source.rindex(":::blob participationGraph"),
            source.rindex(":::blob contributorGraphs"),
        )


class MergeLatencyTest(unittest.TestCase):
    """Time to merge split at the queue's first approval, over 24-hour and seven-day windows."""

    URL = "https://queue.example/statistics/landings?days=35"

    @staticmethod
    def at(day, hour=0):
        return datetime(2026, 1, day, hour, tzinfo=UTC).timestamp()

    def landings(self, entries, coverage_day=1):
        return {"source": self.URL, "observed_at": self.at(20), "coverage_since": self.at(coverage_day),
                "landings": entries}

    def metrics(self, entries, *, github=(), forgejo=(), coverage_day=1, last_day=12):
        return stats.merge_latency_metrics(
            list(github), list(forgejo), self.landings(entries, coverage_day),
            date(2026, 1, last_day), datetime(2026, 1, last_day + 1, 6, tzinfo=UTC), history_days=90)

    def test_the_three_measures_add_up_and_follow_their_forge(self):
        github = [pr(1, 2)]                       # opened 2 Jan 00:00
        forgejo = [{**pr(1, 9), "number": 1}]     # same number, another pull request
        entries = [["github", 1, self.at(10, 0), self.at(10, 12)],
                   ["forgejo", 1, self.at(10, 6), self.at(10, 12)]]
        m = self.metrics(entries, github=github, forgejo=forgejo, last_day=10)
        day = m["dates"].index("2026-01-10")
        one = m["series"]["24h"]
        self.assertEqual(one["open_to_land"]["count"][day], 2)
        # GitHub: 8.5 days open, 8 days to approval, 12 h in the queue; Forgejo: 36 h, 30 h, 6 h.
        self.assertEqual(sorted([one["open_to_land"]["p25"][day], one["open_to_land"]["p75"][day]]),
                         sorted([36 + (204 - 36) * .25, 36 + (204 - 36) * .75]))
        self.assertEqual(one["approval_to_land"]["median"][day], 9.0)
        self.assertEqual(one["open_to_approval"]["median"][day], (192 + 30) / 2)

    def test_a_seven_day_point_needs_a_whole_week_of_coverage(self):
        github = [pr(1, 1)]
        entries = [["github", 1, self.at(5), self.at(5, 6)]]
        m = self.metrics(entries, github=github, coverage_day=4, last_day=12)
        # Coverage from exactly midnight covers that day itself.
        self.assertEqual(m["dates"][0], "2026-01-04")
        seven = m["series"]["7d"]["open_to_land"]["median"]
        # Coverage starts 4 Jan 00:00: the week ending 9 Jan reaches before it, the one ending 10 Jan does not.
        self.assertEqual(seven[m["dates"].index("2026-01-09")], None)
        self.assertEqual(seven[m["dates"].index("2026-01-10")], 102.0)
        # By 12 Jan the 5 Jan landing has left the week: an empty window, not a missing one.
        self.assertIsNone(seven[m["dates"].index("2026-01-12")])
        self.assertEqual(m["series"]["7d"]["open_to_land"]["count"][m["dates"].index("2026-01-12")], 0)
        self.assertEqual(m["series"]["24h"]["open_to_land"]["count"][m["dates"].index("2026-01-06")], 0)

    def test_current_covers_the_window_ending_at_the_snapshot(self):
        github = [pr(1, 10), pr(2, 12)]
        entries = [["github", 1, self.at(11), self.at(12, 1)], ["github", 2, self.at(13), self.at(13, 2)]]
        m = self.metrics(entries, github=github, last_day=12)
        self.assertEqual(m["current"]["as_of"], "2026-01-13T06:00:00Z")
        self.assertEqual(m["current"]["24h"]["open_to_land"]["count"], 1)
        self.assertEqual(m["current"]["24h"]["open_to_land"]["median"], 26.0)
        self.assertEqual(m["current"]["7d"]["open_to_land"]["count"], 2)

    def test_an_approval_before_opening_is_clamped_and_unknown_prs_are_counted(self):
        entries = [["github", 1, self.at(1), self.at(10)], ["github", 9, self.at(9), self.at(10)]]
        m = self.metrics(entries, github=[pr(1, 5)], last_day=10)
        day = m["dates"].index("2026-01-10")
        self.assertEqual(m["series"]["24h"]["open_to_approval"]["median"][day], 0.0)
        self.assertEqual(m["series"]["24h"]["approval_to_land"]["median"][day], 120.0)
        self.assertEqual(m["unmatched_landings"], 1)

    def test_days_past_the_feed_s_last_observation_are_not_published(self):
        entries = [["github", 1, self.at(5), self.at(5, 6)]]
        m = stats.merge_latency_metrics(
            [pr(1, 1)], [], {**self.landings(entries), "observed_at": self.at(8)},
            date(2026, 1, 12), datetime(2026, 1, 13, tzinfo=UTC), 90)
        one = m["series"]["24h"]["open_to_land"]["count"]
        self.assertEqual(one[m["dates"].index("2026-01-07")], 0)
        self.assertIsNone(one[m["dates"].index("2026-01-08")])
        self.assertEqual(m["current"]["as_of"], "2026-01-08T00:00:00Z")

    def test_a_current_window_reaching_before_coverage_is_null(self):
        m = self.metrics([["github", 1, self.at(12), self.at(12, 6)]], github=[pr(1, 11)],
                         coverage_day=10, last_day=12)
        self.assertIsNone(m["current"]["7d"])
        self.assertEqual(m["current"]["24h"]["open_to_land"]["count"], 1)

    def test_an_empty_feed_is_covered_days_with_nothing_landed(self):
        m = self.metrics([], github=[pr(1, 1)], last_day=5)
        self.assertEqual(m["series"]["24h"]["open_to_land"]["count"][-1], 0)
        self.assertIsNone(m["series"]["24h"]["open_to_land"]["median"][-1])

    def test_a_landing_before_opening_is_set_aside(self):
        m = self.metrics([["github", 1, self.at(2), self.at(3)]], github=[pr(1, 5)], last_day=5)
        self.assertEqual(m["inconsistent_landings"], 1)
        self.assertEqual(m["series"]["24h"]["open_to_land"]["count"][m["dates"].index("2026-01-03")], 0)

    def test_no_feed_means_no_chart_data(self):
        self.assertIsNone(stats.merge_latency_metrics([pr(1, 1)], [], None, date(2026, 1, 5),
                                                      datetime(2026, 1, 6, tzinfo=UTC), 90))

    def feed(self, since, observed, entries):
        return {"schema": stats.QUEUE_LANDINGS_SCHEMA, "fields": stats.QUEUE_LANDINGS_FIELDS,
                "since": since, "observed_at": observed, "landings": entries}

    def test_contiguous_feeds_accumulate_and_a_gap_restarts_coverage(self):
        first = self.feed(self.at(1), self.at(5), [["github", 1, self.at(2), self.at(3)]])
        with patch.object(stats, "read_queue_landings", return_value=first):
            kept = stats.fetch_queue_landings(self.URL, None)
        second = self.feed(self.at(4), self.at(8), [["github", 2, self.at(6), self.at(7)]])
        with patch.object(stats, "read_queue_landings", return_value=second):
            joined = stats.fetch_queue_landings(self.URL, kept)
        self.assertEqual([e[1] for e in joined["landings"]], [1, 2])
        self.assertEqual(joined["coverage_since"], self.at(1))
        late = self.feed(self.at(10), self.at(14), [["github", 3, self.at(11), self.at(12)]])
        with patch.object(stats, "read_queue_landings", return_value=late):
            restarted = stats.fetch_queue_landings(self.URL, joined)
        self.assertEqual(([e[1] for e in restarted["landings"]], restarted["coverage_since"]),
                         ([3], self.at(10)))

    def test_a_failed_read_keeps_the_previous_landings(self):
        previous = {"source": self.URL, "observed_at": 1.0, "coverage_since": 0.0, "landings": []}
        with patch.object(stats, "read_queue_landings", side_effect=OSError("down")):
            self.assertIs(stats.fetch_queue_landings(self.URL, previous), previous)

    def test_a_malformed_feed_is_rejected(self):
        for body in ({"schema": "x"}, self.feed(1, 2, [["github", "1", 1, 2]]), [], self.feed(1, 2, [[1, 2]]),
                     {**self.feed(1, 2, []), "since": float("inf")}, self.feed(1, 2, [["github", 1, 2, 1.5]]),
                     self.feed(1, 2, [["github", 1, 1, 3]])):
            raw = json.dumps(body).encode()
            response = MagicMock()
            response.__enter__.return_value.read.side_effect = lambda n=-1, raw=raw: raw
            with self.subTest(body=body), patch.object(stats.urllib.request, "urlopen", return_value=response):
                with self.assertRaises(ValueError):
                    stats.read_queue_landings(self.URL)

    def test_the_chart_is_written_with_or_without_data(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "merge-latency.svg"
            stats.render_merge_latency(path, None)
            self.assertIn("No queue landing data yet", path.read_text())
            m = self.metrics([["github", 1, self.at(10), self.at(10, 12)]], github=[pr(1, 9)], last_day=12)
            stats.render_merge_latency(path, m)
            self.assertIn("latest median", path.read_text())
            # Two landing days with an empty day between them draw two separate lines.
            gap = self.metrics([["github", 1, self.at(10), self.at(10, 12)], ["github", 2, self.at(12), self.at(12, 12)]],
                               github=[pr(1, 9), pr(2, 11)], last_day=12)
            stats.render_merge_latency(path, gap)
            self.assertEqual(path.read_text().count("<polyline"), 2)


if __name__ == "__main__":
    unittest.main()
