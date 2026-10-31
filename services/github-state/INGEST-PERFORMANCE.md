# Per-event repair scheduling performance

Measured on 2026-10-31 against isolated real PostgreSQL, comparing full source
and store at `6dc9cd35997ae7fd582a9cfdf4234f3d92f9d7ed` with the implementation fingerprint
in [raw results](validation/ingest-batching-2026-10-31.json). Dependencies are
identical. Three trials alternate version order. Numbers are medians across
trials; timings include ordinary resource/staging preparation, writer admission,
transaction setup, application and commit. No GitHub request is made.

| Workload | Application SQL round trips | Delivery elapsed |
| --- | ---: | ---: |
| pull_request | 41 → 13 | 1373 → 540 ms |
| status | 13 → 10 | 534 → 555 ms |
| issue_comment | 21 → 16 | 712 → 566 ms |
| pull_request_review_comment | 25 → 16 | 785 → 684 ms |
| pull_request_review | 16 → 6 | 694 → 374 ms |
| push | 30 → 12 | 1027 → 622 ms |
| All six deliveries | 146 → 73 | 5059 → 3474 ms |

Each version commits six deliveries in six transactions per trial. A comparison
of all durable job keys, priorities, dirty generations, lanes, attempts and wait
reasons asserts equivalent scheduling. A separate regression compares bulk
scheduling with the retained individual enqueue operation over 64 combinations
of coverage, demand, dirty and missing-only requests, existing retries and active
leases. Duplicate requests retain cumulative dirty generations and last priority.

Application round trips are counted below the buffering/cache layer, after
transaction setup and through the final buffer flush. The measurement includes
SQL I/O, not just CPU. Individual event timings can vary with connection/host load;
this small isolated workload does not establish production ingestion or backlog
drainage rates. It does not exercise burst queue contention or large-history
publication; these remain explicit production and subsequent-phase checks.

Run from `services/github-state` with isolated test application/migration URLs in
`TEST_POSTGRES_URL` and `TEST_POSTGRES_MIGRATION_URL` (usernames containing `_test_`):

```sh
node scripts/benchmark-ingest.mjs
```

`BASELINE_REF` defaults to `6dc9cd3`, `BENCHMARK_TRIALS` to 3 and
`BENCHMARK_OUTPUT` to `/tmp/ingest-benchmark.json`. The baseline must be present
in the Git object database. The baseline compiles all TypeScript source from
that revision, including its own PostgreSQL store. Catalog/contract JSON are
unchanged at this comparison; choose a matching snapshot when using another
baseline. Fixture schemas are dropped when each trial ends.
