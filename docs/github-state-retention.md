# GitHub state retention and outage drills

This is the SQLite/R2 prevention release for [Worker
#317](https://github.com/TauCetiProject/TauCetiWorker/issues/317). It keeps S0
recovery available and adds no backend switch, migration, GitHub quota spender, or
production fault injection. PostgreSQL implementation and Neon restore testing must
follow the repository-store interface and isolated test infrastructure in [Worker
#308](https://github.com/TauCetiProject/TauCetiWorker/issues/308). Production
migration remains [Worker
#316](https://github.com/TauCetiProject/TauCetiWorker/issues/316)'s release.

## Ownership and storage policy

The shared-state service operator owns capacity alarms, retention overrides, archive
receipts and replay. The existing installation scheduler exclusively owns GitHub
quotas. The PostgreSQL backend operator owns the dedicated Neon project's effective
account/project allowance, compute, connections and restore history after S1.
Automatically growing storage is not a hard allowance; do not reuse SQLite's 10 GiB
threshold for PostgreSQL.

| Data | Default window / retirement condition |
| --- | --- |
| Legacy SQL cursors | Ten minutes; delete only after their stored expiry |
| R2 cursors | Ten-minute validity plus the existing one-minute deletion margin |
| Change journal | Seven days; clients beyond retained revisions receive reset |
| Delivery receipts | Fourteen days **from application**; automatically pin the entire class while parked/held input is observed, or its inventory is unavailable |
| Legacy raw repair telemetry | Seven days; new repairs and query counts use bounded counters and latency buckets instead of SQL samples |
| Installation accounting | Seven days; independent bounded maintenance also runs while repository ingestion/auditing is paused |
| Aggregated diagnostic intervals | Seven days in R2; no payloads, credentials or event bodies |
| Raw webhooks | Fourteen days; a parked body is a separate digest-verified, non-expiring copy |
| Daily observational archives | Seven days, per-page expiry; they are not a transactional point-in-time backup |
| Staging pages | One day; an expired traversal restarts through the existing quota owner |
| Demand | One day (its scheduling significance lasts five minutes); new automatic expiry starts in dry run |
| PR headers, histories, coverage, tombstones, landing evidence | Retained; quiet or closed does not authorize deletion |
| Workflow attempts and obsolete heads | Retain resources, attempt metadata and memberships. Retire unneeded polling jobs only when not leased, recently demanded or important/pending; preserve current attempt isolation |
| Survey projections/inventory | Owned by their source PR/history. Retain source and coverage; rebuild through existing invalidation, never GitHub refetch for retention |
| Bulk CI payloads | Opt-in verified R2 archival of old inline workflow-job/check/status bodies; keep indexed SQL resource/membership and workflow attempt guard fields. Workflow-run headers stay inline |
| Resource spills | Retain indefinitely until a complete reference index and explicit archive-release protocol can prove retirement safe |
| Recovery/migration exports and parked/held bodies/pointers | Pinned; normal expiry never deletes them, even with accidental expiry metadata |

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
Each class examines at most 1,000 rows (50 for legacy cursors); indexed repair
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
subject to pins/explicit class overrides. No SQL schema migration is needed. Background retention and archival participate
in the source-migration drain barrier and stop at its fence. Bucket expiry
rechecks the migration hold before batch deletion; migration inventories include
the staging-spill prefix.

CI archival is separately opt-in, with a maximum 100 resource candidates, 4 MiB and
50 ms between operations. The R2 bytes are uploaded with a digest and read back for
verification before a conditional SQL pointer update; concurrent resource changes
win. Reads use the existing transparent spill decoder. No semantic revision,
coverage, tombstone or membership is removed. Failed uploads leave inline data
intact and are retried on the next pass. Resource rows/spill references are not
retired by archival.

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

CI archival's candidate count is an upper bound, not a throughput promise. Normal
R2 latency can exhaust its cooperative I/O budget after one archived body; track
progress and repeat passes. Protected orphan uploads from lost publication races
remain owned by the future reference-graph collector.

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
`truncated: true` means a lower bound, never an exact total. Queue/DLQ depth is
explicitly unavailable until a management-metrics source is configured; the recovery
CLI's full bounded inventory and operator queue metrics remain necessary. Compute is
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
uv run python "$S" --environment test retention --mode dry_run --archive-ci
uv run python "$S" --environment test export --table resources --table deliveries --table demand
uv run python "$S" --environment test receipt --probe-bytes 65536
```

Expected: policy `dry_run`, bounded class counts/positions, `ci.enforce: false` and
eligible counts after cron scans the relevant range; inspection itself does not run
cleanup or archival; the export reports `verified: true`, and the receipt's probe
reports `writable: true`. Record the archive ID, restore/drill evidence, deployment
and queue/DLQ depth. Compare complete scan epochs, rather than expecting one pass to
cover a large store. Then enable bounded retention on test before production:

```sh
uv run python "$S" --environment test retention --mode enforce --archive-ci
uv run python "$S" --environment test retention --mode enforce --disable expired_changes --pin expired_deliveries --pin daily_archives
uv run python "$S" --environment test retention --mode dry_run --disable expired_changes
```

Expected: only the named cleanup class is disabled/pinned; stored reads and input
application continue. Omitting `--archive-ci` disables future CI archival. To stop
every SQL/R2 expiry class while investigating, supply `--disable` for all names in
`RULES` and `R2_CLASSES` in `src/retention.ts`. Retain migration and parked input
regardless of overrides. Inspect health's stale flags and failed class checkpoints;
repeated checkpoint failure is not proof of cleanup progress.

For warning: verify real bytes and growth, inspect hourly estimates and cleanup lag,
check prepared observation age, obtain a verified archive/restore receipt, and
expedite [Worker #316](https://github.com/TauCetiProject/TauCetiWorker/issues/316)
if reusable headroom is inadequate. For critical: preserve cached reads, confirm
parked durability, disable a faulty class, prove writable headroom and replay using
[the Worker recovery
runbook](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/github-state-recovery.md).
Stop if an archive digest/restore check fails, a reference cannot be accounted for,
parking cannot be proven durable, or cleanup stops advancing. Never clear coverage,
delete PR histories, bootstrap from GitHub, or inject production faults to complete
a drill. Code rollback retains archives/pins and must obey S0's cursor and
parked-ingress compatibility requirements.

## Repeatable drill and restore validation

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
simulated retention days and 100,000 repair events. Restore validation verifies
every export page digest and reconstructs an independent SQLite store, checking
exact IDs, tombstones, membership, coverage and spills.

The PostgreSQL drill is **not run or claimed** by this release. After [Worker
#308](https://github.com/TauCetiProject/TauCetiWorker/issues/308), use only its
isolated test database/branch and dedicated Neon project: verify the effective
allowance and seven-day restore history, capture the migration archive and
checksums, exercise unavailable/slow/restarting connections, restore into a separate
test branch, compare semantic state and parked replay receipts, and record p50/p95
and connection/compute observations. Do not proceed to production fault testing
without that test evidence. Actual Neon restore and migration archive format
validation depend on S1/S2; a local SQLite archive restore is not evidence of a Neon
restore.
