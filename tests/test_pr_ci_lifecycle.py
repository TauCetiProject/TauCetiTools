"""PR lifecycle races, exact dispatch identities and large GitHub inventories."""

import copy
import io
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).parents[1] / "tauceti_tools/assets/scripts"))
import pr_ci_lifecycle as lifecycle
from github_api import APIError

HEAD, OLD = "a" * 40, "b" * 40
REPO = "owner/project"


def run(**changes):
    return dict(
        id=10,
        run_attempt=1,
        status="pending",
        event="pull_request_target",
        path=".github/workflows/pr-build.yml",
        repository=dict(full_name=REPO),
        display_title="pr-build pr=7 head=" + HEAD,
        head_sha=HEAD,
        pull_requests=[],
        created_at="2026-11-15T01:00:00Z",
        **changes,
    )


def pull(state="OPEN", head=HEAD, labels=(), statuses=()):
    return dict(
        number=7,
        state=state,
        headRefOid=head,
        headRefName="feature",
        labels=dict(nodes=[dict(name=x) for x in labels], pageInfo=dict(hasNextPage=False)),
        commits=dict(nodes=[dict(commit=dict(oid=head, status=dict(contexts=list(statuses))))]),
    )


class GitHub:
    def __init__(self, initial=None, fresh=None, current=None, error=None):
        self.initial = initial or pull("CLOSED")
        self.fresh = self.initial if fresh is None else fresh
        self.current = current or run()
        self.error = error
        self.calls, self.writes, self.graph_reads = [], [], 0

    def graphql(self, query, variables):
        self.graph_reads += 1
        value = self.initial if self.graph_reads == 1 else self.fresh
        return dict(repository={f"p{n}": copy.deepcopy(value) for n in re.findall(r"p(\d+):pullRequest", query)})

    def request(self, method, path, data=None):
        self.calls.append((method, path))
        if method == "POST":
            self.writes.append(path)
            if self.error:
                raise self.error
            return None
        if "/actions/runs/" in path:
            return copy.deepcopy(self.current)
        if "/pulls/" in path:
            return dict(head=dict(ref="feature"))
        raise AssertionError(path)


class LifecycleTests(unittest.TestCase):
    def reconcile(self, api, runs=None, **kwargs):
        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
            patch.object(
                lifecycle,
                "local_route",
                side_effect=lambda api, repo, pr: bool(
                    pr
                    and any(x["name"] == "self-tested" for x in pr["labels"]["nodes"])
                    and not any(x["name"] in {"full-lint", "lint-repair"} for x in pr["labels"]["nodes"])
                ),
            ),
        ):
            return lifecycle.reconcile(api, REPO, active=[run()] if runs is None else runs, **kwargs)

    def test_closed_merged_and_superseded_work_is_cancelled_in_every_active_state(self):
        for state in lifecycle.ACTIVE:
            for pr, why in [
                (pull("CLOSED"), "pr-closed"),
                (pull("MERGED"), "pr-closed"),
                (pull(head=OLD), "head-superseded"),
            ]:
                with self.subTest(state=state, reason=why):
                    current = run()
                    current["status"] = state
                    api = GitHub(initial=pr, current=current)
                    result = self.reconcile(api, runs=[current], apply=True)
                    self.assertEqual(result[0]["reason"], why)
                    self.assertEqual(api.writes, [f"repos/{REPO}/actions/runs/10/cancel"])

    def test_current_head_keeps_its_queue_position_regardless_of_age(self):
        api = GitHub(initial=pull())
        self.assertEqual(self.reconcile(api, apply=True), [])
        self.assertEqual(api.writes, [])
        self.assertEqual(api.graph_reads, 1)

    def test_persistent_self_tested_cancels_without_a_receipt(self):
        for statuses in [[], [dict(context="self-tested/7", state="FAILURE")]]:
            api = GitHub(initial=pull(labels=["self-tested"], statuses=statuses))
            self.assertEqual(self.reconcile(api, apply=True)[0]["reason"], "self-tested")
        for labels in [[], ["self-tested", "full-lint"], ["self-tested", "lint-repair"]]:
            api = GitHub(initial=pull(labels=labels))
            self.assertEqual(self.reconcile(api, apply=True), [])
            self.assertEqual(api.writes, [])

    def test_reopen_restored_head_and_removed_label_races_prevent_cancellation(self):
        assertion = dict(context="self-tested/7", state="success")
        for initial, fresh in [
            (pull("CLOSED"), pull()),
            (pull(head=OLD), pull()),
            (pull(labels=["self-tested"], statuses=[assertion]), pull()),
        ]:
            api = GitHub(initial=initial, fresh=fresh)
            self.assertEqual(self.reconcile(api, apply=True), [])
            self.assertEqual(api.writes, [])

    def test_new_attempt_completed_run_changed_identity_and_staging_are_protected(self):
        for changes in [
            dict(run_attempt=2),
            dict(status="completed"),
            dict(display_title="pr-build pr=8 head=" + HEAD),
            dict(event="repository_dispatch", display_title="bors branch=main batch=3 head=" + HEAD),
            dict(path=".github/workflows/other.yml"),
            dict(repository=dict(full_name="elsewhere/repo")),
        ]:
            current = run()
            current.update(changes)
            api = GitHub(current=current)
            self.assertEqual(self.reconcile(api, apply=True), [])
            self.assertEqual(api.writes, [])
        staging = run()
        staging.update(event="merge_group")
        api = GitHub()
        self.assertEqual(self.reconcile(api, runs=[staging], apply=True), [])
        self.assertEqual(api.graph_reads, 0)

    def test_dispatch_uses_bound_head_instead_of_workflow_sha(self):
        current = run()
        current.update(event="repository_dispatch", head_sha=OLD)
        api = GitHub(initial=pull(), current=current)
        self.assertEqual(self.reconcile(api, runs=[current], apply=True), [])
        api = GitHub(initial=pull(head=OLD), current=current)
        self.assertEqual(self.reconcile(api, runs=[current], apply=True)[0]["reason"], "head-superseded")
        legacy = dict(current, display_title="pr-build 7")
        api = GitHub(initial=pull(head=OLD), current=legacy)
        self.assertEqual(self.reconcile(api, runs=[legacy], apply=True), [])
        api = GitHub(initial=pull("CLOSED"), current=legacy)
        self.assertEqual(self.reconcile(api, runs=[legacy], apply=True)[0]["reason"], "pr-closed")

    def test_unknown_and_conflicting_identities_do_not_authorize_cancellation(self):
        for changes in [dict(display_title="pr-build"), dict(pull_requests=[dict(number=8)])]:
            current = run()
            current.update(changes)
            api = GitHub()
            self.assertEqual(self.reconcile(api, runs=[current], apply=True), [])
            self.assertEqual(api.writes, [])

    def test_dry_run_is_read_only_and_api_reserve_defers_mutation(self):
        api = GitHub()
        self.assertFalse(self.reconcile(api)[0]["cancellation_requested"])
        self.assertEqual(api.writes, [])
        api.stats = {"write": {"buckets": {"core": {"remaining": "1499"}}}}
        self.assertEqual(self.reconcile(api, apply=True, api_reserve=1500), [])
        self.assertEqual(api.writes, [])

    def test_failed_or_ambiguous_post_is_not_retried_and_next_pass_reobserves(self):
        for status in [0, 403, 409, 503]:
            api = GitHub(error=APIError("POST", "cancel", status, dict(message="unavailable")))
            self.assertEqual(self.reconcile(api, apply=True), [])
            self.assertEqual(len(api.writes), 1)
            api.error = None
            api.graph_reads = 0
            self.assertEqual(len(self.reconcile(api, apply=True)), 1)
            self.assertEqual(len(api.writes), 2)

    def test_strict_event_and_timer_pass_fail_visibly_after_deferred_cancellation(self):
        api = GitHub(error=APIError("POST", "cancel", 403, dict(message="permission missing")))
        with self.assertRaisesRegex(RuntimeError, "cancellations deferred"):
            self.reconcile(api, apply=True, strict=True)
        self.assertEqual(len(api.writes), 1)

    def test_targeted_event_path_scans_branch_and_dispatches_then_filters_pr(self):
        api = GitHub()
        other = run()
        other.update(id=11, display_title="pr-build pr=8 head=" + HEAD)
        with patch.object(lifecycle, "runs", return_value=iter(())):
            self.assertEqual(self.reconcile(api, runs=[], number=7, apply=True), [])

        def inventory(*args, **kwargs):
            return iter([run(), other])

        with patch.object(lifecycle, "runs", side_effect=inventory) as calls, redirect_stdout(io.StringIO()):
            result = lifecycle.reconcile(api, REPO, number=7, apply=True)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(calls.call_args_list), 15)
        self.assertTrue(any("branch=feature" in c.args[2] for c in calls.call_args_list))
        self.assertTrue(any("event=repository_dispatch" in c.args[2] for c in calls.call_args_list))

    def test_hourly_recovery_reuses_its_complete_inventory_for_cleanup(self):
        import ci_recovery

        api = GitHub()
        with (
            patch.object(ci_recovery, "inventory", return_value=("main", [])),
            patch.object(ci_recovery, "runs", return_value=iter(())),
            patch.object(ci_recovery, "reconcile") as cleanup,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(ci_recovery.recover(api, REPO, apply=True), [])
        cleanup.assert_called_once_with(api, REPO, active=[], apply=True, api_reserve=0, api_budget=0)

    def test_standalone_recovery_checkout_contains_its_policy_and_exports_package_root(self):
        root = Path(__file__).parents[1]
        workflow = (root / ".github/workflows/retry-ci-status.yml").read_text()
        self.assertIn("sparse-checkout: tauceti_tools\n", workflow)
        self.assertIn("PYTHONPATH: ${{ github.workspace }}", workflow)
        import os
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(
                [sys.executable, str(root / "tauceti_tools/assets/scripts/ci_recovery.py"), "--help"],
                cwd=cwd,
                env={**os.environ, "PYTHONPATH": str(root)},
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--cleanup-only", result.stdout)

    def test_all_pages_are_read_before_any_cancellation(self):
        api = GitHub()
        visited = []

        def inventory(*args, **kwargs):
            visited.append(args[2])
            self.assertEqual(api.writes, [])
            yield run()

        with patch.object(lifecycle, "runs", side_effect=inventory), redirect_stdout(io.StringIO()):
            lifecycle.reconcile(api, REPO, apply=True)
        self.assertEqual(len(visited), 5)
        self.assertEqual(len(api.writes), 1)


class InventoryTests(unittest.TestCase):
    def test_over_one_thousand_runs_and_tied_timestamps_have_complete_unique_coverage(self):
        class API:
            def __init__(self):
                self.rows = [dict(id=i, created_at=f"2026-11-15T01:{i // 60:02}:{i % 60:02}Z") for i in range(1605)]

            def request(self, method, path):
                q = parse_qs(urlsplit(path).query)
                rows = self.rows
                date = q.get("created", [""])[0]
                if ".." in date:
                    low, high = date.split("..")
                    rows = [r for r in rows if low <= r["created_at"] <= high]
                elif date.startswith("<="):
                    rows = [r for r in rows if r["created_at"] <= date[2:]]
                elif date.startswith(">="):
                    rows = [r for r in rows if r["created_at"] >= date[2:]]
                page = int(q["page"][0])
                rows = sorted(rows, key=lambda r: r["created_at"], reverse=True)
                return dict(total_count=len(rows), workflow_runs=rows[(page - 1) * 100 : min(page * 100, 1000)])

        api = API()
        # Include a boundary containing several runs with the same timestamp.
        for r in api.rows[95:110]:
            r["created_at"] = api.rows[100]["created_at"]
        rows = list(lifecycle.runs(api, REPO, "status=pending"))
        self.assertEqual({r["id"] for r in rows}, set(range(1605)))
        self.assertEqual(len(rows), 1605)
        recent = list(lifecycle.runs(api, REPO, "event=workflow_dispatch&created=%3E%3D2026-11-15T01:20:00Z"))
        self.assertEqual(len(recent), 405)

    def test_unsplittable_second_overflow_fails_instead_of_claiming_complete_inventory(self):
        class API:
            def request(self, method, path):
                return dict(
                    total_count=1001, workflow_runs=[dict(id=i, created_at="2026-11-15T01:00:00Z") for i in range(100)]
                )

        with self.assertRaisesRegex(RuntimeError, "single second"):
            list(lifecycle.runs(API(), REPO, "status=pending&created=%3E%3D2026-11-15T01:00:00Z"))


if __name__ == "__main__":
    unittest.main()
