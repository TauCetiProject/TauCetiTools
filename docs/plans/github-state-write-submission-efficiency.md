# Make GitHub-state write submissions more efficient

## Objective and scope

Reduce writer occupancy and queueing so webhook application, synchronous PR
correction, and useful background reconciliation progress together. Start by
avoiding unnecessary admissions and reducing work per transaction. Keep one
authoritative writer per repository and the existing durable recovery paths.

Implementation belongs in upstream `TauCetiProject/TauCetiTools`, principally
`services/github-state/src/repository.ts`, `repository_store.ts`, and focused
tests and benchmarks. This plan follows the synchronous-repair changes in
[Tools #131](https://github.com/TauCetiProject/TauCetiTools/pull/131) and
[#132](https://github.com/TauCetiProject/TauCetiTools/pull/132). The investigated
source is `52cd945872dbbc1a89dac29030f1e2915003a64a`; rebase implementation on
current upstream `main` and record its exact revision.

Initial work requires no new service, database, schema, persistent classification,
queue, priority lane, distributed coordinator, acknowledgement protocol, or
operator reset. Infrastructure changes are a separate, evidence-dependent option.
No optimization may make restart recovery depend on an in-memory batch surviving.

## Evidence and limits

The production capture from 2026-11-01 19:52:33–19:55:56 UTC contains 220 distinct
logged PostgreSQL transactions. Their combined writer occupancy was 147.279 s:
78.862 s in the application phase and 68.417 s, or 46.5%, in connection setup,
transaction/session setup, ownership fencing, commit, and close. The application
phase includes SQL network round trips; it is not a measurement of database CPU.

| Purpose | Transactions | Writer occupancy | Mean queue wait |
| --- | ---: | ---: | ---: |
| Ingest | 61 | 47.956 s | 7.189 s |
| Other | 72 | 34.539 s | 4.490 s |
| Publication | 28 | 32.555 s | 4.477 s |
| Repair bookkeeping | 45 | 24.580 s | 4.138 s |
| Read demand | 11 | 6.296 s | 3.545 s |
| Enqueue | 3 | 1.353 s | 4.142 s |

One ingestion transaction waited 15.098 s in the application writer queue and
then occupied the writer for 0.797 s. A synchronous correction took 14.404 s:
9.088 s enqueueing its recovery job, 1.354 s in the installation-owned GitHub
read, and 3.753 s publishing. It joined the writer twice. Other controlled
production corrections took 7.997 and 9.645 s. These findings are recorded in
[Tools #132](https://github.com/TauCetiProject/TauCetiTools/pull/132).

This is a short, changing production workload, not a controlled throughput
benchmark. Transaction logging has a duration threshold and the capture is not
a complete census of all writes. Do not derive a fleet-wide capacity guarantee
or a promised speedup from these averages. Durable Object invocations can
interleave: the HTTP path attached to a log frame does not reliably identify
which operation caused an asynchronously recorded transaction.

A separate investigation used one disposable PostgreSQL test schema, synthetic
data, and mocked GitHub/Bors responses. It made no production writes:

- Three calls to `refreshActivityLandings()` with fresh committed poll metadata
  opened three transactions, made zero application mutations, made zero upstream
  calls, and consumed 1.183 s in total. This proves an avoidable admission exists;
  it does not establish that this operation dominates production's `other` bucket.
- A successful synchronous PR correction used two transactions and one upstream
  read, but left its targeted job dirty. Claiming and processing that satisfied
  obligation subsequently used three transactions and another upstream read.
- The extra background PR preparation includes a standalone update of the job's
  `started` timestamp, separate from claim and publication. Its necessity for a
  scalar, non-paginated PR read deserves a specific audit.

Temporary investigation artifacts are `/tmp/tauceti-write-submission-probe.mjs`,
`/tmp/tauceti-write-submission-probe.json`,
`/tmp/tauceti-sync-repair-structured-logs.json`, and
`/tmp/tauceti-sync-repair-timeout-investigation.json`. Preserve sanitized
regressions and reproducible benchmark results in Tools during implementation;
temporary files must not become runtime dependencies. Keep raw request traces
and credentials out of commits.

Existing optimizations must be treated as the baseline: read demands already
coalesce, ingest scheduling already uses set-based writes, compatible repair
claims already share an admission, and publication already has bounded grouping.
See the recorded [ingest](https://github.com/TauCetiProject/TauCetiTools/blob/main/services/github-state/validation/ingest-batching-2026-10-31.json),
[demand](https://github.com/TauCetiProject/TauCetiTools/blob/main/services/github-state/validation/history-demand-2026-10-31.json),
and [publication results](https://github.com/TauCetiProject/TauCetiTools/blob/main/services/github-state/PUBLICATION-EFFICIENCY-RESULTS.md).

In particular, fixed publication chunks were already rejected. The uncontended
confirmation changed cohort completion from 78.12 to 55.60 s, but worsened input
p95 from 5.39 to 7.55 s. Larger chunks also failed the input-latency criterion.
Keep the current publication yield policy; another batching proposal must offer
new evidence, not repeat the rejected change.

## Invariants

1. Keep repository publication order, revision monotonicity, canonical PR state,
   generation fencing, migration barriers, and the database ownership lock.
   Removing the JavaScript FIFO alone would move contention to that lock and
   would not establish safe concurrent publication.
2. A webhook is acknowledged only after its existing durable disposition
   commits. A repair succeeds only after its correction commits. Preserve
   delivery deduplication, dirty generations, active leases, real retry deadlines,
   complete-collection semantics, and visibility to subsequent readers.
3. Keep durable retry obligations until the same transaction proves them
   satisfied. Unknown ownership or newer work retains ordinary recovery. No
   caller must return to acknowledge completion or unblock another operation.
4. Preserve confirmed-rollback handling and uncertain-commit cleanup. Never
   replay an uncertain write merely because a client lost its response. Cleanup
   of a possibly live owner must retain its exact session identity.
5. Prepare external data before entering the writer. Never wait for GitHub,
   Bors, R2 body retrieval, or batch filling while holding a database transaction.
6. Keep independent readers independent of writer admission and retain normal
   autonomous cron/alarm recovery. Measurements do not become health, freshness,
   or admission prerequisites.
7. Add no repair-response deadline, socket deadline, minimum-fill timer, polling
   loop, cooldown, or queue-size rule that drops work. Existing transport failure
   handling and round cancellation remain unchanged.

## 1. Establish attribution and a reproducible baseline

Use existing transaction IDs, `purpose`, `phase_ms`, queue wait, SQL round-trip
counts, ingestion summaries, and synchronous-repair phase timings. Where `other`
hides the cause, add a small fixed diagnostic operation label at the call site.
It must not affect scheduling or create a persisted operation lifecycle.

Record admissions, committed useful changes, no-op admissions, and writer time
per operation. Distinguish source events, read observations, repair attempts,
retries, completed collections, and retirements. Classify a no-op from actual
application work; a read-looking callback can still require an ownership fence
or a reservation, so do not automatically bypass transactions containing SELECTs.

Freeze an implementation revision and an identical workload before comparing
variants. Reuse the existing real PostgreSQL benchmark harnesses with isolated
`_test_` application/migration credentials. Run database experiments serially and
record overlapping CI or other benchmark activity; retain and exclude whole
contaminated comparison pairs. Do not run schema/fault experiments against
production or the shared parent database.

## 2. Avoid proven unnecessary admissions

First implement a committed read-side fast path for fresh landing-poll metadata.
`refreshActivityLandings()` should discover a still-current transport/version
and poll timestamp without taking the writer. If work may be due, retain the
existing authoritative check and reservation inside the transaction. Missing,
malformed, failed, or outdated evidence must fall through to ordinary recovery.
Do not extend the poll interval, hide prior errors, or turn a cache into ownership.

Audit other high-frequency no-op callers with the same pattern. Prefer batching
related metadata reads and skipping a proven no-op to an additional cache or
timer. Preserve the in-transaction check because concurrent callers can observe
the same old value. Existing demand coalescing and compatible claim batching
must not be counted again as new savings.

Audit the standalone `started` update for scalar background PR reads. Separate
the actual HTTP observation timestamp/revision fence from durable continuation
state. Remove that write only if claim ownership, dirty-generation handling,
lease release, restart, and staged-data addressing remain correct without it.
Do not apply the shortcut to paginated or nested histories, and never fall back
to a timestamp captured before a quota or writer wait as proof of a fresh read.

Accept each shortcut only with a regression showing fewer writer admissions and
identical durable outcomes, including a concurrent caller and a restart. This
step need not depend on the later batching or transport experiments.

## 3. Complete the obligation satisfied by synchronous repair

Keep the targeted recovery job durable before the synchronous network read.
During that existing enqueue transaction, capture the job generation and lease
facts needed to identify the obligation created or observed by this request.
Use an existing write's returned row where possible rather than adding a lookup.

After a full correction is accepted, complete that targeted obligation inside
the same publication transaction only when all of the following hold:

- The canonical correction and its complete PR coverage are being committed.
- The job still has the captured generation and no incompatible pagination or
  continuation state.
- It was unowned at capture and remains unowned; no active/replacement lease or
  newer dirty generation is being completed on another owner's behalf.

Use the existing completion/verification schedule rather than deleting the job
or inventing a new completion marker. Reuse existing interval, requested-work,
and retirement rules. If any condition is uncertain, leave background work
intact: one duplicate read is preferable to losing an obligation. Complete only
PR detail; changed-head files/status work remains independently pending.

Return the committed current record and publish completion diagnostics only
after confirmed commit. Rollback must restore both the old record and pending
obligation. A lost response after commit must remain safe to repeat. Preserve
real installation quota restrictions; satisfying one job does not clear them.

The isolated uncontended regression should eliminate the demonstrated extra
upstream read and its three background admissions. Race tests must deliberately
retain that work when a newer webhook or lease makes completion unsafe. This
is a targeted saving, not a claim that every repair avoids three transactions.

## 4. Reduce round trips within existing transactions

Rank candidates using measured driver round trips and writer occupancy. Batch
independent reads, use set-based updates, and reuse the existing transaction-local
read cache. Preserve buffer flushes when later statements depend on earlier
writes. Do not trade fewer driver calls for more transferred data, repeated JSON
decoding, unbounded SQL, or larger memory peaks. Keep row/byte and native binding
limits and the existing large-publication regression bounds.

One optional experiment is combining BEGIN/settings and session identification
into a single protocol exchange, if the installed driver supports the required
result handling. Capture and validate the exact session identity before attempting
the ownership lock. Do not merge that boundary away: losing an ownership response
must still leave enough identity to clean up precisely that session before
another writer proceeds. Keep generation/schema validation, confirmed COMMIT,
and uncertain-session cleanup unchanged. Reject the experiment if those proofs
become less clear or require new recovery machinery.

Do not introduce an application-global Client/Pool as a quick fix. The current
Neon driver documents request-lifetime restrictions in edge runtimes; retain
request-scoped ownership unless a separately validated design establishes a safe
alternative. See [Neon's driver usage notes](https://neon.com/docs/serverless/serverless-driver#pool-and-client-usage-notes).

## 5. Consider narrow ready-work batching only if still needed

Measure the first three changes before broadening scope. Preserve the rejected
publication experiment's conclusions and the current yield behavior by default.

If admissions still dominate, prototype a narrow batch of already-prepared,
already-waiting ingest data or compatible bookkeeping. Start a lone request
immediately; never wait for a minimum batch size. Bound each batch by existing
row/byte conventions and compare webhook and repair latency as well as aggregate
work completed. Do not build a generic cross-request transaction coalescer.

Each request keeps its ordinary retry owner. Plain prepared data can join a
batch; request-owned network operations or database clients must not be shared
across unrelated request lifetimes. Preserve application order for related PR
updates, per-delivery deduplication, generation increments, and each durable
receipt. Do not collapse source events into a last-value snapshot.

All batch acknowledgements follow confirmed commit. Parse-invalid input is
isolated during preparation. If a data error rolls back a batch, reuse existing
confirmed-rollback member isolation/fallback where applicable. A backend failure
or uncertain commit follows normal receipt/lease recovery, not immediate split
and replay. Healthy siblings and inputs arriving during recovery must not be
orphaned or wait for an operator to restart a batch.

Stop this experiment if it improves cohort time by delaying input application or
targeted repair. Fewer admissions alone are not acceptance.

## Verification and acceptance

Test the actual service/store paths on native SQLite and isolated PostgreSQL.
Preserve meaningful existing tests; add cases for the new behavior rather than
tests that merely reproduce a helper's implementation.

| Scenario | Required result |
| --- | --- |
| Fresh poll metadata; concurrent stale checks | Proven no-ops avoid admission; a due poll is still reserved correctly. |
| Missing/corrupt metadata or poll failure | Existing autonomous retry remains; unrelated work is unaffected. |
| Successful synchronous correction, no races | Canonical reads agree; the same fulfilled obligation does not refetch. |
| Newer webhook/generation, active or replacement lease, continuation | That work survives; no foreign owner is completed or released. |
| Actual HTTP read overlaps newer input | The observation/revision fence still rejects a superseded result without false verification. |
| Failure before commit; lost reply after commit; process restart | Receipts, generations, and normal retry recover without acknowledgement or manual repair. |
| Ownership reply lost; COMMIT uncertain; cleanup delayed | Exact-session cleanup and database fencing remain effective; no blind replay. |
| One invalid item among valid waiting deliveries | Healthy work recovers; duplicate delivery still applies at most once. |
| Shared head, closure/reopen, large/archived body, partial history | Existing identity, lifecycle, coverage, and staging guarantees remain. |
| Quota/capacity wait, empty queue, new arrivals during recovery | Real deadlines and automatic wakeups survive; no work is silently dropped. |
| Migration freeze or backend generation change | Old work cannot publish into the new owner/backend. |

Benchmark quiet input, bursts, and sustained mixed load. Include ordinary and
large webhook bodies, distinct-PR repair reports, coalesced reports for one PR,
background histories, demand, housekeeping, and public/authenticated reads. Use
the same fixed arrival trace and duration for each variant, then account for all
remaining work. A faster cohort must not receive less input or leave uncounted
pending work. Include empty and nonempty upstream histories and slow external
reads outside the writer.

Report admissions and writer occupancy per committed useful unit, SQL round
trips, pending durable obligations, missing-to-complete transitions, webhook
end-to-end and writer-queue p50/p95, synchronous repair p50/p95, read p95,
background completion rate, bytes/memory, and failures/retries. Retirements and
scope exits are separate from useful completions. An input-lag snapshot is not
a latency distribution.

Declare acceptance criteria before examining candidates. Reuse the prior
experiment's starting criteria: at least 5% improvement in fixed-work completion,
input p95 no worse than control × 1.10 + 250 ms, and reader p95 no worse than
control × 1.10 + 100 ms. Add the same input-style non-regression criterion for
targeted repair p95. These are offline comparison criteria, not runtime timeouts
or gates. Require three uncontended alternating pairs per representative workload,
correct durable outcomes, and no unexplained failure/recovery regression. A
targeted no-op optimization can instead demonstrate eliminated admissions for
that case plus non-regression under mixed load; do not promise a fleet speedup
from a synthetic microbenchmark.

## Delivery and production observation

Land independent upstream PRs in this order: attribution and proven no-op
admissions; generation/lease-safe completion of satisfied PR repair; measured SQL
or setup round-trip reductions; optional narrow batching only if still justified.
Keep initial changes schema-compatible and independently reversible. Leave
publication yield, FIFO admission, scheduler lanes/concurrency, provider quotas,
and response lifetime unchanged unless a later proposal supplies separate evidence.

Deploy accepted changes live, with worker and CI admissions enabled. Do not drain,
pause the fleet, reset counters, delete jobs, rewrite generations, or restart all
workers to demonstrate improvement. Check the actual repository and installation
object versions after rollout, allowing for mixed-version propagation. Retain the
same backend/generation and existing rollback/recovery obligations.

Observe comparable busy production windows before and after each change. Confirm
webhooks and targeted corrections reach subsequent readers, useful background
work continues, workers complete useful rounds, and pending obligations recover
through ordinary operation. Keep production observation separate from benchmark
claims; record workload or deployment changes that prevent attribution. Roll
back or fix forward an independently deployed optimization if correctness or
recovery regresses; fewer failures caused by suppressed work are not success.

Consider connection transport, placement, or additional infrastructure only if
measured residual costs justify it. A pooled transport changes session identity,
ownership cleanup, and possibly query caching semantics; it is not a transparent
configuration switch. Broad concurrent publication would require a separate
revision/ownership design. Neither is a prerequisite for the initial savings.
