# Repair refill validation

These are isolated fixtures with synthetic upstream responses and zero GitHub
requests. They measure scheduler and publication behavior, not fleet throughput
or production arrival rates. Baseline: upstream `2732eb5`.

## PostgreSQL publication under concurrent ingress

Three alternating trials used four compatible groups of 100 missing histories
and eight issue-comment deliveries to a warm, complete history. Steady ingress
arrived 500 ms apart; burst ingress arrived together. Publication limits remained
eight members, 256 KiB and 128 estimated rows, yielding at whole-history boundaries
to actual queued non-publication work. Every trial committed 400 histories and
all eight deliveries without a failed writer transaction.

| Median | Baseline burst | Refill burst | Baseline steady | Refill steady |
| --- | ---: | ---: | ---: | ---: |
| Drainage, seconds | 146.81 | 138.26 | 146.90 | 141.61 |
| Writer admissions | 83 | 69 | 87 | 73 |
| Publication admissions | 62 | 56 | 66 | 60 |
| Application round trips, including stored reads | 5,230 | 5,214 | 5,246 | 5,230 |
| Ingress p95, seconds | 7.56 | 7.85 | 5.97 | 5.98 |
| Ingress admission p95, seconds | 6.61 | 6.77 | 5.38 | 5.38 |
| Maximum publication hold, seconds | 3.41 | 3.24 | 3.32 | 3.19 |

Drainage improved 5.8% and 3.6%, with 16–17% fewer writer admissions. Ingress
latency did not improve: burst p95 rose 3.8% (287 ms), while steady p95 was nearly
unchanged. Trial ranges overlap; with only eight deliveries per trial, p95 is
the slowest delivery. This supports no claim of a statistically established
latency improvement. Production lag and parked input still need observation.

Refill published about 7.1 members per chunk in burst traffic and 6.7 in steady
traffic, with four and eight queued-work yields respectively. Chunks did not
collapse to one member. Claim/cleanup admissions fell from eight to four.
Native SQLite and cross-backend 400-key lease tests also measure one SQL
statement renewing 399 still-owned rows inside an existing writer admission,
while preserving a replacement token. Near-expiry renewal updated 704–736 rows in burst trials and 736–768 in steady
trials, over roughly 140 seconds; these are repeated matching-token renewals,
not newly completed histories. Renewal happens inside existing admissions.

Three separate 100-history trials without ingress took a median 34.33 seconds
before and 33.21 afterward. Writer admissions fell 18 to 15; publication remained
13 admissions. Including root stored reads, application round trips rose 1,268
to 1,278 (0.8%). Maximum publication hold rose 3.11 to 3.24 seconds. The read-only
eligibility precheck saves writer admissions while adding HTTP reads; counting
writer queries alone would hide that cost.

Raw trials: [publication](validation/refill-publication-postgres.json) and
[analysis](validation/refill-postgres-analysis.json).

## A fetch longer than its lease

Three alternating PostgreSQL trials used 21 independent file histories, one
actual 65-second transport wait, and ordinary 60-second cron calls. Results
retain incomplete baseline work at the same held-hop observation horizon.

| Every trial | Baseline | Refill |
| --- | ---: | ---: |
| Histories complete at the horizon | 4 | 21 |
| Short histories complete before the held response | 3 | 20 |
| Histories still durably missing | 17 | 0 |
| Fetches per attempted history | 1 | 1 |

Median observation time was 67.10 seconds before and 66.65 afterward. Refill
needed 45 writer admissions for 21 completions, versus 13 for four; per-completion
admissions fell about 34%. The candidate ran two ordinary expiry passes and three
coverage/maintenance passes across the cron and quantum boundaries, rather than
one pass per completed group. No total-history deadline was introduced.

Raw trials: [long hop](validation/refill-long-hop-postgres.json).

## Shared installation and runner capacity

Three alternating PostgreSQL trials used two repositories with 16 missing file
histories each, four shared reservations, a two-second initial capacity hold,
100-ms repair reads and an actual 40-request runner scan. The scan inventories
13 repositories and one in-progress run per repository, with 100-ms scan reads.

Refill completed all 32 histories in 20.69–20.96 seconds in every trial. Baseline
completed in 70.60 and 70.84 seconds; its third trial retained three missing
histories at the 90-second horizon. Both repositories made progress. Total writer
admissions were 78 in every candidate trial, versus 121, 121 and 111 before
(the last baseline admission count covers incomplete work). Runner scans all
completed: median 3.612 seconds before and 3.610 afterward. Peak reservations
remained four. Wakes grant no reservations and ordinary requests still acquire
them through the existing installation owner.

Raw trials: [shared installation](validation/refill-shared-installation-postgres.json).

## Sustained SQLite load with registration recovery

Three alternating trials on runtime `566c2d3` used two repositories with 256
missing histories each and the same actual 40-request scan. All 512 histories
and all scans completed in every trial, with at most four reservations.

| Median | Baseline | Refill |
| --- | ---: | ---: |
| Drainage, seconds | 18.25 | 16.15 |
| Writer admissions | 6,345 | 3,114 |
| Runner scan, seconds | 8.96 | 5.12 |

This improves drainage by 11.5% and reduces writer admissions by 50.9%, with
both repositories retaining progress. It exercises Node SQLite with delivered
alarm emulation; native scheduling behavior is covered by workerd tests rather
than inferred from these timings. The original invalid invocation used the
ignored `BENCHMARK_HISTORY_COUNT` and the default four-second window; it tested
16 histories per repository and ended before the scan. That failed invocation
is retained separately and is not counted among these six trials.

[Readable trials](validation/refill-shared-installation-sqlite.json) include
per-repository completions, waits and writer-purpose totals. The
[compressed complete raw data](validation/refill-shared-installation-sqlite.json.gz)
retains every writer record; its decompressed checksum is in the readable file.

## Source and verification scope

The PostgreSQL data measures frozen source `e3971d3` (runtime `acdbabc`),
fingerprint `0567bdfbeeade48ea84d4b8f1c8ab805351d91bbf5be899ddb93f3b17d7442f1`.
It includes every source TypeScript file, the lockfile and the catalog. The
registration implementation at `566c2d3` changes only the optional SQLite
`/availability` caller and that endpoint's bounded advisory registration. The
PostgreSQL caller returns before this path; the runner uses its existing local
hint. These changes do not occur on the measured PostgreSQL execution paths.
The runtime fingerprint is retained in [the source manifest](validation/refill-runtime-source.json).

Focused verification of registration covers owner restart on durable
reservations, lost notifications, repeated full hints, recipient ordering,
invalid optional recipients, stale/failed/full responses and slow notifications.
Persistent unclassified publication faults were exercised over three external
turns on both backends: six webhook receipts committed, read demand persisted,
there was one failed batch per turn and eight writer admissions per turn, and
ordinary recovery progressed after fault removal. A native shared-installation
reset interrupted a runner scan, then an uncached scan succeeded; both repository
owners remained intact, retained durable retries and served independent writes.

The complete service and isolated PostgreSQL suites, publication-memory check,
package tests and unchanged 1,200-PR inventory fixture are CI gates on PR #120.
Node heap measurements do not establish a native isolate memory limit. All
production cohort, backlog, parking/lag, client latency and seven-worker fleet
acceptance measures remain separate from these fixtures.

## Reproduction

Run database workloads serially. For publication, use
`BENCHMARK_LIMITS=8 BENCHMARK_TRIALS=3 BENCHMARK_GROUPS=4
BENCHMARK_HISTORY_COUNT=100 BENCHMARK_INGRESS_COUNT=8 BENCHMARK_FULL_TRIALS=3`
with `node scripts/benchmark-publication.mjs`. Use
`BENCHMARK_TRIALS=3 node scripts/benchmark-refill.mjs` for the real long hop.
PostgreSQL runs require the isolated test application and migration credentials.

For shared-installation PostgreSQL, use `BENCHMARK_TRIALS=3
BENCHMARK_WINDOW_MS=90000 BENCHMARK_SHARED_HISTORIES=16
BENCHMARK_RUNNER_REPOSITORIES=13 BENCHMARK_RUNNER_RUNS=1
BENCHMARK_RUNNER_DELAY_MS=100` with
`node scripts/benchmark-shared-installation.mjs`.
For SQLite sustained load, unset PostgreSQL credentials and use
`BENCHMARK_SHARED_HISTORIES=256 BENCHMARK_WINDOW_MS=20000` with the same scan
settings. This is a Node SQLite fixture with delivered alarm emulation; native
workerd cancellation, alarms and recovery are verified separately.
