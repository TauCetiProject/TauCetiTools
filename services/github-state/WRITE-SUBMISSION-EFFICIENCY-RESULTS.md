# GitHub-state write submission efficiency

The implementation starts from upstream `229fab98865e03632b369b08f745beecb31a1054`.
It removes proven unnecessary work while retaining the repository FIFO, database
ownership lock, session-identification boundary, and current publication yielding.

- Fresh landing-poll metadata now needs one committed read and no writer admission.
  Due checks still reserve atomically. Invalid metadata, concurrent reservations,
  failed polls, and reservation rollback retain ordinary recovery.
- A synchronous correction can complete its unchanged, unowned PR obligation in
  the correction transaction. Newer generations, uncertain identities, active or
  expired leases, continuations, and started/deferred recovery remain pending.
  Files/status obligations and installation quota restrictions remain independent.
- Synchronous completion metrics merge only after confirmed commit, with distinct
  event names that do not change background requested-latency statistics.
- Background PR preparation batches revision and validator reads. Completion uses
  existing transaction-local caching for job/demand facts. The durable start write
  remains: removing it would need a separate audit of the shared continuation and
  staging paths.
- Transaction diagnostics add fixed operation names and writer-application query
  counts. Those fields do not affect admission, yielding, or retry ownership.

Background `requested_latency` loses the duplicate PR completions satisfied by
the synchronous path; its remaining population can become slower even when
service improves. Compare like-for-like background cohorts and inspect
`synchronous_*` events and `pr_refresh` timings separately.

There is no schema change, global connection pool, service, batching timer,
response deadline, scheduler redesign, or operator reset. The bounded savings do
not justify the optional broader batching or connection-protocol experiments.

## Validation

`npm test` passed 668 tests; its 93 PostgreSQL-only skips are expected without test
credentials. The focused PostgreSQL suite passed all 41 PR-repair and landing-poll
regressions, including post-completion rollback, lost committed replies, restart,
newer equivalent input, lease/continuation/retry races, malformed metadata, and
quota preservation. An additional expired-staging recovery regression passed on
both backends. All 64 connection-failure/fencing tests passed, as did the
existing large-publication memory check (11,100 input rows, 11,015 published rows,
44,060 dependent jobs). Each intermediate branch also passed typechecking and its
focused native regressions.

## Comparison method and limits

The accepted artifact records the source commit, source/harness fingerprints,
Node version, exact arrivals, per-operation timings, writer admissions, SQL calls,
upstream calls, committed deliveries, and remaining dirty generations. Each
variant uses a disposable PostgreSQL schema and mocked upstreams; no production
writes or real GitHub/Bors requests occur. Comparisons run serially with three
alternating pairs for each of quiet, burst, and sustained arrivals.

Each run includes three PR corrections, four webhook deliveries (one with an
80 KB body), two histories (empty and nonempty), one due receipt poll and later
poll checks, and four direct stored-PR reads. It accounts for the same PR
obligations and verifies identical pending fanout generations. The baseline's
remaining PR obligations are processed sequentially after foreground input; this
is a targeted demonstration of eliminated work under mixed arrivals, not a
measurement of maximum system throughput. Reader timings exclude HTTP/edge and
demand overhead. Housekeeping and installation quota pressure are covered by
other correctness tests, not this benchmark.

Latency is measured from scheduled arrival, including local dispatch delay. With
three/four observations per kind per trial, report **median trial maxima**, not
production p95 estimates. Apply the plan's comparison margins to those maxima:
completion at least 5% faster; input/repair no worse than control × 1.10 + 250 ms;
reader no worse than control × 1.10 + 100 ms. These are offline comparisons, not
runtime limits. Production latency distributions still require comparable live
observation after rollout.

SQL counts distinguish writer-application calls, standalone HTTP read calls, and
the four existing sequential BEGIN/session/owner/COMMIT driver calls per committed
transaction. Connection handshakes are excluded. Outer store wrapper counts are
reported separately because one wrapper spans several setup commands. Counts
include the harness's final state verification reads in both variants.

Exploratory captures are excluded from acceptance: an initial fixture lost its
receipt URL during ingestion; another comparison was invalidated by the source
fingerprint guard while corrections were being incorporated. Only the frozen
accepted capture below supports the reported comparison.

## Accepted measurements

[Full measurements and fingerprints](validation/write-submissions-2026-11-01.json).

Exact measured source/harness commit: `29640295def0bc9f19b3408be06e8079ef0c7939`.
The final harness additionally spells out maxima and accounting scope and asserts
that no demand writer ran; the measured workload and runtime source are unchanged.

| Workload | Completion, s | Writer admissions | Writer occupancy, s | GitHub reads |
| --- | ---: | ---: | ---: | ---: |
| quiet | 13.93 → 8.96 | 27 → 16 | 13.11 → 8.52 | 8 → 5 |
| burst | 14.08 → 9.60 | 27 → 18 | 13.31 → 9.14 | 8 → 5 |
| sustained | 13.91 → 8.98 | 27 → 16 | 13.12 → 8.56 | 8 → 5 |

| Workload | Input maximum, ms | Repair maximum, ms | Reader maximum, ms | All counted SQL calls |
| --- | ---: | ---: | ---: | ---: |
| quiet | 2126 → 2028 | 5074 → 5027 | 48 → 48 | 457 → 324 |
| burst | 3804 → 3397 | 8814 → 9021 | 26 → 22 | 456 → 333 |
| sustained | 4340 → 3839 | 7480 → 7227 | 26 → 23 | 456 → 323 |

All three modes passed every comparison margin. Each committed four deliveries,
completed the same three PR obligations and two histories, retained identical
pending fanout generations, and recorded no failed transaction. Bors was read
once in each variant. No Tools CI run overlapped the accepted capture
(2026-11-01T21:16:04.915Z to 2026-11-01T21:21:04.667Z); local database
experiments were run serially. No production state or admission setting changed.

## Reproduction and rollout

With isolated `_test_` application/migration credentials in `TEST_POSTGRES_URL` and
`TEST_POSTGRES_MIGRATION_URL`, run from `services/github-state`:

```sh
BASELINE_REF=229fab98865e03632b369b08f745beecb31a1054 npm run benchmark:write-submissions
```

The implementation is split into independent no-op/diagnostic, synchronous
completion, and SQL-reduction PRs, in that merge order. All target upstream main.
Optional transport or broader batching work needs new evidence. Production was
not changed during implementation. Deploy accepted changes live with normal
admissions enabled, then observe input application, synchronous correction,
useful background completion, and autonomous recovery as specified in the plan.
