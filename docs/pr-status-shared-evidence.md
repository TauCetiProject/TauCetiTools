# PR-status reconciliation with shared evidence

The shared reader is opt-in through `TAUCETI_PR_STATUS_SHARED_EVIDENCE=true`.
It uses the existing GitHub-state service, the pinned readiness reducer and the
existing batch checkpoint. It does not import TauCetiWorker or fall back to
per-PR GitHub enumeration when shared coverage is unavailable.

Reads send `X-TauCeti-Observe-Only: true`. The service retains authentication and
repository scope, while using its existing observation-only query path: warm
reads do not revive verification jobs or create repair demand. A genuinely cold
resource can request repair through the normal authenticated query path. Each
batch coalesces those requests and permits at most five distinct resource
demands. A request for repair is not complete evidence.

The service's `X-TauCeti-Checked-Revision` header records the boundary through
which an unchanged selected resource was revalidated. It leaves the cached JSON
representation and its ETag unchanged. The reader uses that boundary for its
head/base/comment/status fences. Inventory grace responses retain their older
boundary, so discovery cannot skip events that the inventory has not observed.
Selecting `refs` with `{"name":"main"}` reads the exact retained main resource;
a missing reference has a single-reference repair recipe.

## Rollout

1. Deploy and verify the service contract, preserving the running bundle's
   unrelated production fixes and settings.
2. Pin the merged Tools revision in TauCeti's trusted main workflows. Supply the
   existing service client credential as `TAUCETI_GITHUB_STATE_CLIENT_TOKEN`.
   This is a service credential, not a GitHub installation token.
3. Dispatch `pr-labels.yml` for a selected PR with `shared_evidence=true`. The
   default remains the existing reader until the canary has been checked.
4. Set the repository variable `PR_STATUS_SHARED_EVIDENCE=true` to enable the
   retained shared-discovery loop. Its five-minute recovery schedule does not
   start the legacy reader while the flag is off. The shared hourly backstop
   wakes that same loop; it does not start a second sweep with another checkpoint.
5. Check actual label transitions, review/queue progress, pending age, quota
   recovery and a complete backfill cycle before declaring the rollout complete.

Keep normal CI and worker admissions enabled. The dedicated status runners and
queue-control runners retain their current capacity and trust restrictions.

## Checkpoints and outcomes

The checkpoint retains pending PRs, retryable errors, quota holds by credential
and API resource, and the last complete shared change boundary. Disposable
decision caches bind the repository, head, merge base, policy, relevant labels,
comments and statuses. Membership validity is never extended by decision reuse.
Verified immutable comparisons and changed paths are reusable across restarts.

Exit 0 means the observed discovery boundary completed without errors. Exit 75
means resumable deferral, including a time budget, quota hold or missing shared
coverage. Exit 1 represents a genuine error. Required checkpoint-write or upload
failures remain failures. The job summary and `last_outcome` distinguish these
states; a successful artifact upload does not mean the full workload completed.

## Measurement

`github-api-summary` reports direct reads, writes, GraphQL points and resource
budgets by credential role. `github-state-summary` reports service operations,
missing coverage, bounded repair demands and reused decisions.
`pr-status-git-inputs` records commit-graph fetches, preparation time, cache size
and immutable-result reuse. `pr-status-pending` reports retained work and its
oldest observed age.

Self-tested Mathlib bumps run the existing `check-bump.sh` over observations
obtained through the caller's structured GitHub client. A missing observation
stops the local check; the parent fetches that resource once and reruns the
trusted check with its recorded response. The guard needs at most five upstream
resources and the adapter permits at most eight. Standard GitHub access tokens
are removed from its child environment. The observations are temporary and
scoped to that guard invocation.

These selected upstream reads contribute to the same credential/resource
telemetry and quota hold as the rest of reconciliation. A refusal propagates as
a resumable deferral; unreadable or malformed responses remain unknown. Genuine
pin-policy failures retain the guard's verdict. The separate multi-upstream
resolver retains its existing transport.

Compare the same recorded evidence against the prior deployed reader. Include
additional service-side upstream collection in that comparison: a warm
observation must not merely move repeated reads to the service's installation.
Installation-wide budget movement includes unrelated consumers and is not an
attribution metric. Production verification must supplement the replay.
