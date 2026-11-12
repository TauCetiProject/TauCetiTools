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
                    object=dict(status=dict(contexts=[dict(context="build", state="SUCCESS")] if self.build else [])),
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
            return dict(
                total_count=int(self.build), statuses=[dict(context="build", state="SUCCESS")] if self.build else []
            )
        if "status=" in path:
            self.active_reads += 1
            live = self.fresh_live if self.active_reads > 5 else self.live
            return dict(workflow_runs=[live] if live and "status=in_progress" in path else [])
        if "event=" in path:
            live = self.fresh_live or self.manual
            return dict(workflow_runs=[live] if live and f"event={self.manual_event}&" in path else [])
        if path == "repos/owner/project/actions/runs/10":
            return self.history[0] if self.history else run()
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
            commits=dict(
                nodes=[dict(commit=dict(oid=HEAD, status=dict(contexts=[dict(context="build", state="SUCCESS")])))]
            ),
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

    def test_only_current_pr_success_with_label_counts_as_preflight(self):
        pull = dict(number=7, labels=dict(nodes=[dict(name="self-tested")]))
        commit = dict(status=dict(contexts=[dict(context="self-tested/7", state="SUCCESS")]))
        self.assertTrue(recovery.has_preflight(pull, commit))
        for other in (
            dict(pull, number=8),
            dict(pull, labels=dict(nodes=[])),
            dict(pull, labels=dict(nodes=[dict(name="self-tested"), dict(name="full-lint")])),
        ):
            self.assertFalse(recovery.has_preflight(other, commit))
        commit["status"]["contexts"][0]["state"] = "FAILURE"
        self.assertFalse(recovery.has_preflight(pull, commit))

    def test_dead_pending_status_does_not_count_as_a_finished_build(self):
        pull = dict(number=1)
        for state in ("PENDING", "pending", ""):
            with self.subTest(state=state):
                self.assertFalse(
                    recovery.has_preflight(pull, dict(status=dict(contexts=[dict(context="build", state=state)])))
                )
        for state in ("SUCCESS", "FAILURE", "ERROR"):
            with self.subTest(state=state):
                self.assertTrue(
                    recovery.has_preflight(pull, dict(status=dict(contexts=[dict(context="build", state=state)])))
                )

    def test_abandoned_pending_status_is_recovered_but_a_live_run_is_not(self):
        class Pending(GitHub):
            def graphql(self, query, variables):
                value = super().graphql(query, variables)
                if query == recovery.CURRENT:
                    value["repository"]["object"] = dict(status=dict(contexts=[dict(context="build", state="PENDING")]))
                return value

        api = Pending()
        self.assertEqual(len(self.recover(api, apply=True)), 1)
        self.assertEqual(len(api.writes), 1)
        active = run()
        active.update(status="in_progress")
        api = Pending(live=active)
        self.assertEqual(self.recover(api, apply=True), [])
        self.assertEqual(api.writes, [])

    def test_default_sweep_has_no_count_cap_and_explicit_limit_is_optional(self):
        pulls = [dict(number=number, headRefOid=HEAD, updatedAt=OLD) for number in range(1, 26)]
        for options, expected in (({}, 25), ({"limit": 0}, 25), ({"limit": 2}, 2)):
            with self.subTest(options=options):
                api = GitHub()
                with (
                    patch.object(recovery, "inventory", return_value=("main", pulls)),
                    patch.object(recovery, "still_missing", return_value=True),
                ):
                    selected = self.recover(api, apply=True, **options)
                self.assertEqual(len(selected), expected)
                self.assertEqual(len(api.writes), expected)

    def test_shared_api_reserve_stops_dispatch_and_next_pass_can_resume(self):
        api = GitHub()
        api.stats = {"read": {"buckets": {"core": {"remaining": "1499"}}}}
        self.assertEqual(self.recover(api, apply=True, api_reserve=1500), [])
        self.assertEqual(api.writes, [])
        api.stats["read"]["buckets"]["core"]["remaining"] = "5000"
        self.assertEqual(len(self.recover(api, apply=True, api_reserve=1500)), 1)
        self.assertEqual(len(api.writes), 1)

    def test_ignored_label_events_do_not_block_recovery(self):
        ignored = run()
        ignored.update(display_title="Ignored label event 12", status="in_progress")
        api = GitHub(live=ignored, history=[ignored])
        self.assertEqual(len(self.recover(api, apply=True)), 1)
        self.assertEqual(len(api.writes), 1)

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


def queued(**changes):
    value = run()
    value.update(
        status="queued", conclusion=None, run_attempt=1, event="pull_request_target",
        path=".github/workflows/pr-build.yml", repository=dict(full_name="owner/project"),
    )
    value.update(changes)
    return value


class AcquisitionGitHub(GitHub):
    def __init__(self, *, jobs=None, current_run=None, cancellation=None, fresh_jobs=None, after_cancel=None, **kwargs):
        live = kwargs.pop("live", queued())
        super().__init__(live=live, history=[live], **kwargs)
        self.current_run = live if current_run is None else current_run
        self.jobs = dict(total_count=0, jobs=[]) if jobs is None else jobs
        self.fresh_jobs, self.job_reads = fresh_jobs, 0
        self.cancellation = cancellation
        self.after_cancel = after_cancel
        for response in [self.jobs, self.fresh_jobs]:
            if response:
                for index, job in enumerate(response.get("jobs", []), 1):
                    job.setdefault("id", index)

    def request(self, method, path, data=None):
        if method == "GET" and "/attempts/" in path and "/jobs?" in path:
            self.job_reads += 1
            return self.fresh_jobs if self.job_reads > 1 and self.fresh_jobs is not None else self.jobs
        if method == "GET" and path == "repos/owner/project/actions/runs/10":
            return self.current_run
        if method == "GET" and "status=" in path:
            return dict(workflow_runs=[self.live] if f"status={self.live['status']}&" in path else [])
        if method == "POST" and path.endswith(("/cancel", "/force-cancel")):
            self.writes.append((path, data))
            if self.cancellation:
                raise self.cancellation
            if self.after_cancel:
                self.current_run = self.live = self.after_cancel
            return None
        return super().request(method, path, data)


class AcquisitionTests(unittest.TestCase):
    def recover(self, api, **kwargs):
        return recovery.recover(api, "owner/project", now=NOW, **kwargs)

    def test_stale_empty_run_is_cancelled_and_replaced_and_dry_run_is_read_only(self):
        for apply in [False, True]:
            api = AcquisitionGitHub()
            selected = self.recover(api, apply=apply)
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["dispatched"], apply)
            self.assertEqual(len(api.writes), 2 if apply else 0)
            if apply:
                self.assertTrue(api.writes[0][0].endswith("/cancel"))
                self.assertTrue(api.writes[1][0].endswith("/dispatches"))

    def test_completed_attempt_shell_and_uncancellable_empty_shell_do_not_reserve_forever(self):
        terminal = [dict(status="completed", runner_id=7, runner_name="old-picker"),
                    dict(status="completed", conclusion="cancelled", runner_id=0)]
        error = recovery.APIError("POST", "cancel", 409, dict(message="Cannot cancel a workflow run that has not yet queued."))
        for jobs in [[], terminal]:
            with self.subTest(jobs=jobs):
                api = AcquisitionGitHub(jobs=dict(total_count=len(jobs), jobs=jobs), cancellation=error)
                self.assertEqual(len(self.recover(api, apply=True)), 1)
                self.assertEqual([p.rsplit("/", 1)[-1] for p, _ in api.writes],
                                 ["cancel", "force-cancel", "dispatches"])

    def test_fresh_queued_attempt_and_changed_attempt_are_protected(self):
        for run in [queued(updated_at="2026-10-28T17:30:00Z"), queued(run_attempt=2)]:
            with self.subTest(run=run):
                api = AcquisitionGitHub(current_run=run)
                self.assertEqual(self.recover(api, apply=True), [])
                self.assertEqual(api.writes, [])

    def test_acquired_jobs_and_incomplete_or_unknown_job_observations_are_protected(self):
        cases = [
            dict(total_count=1, jobs=[dict(status="in_progress", runner_id=7)]),
            dict(total_count=1, jobs=[dict(status="queued", runner_name="acquired")]),
            dict(total_count=2, jobs=[dict(status="queued", runner_id=0)]),
            dict(total_count=1, jobs=[dict(status="unknown")]),
            dict(total_count=2, jobs=[dict(id=1, status="queued"), dict(id=1, status="queued")]),
            dict(total_count=1, jobs=[dict(status="queued", run_attempt=2)]),
        ]
        for jobs in cases:
            with self.subTest(jobs=jobs):
                api = AcquisitionGitHub(jobs=jobs)
                self.assertEqual(self.recover(api, apply=True), [])
                self.assertEqual(api.writes, [])

    def test_completed_picker_does_not_reserve_an_unacquired_sandbox(self):
        jobs = [dict(status="completed", runner_id=7), dict(status="queued", runner_id=0)]
        api = AcquisitionGitHub(jobs=dict(total_count=2, jobs=jobs))
        self.assertEqual(len(self.recover(api, apply=True)), 1)

    def test_acquisition_racing_inventory_prevents_dispatch(self):
        api = AcquisitionGitHub(fresh_jobs=dict(total_count=1, jobs=[dict(status="in_progress", runner_id=7)]))
        self.assertEqual(self.recover(api, apply=True), [])
        self.assertEqual([p.rsplit("/", 1)[-1] for p, _ in api.writes], ["cancel"])

    def test_cancel_transition_does_not_restart_grace_but_a_new_attempt_does(self):
        for attempt, count in [(1, 1), (2, 0)]:
            with self.subTest(attempt=attempt):
                current = queued(run_attempt=attempt, updated_at="2026-10-28T17:59:30Z")
                api = AcquisitionGitHub(after_cancel=current)
                self.assertEqual(len(self.recover(api, apply=True)), count)

    def test_next_pass_recovers_a_cancelled_unstarted_build_without_waiting_another_grace(self):
        completed = queued(status="completed", conclusion="cancelled", updated_at="2026-10-28T17:59:30Z")
        for acquired, count in [(0, 1), (7, 0)]:
            with self.subTest(acquired=acquired):
                jobs = [dict(name="pick-runner / pick", status="completed", runner_id=7),
                        dict(name="sandboxed-build", status="completed", runner_id=acquired)]
                api = AcquisitionGitHub(live=completed, jobs=dict(total_count=2, jobs=jobs))
                self.assertEqual(len(self.recover(api, apply=True)), count)

    def test_unknown_cancellation_conflict_or_ambiguous_post_does_not_dispatch_or_retry(self):
        for status, message in [(409, "another conflict"), (503, "lost response"), (0, "network failure")]:
            with self.subTest(status=status):
                api = AcquisitionGitHub(cancellation=recovery.APIError("POST", "cancel", status, dict(message=message)))
                self.assertEqual(self.recover(api, apply=True), [])
                self.assertEqual([p.rsplit("/", 1)[-1] for p, _ in api.writes], ["cancel"])

    def test_staging_is_owned_by_its_controller(self):
        staging = queued(event="repository_dispatch", display_title="bors branch=main batch=415 head=" + HEAD)
        api = AcquisitionGitHub(live=staging)
        self.assertEqual(self.recover(api, apply=True), [])
        self.assertEqual(api.job_reads, 0)
        self.assertEqual(api.writes, [])

    def test_obsolete_queue_cleanup_also_runs_when_no_pr_needs_another_build(self):
        api = AcquisitionGitHub()
        with patch.object(recovery, "inventory", return_value=("main", [])):
            self.assertEqual(self.recover(api, apply=True), [])
        self.assertEqual([p.rsplit("/", 1)[-1] for p, _ in api.writes], ["cancel"])

    def test_late_status_after_active_recheck_does_not_dispatch(self):
        api = AcquisitionGitHub()
        with patch.object(recovery, "still_missing", side_effect=[True, False]):
            self.assertEqual(self.recover(api, apply=True), [])
        self.assertEqual([p.rsplit("/", 1)[-1] for p, _ in api.writes], ["cancel"])

    def test_acquired_old_head_does_not_block_a_new_head(self):
        live = queued(status="in_progress", head_sha="b" * 40, display_title="pr-build pr=1 head=" + "b" * 40)
        api = AcquisitionGitHub(live=live, jobs=dict(total_count=1, jobs=[dict(status="in_progress", runner_id=7)]))
        api.history = []
        self.assertEqual(len(self.recover(api, apply=True)), 1)
        self.assertEqual([p.rsplit("/", 1)[-1] for p, _ in api.writes], ["dispatches"])

    def test_cancelling_a_paginated_inventory_does_not_skip_the_next_page(self):
        class Paginated:
            def __init__(self):
                self.live = {number: queued(id=number) for number in range(1000, 1101)}
                self.cancelled = []

            def request(self, method, path, data=None):
                if "status=" in path or "event=" in path:
                    page = int(path.rsplit("page=", 1)[-1])
                    rows = list(self.live.values()) if "status=queued&" in path else []
                    return dict(workflow_runs=rows[(page - 1) * 100:page * 100])
                number = int(path.split("/runs/", 1)[1].split("/", 1)[0])
                if method == "POST":
                    self.cancelled.append(number)
                    del self.live[number]
                    return None
                if "/jobs?" in path:
                    return dict(total_count=0, jobs=[])
                return self.live[number]

        api = Paginated()
        with patch.object(recovery, "inventory", return_value=("main", [])):
            self.assertEqual(self.recover(api, apply=True), [])
        self.assertEqual(api.cancelled, list(range(1000, 1101)))

    def test_needed_build_dispatches_before_obsolete_cleanup_consumes_the_budget(self):
        class Priority(AcquisitionGitHub):
            other = queued(id=20, head_sha="b" * 40, display_title="pr-build pr=2 head=" + "b" * 40)

            def request(self, method, path, data=None):
                if method == "GET" and "status=" in path:
                    return dict(workflow_runs=[self.other, self.live] if "status=queued&" in path else [])
                if method == "GET" and path == "repos/owner/project/actions/runs/20":
                    return self.other
                value = super().request(method, path, data)
                if method == "POST" and path.endswith("/20/cancel"):
                    self.stats = {"read": {"buckets": {"core": {"remaining": "1499"}}}}
                return value

        api = Priority()
        self.assertEqual(len(self.recover(api, apply=True, api_reserve=1500)), 1)
        self.assertEqual([p.rsplit("/", 2)[-2:] for p, _ in api.writes],
                         [["10", "cancel"], ["pr-build.yml", "dispatches"], ["20", "cancel"]])

    def test_fresh_head_run_blocks_dispatch_without_rescanning_global_status_lists(self):
        class Race(GitHub):
            head_reads = 0

            def request(self, method, path, data=None):
                if "head_sha=" in path:
                    self.head_reads += 1
                    if self.head_reads > 1:
                        return dict(workflow_runs=[queued(status="in_progress")])
                return super().request(method, path, data)

        api = Race()
        with patch.object(api, "request", wraps=api.request) as calls:
            self.assertEqual(self.recover(api, apply=True), [])
        self.assertEqual(sum("status=" in c.args[1] for c in calls.call_args_list), 5)
        self.assertEqual(api.writes, [])


if __name__ == "__main__":
    unittest.main()
