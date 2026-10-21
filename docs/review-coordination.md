# Shared review coordination

`tauceti_tools.claims.GitClaim` wraps the existing `claim.sh` Git-ref lease
protocol. The script is bundled in the wheel; acquire, renew and release use
Git compare-and-swap, with no REST or GraphQL calls. `review_repository` uses
an explicit `CLAIM_REPO`, otherwise existing shared-repository access or an
existing direct personal fork. No fork or invitation is created here.

Reviews claim `review/<owner>/<repo>/<PR>/<full-head-SHA>` with a run-unique
owner. Different models compete for the same head. Leases expire and renew;
a coordination failure does not introduce a permanent worker block.

During the transition, updated Review clients also publish legacy comment
markers with `coordination_protocol: git+comment/v1` and a `git_claim` boolean.
The shared GitHub state service counts newly observed created-marker webhooks
in bounded hourly buckets. Repeated deliveries, marker edits and deletions do
not add starts. Counts survive marker deletion. No reporting GitHub API calls,
new database tables, or new pollers are required.

`POST /v1/activity` includes `review_coordination`: observed starts over 24 hours
and seven days, classified as dual-protocol, legacy-only, or unknown, plus how
many dual starts held a Git claim. Fractions exclude unknown starts, whose count
is reported separately. Hourly windows can include up to one extra boundary
hour. Observation begins with deployment; these are observed starts, not an
inventory of installed clients. Local fleet receipts additionally count marker
post failures, which webhooks cannot observe.

Review adoption about seven days after deployment; choose the actual cutover
from observed legacy use and active lease bounds. Do not automatically switch
protocols on a date. The subsequent release removes comment markers and their
settlement polling. Truly legacy-only clients must upgrade before that cutover.
