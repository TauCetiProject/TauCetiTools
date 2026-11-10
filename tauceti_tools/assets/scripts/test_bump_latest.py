#!/usr/bin/env python3
"""Selection, fixed targets, and publication recovery without network or credentials."""

import base64
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def content(target):
    return {
        "content": base64.b64encode(
            json.dumps({"packages": [{"name": "mathlib", "rev": target}]}).encode()
        ).decode()
    }


OLD_HEAD, NEW_HEAD = (c * 40 for c in "ef")


class FakeGitHub:
    """Routes the GitHub reads a retarget makes. The old bump #7 targets NEW."""

    def __init__(self, age_hours=bl.BUMP_RETARGET_HOURS + 1, labels=(), existing=(), carried="ahead"):
        self.old = pr(
            number=7,
            state="open",
            head={"sha": OLD_HEAD, "ref": f"bump-mathlib/{NEW}", "repo": {"full_name": bl.REPO}},
            labels=[{"name": name} for name in labels],
        )
        self.new = pr(
            number=8,
            state="open",
            head={"sha": NEW_HEAD, "ref": f"bump-mathlib/{TIP}", "repo": {"full_name": bl.REPO}},
            body="Bump\n\n<!-- tauceti-retarget-from: #7 -->\n",
        )
        self.age_hours = age_hours
        self.existing = list(existing)
        self.carried = carried
        self.calls = []

    def api(self, path):
        self.calls.append(path)
        if path == f"repos/{bl.REPO}/pulls/7":
            return self.old
        if path.startswith(f"repos/{bl.REPO}/contents/lake-manifest.json?ref="):
            ref = path.split("ref=")[1]
            return content(NEW if ref == OLD_HEAD else TIP)
        if path.startswith(f"repos/{bl.MATHLIB}/git/commits/"):
            stamp = bl.time.time() - self.age_hours * 3600
            return {"committer": {"date": datetime.fromtimestamp(stamp, timezone.utc).isoformat()}}
        if path.startswith(f"repos/{bl.MATHLIB}/actions/runs/"):
            return run(TIP)
        if path.startswith(f"repos/{bl.REPO}/pulls?state=all&head="):
            return self.existing
        if path.startswith(f"repos/{bl.REPO}/pulls?state=open&head="):
            return [self.new]
        if path.startswith(f"repos/{bl.REPO}/compare/"):
            return {"status": self.carried}
        raise AssertionError(f"unexpected read {path}")


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

    def selector_commands(self, tags="", entries=(), toolchains=None):
        def cmd(args, **kwargs):
            if "rev-list" in args:
                return "\n".join([*self.NEWER, OLD])
            if "ls-remote" in args:
                return tags
            if args[:2] == ["git", "log"]:
                return "\n".join(entries)
            if args[:2] == ["git", "show"] and args[2].endswith(":lean-toolchain"):
                return (toolchains or {})[args[2].split(":")[0]]
            if args[:2] == ["git", "show"]:
                raise AssertionError("a selectable release needs no age check")
            return ""

        return cmd

    def test_selector_stops_at_a_toolchain_entry_before_its_tag_exists(self):
        toolchains = {NEXT: "leanprover/lean4:nightly-2026-11-01\n",
                      TAG1: "leanprover/lean4:v4.36.0-rc1\n",
                      TAG2: "leanprover/lean4:v4.36.0-rc1\n"}
        listing = self.runs()[:1]  # a busy master: the listing misses older successes
        direct = {c: [run(c)] for c in self.NEWER}

        def api(path):
            if "head_sha=" in path:
                return {"workflow_runs": direct[path.split("head_sha=")[1].split("&")[0]]}
            return {"workflow_runs": listing}

        with (
            patch.object(bl, "command", side_effect=self.selector_commands(
                entries=[NEXT, TAG1, TAG2], toolchains=toolchains)),
            patch.object(bl, "api", side_effect=api),
            patch.object(bl, "verify_cache", return_value=True) as probe,
            patch.object(bl, "declined", return_value=False),
        ):
            self.assertEqual(bl.select_target(OLD), run(TAG1))
            self.assertEqual([c.args[1] for c in probe.call_args_list], [TAG1])

    def test_selector_stops_at_a_release_tag_on_master(self):
        with (
            patch.object(bl, "command", side_effect=self.selector_commands(
                tags=f"{TAG1}\trefs/tags/v4.36.0-rc1")),
            patch.object(bl, "api", return_value={"workflow_runs": self.runs()}),
            patch.object(bl, "verify_cache", return_value=True) as probe,
            patch.object(bl, "declined", return_value=False),
        ):
            self.assertEqual(bl.select_target(OLD), run(TAG1))
            self.assertEqual([c.args[1] for c in probe.call_args_list], [TAG1])

    def test_a_release_whose_bump_was_closed_unmerged_is_stepped_over(self):
        chosen = bl.choose(
            self.runs(), self.NEWER, self.TAGS, lambda c: True, lambda c: 0,
            refused=lambda c: c == TAG1,
        )
        self.assertEqual(chosen, run(TAG2))

    def test_only_an_unmerged_closure_declines_a_release(self):
        for prs, expected in [([], False), ([{"merged_at": "2026-11-01T00:00:00Z"}], False),
                              ([{"merged_at": None}], True)]:
            with self.subTest(prs=prs), patch.object(bl, "api", return_value=prs) as api:
                self.assertEqual(bl.declined(TAG1), expected)
                self.assertIn(f"head=TauCetiProject:bump-mathlib/{TAG1}", api.call_args.args[0])

    def test_toolchain_entries_keep_the_first_commit_per_release(self):
        toolchains = {NEXT: "leanprover/lean4:nightly-2026-11-01", TAG1: "leanprover/lean4:v4.36.0-rc1",
                      MID: "leanprover/lean4:v4.36.0-rc1", TAG2: "leanprover/lean4:v4.36.0-rc2"}
        with patch.object(bl, "command", side_effect=self.selector_commands(
                entries=[NEXT, TAG1, MID, TAG2], toolchains=toolchains)):
            self.assertEqual(bl.toolchain_entries("checkout", OLD),
                             {TAG1: ["v4.36.0-rc1"], TAG2: ["v4.36.0-rc2"]})

    def test_release_tags_are_read_outside_the_credentialed_checkout(self):
        with patch.object(bl, "command", return_value="") as cmd:
            bl.release_tags()
        self.assertEqual(cmd.call_args.kwargs["cwd"], bl.tempfile.gettempdir())

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
        github = FakeGitHub(age_hours=bl.BUMP_RETARGET_HOURS - 1)
        with (
            patch.object(bl, "command", return_value=OLD) as cmd,
            patch.object(
                bl.Path,
                "read_text",
                return_value=json.dumps({"packages": [{"name": "mathlib", "rev": OLD}]}),
            ),
            patch.object(bl, "open_bumps", return_value=[github.old]),
            patch.object(bl, "bump_state", return_value=([github.old], [])),
            patch.object(bl, "api", side_effect=github.api),
            patch.object(bl, "queue_waiting", side_effect=AssertionError("fresh bump needs no queue")),
        ):
            bl.publish(TIP, 42)
            self.assertEqual(cmd.call_count, 1)
            self.assertFalse(any("runs/" in path for path in github.calls))

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


class RetargetTests(unittest.TestCase):
    def publish(self, github, *, waiting=(), queue_error=None):
        """Publish TIP with the stale (or fresh) active bump #7 open; return the commands."""
        fixtures = Path(__file__).with_name("bump_manifest_fixtures")
        base, upstream = (
            json.loads((fixtures / name).read_text()) for name in ["base.json", "mathlib.json"]
        )

        def cmd(args, **kwargs):
            if args[:3] == ["git", "rev-parse", "HEAD"]:
                return OLD
            if args[:3] == ["git", "rev-parse", "FETCH_HEAD"]:
                return OLD_HEAD
            return ""

        relationships = {(OLD, NEW): "ahead", (OLD, TIP): "ahead", (NEW, TIP): "ahead"}
        with (
            patch.object(bl, "command", side_effect=cmd) as commands,
            patch.object(bl.Path, "read_text", return_value=json.dumps(base)),
            patch.object(bl.Path, "write_text", autospec=True) as self.writes,
            patch.object(bl, "open_bumps", return_value=[github.old]),
            patch.object(bl, "bump_state", return_value=([github.old], [])),
            patch.object(bl, "relationship", side_effect=lambda a, b: relationships.get((a, b), "behind")),
            patch.object(bl, "api", side_effect=github.api),
            patch.object(
                bl, "upstream_file", side_effect=[json.dumps(upstream), "leanprover/lean4:v4.35.0\n"]
            ),
            patch.object(bl, "release_tags", return_value={}),
            patch.object(
                bl, "queue_waiting", side_effect=queue_error, return_value=set(waiting)
            ),
        ):
            bl.publish(TIP, 42)
        return [c.args[0] for c in commands.call_args_list]

    @staticmethod
    def pushed(commands):
        return [c for c in commands if c[:2] == ["git", "push"]]

    @staticmethod
    def closed(commands):
        return [c for c in commands if c[:3] == ["gh", "pr", "close"]]

    def test_a_stale_active_bump_is_retargeted_from_its_head(self):
        commands = self.publish(github := FakeGitHub())
        self.assertIn(["git", "fetch", "--no-tags", "origin", f"refs/heads/bump-mathlib/{NEW}"], commands)
        worktrees = [c for c in commands if c[:3] == ["git", "worktree", "add"]]
        self.assertTrue(worktrees and all(c[-1] == OLD_HEAD for c in worktrees))
        self.assertEqual(len(worktrees), 2)
        check = next(c for c in commands if c[0] == "bash")
        self.assertEqual(check[2], check[3])
        self.assertTrue(check[2].endswith("/base"))
        self.assertEqual(
            self.pushed(commands),
            [["git", "push", f"--force-with-lease=refs/heads/bump-mathlib/{TIP}:", "origin",
              f"HEAD:refs/heads/bump-mathlib/{TIP}"]],
        )
        created = [c for c in commands if c[:3] == ["gh", "pr", "create"]]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][created[0].index("--title") + 1], f"chore: bump Mathlib to {TIP[:8]}")
        self.assertEqual(
            self.closed(commands),
            [["gh", "pr", "close", "7", "--repo", bl.REPO, "--comment",
              "Retargeted to #8; the adaptation commits carry over."]],
        )
        self.assertTrue(any(f"compare/{OLD_HEAD}...{NEW_HEAD}" in p for p in github.calls))

    def test_retarget_body_names_and_marks_its_predecessor(self):
        self.publish(FakeGitHub())
        bodies = [c.args[1] for c in self.writes.call_args_list if c.args[0].name == "body.md"]
        self.assertEqual(len(bodies), 1)
        self.assertIn("This bump continues #7", bodies[0])
        self.assertIn("carries its source repairs", bodies[0])
        self.assertEqual(bl.continuations([pr(number=7), pr(number=8, body=bodies[0])])[0][0], 7)

    def test_a_fresh_active_bump_is_preserved(self):
        commands = self.publish(FakeGitHub(age_hours=bl.BUMP_RETARGET_HOURS - 1))
        self.assertEqual(self.pushed(commands), [])
        self.assertEqual(self.closed(commands), [])

    def test_a_bump_held_by_the_merge_queue_is_not_retargeted(self):
        for name, github, kwargs in [
            ("inserted", FakeGitHub(labels=["bors-staging"]), {}),
            ("waiting", FakeGitHub(), {"waiting": [7]}),
            ("unreadable", FakeGitHub(), {"queue_error": OSError("connection refused")}),
            ("held", FakeGitHub(labels=["hold"]), {}),
        ]:
            with self.subTest(name):
                commands = self.publish(github, **kwargs)
                self.assertEqual(self.pushed(commands), [])
                self.assertEqual(self.closed(commands), [])
                self.assertFalse(any("runs/" in path for path in github.calls))

    def test_a_target_not_ahead_of_the_old_one_is_not_retargeted(self):
        github = FakeGitHub()
        commands = []
        with (
            patch.object(bl, "command", side_effect=lambda args, **kw: commands.append(args) or ""),
            patch.object(bl, "api", side_effect=github.api),
            patch.object(bl, "queue_waiting", return_value=set()),
            patch.object(bl, "relationship", return_value="behind"),
        ):
            bl.retarget(7, OLD, 42)
        self.assertEqual(commands, [])
        self.assertFalse(any("runs/" in path for path in github.calls))

    def test_an_interrupted_retarget_only_closes_the_old_bump(self):
        github = FakeGitHub()
        commands = []
        with (
            patch.object(bl, "command", side_effect=lambda args, **kw: commands.append(args) or OLD),
            patch.object(
                bl.Path,
                "read_text",
                return_value=json.dumps({"packages": [{"name": "mathlib", "rev": OLD}]}),
            ),
            patch.object(bl, "open_bumps", return_value=[github.old, github.new]),
            patch.object(bl, "bump_state", return_value=([github.old, github.new], [])),
            patch.object(bl, "api", side_effect=github.api),
            patch.object(bl, "queue_waiting", return_value=set()),
        ):
            bl.publish("", 0)
        self.assertEqual(
            commands[1:],
            [["gh", "pr", "close", "7", "--repo", bl.REPO, "--comment",
              "Retargeted to #8; the adaptation commits carry over."]],
        )

    def test_an_existing_successor_pr_is_reused(self):
        github = FakeGitHub(existing=[{"number": 8, "state": "open", "head": {"sha": NEW_HEAD}}])
        commands = self.publish(github)
        self.assertEqual(self.pushed(commands), [])
        self.assertFalse(any(c[:3] == ["gh", "pr", "create"] for c in commands))
        self.assertEqual(len(self.closed(commands)), 1)
        github = FakeGitHub(existing=[{"number": 8, "state": "closed", "head": {"sha": NEW_HEAD}}])
        commands = self.publish(github)
        self.assertEqual(self.pushed(commands) + self.closed(commands), [])

    def test_old_commits_missing_from_the_successor_keep_it_open(self):
        github = FakeGitHub(existing=[{"number": 8, "state": "open", "head": {"sha": NEW_HEAD}}],
                            carried="diverged")
        self.assertEqual(self.closed(self.publish(github)), [])

    def test_selection_outputs_a_target_only_for_a_retargetable_bump(self):
        for age, chosen, expected in [
            (bl.BUMP_RETARGET_HOURS + 1, run(TIP), (run(TIP), True)),
            (bl.BUMP_RETARGET_HOURS - 1, run(TIP), (None, False)),
            (bl.BUMP_RETARGET_HOURS + 1, run(NEW), (None, False)),
        ]:
            github = FakeGitHub(age_hours=age)
            with (
                self.subTest(age=age, chosen=chosen["head_sha"]),
                patch.object(bl, "api", side_effect=github.api),
                patch.object(bl, "queue_waiting", return_value=set()),
                patch.object(bl, "select_target", return_value=chosen),
                patch.object(bl, "relationship", side_effect=lambda a, b: "ahead" if a != b else "identical"),
            ):
                self.assertEqual(bl.selection(OLD, [github.old], []), expected)
        github = FakeGitHub()
        with patch.object(bl, "select_target", side_effect=AssertionError("no selection")):
            self.assertEqual(bl.selection(OLD, [github.old, github.new], []), (None, True))

    def test_unreadable_queue_status_fails_closed(self):
        with patch.object(bl.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertIn("unreadable", bl.hold_reason(pr(number=7)))
        malformed = MagicMock()
        malformed.__enter__.return_value.read.return_value = b'{"waiting": {}}'
        with patch.object(bl.urllib.request, "urlopen", return_value=malformed):
            self.assertIn("unreadable", bl.hold_reason(pr(number=7)))
        listed = MagicMock()
        listed.__enter__.return_value.read.return_value = json.dumps(
            {"waiting": {"inputs": [{"number": 7, "head_sha": OLD}]}}
        ).encode()
        with patch.object(bl.urllib.request, "urlopen", return_value=listed):
            self.assertIn("waiting", bl.hold_reason(pr(number=7)))
            self.assertIsNone(bl.hold_reason(pr(number=9)))


if __name__ == "__main__":
    unittest.main()
