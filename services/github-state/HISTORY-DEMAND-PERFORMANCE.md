# History demand and repair performance

Measured 2026-10-31 against isolated real PostgreSQL, comparing the complete
source snapshot at `7051ff816e703984565612babdd5fdd80d8cf89d` with the implementation
identified in the [raw results](validation/history-demand-2026-10-31.json).
The source fingerprint hashes `repository.ts` followed by `repository_store.ts`.
The two versions use identical installed dependencies. Each workload ran three
times, alternating version order between trials. Values below are medians of
trial values; p95 is calculated within each trial before taking its median.

| Metric | Before | After | Reduction |
| --- | ---: | ---: | ---: |
| Cache-miss response p95 | 5.18 s | 0.41 s | 92% |
| Cache-miss response p50 | 4.45 s | 0.20 s | 95% |
| Demand bookkeeping transactions | 12 | 3 | 75% |
| Demand durable | 5.18 s | 2.71 s | 48% |
| All four histories complete | 11.62 s | 8.95 s | 23% |
| Total writer transactions, four histories | 26 | 17 | 35% |
| Writer admission p95 | 2.65 s | 1.44 s | 45% |
| All eight batched histories complete | 20.65 s | 7.60 s | 63% |
| Total writer transactions, eight histories | 65 | 14 | 78% |
| Fixture upstream batch calls, eight histories | 4 | 1 | 75% |

## Workloads and correctness

The cache-miss workload uses four PR numbers from the fleet investigation
(#23580, #23597, #23622 and #23657), each seeded with complete headers and issue
comments but an incomplete review-comment collection and four-hour-old demand.
Thirty concurrent calls to the authenticated repository `/query` handler repeat
those four histories and one progress query six times. A controlled writer
transaction holds admission for 500 ms; its own transaction is excluded from
counts. All replies must correctly report unavailable coverage. The normal
alarm, leased scheduler and publication code must then complete all four
histories and clear their requested backlog. Each version makes exactly four
fixture upstream reads and completes all four histories in every trial.

The companion workload seeds eight missing issue-comment histories and invokes
the real `runAlarm` scheduler. All eight must become complete. The previous
scheduler lets several leaders race to claim companions, fragmenting this cohort
into four upstream batches. Preparing a complete group before selecting another
leader uses one upstream batch. A separate mixed-backlog regression checks that
25 thread histories form 10/10/5 batches while an unrelated request runs in the
fourth slot. A native Workers-runtime regression verifies a full 100-history
batch on Durable Object SQLite, including claims and lease cleanup under its
100-bound-parameter limit.

Counts instrument the root PostgreSQL store's `transaction` method, including
standalone mutations that enter it internally. Read-only HTTP database queries
are excluded. The writer-admission interval runs from transaction invocation to
its application callback, so it includes connection setup and ownership checks
as well as queueing. Slow-transaction production telemetry separately reports
queue wait and transaction phases, now with a bounded caller-purpose field.

## Scope and reproduction

These workloads use empty synthetic history responses; they make **zero GitHub
requests**. They measure repository bookkeeping, batching and publication against
a real database from this host. Unrelated storage diagnostics are disabled for
both versions; companion-workload survey maintenance is also excluded equally.
They do not measure Cloudflare network latency, large real history payloads,
production backlog drainage, unchanged review attempts or fleet throughput.
Production fixed-cohort completion and oldest real-demand age still need a
post-deployment observation. Database and host load can change timings.

Run from `services/github-state` with isolated test application and migration
credentials, whose usernames must contain `_test_`:

```sh
npm ci
npm run benchmark:history-demand
```

`TEST_POSTGRES_URL` and `TEST_POSTGRES_MIGRATION_URL` must be set in the
environment. `BASELINE_REF` defaults to `7051ff8`; `BENCHMARK_TRIALS` defaults to
3. `BENCHMARK_OUTPUT` selects the result path (default
`/tmp/history-demand-benchmark.json`). The baseline must be available in the
local Git object database. The harness compiles all baseline source modules,
including its PostgreSQL store, rather than mixing old scheduling with the new
store. Every isolated fixture schema is dropped at completion.
