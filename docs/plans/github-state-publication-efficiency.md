# Improve publication throughput without adding scheduling machinery

Status: status-dormancy correction merged and deployed in PR #123. Fixed-chunk
publication candidates were rejected because they worsened input latency; the
existing yield behavior is retained. Measurements and rollout results are in
[the results](../../services/github-state/PUBLICATION-EFFICIENCY-RESULTS.md).
Scope: upstream `TauCetiProject/TauCetiTools`, primarily
`services/github-state`, from current upstream `main`. The inspected baseline is
`cf29ced7b50a144a0d8cc4fda6f55781dbdca59c` (PR #120).

## Objective and evidence

Complete missing histories and apply webhooks faster while preserving automatic
recovery, client responsiveness and useful background progress. Use existing
scheduling and retirement paths. This round has two independent implementation paths:
a status-dormancy correction, and a publication-chunk experiment accepted only
if measurements support it.

The 2026-10-31 19:22 UTC check found 167 distinct missing collections: 41
issue-comment, 43 review-comment and 83 status histories. All 24 tracked review
histories were complete, but latest applied-webhook lag was about seven minutes
and 270 events were parked. In one owner epoch, 2,050 published histories required
2,021 admissions and 1,713 queued-work yields. GitHub quota remained ample.
Raw evidence is in
`/home/kim/.local/state/tauceti/backlog-drainage/check-20261031T1922/`.

Source establishes two avoidable costs:

- Publication yields after a history whenever any non-publication writer is
  queued. Every subsequent admission opens a PostgreSQL connection, establishes
  its session identity, locks the backend owner, applies work, commits and closes.
  Repeating this setup can increase total input lag even when yielding reduces
  an individual waiter's immediate delay.
- Status dormancy checks whether all historically associated PRs are closed.
  Historical head associations accumulate, so an old head of a still-open PR
  continues to be refreshed even after that PR moves to another head.

An earlier batch completed 100 status histories, of which 96 were historical
heads. That does not establish their pre-fetch dirty state or demand, or prove
all 96 could have been skipped. Separately, 4,944 complete, dirty closed-PR file
jobs remain. The status correction does not eliminate those file jobs: they
continue through their normal final reconciliation and retirement.

## Rules that stay unchanged

Preserve FIFO writer admission, ownership locks, migration barriers, key/token
checks, dirty-generation handling, webhook ordering and atomic receipt commits.
Retain previous complete data during partial publication and failures. Keep
existing member-error rollback, uncertain-commit handling, retry/backoff,
lease-expiry recovery and staging retention. Never replay an uncertain commit.

Preserve repair concurrency, finite scheduling quanta, lane weights, old-request
turns, compatible fetches, automatic cron/alarm continuation and quota decisions.
Keep reads independent of writer admission and anonymous reads demand-free.

Add no sleep, minimum batch fill, cooldown, health gate, admission prerequisite,
queue-priority system, worker setting, dependency service or total-history
execution deadline. Metrics are diagnostic only. Do not modify provider
coordination or the personal `~/.claude` system.

## 1. Measure with existing facilities

Before editing scheduling behavior:

1. Use a read-only SQL breakdown of eligible jobs by operation, lane, complete,
   dirty, requested provenance, recent demand and current/historical status head.
   Use distinct collection keys consistently. Keep closures and superseded heads
   separate from completions; no exact accounting depends on matching a separate
   coverage aggregate.
2. Reuse transaction `phase_ms`, `close_ms`, query counts, ingestion counters,
   lease-renewal counts and grouped-publication metrics. Stored logs may be
   unavailable; use benchmark transaction hooks and bounded existing health
   counters rather than making logging access a prerequisite for progress.
3. If waiter attribution is needed, extend the existing `WriterPurpose` counter
   to report counts by its existing purposes. Count the purposes present at a
   yield; multiple purposes can coexist, so these are not exclusive causes.
   Keep `other` as unknown. Correct clearly mistagged call sites only where their
   purpose is already known; never move a caller into or out of `publication`,
   since that would change the existing yield counter. Forward counters through
   existing wrappers and update them exactly once as waiters enter/start/leave,
   including failures.
   This changes no admission or yield decision and performs no extra SQL.

Do not add runtime job classifiers, a telemetry framework or per-resource labels.
Read-only workload classification belongs in the investigation and fixtures.
Interpret SQLite separately: its standalone reads use the local serial lane,
whereas PostgreSQL stored reads do not join that writer queue.

## 2. Correct status dormancy through the existing retirement path

Make one consistent predicate change in individual `dormant()`, the bulk
companion dormancy check and `enqueueMany`'s dormancy calculation: a historical
status SHA can be dormant when no associated PR is open with that SHA as its
current head. Reuse the existing
indexed `head_prs` association lookup and authoritative PR state under the writer.

- A current head shared by any open PR remains active. Unknown associations,
  missing resources, unavailable head data or failed reads retain ordinary work;
  never interpret missing information as proof of dormancy. Preserve existing
  closed-PR semantics where state is authoritative.
- Preserve recent-demand protection. Outstanding requested work must finish its
  ordinary fetch/publication. Existing complete/clean and newer-generation
  checks normally protect it; assert this in tests. Any additional requested-row
  check belongs in before-fetch retirement/claiming, not in `dormant(q)`, which
  is also used for lane selection and after successful settlement. Use existing
  provenance, without a new persistent demand format.
- Before-fetch retirement still requires complete, clean, unowned work and the
  existing generation/started/lease predicates. Never clear dirty flags or
  fabricate coverage. Dirty or incomplete jobs retain normal reconciliation.
- After a normal successful reconciliation, use existing `finishJob` retirement
  only when no newer generation arrived. Keep the established ownership checks;
  an old completion cannot remove a replacement owner's work.
- New demand, a head change or a reopened PR uses existing enqueue paths to
  recreate work. Verify that dormant enqueue/retirement and shared-SHA cases
  agree. Keep stored resources and coverage, and use normal staging cleanup.

No new scan, background sweep, companion ranking or lane policy is required.
This prevents recurring obsolete status refreshes; it does not promise to clear
all historical obligations immediately.

## 3. Test bounded chunks instead of adding waiter priorities

Reproduce production fragmentation with four full compatible groups and
continuous representative PR/status/comment ingress, client demand, maintenance
and repair bookkeeping. Ingress must continue throughout publication; eight
startup deliveries alone are insufficient.

Compare the current yield-after-one behavior against completing already-prepared
chunks without that queued-work break. Test small fixed caps of two, four and
eight members within the existing 256-KiB/128-row bounds. These are isolated
benchmark variants, not new worker configuration or runtime adaptation.
Oversized items keep their existing individual path; partial chunks run now.
Use separate builds or a test-only substitution unreachable from deployed
bindings/environment variables. An accepted release hardcodes its selected cap
and yield behavior; do not leave a new runtime switch behind.

This experiment applies to grouped page-1 issue-comment, status and reaction
results. Review-comment histories, file histories and other individual paths do
not gain batching from it. Report useful throughput per operation; faster status
refreshes alone do not establish overall missing-history drainage.

The experiment changes when the current transaction ends, not FIFO order.
Queued callers run before the publication remainder rejoins the lane. Each
publication adds at most its bounded chunk ahead of an existing waiter; a waiter
can still have other earlier admissions ahead of it. Item/byte limits are not a
hard wall-clock guarantee, so measure large-history and slow-database holds too.

Choose the smallest cap that produces a repeatable useful-throughput improvement
with acceptable end-to-end input and read latency. Higher admission p95 alone
can accompany lower total application lag; assess both. Do not approve a change
that improves a completion average by materially worsening webhook lag, client
latency, parking, cleanup, lease recovery or maintenance/migration progress.
Earlier fixed-chunk experiments had worse ingress latency; include those
workloads again rather than assuming bounded chunks are automatically better.

If no candidate meets these requirements, keep the existing yield behavior and
report the measured constraint. Do not compensate with urgency classes, another
queue, larger concurrency or artificial waits. A later transaction-setup change
needs a separate concrete proposal based on phase timings.

Leave publication metadata prefetch batched: its current three logical reads are
one PostgreSQL round trip for at most eight keys. Do not replace it with lazy
per-member queries, cache mutable state across commits, or rewrite publication
buffers speculatively. Connection reuse, session-identity setup changes and
moving socket cleanup outside the writer are outside this round; each affects
failure recovery and requires separate evidence and review.

## Verification and rollout

Keep the two runtime changes separately reviewable; neither depends on the other.
Get an independent opinion on each final implementation. Scale focused tests to
the change, then run required package/service/PostgreSQL and memory checks on
its final head. Retain the unchanged 1,200-PR fixture and existing refill/recovery
regressions rather than creating duplicate test frameworks.

For status dormancy, exercise current/historical and shared heads, explicit and
recent demand, dirty/incomplete state, missing metadata, reopen/head-change races,
generation advancement and replacement leases. Prove automatic recreation after
retirement. Check both individual and bulk paths on PostgreSQL and native SQLite,
including native binding limits. Measure query cost on representative backlog
sizes: the change must not add a full-backlog sort under the writer.

For publication, retain whole-history visibility, stale-token, newer-webhook,
rollback/member-failure, uncertain-commit and cleanup-failure cases. Exercise
queued ingress/demand/maintenance/migration arriving during a chunk, oversized
and multi-page histories, continuous backlog, restart and a fetch exceeding its
lease. Verify old requests, cleanup and maintenance advance. Repeat the existing
shared-installation test with two repositories and the real runner scan.

Use repeated alternating comparisons on the same isolated database, serially
without competing CI/benchmarks. Include quiet, burst and sustained input,
realistic historical mixes and slow upstream/writer cases. Save every trial,
source fingerprint and failure. Report useful missing-to-complete throughput,
background refreshes separately, transactions/SQL round trips per completion,
writer holds and admission latency, end-to-end input/client latency, parking,
memory, lease-renewal work and peer/old-request progress. Set material-regression
tolerances from baseline variability before examining candidate results.

After independent review and green CI, deploy one accepted change at a time with
unchanged runtime settings and bindings. No deployment is authorized by writing
this plan. Record a fresh cohort and preserve the previous 24 histories. Verify
both deployed object versions and fresh diagnostics before measuring two
consecutive ten-minute windows and the full explicit `workers.toml` fleet.
Extend if needed for a slow cohort or representative traffic. Normal provider
limits are successful operation. Keep true arrivals, receipt commits, scope exits
and completed distinct histories separate.

Use bounded read-only sampling and stored metrics. Do not enable live tails
solely for measurement, since doing so can update/reset an owner. Missing
telemetry remains a reported limit, never zero observed cost.

Rollback a correctness/recovery regression promptly; rollback a material
performance regression and investigate before the next stage. Preserve committed
receipts, coverage, unfinished jobs, retry deadlines and staging, and verify the
old release can resume automatically. Validly retired historical status jobs need not be
recreated on rollback: the old release must tolerate their absence, and ordinary
head-change, inventory and demand paths must recreate newly relevant work.
This round requires no persistent-format change.
Rollout checks never pause the fleet. Stop observers explicitly and report the
results, remaining constraints and any stage that was not justified by evidence.
