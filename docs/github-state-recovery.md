# Scheduled ingress recovery

The existing minute cron owns recovery; no new GitHub poller, quota owner, or
bootstrap path is introduced. New accepted webhook bodies and replay pointers
are pinned in R2 before HTTP acknowledgement. Queue acknowledgements still
require application or durable parking. Pinned input has no expiry metadata.

Queue application cleans its pinned input immediately; scheduled cleanup is
the fallback after crashes or R2 errors. Concurrent event calls for one delivery
coalesce before body reads. Each repository pass visits pages of 50 pointers
in groups of 10, with up to 20 pages per lane and a shared 20-second dispatch
budget. Durable continuation cursors rotate past
backed-off items; the first lane alternates so a slow normal backlog cannot
starve held input. Five failures move an item to held without ending scheduled
ownership. Per-item retry delays grow from 5 seconds to 10 minutes. One bad
body, pointer, or R2 operation does not cancel other items in the group.

A separate bounded pass on the same cron visits raw deliveries in pages of 50
(up to 20 pages), with a durable keyset cursor and 20-second dispatch budget.
Accepted raw objects carry target/digest metadata so already-durable input
needs only HEAD checks. Legacy committed SQL receipts are promoted without
replaying them. Only visited keys advance either recovery cursor. It reconciles queue/DLQ
loss using stored bytes, delivery IDs, and application receipts. Raw expiry
belongs to this pass and happens only after pinning or a durable explicit
invalid-event disposition. Invalid JSON and intentionally unmirrored or
out-of-scope events get digest-bearing dispositions under `invalid/`. Missing
bodies, digest mismatches, or absent event metadata remain precise gaps under
`control/raw-gaps/`; their source is retained. No GitHub refetch repairs a gap.

An event response waits for SQL durability and then records a durable R2
application receipt under `receipts/<repository>/<delivery>.json`. Replay checks
that receipt before reading a body or applying again. SQL receipt expiry first
archives old committed receipts to R2 in bounded rowid batches of 100, preserving
the retention policy’s explicit pins/disables. Pending pointers are observed,
while durable R2 receipts protect deduplication independently of SQL retention.
Archival uses groups of ten with a separate two-second I/O dispatch budget,
so network latency does not consume the synchronous SQL expiry budget. A crash between
application and cleanup therefore remains idempotent even beyond fourteen
days. Receipt retention is deliberately independent of raw-body expiry.

R2 conditional writes coordinate 60-second recovery leases and reject stale
completion writes. Every tick reloads the durable full marker, including markers published after
object construction and retries after control-store errors. Full-mode ticks
perform bounded expiry, then persist the
next probe time before allocating. Probe cooldowns survive process resets,
grow from one minute to ten minutes, and coalesce concurrent ticks in the
repository object. Automatic probes prove 4 MiB and retain recent probe
cooldowns across full/writable transitions to reduce flapping. Only a committed headroom write **and its committed cleanup**
can publish a writable marker. R2/probe/cleanup failure keeps full mode and
stored reads available. Verified success restarts the ordinary refresh alarm;
GitHub work continues through the existing installation scheduler and quotas.

Ingress mode and repository migration fences still govern application and
probing. Raw pinning may continue during a freeze, but original archives remain
retained. Current migration code supports source export in test only and rejects
production cutover; this change does not authorize a new backend or generation.
Future backend switching must route replay through the authoritative generation
and retain the existing migration mutation fence.

`GET /admin/ingress?repository=...` reads prepared recovery state, ingress mode,
and storage/raw recovery markers. `/v1/health` includes the same recovery state.
Neither status path lists R2 objects or dispatches GitHub work. Repository
readers receive sanitized raw progress without other repositories’ gap details. Prepared counts
are lower bounds when `truncated` is true (up to 1,000 entries per lane), with
`oldest_received`, `prepared_at`, last attempt, next retry, per-lane result,
raw-gap counts, and last/next headroom probe. Counts are null until the first
successful scheduled preparation. When maintenance fails, the last prepared
observation remains available with its timestamp. Independent expiry sweeps visit only registered retention-class prefixes;
permanent receipts/dispositions cannot lengthen those sweeps. Raw expiry
remains recovery-owned and honors the same raw-webhook retention pins/disables;
its prepared progress feeds the existing retention observation. Per-object
`pinned: true` metadata also protects raw bodies. Class policies and migration
fences are checked immediately before each page’s batch deletion.

## Validation and release boundary

Local fixtures cover held retries after five failures, queue/DLQ raw recovery,
expired retry times, a valid item beyond a page of deferred poison items,
near-expiry bodies, invalid/gapped raw input, duplicate delivery, cleanup/R2
failures, overlapping/crashed owners, commit-time full probe failure, reset,
repeated probe cooldown, SQL receipt expiry, migration freeze, and reads without
maintenance scans. The persisted Miniflare fixture restores capacity after a
restart and uses only ordinary scheduled passes to verify headroom, migrate a
legacy schema, replay input, and resume refresh. Clock/cooldown edits in fixtures
simulate elapsed time; no production fault is induced. Recovery introduces zero
GitHub reads; the pre-existing cron audit and refresh keep their original owner.

Read-only pre-implementation audit on 2026-10-20 observed production deployment
`83334067-dc3b-4785-bc74-dc96fc298f79` writable, and test deployment
`152fc04d-bd36-44b7-bd38-9ba2bbdd713f` in durable full mode. Cloudflare deployed
source inspection verified commit-time full markers and bounded concurrent
parking/replay from Worker PRs #327 and #330 in both versions. They are already
present; neither merge is a prerequisite to repeat.

Merging this implementation establishes code evidence only. Deployment,
observed draining, invalid-input disposition counts, and measured recovery of
this implementation have **not** been recorded by this change. A release receipt
must separately record environment/version, prepared backlog before/after,
latest applied delivery, last successful probe, refresh resumption, delivery
volume per repository and measured recovery throughput, remaining
invalid/gapped inputs, and measured request/read behavior. Legacy raw bodies without receipt-time metadata may temporarily create a
second pointer keyed by upload time; delivery receipts and coalescing keep
application idempotent. Legacy bodies without any surviving receipt must replay rather than be
marked applied or discarded; resulting repair jobs use the existing scheduler
and quota limits. Existing emergency
release and Neon implementation retain their independent acceptance boundaries.

Rollback must keep the recovery cron owner, durable receipt checks, retry
control state, and all unapplied bodies/pointers. Revert application behavior
forward while retaining this ingress recovery contract; do not downgrade to an
older scheduled handler that abandons held input, or delete the `parked/`,
`receipts/`, `invalid/`, or `control/` prefixes. A migration after destination
writes still requires forward recovery in its authoritative backend.
