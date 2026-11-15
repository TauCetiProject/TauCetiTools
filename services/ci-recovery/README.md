# Continuous PR CI recovery

The Actions sweep remains a fallback. Its nominal hourly schedule can be delayed
or dropped for hours, and its twenty-dispatch limit cannot drain a large backlog.
Run the recovery timer on the authoritative queue host hourly to recover missing
builds across all authors, without a PR-count cap. Systemd serializes passes. Ordinary workflow routing and runner capacity still apply. A pass stops before
consuming the last 1,500 requests of the shared installation API budget.

Install an immutable copy of this Tools checkout under
`~/TauCetiCIRecovery/releases/<revision>` and point `~/TauCetiCIRecovery/current`
at it. Copy the service and timer to `~/.config/systemd/user/`, reload the user
manager, and enable/start the timer. The wrapper requires the queue checkout and
its Python environment at `~/TauCetiQueue` (override its import path with
`TAUCETI_QUEUE_CHECKOUT`) and uses the queue host's existing GitHub App files.
Credentials are minted per pass and never written into the release or journal.

The shared sweeper first snapshots every active ordinary PR build, including
`pending`, `waiting`, `queued`, `requested` and `in_progress` runs. Date windows
split inventories larger than GitHub's 1,000-result cap; equal timestamps stay
in one window. No cancellation changes a collection while it is being paginated.

An ordinary PR build is obsolete when its PR closed/merged, its explicitly tested
head differs from the current head, or the current head has both a `self-tested`
label and its successful PR-bound assertion. A label alone never authorizes
cancellation. Full-lint requests retain ordinary CI. Legacy dispatches without a
tested head can be cleaned up after closure but cannot prove head supersession.
Unknown identities and staging builds are preserved. Each cancellation rechecks
the specific run/attempt and then the current PR state; reopen, restored-head,
removed-label and rerun races invalidate an earlier decision. A rejected or
ambiguous cancellation is logged and reobserved on the next pass, without
repeating the POST in the same pass. Cancellation never publishes a CI verdict.
Status publication also checks closure, current head and accepted local testing.

Enable the companion `tauceti-ci-cleanup.service` and `tauceti-ci-cleanup.timer`
on the same host for cleanup every three minutes, independently of Actions
runner capacity. Install both unit files alongside the existing recovery units,
then run `systemctl --user daemon-reload` and
`systemctl --user enable --now tauceti-ci-cleanup.timer`. Both commands use the
same release, GitHub App wrapper and nonblocking host lock: overlapping passes
leave the next timer to retry. The cleanup-only pass never dispatches new builds.
The existing hourly recovery timer and worker/CI admissions remain enabled.

The generated `pr-ci-cleanup.yml` provides the fast event path for closure,
head changes, routing-label changes and successful `self-tested/<PR>` statuses.
It runs only trusted default-branch tools and uses `actions: write`. Burst events
coalesce globally; the host sweep covers any event whose pending run collapses.
A targeted pass scans that PR's branch plus manual dispatches on main, then
filters by exact PR identity. The complete host sweep covers historical branches.

For reviewable previews, run `python scripts/ci_recovery.py --repo OWNER/REPO
--cleanup-only` (optionally `--pr NUMBER`); add `--apply` to cancel. JSON logs name
the PR, tested head, run attempt and cancellation reason. Each pass reports
counts, deferred API errors, the oldest observed obsolete run's age and an
`overdue` flag after five minutes. Remaining obsolete runs are visible rather
than being reported as successfully cancelled. Inspect both services' journals.

Missing-build recovery still checks immutable current PR heads, statuses, live
builds and recent dispatches immediately before replacement. A genuine current-
head queued job keeps its place regardless of age. Empty/terminal workflow
shells can be recovered after the acquisition grace; an acquired current-head
job cannot. Explicit old heads do not reserve a newer head. Recovery leaves
`self-tested` routing decisions to the existing PR assessment, avoiding competing
build dispatches while the label is being validated. Staging remains controller-
owned. The shared API reserve stops a pass before exhausting the queue's budget.

Recovery dispatches the ordinary trusted default-branch build. It never approves
or merges a PR, and it never fabricates a successful status.

Inspect `journalctl --user -u tauceti-ci-recovery.service` and
`systemctl --user list-timers tauceti-ci-recovery.timer` for progress. The final
line records how many missing builds the pass dispatched. Keep the Actions
fallback, which shares live-run checks with the host process.
