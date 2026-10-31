# Grouped history publication measurements

Grouped publication reduces transaction setup and SQL work while preserving
whole-history commits. Small independent flat histories use at most eight
members, 256 KiB of prepared data or 128 rows per chunk. Oversized and coupled
histories retain their individual path. Queued non-publication work ends a
chunk after a complete owned member; the remainder immediately rejoins FIFO.
There is no batch-fill timer, new eligibility wait or queue reorder.

## Controlled PostgreSQL comparison

The 2026-10-31 experiment compares baseline
`38ec297668d30095b41dd67d8d44f1306519b8ef` with application source at
`4d693fcf2017985f9c85707493eb20dec582d748`. The SHA-256 of concatenated
`src/repository.ts`, `src/repository_store.ts` and `src/monitoring.ts` is
`8c6dd267c2d28c76dad20ed45fb8e680f6c4504a823a2e1b73649c36ec0f8485`.
Raw trials are in
[publication-batching-2026-10-31.json](validation/publication-batching-2026-10-31.json).

Each mixed-load trial completes four compatible upstream groups of eight
histories, together with eight issue-comment webhook deliveries to a warm,
complete collection. Burst input releases the deliveries together; steady input
spaces them 500 ms apart. A separate fixture completes one upstream batch of
100 empty histories. Each comparison uses three alternating trials per version
against isolated real PostgreSQL. Upstream responses are fixtures; no GitHub
requests are made. Every target history must commit complete coverage, every
delivery must commit its receipt, and all job leases must be released.

Values below are medians across trials. Maximum publication hold is the median
of each trial's maximum, rather than the maximum across the entire experiment.
SQL counts cover application statements, excluding transport/session setup.

| Measure | Burst before | Burst after | Steady before | Steady after |
| --- | ---: | ---: | ---: | ---: |
| Total elapsed, seconds | 36.14 | 25.84 | 36.17 | 26.63 |
| Total transaction admissions | 53 | 35 | 53 | 39 |
| Application SQL round trips | 1,033 | 769 | 1,033 | 785 |
| Publication transactions | 32 | 14 | 32 | 18 |
| Maximum publication hold, seconds | 1.07 | 3.14 | 1.06 | 2.47 |
| Ingestion completion p95, seconds | 8.00 | 7.54 | 7.30 | 6.83 |
| Ingestion admission p95, seconds | 7.17 | 6.80 | 6.73 | 6.28 |

Mixed-load elapsed time falls by 28.5% for burst input and 26.4% for steady
input. Ingestion p95 improves in both fixtures, although individual publication
transactions hold the writer longer. This tradeoff must be checked again with
production traffic and with continuous slot refill; it does not establish a
universal latency bound.

| Full 100-history batch | Before | After |
| --- | ---: | ---: |
| Total elapsed, seconds | 74.90 | 34.38 |
| Total transaction admissions | 105 | 18 |
| Application SQL round trips | 2,114 | 1,266 |
| Publication transactions | 100 | 13 |
| Maximum publication hold, seconds | 0.90 | 3.03 |

The complete-batch fixture takes 54.1% less elapsed time and uses 87% fewer
publication transactions. It has no concurrent ingestion, so it is not an
ingestion latency measurement.

## Limit selection and contention

The preceding adaptive 2/4/8-member experiment is retained in
[publication-limits-2026-10-31.json](validation/publication-limits-2026-10-31.json).
Its exact source is commit `308bd5563a6837057de7a443d121e0efb77bfa45`; it predates
diagnostic counters and the stale-first-member optimization. Eight members gave
the best combined throughput in that fixture. The table above reruns the selected
application source with four concurrent groups and repeated full-batch trials.

An earlier fixed-chunk candidate improved elapsed time but increased ingestion
p95 by roughly 17–20%. It was rejected. The selected implementation adapts only
to actual queued work at whole-history boundaries. Protected health counters
and the raw benchmark's `grouped_publication` fields confirm that these yields
occur. These counters include refreshes of complete histories and may undercount
uncertain commits; they are not a missing-history completion ledger.

## Reliability and validation scope

Local service checks passed with 510 tests and 91 PostgreSQL-only skips. The
focused real PostgreSQL publication suite passed 31 tests with one SQLite-only
skip. Native Workers tests exercise the binding limits, member-data failures,
alarm ordering and queued-work yielding. Coverage includes consumed-input replay,
lease replacement, concurrent webhooks, mixed exact waits, middle-chunk failures,
uncertain commits and backend exponential recovery. Final-head CI must also run
the full PostgreSQL suite before rollout.

The large-publication memory fixture passed with 11,100 input rows and 44,060
dependent repair jobs using Node's configured 96 MiB old-space flag. The measured
V8 heap limit was 288 MiB and peak sampled RSS was about 503 MiB; this is not a
proof of native isolate memory capacity. The grouped path retains its separate
prepared-byte/row/member limits and supports larger histories individually.

These fixtures measure publication cost, not production backlog drainage. In
particular, coupled review-thread publication remains individual. Completing the
old review-history cohort, measuring arrivals against completions, and removing
the scheduler's whole-wave barrier are separate rollout requirements.

## Reproduction

Configure isolated test PostgreSQL application and migration credentials using
the normal service test environment. Never point the benchmark at production.

```sh
BENCHMARK_GROUPS=4 BENCHMARK_LIMITS=8 BENCHMARK_FULL_TRIALS=3 \
  BENCHMARK_OUTPUT=/tmp/publication-results.json npm run benchmark:publication
npm test
npm run test:publication-memory
npm run test:postgres
```

Run comparisons without another database test workload. Preserve the raw trials,
source fingerprint, environment and final CI results alongside any deployment
measurement.
