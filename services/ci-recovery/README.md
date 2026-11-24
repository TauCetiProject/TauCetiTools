# Continuous PR CI recovery

The Actions sweep remains a fallback. Its nominal hourly schedule can be delayed
or dropped for hours, and its twenty-dispatch limit cannot drain a large backlog.
Run the recovery timer on the authoritative queue host hourly to recover missing
builds across all authors, without a PR-count cap. Systemd serializes passes. Ordinary workflow routing and runner capacity still apply.

The sweeper shares the queue App's installation (7,150 requests an hour) with the queue, and has a
fixed share of it: a pass stops after `--api-budget 1500` of its own requests, and before consuming
the last 1,500 requests of the window (`--api-reserve 1500`). The reserve is above the queue's own
floor (`github_background_floor`, 1,000), so the sweeper always stops before the queue's admission
reads pause, and those stop before landing, dispatch and commands run short.

Install an immutable copy of this Tools checkout under
`~/TauCetiCIRecovery/releases/<revision>` and point `~/TauCetiCIRecovery/current`
at it. Copy the service and timer to `~/.config/systemd/user/`, reload the user
manager, and enable/start the timer. The wrapper requires the queue checkout and
its Python environment at `~/TauCetiQueue` (override its import path with
`TAUCETI_QUEUE_CHECKOUT`) and uses the queue host's existing GitHub App files.
Every request uses an installation token of the queue App minted per pass; it is never written
into the release or journal, and the host's personal `gh` login is not used.

Any PR author may add the `self-tested` label; it skips PR CI, and readiness and admission honour
it from any author. This pass therefore never builds a labelled head that lacks a `build` status,
and never retries its interrupted build.

The shared sweeper first snapshots every active ordinary PR build, including
`pending`, `waiting`, `queued`, `requested` and `in_progress` runs. Date windows
split inventories larger than GitHub's 1,000-result cap; equal timestamps stay
in one window. No cancellation changes a collection while it is being paginated.

An ordinary PR build is obsolete when its PR closed/merged, its explicitly tested
head differs from the current head, or inline scope eligibility accepts its persistent `self-tested`
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

An interrupted build gets one automatic retry. When a head's current `build` failure comes from a
cancelled pr-build run whose `sandboxed-build` job acquired a runner and ended `cancelled` (as a job
does at GitHub's six-hour timeout), the pass dispatches a replacement build without the startup
grace. The interrupted run must be the head's original `pull_request_target` attempt: a dispatched
or rerun build that is interrupted too is left red and reported as stuck, so a build that really
hangs never loops. The stuck-head report (below) is the retry memory: each retry is recorded there
before it is requested, so a retry that ends without a verdict is not repeated, and a pass without
a writable report (such as the Actions fallback) reports interrupted builds but never retries them.
A memory entry holds while the head's failing status is the same one (a rerun posts a new status),
and for two weeks while the PR is not seen open and ready. One listing of the last two days'
cancelled pr-build runs finds the candidates, and each costs one jobs read; the pass budget is
checked before the listing and before each jobs read. A build cut off before that window is found
through its `build` description instead ("build not run — tested H/B/T identity missing or
invalid", which PR CI posts when a run is cancelled before it records the tested head; a label
event's `skip_build` dispatch does this through the per-PR concurrency group): its run is read
directly, at one run read and one jobs read, and judged the same way, whatever its age.

Each pass also reports stuck heads: open, non-draft, non-`self-tested` heads that, three hours after
their last push, have no `build` status and no live pr-build, or whose build and its one retry were
both interrupted, and that carry none of the final-state labels (`awaiting-author`,
`needs-human-review`, `merge-conflict`, `hold`, `wip`, `keep`, `on-hold`). It reuses the pass's PR
inventory and live-run snapshot, so it costs no extra requests. The journal gets one JSON line per
stuck head (`stuck_head`, `reason`, `hours`) and a `stuck heads: N, oldest Xh` line. The wrapper
writes the same list to `~/.local/state/tauceti-ci-recovery/stuck-heads.json`
(`TAUCETI_CI_STUCK_REPORT` or `--stuck-report` names another path); the merge queue on this host
shows it in `tauceti-queue status` and sends one direct message when the count stays above zero for
six hours. The report keeps `stuck_since`, the first pass of the current stuck period, across passes
up to three hours apart.

Recovery dispatches the ordinary trusted default-branch build. It never approves
or merges a PR, and it never fabricates a successful status.

Inspect `journalctl --user -u tauceti-ci-recovery.service` and
`systemctl --user list-timers tauceti-ci-recovery.timer` for progress. The final
line records how many missing builds the pass dispatched. Keep the Actions
fallback, which shares live-run checks with the host process.
