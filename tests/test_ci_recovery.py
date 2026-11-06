"""Exercise recovery decisions and dispatches against GitHub-shaped responses."""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).parents[1] / "tauceti_tools/assets/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("ci_recovery", SCRIPTS / "ci_recovery.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)

OLD, NOW = "2026-10-21T05:00:00Z", recovery.timestamp("2026-10-28T18:00:00Z")
HEAD = "a" * 40


def run(**changes):
    return dict(
        id=10,
        head_sha=HEAD,
        display_title="pr-build pr=1 head=" + HEAD,
        pull_requests=[],
        status="completed",
        conclusion="failure",
        created_at=OLD,
        updated_at=OLD,
        **changes,
    )


class GitHub:
    def __init__(
        self,
        *,
        live=None,
        fresh_live=None,
        manual=None,
        current=None,
        build=False,
        history=None,
        manual_event="workflow_dispatch",
    ):
        self.live, self.fresh_live, self.manual = live, fresh_live, manual
        self.current = current or dict(state="open", draft=False, head=dict(sha=HEAD))
        self.build = build
        self.history = [run()] if history is None else history
        self.manual_event = manual_event
        self.active_reads = 0
        self.writes = []

    def graphql(self, query, variables):
        if query == recovery.CURRENT:
            return dict(
                repository=dict(
                    pullRequest=dict(
                        state=self.current["state"].upper(),
                        isDraft=self.current["draft"],
                        headRefOid=self.current["head"]["sha"],
                    ),
                    object=dict(status=dict(contexts=[dict(context="build")] if self.build else [])),
                )
            )
        pull = dict(
            number=1,
            headRefOid=HEAD,
            isDraft=False,
            updatedAt=OLD,
            commits=dict(nodes=[dict(commit=dict(oid=HEAD, status=None))]),
        )
        return dict(
            repository=dict(
                defaultBranchRef=dict(name="main"),
                pullRequests=dict(nodes=[pull], pageInfo=dict(hasNextPage=False, endCursor=None)),
            )
        )

    def request(self, method, path, data=None):
        if method == "POST":
            self.writes.append((path, data))
            return None
        if "/pulls/" in path:
            return self.current
        if "/commits/" in path:
            return dict(total_count=int(self.build), statuses=[dict(context="build")] if self.build else [])
        if "status=" in path:
            self.active_reads += 1
            live = self.fresh_live if self.active_reads > 5 else self.live
            return dict(workflow_runs=[live] if live and "status=in_progress" in path else [])
        if "event=" in path:
            return dict(workflow_runs=[self.manual] if self.manual and f"event={self.manual_event}&" in path else [])
        return dict(workflow_runs=self.history)


class RecoveryTests(unittest.TestCase):
    def recover(self, api, **kwargs):
        return recovery.recover(api, "owner/project", now=NOW, **kwargs)

    def test_old_missing_result_dispatches_default_branch_and_dry_run_does_not_write(self):
        for history in ([run()], []):
            with self.subTest(history=history):
                api = GitHub(history=history)
                self.assertEqual(self.recover(api)[0]["previous_run"], 10 if history else None)
                self.assertEqual(api.writes, [])
                self.recover(api, apply=True)
                self.assertEqual(
                    api.writes,
                    [
                        (
                            "repos/owner/project/actions/workflows/pr-build.yml/dispatches",
                            dict(ref="main", inputs=dict(pr="1")),
                        )
                    ],
                )

    def test_running_manual_replacement_on_main_sha_and_racing_replacement_are_skipped(self):
        live = run()
        live.update(head_sha="b" * 40, display_title="pr-build 1", status="in_progress", conclusion=None)
        for kwargs in (dict(live=live), dict(fresh_live=live)):
            with self.subTest(kwargs=kwargs):
                api = GitHub(**kwargs)
                self.assertEqual(self.recover(api, apply=True), [])
                self.assertEqual(api.writes, [])

    def test_closed_moved_draft_and_late_status_do_not_rebuild(self):
        for changes in (dict(state="closed"), dict(head=dict(sha="b" * 40)), dict(draft=True), {}):
            with self.subTest(changes=changes):
                current = dict(state="open", draft=False, head=dict(sha=HEAD))
                current.update(changes)
                api = GitHub(current=current, build=not changes)
                self.assertEqual(self.recover(api, apply=True), [])
                self.assertEqual(api.writes, [])

    def test_recent_manual_attempt_gets_replay_grace_without_a_new_retry_ledger(self):
        manual = run()
        manual.update(
            id=11,
            head_sha="b" * 40,
            display_title="pr-build 1",
            created_at="2026-10-28T17:30:00Z",
            updated_at="2026-10-28T17:35:00Z",
        )
        for event in ("workflow_dispatch", "repository_dispatch"):
            api = GitHub(manual=manual, manual_event=event)
            self.assertEqual(self.recover(api, apply=True), [])
            self.assertEqual(api.writes, [])

    def test_inventory_paginates_and_excludes_built_and_draft_prs(self):
        missing = dict(
            number=1, headRefOid=HEAD, isDraft=False, commits=dict(nodes=[dict(commit=dict(oid=HEAD, status=None))])
        )
        built = dict(
            number=2,
            headRefOid=HEAD,
            isDraft=False,
            commits=dict(nodes=[dict(commit=dict(oid=HEAD, status=dict(contexts=[dict(context="build")])))]),
        )
        api = GitHub()
        pages = [
            dict(nodes=[built], pageInfo=dict(hasNextPage=True, endCursor="next")),
            dict(nodes=[dict(missing, isDraft=True), missing], pageInfo=dict(hasNextPage=False)),
        ]
        with patch.object(
            api,
            "graphql",
            side_effect=[dict(repository=dict(defaultBranchRef=dict(name="main"), pullRequests=p)) for p in pages],
        ) as query:
            self.assertEqual(recovery.inventory(api, "owner/project"), ("main", [missing]))
        self.assertEqual(query.call_args.args[1]["after"], "next")

    def test_dispatch_rejection_fails_visibly_without_repeating_the_post(self):
        api = GitHub()
        original = api.request

        def request(method, path, data=None):
            if method == "POST":
                raise recovery.APIError(method, path, 403, {"message": "Actions write permission missing"})
            return original(method, path, data)

        with patch.object(api, "request", side_effect=request) as calls, self.assertRaises(recovery.APIError):
            self.recover(api, apply=True)
        self.assertEqual(sum(call.args[0] == "POST" for call in calls.call_args_list), 1)


if __name__ == "__main__":
    unittest.main()
