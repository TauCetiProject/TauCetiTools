# CI API quotas and publication recovery

Build identity, ancestry, merge-base and Lake-pin tree checks use isolated Git
object readers. They never load a candidate's Git configuration or hooks. Bors
still selects the staging ref through its fixed HTTPS authority; the fetched ref
must match the dispatched head and descend from the dispatched base.

CI reads use the repository's Actions token by default. Essential commit-status
writes can use a separate App installation, protecting them from exhaustion of
the repository's smaller workflow quota. The credentials action requests only
`statuses:write` for this token and restricts it to the content repository.
Tokens are scoped to trusted steps and are not inherited by candidate builds.

Configure these repository Actions secrets:

| Secret | Purpose |
| --- | --- |
| `CI_STATUS_APP_ID` | Numeric ID of an installed App with commit statuses write permission |
| `CI_STATUS_APP_PRIVATE_KEY` | That App's PEM private key |
| `CI_READ_APP_ID`, `CI_READ_APP_PRIVATE_KEY` | Optional separate read installation, with contents and pull requests read |

Without status-App secrets the workflow remains usable with its normal Actions
token and reports that reads and writes share a pool. A partial ID/key pair is
an actionable configuration error. The review/automation App is not implicitly
reused: it may not have commit-status permission. A private deployment needs
Actions read access for run/attempt observations; the default workflow token
already has that permission.

For TauCeti, status writes use a token scoped to statuses from the existing Bors
App installation. This pool is separate from the Actions repository pool and
the review App. It remains shared with Bors itself: minting additional tokens
for the same installation does not create additional capacity. A dedicated CI
App can replace these two secrets without changing workflows. Change repository
secrets at https://github.com/TauCetiProject/TauCeti/settings/secrets/actions and
manage installations at https://github.com/settings/installations.

The shared client logs sanitized endpoint, credential role, HTTP status and
quota limit/remaining/reset information. It uses conditional reads for repeated
run/job observations, honors primary reset and secondary Retry-After timing,
and bounds waits and retries. It never treats an API error body as PR metadata.
The state server intentionally does not retain Actions jobs/runs; these changes
do not recreate that unbounded database history or add a polling service.

Before posting, trusted reporting saves small JSON intents outside the candidate
sandbox. Successful publication removes them. Failed publication retains them
as `ci-status-intents-sandbox` or `ci-status-intents-finalizer` artifacts for seven
days. A failure-only reporting workflow downloads intents from the exact source
run, validates their schema and run/attempt binding, and retries publication.
It never rebuilds or executes candidate code. Changed PR heads/base branches,
canceled sources, and superseded attempts cannot receive stale passing statuses.
Finalization recovery rechecks publisher and Bors evidence when it was not yet
available before the interruption. Normal reporting waits at most ten minutes;
the isolated recovery workflow allows a quota-reset wait up to 3,700 seconds.

An explicit trusted failed `GitHub API quota unavailable` step identifies quota
exhaustion to Fleet using job metadata it already reads. With healthy host/setup
evidence, that failure does not cool down the runner. Host failures, other setup
failures and timeouts retain their existing protection. No log-download request
is needed for this classification.
