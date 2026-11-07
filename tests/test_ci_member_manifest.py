"""Large batches keep exact membership outside the bounded GitHub dispatch body."""

import copy
import hashlib
import unittest
from dataclasses import replace

from tauceti_tools import ci_candidate as cc
from tauceti_tools import ci_candidate_workflow as cw
from tests.test_ci_candidate import context, policy, request, run


def retained_request(count=1197):
    config = policy()
    req = request(config)
    members = tuple(cc.Member(number, f"{number:040x}") for number in range(1, count + 1))
    raw = cc.member_manifest_bytes(req.epoch, req.request_id, members)
    manifest = cc.MemberManifest("9" * 40, hashlib.sha256(raw).hexdigest(), count, len(raw))
    return config, replace(req, members=members, member_manifest=manifest), raw


class MemberManifests(unittest.TestCase):
    def test_large_batch_round_trip_keeps_request_and_result_small_without_truncation(self):
        for count in (1197, 2394):
            with self.subTest(count=count):
                config, original, raw = retained_request(count)
                self.assertGreater(len(raw), cc.MAX_REQUEST_BYTES)
                self.assertLess(len(cc.canonical(original.dispatch())), 4096)
                incoming = cc.CandidateRequest.parse(original.record(), policy=config)
                with self.assertRaisesRegex(ValueError, "not been verified"):
                    _ = incoming.accepted_members
                bound = incoming.bind_members(raw)
                self.assertEqual(len(bound.accepted_members), count)
                self.assertEqual(bound.accepted_members, original.members)
                self.assertEqual(bound.digest, original.digest)
                self.assertEqual(incoming.digest, original.digest)
                self.assertEqual(bound.manifest_ref, f"refs/heads/tauceti-ci/{bound.epoch}/{bound.request_id}/members")
                with self.assertRaisesRegex(ValueError, "manifest parents"):
                    bound.member_ref(bound.accepted_members[-1])
                self.assertEqual(bound.accepted_heads, tuple(member.head for member in bound.accepted_members))
                result = cc.make_result(
                    incoming,
                    config,
                    context(incoming, config),
                    tested_commit=incoming.head,
                    tested_tree=incoming.tree,
                    verdicts=dict.fromkeys(cc.VERDICTS, "success"),
                )
                self.assertLess(len(cc.canonical(result)), 8192)
                cc.verify_result(original, config, result, run(original, config), run_id=101, attempt=2)

    def test_manifest_parents_retain_exact_distinct_heads_and_large_lint_uses_no_api(self):
        config, req, _raw = retained_request()
        header = b"tree " + b"1" * 40 + b"\n" + b"\n".join(b"parent " + h.encode() for h in req.accepted_heads)
        cw.check_member_parents(req, header)
        for invalid in (
            b"tree " + b"1" * 40,
            header + b"\nparent " + b"f" * 40,
            b"tree " + b"1" * 40 + b"\n" + b"\n".join(b"parent " + h.encode() for h in reversed(req.accepted_heads)),
        ):
            with self.assertRaises(ValueError):
                cw.check_member_parents(req, invalid)
        from unittest.mock import Mock

        client = Mock()
        self.assertFalse(cw.native_lint_policy(req, config, context(req, config), client=client))
        client.request.assert_not_called()

    def test_digest_count_nonce_schema_shape_and_accepted_head_mismatches_fail_closed(self):
        config, req, raw = retained_request()
        incoming = cc.CandidateRequest.parse(req.record(), policy=config)
        with self.assertRaises(ValueError):
            incoming.bind_members(raw + b"\n")
        value = cc.read_json(raw, max_bytes=cc.MAX_MEMBER_MANIFEST_BYTES)
        for key, wrong in (
            ("epoch", "8" * 32),
            ("request_id", "8" * 32),
            ("schema", "other"),
            ("members", []),
            ("members", value["members"][:-1]),
            ("members", [value["members"][0]] * len(value["members"])),
        ):
            modified = dict(value, **{key: wrong})
            candidate = cc.canonical(modified)
            descriptor = replace(req.member_manifest, sha256=hashlib.sha256(candidate).hexdigest(), size=len(candidate))
            with self.subTest(key=key, wrong=str(wrong)[:80]), self.assertRaises(ValueError):
                replace(incoming, member_manifest=descriptor).bind_members(candidate)
        changed = copy.deepcopy(value)
        changed["members"][0]["head"] = "f" * 40
        with self.assertRaises(ValueError):
            incoming.bind_members(cc.canonical(changed))
        with self.assertRaises(ValueError):
            replace(req, members=req.members[:-1])

    def test_bounds_and_canonical_shape_cannot_be_replaced_by_payload_urls_or_refs(self):
        config, req, raw = retained_request()
        for key, wrong in (
            ("count", True),
            ("size", 0),
            ("count", cc.MAX_RETAINED_MEMBERS + 1),
            ("size", cc.MAX_MEMBER_MANIFEST_BYTES + 1),
            ("commit", "0" * 40),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                replace(req.member_manifest, **{key: wrong})
        for key in ("url", "ref", "path", "credential_origin"):
            bad = req.record()
            bad["members"]["manifest"][key] = "https://attacker.invalid/members"
            with self.subTest(key=key), self.assertRaises(ValueError):
                cc.CandidateRequest.parse(bad, policy=config)
        with self.assertRaises(ValueError):
            replace(req, member_manifest=None)
        with self.assertRaises(ValueError):
            cc.member_manifest_bytes(req.epoch, req.request_id, ())
        # The descriptor identifies exact bytes, and the encoding is canonical.
        padded = raw + b" "
        incoming = cc.CandidateRequest.parse(req.record(), policy=config)
        with self.assertRaisesRegex(ValueError, "canonical"):
            replace(
                incoming,
                member_manifest=replace(
                    req.member_manifest, sha256=hashlib.sha256(padded).hexdigest(), size=len(padded)
                ),
            ).bind_members(padded)


if __name__ == "__main__":
    unittest.main()
