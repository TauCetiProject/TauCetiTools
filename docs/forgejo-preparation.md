# Forgejo preparation

Implementation tracker: https://github.com/TauCetiProject/TauCetiWorker/issues/602.
Development fork: https://github.com/TauCetiProject/forgejo.
Unmodified GitHub CLI follow-on: https://github.com/TauCetiProject/forgejo/issues/1.

## Architecture and boundaries

Forgejo owns source, PRs, reviews, issues, and merges. Clients read it directly.
The existing queue owns admission and repair. GitHub Actions remains the initial
execution backend and fetches immutable retained candidates directly from Forgejo.
The main branch mirrors back to GitHub. The shared GitHub state service remains
operational for current consumers and is retired after migration.

Agents retain familiar `gh` commands through a process-local compatibility layer.
Explicit server references select their server; unsupported Forgejo commands fail
without falling through to the GitHub mirror. Credentials and authority never
transfer merely because names or numeric IDs match between servers.

Preparation ends with a complete isolated author/review/fix/queue/CI/merge/mirror
rehearsal. It does not activate production Forgejo bindings or shut down services.

## Foundation delivered by this change

`tauceti_tools.forge` provides qualified `Forge`, `RepositoryRef`, and
`PrincipalRef` records, immutable source links, reference resolution, and a bounded
direct Forgejo REST client. Mutations are attempted once; ambiguous failures require
reconciliation. Page failures raise rather than returning partial success. Listings
use page numbers and are observations, not atomic snapshots of changing repositories.
HTTP redirects are
not followed with credentials. HTTP origins are accepted only on loopback for tests.

Schema v1 continues to resolve repository strings to GitHub and retains state keys.
Schema v2 adds named forge connections and `{forge, name}` references; string
references still mean GitHub. The independent `[ci]` execution repository initially
supports `github-actions`. Immutable tool-pin validation still applies.

New clients use `project.repository_ref(role)` and `project.ci_repository_ref()`.
Legacy owner/name/environment access rejects non-GitHub bindings, preventing an
unfinished adapter from acting on the GitHub mirror. This guard must be removed
only as each caller obtains full qualified-reference support.

The experimental `tauceti-gh` executable supports `pr list` and `pr view`, JSON
projection, `--jq`, configured repository selection, and explicit GitHub delegation.
It is not installed over the operator's `gh` and is not yet wired into workers.
Forgejo authentication uses a private `TAUCETI_FORGEJO_TOKEN_FILE` and requires
`TAUCETI_FORGEJO_ORIGIN` to match the normalized destination origin. Set both in
trusted process configuration outside the checkout. Public reads can omit them.
Ambient tokens for GitHub Enterprise destinations similarly require a matching
`TAUCETI_GITHUB_ORIGIN`. Configured destinations cannot redirect a credential to
another origin. Without `-R`/`GH_REPO`, all-GitHub projects retain native gh checkout
detection; mixed projects require a checkout origin matching exactly one configured
repository. Otherwise specify `-R HOST/OWNER/REPO` explicitly.
Unsupported JSON fields and commands are errors; missing source data is incomplete.
Additional commands
and GraphQL operations remain implementation work, not silently fabricated data.

Example against a disposable instance or an existing authorized repository:

```sh
tauceti-gh --conf project.toml pr list --json number,title
tauceti-gh --conf project.toml pr view 12 --json title,body,comments
tauceti-gh --conf project.toml pr view 12 -R github.com/OWNER/REPO --json title
```

See `examples/forgejo-preparation.toml` for the schema. The example names do not
assert that repositories already exist on the destination server.

Run the real-server smoke without Docker or admin privileges:

```sh
FORGEJO_BINARY=/path/to/forgejo python tests/integration_forgejo.py
```

It initializes a private temporary SQLite instance, creates a repository, branch,
commit, PR and comment, reads them through the direct client and command wrapper,
creates and edits a code-review comment through the native generic edit endpoint,
and verifies an unauthenticated edit is refused,
and stops the server afterward. SSH, Actions and registration are disabled and
credentials are never printed. Forgejo 17's issue-comment endpoint is unpaginated;
its PR listing endpoint uses pagination. The smoke covers both actual behaviors.

## Remaining ordered work

1. Inventory executable integrations, prompts, generated templates, credentials,
   query/command/JSON-field contracts, and reporting consumers. Inspect production
   pins rather than relying on stale checkouts. Keep sanitized compatibility fixtures.
2. Expand direct forge operations and normalize PR/review/status/error records.
   Add credential selection and provider-specific account validation; preserve
   budget/cost history and qualify caches, outboxes, claims, and local state.
3. Extend Forgejo's native review APIs for replies and conversation
   resolution/reopening. Generic comment editing already works for code comments
   and is covered by the real-server smoke. Use native services and permission checks, update OpenAPI,
   test webhook behavior, and submit focused PRs upstream. Publish a tested fork
   build pinned to an immutable revision.
4. Complete the familiar `gh` command corpus, flags, pagination, exit statuses,
   authentication, repository/fork operations, reviews, issues, statuses, and CI
   navigation. Parse supported GraphQL operations structurally. Move internal
   domain logic onto typed primitives. Keep full unmodified-gh compatibility as
   the separately tracked follow-on.
5. Adapt workers, Review's tokenless and trusted phases, Progress, provenance/link
   parsing, Git credentials, publication recovery, and claims. Test complete
   histories and do not convert imported GitHub approvals into Forgejo authority.
6. Split the queue's source-forge and CI interfaces. Keep its controller, policy,
   repair machinery, and Bors-compatible surface. Normalize verified webhooks;
   persist request identities and reconciliation in the existing durable store.
7. Add versioned build requests/results binding source, accepted heads, base,
   candidate/tree, retained refs, policy/config digest, harness, run, and attempt.
   Retain objects in protected immutable Forgejo refs. Verify fetched objects and
   trusted finalizer provenance; reject stale results. Keep merge credentials out
   of GitHub runners and candidate execution offline in the existing sandbox.
8. Extract portable build procedures. Assign every workflow to GitHub execution
   or a shared trusted command triggered by queue events/timers. Adapt post-merge
   attribution and labels to fully qualified source PRs. Test actual pinned
   production-shaped workflows and only mirror `main`.
9. Separate fleet development/CI identities and runner providers. Retain GitHub
   JIT runners initially. Update installers, canonical templates, caches, docs,
   CI/activity/archive schemas, immutable pins, and historical readers. Preserve
   the dependency-light fleet deployment and prepared-summary reporting paths.
10. Replace every shared-state consumer, including reporting and runner selection.
    Use bounded local caching and targeted reads; collect direct API/load evidence.
    Prepare final archival exports and a shutdown/cutover runbook.
11. Run a disposable Forgejo instance with separate identities, repositories,
    queue data, caches, notifications, and public GitHub mirror. Expose temporary
    HTTPS for hosted runners. Run a real author/review/repair cycle plus deterministic
    fault tests; verify exact merged/mirrored SHAs, post-merge builds, cache/docs,
    archives and progress. Block the shared state endpoint throughout and repeat
    across restarts.
12. Seed production-representative PR/history sizes; test current fleet concurrency
    and a 2x burst. Record latency, requests, server/database load and backlog.
    Require complete results, no sustained growing backlog at normal load, and
    recovery from the burst. Fix client/native API bottlenecks before introducing
    replicated state infrastructure.

## Release and operational rules

Open component PRs against upstream default branches and identify prerequisite PRs.
Use isolated configurations to test new pins. Production forge activation, live
imports, mirroring, and state-service shutdown are separate migration operations.
Fleet updates use the live path with admissions enabled and verify revisions,
useful completions, interrupted work, and recovery afterward.
