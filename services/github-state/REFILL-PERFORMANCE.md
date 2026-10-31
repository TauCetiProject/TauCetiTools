# Repair refill validation

Verification is in progress. Refill has not been deployed. Each result names
the runtime revision it measures. Production remains on the separately measured grouped-publication
release. These results are isolated fixtures, not fleet throughput claims.

## Shared installation under sustained repair load

Three alternating trials compared `2732eb5` with runtime source `9e48298` using
two repositories with 256 missing file histories each, four shared reservations,
100-ms fixture upstream reads, a two-second initial capacity hold, ordinary
SQLite alarms and the actual runner-capacity scanner. The scanner inventories
two empty repositories in five requests. All histories and scans completed in
every trial.

| Median | Previous revision | Refill |
| --- | ---: | ---: |
| Repair drainage | 17,238 ms | 15,220 ms |
| Transaction admissions | 5,957 | 2,078 |
| Runner scan | 2,004 ms | 2,005 ms |

An earlier refill candidate completed repairs but starved the scanner until its
existing deadline. Local scan recipients now share the bounded capacity-release
wake mechanism after an actual scoped rejection, retaining the real retry
deadline, timed fallback and ordinary reservation acquisition. The three trials
above include that correction. A longer, realistic scan remains to be measured.

## Verification status

- The service source passes 564 tests; 93 PostgreSQL-only cases are skipped in
  that run. The 1,200-PR inventory fixture keeps its original deadline and data.
- The 11,100-row publication-memory fixture passes with 44,060 dependent jobs.
  Its Node heap measurements do not establish a native isolate memory limit.
- The preceding candidate `31aca6b` passed the complete isolated PostgreSQL
  suite: 223 passes and three SQLite-only skips. The final-source run is active.
- A real native SQLite alarm test now verifies that paced member retries retain
  failed tails across firing scheduling fallbacks, avoid external maintenance,
  re-arm durable recovery and reset normally on a genuine tick. Uncertain alarm
  writes lose optional tags and recover externally.
- Final real PostgreSQL comparisons still need four full 100-history groups
  under steady and burst ingress, a real fetch longer than the lease, and
  shared-installation fairness with a realistic runner scan.

The benchmark programs use fixture upstreams and make no GitHub requests.
PostgreSQL runs require the documented isolated test application and migration
credentials. Run the publication benchmark with `BENCHMARK_LIMITS=8`,
`BENCHMARK_GROUPS=4`, `BENCHMARK_HISTORY_COUNT=100` and
`BENCHMARK_TRIALS=3`; run `scripts/benchmark-refill.mjs` and
`scripts/benchmark-shared-installation.mjs` separately against the same baseline.
Do not run database benchmarks concurrently with another database test suite.

Merge, deployment and the prepared production/fleet observation remain pending.
