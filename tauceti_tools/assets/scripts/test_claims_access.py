"""Who gets admitted to the claim namespace, and which leases are safe to delete.

Run with: python3 scripts/test_claims_access.py
"""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

import claims_access as ca


class HumanAuthors(unittest.TestCase):
    def test_bots_never_get_the_claim_namespace(self):
        prs = [
            {"author": {"login": "alice", "is_bot": False}},
            {"author": {"login": "tauceti-review", "is_bot": True}},
            {"author": {"login": "dependabot[bot]"}},
        ]
        self.assertEqual(ca.human_authors(prs), {"alice"})

    def test_authors_are_distinct_and_deleted_accounts_are_skipped(self):
        prs = [
            {"author": {"login": "alice"}},
            {"author": {"login": "alice"}},
            {"author": None},
            {"author": {"login": ""}},
            {},
        ]
        self.assertEqual(ca.human_authors(prs), {"alice"})

    def test_no_merged_prs_admits_nobody(self):
        self.assertEqual(ca.human_authors([]), set())
        self.assertEqual(ca.human_authors(None), set())


class HasAccess(unittest.TestCase):
    """Access inherited from the organization is invisible to the collaborator list this App can see,
    so without an explicit check every org member is a candidate to invite on every single run."""

    def answer(self, returncode, stdout):
        return lambda args, token, check=True: SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    def test_anyone_who_can_already_push_is_left_alone(self):
        for permission in ("admin", "maintain", "write"):
            with self.subTest(permission=permission), mock.patch.object(ca, "gh", self.answer(0, permission + "\n")):
                self.assertTrue(ca.has_access("an-org-member"))

    def test_read_only_or_no_access_still_needs_an_invitation(self):
        for permission in ("read", "none", ""):
            with self.subTest(permission=permission), mock.patch.object(ca, "gh", self.answer(0, permission + "\n")):
                self.assertFalse(ca.has_access("a-contributor"))

    def test_an_unreadable_answer_falls_towards_inviting(self):
        # A redundant invitation is a no-op; a missed one is a fleet that never coordinates.
        with mock.patch.object(ca, "gh", self.answer(1, "")):
            self.assertFalse(ca.has_access("a-contributor"))


class Sweepable(unittest.TestCase):
    def lease(self, expires_at):
        return json.dumps({"schema": "tauceti-claim/v1", "owner": "w1", "expires_at": expires_at})

    def test_a_live_lease_is_never_swept(self):
        self.assertFalse(ca.is_sweepable(self.lease(2000), 1000))

    def test_a_lease_inside_the_grace_period_is_never_swept(self):
        # Expired, but only just: a worker may be taking it over right now, and that takeover is a
        # CAS the sweep must not race.
        self.assertFalse(ca.is_sweepable(self.lease(1001), 1000))

    def test_a_long_expired_lease_is_swept(self):
        self.assertTrue(ca.is_sweepable(self.lease(500), 1000))

    def test_an_unreadable_lease_is_left_alone(self):
        # Anything in this namespace that we cannot parse was not written by a worker we understand,
        # so deleting it would be guessing.
        for junk in ("", "not json", "[]", '"a string"', json.dumps({"owner": "w1"})):
            with self.subTest(junk=junk):
                self.assertFalse(ca.is_sweepable(junk, 1000))

    def test_a_non_numeric_expiry_is_left_alone(self):
        for expires in ("soon", None, True, {"at": 1}):
            with self.subTest(expires=expires):
                self.assertFalse(ca.is_sweepable(self.lease(expires), 1000))


class QueueLanded(unittest.TestCase):
    """The merge queue closes the PRs it lands, so GitHub reports them as closed, not merged."""

    def test_a_pr_titled_as_landed_counts_for_its_author(self):
        prs = [
            {"number": 3, "author": {"login": "alice"}, "title": ca.LANDED_TITLE + "feat: a lemma"},
            {"number": 2, "author": {"login": "alice"}, "title": ca.LANDED_TITLE + "feat: another"},
            {"number": 1, "author": {"login": "bob"}, "title": "feat: closed without landing"},
            {"number": 0, "author": {"login": "tauceti-review-bot[bot]"}, "title": ca.LANDED_TITLE + "chore: bump"},
        ]
        answer = SimpleNamespace(returncode=0, stdout=json.dumps(prs), stderr="")
        with mock.patch.object(ca, "gh", lambda args, token, check=True: answer):
            self.assertEqual(ca.queue_landed_prs(), {"alice": [3, 2]})

    def closers(self, *logins, returncode=0):
        out = "".join(login + "\n" for login in logins)
        return lambda args, token, check=True: SimpleNamespace(returncode=returncode, stdout=out, stderr="")

    def test_the_queue_closed_it(self):
        with mock.patch.object(ca, "gh", self.closers("tauceti-queue[bot]")):
            self.assertTrue(ca.closed_by_lander(7))

    def test_the_title_alone_is_not_enough(self):
        # An author can retitle and close their own PR. Only the queue's closing counts, and the last.
        for closers in (("alice",), ("tauceti-queue[bot]", "alice"), ()):
            with self.subTest(closers=closers), mock.patch.object(ca, "gh", self.closers(*closers)):
                self.assertFalse(ca.closed_by_lander(7))

    def test_an_unreadable_answer_is_no(self):
        with mock.patch.object(ca, "gh", self.closers("tauceti-queue[bot]", returncode=1)):
            self.assertFalse(ca.closed_by_lander(7))


class Grant(unittest.TestCase):
    """What `grant` sends, from what GitHub reports. Every login here can only read the claims repo."""

    def grant(self, merged=(), closed=(), invitations=(), closers=None):
        calls = []

        def gh(args, token, check=True):
            calls.append(args)
            path = " ".join(args)
            out = ""
            if args[:2] == ["pr", "list"]:
                out = json.dumps(list(merged if "merged" in args else closed))
            elif "/invitations?" in path:
                out = "".join(f"{login} {number} {str(expired).lower()}\n" for login, number, expired in invitations)
            elif path.endswith("/permission --jq .permission"):
                out = "read\n"
            elif "/events?" in path:
                number = int(path.split("/issues/")[1].split("/")[0])
                out = "".join(login + "\n" for login in (closers or {}).get(number, ()))
            elif args[:3] == ["api", "-X", "PUT"]:
                out = "{}"
            return SimpleNamespace(returncode=0, stdout=out, stderr="")

        with mock.patch.object(ca, "gh", gh), mock.patch.object(ca, "DRY_RUN", False):
            failures = ca.grant()
        writes = [(args[2], args[3].rsplit("/", 1)[1]) for args in calls if args[:2] == ["api", "-X"]]
        return failures, writes, calls

    def landed(self, number, login):
        return {"number": number, "author": {"login": login}, "title": ca.LANDED_TITLE + "feat: x"}

    def test_the_author_of_a_pr_the_queue_landed_is_invited(self):
        _, writes, _ = self.grant(closed=[self.landed(5, "alice")], closers={5: ["tauceti-queue[bot]"]})
        self.assertEqual(writes, [("PUT", "alice")])

    def test_a_retitled_pr_the_author_closed_invites_nobody(self):
        _, writes, _ = self.grant(closed=[self.landed(5, "alice")], closers={5: ["alice"]})
        self.assertEqual(writes, [])

    def test_a_pr_merged_by_github_needs_no_further_check(self):
        _, writes, calls = self.grant(merged=[{"author": {"login": "bob"}}])
        self.assertEqual(writes, [("PUT", "bob")])
        self.assertFalse([args for args in calls if "/events?" in " ".join(args)])

    def test_an_expired_invitation_is_cancelled_and_sent_again(self):
        _, writes, _ = self.grant(merged=[{"author": {"login": "bob"}}], invitations=[("bob", "42", True)])
        self.assertEqual(writes, [("DELETE", "42"), ("PUT", "bob")])

    def test_a_live_invitation_is_left_alone(self):
        _, writes, _ = self.grant(merged=[{"author": {"login": "bob"}}], invitations=[("bob", "42", False)])
        self.assertEqual(writes, [])

    def test_an_invitation_sent_by_email_is_ignored(self):
        with mock.patch.object(ca, "gh_lines", lambda args, token: ["null 7 false", "bob 8 true"]):
            self.assertEqual(ca.invitations(), {"bob": ("8", True)})


if __name__ == "__main__":
    unittest.main()
