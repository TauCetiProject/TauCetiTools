#!/usr/bin/env python3
"""Tests for the housekeeping job that retires long-red PRs.

This file closes other people's work, so the cases worth pinning are the ones where it would close
something it should not: a PR whose build has since gone green, a draft, one a human has asked to
keep, or one whose timeline and label list disagree.

The `red` job exists because every other job here reads the review scoreboard, and a PR that never
went green never reached review and never got one. #1556 sat red for four weeks with zero
scoreboards and nothing in this file could see it. Its clock is the current `ci-failed` spell
rather than `updatedAt`, because `updatedAt` bumps on any comment or label -- on #1556 the conflict
bot's notice reset the stale timer three weeks after the last human touch.

Pure logic; no network. Run: python3 scripts/test_housekeeping.py
"""

import datetime
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("REPO", "example/project")
os.environ.setdefault("GH_TOKEN", "unused-in-tests")

import housekeeping as hk  # noqa: E402

UTC = datetime.timezone.utc


def at(day, hour=0):
    return f"2026-08-{day:02d}T{hour:02d}:00:00Z"


def labeled(day, name="ci-failed", hour=0):
    return {"event": "labeled", "created_at": at(day, hour), "label": {"name": name}}


def unlabeled(day, name="ci-failed", hour=0):
    return {"event": "unlabeled", "created_at": at(day, hour), "label": {"name": name}}


class LabelSpell(unittest.TestCase):
    """`label_spell_start` decides how long a PR has been red, so it decides what gets closed."""

    def test_a_single_labelling_starts_the_spell(self):
        self.assertEqual(hk.label_spell_start([labeled(3)], "ci-failed"),
                         datetime.datetime(2026, 8, 3, tzinfo=UTC))

    def test_an_unlabelling_ends_it(self):
        # The build went green. Reading the stale `labeled` event as a live spell would close a PR
        # that is now fine, which is the worst thing this file could do.
        self.assertIsNone(hk.label_spell_start([labeled(3), unlabeled(5)], "ci-failed"))

    def test_relabelling_restarts_the_clock(self):
        # Every CI cycle unlabels and relabels, so a PR whose build is being re-run starts over.
        # This is what makes seven days mean abandonment rather than effort.
        self.assertEqual(
            hk.label_spell_start([labeled(3), unlabeled(5), labeled(9)], "ci-failed"),
            datetime.datetime(2026, 8, 9, tzinfo=UTC))

    def test_other_labels_are_ignored(self):
        events = [labeled(3, "merge-conflict"), labeled(4), unlabeled(6, "merge-conflict")]
        self.assertEqual(hk.label_spell_start(events, "ci-failed"),
                         datetime.datetime(2026, 8, 4, tzinfo=UTC))

    def test_a_pr_that_was_never_labelled_has_no_spell(self):
        self.assertIsNone(hk.label_spell_start([labeled(3, "awaiting-review")], "ci-failed"))

    def test_events_without_a_label_do_not_crash_it(self):
        # The timeline carries referenced/closed/assigned events too, with no label field.
        events = [{"event": "referenced", "created_at": at(2), "label": None}, labeled(4)]
        self.assertEqual(hk.label_spell_start(events, "ci-failed"),
                         datetime.datetime(2026, 8, 4, tzinfo=UTC))


class RedSelection(unittest.TestCase):
    """The list-side filter, which decides which PRs are even considered."""

    @staticmethod
    def pr(number, labels=("ci-failed",), draft=False):
        return {"number": number, "isDraft": draft,
                "labels": [{"name": n} for n in labels], "updatedAt": at(20)}

    def candidates(self, prs):
        return [p for p in prs
                if p.get("isDraft") is False and not hk.has_keep_label(p)
                and hk.has_label(p, hk.CI_FAILED_LABEL)]

    def test_a_red_pr_is_a_candidate(self):
        self.assertEqual([p["number"] for p in self.candidates([self.pr(1)])], [1])

    def test_a_keep_label_spares_it(self):
        for label in sorted(hk.KEEP_LABELS):
            with self.subTest(label=label):
                self.assertEqual(self.candidates([self.pr(1, ("ci-failed", label))]), [])

    def test_a_draft_is_spared(self):
        self.assertEqual(self.candidates([self.pr(1, draft=True)]), [])

    def test_an_unknown_draft_state_is_spared(self):
        # Fail closed, matching the stale job: isDraft must be explicitly False.
        p = self.pr(1)
        p["isDraft"] = None
        self.assertEqual(self.candidates([p]), [])

    def test_a_pr_that_is_not_red_is_not_a_candidate(self):
        self.assertEqual(self.candidates([self.pr(1, ("awaiting-review",))]), [])

    def test_the_default_window_is_a_week(self):
        self.assertEqual(hk.CI_FAILED_DAYS, 7)


class StaleSelection(unittest.TestCase):
    def run_case(self, *, labels=("awaiting-author",), draft=False, live_changes=None, updated="2020-01-01T00:00:00Z"):
        pr = {"number": 8282, "state": "OPEN", "isDraft": draft, "updatedAt": updated,
              "headRefOid": "a" * 40, "body": "", "labels": [{"name": x} for x in labels]}
        live = {**pr, **(live_changes or {})}
        def read(args):
            return live if args[:2] == ["pr", "view"] else [pr]
        with patch.object(hk, "gh_json", side_effect=read), \
             patch.object(hk, "latest_scoreboard_meta", return_value=None) as scoreboard, \
             patch.object(hk, "close", return_value=True) as close:
            self.assertEqual(hk.main(), 0)
        return close, scoreboard

    def test_stale_author_status_does_not_require_an_associated_scoreboard(self):
        close, scoreboard = self.run_case()
        close.assert_called_once()
        # The unchanged budget pass still reads once; stale adds no history read.
        scoreboard.assert_called_once_with(8282)

    def test_no_author_status_never_closes_on_an_arbitrary_comment(self):
        close, _ = self.run_case(labels=())
        close.assert_not_called()

    def test_draft_and_operator_holds_remain_exempt(self):
        self.run_case(draft=True)[0].assert_not_called()
        for label in hk.KEEP_LABELS:
            with self.subTest(label=label):
                self.run_case(labels=("awaiting-author", label))[0].assert_not_called()

    def test_recent_activity_resets_the_existing_inactivity_clock(self):
        self.run_case(updated="2099-01-01T00:00:00Z")[0].assert_not_called()

    def test_live_push_hold_or_status_change_prevents_close(self):
        for changes in ({"updatedAt": "2099-01-01T00:00:00Z"}, {"state": "CLOSED"},
                        {"isDraft": True}, {"labels": [{"name": "awaiting-author"}, {"name": "keep"}]},
                        {"labels": [{"name": "awaiting-review"}]}):
            with self.subTest(changes=changes):
                self.run_case(live_changes=changes)[0].assert_not_called()


class AutomaticBump(unittest.TestCase):
    def test_no_retirement_path_closes_a_canonical_bot_bump(self):
        pr = {"number": 99, "isDraft": False, "headRefOid": "a" * 40,
              "updatedAt": "2020-01-01T00:00:00Z", "body": "",
              "author": {"is_bot": True}, "headRefName": "bump-mathlib/test",
              "headRepositoryOwner": {"login": "TauCetiProject"},
              "headRepository": {"name": "TauCeti"},
              "labels": [{"name": "ci-failed"}, {"name": hk.BUDGET_LABEL}]}
        with patch.object(hk.core, "REPO", "TauCetiProject/TauCeti"), \
                patch.object(hk, "gh_json", return_value=[pr]), \
                patch.object(hk, "latest_scoreboard_meta", side_effect=AssertionError("bump must not be retired")), \
                patch.object(hk, "close", side_effect=AssertionError("bump must stay open")):
            self.assertEqual(hk.main(), 0)
        self.assertTrue(hk.core.automatic_bump(pr, repo="TauCetiProject/TauCeti"))
        self.assertFalse(hk.core.automatic_bump(pr, repo="another/repo"))
        for changes in [{"author": {"is_bot": False}},
                        {"headRepositoryOwner": {"login": "external"}},
                        {"headRefName": "feature"}]:
            self.assertFalse(hk.core.automatic_bump(pr | changes, repo="TauCetiProject/TauCeti"))


class Comment(unittest.TestCase):
    def test_it_says_why_and_how_to_opt_out(self):
        body = hk.CI_FAILED_COMMENT.format(days=hk.CI_FAILED_DAYS)
        self.assertIn("7", body)
        self.assertIn("branch is kept", body)
        self.assertIn("keep", body)


if __name__ == "__main__":
    unittest.main()
