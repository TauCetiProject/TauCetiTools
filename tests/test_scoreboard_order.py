"""Scoreboard selection is independent of the order a source returns comments in."""

import itertools
import json
import unittest

from tauceti_tools.survey_facts import scoreboard, scoreboard_order

HEAD = "a" * 40


def board(cid, updated, states, *, created="2026-11-16T00:00:00Z", mode="commit"):
    meta = {"head_sha": HEAD, "mode": mode, "states": states, "submitted_by": f"reviewer-{cid}"}
    return {
        "id": cid,
        "created_at": created,
        "updated_at": updated,
        "body": "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 " + json.dumps(meta) + "-->",
    }


class ScoreboardOrderTest(unittest.TestCase):
    def test_last_edit_wins_over_last_created(self):
        # Two reviewers judged the same head. The board created first was edited last, so it is the
        # current verdict for the worker exactly as it is for the queue's admission rule.
        edited = board(6859938902, "2026-11-16T18:00:40Z", {"proof-quality": "blocking_request"},
                       created="2026-11-13T17:18:25Z")
        posted = board(6924490807, "2026-11-16T17:53:30Z", {"proof-quality": "green"},
                       created="2026-11-16T17:53:30Z")
        for comments in itertools.permutations([edited, posted]):
            self.assertEqual(scoreboard(list(comments))["submitted_by"], "reviewer-6859938902")

    def test_equal_timestamps_fall_to_the_larger_comment_id(self):
        boards = [board(cid, "2026-11-16T18:00:00Z", {"reuse": state})
                  for cid, state in ((7, "green"), (12, "blocking_request"), (9, "green"))]
        for comments in itertools.permutations(boards):
            self.assertEqual(scoreboard(list(comments))["submitted_by"], "reviewer-12")

    def test_creation_time_stands_in_for_a_missing_edit_time(self):
        older = board(1, "", {"reuse": "green"}, created="2026-11-16T10:00:00Z")
        newer = board(2, "", {"reuse": "blocking_request"}, created="2026-11-16T11:00:00Z")
        for comments in itertools.permutations([older, newer]):
            self.assertEqual(scoreboard(list(comments))["submitted_by"], "reviewer-2")

    def test_in_progress_boards_never_win(self):
        done = board(1, "2026-11-16T10:00:00Z", {"reuse": "green"})
        init = board(2, "2026-11-16T11:00:00Z", {"reuse": "blocking_request"}, mode="init")
        for comments in itertools.permutations([done, init]):
            self.assertEqual(scoreboard(list(comments))["submitted_by"], "reviewer-1")

    def test_comments_without_ids_fall_to_the_later_input_position(self):
        first = board("x", "2026-11-16T18:00:00Z", {"reuse": "green"})
        second = board("y", "2026-11-16T18:00:00Z", {"reuse": "blocking_request"})
        self.assertEqual(scoreboard([first, second])["submitted_by"], "reviewer-y")
        self.assertEqual(scoreboard([second, first])["submitted_by"], "reviewer-x")

    def test_malformed_ids_sort_below_numeric_ones(self):
        self.assertLess(scoreboard_order({"id": True, "updated_at": "x"}),
                        scoreboard_order({"id": "0", "updated_at": "x"}))
        self.assertEqual(scoreboard_order({"id": " 5"})[1], -1)


if __name__ == "__main__":
    unittest.main()
