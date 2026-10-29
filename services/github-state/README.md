# Shared GitHub state

The production Tau Ceti mirror publishes `/v1/query` and `/v1/query_batch` reads
for `TauCetiProject/TauCeti` without a client token. Ordinary Tau Ceti workers
therefore need no state-service configuration. `PUBLIC_READ_REPOSITORIES` is an
explicit JSON list of public repository names, intersected with the installed
`ALLOWED_REPOSITORIES`; it is empty unless the operator configures it. Private
repositories and all administration, health, changes and activity APIs retain
their existing authentication. Custom deployments should publish only data
intended for anonymous readers. Anonymous queries serve existing mirrored data;
they never renew demand, schedule hydration, configure a repository or upload
cursor objects. Their snapshots are returned inline, bounded at 16 MiB;
missing coverage and larger snapshots return unavailable. Authenticated clients
retain paginated snapshots and background demand.


Reporting clients can use authenticated `POST /v1/activity` with
`{"repository":"TauCetiProject/TauCeti","numbers":[1,2]}` (maximum 500 numbers).
It returns stored PR headers, original observation timestamps and missing IDs.
This route is observation-only: no repository configuration, demand renewal,
repair jobs, upstream validation or installation-scheduler access. Closed PRs
without cached merge evidence have no landing timestamp. See
[activity reporting](../../docs/activity.md); reporting clients must retain stale
data or unknowns when unavailable, without direct GitHub fallback.

The Cloudflare service receives GitHub webhooks and serves repository state to
TauCeti clients. Production and test each use a separate private, read-only
GitHub App installation on `TauCetiProject/TauCeti`. The service owns those read
quotas. Authoring and commenting continue with the workers' existing credentials.

```mermaid
flowchart LR
  GH[GitHub webhooks] --> W[Cloudflare Worker]
  W --> R[R2 raw deliveries]
  W --> Q[Cloudflare Queue and dead letter queue]
  Q --> D[Repository Durable Object]
  D --> SQL[Selected SQLite or PostgreSQL store]
  C[TauCeti clients] --> W
  W --> D
  D --> I[Installation Durable Object]
  I --> A[GitHub REST and GraphQL]
  D --> R
```

The [Neon PostgreSQL backend](POSTGRES.md) retains the public API and installation
quota owner. The production repository moved to PostgreSQL on 2026-10-26; the
[accepted migration receipt](validation/migration-production-release-2026-10-26.json)
records the checks and retained recovery data. Provisioning, test isolation,
restore evidence, the performance gate and generation fencing are documented
in [POSTGRES.md](POSTGRES.md).

## Data and recovery

The Worker verifies the raw-body HMAC before durably writing a delivery to R2
and submitting its pointer to the queue. The repository object acknowledges
only after recording the event and repair jobs. Delivery IDs make replays
idempotent; timestamps and tombstones protect newer records from older events
and API responses. Large identifiers retain their decimal digits.

The selected repository store holds indexed resources, collection membership and coverage, repair
jobs with pagination state, a seven-day change journal, and stable query
snapshots. Initial inventories and histories publish complete coverage after
all pages finish. A failed page preserves the previous complete collection.
Snapshots paginate at 500 rows or approximately 1.49 MB per page. Continuation
pages are written to private R2 (at most four uploads at a time), never to
SQLite; continuation reads individual pages rather than repeatedly decoding the
entire inventory.
Concurrent queries at the same revision share a snapshot.

PR and issue headers and commit statuses update from webhook payloads. A
changed PR does not start a full repository survey. Full inventories reconcile
hourly; overlapping deltas catch missed updates.

GitHub Actions runs and jobs and check runs are not mirrored. No client reads
them, and keeping every run, job and check run for every commit was about 90%
of the repository database. CI webhooks are acknowledged without being stored,
and `checks`, `workflow_runs` and `workflow_jobs` queries answer
`status: "unavailable"` with reason `not_mirrored`, so a client uses its direct
GitHub path rather than an empty result.
Existing CI rows are removed, without an archive, with the `ci_resources`,
`ci_members`, `ci_collections` and `ci_meta` cleanup classes.
Quiet closed PRs also stop periodic history and head polling once their
baseline is complete. Webhook updates continue, and renewed client demand
reactivates background reconciliation while returning the stored result.

Raw deliveries are eligible for expiry after fourteen days only after durable
replay input or an explicit invalid-event disposition exists. There are no daily
R2 archives; any left from earlier versions expire. Staging pages after one day, and query cursors after ten minutes. R2 cursor
objects expire after a one-minute grace period and have their own retention sweep.
At the SQLite size limit, stored reads keep working and ingestion parks durably in R2
until headroom returns. The [storage recovery runbook](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/github-state-recovery.md)
describes the admin endpoints and the example harness's recovery commands. Active resource spill objects
remain readable while referenced by the SQL state. The existing minute cron now
retries held and parked input, reconciles retained raw/DLQ deliveries, and probes
full-mode headroom automatically. See [scheduled recovery](../../docs/github-state-recovery.md)
for ownership, prepared status, release evidence, and rollback requirements.

The installation object owns token refresh, all GitHub read requests,
per-resource quotas, API selection, and accounting. Every GraphQL query asks
for `rateLimit.cost`; concurrent header differences are not point accounting.
REST 304 responses record zero primary points. Similar quota pressure permits
selection by measured latency; scarce pools are preserved for operations with
no alternative API. Batch histories and reactions reduce request cost. Nested
or outer overflow continues through the normal paginated reader.

Primary exhaustion defers that pool and tries a compatible alternative.
REST-exhausted flat review histories promote the existing GraphQL thread
reconciler, which supplies the same flat collection without duplicating a
reader. REST-capable batches can also move to REST before GraphQL exhaustion
when their measured cost/headroom makes that pool preferable.
Secondary limits pause both pools after the actual rejection and honor
`Retry-After`. HTTP-200 GraphQL quota errors are also recognized. Known data
remains available while background reads wait. New content, concurrency,
compute, point, or primary quota categories appear in protected health telemetry
and structured logs. There is no preemptive pacing of GitHub writes.
Exhaustion midway through pagination restarts the staged read in a compatible
pool; it never mixes REST pagination URLs with GraphQL cursors or publishes a
partial collection. Token renewal respects its own exhausted quota too.

Background scheduling uses requested-gap, active-change, verification, and
archival lanes, weighted 8:4:2:1 with unused shares borrowed by eligible work.
Missing requested coverage is promoted immediately without revoking actual
quota cooldowns or pagination. Old numeric priorities cannot keep historical
CI ahead of worker-needed histories. Each alarm launches up to sixteen
requests in four-request waves during a ten-second launch window. Network
calls run outside installation-wide locks; atomic quota reservations and
expiring job leases protect concurrent requests. Large nested thread histories
checkpoint one comment page per quantum instead of monopolizing a turn.

`RECONCILIATION_SETTINGS` is an optional JSON variable with `concurrency`
(default/max 4), `dispatches` (default 16, max 100), `launch_ms` (default 10000,
max 30000), and four positive `weights` (default `[8,4,2,1]`). Settings affect
background reads only. Health includes lane eligibility, oldest requested age,
repair arrivals/completions/cancellations, wait reasons, requested latency p95,
and measured cost/latency by query shape and batch size.

Repair publication releases buffered staging text as it decodes pages and drops
its decoded inventory before preparing dependent jobs. PostgreSQL publication
cache groups contain at most 128 rows and 2 MiB each of old body bytes and new
serialized string storage (allowing one oversized row). Old body sizes determine
the groups before bodies are fetched, including compact listings over large
stored detail rows. Membership is decided from the final stored resource state.
Follow-up jobs are deduplicated once and processed in groups of at most 512,
reading only IDs, states, membership and scheduling fields. They do not reload
PR bodies or nested job cursors. All writes remain in the same transaction.

These limits describe PostgreSQL cache-group inputs. Decoded inventory and
objects used for merging or serialization also consume memory.

The SQLite publication fixture records process memory, a pre-refresh baseline,
and Node/V8 heap-limit metadata. It runs with a 96 MiB Node old-space flag without
forced garbage collection. A historical CI run with that flag sampled
140,267,192 bytes (133.8 MiB) of Node heap usage. Process-wide samples can include
startup allocations and uncollected garbage; Worker-isolate capacity requires
separate runtime evidence. The
[publication evidence](validation/publication-memory-native-2026-10-20.json) also
records native `recent_prs` collection/job completion observations. Those may
follow an unchanged response and do not establish full publication or measure
isolate memory usage.

For an isolate-reset investigation, temporarily set `RECONCILIATION_TRACE` to
`"true"`. Background alarm and publication stage logs contain only the backend,
operation, page number, and start timestamp. They exclude query arguments, keys,
cursors, credentials, and payloads. Tracing is disabled by default; remove the
variable after collecting the diagnostic trace.
Set it in the selected environment's Wrangler `vars` and deploy; changing the
variable restarts the object. Traces include aggregate row, repair and staged-character
counts. `publication_applied` precedes commit; only `refresh_committed` confirms
the transaction completed. Background storage work can continue after
`alarm_complete`; its completion is recorded separately as `storage_prepared`.
An opaque random `trace` correlates stages of the same job object within a
dispatch, including concurrent jobs with identical operation/page metadata.
`publication_encode_pages` records the staged size before the encoding scan.

## Client behavior

Clients request the newest available service data, including conditional reads.
They do not impose an age threshold or validate with GitHub before spending.
Unavailable coverage, invalid client credentials, or failed transport uses the
existing direct facade. A shared circuit breaker bounds repeated outage probes.
Healthy pagination can exceed the initial five-second outage interval; each
request and stalled interval remains bounded. Partial histories never become
empty results. The direct facade and service share the field/route catalog in
`tauceti_worker/github_catalog.json`.

Worker surveys use a materialized, roadmap-filtered fact snapshot instead of
fetching every PR's full comment histories. The scheduling cascade remains in
the worker for full service-outage fallback. See [compact surveys](../../docs/compact-surveys.md)
for scope rules, per-history coverage, migration, telemetry, and cross-language
contract tests.
The v1 response keeps `review.markers` as an empty array for existing clients.
Coordination comments and claims in older cached projections do not populate it.
Survey ETags include a representation tag so an older markerless cached response
is replaced once, after which ordinary conditional reads return 304 again.

```toml
max_open_prs = 128
github_state_url = "https://github-state.taucetiproject.org"
github_state_token_file = "/home/kim/.config/tauceti-github-state/client-token"
```

The cutoff defaults to eight and must be a positive integer. Read-policy
configuration changes wait for an idle round boundary; they must not interrupt
paid sessions. Direct quota failures retain publication intents privately,
including copied body files, so replay does not repeat paid authoring. Ambiguous
creates reconcile with GitHub before replay; opaque ambiguous mutations remain
pending rather than being blindly duplicated.

Shared host snapshots of TauCetiRoadmap refresh hourly and review references
every fifteen minutes. These are immutable, self-contained read-only checkouts
with leases for active users. A fetch failure can reuse the existing snapshot.

## Operator commands

Registration forms already contain the organization, webhook URL, read
permissions, and subscriptions. Register production and test separately:

- https://github-state.taucetiproject.org/setup/register
- https://github-state-test.taucetiproject.org/setup/register

Exchange the one-use manifest code on stdin. Credentials stay under the private
`~/.config/tauceti-github-state[-test]` directory. Node converts GitHub's RSA key
to PKCS8 when system OpenSSL is unavailable. `finish-exchange` can recover from
a conversion failure without consuming another registration code.

```sh
python3 services/github-state/scripts/setup.py inspect --environment test
python3 services/github-state/scripts/setup.py deploy --environment test
python3 services/github-state/scripts/setup.py bootstrap --environment test
```

`inspect` discovers the installation and checks its permissions and selected
repository using App authentication. Deployment provisions resources
idempotently and uploads secrets without printing them. Wrangler keeps remote
variables, so later code deployments preserve the App and allowlist settings.

```sh
cd services/github-state
npm ci
npm test
npm run test:publication-memory
npm run deploy -- --env test
npm run deploy -- --env ''
```

`POST /v1/activity` also derives Lean LOC from complete cached PR file lists,
checking immutable file URLs against the PR's head/base and header totals.
Derived counts are cached by PR head/base and file-collection revision, with at
most 32 uncached computations per request. It reads stored resources only: no
client demand, upstream request, repair job, or closed-PR polling is created.

`/healthz` is public and exposes only service identity/version. `/v1/health`,
`/v1/query`, `/v1/query_batch`, and `/v1/changes` require a repository-scoped
client token. Valid credentials querying an unavailable repository scope get
a structured `status:unavailable` response with no data, so that scope cannot
poison outage handling for supported repositories. Invalid credentials still
return 401. `/admin/bootstrap`, `/admin/storage[/...]` and `/admin/ingress[/...]` require the separate admin credential.
Installation tokens and private keys never reach clients.

`allow_observed=true` remains accepted on `workflow_jobs` queries for protocol
compatibility, and other operations still reject it; the answer is
`not_mirrored`.

The test environment enables `/admin/reference`: a protected, uncached
catalog-only read through its installation quota owner. It requires the admin
token and `REFERENCE_READS=true`; production disables it by default. Reference
reads share test accounting and quota reservations, and never repair the test
mirror before comparing it. No arbitrary API paths or queries are proxied. Catalog-generated measurement
batches (up to 100 aliases and twenty initial review threads) use this same
owner, returning only cost/latency/overflow summaries. `measure.py` no longer
mints an independent installation token or spends quota outside that owner.


## Measurements and worker rollout

Service measurement commands remain in `scripts/measure.py`, `measure_surveys.py`,
`measure_capacity.py` and `observe_reconciliation.py`.
The example worker's process-manager rollout and shadow commands live in
[TauCetiWorker/services/github-state/scripts](https://github.com/TauCetiProject/TauCetiWorker/tree/main/services/github-state/scripts).
Downstream deployment and credential setup are documented in the
[manual](../../docs/project-setup.md#github-state).

The [migration source runbook](../../docs/github-state-migration.md) describes
S2's supervised export/import, exclusive write switch, production gates and recovery protocol.
Source verification does not select PostgreSQL or constitute a release receipt.

### Shared runner-capacity discovery

`GET /v1/runner-capacity?repository=OWNER/REPO` requires an existing client token
authorized for that configured repository. Its installation owner performs one
bounded organization inventory and shares the result for two minutes, coalescing
concurrent requests. It counts running GitHub-hosted jobs, excluding Namespace
and self-hosted labels, across the repositories visible to the installation.
The current deployment organization has only public repositories. Adding private
repositories requires granting the installation visibility to those repositories.

The response includes `complete`, `organization`, `busy`, `observed_at`, `retry_at`,
and refresh request counts. This is an advisory observation, not a reserved slot
or an atomic snapshot. Missing pages or quota rejection produce HTTP 503 with
`complete:false`, never a partial count. Failed refreshes are cached for two minutes
(or a longer instructed cooldown). Routing version 2 also reports queued/running
Namespace build jobs and queued fleet targets from the same bounded scan, shared
by all pickers. Picker, recovery, publication, and finalizer control jobs are
excluded from the build count.

Refresh uses existing installation quota accounting, honors secondary cooldowns,
leaves at least 1,500 core requests for repository work, and spends at most 2,400
read attempts per rolling hour, including lost network responses on this operation. Brief request-slot
contention yields within the scan deadline rather than invalidating the snapshot. A scan is bounded to
80 requests, at most three simultaneous reads, and a 15-second launch window; individual requests retain their
existing timeout. There is no alarm, CI event storage, or new polling task.

Pickers retain bounded direct discovery when this view is unavailable. Visiting
ordinary reports/status does not invoke capacity discovery.
