# GitHub state migration

[#316](https://github.com/TauCetiProject/TauCetiWorker/issues/316) owns the production
migration and receipts. S0 ([#303](https://github.com/TauCetiProject/TauCetiWorker/issues/303))
is stored-read compatibility and durable parked ingress. S1
([#308](https://github.com/TauCetiProject/TauCetiWorker/issues/308)) is the dedicated
Neon PostgreSQL backend. Both must be deployed and accepted before production
start. Merging code or closing an issue does not prove deployed acceptance.

## Preparation and commands

The admin command defaults to verification. Environment selection is mandatory.
Credentials come from `~/.config/tauceti-github-state[-test]/admin-token` and never
appear in output. `--service-url` accepts an HTTPS origin without credentials,
query or fragment; `--credentials-dir` selects another private credential directory.

```sh
python3 services/github-state/scripts/migrate.py --environment production
python3 services/github-state/scripts/migrate.py --environment test status
python3 services/github-state/scripts/migrate.py --environment test \
  --generation c707ddba-076d-4a63-8696-96533031ce88 \
  --target neon-test-branch-database --apply start
```

`start` authorizes the durable supervised operation. Ordinary minute cron advances
it after the command exits or its process dies. `resume` optionally accelerates
bounded steps; recovery does not require it. All operations reuse the same UUID v4
and target. Never reuse a completed/abandoned generation. Legacy `freeze` is the
source-only **test** protocol and stops at `source_verified`.

Before enabling `MIGRATION_ENVIRONMENT=test|production`, deploy the coordinator,
queue consumers, installation quota owner and retention guards everywhere. Fence
old deployments and drain pre-existing GitHub dispatches and R2 deletion batches.
Keep the normal backend SQLite; do not select PostgreSQL with a global canary flag.
Verify the normal queue and DLQ each have this deployment as their consumer. The
DLQ consumer uses batches of ten and one concurrent invocation; it applies or
parks pointers with the same receipt-before-acknowledgement contract. Fresh queue
metrics are required before releasing migration replay and its receipt.
Pin **every** R2 retention class in `control/<repo>/retention-policy.json` and ensure
no bucket lifecycle independently expires this data.

Prepare an empty per-repository target with the privileged migration URL in the
operator's environment (never pass a connection string as an argument):

```sh
node services/github-state/scripts/migrate-postgres.mjs REPOSITORY_ID GENERATION --standby
```

This requires `MIGRATION_DATABASE_URL` and `APPLICATION_ROLE`. Standby initialization
creates no inventory, scheduler jobs or bootstrap metadata. Its `backend_control`
owner is `migration`, so the application role cannot write. Existing ownership of
another generation or an active schema is rejected. Record verified capacity and
an actual recent restore drill in `capacity_statistics` using the migration role.
Storage must be at least 1 TiB; restore evidence must be less than seven days old.

Worker secrets `MIGRATION_DATABASE_URL` and `REPOSITORY_DATABASE_URL` must address
the migration and application roles of the **same** enrolled Neon endpoint/database.
`MIGRATION_TARGET` is JSON containing sanitized `identity`, `hostname`, `database`,
`application_role`, and `migration_role`. URLs are never returned in receipts.
The target checks exact expected tables/columns, generation/schema, database/role,
application grants, live PostgreSQL 18/storage settings, restore evidence and empty
data before freezing. Existing held input must be resolved before starting. Preflight authenticates the application credential separately
and proves that role cannot update backend ownership. Application and migration
roles/URLs must differ.

Install the private deployed acceptance receipt at
`control/<repo>/migration-acceptance.json`, after performing its checks. It contains:

- `version:1`, `environment`, `repository` (numeric string), `target`, `deployment`,
  `source_object`, and `valid_until` (Unix milliseconds).
- `checks` with true results for `s0_compatibility`, `parked_ingress`,
  `queue_dlq_ownership`, `r2_retention_drained`, `s1_schema_roles`, `capacity`,
  `restore`, `unchanged_clients`, `stored_shadow`, `no_github_refetch`, and
  `warm_read_p95`.
- `latency.baseline_p95_ms` and `latency.target_p95_ms` from comparable deployed,
  warm stored-read workloads. Target must be positive and at most 1.2 times baseline.
- `queries`: 1–20 valid stored API queries. Production requires a survey, an
  inventory and a history query, plus `activity_numbers` (1–20 positive integers).
  Include existing continuation cursors and `cursor_coverage` describing tested
  versions/expiry coverage. Non-cursor shadow results must be complete successes;
  two unavailable results do not validate a target.

These are release evidence from verified operations, not request flags. The
service binds the receipt hash to the generation and separately verifies target
SQL identity, schema, grants and capacity. Preserve test logs, baseline conditions,
restore/project/region/PG18/always-on compute evidence with the release artifacts.
Never place credentials or payloads in a public receipt.

## Durable phases and ownership

Control and checkpoints live in R2 `control/<repo>/migration.json`, outside frozen
SQL. Etag preconditions reject stale controllers. Sanitized status exposes phase,
generation, source/target identity, bookmark, revision, per-table counts and hashes,
progress, renewable lease, failure code, target checks and write owner.

1. `preflighting`: scan source cells in bounded keyset pages while ordinary input
   still applies. Reject incompatible REAL/BLOB/NUL cells and malformed manifests.
   Revalidate target readiness before entering the freeze. Every exported page
   rechecks compatibility to cover rows changed after this scan.
2. `freezing`: block all new repository mutations and GitHub repair/reference
   dispatch, park signed input durably, and drain in-flight mutations/retention and whole installation requests, including
   references between individual upstream calls. Require the quota owner to report
   the same deployment version.
   Install persistent triggers and a generation owner in SQLite. This fences
   deployments predating the asynchronous store adapter. Sync, take an authoritative
   recovery bookmark, discover and validate every source table.
3. `exporting` / `verifying`: export bounded rowid keyset pages and compare them
   against the frozen source, including schema/revision/count/hash and R2 references.
4. `source_verified` / `importing`: import each page and its ledger entry in one
   PostgreSQL transaction. Response loss reuses the ledger, never inserts twice.
5. `target_verifying`: compare every target page, exact ID/time/text value, total
   count and canonical archive hash. Check explicit referenced blobs again.
6. `preparing`: reset only recorded expired partial staging checkpoints, including their API/ETag
   state, preserving cooldowns, attempts, priority and generations. All other partial jobs remain intact.
7. `switching`: compare representative stored queries, activity/landing results
   and prepared coverage through both adapters without GitHub requests. Retain proof, expire obsolete source-worker
   leases, prepare rowid sequences, and atomically activate PostgreSQL's generation
   under its ownership lock. Save activation intent before that transaction. Commit
   a local PostgreSQL ownership marker in SQLite before enabling target input.
   SQLite was already durably fenced. A gap with no writer
   is safe; there is never a canary with two writable stores.
8. `replaying`: select PostgreSQL in the coordinator and resume the existing bounded
   ingress/reconciliation owners. Permanent delivery receipts commit before parked
   body/pointer cleanup; duplicates cannot publish another semantic change.
9. `complete`: record release evidence after parked/held pointers drain and fresh,
   post-selection queue and DLQ metrics both report zero backlog. The same deployed
   durable parking/receipt handler owns both consumers. A completed held traversal
   after selection permits bounded reconciliation to resume even
   if a poison input remains deferred by its preserved retry deadline; such inputs are logged and still block
   the final completion receipt. Historical bucket recovery
   continues independently; its checkpoint/gaps remain explicit in the receipt.

The freeze lease is 15 minutes and renews only after successful progress. A crash
within that interval resumes automatically. Expiry or a source deployment change
aborts a pre-switch generation; retained archives cannot silently resume as that
generation. This bounds unattended freezes during persistent faults. Start a new
operator-authorized generation after resolving the cause.

Abort is persisted before fencing an inactive target and releasing a writable
source. If the inactive target is unreachable or removed **before activation
intent**, supervision quarantines it and releases SQLite: the durable abort
checkpoint prevents stale controllers from saving activation intent. Imported
partial data remains non-authoritative and cannot be reused as a new generation.
After activation intent, an unavailable/missing target requires forward recovery;
SQLite is never released without proving that activation did not commit. A
committed activation discovered after lease expiry always selects PostgreSQL.
A source too full to commit its release remains fenced with stored reads/parked
input, and ordinary supervision retries.

Selection also publishes a superseded PostgreSQL storage marker so queue owners
never treat the retained SQLite file as full after migration settings are retired.
Monitoring and retention use the selected repository generation.

The local `backend_owner` marker records PostgreSQL generation, target identity
and selection time before any target event can apply. Cold starts honour it even
when R2 is unavailable or migration bindings have been retired. They never serve
stale SQLite data as the current PostgreSQL revision. Before selection only, a
cold control outage can expose the retained source with an explicit
`X-TauCeti-Read-Fallback: last-known-source` response and
`X-TauCeti-Source-Frozen-At` timestamp; its SQL write fence remains installed.

## Archive and data preservation

Every SQL user table must match `REQUIRED_TABLES`; unknown/missing tables or
columns stop the operation. Historical column order is supported: imports and
verification use archived column names after validating the allowed manifest,
so positional differences cannot silently swap same-type fields. Empty tables are included. Archive pages preserve schema/version,
rowids, IDs, revisions, observed/verified timestamps, tombstones, collection coverage,
metadata, surveys, activity/landings, membership/history, deliveries, head indices,
jobs, demand and cooldowns. Daily observational snapshots and the changes journal
are not used as migration backups.

Pages under `migrations/<repo>/<generation>/<table>/<index>.json` have at most 250
rows and a 512 KB logical target, allowing one larger row for progress. Cells carry
their SQLite type and exact string/null value; signed 64-bit integers never cross
JavaScript as numbers. Text and JSON remain verbatim. The generic source format
also supports hex blobs and SQLite round-trip REAL strings; the current PostgreSQL
application schema explicitly rejects incompatible blobs/reals instead of coercing
them. SHA-256 folded over typed rows in rowid order is independent of page size.
Each page also has a content digest. Checkpoints have constant size per table.

R2 inventories include resources, cursors, staging and staging-spills. Implicit
partial-job references are checked in bounded HEAD groups. Previously expired
staging is recorded in a hashed, numerically ordered `expired-staging/` manifest;
only its execution pagination/API/ETag state is repaired after raw target
verification, with an exact one-row update and checked postconditions. Explicit
missing resource spills stop validation. Read-created continuation pages retain
normal expiry and stay in the shared bucket; inventory spans its scan period.

Stored reads keep original timestamps, public URLs/auth/DTOs/coverage/304 behavior,
and active R2 cursors throughout freeze. Optional demand/metrics/repair writes are
suppressed. No import calls configure/bootstrap or seeds new GitHub inventory.
The reconciler resumes with existing concurrency/quota limits and real retry due
values. A backlog does not authorize an additional GitHub reader.

## Recovery, receipts and observation

```sh
python3 services/github-state/scripts/migrate.py --environment production \
  --generation GENERATION --target TARGET --apply abandon
```

Abandonment is valid only before target activation. It retains archives, fences the
target and releases a writable source. A separately selected operator ingress pause
remains in effect. After PostgreSQL accepts writes, abandonment cannot reactivate
SQLite: preserve PostgreSQL and parked input and repair/restore forward.

Terminal `receipt.json` is immutable and recovered with create-only publication;
ordinary cron does not rewrite it. Separate source/cutover stage receipts remain
available. The terminal receipt includes schema/deployment and sanitized source/target
identities, bookmark/revision, counts/checksums, shadow proof, staging transforms,
cursor/latency evidence, parked/held/raw recovery state, latest applied input,
GitHub request accounting, health and backend write owner. Confirm unchanged external
clients and backlog drain against live telemetry before declaring release accepted.

Keep the original DO and referenced archives read-only through observation. It can
only be an explicitly timestamped emergency last-known-data read fallback after
selection. Never flip writes back and call it lossless rollback. Retirement,
deletion and any reverse migration are separate deliberate operations after
recoverability is demonstrated.

After observing a completed migration and verifying recovery, release the broad
retention hold explicitly with an approved policy file (the original DO and
migration archives remain retained/read-only):

```sh
python3 services/github-state/scripts/migrate.py --environment production \
  --generation GENERATION --target TARGET --retention-policy approved-policy.json \
  --apply finish-observation
```

The command requires `complete` and proves the PostgreSQL owner. It checkpoints
intent before updating retention; ordinary supervision finishes an interrupted
release and publishes a separate immutable observation receipt. No SQL source
is unfenced or deleted. Retire `MIGRATION_ENVIRONMENT` with the migration-only
bindings/credentials after observation. Keep the application URL and archive
access needed for normal operation. The local marker selects each repository's
own generation; leave deployment-wide `REPOSITORY_BACKEND` and
`REPOSITORY_GENERATION` unset so unmigrated or newly added repositories still use
SQLite. Do not roll back to code predating the local selection protocol.

A retained archive includes expiring execution staging and cursors as well as
semantic data. After observation, an approved retention release can expire those
objects. Keep the staging/spill classes pinned if a complete source recovery
snapshot must remain restorable. The SQL archive is never a promise to extend a
client cursor beyond its original expiry. Removing original SQL or migration
archives is a separate deliberate operation.

If the target is lost or unreachable after activation intent, retain its control
record and every parked body/pointer. Do not edit the source fence or change the
control phase back to exporting. Restore the original Neon branch/database from
a verified point-in-time backup at the original endpoint, preserving roles,
schema and generation. Verify its owner row and the latest applied-input watermark. Keep service reads
and writes fenced during restoration; an ordinary S2 `resume` is not a point-in-time
reconstruction tool. A separate, verified forward-recovery procedure must:

- Preserve/quarantine the R2 receipts committed after the restored SQL watermark;
  normal delivery replay skips those receipts, so it cannot reconstruct lost SQL.
- Recover the corresponding original raw bodies and pointers, verify their digests
  and apply them through a recovery owner that commits fresh SQL delivery evidence
  before reinstating authoritative receipts. Missing bodies remain an explicit gap.
- Reconstruct any later non-webhook publications from retained recovery evidence;
  a raw-webhook replay alone does not reconstruct every scheduler or metadata write.
- Advance the revision above every value already published to clients, and explicitly
  invalidate obsolete change-feed/cursor generations without reusing an old ETag.
- Verify counts, hashes, input accounting and unchanged-client workflows on an
  isolated restored copy before restoring production access.

Resume supervision only after that procedure has been verified and the original
enrolled identity is healthy. The included restore drill verifies SQL restoration
of all fixture tables; it does not authorize post-write production reconstruction. If a new
endpoint is required, stop and perform an independently verified forward recovery
with a new identity and migrated write-owner metadata; ordinary S2 abandonment
cannot authorize that identity change. The retained SQLite copy may be exposed
only as an explicitly timestamped emergency read fallback, never as a writer.
