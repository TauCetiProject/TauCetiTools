#!/usr/bin/env python3
"""Selection, fixed targets, and publication recovery without network or credentials."""

import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bump_latest as bl

OLD, NEW, TIP = (c * 40 for c in "abc")
# Newest first, as `git rev-list --first-parent` prints them: TIP, TAG2, MID, TAG1, NEXT, then OLD.
NEXT, TAG1, MID, TAG2 = (c * 40 for c in "d123")


def run(target, **kwargs):
    return (
        dict(
            head_sha=target,
            id=42,
            html_url="https://example.org/build/42",
            event="push",
            head_branch="master",
            status="completed",
            conclusion="success",
            head_repository={"full_name": bl.MATHLIB},
        )
        | kwargs
    )


def pr(target=NEW, **kwargs):
    return (
        dict(
            number=1,
            user={"type": "Bot"},
            head={"sha": target, "ref": "bump-mathlib/fix-test", "repo": {"full_name": bl.REPO}},
            base={"ref": "main"},
            labels=[],
        )
        | kwargs
    )


class BumpTests(unittest.TestCase):
    def test_history_order_beats_completion_order_and_deduplicates(self):
        self.assertEqual(
            bl.ordered_runs([run(NEW), run(TIP), run(TIP)], [TIP, NEW]), [run(TIP), run(NEW)]
        )
        for changes in [
            dict(event="workflow_dispatch"),
            dict(head_branch="staging"),
            dict(status="in_progress"),
            dict(conclusion="failure"),
            dict(head_repository={"full_name": "attacker/mathlib"}),
        ]:
            self.assertEqual(bl.ordered_runs([run(TIP, **changes)], [TIP]), [])

    def test_manifest_derivation_matches_the_existing_guard(self):
        fixtures = Path(__file__).with_name("bump_manifest_fixtures")
        base, upstream = (
            json.loads((fixtures / name).read_text()) for name in ["base.json", "mathlib.json"]
        )
        before = copy.deepcopy(base)
        proposed = bl.derive_manifest(base, upstream, NEW)
        self.assertEqual(base, before)
        self.assertEqual(bl.pin(proposed), NEW)
        self.assertEqual(bl.problems(proposed, upstream, base), [])
        self.assertTrue(all(p["inherited"] for p in proposed["packages"] if p["name"] != "mathlib"))

    def test_fixed_target_holds_and_obsolete_closure(self):
        data = {
            "content": base64.b64encode(
                json.dumps({"packages": [{"name": "mathlib", "rev": NEW}]}).encode()
            ).decode()
        }
        with (
            patch.object(bl, "api", return_value=data),
            patch.object(bl, "relationship", return_value="ahead"),
        ):
            self.assertEqual(bl.bump_state(OLD, [pr()]), ([pr()], []))
        with (
            patch.object(bl, "api", return_value=data),
            patch.object(bl, "relationship", return_value="behind"),
        ):
            self.assertEqual(bl.bump_state(TIP, [pr()]), ([], [pr()]))
        held = pr(labels=[{"name": "hold"}])
        with patch.object(bl, "api", side_effect=AssertionError("held target must not be changed")):
            self.assertEqual(bl.bump_state(TIP, [held]), ([held], []))

    def test_divergence_does_not_authorize_obsolete_closure(self):
        with patch.object(bl, "api", return_value={"status": "diverged"}):
            with self.assertRaises(RuntimeError):
                bl.relationship(OLD, NEW)

    def test_partial_cache_only_is_a_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "mathlib"
            checkout.mkdir()
            (checkout / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
            for warning in (
                "Warning: some files were not found in the cache.",
                "Warning: 17 file(s) were not found in the cache.",
            ):
                with (
                    self.subTest(warning=warning),
                    patch.object(bl, "command", side_effect=["", "", warning]) as cmd,
                ):
                    self.assertFalse(bl.verify_cache(checkout, NEW))
                    self.assertTrue(cmd.call_args.kwargs["combined"])
                    environment = cmd.call_args.kwargs["env"]
                    self.assertNotIn("GH_TOKEN", environment)
                    self.assertNotIn("GITHUB_TOKEN", environment)
            with patch.object(bl, "command", side_effect=RuntimeError("network down")):
                with self.assertRaisesRegex(RuntimeError, "network down"):
                    bl.verify_cache(checkout, NEW)
            with patch.object(bl, "command", side_effect=["", "", "downloaded", ""]) as cmd:
                self.assertTrue(bl.verify_cache(checkout, NEW))
                self.assertIn("--rehash", cmd.call_args.args[0])

    def test_release_tags_peel_annotated_tags_and_ignore_other_names(self):
        listing = "\n".join(
            [
                f"{TAG1}\trefs/tags/v4.36.0-rc1",
                f"{MID}\trefs/tags/v4.36.0-rc2",
                f"{TAG2}\trefs/tags/v4.36.0-rc2^{{}}",
                f"{TIP}\trefs/tags/v4.36.0-nightly",
                f"{NEXT}\trefs/tags/vfoo",
            ]
        )
        with patch.object(bl, "command", return_value=listing) as cmd:
            self.assertEqual(bl.release_tags(), {TAG1: ["v4.36.0-rc1"], TAG2: ["v4.36.0-rc2"]})
            self.assertEqual(cmd.call_args.args[0][:3], ["git", "ls-remote", "--tags"])

    NEWER = [TIP, TAG2, MID, TAG1, NEXT]
    TAGS = {TAG1: ["v4.36.0-rc1"], TAG2: ["v4.36.0-rc2"]}

    def runs(self):
        return [run(c) for c in self.NEWER]

    def test_the_oldest_release_tag_beats_a_newer_cached_commit(self):
        probed = []
        chosen = bl.choose(
            self.runs(), self.NEWER, self.TAGS, lambda c: probed.append(c) or True, lambda c: 0
        )
        self.assertEqual(chosen, run(TAG1))
        self.assertEqual(probed, [TAG1])

    def test_an_unselectable_young_release_waits_rather_than_bumping_around_it(self):
        for runs, verify in [
            ([run(c) for c in self.NEWER if c != TAG1], lambda c: True),  # build unfinished
            (self.runs(), lambda c: c != TAG1),  # cache incomplete
        ]:
            with self.subTest():
                self.assertIsNone(bl.choose(runs, self.NEWER, self.TAGS, verify, lambda c: 3600))

    def test_an_unselectable_old_release_is_stepped_over_to_the_next_one(self):
        chosen = bl.choose(
            self.runs(), self.NEWER, self.TAGS, lambda c: c != TAG1,
            lambda c: bl.RELEASE_GRACE_SECONDS,
        )
        self.assertEqual(chosen, run(TAG2))

    def test_after_every_release_is_stepped_over_the_newest_cache_wins(self):
        chosen = bl.choose(
            self.runs(), self.NEWER, self.TAGS, lambda c: c not in self.TAGS,
            lambda c: bl.RELEASE_GRACE_SECONDS,
        )
        self.assertEqual(chosen, run(TIP))

    def test_without_a_release_in_range_the_newest_cache_wins(self):
        self.assertEqual(bl.choose(self.runs(), self.NEWER, {}, lambda c: True, lambda c: 0), run(TIP))
        self.assertEqual(
            bl.choose(self.runs(), self.NEWER, {OLD: ["v4.35.0"]}, lambda c: True, lambda c: 0), run(TIP)
        )

    def test_selector_stops_at_a_release_tag_on_master(self):
        def cmd(args, **kwargs):
            if "rev-list" in args:
                return "\n".join([*self.NEWER, OLD])
            if "ls-remote" in args:
                return f"{TAG1}\trefs/tags/v4.36.0-rc1"
            if "show" in args:
                raise AssertionError("a selectable release needs no age check")
            return ""

        with (
            patch.object(bl, "command", side_effect=cmd),
            patch.object(bl, "api", return_value={"workflow_runs": self.runs()}),
            patch.object(bl, "verify_cache", return_value=True) as probe,
        ):
            self.assertEqual(bl.select_target(OLD), run(TAG1))
            self.assertEqual([c.args[1] for c in probe.call_args_list], [TAG1])


    def test_selector_skips_missing_cache_but_not_transport_failures(self):
        def cmd(args, **kwargs):
            return "\n".join([TIP, NEW, OLD]) if "rev-list" in args else ""

        with (
            patch.object(bl, "command", side_effect=cmd),
            patch.object(bl, "api", return_value={"workflow_runs": [run(NEW), run(TIP)]}),
        ):
            with patch.object(bl, "verify_cache", side_effect=[False, True]) as probe:
                self.assertEqual(bl.select_target(OLD), run(NEW))
                self.assertEqual([c.args[1] for c in probe.call_args_list], [TIP, NEW])
            with patch.object(bl, "verify_cache", side_effect=RuntimeError("API failure")) as probe:
                with self.assertRaises(RuntimeError):
                    bl.select_target(OLD)
                self.assertEqual(probe.call_count, 1)

    def test_publication_preserves_active_repairs(self):
        with (
            patch.object(bl, "command", return_value=OLD) as cmd,
            patch.object(
                bl.Path,
                "read_text",
                return_value=json.dumps({"packages": [{"name": "mathlib", "rev": OLD}]}),
            ),
            patch.object(bl, "open_bumps", return_value=[pr()]),
            patch.object(bl, "bump_state", return_value=([pr()], [])),
            patch.object(bl, "api", side_effect=AssertionError("no candidate mutation")),
        ):
            bl.publish(NEW, 42)
            self.assertEqual(cmd.call_count, 1)

    def test_closed_pr_is_not_reopened(self):
        with (
            patch.object(bl, "command", return_value=OLD),
            patch.object(
                bl.Path,
                "read_text",
                return_value=json.dumps({"packages": [{"name": "mathlib", "rev": OLD}]}),
            ),
            patch.object(bl, "open_bumps", return_value=[]),
            patch.object(bl, "bump_state", return_value=([], [])),
            patch.object(bl, "relationship", return_value="ahead"),
            patch.object(bl, "api", side_effect=[run(NEW), [{"number": 9, "state": "closed"}]]),
        ):
            bl.publish(NEW, 42)

    def test_new_hold_prevents_obsolete_closure(self):
        held = pr(state="open", labels=[{"name": "hold"}])
        with (
            patch.object(bl, "command", return_value=OLD) as commands,
            patch.object(
                bl.Path,
                "read_text",
                return_value=json.dumps({"packages": [{"name": "mathlib", "rev": TIP}]}),
            ),
            patch.object(bl, "open_bumps", return_value=[pr()]),
            patch.object(bl, "bump_state", side_effect=[([], [pr()]), ([held], [])]),
            patch.object(bl, "api", return_value=held),
        ):
            bl.publish(NEW, 42)
            self.assertFalse(
                any(c.args[0][:3] == ["gh", "pr", "close"] for c in commands.call_args_list)
            )

    def test_existing_repair_branch_is_never_overwritten(self):
        fixtures = Path(__file__).with_name("bump_manifest_fixtures")
        base, upstream = (
            json.loads((fixtures / name).read_text()) for name in ["base.json", "mathlib.json"]
        )
        existing = {
            "content": base64.b64encode(
                json.dumps({"packages": [{"name": "mathlib", "rev": NEW}]}).encode()
            ).decode()
        }

        def cmd(args, **kwargs):
            return (
                OLD
                if args[:3] == ["git", "rev-parse", "HEAD"]
                else (NEW + "\tbranch" if "ls-remote" in args else "")
            )

        with (
            patch.object(bl, "command", side_effect=cmd) as commands,
            patch.object(bl.Path, "read_text", return_value=json.dumps(base)),
            patch.object(bl, "open_bumps", return_value=[]),
            patch.object(bl, "bump_state", return_value=([], [])),
            patch.object(bl, "relationship", return_value="ahead"),
            patch.object(bl, "api", side_effect=[run(NEW), [], existing]),
            patch.object(
                bl,
                "upstream_file",
                side_effect=[json.dumps(upstream), "leanprover/lean4:v4.35.0\n"],
            ),
            patch.object(bl.Path, "write_text"),
            patch.object(bl, "release_tags", return_value={NEW: ["v4.36.0-rc2"]}),
        ):
            bl.publish(NEW, 42)
            self.assertFalse(any("push" in c.args[0] for c in commands.call_args_list))
            created = [c.args[0] for c in commands.call_args_list if c.args[0][:3] == ["gh", "pr", "create"]]
            self.assertEqual(len(created), 1)
            title = created[0][created[0].index("--title") + 1]
            self.assertEqual(title, f"chore: bump Mathlib to v4.36.0-rc2 ({NEW[:8]})")


if __name__ == "__main__":
    unittest.main()
