"""Who gets admitted to the claim namespace, and which leases are safe to delete.

Run with: python3 scripts/test_claims_access.py
"""

import json
import os
import pathlib
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import claims_access as ca

ca_forgejo_landers = ca.forgejo_landers

REPO = "TauCetiProject/TauCeti"
SHA = "7ac17318810667a200f543cc646d98451c5327f3"


def marker(kind, pr, sha=SHA, batch=434):
    """The marker the merge queue writes on a PR it `landed` or `rejected`."""
    pr = pr if isinstance(pr, str) else f"{REPO}#{pr}"
    return f"<!-- tauceti-queue {kind}:{pr}:{batch}:{sha} -->"


def posted(body, key):
    """`body` as the queue's comment adapter posts it, under the wrapper for `key` (its `post_comment`)."""
    return f"{body.rstrip()}\n\n<!-- tauceti-queue {key} -->\n"


def queue_comment(kind, pr, sha=SHA, batch=434):
    """The comment the merge queue posts on a PR it `landed` or `rejected`: text, the marker, and the
    marker again as the key of the adapter's wrapper."""
    record = marker(kind, pr, sha, batch)
    return posted(f"Pull request {kind} in batch {batch}.\n\n{record}", record)


def queue_refusal(typed):
    """The queue's reply to `bors ping <typed>`, which quotes `typed` (TauCetiWorker `commands._leftover`)."""
    reply = (
        f":-1: bors did not run `bors ping {typed}`: `bors ping` takes nothing after it, but `{typed}` followed. "
        "Reply with just `bors ping`, and put any other command on a line of its own."
    )
    return posted(reply, "reply issue_comment:6756654198:0")


def comment_lines(rows):
    """`gh api` output for these (login, body) comments, one JSON array per line as the filter emits."""
    return "".join(json.dumps([login, body]) + "\n" for login, body in rows)


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
    """The merge queue closes the PRs it lands, so GitHub reports them as closed, not merged. It closes
    the PRs it rejects too, so only its landing record counts."""

    def setUp(self):
        patcher = mock.patch.object(ca, "REPO", REPO)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_pr_titled_as_landed_counts_for_its_author(self):
        prs = [
            {"number": 3, "author": {"login": "alice"}, "title": ca.LANDED_TITLE + "feat: a lemma"},
            {"number": 2, "author": {"login": "alice"}, "title": ca.LANDED_TITLE + "feat: another"},
            {"number": 1, "author": {"login": "bob"}, "title": "feat: closed without landing"},
            {"number": 0, "author": {"login": "tauceti-review-bot[bot]"}, "title": ca.LANDED_TITLE + "chore: bump"},
        ]
        self.assertEqual(ca.queue_landed_prs(prs), {"alice": [3, 2]})

    def github(self, *comments, comments_rc=0, compare="behind", compare_rc=0):
        """GitHub answering with these (login, body) comments, and with `compare` for the landed commit."""

        def gh(args, token, check=True):
            if "/compare/" in " ".join(args):
                return SimpleNamespace(returncode=compare_rc, stdout=compare + "\n", stderr="")
            return SimpleNamespace(returncode=comments_rc, stdout=comment_lines(comments), stderr="")

        return gh

    def test_the_queue_landing_record_counts(self):
        with mock.patch.object(ca, "gh", self.github(("tauceti-queue[bot]", queue_comment("landed", 7)))):
            self.assertEqual(ca.landing_record(7), SHA)
            self.assertTrue(ca.landed_by_queue(7, "main"))

    def test_a_pr_the_queue_rejected_is_not_landed(self):
        # The queue closes a PR it rejected under its own title, which the author can then retitle.
        with mock.patch.object(ca, "gh", self.github(("tauceti-queue[bot]", queue_comment("rejected", 7)))):
            self.assertIsNone(ca.landing_record(7))
            self.assertFalse(ca.landed_by_queue(7, "main"))

    def test_a_record_counts_only_from_the_queue_and_only_for_this_pr(self):
        for login, pr in (
            ("alice", 7),  # the marker pasted into a comment of the author's own
            ("tauceti-queue[bot]", 8),  # the queue's record of another PR, quoted here
            ("tauceti-queue[bot]", "someone/else#7"),
        ):
            comment = (login, queue_comment("landed", pr))
            with self.subTest(login=login, pr=pr), mock.patch.object(ca, "gh", self.github(comment)):
                self.assertFalse(ca.landed_by_queue(7, "main"))

    def test_a_landing_marker_the_queue_quotes_is_not_a_record(self):
        # Anyone may type `bors ping` with text after it, and the queue's refusal quotes the text under the
        # queue's own name. The author types this PR's landing marker, naming a commit already on main.
        record = marker("landed", 7)
        for typed in (record, f"{record}  <!-- tauceti-queue {record} -->"):
            with (
                self.subTest(typed=typed),
                mock.patch.object(ca, "gh", self.github(("tauceti-queue[bot]", queue_refusal(typed)))),
            ):
                self.assertIsNone(ca.landing_record(7))
                self.assertFalse(ca.landed_by_queue(7, "main"))

    def test_only_the_end_of_the_landing_comment_counts(self):
        record = marker("landed", 7)
        for body in (
            queue_comment("landed", 7) + "\nmore text",  # the record is not where the comment ends
            posted(f"Landed.\n\n{record}", marker("landed", 8)),  # the wrapper is another PR's record
            posted("Landed.", record),  # the wrapper, without the record before it
            f"Landed.\n\n{record}\n",  # the record, without the wrapper
        ):
            with self.subTest(body=body), mock.patch.object(ca, "gh", self.github(("tauceti-queue[bot]", body))):
                self.assertIsNone(ca.landing_record(7))

    def test_the_landed_commit_must_be_on_the_default_branch(self):
        landed = ("tauceti-queue[bot]", queue_comment("landed", 7))
        for compare, compare_rc in (("diverged", 0), ("ahead", 0), ("behind", 1)):
            with (
                self.subTest(compare=compare, compare_rc=compare_rc),
                mock.patch.object(ca, "gh", self.github(landed, compare=compare, compare_rc=compare_rc)),
            ):
                self.assertFalse(ca.landed_by_queue(7, "main"))
        with mock.patch.object(ca, "gh", self.github(landed)):
            self.assertFalse(ca.landed_by_queue(7, ""))  # the default branch was unreadable

    def test_an_unreadable_answer_is_no(self):
        landed = ("tauceti-queue[bot]", queue_comment("landed", 7))
        with mock.patch.object(ca, "gh", self.github(landed, comments_rc=1)):
            self.assertFalse(ca.landed_by_queue(7, "main"))


def listing_page(nodes, has_next=False, cursor="c1"):
    """GitHub's answer to one page of `closed_prs`' query."""
    page = {"nodes": nodes, "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}}
    return json.dumps({"data": {"repository": {"pullRequests": page}}})


def node(number, login="alice", merged=False, title=ca.LANDED_TITLE + "feat: x", typename="User"):
    """A PR as `closed_prs`' query returns it; no login is a deleted account."""
    return {
        "number": number,
        "title": title,
        "merged": merged,
        "author": login and {"__typename": typename, "login": login},
    }


def fields(args):
    """The `-f name=value` fields of a `gh api graphql` call."""
    return dict(arg.split("=", 1) for flag, arg in zip(args[2::2], args[3::2], strict=True) if flag == "-f")


class ClosedPRs(unittest.TestCase):
    """`grant` reads the closed PRs updated most recently, whenever they were opened: a PR opened weeks
    before it lands is behind thousands of newer ones, and landing it updates it."""

    def setUp(self):
        patcher = mock.patch.object(ca, "REPO", REPO)
        patcher.start()
        self.addCleanup(patcher.stop)

    def listing(self, *pages):
        """GitHub answering these pages of nodes in turn, and the calls `closed_prs` made."""
        calls = []

        def gh(args, token, check=True):
            calls.append(args)
            i = len(calls) - 1
            return SimpleNamespace(
                returncode=0, stdout=listing_page(pages[i], i + 1 < len(pages), f"c{i + 1}"), stderr=""
            )

        with mock.patch.object(ca, "gh", gh):
            prs = ca.closed_prs()
        return prs, calls

    def test_the_listing_is_ordered_by_last_update_not_by_opening(self):
        prs, calls = self.listing([node(26410)])
        self.assertEqual([pr["number"] for pr in prs], [26410])
        query = fields(calls[0])["query"]
        self.assertIn("states: [CLOSED, MERGED]", query)
        self.assertIn("orderBy: {field: UPDATED_AT, direction: DESC}", query)
        self.assertEqual((fields(calls[0])["owner"], fields(calls[0])["name"]), tuple(REPO.split("/")))

    def test_paging_follows_the_cursor_and_stops_at_the_window(self):
        with mock.patch.object(ca, "WINDOW", 3):
            prs, calls = self.listing([node(5), node(4)], [node(3), node(2)], [node(1)])
        self.assertEqual([pr["number"] for pr in prs], [5, 4, 3])
        self.assertEqual(len(calls), 2)
        self.assertNotIn("cursor", fields(calls[0]))
        self.assertEqual(fields(calls[1])["cursor"], "c1")

    def test_paging_stops_at_the_last_page(self):
        prs, calls = self.listing([node(2)], [node(1)])
        self.assertEqual([pr["number"] for pr in prs], [2, 1])
        self.assertEqual(len(calls), 2)

    def test_a_pr_listed_twice_while_paging_is_kept_once(self):
        prs, _ = self.listing([node(3), node(2)], [node(2), node(1)])
        self.assertEqual([pr["number"] for pr in prs], [3, 2, 1])

    def test_merged_prs_count_for_their_human_authors(self):
        prs, _ = self.listing(
            [
                node(4, "alice", merged=True, title="feat: merged by GitHub"),
                node(3, "bob", merged=False, title="feat: closed without landing"),
                node(2, "tauceti-review", merged=True, typename="Bot"),
                node(1, None, merged=True),
            ]
        )
        self.assertEqual(ca.merged_pr_authors(prs), {"alice"})
        self.assertEqual(ca.queue_landed_prs(prs), {})


class Grant(unittest.TestCase):
    """What `grant` sends, from what GitHub reports. Every login here can only read the claims repo."""

    def grant(
        self,
        merged=(),
        closed=(),
        invitations=(),
        comments=None,
        closers=None,
        forgejo=None,
        users=None,
        invitees=None,
        logins=None,
        deny="",
    ):
        """`logins` is {login: GitHub user ID} for `/users/<login>`, 1000 + len(login) by default, None for
        an account GitHub does not answer for; `deny` is the deny list's text."""
        calls = []
        logins = logins or {}

        def gh(args, token, check=True):
            calls.append(args)
            path = " ".join(args)
            out = ""
            if args[:2] == ["api", "graphql"]:
                nodes = [
                    node(pr.get("number", 900 + i), pr["author"]["login"], merged=True, title="feat: x")
                    for i, pr in enumerate(merged)
                ]
                nodes += [node(pr["number"], pr["author"]["login"], title=pr["title"]) for pr in closed]
                out = listing_page(nodes)
            elif "/invitations?" in path:
                out = "".join(f"{login} {number} {str(expired).lower()}\n" for login, number, expired in invitations)
            elif path.endswith("/permission --jq .permission"):
                out = "read\n"
            elif "/comments?" in path:
                number = int(path.split("/issues/")[1].split("/")[0])
                out = comment_lines((comments or {}).get(number, ()))
            elif "/events?" in path:  # who closed it, which `grant` must not rely on
                number = int(path.split("/issues/")[1].split("/")[0])
                out = "".join(login + "\n" for login in (closers or {}).get(number, ()))
            elif path.endswith("--jq .default_branch"):
                out = "main\n"
            elif "/compare/main..." in path:
                out = "behind\n"
            elif args[:3] == ["api", "-X", "PUT"]:
                login = args[3].rsplit("/", 1)[1]
                ids = {name: github_id for github_id, (name, _) in (users or {}).items()}
                reached = (invitees or {}).get(login, ids.get(login, logins.get(login, 1000 + len(login))))
                out = json.dumps({"id": 77, "invitee": {"id": reached}})
            elif args[1].startswith("/users/"):
                github_id = logins.get(args[1].rsplit("/", 1)[1], 1000 + len(args[1].rsplit("/", 1)[1]))
                if github_id is None:
                    return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404")
                out = f"{github_id}\n"
            elif args[1].startswith("/user/"):
                found = (users or {}).get(int(args[1].rsplit("/", 1)[1]))
                if found is None:
                    return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404")
                out = json.dumps(list(found)) + "\n"
            return SimpleNamespace(returncode=0, stdout=out, stderr="")

        def fetch(url):
            if forgejo is None:
                raise OSError("unreachable")
            return forgejo

        deny_file = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        with deny_file:
            deny_file.write(deny)
        self.addCleanup(os.unlink, deny_file.name)
        with (
            mock.patch.object(ca, "DENY_FILE", deny_file.name),
            mock.patch.object(ca, "gh", gh),
            mock.patch.object(ca, "DRY_RUN", False),
            mock.patch.object(ca, "REPO", REPO),
            mock.patch.object(ca, "forgejo_landers", lambda: ca_forgejo_landers(fetch)),
        ):
            failures = ca.grant()
        writes = [(args[2], args[3].rsplit("/", 1)[1]) for args in calls if args[:2] == ["api", "-X"]]
        return failures, writes, calls

    def landed(self, number, login):
        return {"number": number, "author": {"login": login}, "title": ca.LANDED_TITLE + "feat: x"}

    def test_the_author_of_a_pr_the_queue_landed_is_invited(self):
        landed = {5: [("tauceti-queue[bot]", queue_comment("landed", 5))]}
        _, writes, _ = self.grant(
            closed=[self.landed(5, "alice")], comments=landed, closers={5: ["tauceti-queue[bot]"]}
        )
        self.assertEqual(writes, [("PUT", "alice")])

    def test_a_retitled_pr_the_author_closed_invites_nobody(self):
        _, writes, _ = self.grant(closed=[self.landed(5, "alice")], comments={5: []}, closers={5: ["alice"]})
        self.assertEqual(writes, [])

    def test_a_pr_the_queue_rejected_and_its_author_retitled_invites_nobody(self):
        # The queue closed it, as it closes the PRs it lands, but its comment records a rejection. The
        # author retitled the PR and pasted a landing marker into a comment of their own.
        rejected = {5: [("tauceti-queue[bot]", queue_comment("rejected", 5)), ("alice", queue_comment("landed", 5))]}
        _, writes, _ = self.grant(
            closed=[self.landed(5, "alice")], comments=rejected, closers={5: ["tauceti-queue[bot]"]}
        )
        self.assertEqual(writes, [])

    def test_a_landing_marker_the_queue_quoted_invites_nobody(self):
        # Nothing landed. The author typed `bors ping` and this PR's landing marker, naming a commit on main,
        # the queue's refusal quoted it, and the author closed and retitled the PR.
        typed = f"bors ping {marker('landed', 5)}"
        quoted = {5: [("alice", typed), ("tauceti-queue[bot]", queue_refusal(marker("landed", 5)))]}
        _, writes, _ = self.grant(closed=[self.landed(5, "alice")], comments=quoted, closers={5: ["alice"]})
        self.assertEqual(writes, [])

    def test_a_pr_merged_by_github_needs_no_further_check(self):
        _, writes, calls = self.grant(merged=[{"author": {"login": "bob"}}])
        self.assertEqual(writes, [("PUT", "bob")])
        self.assertFalse([args for args in calls if "/comments?" in " ".join(args)])

    def test_an_expired_invitation_is_cancelled_and_sent_again(self):
        _, writes, _ = self.grant(merged=[{"author": {"login": "bob"}}], invitations=[("bob", "42", True)])
        self.assertEqual(writes, [("DELETE", "42"), ("PUT", "bob")])

    def test_a_live_invitation_is_left_alone(self):
        _, writes, _ = self.grant(merged=[{"author": {"login": "bob"}}], invitations=[("bob", "42", False)])
        self.assertEqual(writes, [])

    def forgejo(self, *rows, **fields):
        document = {"schema": ca.FORGEJO_LANDERS_SCHEMA, "repository": REPO, "generated_at": time.time()}
        return {**document, "landers": [dict(row) for row in rows], **fields}

    def test_a_forgejo_lander_is_invited_by_github_id(self):
        _, writes, calls = self.grant(
            forgejo=self.forgejo({"github_id": 56166236, "forgejo_pr": 6051}),
            users={56166236: ("CBirkbeck", "User")},
        )
        self.assertEqual(writes, [("PUT", "CBirkbeck")])
        # the queue host has already confirmed the landing; nothing on GitHub is read for it
        self.assertFalse([args for args in calls if "/comments?" in " ".join(args)])

    def test_forgejo_landers_that_are_bots_unknown_or_malformed_invite_nobody(self):
        _, writes, _ = self.grant(
            forgejo=self.forgejo(
                {"github_id": 1, "forgejo_pr": 1},
                {"github_id": 2, "forgejo_pr": 2},
                {"github_id": "3", "forgejo_pr": 3},
                {"github_id": True, "forgejo_pr": 4},
                {"github_id": -5},
            ),
            users={1: ("tauceti-review", "Bot"), 3: ("mallory", "User"), 5: ("eve", "User")},
        )
        self.assertEqual(writes, [])

    def test_an_unreachable_or_foreign_attribution_is_no_one_and_github_still_counts(self):
        for answer in (
            None,
            {"schema": "other/v1", "landers": [{"github_id": 5}]},
            {"schema": ca.FORGEJO_LANDERS_SCHEMA},
        ):
            _, writes, _ = self.grant(merged=[{"author": {"login": "bob"}}], forgejo=answer, users={5: ("eve", "User")})
            self.assertEqual(writes, [("PUT", "bob")])

    def test_a_stale_or_foreign_attribution_invites_nobody(self):
        for fields in ({"generated_at": time.time() - 3 * 86400}, {"repository": "Other/Repo"}, {"generated_at": "x"}):
            _, writes, _ = self.grant(
                forgejo=self.forgejo({"github_id": 7, "forgejo_pr": 1}, **fields), users={7: ("carol", "User")}
            )
            self.assertEqual(writes, [])

    def test_an_invitation_that_reaches_another_account_is_cancelled(self):
        # carol renamed between the lookup and the invitation, and someone else took the login
        failures, writes, _ = self.grant(
            forgejo=self.forgejo({"github_id": 7, "forgejo_pr": 1}),
            users={7: ("carol", "User")},
            invitees={"carol": 8},
        )
        self.assertEqual((failures, writes), (1, [("PUT", "carol"), ("DELETE", "77")]))

    def test_a_forgejo_lander_who_already_has_access_is_left_alone(self):
        _, writes, _ = self.grant(
            forgejo=self.forgejo({"github_id": 7, "forgejo_pr": 1}),
            users={7: ("carol", "User")},
            invitations=[("carol", "9", False)],
        )
        self.assertEqual(writes, [])

    def test_a_denied_forgejo_lander_is_never_invited(self):
        _, writes, _ = self.grant(
            forgejo=self.forgejo({"github_id": 7, "forgejo_pr": 1}, {"github_id": 8, "forgejo_pr": 2}),
            users={7: ("carol", "User"), 8: ("dave", "User")},
            deny="7  # carol\n",
        )
        self.assertEqual(writes, [("PUT", "dave")])

    def test_a_denied_github_author_is_never_invited_even_after_a_rename(self):
        # mallory landed as `mallory` and has since renamed; the entry is by ID, and the comment is only a note
        _, writes, _ = self.grant(
            merged=[{"author": {"login": "mallory2"}}, {"author": {"login": "bob"}}],
            logins={"mallory2": 42, "bob": 43},
            deny="# removed for abuse\n42  # mallory\n",
        )
        self.assertEqual(writes, [("PUT", "bob")])

    def test_a_denied_author_whose_invitation_expired_is_not_invited_again(self):
        _, writes, _ = self.grant(
            merged=[{"author": {"login": "mallory"}}],
            invitations=[("mallory", "9", True)],
            logins={"mallory": 42},
            deny="42",
        )
        self.assertEqual(writes, [])

    def test_a_denied_queue_lander_is_never_invited(self):
        landed = {5: [("tauceti-queue[bot]", queue_comment("landed", 5))]}
        _, writes, _ = self.grant(closed=[self.landed(5, "alice")], comments=landed, logins={"alice": 42}, deny="42\n")
        self.assertEqual(writes, [])

    def test_an_invitation_that_reaches_a_denied_account_is_cancelled(self):
        # bob renamed between the lookup and the invitation, and a denied account took the login
        failures, writes, _ = self.grant(
            merged=[{"author": {"login": "bob"}}], logins={"bob": 43}, invitees={"bob": 42}, deny="42\n"
        )
        self.assertEqual((failures, writes), (1, [("PUT", "bob"), ("DELETE", "77")]))

    def test_an_author_whose_id_cannot_be_read_is_not_invited(self):
        _, writes, _ = self.grant(merged=[{"author": {"login": "bob"}}], logins={"bob": None})
        self.assertEqual(writes, [])

    def test_an_unreadable_deny_list_invites_nobody(self):
        for deny in ("42\nmallory\n", "0\n", "-3\n"):
            failures, writes, calls = self.grant(merged=[{"author": {"login": "bob"}}], deny=deny)
            self.assertEqual((failures, writes, calls), (1, [], []))

    def test_an_invitation_sent_by_email_is_ignored(self):
        with mock.patch.object(ca, "gh_lines", lambda args, token: ["null 7 false", "bob 8 true"]):
            self.assertEqual(ca.invitations(), {"bob": ("8", True)})


class DenyList(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "deny.txt"
            path.write_text(text)
            return ca.denied_ids(path)

    def test_ids_comments_and_blank_lines(self):
        self.assertEqual(self.read("# header\n\n12345678  # some-login\n  99\n12345678\n"), {12345678, 99})

    def test_a_login_instead_of_an_id_is_refused(self):
        with self.assertRaisesRegex(ValueError, ":2: 'mallory'"):
            self.read("1\nmallory  # 42\n")

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(OSError):
            ca.denied_ids("/nonexistent/claims-access-deny.txt")

    def test_the_committed_deny_list_reads(self):
        self.assertIsInstance(ca.denied_ids(pathlib.Path(ca.__file__).with_name("claims-access-deny.txt")), set)


if __name__ == "__main__":
    unittest.main()
