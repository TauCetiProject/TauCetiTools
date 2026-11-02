"""Abandoned maintenance CI recovery, with no live requests or writes."""

import copy
import datetime as dt
import unittest

import maintenance_builds as mb

NOW = dt.datetime(2026, 11, 2, tzinfo=dt.timezone.utc)
OLD = (NOW - dt.timedelta(days=2)).isoformat()
HEAD = "a" * 40


class Client:
    def __init__(self):
        self.pr = dict(number=19872, state="open", draft=False, labels=[],
                       user=dict(login="tauceti-review-bot[bot]", type="Bot"),
                       head=dict(sha=HEAD, ref="bump-mathlib/target", repo=dict(full_name=mb.REPO)),
                       base=dict(ref="main"))
        self.statuses, self.manual, self.writes = [], [], []
        self.runs = [dict(status="completed", conclusion="cancelled", updated_at=OLD)]
        self.commit_date = OLD
        self.changed = False
        self.reads = 0

    def items(self, path):
        return []

    def request(self, method, path, data=None):
        if method == "POST":
            self.writes.append((path, data))
            return None
        if path.startswith("search/issues"):
            return dict(total_count=1, incomplete_results=False, items=[dict(number=19872)])
        if path.endswith("/pulls/19872"):
            self.reads += 1
            pr = copy.deepcopy(self.pr)
            if self.changed and self.reads > 1:
                pr["head"]["sha"] = "b" * 40
            return pr
        if "/statuses?" in path:
            return self.statuses
        if "/git/commits/" in path:
            return dict(committer=dict(date=self.commit_date))
        runs = self.runs if "head_sha=" in path else self.manual
        return dict(total_count=len(runs), workflow_runs=runs)


class RecoveryTests(unittest.TestCase):
    def test_cancelled_bump_without_status_dispatches_ordinary_ci(self):
        client = Client()
        self.assertEqual(mb.recover(client, now=NOW), [19872])
        self.assertEqual(client.writes, [(f"repos/{mb.REPO}/actions/workflows/pr-build.yml/dispatches",
                                         dict(ref="main", inputs=dict(pr="19872", maintenance="true")))])

    def test_existing_build_red_green_pending_and_active_runs_are_left_alone(self):
        for state in ("failure", "success", "pending", "error"):
            client = Client()
            client.statuses = [dict(context="build", state=state)]
            self.assertEqual(mb.recover(client, now=NOW), [])
        for status in ("queued", "in_progress", "waiting"):
            client = Client()
            client.runs[0]["status"] = status
            self.assertEqual(mb.recover(client, now=NOW), [])

    def test_manual_dispatch_cooldown_includes_queued_runs_on_main_sha(self):
        client = Client()
        client.manual = [dict(display_title="pr-build 19872", head_sha="b" * 40, status="queued")]
        self.assertEqual(mb.recover(client, now=NOW), [])

    def test_dry_run_and_head_race_cannot_write(self):
        client = Client()
        self.assertEqual(mb.recover(client, now=NOW, dry_run=True), [19872])
        self.assertEqual(client.writes, [])
        client = Client()
        client.changed = True
        self.assertEqual(mb.recover(client, now=NOW), [])
        self.assertEqual(client.writes, [])

    def test_holds_drafts_forks_and_human_authors_cannot_dispatch(self):
        for change in (dict(labels=[dict(name="hold")]), dict(draft=True),
                       dict(user=dict(login="human", type="User")), dict(state="closed")):
            client = Client()
            client.pr.update(change)
            self.assertEqual(mb.recover(client, now=NOW), [])
        client = Client()
        client.pr["head"]["repo"]["full_name"] = "fork/TauCeti"
        self.assertEqual(mb.recover(client, now=NOW), [])

    def test_recent_commit_or_recent_cancellation_waits_for_grace(self):
        recent = (NOW - dt.timedelta(minutes=5)).isoformat()
        client = Client()
        client.commit_date = recent
        self.assertEqual(mb.recover(client, now=NOW), [])
        client = Client()
        client.runs[0]["updated_at"] = recent
        self.assertEqual(mb.recover(client, now=NOW), [])

    def test_lint_repair_uses_the_same_recovery(self):
        client = Client()
        client.pr["head"]["ref"] = "lint-repair/main"
        self.assertEqual(mb.recover(client, now=NOW), [19872])


if __name__ == "__main__":
    unittest.main()
