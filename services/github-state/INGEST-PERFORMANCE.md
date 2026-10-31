# Per-event repair scheduling performance

Measured on 2026-10-31 against isolated real PostgreSQL, comparing full source
and store at `6dc9cd35997ae7fd582a9cfdf4234f3d92f9d7ed` with the implementation fingerprint
in [raw results](validation/ingest-batching-2026-10-31.json). Dependencies are
identical. Three trials alternate version order. Numbers are medians across
trials; timings include ordinary resource/staging preparation, writer admission,
transaction setup, application and commit. No GitHub request is made.

| Workload | Application SQL round trips | Delivery elapsed |
| --- | ---: | ---: |
| pull_request | 41 → 13 | 1379 → 656 ms |
| status | 13 → 10 | 584 → 458 ms |
| issue_comment | 21 → 16 | 676 → 594 ms |
| pull_request_review_comment | 25 → 16 | 814 → 550 ms |
| pull_request_review | 16 → 6 | 559 → 368 ms |
| push | 30 → 12 | 1077 → 472 ms |
| All six deliveries | 146 → 73 | 5193 → 3104 ms |

Each version commits six deliveries in six transactions per trial. A comparison
of all durable job keys, priorities, dirty generations, lanes, attempts and wait
reasons asserts equivalent scheduling. A separate regression compares bulk
scheduling with the retained individual enqueue operation over 64 combinations
of coverage, demand, dirty and missing-only requests, existing retries and active
leases. Duplicate requests retain cumulative dirty generations and last priority.

A second workload submits eight independent review events concurrently through
the normal per-repository writer. Median burst elapsed is
5005 → 3028 ms, with
128 → 48 SQL round trips and eight committed deliveries in
eight transactions in both versions. Durable repair state is again identical.
This isolates burst admission and scheduling; it is not a replay of all live
webhook shapes at production arrival rates.

Application round trips are counted below the buffering/cache layer, after
transaction setup and through the final buffer flush. The measurement includes
SQL I/O, not just CPU. Individual event timings can vary with connection/host load;
this small isolated workload does not establish production ingestion or backlog
drainage rates. It does not exercise large-history publication or the Cloudflare queue delivery
path; these remain explicit production and subsequent-phase checks.

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
