"""Native publication contracts run without Worker, Review or a Git clone.

Ordering and retry cases use the same protocol cases as the queue consumer.
"""

import copy
import hashlib
import json
import unittest
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import AmbiguousWrite, Forge, ForgeError, Incomplete, RepositoryRef
from tauceti_tools.native_publication import (
    PUB_DESCRIPTION,
    RECOVERY_CONTEXT,
    REVIEW_CONTEXT,
    NativePublicationClient,
    NativePublicationPolicy,
    NativePublishers,
    PublicationDamaged,
    PublicationSuperseded,
)
from tauceti_tools.source_identity import SourceActor


class ExistingLedger:
    """The three-method caller contract, with transaction rollback for tests."""

    def __init__(self):
        self.settings = {}

    def get_setting(self, key):
        return copy.deepcopy(self.settings.get(key))

    def set_setting(self, key, value):
        self.settings[key] = copy.deepcopy(value)

    @contextmanager
    def transaction(self):
        before = copy.deepcopy(self.settings)
        try:
            yield
        except BaseException:
            self.settings = before
            raise


class Publications(unittest.TestCase):
    def setUp(self):
        self.repository = RepositoryRef(Forge("forgejo", "https://source.example"), "team/Math")
        self.identity = RepositoryIdentity(self.repository, 41)
        self.actor = SourceActor.from_native(self.repository.forge, {"id": 7, "login": "reviewer"})
        self.queue = SourceActor.from_native(self.repository.forge, {"id": 8, "login": "queue"})
        self.publishers = NativePublishers(
            self.identity,
            frozenset({self.actor.principal}),
            frozenset({self.queue.principal}),
            frozenset({self.queue.principal}),
            generation="1" * 32,
        )
        self.policy = NativePublicationPolicy(self.publishers, source_revision="b" * 40)
        self.rows = []
        self.status_rows = []
        self.permission = "write"
        self.calls = []
        self.sequence = 0

        def request(method, path, data=None, **kwargs):
            self.calls.append((method, path, data))
            if path.endswith("/permission"):
                actor = self.queue if "/queue/" in path else self.actor
                return {
                    "user": {"id": int(actor.principal.native_id), "login": actor.login},
                    "permission": self.permission,
                }
            if method == "POST" and "/statuses/" in path:
                row = {
                    "id": 100 + len(self.status_rows),
                    "creator": self.api.current_user(),
                    "context": data["context"],
                    "status": data["state"],
                    "description": data["description"],
                }
                self.status_rows.append(row)
                return copy.deepcopy(row)
            raise AssertionError((method, path))

        def post(repository, number, body):
            row = {
                "id": 1000 + len(self.rows),
                "body": body,
                "user": self.api.current_user(),
                "created_at": "2026-11-08T00:00:00Z",
                "updated_at": "2026-11-08T00:00:00Z",
            }
            self.rows.append(row)
            return copy.deepcopy(row)

        def edit(repository, comment_id, body):
            row = next(row for row in self.rows if row["id"] == comment_id)
            row["body"] = body
            return copy.deepcopy(row)

        self.api = SimpleNamespace(
            forge=self.repository.forge,
            repository=Mock(return_value={"id": 41, "full_name": "team/Math"}),
            request=Mock(side_effect=request),
            comments=lambda *_: copy.deepcopy(self.rows),
            statuses=lambda *_: copy.deepcopy(self.statuses()),
            current_user=Mock(return_value={"id": 7, "login": "reviewer"}),
            post_comment=Mock(side_effect=post),
            edit_comment=Mock(side_effect=edit),
        )
        self.source = NativePublicationClient(self.identity, self.api, publication_policy=self.policy)
        self.store = ExistingLedger()
        self.source.configure_publisher_policy(self.store)
        self.body = (
            "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 "
            + json.dumps({"head_sha": "a" * 40, "merge_base_sha": "b" * 40, "states": {"proof": "green"}})
            + "-->"
        )

    def comment(self, *, actor=None, body=None, comment_id=1):
        actor = actor or self.actor
        return {
            "id": comment_id,
            "body": self.policy.bind_comment(body or self.body, actor),
            "user": {"id": int(actor.principal.native_id), "login": actor.login},
            "created_at": "2026-11-08T00:00:00Z",
            "updated_at": "2026-11-08T00:00:00Z",
        }

    def statuses(self):
        return [item for row in self.status_rows for item in (row if isinstance(row, list) else [row])]

    def prepare(self, *args, **kwargs):
        self.sequence += 1
        return self.source.prepare_publication(*args, sequence=kwargs.pop("sequence", self.sequence), **kwargs)

    def stamp(self, row, *, actor=None, status_id=1, kind="review"):
        actor = actor or self.actor
        prefix = (
            PUB_DESCRIPTION + f"{self.source.publication_policy.publishers.generation}:{status_id}:{status_id:032x}:"
        )
        common = {
            "context": (REVIEW_CONTEXT if kind == "review" else RECOVERY_CONTEXT) + "1",
            "creator": {"id": int(actor.principal.native_id), "login": actor.login},
            "created_at": "2026-11-08T00:00:00Z",
        }
        body_hash = hashlib.sha256(row["body"].encode()).hexdigest()
        return [
            dict(common, id=status_id * 2, status="pending", description=prefix + "pending:" + body_hash),
            dict(common, id=status_id * 2 + 1, status="success", description=prefix + str(row["id"]) + ":" + body_hash),
        ]

    def read(self):
        return self.source.read_publications(1, "a" * 40, self.statuses())

    def test_a_new_client_needs_its_persisted_policy_before_reading_or_publishing(self):
        fresh = NativePublicationClient(self.identity, self.api, publication_policy=self.policy)
        with self.assertRaises(Incomplete):
            fresh.prepare_publication("team/Math", 1, "a" * 40, self.body, "review", sequence=1)
        with self.assertRaises(Incomplete):
            fresh.read_publications(1, "a" * 40)
        fresh.configure_publisher_policy(self.store)
        self.assertEqual(fresh.read_publications(1, "a" * 40), ((), "absent", "absent"))

    def test_publisher_rotation_cannot_reuse_an_authority_generation(self):
        changed = replace(self.publishers, generation="2" * 32)
        fresh = NativePublicationClient(
            self.identity, self.api, publication_policy=NativePublicationPolicy(changed, source_revision="c" * 40)
        )
        self.assertTrue(fresh.configure_publisher_policy(self.store))
        with self.assertRaisesRegex(ValueError, "never reused"):
            self.source.configure_publisher_policy(self.store)

    def test_origin_repository_and_producer_revision_are_explicit(self):
        with self.assertRaises(ValueError):
            NativePublicationPolicy(self.publishers, source_revision="main")
        with self.assertRaises(ValueError):
            NativePublicationClient(replace(self.identity, native_id=42), self.api, publication_policy=self.policy)
        other = SimpleNamespace(forge=Forge("forgejo", "https://other.example"))
        with self.assertRaises(ValueError):
            NativePublicationClient(self.identity, other, publication_policy=self.policy)
        self.api.repository.return_value = {"id": 42, "full_name": self.repository.name}
        with self.assertRaises(Incomplete):
            self.source.read_publications(1, "a" * 40)

    def test_publication_retry_observes_pending_without_resending_the_intent(self):
        intent = self.prepare("team/Math", 1, "a" * 40, self.body, "review-marker")
        original = self.api.post_comment.side_effect

        def lose(*args):
            original(*args)
            raise AmbiguousWrite("lost accepted comment response")

        self.api.post_comment.side_effect = lose
        with self.assertRaises(AmbiguousWrite):
            self.source.publish(intent)
        self.assertEqual(self.read()[1], "pending")
        self.api.post_comment.side_effect = original
        self.assertEqual(self.source.publish(intent), "1000")
        self.assertEqual(len(self.status_rows), 2)
        self.assertEqual(self.api.post_comment.call_count, 1)
        self.assertEqual(self.source.publish(intent), "1000")
        self.assertEqual(len(self.status_rows), 2)

    def test_older_frozen_intent_cannot_overwrite_a_newer_publication(self):
        first = self.prepare("team/Math", 1, "a" * 40, self.body, "first")
        self.source.publish(first)
        second = self.prepare("team/Math", 1, "a" * 40, self.body + "\nNew verdict.", "second")
        self.source.publish(second)
        with self.assertRaises(PublicationSuperseded):
            self.source.publish(first)
        self.assertEqual(len(self.status_rows), 4)

    def test_intent_that_never_started_cannot_overwrite_a_newer_verdict(self):
        old = self.prepare("team/Math", 1, "a" * 40, self.body, "same")
        new = self.prepare("team/Math", 1, "a" * 40, self.body + "\nNew", "same")
        newest = self.source.publish(new)
        with self.assertRaises(PublicationSuperseded):
            self.source.publish(old)
        self.assertEqual(str(self.read()[0][0]["id"]), newest)

    def test_offline_intents_delivered_in_order_keep_the_newer_red_verdict(self):
        first = self.prepare("team/Math", 1, "a" * 40, self.body, "review")
        second = self.prepare("team/Math", 1, "a" * 40, self.body.replace("green", "blocking_request"), "review")
        self.api.repository.side_effect = ForgeError("outage before registration", status=503)
        with self.assertRaises(ForgeError):
            self.source.publish(first)
        self.api.repository.side_effect = None
        self.source.publish(first)
        expected = self.source.publish(second)
        self.assertEqual(str(self.read()[0][0]["id"]), expected)
        self.assertIn("blocking_request", self.read()[0][0]["body"])

    def test_offline_init_and_final_can_both_be_prepared_before_either_is_sent(self):
        init = self.prepare(
            "team/Math", 1, "a" * 40, self.body.replace('"states":', '"mode":"init","states":'), "review"
        )
        final = self.prepare("team/Math", 1, "a" * 40, self.body, "review")
        self.source.publish(init)
        self.source.publish(final)
        self.assertNotIn('"mode":"init"', self.read()[0][0]["body"])

    def test_equal_sequences_with_different_intents_fail_closed(self):
        first = self.prepare("team/Math", 1, "a" * 40, self.body, "review", sequence=1)
        second = self.prepare(
            "team/Math", 1, "a" * 40, self.body.replace("green", "blocking_request"), "review", sequence=1
        )
        self.source.publish(first)
        with self.assertRaises(Incomplete):
            self.source.publish(second)

    def test_damaged_completed_intent_requires_republication_without_repeating_inference(self):
        intent = self.prepare("team/Math", 1, "a" * 40, self.body, "review")
        original_id = self.source.publish(intent)
        self.rows[0]["body"] = "Moderated"
        with self.assertRaises(PublicationDamaged):
            self.source.publish(intent)
        replacement = self.prepare("team/Math", 1, "a" * 40, self.body, "review")
        self.assertNotEqual(self.source.publish(replacement), original_id)
        self.assertEqual(self.read()[1], "success")

    def test_a_frozen_clear_cannot_erase_a_newer_recovery_hold(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        first = self.prepare("team/Math", 1, "a" * 40, "First hold", "hold", kind="recovery")
        self.source.publish(first)
        clear = self.prepare("team/Math", 1, "a" * 40, "", "clear", kind="recovery", clear=True)
        newest = self.prepare("team/Math", 1, "a" * 40, "New hold", "hold", kind="recovery")
        self.source.publish(newest)
        with self.assertRaises(PublicationSuperseded):
            self.source.publish(clear)
        self.assertEqual(self.read()[2], "success")

    def test_new_intents_with_identical_bodies_can_follow_a_recovery_clear(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        first = self.prepare("team/Math", 1, "a" * 40, "Hold", "same", kind="recovery")
        self.source.publish(first)
        self.source.publish(self.prepare("team/Math", 1, "a" * 40, "", "clear", kind="recovery", clear=True))
        again = self.prepare("team/Math", 1, "a" * 40, "Hold", "same", kind="recovery")
        self.assertNotEqual(first["nonce"], again["nonce"])
        self.source.publish(again)
        self.assertEqual(self.read()[2], "success")

    def test_concurrent_publisher_cannot_edit_the_newer_intents_comment(self):
        first = self.prepare("team/Math", 1, "a" * 40, self.body, "shared")
        original = self.source._upsert_comment
        nested = []

        def interleave(number, full, marker, actor):
            second = self.prepare("team/Math", 1, "a" * 40, self.body + "\nNewest", "shared")
            with patch.object(self.source, "_upsert_comment", side_effect=original):
                nested.append(self.source.publish(second))
            return original(number, full, marker, actor)

        with patch.object(self.source, "_upsert_comment", side_effect=interleave):
            with self.assertRaises(PublicationSuperseded):
                self.source.publish(first)
        self.assertEqual(str(self.read()[0][0]["id"]), nested[0])
        self.assertIn("Newest", self.read()[0][0]["body"])
        self.api.edit_comment.assert_not_called()

    def test_late_success_after_newer_pending_cannot_revive_an_older_review(self):
        first = self.prepare("team/Math", 1, "a" * 40, self.body, "shared")
        original = self.api.request.side_effect
        nested = []

        def interleave(method, path, data=None, **kwargs):
            if (
                method == "POST"
                and data
                and data.get("description", "").startswith(self.source._intent_prefix(first))
                and data["state"] == "success"
            ):
                second = self.prepare("team/Math", 1, "a" * 40, self.body + "\nNewest", "shared")
                with patch.object(self.api.request, "side_effect", original):
                    nested.append(self.source.publish(second))
            return original(method, path, data, **kwargs)

        self.api.request.side_effect = interleave
        with self.assertRaises(PublicationSuperseded):
            self.source.publish(first)
        self.assertEqual(str(self.read()[0][0]["id"]), nested[0])

    def test_competing_pending_registration_cannot_acquire_a_stale_predecessor(self):
        first = self.prepare("team/Math", 1, "a" * 40, self.body, "shared")
        second = self.prepare("team/Math", 1, "a" * 40, self.body + "\nNewest", "shared")
        original = self.api.request.side_effect
        nested = []

        def interleave(method, path, data=None, **kwargs):
            if method == "POST" and data and data.get("description", "").startswith(self.source._intent_prefix(first)):
                with patch.object(self.api.request, "side_effect", original):
                    nested.append(self.source.publish(second))
            return original(method, path, data, **kwargs)

        self.api.request.side_effect = interleave
        with self.assertRaises(PublicationSuperseded):
            self.source.publish(first)
        self.assertEqual(str(self.read()[0][0]["id"]), nested[0])

    def test_publisher_reconciliation_does_not_require_other_user_admin_lookup(self):
        intent = self.prepare("team/Math", 1, "a" * 40, self.body, "review")
        expected = self.source.publish(intent)
        with patch.object(self.source, "_permission", side_effect=ForgeError("forbidden", status=403)):
            self.assertEqual(self.source.publish(intent), expected)

    def test_restored_history_with_an_entirely_lost_recovery_account_still_denies(self):
        missing_actor = SourceActor.from_native(self.repository.forge, {"id": 99, "login": "retired-bot"})
        record = self.comment(actor=missing_actor)
        old = self.stamp(record, actor=missing_actor, kind="recovery")
        for status in old:
            status["description"] = status["description"].replace(self.publishers.generation, "f" * 32)
        self.status_rows = [old]
        self.assertEqual(self.read()[2], "revoked")

    def test_retired_writer_rows_after_a_current_explicit_clear_cannot_reimpose_a_hold(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        clear = self.prepare("team/Math", 1, "a" * 40, "", "clear", kind="recovery", clear=True)
        self.source.publish(clear)
        outsider = SourceActor.from_native(self.repository.forge, {"id": 99, "login": "writer"})
        old = self.stamp(self.comment(actor=outsider), actor=outsider, status_id=2, kind="recovery")
        for index, row in enumerate(old):
            row["id"] = 1000 + index
            row["description"] = row["description"].replace(self.publishers.generation, "f" * 32)
        self.status_rows.append(old)
        self.assertEqual(self.read()[2], "clear")

    def test_unverified_barrier_preserves_newer_trusted_hold_over_an_old_clear(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        old_clear = self.prepare("team/Math", 1, "a" * 40, "", "clear", kind="recovery", clear=True, sequence=5)
        hold = self.prepare("team/Math", 1, "a" * 40, "Newer hold", "hold", kind="recovery", sequence=9)
        self.source.publish(hold)
        self.status_rows.append(
            {
                "id": 1000,
                "context": RECOVERY_CONTEXT + "1",
                "status": "pending",
                "creator": None,
                "description": PUB_DESCRIPTION + self.publishers.generation + ":junk",
            }
        )
        with self.assertRaises(PublicationSuperseded) as refused:
            self.source.publish(old_clear)
        self.assertEqual(refused.exception.sequence, 9)
        self.assertEqual(self.read()[2], "revoked")
        original = self.api.request.side_effect

        def native_ids(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0] == "POST" and "/statuses/" in args[1]:
                self.status_rows[-1]["id"] = 2000 + len(self.status_rows)
            return result

        self.api.request.side_effect = native_ids
        self.source.publish(hold)  # Exact highest intent may reconcile its own registration after the barrier.
        self.assertEqual(self.read()[2], "success")
        with self.assertRaises(PublicationSuperseded):
            self.source.publish(old_clear)

    def test_unverified_barrier_never_revives_old_green_over_a_newer_red(self):
        old = self.prepare("team/Math", 1, "a" * 40, self.body, "green", sequence=5)
        red = self.prepare("team/Math", 1, "a" * 40, self.body.replace("green", "blocking_request"), "red", sequence=6)
        self.source.publish(red)
        self.status_rows.append(
            {
                "id": 1000,
                "context": REVIEW_CONTEXT + "1",
                "status": "pending",
                "creator": None,
                "description": PUB_DESCRIPTION + self.publishers.generation + ":junk",
            }
        )
        with self.assertRaises(PublicationSuperseded) as refused:
            self.source.publish(old)
        self.assertEqual(refused.exception.sequence, 6)
        self.assertEqual(self.read()[1], "revoked")

    def test_barriers_during_registration_or_completion_leave_the_exact_clear_retryable(self):
        self.api.current_user.return_value = {"id": 8, "login": "queue"}
        original = self.api.request.side_effect
        for stage in ["pending", "success"]:
            with self.subTest(stage=stage):
                clear = self.prepare("team/Math", 1, "a" * 40, "", "clear", kind="recovery", clear=True)
                injected = []

                def insert_barrier(*args, stage=stage, injected=injected, **kwargs):
                    value = original(*args, **kwargs)
                    if args[0] == "POST" and "/statuses/" in args[1] and args[2]["state"] == stage and not injected:
                        injected.append(True)
                        self.status_rows.append(
                            {
                                "id": 100 + len(self.status_rows),
                                "context": RECOVERY_CONTEXT + "1",
                                "status": "pending",
                                "creator": None,
                                "description": PUB_DESCRIPTION + self.publishers.generation + ":junk",
                            }
                        )
                    return value

                self.api.request.side_effect = insert_barrier
                with self.assertRaises(Incomplete):
                    self.source.publish(clear)
                self.assertEqual(self.read()[2], "revoked")
                self.api.request.side_effect = original
                self.assertEqual(self.source.publish(clear), "clear")
                self.assertEqual(self.read()[2], "clear")
