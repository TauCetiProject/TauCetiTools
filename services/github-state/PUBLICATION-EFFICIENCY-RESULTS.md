# Publication efficiency results (2026-10-31)

The status retirement fix is merged in [PR #123](https://github.com/TauCetiProject/TauCetiTools/pull/123)
and deployed. Fixed publication chunks of two, four and eight members were
rejected because they increased webhook ingestion latency. The current publication
yield policy remains in place. No new runtime setting, queue, priority, wait,
health prerequisite or provider coordination was introduced.

## Accepted status correction

Complete, clean, unowned status jobs for superseded PR heads can retire through
the existing reconciliation paths. A SHA shared by a current open PR remains
active. Missing/unknown head information retains ordinary work. Outstanding
requested work, dirty generations and replacement leases retain their protections;
new demand, reopening and head changes recreate work automatically. Stored
resources and coverage remain intact.

The lookup uses indexed head associations and a compact materialized projection,
without a backlog sort. Unrelated enqueue batches do not parse PR head data.
The PostgreSQL JSON projection preserves string typing and tolerates body text
that PostgreSQL JSONB cannot represent without changing the stored resource.

[Status benchmark](STATUS-DORMANCY-PERFORMANCE.md): three alternating PostgreSQL
trials with 5,000 head associations and 100 complete/clean histories reduced
median elapsed time from 38.46 s to 1.66 s, writer admissions from 14 to 2,
application driver round trips from 1,364 to 43 and upstream histories requested
from 100 to 2. All 100 complete collections remained readable. These are avoided
background refreshes, not new missing-history completions or a fleet throughput
multiplier.

Focused SQLite/native and PostgreSQL checks cover current, historical, shared,
unknown and non-string heads; demand; dirty/incomplete work; generation and lease
replacement; recreation; and 130-head native binding limits. Required CI
[41237040855](https://github.com/TauCetiProject/TauCetiTools/actions/runs/41237040855)
passed: 403 package tests, 604 service tests, 245 PostgreSQL tests, publication
memory and the unchanged 1,200-PR inventory fixture. Platform-specific skips were
93 in the service run and 15 in PostgreSQL. The merge tree `3911713` is identical
to the tested synthetic merge, including PR #121's queue setting and wait metrics.

## Publication experiment

The frozen confirmation benchmark builds the real FIFO writer with test-only
substitutions
for fixed caps 2/4/8 and removal of the queued-work break. Runtime source is
unchanged. Four compatible issue-comment groups publish while mixed status,
PR and issue-comment ingress arrives once a second until cohort completion.
Read-only client queries run on each arrival; demand and maintenance are called
periodically, but can be read-only/no-op. Storage preparation is disabled.
All upstream histories in this fixture are empty, and upstream services are
mocked: no GitHub requests are made.

Initial 4×16-history screening used an earlier, unhashed harness revision,
with the control first in a single trial and reads every ten arrivals (only
4–8 reader samples per row). Its later-fixed promise/cleanup and sampling
limitations are retained in the record; it is preliminary input-latency evidence.

Screening results:

| Yield policy | Cohort completion, s | Input p95, s | Publication admissions |
| --- | ---: | ---: | ---: |
| Current | 78.18 | 5.29 | 65 |
| Fixed 2 | 55.97 | 7.47 | 33 |
| Fixed 4 | 39.44 | 11.06 | 17 |
| Fixed 8 | 32.72 | 18.36 | 9 |

The input acceptance limit was declared before examining candidates:
control median p95 × 1.10 + 250 ms. Reader p95 allowance was × 1.10 + 100 ms,
and useful cohort completion had to improve by at least 5%. These are benchmark
acceptance criteria, not runtime eligibility rules. Every screening candidate
failed the input criterion, despite the empty histories favoring batching.

Three uncontended, alternating 4×16-history pairs confirmed rejection of cap 2:

| Median | Current | Fixed 2 |
| --- | ---: | ---: |
| Cohort completion, s | 78.12 | 55.60 |
| Input p95, s | 5.39 | 7.55 |
| Reader p95, ms | 179 | 202 |
| Publication admissions | 65 | 33 |

Cap 2 exceeds the input limit of 6.18 s. All histories completed and no healthy-fixture writer failed.

CI job timestamps exposed contention in the original confirmation pairs 0/2
and both first-replacement pairs. Entire pairs were excluded, including their
uncontended halves. The clean set is original pair 1 plus the two final pairs
starting after 20:46 UTC; no subsequent CI run was present during that interval.
Every excluded row remains in the record.

The auxiliary 4×100, burst and quiet runs also overlapped CI and are not acceptance
evidence. Both variants completed 400 histories in the large run; input p95 was
5.48 s for current yield versus 7.69 s for cap 2. Burst input p95 was 6.90 versus
9.30 s. Quiet 64-history cohort time was 26.84 versus 31.76 s; absent contention,
current yield can already use its normal eight-item chunks. These single pairs
cannot establish a clean performance comparison.

Arrival rate is fixed but cohort horizons and delivery counts differ. A faster
cohort can leave more deliveries pending; neither cohort time nor the time to
commit all arrivals establishes combined workload throughput. Completions are
counted after confirmed transaction return. For the frozen confirmation and
auxiliary runs, source, variant and harness hashes,
per-purpose writer time and outstanding input at cohort completion are retained.
Purpose-at-yield counts are nonexclusive observations, not exclusive causes.

The existing large-history, failure, recovery, memory and shared-installation
regressions passed for the accepted status-only release. Further candidate-specific
large/slow/migration/recovery qualification and three uncontended 4×100 trials
were not pursued for an already rejected yield change. No publication runtime
change was deployed.

## Production observation

Version `52b54f3d-cee3-45b8-9f4d-473fe45dd8d7`, merge `3911713`, took 100% traffic
at 20:16 UTC. Both state and quota objects reported this version with fresh,
connected diagnostics before the prepared baseline at 20:22:36 UTC. Existing
bindings and queue consumer settings were verified unchanged; concurrency 16
was already deployed by PR #121.

The original 20:22–20:42 UTC window went from 271 to 113 to 1 distinct missing
open-PR issue-comment/review-comment/current-status collections. Its first ten
minutes observed 15 issue-comment, 60 review-comment and 110 status distinct
missing-to-complete transitions, plus two missing keys leaving scope. PR #122
reached the objects at 20:37 UTC, so the second half is a mixed-deployment window
and is not used as a stable-release comparison.

The successor `02f735d7-b164-44bc-9262-723c473c4623` is merged main `8bfd5ca`,
which includes the status fix, with identical bindings and runtime settings. Its
code also includes PR #122. After its initial stale diagnostic sample, a second prepared 21-sample window ran 20:38:37–20:58:37
UTC. Both object versions matched throughout; all 21 storage checks were connected
and diagnostics fresh.

| Stable successor window | Missing at start → end | Distinct issue-comment completions | Distinct review-comment completions | Distinct status completions |
| --- | ---: | ---: | ---: | ---: |
| 20:38–20:48 UTC | 10 → 3 | 4 | 5 | 26 |
| 20:48–20:58 UTC | 3 → 0 | 7 | 6 | 17 |

There were 55 observed entries into missing scope and 65 missing-to-complete
transitions across those windows; none of the missing reduction came from scope
exits. Entries into missing scope are not a count of raw GitHub webhook arrivals.
Complete scope entries and background refreshes are not credited as hydration.
The zero is for these three collections on currently open PRs, not every queued
operation or every historical PR.

All eight fresh status keys were complete by 20:35 UTC, before the successor
rollout. Two were already complete at the first prepared baseline; the other six
completed during the observation. All 24 retained review histories remained
complete. Post-window authenticated reads returned complete results for all 24
review histories, four progress queries and eight status histories. Anonymous
survey pagination returned two complete pages (500 + 86 rows).

All seven configured worker identities were observed alive in every stable sample,
with zero restarts and no degraded GitHub instrumentation at the end. All retained
the existing default cap of 32. The final states were one running a review, five
waiting for provider quota and one in its progress-attempt backoff; one worker had
a pending publication. These waits are not evidence of a new state-server prerequisite.
In that final sample, provider availability/pacing was the immediate fleet constraint.

The stable-window latest-applied-input lag gauge ranged up to 23.10 s and ended
at 13.55 s. Parked input peaked at 26 and ended at zero. One observed R2 listing
throttle (`10058`, concurrent request rate) produced a failure counter of one in
two consecutive samples; existing retry/backoff cleared it without intervention,
and the final failure counter was zero. No persistent recovery error or worker
restart was observed. This is not a claim of zero transient failures.

Both bounded observers exited normally and recorded stop receipts. No live tail
was started. The archived job profile still includes 4,907 complete, dirty,
closed-PR file jobs eligible in the archive lane; these require their ordinary
final reconciliation and are outside the status-head correction.


Minute snapshots can miss short transitions; queue metrics are approximate and
can lag. The latest-applied-input lag is a sampled gauge, not a latency distribution.
The production workload has no matched control and includes both the prior queue
change and PR #122’s housekeeping change, so improvement cannot be attributed
solely to status retirement. Job retirements and scope exits are never counted as hydration.

## Separate existing SQLite wake-up gap

Post-merge main CI [41239553470](https://github.com/TauCetiProject/TauCetiTools/actions/runs/41239553470)
passed PostgreSQL but failed the SQLite shared-installation case at 15/16 histories
for one owner. Five focused local repeats passed. The failure snapshot nevertheless
shows a real ready-work gap: an overdue, unleased capacity-wait job remained while
the only durable alarm was a completed file job's annual refresh.

A capacity notification can arrive before the rejected job registers its exclusion.
The notification is then spent; the SQLite branch of `scheduleCapacityDeadline`
returns without arming the in-memory fallback, and alarm selection can omit the
now-overdue excluded job. An ordinary wake, including configured minute cron,
resumes it. The exact interleaving is inferred from the snapshot. These paths
are unchanged between `cf29ced` and `8bfd5ca`; neither status retirement nor PR #122
introduced the gap. Production uses PostgreSQL, whose existing deadline timer
covers this path, and no corresponding production stall was observed.

A separate small fix should remove the SQLite-only timer exception, enable the
existing deadline tests on SQLite and retain shared-capacity launch/alarm bounds.
Increasing the test timeout would hide the gap. This round does not change that
scheduler behavior; the passing PR #123 validation above is not a claim that all
later main runs passed.

## Remaining constraint and reproduction

Publication is still fragmented under competing ingest. The experiment exposes
a real tradeoff: fewer remote PostgreSQL transactions reduce setup overhead, but
longer chunks delay ingress. The next proposal should target measured connection,
session/owner setup and query cost, with separate recovery review, rather than
adding scheduling machinery or increasing concurrency. In the uncontended 64-history control, per-phase medians were connect 67 ms,
begin 29 ms, session 24 ms, owner lock 23 ms, apply 373 ms, commit 24 ms and
close 26 ms. These independent phase medians are not an additive measured hold.
Both setup and application SQL cost deserve measurement before a separate change.
Dirty historical status and closed-PR file work still needs its normal final reconciliation.

Run database workloads serially, with isolated `_test_` application/migration
credentials. `scripts/benchmark-publication-sustained.mjs` defaults to the current
HEAD as its control and refuses source/schema differences from `BASELINE_REF`.
Options `BENCHMARK_CAPS`, `BENCHMARK_TRIALS`, `BENCHMARK_HISTORY_COUNT` (1–100),
`BENCHMARK_MODE` (steady/burst/quiet) and `BENCHMARK_OUTPUT` affect this script only.
For example, from `services/github-state`:

```sh
BENCHMARK_CAPS=2 BENCHMARK_TRIALS=3 BENCHMARK_HISTORY_COUNT=100 \
  BENCHMARK_MODE=steady BENCHMARK_OUTPUT=/tmp/publication.json \
  node scripts/benchmark-publication-sustained.mjs
```

The frozen confirmation and auxiliary measurements used harness commit `5754ddc` and control
`cf29ced`, before status retirement. To reproduce that source comparison, check
out `5754ddc` in a separate worktree and install its locked dependencies. Run the
script there; setting `BASELINE_REF` on different runtime source is intentionally
rejected. The screening predates that commit and has no harness hash.
The final script's HEAD default and preparation-error cleanup are reproduction
conveniences added after measurement, not the harness that produced the historical
measurements. The preparation check observes the already registered repair groups;
it adds no timer or execution deadline. Unexpected fixture paths are also retained
for a final assertion even if a repair retry absorbs the original error. The
current script passed a four-group check on both control and cap 2; injected alarm
rejection and empty dispatch both failed as expected and closed the test fixture.

[Condensed trials and provenance](validation/publication-efficiency.json) preserve
all trials, exclusions, decisions and fingerprints; the complete raw logs,
including fixture-seeding failures and excluded measurements, remain in the
operator's `publication-efficiency` investigation directory. No failing trial was
silently replaced in the record.
