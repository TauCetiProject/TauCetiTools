# Shared review coordination

`tauceti_tools.claims.GitClaim` wraps the existing `claim.sh` Git-ref lease
protocol. Acquire, renew and release use Git compare-and-swap without REST or
GraphQL calls. `review_repository` uses an explicit `CLAIM_REPO`, otherwise
existing shared-repository access or an existing direct personal fork. It creates
no fork or invitation.

Reviews claim `review/<owner>/<repo>/<PR>/<full-head-SHA>` with a run-unique owner.
Different models compete for the same head. A busy lease yields before inference;
leases renew, release on exit and expire after crashes. Git outages permit work
with a duplicate-review warning instead of blocking autonomous operation.

Comment-based coordination has been removed. Old comments are ignored; there is
no dual-protocol period, marker publication, deletion or settlement polling.
Completed scoreboards and contest replies retain their existing meaning.
Local fleet receipts record starts and whether a Git lease was held without
network calls. Historical webhook adoption counters are historical observations,
not a cutover requirement or an active coordination mechanism.
