# Disposable deployed storage drill

The service operator owns this drill. Use a fresh synthetic Worker, R2 bucket,
queues and Neon child branch, then retain a sanitized receipt. Do not fault a shared
test mirror or production. Production cutover belongs to the rollout coordinator
under Worker #302 and the separate [S2 migration runbook](github-state-migration.md).
A merged PR is not deployed acceptance.

The [fixture](../services/github-state/scripts/fixtures/deployed-outage.ts) wraps
the production entry point and stores. It disables upstream repairs, excludes
installation audits, permits database traffic only to its configured test endpoint,
and blocks all other external HTTP/WebSocket attempts. Blocked attempts are
recorded as best-effort R2 diagnostic receipts across isolates; persistence failure
can undercount attempts, while the egress guard still prevents physical GitHub calls. Actual migration, scheduled
ingress recovery, queue handling, transactions, R2 operations and retention still
run through production code. SQLite full mode and R2/database errors are injected;
a provider compute restart and point-in-time branch restore are separate operator
operations. These tests do not model every Cloudflare or Neon infrastructure fault.

## Provision and preflight

Create a private directory (`umask 077`) and a fresh name from
`issue317-drill-$(openssl rand -hex 4)`. Record the dedicated Neon project's test
branch as parent and create a disposable child compute. Copy only test-role URLs
into `application-url` and `migration-url`; the database must be
`github_state_test` and both roles must be test roles. Verify the child's actual
provider branch/endpoint IDs and hostname before any restart or schema operation.
Never restart the parent test endpoint.

Commit the implementation and build one immutable module before deploying:

```bash
node scripts/build-deployed-drill.mjs "$DRILL_DIR"
```

Record `build-manifest.json`, including commit, source fingerprint and bundle SHA256.
Set `no_bundle:true` and `main` to `$DRILL_DIR/deployed-outage.mjs` in the private
config so Wrangler uploads these exact module bytes. Associate each resulting
version ID (including a secret-only version) with that manifest.

Copy [the configuration template](../services/github-state/scripts/fixtures/deployed-outage.example.json)
to `$DRILL_CONFIG`. Replace every example name, account, hostname, repository ID
and token hash. Use the immutable emitted module as `main` rather than rebuilding during deploy. Bind only the fresh bucket, queues and namespaces.
The bucket name and `DRILL_DISPOSABLE_BUCKET` must equal `DRILL_RUN_ID`.
Do not install GitHub credentials. Create separate random admin, client and webhook
tokens in `$DRILL_DIR/{admin-token,client-token,webhook-token}`, mode 0600.

From `services/github-state`, create/deploy these disposable resources:

```bash
npx wrangler r2 bucket create "$DRILL_NAME" --location enam --config "$DRILL_CONFIG"
npx wrangler queues create "$DRILL_NAME-events" --config "$DRILL_CONFIG"
npx wrangler queues create "$DRILL_NAME-dlq" --config "$DRILL_CONFIG"
npx wrangler deploy --config "$DRILL_CONFIG"
npx wrangler secret bulk "$DRILL_DIR/secrets.json" --config "$DRILL_CONFIG"
```

The private `secrets.json` contains `REPOSITORY_DATABASE_URL`,
`MIGRATION_DATABASE_URL`, `WEBHOOK_SECRET` and `MIGRATION_TARGET`, as documented by
S2. It must contain only this child's test credentials/identity. After uploading,
remove that redundant secrets file securely according to operator policy. Schema
initialization uses `scripts/postgres-schema.mjs`: `initializeSchema` for the source
repository with `{standby:true}`, and for the independent canary with generation
`canary-v1`. Never reuse an existing nonempty target. Verify PG18, allowance,
least-privilege application role, migration role and a recent actual restore receipt
using S2 preflight before starting migration.

Write private `$DRILL_DIR/state.json` with these fields (IDs are provider receipts,
not guesses):

```json
{
  "name": "issue317-drill-00000000",
  "service_url": "https://issue317-drill-00000000.YOUR_ACCOUNT.workers.dev",
  "worker_hostname": "issue317-drill-00000000.YOUR_ACCOUNT.workers.dev",
  "production_actions": false,
  "repository": {"id": "317000000001", "name": "issue317/drill", "installation": "317"},
  "branch": "DISPOSABLE_NEON_BRANCH_ID",
  "endpoint": "DISPOSABLE_NEON_ENDPOINT_ID",
  "hostname": "DISPOSABLE_NEON_ENDPOINT_HOST"
}
```

## Run and expected output

The runner pins the full recorded Worker hostname and rejects other Worker names,
non-test repository names and redirects. Its explicit checks also run under
`python -O`; verification does not depend on Python assertions.
It does not provision, restart or delete resources. Each fault case restores its
fault flag in `finally`, and each repetition creates fresh acknowledged input.
If the process is killed, clear `/fixture/faults` through the authenticated fixture
endpoint; for SQLite clear `/admin/storage/fault` to `mode:none`. Never bypass a
migration ownership fence to recover a failed drill.

```bash
python -O scripts/deployed-outage-drill.py "$DRILL_DIR" seed
python -O scripts/deployed-outage-drill.py "$DRILL_DIR" frozen-source
python -O scripts/deployed-outage-drill.py "$DRILL_DIR" sqlite-full
```

Expect one JSON result per command with `verified:true` and a private receipt path.
Seed creates 128 PRs, a paginated inventory and a spill-backed exact large-ID
comment. Identical refreshes preserve revision/object count. Full SQLite must serve
warm reads, report missing files as unavailable, acknowledge a durable webhook,
and apply it through the existing scheduler after the fault is cleared. No manual
replay or queue retry consumption is needed.

Measure the SQLite and canary warm read p95 before migration; require the S2
latency boundary. Install a measured acceptance receipt and start the synthetic
repository's migration using the S2 protocol. Keep deployment identity stable until
selection completes. The ordinary minute scheduler must export/import/checksum,
shadow-compare, activate and replay input. Record source/target identities and table
checksums, cursor/expired-cursor comparisons and acknowledged delivery IDs. A
source deployment change or expired pre-switch lease intentionally aborts that
generation; follow S2 recovery rather than changing the lease/checkpoint by hand.

After the selected backend is PostgreSQL:

```bash
python -O scripts/deployed-outage-drill.py "$DRILL_DIR" postgres-unavailable
python -O scripts/deployed-outage-drill.py "$DRILL_DIR" postgres-slow
python -O scripts/deployed-outage-drill.py "$DRILL_DIR" r2-unavailable
python -O scripts/deployed-outage-drill.py "$DRILL_DIR" inspect
```

Expect `verified:true`, a warm cached result preserving its original timestamps,
health below three seconds, ordinary recovery and no duplicate comment. During R2
failure ingress must return 503; acknowledgement occurs only after durability is
restored. A cold database read must return an explicit unavailable response with a
request ID and no substituted empty data. Warm cache survival is limited to the
selected Durable Object instance and normal cache expiry.

The guarded operator helper verifies branch ownership and actual provider metadata,
then records restart/PITR evidence including source identity and request IDs:

```bash
node scripts/deployed-provider-drill.mjs "$DRILL_DIR" "$NEON_TOKEN_FILE" restart
node scripts/deployed-provider-drill.mjs "$DRILL_DIR" "$NEON_TOKEN_FILE" restore
```

It requires `project`, `parent`, `branch`, `endpoint` and `hostname` in private
`state.json`, plus the test-only `migration-url`. The project must match the
recorded dedicated project, the parent must be a test branch, and the child must
be owned by this run. The restore helper locks all 24 synthetic tables while
recording their checksums and point, writes an after-point sentinel, validates
a fresh PITR child and reads its retained spill references through R2. A failed
restore preserves the child for investigation and clears the source fault flag;
only a verified restore child is deleted automatically.

For a real restart, first verify via Neon API that the endpoint belongs to the
freshly created child and its branch name exactly equals `$DRILL_NAME`. Record
`pg_postmaster_start_time()`, issue the single provider endpoint restart, wait for
its returned operation IDs, reconnect and prove the postmaster timestamp advanced.
Read warm cached history and submit a signed webhook during restart, then verify
one delivery receipt and ordinary replay. Never retry an uncertain restart mutation
without checking its operation receipt.

Run the repeatable dedicated-project restore validation separately:

```bash
node scripts/restore-postgres-drill.mjs "$NEON_TOKEN_FILE" "$DRILL_DIR/restore.json"
```

This requires the isolated test URL environment variables used by
`npm run test:postgres`, creates its own temporary fixture/restore child, validates
checksums and exact IDs, restarts only that child and removes it. It does not restore
the deployed fixture automatically. For the latter, quiesce its synthetic writes,
record every table's canonical checksum and a database timestamp, write a sentinel
after that timestamp, then create a second child at the recorded point. Require
all table checksums to match, the sentinel to be absent and referenced R2 objects
to remain readable. Retain both identity and archive receipts; do not select that
restore child as an application backend by changing credentials alone.

Local/isolated contracts also cover quotas, malformed responses, archive failures,
monitoring failure, retention crashes and stale checkpoints:

```bash
npm run drill -- "$DRILL_DIR/local.json"
node scripts/outage-drill.mjs --postgres "$DRILL_DIR/postgres.json"
```

Stop on failed checksums, unknown ownership, missing acknowledged input, external
fetch attempts, exhausted queue retries, failed parking durability, unknown provider
mutation outcome or failed restore. Preserve source SQL, migration archives and
parked input; stop further fault injection and use the recovery runbooks. A failed
cleanup/monitoring job must not disable the next scheduled recovery attempt.

## Receipt and teardown

Record the exact runner/build revision for every phase, rather than applying a
newer script revision retrospectively to historical receipts. The slow case
delays HTTP SQL/upgrade requests; it does not simulate latency within established
WebSocket streams.

Record all acknowledged delivery IDs with SQL receipt count exactly one, explicit
missing coverage/errors/request IDs, last prepared parked/held and queue/DLQ counts,
maximum observed queue attempt, deployment/source identities and the bounded R2
network-attempt audit. Its counters are diagnostic; zero physical GitHub requests
are enforced by the deny-by-default egress guard. Keep actual values unknown if their observations are unavailable.
No log emission is evidence that an operator was notified.

Teardown is allowed only after all acknowledged input is accounted for, parked/held
and queues are empty and receipts are saved. Stop this fixture's scheduled work,
remove its queue consumers, then empty only its disposable bucket using the
admin fixture's bounded `/fixture/delete-objects` endpoint with
`{"run":"<DRILL_NAME>"}`. Each call deletes at most 100 objects; repeat while
`remaining:true`. This endpoint additionally requires the explicit disposable
bucket confirmation variable. Delete the test Worker, queues, bucket and owned Neon
child using their recorded IDs. Record teardown outcomes. Never delete the shared
test branch, shared bucket, production bindings or retained migration data from a
real repository. On failed acceptance, preserve the failing fixture until recovery
is complete rather than treating teardown as success.

The [2026-10-20 deployed receipt](evidence/github-state-deployed-2026-10-20.json)
separates the historical mutable-source rehearsal from a fresh immutable-build
rehearsal. Historical sections are not acceptance of a later PR head.

For the recorded disposable configuration, teardown commands after the bucket is
verified empty are:

```bash
# First set triggers.crons=[] in the private config.
npx wrangler triggers deploy --config "$DRILL_CONFIG"
npx wrangler queues consumer remove "$DRILL_NAME-events" "$DRILL_NAME" --config "$DRILL_CONFIG"
npx wrangler queues consumer remove "$DRILL_NAME-dlq" "$DRILL_NAME" --config "$DRILL_CONFIG"
# Empty/verify the confirmed disposable bucket through the fixture, then:
npx wrangler delete "$DRILL_NAME" --config "$DRILL_CONFIG"
npx wrangler queues delete "$DRILL_NAME-events" --config "$DRILL_CONFIG"
npx wrangler queues delete "$DRILL_NAME-dlq" --config "$DRILL_CONFIG"
npx wrangler r2 bucket delete "$DRILL_NAME" --config "$DRILL_CONFIG"
```

Delete only the recorded owned Neon child after provider metadata confirms its
name, parent and endpoint. Check the provider branch listing afterward and retain
proof that the shared parent still exists. The recorded drill encountered one
transient cleanup HTTP 500; a read-only inventory showed no deletion, and a later
attempt completed. Its subsequent recovery and teardown were not disabled.
