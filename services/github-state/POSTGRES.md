# PostgreSQL repository storage

Production remains on SQLite. Backend selection and production data cutover are
owned by [#316](https://github.com/TauCetiProject/TauCetiWorker/issues/316), after
both the recovery release and this backend have been deployed and tested. No
client change, GitHub bootstrap, schema migration or PostgreSQL query occurs
merely because this code is deployed with the existing configuration.

## Ownership and transactions

`RepositoryState` remains the repository's coordinator. Its asynchronous
`RepositoryStore` uses parameterized SQL for all runtime repository tables and
metadata. SQLite retains its existing schema and recovery endpoints.
PostgreSQL has a schema per numeric repository ID, `github_state_<id>`; each
schema includes the repository tables, a backend/generation control row and
operator-prepared capacity statistics. PostgreSQL schema version 1 represents
the current SQLite version 5 repository state, including survey projections, head
indices, activity evidence, jobs and delivery receipts. PostgreSQL additionally
accepts legacy SQL cursors for migration compatibility; SQLite follows the upstream
R2-only cursor implementation. Identity `rowid` columns support bounded maintenance
and source migration; they are absent from client DTOs.
IDs and JSON remain text. PostgreSQL orders decimal IDs as `numeric`, avoiding
both JavaScript precision loss and PostgreSQL `bigint` overflow. Counter and
Unix timestamp columns use checked safe integers; landing timestamps retain
microseconds, while observation, verification, leases and expiry use milliseconds.

SQLite serializes its adapter operations and gates transaction callbacks against
other Worker events, including native recovery/status reads. Transaction failures
are caught inside the gate so an ordinary rollback does not reset the object.
Both backends serialize writers before opening a transaction. PostgreSQL readers
continue to see committed rows while a writer waits or publishes.

The Neon driver uses HTTP for standalone queries and noninteractive batches.
Interactive transactions create a `Client`, lock and validate the repository's
backend/generation row, and close the client in `finally`. Each mutation scope
has its own store and revision state. GitHub HTTP requests, webhook body reads, large resource uploads and reads of
checkpointed staging pages finish before the publication transaction starts.
Staging pages upload to immutable UUID-suffixed R2 keys before writer ownership
is acquired. The lease-checked transaction publishes a versioned `staging_pages:`
metadata map with its job checkpoint. Late uploads from expired leases remain
unreferenced and cannot replace a newer page. Final publication retires the map;
legacy deterministic page keys remain readable. Source export verifies both
address formats, so S2 can preserve partial jobs without changing client cursors. Only the existing installation Durable Object
reserves quota, sends GitHub requests and maintains real cooldowns.

Resource changes, memberships, projections, repair jobs, change revisions and
the final delivery receipt commit together. R2 writes finish before their
references commit. Original input receipt times are stored as the internal resource JSON field
`__tauceti_received_at` (removed on decode), rather than a permanent metadata row
for every resource. They fence reordered webhooks;
observation times also guard slow upstream responses. Lease replacement aborts
the entire publication transaction. Claims and retirement check the selected
job's generation and checkpoint. A new generation survives an older lease's
completion, including its requested timestamp.

Full resource/member key strings use hash exclusion constraints with exact
equality rechecks, so long file paths do not hit a B-tree tuple-size limit or
collapse on a hash collision. Text columns use the `C` collation to preserve
SQLite binary ordering. Resource insert/update selection is protected by the
writer lock; it does not use a B-tree conflict arbiter.

Revision allocation is serialized by the repository lock. Independent publication writes and changes are coalesced into parameterized
set operations, flushed before uncached reads and before commit. Repeated row
identities force a flush to preserve statement order. The installation scheduler
remains single-owner; repository alarm work is single-flight, and scheduler
selection, retirement and claiming share one transaction. Inventory
fan-out uses multirow job/metric inserts. Compact surveys prefetch related
metadata/status rows in bounded sets, histories read bounded SQL pages, and
activity reads batch PR, landing and LOC metadata. Read caches belong to a
single query or publication transaction. Repair and query telemetry uses bounded
in-memory buckets with R2 interval archives, following the existing retention
service; legacy SQL telemetry remains available for controlled expiry/export.
Retention pins apply on both backends. SQL pin acknowledgments wait for in-flight
SQL sweeps and receipt archival. R2 cron retains its existing per-batch policy
checks; a previously authorized R2 deletion batch may finish after acknowledgment. Expired SQL delivery receipts are
archived durably to `receipts/<repository>/` in R2 before batched conditional SQL
deletion. Event application rechecks that evidence under writer ownership; the
existing parked-input and acknowledgement contract remains authoritative.
PostgreSQL sweeps bound examined rows and bytes, batch conditional deletes and
use backend/generation-specific checkpoints in the existing R2 control record.
SQLite physical cleanup and recovery tools remain SQLite-specific.

Warm DTO snapshots use the existing
bounded cache. A stored query is retried if the committed revision changes while
it reads; ongoing churn returns explicit unavailable coverage. Complete stored
reads renew optional demand/cache writes using the request's lifetime
extension instead of waiting for a writer lock. Optional work is bounded to two
concurrent tasks per repository, and database lock acquisition is time bounded. Missing coverage still records
its repair intent before returning unavailable.

New R2 cursors are read directly. PostgreSQL also provides SQL fallback for imported
older cursors, including UUID-shaped legacy IDs. Query, repository, coverage and expiry checks remain in
place. Database outages and fenced writers return sanitized, parkable failures;
queue acknowledgement still requires an applied receipt or durable parked input.

## Provisioning and credentials

The dated [provisioning receipt](validation/neon-provisioning-2026-10-20.json)
records the independent `tauceti-github-state` project in TauCetiProject:
PostgreSQL 18, `aws-us-east-2`, always-on 0.5–2 CU, seven-day history, and
1 TiB of effective growing storage. The API's project quota is 1,099,511,627,776
bytes, its account branch allowance is 16 TiB, and PostgreSQL reports
`neon.max_cluster_size = 1TB`. This verifies that a 10 GB quota was not retained.
See [Neon's quota semantics](https://api-docs.neon.tech/reference/createproject)
and [driver connection semantics](https://neon.com/docs/serverless/serverless-driver).

Production uses database `github_state` with separate `github_state_app` and
`github_state_migration` roles. The test branch was created with schema only,
uses database `github_state_test`, and has independent `github_state_test_app`
and `github_state_test_migration` credentials. Both test credentials were
rejected by the production endpoint. The application roles have no superuser,
role creation or database creation permissions. Grants allow repository DML,
sequence use, control-row locking and read-only capacity metadata, with no schema
creation or generation/capacity changes. Bors remains in its own project, with
its compute and migrations untouched.

Credentials are held in operator secrets and server/CI secret stores, never in
this repository or client configuration. CI receives only the two isolated test
URLs as `STATE_TEST_POSTGRES_URL` and `STATE_TEST_POSTGRES_MIGRATION_URL`; it has
no Neon API key or production credential. The PostgreSQL CI job runs for trusted
repository branches. Fork pull requests still run the SQLite suite.

## Explicit test setup

Supply migration credentials through the operator secret store. Initialize only
the intended database and repository, using a separate application role:

```sh
# MIGRATION_DATABASE_URL and APPLICATION_ROLE are already supplied securely.
node scripts/migrate-postgres.mjs "$REPOSITORY_ID" "$GENERATION"
# STORAGE_LIMIT_BYTES must come from verified effective account/project limits.
node scripts/prepare-postgres-statistics.mjs "$REPOSITORY_ID" "$GENERATION"
```

The migration CLI does not change existing backend ownership. Capacity preparation
samples database size once; protected health reads the saved value and timestamp,
backend/schema identity, connection failures and latest applied input. The existing cron refreshes capacity/connection gauges every minute after the
additive monitoring migration is installed; physical table/index analysis runs at
most hourly. Status serves prepared observations without database calls. See
[retention and outage drills](../../docs/github-state-retention.md#postgresql-sampling-alarms-and-restore-drills)
for privilege boundaries, quota-scope limitations, queue metrics and exact test commands.

The Worker defaults to SQLite. Selecting PostgreSQL requires all three server
bindings: `REPOSITORY_BACKEND=postgres`, `REPOSITORY_GENERATION`, and the secret
`REPOSITORY_DATABASE_URL`. To select it for the existing test environment, put
that environment's application URL in its server secret, then pass the two
nonsecret vars only to `--env test`:

```sh
npx wrangler secret put REPOSITORY_DATABASE_URL --env test
npx wrangler deploy --env test \
  --var REPOSITORY_BACKEND:postgres --var "REPOSITORY_GENERATION:$GENERATION"
```

These commands are explicit operator actions. They do not select production.
An empty provisioned PostgreSQL schema returns `repository_unconfigured` on reads
and ticks; it does not bootstrap GitHub. Populate it through controlled test data
or the separately authorized bootstrap endpoint. PostgreSQL uses the existing once-per-minute cron ticks to drive the same fenced
scheduler and does not write alarm metadata into the obsolete SQLite file.
The installation Durable Object remains the sole quota owner.

## Verification

`npm test` runs the existing SQLite suites. With isolated `TEST_POSTGRES_URL` and
`TEST_POSTGRES_MIGRATION_URL` supplied securely, `npm run test:postgres` runs the
service, survey, snapshot, scheduler and activity contracts on real PostgreSQL.
Worker contracts use disposable databases with the actual Neon HTTP driver and
request-scoped WebSockets; only GitHub responses are faked. Fixtures create and
drop their own schemas/databases and never connect to production.

Additional tests interrupt transactions and publication, fail resource uploads,
reorder/duplicate deliveries, overlap a webhook with slow upstream and stored
reads, replace job generations/leases, exercise outage/reconnect, reject stale
writers and verify least-privileged credentials, exact large IDs, timestamps,
change feeds, 304s and legacy cursors. SQLite ownership tests also exercise the
persistent fence with raw SQL, modelling older deployments.

The test database restore exercise recovered a `before` sentinel on a temporary
point-in-time branch after the source had changed to `after`. The verification
time and restore point are in the provisioning receipt; the temporary branch was
removed after verification. No production data was involved.

Run the stored-read performance gate with the pre-change baseline commit and the
same isolated test credentials:

```sh
BASELINE_REF=<pre-change-TauCetiTools-commit> npm run benchmark:postgres
```

For the original Worker baseline, set `BASELINE_REPOSITORY` to a TauCetiWorker
checkout and `BASELINE_REF=5d5f4b870`. The service now lives in TauCetiTools; its
source migration/export protocol and frozen-mirror recovery behavior remain in
place. The [migration runbook](../../docs/github-state-migration.md) defines the supervised target import and production acceptance gates.

The harness seeds 1,200 PRs and 5,000 comments, checks equivalent response data,
warms both implementations, and measures five fixed trials of 1,200 samples per workload/backend
in separate V8 workers, synchronized per workload, with eight concurrent reads
per backend. The gate uses the combined 6,000 samples, and the receipt also
retains each trial's p50/p95. It measures surveys, first history
pages and R2 continuations, forbids GitHub access, records p50/p95 and cold timings,
and exits unsuccessfully if any warm p95 regression exceeds 20%. The relative warm gate measures the public API with its existing DTO cache
warm on both backends. It is not a claim that remote SQL takes less than one
network round trip. Separate uncached diagnostics bypass that cache and execute
16 samples/workload with eight concurrent reads and report failures separately.
These diagnostic timings do not use the warm relative gate. Cold timings are also recorded. The local harness uses separate Node
workers and node:sqlite; it does not establish deployed Worker latency. A
representative Worker/region comparison is required before production cutover.
The [recorded local run](validation/stored-reads-2026-10-20.json), against
TauCetiTools baseline `26da8f5`, passed the warm gate: PostgreSQL/SQLite p95 ratios
were 0.832 for surveys, 0.827 for first history pages, and 0.917 for continuations.
The uncached first-history diagnostic measured 3.22 seconds p95 at eight concurrent
reads; byte-bounded SQL pages keep transfers at 2 MiB per batch (with one oversized
row allowed for progress). This remains a production sizing and latency concern
for #316, rather than evidence that uncached PostgreSQL matches local SQLite.
The [publication fixture](validation/publication-2026-10-20.json) records
1,200 changed PRs with 234 SQL statements in about 3.6 seconds; a regression test
also bounds unchanged publication statements and overlaps writers for longer
than the database lock timeout. Timings are diagnostic, not a deployed SLA.
Repeat the read and contention gates on representative stored production data
before #316 cutover.

## Cutover fence and rollback

No production fence has been set by this change. The #316 procedure must first
park ingress using the existing R2 control contract, drain active applications,
record the recovery/export point and initialize/copy/verify the target store.
The test-only admin operation `POST /admin/storage/ownership` takes `repository`,
`expected_generation`, `backend`, and `generation`. It requires parked ingress,
blocks new mutation scopes and waits for active scopes. It requires the server
setting `MIGRATION_ENVIRONMENT=test`; #316 must integrate it into the production
source/target acceptance protocol before production use. It atomically installs
SQLite triggers and the new ownership metadata. Those triggers reject writes
from deployments predating the adapter as well. The operation does not bypass an active source-export barrier. Source SQLite must have enough
recovered headroom to commit this barrier; a failed barrier is not a cutover.

PostgreSQL writers independently require the matching generation and schema
version under a control-row lock. Change PostgreSQL ownership only with migration
credentials, after draining the previous writer. Fences are durable state, not a
routing flag. Keep R2 parked inputs, legacy cursor support and delivery evidence
through the transition. Retain the existing installation quota owner and cooldowns.

Before production selection, code rollback leaves SQLite authoritative. After
selection, rollback requires preserving/reconciling PostgreSQL writes and parked
inputs with an explicitly chosen authoritative store. Switching a variable or
routing back to the old SQLite object is not a lossless rollback. Never reset an
authoritative repository or bootstrap GitHub to recover database capacity.
