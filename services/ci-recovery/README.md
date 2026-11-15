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
The wrapper reuses the host's existing organization credential for team membership
reads; repository credentials are minted per pass. Neither is written into the
release or journal.

The shared sweeper first snapshots every active ordinary PR build, including
`pending`, `waiting`, `queued`, `requested` and `in_progress` runs. Date windows
split inventories larger than GitHub's 1,000-result cap; equal timestamps stay
in one window. No cancellation changes a collection while it is being paginated.

An ordinary PR build is obsolete when its PR closed/merged, its explicitly tested
head differs from the current head, or inline trusted-author/scope eligibility accepts its persistent `self-tested`
declaration. No Actions receipt is required. Full-lint requests retain ordinary CI. Legacy dispatches without a
tested head can be cleaned up after closure but cannot prove head supersession.
Unknown identities and staging builds are preserved. Each cancellation rechecks
the specific run/attempt and then the current PR state; reopen, restored-head,
removed-label and rerun races invalidate an earlier decision. A rejected or
ambiguous cancellation is logged and reobserved on the next pass, without
repeating the POST in the same pass. Cancellation never publishes a CI verdict.
Status publication also checks closure, current head and accepted local testing.

There is no additional cleanup daemon or timer. The existing hourly recovery
pass calls the same lifecycle reconciler using the active-run inventory it
already collected, before it considers replacement builds. It catches missed
or coalesced events and interrupted cancellation attempts. Systemd continues to
serialize that existing service; worker/CI admissions remain enabled.

The generated `pr-ci-cleanup.yml` provides the fast event path for closure,
head changes and routing-label changes.
It runs only trusted default-branch tools and uses `actions: write`. Burst events
coalesce globally; the existing hourly host sweep covers any event whose pending run collapses.
A targeted pass scans that PR's branch plus manual dispatches on main, then
filters by exact PR identity. The existing hourly host sweep covers historical branches.

For reviewable previews, run `python scripts/ci_recovery.py --repo OWNER/REPO
--cleanup-only` (optionally `--pr NUMBER`); add `--apply` to cancel. JSON logs name
the PR, tested head, run attempt and cancellation reason. Each pass reports
counts, deferred API errors, the oldest observed obsolete run's age and an
`overdue` flag after five minutes. Remaining obsolete runs are visible rather
than being reported as successfully cancelled. Inspect the existing recovery service's journal.

Missing-build recovery still checks immutable current PR heads, statuses, live
builds and recent dispatches immediately before replacement. A genuine current-
head queued job keeps its place regardless of age. Empty/terminal workflow
shells can be recovered after the acquisition grace; an acquired current-head
job cannot. Explicit old heads do not reserve a newer head. Recovery validates the persistent `self-tested` declaration inline before
considering build dispatches. Staging remains controller-
owned. The shared API reserve stops a pass before exhausting the queue's budget.

Recovery dispatches the ordinary trusted default-branch build. It never approves
or merges a PR, and it never fabricates a successful status.

Inspect `journalctl --user -u tauceti-ci-recovery.service` and
`systemctl --user list-timers tauceti-ci-recovery.timer` for progress. The final
line records how many missing builds the pass dispatched. Keep the Actions
fallback, which shares live-run checks with the host process.
