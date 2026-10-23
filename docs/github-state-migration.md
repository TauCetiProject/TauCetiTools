# GitHub state migration

[#316](https://github.com/TauCetiProject/TauCetiWorker/issues/316) owns the production
migration and receipts. S0 ([#303](https://github.com/TauCetiProject/TauCetiWorker/issues/303))
is stored-read compatibility and durable parked ingress. S1
([#308](https://github.com/TauCetiProject/TauCetiWorker/issues/308)) is the dedicated
Neon PostgreSQL backend. Both must be deployed and accepted before production
start. Merging code or closing an issue does not prove deployed acceptance.

The [2026-10-22 test checkpoint](../services/github-state/validation/migration-test-checkpoint-2026-10-22.json)
records a verified import of all twenty tables and PostgreSQL selection. Replay
and final client, cursor, latency and request-rate acceptance remain incomplete;
the checkpoint is not a release receipt and does not authorize production cutover.
It preserves the original source identity and baseline, records the reviewed code
updates separately, and records a hold after external test deployments changed
the running artifact. Stopping the external operator initially left scheduled
recovery running under unaccepted code against authoritative PostgreSQL. The
checkpoint records that interval and the later durable ingress pause, which
blocks webhook application and parked replay while accepting signed input into
R2. Raw recovery continues after this pause, including R2 cursor and CI/non-CI
gap-marker changes; its unaccepted-code interval remains open. Interval receipts,
cleanup and CI gap disposition require accounting before adoption or final
release; a lower raw-gap count is not delivery proof. Test ingress resumes only
after the accepted live version and single PostgreSQL owner are reverified.

The [2026-10-23 client compatibility deployment](../services/github-state/validation/migration-client-compatibility-2026-10-23.json)
records the production deployment of [PR #90](https://github.com/TauCetiProject/TauCetiTools/pull/90)
while SQLite remains authoritative.
The original Worker client passed fresh-cache and retained-cache survey,
inventory, history, pagination and 304 checks. Rereading a byte-exact copy of the retained survey
cache added the required marker-list field to all 399 rows that lacked it.
The unchanged client performed the refresh; the original cache was left intact. This is a code-only deployment
receipt: test replay and its final release gates remain outstanding, and no
production data migration has started. It does not replace the completed test
release or fresh deployed S0/S1 acceptance required before production cutover.

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

For large repositories, keep the resumable admin command running during the
migration; cron provides recovery at a slower pace. Each request performs at
most five bounded steps, so cron alone can take days across millions of rows.
Use the enrolled generation and target from `start`:

```sh
python3 services/github-state/scripts/migrate.py --environment production \
  --generation GENERATION --target TARGET --apply \
  --steps 10000 --batch-steps 5 resume
```

Repeat the same command when its bounded step allowance ends. Monitor phase,
checkpoint age and the recorded failure code; do not deploy another source
version during a pre-switch generation. The bulk command returns when `replaying`
is reached; use `status` while the normal bounded ingress owners drain the backlog.
Cron publishes the completion receipt when its release gates pass. An HTTP or
transport failure also exits the command: inspect the reason and rerun the same
generation for a transient failure.

Historical parked CI input can dominate the replay backlog even though new CI
webhooks are acknowledged as not mirrored. The existing replay owner batches at
most 100 due CI delivery IDs into one fenced ignored-receipt transaction, without
loading their webhook bodies or creating resources, jobs or change-feed entries.
It still publishes each permanent R2 receipt before deleting that pointer/body.
Lost responses and interrupted cleanup reuse delivery receipts. Semantic events
use the ordinary application path, held retry deadlines remain in force, and an
older repository deployment without the private batch RPC falls back to that path.

Explicit admin replay shares cron's conditional R2 lease and stored lane cursor.
The optional `limit` must be an integer from 1 to 100 (default 25); invalid
values return HTTP 400 before acquiring the lease. Each pass uses a twenty-second
work budget, respects pointer and recovery-error backoffs, and returns `leased`, `deadline`, or `lease_replaced` when appropriate.
The requested limit is an upper bound; successful admin replies report the
effective `page_limit`. A zero-progress deadline with due input and an adequate
initial budget halves the observed page size, down to one pointer. Limits are
persisted in the R2 lease claim before work starts. Cron keeps a fixed page limit
within each lane, alternates lane preference, and checkpoints once between lanes
when budget remains. That conditional handoff preserves the first lane's progress
and names only the second lane in the new claim. A lost claim or handoff response
may conservatively reduce that named lane even if it never started; this uncertain
evidence expires within fifteen minutes. No checkpoint write is added per page.

An expired owner can reduce only its named lane, and a cancelled smaller admin
probe cannot reduce the automatic limit. Four fast full pages allow cautious
growth without extending an existing recovery expiry. Both working limits and failed-size ceilings expire after fifteen minutes;
quiet repositories and held input do not need successful full pages to recover
the default size. Expiry and legacy CI-count evidence can reduce the working limit
but never install a ceiling. Legacy CI counts establish an attempted batch, not
its complete page size, so they select a conservative 25-pointer working limit.
Optional lane-selection read failures fall back to normal replay; actual drain
errors still retain the usual recovery backoff. Prepared recovery metadata records
learned limits/expiry and each pass's page size, due-input count, budget and duration.
This changes page sizing only; ownership, receipts, cursor checkpoints and retry
deadlines still govern application and cleanup.
An expired owner's completion cannot overwrite a replacement checkpoint.
Permanent delivery receipts use conditional creation. Failed cleanup updates a
pointer only if the ETag of its loaded snapshot still matches, so a stale attempt cannot
recreate an already deleted pointer or shorten a newer retry deadline.
`delivery_errors` in the immediate admin response contains bounded delivery/event/
timestamp/reason metadata with `retry_scheduled`, `held`, `superseded` or
`unconfirmed` outcomes for receipt inspection. Superseded updates are counted
separately from failures. These error arrays are excluded from newly stored pass summaries and are
filtered from health and observation responses, including older summaries. Successful manual work does not postpone cron. A completed held sweep only
unblocks reconciliation when that sweep started after target selection; a saved
pre-selection cursor cannot provide that evidence. A parkable refusal retains
the checkpoint before its group. Future retry deadlines remain binding; a sweep
visits deferred input without treating it as applied, and held input still blocks
final migration completion. Treat a lost response as
unknown progress and repeat through the same leased interface.

If replay needs a post-selection service fix, preserve PostgreSQL ownership,
the source write fence, the original selection deployment and baseline. Record
the fix-forward deployment separately in the operator's release receipt, verify the
same target generation and bindings before/after deployment, and run the public
read/client gates against the resulting service. Do not rewrite the source
identity or original checksums to make the new deployment appear to have
performed the export. The runtime receipt keeps the original `source.deployment`
and records the current version in `cutover.release.deployment`; retain the
original selection version and the forward PR/code/version in the operator's
receipt as well. A deployment change before selection still invalidates
the frozen generation and follows the ordinary pre-switch recovery procedure.

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

The native byte-sizing component diagnostic can be reproduced from
`services/github-state` after installing its development dependencies:

```sh
node scripts/benchmark-migration-bytes.mjs
```

It reports its runtime versions and checks byte-count equality. Its query timing
is separate from full migration throughput and the deployed public-read latency gate.

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
rows for source-only exports, or 5,000 rows for supervised cutovers (reduced for
wide tables), and a 512 KB logical target, allowing one larger row for progress. Cells carry
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

Held-input recovery is a fix-forward operation, not permission to discard input.

Repository data writes use serialized interactive transactions at Read Committed
isolation, including standalone write batches. Every data writer must acquire the
database ownership lock. Before
acquiring the ownership lock, it records the session PID, exact backend start time
and a unique transaction-local application name. When a client times out or loses
its transport, the adapter closes that connection instead of queuing rollback
behind the stalled query. Ordinary SQL and application errors still roll back a
healthy connection.

For a direct Neon endpoint, a separate bounded HTTP query terminates only that
identified session in the same database and application role, and waits for server
confirmation. Pooler and other endpoints only check whether the identified
transaction has ended; they never signal a reusable backend. Unconfirmed cleanup
blocks local writers for up to 45 seconds, with a one-second cleanup retry cooldown.
After that grace period, writers retry the database ownership lock even if HTTP
remains unavailable. This is an availability grace period, not a bound on server
COMMIT or socket waits. Expired identities remain available for bounded cleanup
attempts in rotation; only confirmed termination or an ended transaction removes
an identity. A still-held ownership lock causes a bounded lock timeout and leaves
input parked. Stored reads remain available. Health reports cleanup count, age,
blocking/expired state and HTTP cleanup failures. Prepared capacity monitoring
uses its separate generation-checked HTTP function. This local gate does not
survive an isolate restart or store replacement; the database ownership lock and
server timeouts still apply.

A failed or uncertain commit never retries the application callback; termination
or an absent session does not prove rollback. Input replay checks the committed
delivery receipt. Sanitized diagnostics report cleanup result, failing phase,
writer wait and transaction duration without session tokens, SQL or driver error
bodies.

Inspect the pointer/body and their digest privately; never paste payloads or secrets
into logs or issue comments. Recover an exact missing body from retained raw/pinned
input or migration archives, or fix the parser/target error on PostgreSQL. Preserve
the original delivery ID and receipt timestamp. Wait for the existing retry deadline
or invoke bounded `POST /admin/ingress/replay` with `repository`, `held:true` and a
small `limit`; the same owner still respects per-item cooldowns. Verify the target
SQL delivery receipt and permanent R2 receipt before pointer cleanup, then allow
ordinary supervision to finish the release. The retained SQLite store stays fenced.

If the exact input cannot be recovered or applied, keep its pointer/body and record
a sanitized incident with delivery ID, digest, failure code and recovery attempts.
Do not forge an applied/tombstone receipt or delete an unapplied input to obtain a
complete migration receipt. Stored PostgreSQL reads and bounded reconciliation
continue, while final completion and the broad retention hold intentionally wait
for recovery. An irrecoverable data gap is not a successful migration.

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
The terminal receipt's `github_hour` and `github_hour_observation` describe retained
accounting at replay completion, including its original observation time, scan
completeness and known telemetry failure. They are context, not a post-completion
release measurement. Observe a separate fresh full window starting after both
replay completion and the released deployment; a restored report's current
deployment ID does not establish that window. Retained accounting counts logged
requests and cannot prove that interrupted or failed accounting attempts never
occurred. Check migration's GitHub dispatch fence and test its zero-bootstrap
behavior separately.

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
and writes fenced during restoration. Before changing the database, park ingress
with `POST /admin/ingress` (`repository`, `mode:"parked"`), and block public reads
using a reviewed temporary maintenance deployment returning HTTP 503. Disable the
application role's login and terminate its existing sessions using a verified
administrative role. The maintenance deployment must also suspend automatic SQL
writers, scheduled reconciliation and repository alarms while retaining durable
webhook parking. Verify these gates before restoration. Restores can reinstate role settings, so do not restore access at the original
endpoint until a restricted recovery role has verified the restored role remains
NOLOGIN and the maintenance deployment still blocks reads. Use the isolated restored
copy first. Keep these gates in place throughout reconstruction. The CLI `resume`
command advances ordinary S2 checkpoints; it is not a point-in-time reconstruction
tool. A separate, verified forward-recovery procedure must:

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

For large repositories, `--batch-steps 100 --steps 10000 resume` reduces admin
round trips. Each request processes at most five steps and also stops after
20 seconds or its bounded I/O budget. Cron and the next identical invocation
resume the last durable checkpoint. The five-step ceiling limits cumulative
native SQL, R2 and PostgreSQL allocations per invocation, even when more steps
are requested.
Supervised SQL pages contain at most 5,000 rows, 60,000 PostgreSQL parameters,
512,000 logical value bytes (except a single larger row), and 250 distinct R2
references. Wide tables reduce the row bound to preserve the parameter limit.
The legacy source-only export retains its 250-row limit. Page sizes do not affect
the canonical per-table hash or exact rowid checkpoints.
