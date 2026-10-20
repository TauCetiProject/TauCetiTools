# GitHub state retention and outage drills

This covers SQLite/R2 and PostgreSQL prevention for [Worker
#317](https://github.com/TauCetiProject/TauCetiWorker/issues/317). The PostgreSQL
implementation uses the store delivered in [TauCetiTools
#5](https://github.com/TauCetiProject/TauCetiTools/pull/5), which completed Worker
#308. Recovery/replay remains owned by the existing scheduled ingress controller
(Worker #336). Production migration and backend selection remain Worker #316's
release; these diagnostics do not select a backend or bootstrap GitHub.

## Ownership and storage policy

The shared-state service operator owns capacity alarms, retention overrides, archive
receipts and replay. The existing installation scheduler exclusively owns GitHub
quotas. The PostgreSQL backend operator owns the dedicated Neon project's effective
account/project allowance, compute, connections and restore history after S1.
Automatically growing storage is not a hard allowance; do not reuse SQLite's 10 GiB
threshold for PostgreSQL.

| Data | Default window / retirement condition |
| --- | --- |
| Imported PostgreSQL SQL cursors | Delete only after their stored ten-minute expiry, in bounded scans; SQLite no longer creates SQL cursors |
| R2 cursors | Ten-minute validity plus the existing one-minute deletion margin |
| Change journal | Seven days; clients beyond retained revisions receive reset |
| Delivery receipts | Fourteen days **from application**; automatically pin the entire class while parked/held input is observed, or its inventory is unavailable |
| Legacy raw repair telemetry | Seven days; new repairs and query counts use bounded counters and latency buckets instead of SQL samples |
| Installation accounting | Seven days; independent bounded maintenance also runs while repository ingestion/auditing is paused |
| Aggregated diagnostic intervals | Seven days in R2; no payloads, credentials or event bodies |
| Raw webhooks | Fourteen days; a parked body is a separate digest-verified, non-expiring copy |
| Daily observational archives | No longer written; earlier ones expire after seven days |
| Staging pages | One day; an expired traversal restarts through the existing quota owner |
| Demand | One day (its scheduling significance lasts five minutes); new automatic expiry starts in dry run |
| PR headers, histories, coverage, tombstones, landing evidence | Retained; quiet or closed does not authorize deletion |
| Workflow attempts and obsolete heads | Automatic retention preserves stored attempts, head data and memberships. S0 stops mirroring checks/workflow runs/jobs and retires their polling jobs regardless of demand. Other dormant polling retains its existing guards |
| Survey projections/inventory | Owned by their source PR/history. Retain source and coverage; rebuild through existing invalidation, never GitHub refetch for retention |
| CI records | Not mirrored. The `ci_resources`, `ci_members`, `ci_collections` and `ci_meta` cleanup classes delete stored rows from earlier versions, without an archive |
| Resource spills | Retain indefinitely until a complete reference index and explicit archive-release protocol can prove retirement safe |
| Migration exports and parked/held bodies/pointers | Pinned; normal expiry never deletes them, even with accidental expiry metadata |

A resource can be absent from live SQL while a retained cursor, snapshot or pinned
recovery export still references its spill. Age alone is insufficient. The previous
retirement grace period cannot prove a pinned recovery export has been released, so
automatic resource-spill deletion is suspended. Include this conservative R2 growth
in operator capacity planning; this release does not claim to complete
reference-graph garbage collection.

S0's existing frozen-mirror behavior remains: a degraded mirror with no applied
changes for ten minutes reports `mirror_frozen` unavailable. This release adds no
GitHub requests to status, cleanup, archival, or recovery.

## Bounded work and independent recovery

SQL expiry runs before alarm dispatch/hydration and on every cron tick before alarm
scheduling, using the cached retention policy. R2 monitoring, archival and
checkpoint work runs in the background with per-operation I/O deadlines; it does not
delay alarm re-arming or dispatch. One pass examines at most 6,000 rows, accounts
for at most 4 MiB of logical row bytes, with a 50 ms cooperative check between
operations. Synchronous SQL is bounded by rows and bytes: Workers clocks advance
only after I/O, so this check is not a measurable 50 ms CPU limit. Profile CPU on
test infrastructure before rollout ([Cloudflare timer
semantics](https://developers.cloudflare.com/workers/runtime-apis/performance/)).
Each class examines at most 1,000 rows; indexed repair
sample expiry reads only eligible rows. Rowid keysets bound rows examined as well as
deleted; unindexed expiry predicates do not silently scan an entire table. Classes
rotate to prevent starvation.

Checkpoints in `control/<repo>/retention-checkpoint.json` record the epoch upper
rowid, scanned position, examined/eligible/deleted counts, logical bytes, oldest
expired timestamp encountered, and last completed scan. The oldest encountered
timestamp includes rows just removed; it is not a count of remaining backlog. A
class with an old completion time or an unfinished scan is behind. Changed
checkpoints are flushed at most once per thirty seconds. SQL deletes become durable
before their R2 checkpoint. A failed checkpoint repeats safe deletes; current expiry
is rechecked, including reused rowids. Unknown retention policy or parked inventory
fails closed. A cold R2 policy outage cannot prove a durable pin is absent, so
automatic expiry waits for that policy; the explicit emergency SQL cleanup commands
remain available without R2. Cached policy expiry remains independent of later R2
monitoring failures. Other recovery attempts still run. New demand deletion starts
in dry run; S0's receipt and other existing expiry classes remain active by default,
subject to pins/explicit class overrides. SQLite needs no new SQL schema. PostgreSQL monitoring requires the additive
`002_monitoring.sql` function installed explicitly with migration credentials. Background retention and archival participate
in the source-migration drain barrier and stop at its fence. Bucket expiry
rechecks the migration hold before batch deletion; migration inventories include
the staging-spill prefix.

Checks, workflow runs and workflow jobs are not mirrored; their queries report
`not_mirrored`. Commit statuses remain mirrored. There is no R2 archival of
resource payloads.

R2 expiry scans separate cursor, webhook, snapshot, staging, staging-spill and
telemetry prefixes, avoiding the pinned resource/export inventory. Each pass lists
at most 100 objects (500 cursors), deletes at most 100 MiB, using one batch deletion
operation per page. Oversized objects are reported and retained for operator action,
without preventing other objects from expiring. A budget stop repeats the current
page; a failed delete/checkpoint does not advance it. Protected prefixes and
`pinned: true` metadata win over expiry. Class pins and migration holds are rechecked
before each delete request; an R2 batch already dispatched cannot be recalled.
Quiesce cleanup and wait for outstanding batches before relying on a new pin to
protect an already expired object. Checkpoints live in
`control/retention/<class>.json`. Legacy expiring staging spills under `resources/`
have a separate conservative sweep that only deletes `kind: cursor` objects after
expiry. Authoritative resource spills remain protected. An expired staged
continuation restarts its traversal; a transport error retries without discarding
its checkpoint. A single storage operation may outlast a cooperative time budget.

Completed SQL epochs wait one minute before restarting. This avoids repeatedly
scanning the live window on every hot repair alarm. Operator policy updates are
serialized, and generation checks prevent a delayed cold load from overwriting
an acknowledged pin.

Malformed scalar/collection envelopes, missing identities/status fields and
nonprogressing pagination remain explicitly unavailable, with request IDs. They
do not publish empty complete coverage or trigger speculative API fanout. Existing
quota-driven recipe selection and complete-label fallback remain available.

## Prepared monitoring and alarms

Cron prepares capacity/ingress observations each minute in
`control/<repo>/observation.json`, outside SQLite. `/v1/health` serves these and
prepared repository diagnostics without live history/accounting aggregation or
GitHub calls. Cron/alarms refresh the legacy diagnostics projection at most once a
minute, including quiet/degraded stores. `reconciliation_version` remains available
even before the first projection is prepared. Missing or older-than-three-minute
observations are explicitly stale. A cold instance can serve the previous prepared
capacity observation; legacy projections remain stale until prepared again. Status
reads never repair the cache synchronously.

Capacity uses SQLite's actual 10 GiB per-object hard allowance. Table row/density
estimates from rowid bounds and first-100-row logical-byte samples run at most
hourly per instance, with the attempt recorded durably before scanning. No
full-table `COUNT(*)` runs in capacity sampling; sparse rowids and sampling bias are
explicitly estimates. Estimates exclude free pages, index overhead and Cloudflare's
internal metadata; they do not identify the incident's cause. Physical file bytes
need not shrink after deletion. Reusable space is unavailable in this runtime: use
the existing write probe to prove headroom. Forecasts use a baseline at least one
hour old and require three consecutive growing breaching observations before
escalation; flat/shrinking samples reset confirmation. Capacity ratio alarms remain
immediate.

Warning means >=70% of the allowance or projected exhaustion within seven days.
Critical means >=85%, exhaustion within 24 hours, storage-full application failure
lasting a minute, a held/day-old parked backlog, or a failed write/read probe of
parked-ingress R2. The prepared backlog inspects at most two R2 pages per prefix:
`truncated: true` means a lower bound, never an exact total. Queue/DLQ depth, bytes and oldest timestamp are sampled once per cron pass from
`EVENTS.metrics()` and `EVENTS_DLQ.metrics()` and saved in `control/queue-metrics.json`.
These are approximate unacknowledged-message observations, not reads/consumption
of queue contents. Missing bindings or failed metrics preserve the last good
observation with an error/stale flag; never replace unknown depth with zero.
Both deployed environments bind their existing DLQ solely for metrics. Replay
ownership remains unchanged. See [Cloudflare queue metrics](https://developers.cloudflare.com/queues/observability/metrics/). Compute is
unavailable and connection saturation is not applicable to this SQLite runtime.

Request p50/p95 are fixed bucket upper bounds using the Workers I/O clock;
purely synchronous CPU time is not measurable by that clock. Use the configured
Workers invocation-duration/CPU observability when assessing cached-read CPU
latency. The response labels this measurement explicitly. Health serves hourly repair
aggregates; detailed minute rows are only archived in telemetry receipts. Dimension
caps evict the oldest data and report dropped sample/request counts.
Counters/latency distributions are bounded to sixty minute intervals per instance.
Installation maintenance prepares the retained accounting hour across restarts from
a bounded 10,000-row suffix; `window_complete: false` means a lower bound. Before
the first preparation, provisional in-memory counters explicitly use
`installation_instance_since_restart`. Health never scans raw accounting. Seven-day
R2 interval receipts retain repository diagnostics across restarts. Optional
telemetry persistence does not determine repository transaction success.

`github_state_capacity_alarm` emits structured severity, reasons, deployment and
operator action into the already configured Workers observability stream. The
service operator owns routing/escalation in that integration. `emitted` records
stream emission only; `notified` is always false. This code cannot confirm delivery
to a person and configures no new messaging channel. Emission or R2 persistence
failure retains in-memory health diagnostics. If the stream fails, use authenticated
health and existing operator observability tooling.

## Operator commands and rollout

First run the local drill below and deploy additive monitoring with defaults. Use
the recovery tool's existing authenticated test/production configuration; commands
do not print credentials or call GitHub. Each policy update replaces the complete
override set. R2 prefix overrides apply conservatively across all configured
repositories sharing the bucket.

```sh
cd /path/to/TauCetiWorker
S=services/github-state/scripts/storage_recovery.py
uv run python "$S" --environment test retention
uv run python "$S" --environment test retention --mode dry_run
uv run python "$S" --environment test receipt --probe-bytes 65536
```

Expected: policy `dry_run`, bounded class counts/positions and eligible counts
after cron scans the relevant range; inspection itself does not run cleanup; the
receipt's probe reports `writable: true`. Record the deployment and queue/DLQ
depth. Compare complete scan epochs, rather than expecting one pass to
cover a large store. Then enable bounded retention on test before production:

```sh
uv run python "$S" --environment test retention --mode enforce
uv run python "$S" --environment test retention --mode enforce --disable expired_changes --pin expired_deliveries --pin daily_archives
uv run python "$S" --environment test retention --mode dry_run --disable expired_changes
```

Expected: only the named cleanup class is disabled/pinned; stored reads and input
application continue. To stop
every SQL/R2 expiry class while investigating, use:

```sh
uv run python "$S" --environment test retention --mode dry_run \
  --disable expired_legacy_cursors --disable expired_repair_metrics \
  --disable expired_changes --disable expired_retired_objects \
  --disable expired_deliveries --disable expired_demand \
  --disable r2_cursors --disable raw_webhooks --disable daily_archives \
  --disable staging_pages --disable telemetry_intervals \
  --disable staging_spills --disable legacy_staging_spills
```

Expected: all twelve SQL/R2 expiry classes disabled.
This does not stop installation accounting's independent seven-day telemetry
expiry. Retain migration and parked input regardless of overrides. Inspect health's stale flags and failed class checkpoints;
repeated checkpoint failure is not proof of cleanup progress.

For warning: verify real bytes and growth, inspect hourly estimates and cleanup lag,
check prepared observation age, and expedite [Worker #316](https://github.com/TauCetiProject/TauCetiWorker/issues/316)
if reusable headroom is inadequate. For critical: preserve cached reads, confirm
parked durability, disable a faulty class, prove writable headroom and replay using
[the Worker recovery
runbook](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/github-state-recovery.md).
Stop if a reference cannot be accounted for,
parking cannot be proven durable, or cleanup stops advancing. Never clear coverage,
delete PR histories, bootstrap from GitHub, or inject production faults to complete
a drill. Code rollback retains archives/pins and must obey S0's cursor and
parked-ingress compatibility requirements.

## Repeatable drill

From the service directory:

```sh
npm ci
npm run drill -- /tmp/github-state-drill.json
```

Expected: zero exit status and `failed: 0`. The JSON records the committed
implementation revision, source hash/dirty flag, transcript hash, actual passing
test names and the explicit PostgreSQL dependency; the adjacent `.tap` contains
every assertion. The runner includes every service test file, including
scheduler/reference-retention tests. Generate evidence after committing source, then
commit the evidence and transcript separately; `implementation_revision`
intentionally identifies the tested source commit. Fixtures exercise storage-full
reads/cold starts, explicit unavailable coverage, durable acknowledgement,
idempotent replay, retry budgets, request IDs, malformed upstream/service responses,
R2 upload/delete/checkpoint/monitoring failures, quota exhaustion, no additional
GitHub calls during stored-state recovery/monitoring, retained spill reads, fifty
simulated retention days and 100,000 repair events.

## PostgreSQL sampling, alarms and restore drills

The existing minute cron starts background monitoring before attempting repository
SQL. Even an unavailable database or a receipt-archive error cannot prevent the
next monitoring/recovery attempt. `/health` and `/storage/summary` serve prepared
PostgreSQL observations from memory/R2 and make **zero database or GitHub calls**.
Cold health waits at most 100 ms for R2 and marks unknown schema/connectivity and
stale projections explicitly. A committed in-memory revision remains available;
a fresh coordinator without a prepared revision reports null rather than inventing
one. Live repository reads keep their original complete/observed/unavailable
semantics; an outage never becomes an empty complete collection. Warm DTOs retain
their original timestamps; a cold unavailable database returns a precise parkable
error/request ID. Reads do not recreate the database or query GitHub.

Install the additive schema/function before deploying monitoring on a selected
PostgreSQL test backend. From `services/github-state`, with isolated credentials
already supplied privately (never enable shell tracing):

```sh
export MIGRATION_DATABASE_URL="$TEST_POSTGRES_MIGRATION_URL"
export APPLICATION_ROLE=github_state_test_app
node scripts/migrate-postgres.mjs "$REPOSITORY_ID" "$GENERATION"
# Use the effective allowance verified for this dedicated project, not a plan guess.
export STORAGE_LIMIT_BYTES=1099511627776
node scripts/prepare-postgres-statistics.mjs "$REPOSITORY_ID"
```

Expected: matching repository/generation with `schema_version: 1`, then
`prepared: true`. An existing ownership mismatch stops initialization. Deployment
alone performs no DDL. `prepare_monitoring(expected_generation, analyze_tables)`
is a fixed-schema, security-definer function with a fixed search path, time/lock
limits and no dynamic SQL. The application can call it and read prepared statistics;
it cannot set allowance/restore receipts, create schemas or change backend ownership.
The function checks generation and serializes samplers with a try advisory lock.
No runtime migration credential is added.

Every minute, it saves current database bytes, connection usage/limit/reserves,
role-limited active/waiting sessions and per-table live/dead row estimates from
PostgreSQL statistics. CPU utilization is explicitly unavailable from these SQL
views; use the dedicated project's existing Neon compute monitoring. Active-query
and lock-wait observations are separate compute-pressure diagnostics, not a claim
to measure CPU. [PostgreSQL statistics visibility](https://www.postgresql.org/docs/18/monitoring-stats.html)
limits session detail for roles without `pg_read_all_stats`; the response labels
that limitation. This release grants no global statistics role.

Physical table/index/TOAST sizes and their growth are prepared at most hourly.
R2 records the expensive attempt **before** SQL so a failed/cancelled transaction
cannot cause minute-by-minute expensive retries; SQL also throttles persisted
attempts. A missing attempt record permits a pass; unreadable/invalid attempt
state skips physical analysis while minute gauges continue. Failed measurements
retain prior values and timestamps. Dead tuples are estimates, not measured
reusable bytes; neither deletion nor VACUUM guarantees a smaller physical file.

PostgreSQL capacity is growable and uses only an operator-verified effective
project/account allowance. Missing allowance is unknown, never SQLite's 10 GiB.
`pg_database_size(current_database())` measures this database, **a lower bound on
project usage**; sibling databases/branches, retained history and account quotas
require Neon's project/account observations. `usage_scope` and `allowance_scope`
expose that distinction, and `project_usage_bytes` stays null when not measured.
Operators must continue watching actual project/account usage in Neon. The 70%/85%
byte warnings and seven-day/24-hour confirmed forecasts apply to the measured
lower bound; they cannot prove total account headroom. Connection usage emits a
separate warning/critical at 70%/85% of nonreserved slots. Capacity, connection
pressure, a persistent unapplied-input failure and loss of parked durability feed
structured alarms in the configured Workers observability stream. Alarm delivery
failure retains health and never claims human notification. Stale capacity or
recovery observations stay stale even if the wrapper response is recent.

Run the complete SQLite and real PostgreSQL drills separately:

```sh
# No PostgreSQL test environment variables for the SQLite run.
npm run drill -- /tmp/sqlite-prevention.json
# Isolated TEST_POSTGRES_URL and TEST_POSTGRES_MIGRATION_URL supplied privately.
npm run drill -- --postgres /tmp/postgres-prevention.json
```

Expected: `exit_status: 0`, `failed: 0`, correct backend/environment and explicit
skip counts. PostgreSQL mode refuses missing/non-test credentials and runs the
shared service/scheduler/survey/snapshot/activity contracts plus prevention tests.
Fixtures use disposable schemas/databases on the test branch. They cover slow SQL,
self-terminated test connections, an unavailable dependency, coordinator restart,
lost commit response, cached versus missing coverage, acknowledged parked input,
existing held input, idempotent ordinary scheduled replay, bounded retention
plateaus, archive/checkpoint/monitoring failure and recovery. No admin replay
command, duplicate quota owner or direct GitHub fanout is used. The source hash
includes SQL migrations. Generated JSON and complete TAP record passing and
skipped tests separately; a skipped PostgreSQL test is never counted as passed.

Validate a fresh Neon point-in-time restore with a private API-token file:

```sh
node scripts/restore-postgres-drill.mjs "$NEON_TOKEN_FILE" /tmp/neon-restore.json
```

Expected: `verified: true`, `temporary_branch_deleted: true`, exact large ID
preserved and checksums for all 23 tables. The script checks the dedicated project's
recorded **test** branch/endpoint, creates a random disposable repository schema,
records its database timestamp and table hashes, changes the source sentinel, then
creates a temporary point-in-time child of that test branch. It verifies the old
sentinel and every table before deleting the child and source fixture. It then closes its own connection, restarts only that just-created temporary
endpoint and reconnects to recheck every table. It never restarts shared test or
production compute or selects a production endpoint. If an API mutation has an ambiguous
failure, inspect existing `issue317-restore-*` branches before retrying; do not
blindly repeat creates. If cleanup fails, retain the receipt's branch ID and stop
until the temporary branch is removed. The actual isolated restore receipt lives
in `services/github-state/validation/retention-restore-2026-10-20.json`.

The separate migration-archive fixture verifies digests before committing a
transactional restore, rejects corruption without publishing partial state, then
retries successfully. It compares every repository table, tombstones/memberships,
exact timestamps, retained cursor/spill references and landing evidence. This
validates the stored-state archive contract; production S2 import/cutover acceptance
remains owned by #316 and requires its actual migration receipt.

For a PostgreSQL outage, restore the existing test dependency normally, leave
parked/held inputs and receipt objects intact, and let minute cron run. Expected:
`recovery.last_attempt` advances, `next_retry` is prepared, backlog decreases and
`latest_applied_input` advances without an admin replay request. Stop if stored
coverage disappears, acknowledged input lacks a receipt or durable pointer/body,
an archive digest differs, generation is fenced unexpectedly, or retry/backlog
progress stops. Never clear application receipts or switch backends as an outage
workaround. Rollback retains archives/pins and uses #316's write-reconciliation
contract rather than a routing flip. Production fault testing requires the isolated
drill evidence first; this release performs no production deployment/fault.
