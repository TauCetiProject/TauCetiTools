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

## Forgejo leases

`ForgejoGitClaim` opts into the existing custom-ref lease protocol with explicit
`RepositoryIdentity` records for the claims repository and the source repository.
Supply an origin-bound Forgejo API client, a Git token and its credential origin.
The source/resource pair is encoded by `qualified_claim_key` into a valid Git ref;
server identity, native repository ID and resource all participate. Repository-name
case aliases and renames retain the same source key. Existing GitHub leases keep
their current names. Native TTLs must be positive integers at most 86400 seconds;
each concurrent run must have a different owner nonce.

The claims repository must have Actions disabled. Every native command checks its
name, native ID, archive/mirror flags and Actions setting before and after Git.
Administrative repository rename/deletion must not overlap an operation. Token
scopes and actual write access are enforced by the Git server; API access alone
never proves the Git token can acquire a lease. Claims are cooperative deduplication
and confer no review or merge authority.

Credentials live in a clean, origin-scoped Git child environment. Redirects,
credential helpers, inherited tracing/authentication and candidate-selected helper
scripts are disabled. The token is absent from command arguments and persisted
Git config. The existing private local bare cache is keyed by server/native claims
repository ID. A short local initialization lock protects cache setup; remote
ownership still uses `--force-with-lease`.
Unexpected local Git configuration is rejected before authenticated transport.

Return values remain 0 for confirmed success/ownership, 1 for contention or lost
ownership, and 2 for unavailable state. Network/permission failures and unreadable
or malformed history cannot become an absent or expired lease. Native callers must
handle 2 explicitly before starting work; the native Review caller integration is
still required. A local `held` flag is only a hint; `command("holds")` checks the
remote lease again.
Cleanup attempts an owner-checked CAS release even after a lost acknowledgement;
a timed-out command terminates its Git process group before returning unavailable.
Native commands serialize with release so a delayed heartbeat cannot renew a
released run. `release()` returns the protocol result: retry a 2 after the outage
clears, using the same object/owner. Repository checks remain required during
cleanup; a sustained API outage can leave the lease until expiry. Create a new
object with a new nonce for a new run. `last_error` identifies the failed phase
without including remote output or credentials.

Damaged or foreign-format lease records require operator inspection and manual
removal; GC preserves them and continues collecting other expired leases. All
clients sharing the namespace must use the updated canonical script for these
failure semantics. Native transport drops ambient proxy settings.

`tests/integration_forgejo_claims.py` exercises the real HTTPS Git transport on
stock Forgejo: contributor access, custom refs, competing claims, renew/release,
process restart, SIGKILL/expiry takeover and revoked write permission. Set
`FORGEJO_BINARY`; optionally set `FORGEJO_STATIC_ROOT`. The disposable server
disables Actions/registration/SSH and deletes its state when it stops.
