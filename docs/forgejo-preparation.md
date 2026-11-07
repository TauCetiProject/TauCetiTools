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
`[ci].repository` selects build execution; `[repositories].ci` remains the CI
archive. Their accessors are `ci_repository_ref()` and `repository_ref("ci")`.

New clients use `project.repository_ref(role)` and `project.ci_repository_ref()`.
Legacy owner/name/environment access rejects non-GitHub bindings, preventing an
unfinished adapter from acting on the GitHub mirror. This guard must be removed
only as each caller obtains full qualified-reference support.

The experimental `tauceti-gh` executable supports PR and issue list/view,
create/edit/comment/close/reopen, PR reviews, repository view/list/fork, label
list/create/edit, and account status/token commands. Read commands support JSON
projection and `--jq`. Lists support state, limit, author (including `@me`),
and repeated/comma-separated labels. PR lists also support base/head branches.
It is not installed over the operator's `gh` and is not yet wired into workers.
Forgejo/mixed-project use requires a leading `--conf PATH` or trusted `TAUCETI_CONF`;
the wrapper never discovers configuration from the current checkout. Inherited
worker snapshots are reused rather than fetching mutable online configuration.
All-GitHub configurations delegate native arguments unchanged (apart from the
leading wrapper option). Mixed-project delegation is limited to the implemented
commands and flags; other subcommands fail before routing or
credential selection. Full command coverage remains preparation work.
Forgejo authentication uses a private `TAUCETI_FORGEJO_TOKEN_FILE` and requires
`TAUCETI_FORGEJO_ORIGIN` to match the normalized destination origin. Set both in
trusted process configuration outside the checkout. Public reads can omit them.
Ambient tokens for GitHub Enterprise destinations similarly require a matching
`TAUCETI_GITHUB_ORIGIN`. Configured destinations cannot redirect a credential to
another origin. Without `-R`/`GH_REPO`, all-GitHub projects retain native gh checkout
detection; mixed projects require a checkout origin matching exactly one configured
repository. Otherwise specify `-R HOST/OWNER/REPO` explicitly.
Qualified repositories on an already configured Forgejo origin may be selected
without adding each fork to the project registry. Bare names matching multiple
configured forges are ambiguous. Unconfigured bare names are rejected; qualify
the host explicitly. The registry includes CI, tool and upstream repositories. Explicit auth hostnames take precedence over
repository environment variables; account commands never infer a server from
`GH_REPO`.

The command parser follows each command's option arity before routing. String
payloads such as `--body '-R'` or `--body '--repo=literal'` remain payload text.
All-GitHub projects retain native argv unchanged. Unsupported flags fail without
falling through to GitHub. File/stdin bodies are read only on the selected Forgejo
path; they are not materialized when delegating to native GitHub.

`ForgejoWriter` authenticates the selected account before mutations. Trusted
automation can require its stable native ID using `TAUCETI_FORGEJO_USER_ID`.
Label names and edit inputs are resolved before writing. Label additions/removals
use native additive operations rather than replacing another actor's label set.
Forgejo scoped labels can replace another label in the same scope. Label edits
run before title/body edits, enforcing native issue/PR write authority before
applying fields that an issue author can edit. Base-branch existence and nonempty
titles are checked before writing; other server field-validation failures after
label changes are reported as ambiguous. Repository labels work without
organization read scope; inherited label lookup requires that scope explicitly. Duplicate names are rejected on creation/rename, and
ambiguous requested names fail explicitly.
If a later step fails after an earlier write, the entire command reports an
ambiguous outcome requiring reconciliation; no mutation is retried automatically.
Consumers identify ambiguous writes by their stderr prefix (delegated tools may
reuse the same exit status). The CLI returns status 2 with the stable `gh: ambiguous-write:` prefix for these
outcomes. Partial commands identify completed request paths; verified creation
URLs are retained in draft-verification errors.
Reviews bind to a synchronized retained PR head and submit that exact commit ID.
An existing pending review must be submitted/discarded explicitly to avoid
publishing unrelated draft comments. Output reports native official/stale state;
a successful review submission does not grant approval authority.

Noninteractive creation needs a title, and PR creation needs an explicit head.
`--draft` uses Forgejo 17's native `WIP:` title convention and verifies the returned
draft state. A server configured with incompatible WIP prefixes produces an
ambiguous creation outcome to reconcile, rather than a claimed draft success.
Title edits preserve draft state for the stock prefixes; unknown custom prefixes
are rejected. Non-draft creation with a stock WIP title is rejected. Both draft
and ready creation verify the returned state.
Fork creation currently requires `--clone=false` and does not alter Git remotes.
An existing destination is reused only after verifying its native parent.
Already-closed/reopened lifecycle commands are no-ops, and merged PRs cannot be
closed or reopened. Raw `gh api`, structural GraphQL translation, checkout/clone operations,
remaining command flags, trusted outbox integration and worker activation remain
required preparation work. Token-output commands require explicit credential
access and must not be admitted as ordinary tokenless agent reads.
Unsupported JSON fields and commands are errors; missing source data is incomplete.
Additional commands
and GraphQL operations remain implementation work, not silently fabricated data.
Native lists preserve creation-descending server order, remove duplicate IDs,
filter before applying limits, and stop once the requested prefix is found.
They do not assume that imported issue numbers follow creation timestamps.
Repeated labels require every named label; native issue filtering uses OR and
discards unknown labels, so the client applies this predicate itself. Sparse
matches may still require more pages and raise on exhausting the page bound.
Full-collection callers omit the limit and must exhaust pagination. Creation
ordering does not give snapshot isolation: a row leaving a filter during a scan
can shift later rows across page boundaries and cause an omission. Consumers must
reconcile subsequent observations; this is not an atomic queue-admission snapshot.
Review pagination uses the server's page cap and native total count because
visibility filtering can produce empty intermediate pages containing hidden drafts.

`ForgejoReader` provides the same explicit read contracts to internal consumers.
PR JSON fields include source/base/head metadata, comments, historical reviews,
changed files and latest commit status per context. Expensive histories are only
fetched when requested. A file listing must match the native changed-file count;
each file's contents URL must identify the immutable head, and head/base are
rechecked afterward. Status requests bind to the immutable head ID. Native diff
statistics are unavailable for AGit and missing-head-repository responses, whose
zero-value stats do not prove an empty change set.
Forgejo also serializes failed diff calculations as all-zero stats for some
deleted fork branches and unsynchronized fork pushes. Those zeros are not exposed
directly: the reader verifies the retained pull ref, reads its complete file list,
and reconstructs the counts. A genuinely empty diff remains a verified zero;
mismatched refs or unavailable file histories raise `Incomplete`.
Closed PRs whose branch has subsequently moved also reject scalar statistics
instead of describing a newer branch tip. Exact historical diff/status metadata
for reused closed branches remains a required native API/adapter contract before
the reporting consumers or workers can activate Forgejo bindings.
Forgejo's false mergeability also covers checking/error/draft, so it maps to
`UNKNOWN`, never a proven conflict. Historical approvals do not imply a current
`reviewDecision`; that field remains unsupported until native approval policy is
integrated. Reviewer requests are distinct from submitted reviews. Dismissed
reviews retain their dismissal state. IDs are opaque, case-normalized,
forge-qualified strings based on global table IDs, independent of repository
names. Ghost, imported replacement, and Actions system actors receive no
authenticatable user ID. Actions' fixed system actor is identified as a bot;
other missing bot/type information is not invented. Only the viewer's own pending
reviews are exposed, including when a native admin token can see more drafts.
Unrequested optional metadata is not validated or fetched.

Repository reads expose native effective permissions and the default branch.
The direct client's `current_user()` validates the authenticated account response;
ambient GitHub credentials never authenticate a Forgejo request. These contracts
remain observations, not atomic multi-endpoint snapshots. Queue admission still
requires fresh exact-candidate validation when that consumer is integrated.

Example from a checkout whose origin matches the configured content repository
(otherwise add `-R HOST/OWNER/REPO`):

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
credentials are never printed. It also exercises separate operator/reviewer
accounts, reviewer requests and approvals, superseded commit statuses, issue
label/author/branch filtering, repository permissions, CLI reads, merged/closed
PRs with deleted branches, ghost actors, draft visibility and a 65-file PR. The
server caps pages at one item, exercising empty intermediate review pages and
server caps below the client's requested limit. Forgejo 17's
issue-comment and review-comment endpoints are unpaginated; PR/file listings use
pagination. File listings end in JSON `null`, unlike other collection endpoints.
Only that known endpoint accepts a null terminal page; empty HTTP read bodies
remain errors. The integration checks these actual behaviors.

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
