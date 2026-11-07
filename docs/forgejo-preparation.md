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

Mixed projects also support native GitHub Actions `run` subcommands
`list/view/watch/rerun/cancel/delete/download` and `workflow` subcommands
`list/view/run/enable/disable`, with their
explicit command-specific flags. Without `-R` or `GH_REPO`, these commands select
the trusted `[ci].repository`; a missing CI configuration is an error. Explicit
selectors retain their host and override the configured CI default. CI selectors
must identify a configured GitHub repository (CI, repository role, tool or
upstream); an unconfigured GitHub twin of a Forgejo repository is rejected.
Registry membership permits routing; native GitHub authorization and queue
admission remain independent requirements.
A Forgejo selector is rejected, and a bare name shared by Forgejo and GitHub remains
ambiguous. Callers must qualify those names, for example
`-R github.com/TauCetiProject/TauCeti`. No source repository is automatically
rewritten to its mirror. Native gh receives the original arguments, stdin and
file references, plus the selected `GH_HOST` and qualified `GH_REPO`; output and
exit status pass through. `workflow run --json` reads stdin in native gh, while
`run view --json FIELDS` consumes a field list. Ref, field, template and artifact
path values cannot select a server. The wrapper does not open Forgejo credentials
for these commands. The existing Enterprise credential-origin guard still applies.
Unknown commands and flags fail before delegation in mixed projects.
The CLI replaces its process with native gh, preserving the caller's PID,
timeout ownership and native signal/exit behavior. In-process Python `run()`
callers retain a subprocess and its return code. Python's ignored pipe/file-size
signals are restored before exec. An interrupt in the wrapper terminates by
SIGINT without a Python traceback and reports that any in-flight write needs
reconciliation. Flag arities were checked against gh 2.103.3; native
CLI versions and option arities must be verified when preparing integrated
installer pins. A worker inheriting a Forgejo `GH_REPO` must supply an explicit
configured GitHub selector for CI commands.

`tests/fixtures/forge_ci_commands.json` records four accepted CI-routing cases
with immutable Worker/Tools source provenance and synthetic values. Three cases
check content-CI host qualification. The progress retry targets Roadmap and is
refused because its GitHub execution identity is not configured; adding a host
prefix is insufficient. Their source consumers are not yet migrated: legacy
configuration accessors still reject mixed projects, and callers need qualified
source and CI identities. The process-boundary regression uses a local native-CLI stand-in
to verify stdin, argument, output and exit-status forwarding. Neither check
demonstrates a hosted CI run or candidate/run/attempt provenance. The queue must
still implement and verify that provenance before Forgejo activation.

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
closed or reopened. Remaining REST paths/methods, structural GraphQL translation, checkout/clone operations,
remaining command flags, trusted outbox integration and worker activation remain
required preparation work. Token-output commands require explicit credential
access and must not be admitted as ordinary tokenless agent reads.
The REST read adapter supports `gh api user`, `users/LOGIN`, repository metadata
and collaborator permissions/lists, repository labels, exact and matching Git
refs, repository commits by ref, Git commits by full SHA, blobs, file contents
and complete Git trees. Use `--hostname` or
`GH_HOST` when an endpoint's repository exists on multiple configured forges.
Endpoint names select the repository; explicit hostnames remain authoritative.
`GH_REPO` provides `{owner}`/`{repo}` placeholders and keeps its qualified host.
Non-repository endpoints default to the project content forge; use an explicit
host for GitHub account or GraphQL operations. GitHub delegation preserves the
caller's `GH_REPO` for placeholders in fields. Mixed projects require relative
API endpoints; absolute endpoint URLs are rejected before delegation or credentials.

Read contracts support `--method GET`, simple query fields, `--jq`, `--silent`,
and source HTTP status/cache-header output for single resources. Lists support page and
per_page plus `--paginate`/`--slurp` (`--slurp` cannot be combined with `--jq`), preserving logical page boundaries despite
smaller native server pages. Single-page reads stop after their requested prefix.
Commit/tree identities use the hash-verified raw Git object bytes available
through Forgejo 17's native object reader. Commit headers supply the exact tree
SHA, including legacy noncanonical trees; native commit DTOs label it
with the commit SHA. A server that does not expose these bytes raises an explicit
incomplete-data error. Complete trees resolve the immutable tree SHA, then join bounded native
pages only while that SHA and total count remain stable. As in GitHub, the
presence of `recursive` enables recursion, including values `0` and `false`,
following the [GitHub tree API contract](https://docs.github.com/en/rest/git/trees). Collections and trees enforce aggregate byte and page bounds;
incomplete observations raise errors. JSON preserves native numeric IDs and adds
origin-qualified opaque `node_id` identities. It does not invent account types,
GitHub API quotas, ETags, or approval authority.

File/blob content is checked against its byte size and Git blob hash in JSON
and raw mode; omitted large content raises an incomplete-data error. Raw Accept
headers decode the verified content as UTF-8 text. File-based fields,
request bodies, collection HTTP headers, conditional requests, binary raw output,
REST mutations, other REST paths and structural GraphQL translation still require
adapters. Unsupported Forgejo requests fail before opening the credential file.
GitHub requests use their original argv and normal CLI implementation. No global
`gh` replacement or production configuration is installed by these changes.

Source discussion contracts additionally support `gh api repos/OWNER/REPO/pulls/N`,
`issues/N`, `issues/N/comments` and `pulls/N/reviews`. Closed PR reads verify their
retained head, and zero-stat reads reuse the bound diff reconstruction before
exposing head/diff metadata;
a stock server that describes a reused closed branch is unavailable. Deleted-fork
historical data is usable only when those same checks succeed. This is a whole
REST-record contract: even `.title` projection is unavailable when its required
head/diff proof fails. AGit PRs explicitly require a separate retained-head/diff
adapter; a lagging retained ref or missing zero-stat proof is unavailable too.
Imported merged history missing its merge timestamp or commit is unavailable as
a whole REST record. AGit raises a data-dependent `Unsupported` after reading
the PR; unsupported request forms still fail before credentials or I/O.
Stable-field-only callers can instead use the familiar `pr view --json FIELDS`
contract. `head.ref` is the source branch name from native `label`; the native
retained-ref name is preserved as `head.forgejo_ref`. `head.label` retains its
native bare branch name, rather than guessing GitHub's `owner:branch` form when
fork metadata is absent. `base.sha` remains
the live base tip, not an inferred historical merge base. Issue reads require an
issue. Forgejo's false `mergeable` becomes null (unknown), since false also covers
checking, errors and drafts. Positive native mergeability remains a true
observation; it does not prove current merge eligibility. The
issue route does not alias PR metadata. REST preserves native numeric
IDs and adds the same origin-qualified opaque `node_id` namespaces as familiar
commands, including actors, labels, milestones and branch repositories. Native
null label/assignee/requested-reviewer slices normalize to empty arrays. Plain
issues omit the `pull_request` key. Required identity/lifecycle fields must be
present. The merged timestamp must agree with the native merged flag, and merged
PRs must identify a merged commit. `merged_by` and `closed_at` remain native
observations; their presence is not inferred from lifecycle state.

Forgejo17 issue comments have one unpaginated, byte-bounded native response. The
wrapper slices it into requested `page`/`per_page` output rather than repeatedly
fetching the same history. Reviews exhaust counted native pages across hidden
drafts before applying synthetic pages. A changing native total count aborts
the traversal; listings remain observations, not atomic snapshots during
concurrent edits (including same-count removals and insertions). Other actors' pending reviews and
reviewer-request placeholders are excluded, including when using a site-admin
credential. Review state maps to GitHub REST spellings, pending `submitted_at`
is null, and an own web-started draft may have an empty URL before its summary
comment exists. Native `official`, `stale` and `dismissed` remain present. These flags
are observations, not grants of approval authority. Discussion URLs must bind
the configured origin, repository and issue/PR number; duplicate IDs and missing
metadata fail explicitly. Collections enforce aggregate byte and page bounds.
No `author_association` or account type is invented. Requested consumers that
need those fields still require an explicit policy adaptation. `--include`,
date filters, inline-thread comments, REST writes and other source paths remain
unsupported by these contracts.

`tests/fixtures/forge_source_rest_commands.json` pins four request/field cases to
Worker, Review and Progress source blobs, with synthetic argument values. The
tests check accepted plans and normalized JSON before optional jq rendering;
they do not prove complete consumer migration. Worker GraphQL and shared-state
reads, Review comparison/eligibility, qualified claims/outboxes, caches and
activation remain preparation work. The real-server fixture covers visible
reviews across hidden pages, own/foreign drafts, comment pages, REST metadata,
and historical heads with deleted/reused branches in stock/patched profiles.

Unsupported JSON fields and commands are errors; missing source data is incomplete.
Additional commands
and GraphQL operations remain implementation work, not silently fabricated data.
Explicit historical PR diff/head reads require the native head SHA to match
the retained pull ref. Default closed-PR output omits these unverified fields
and requires no retained-ref lookup. `baseRefOid` identifies the live base branch,
not a historical comparison base. A deleted source fork is allowed only
when the remaining native diff metadata is complete; requested files still need
immutable head binding and a stable metadata re-read. Closed, unmerged file reads and every zero-stat reconstruction require every native
file page to identify `X-Forgejo-Diff-Base` and `X-Forgejo-Diff-Head`, matching the
stored comparison base and retained head, plus `X-Forgejo-Diff-Format: 1`.
Format 1 requires rename-preserving pagination and accurate per-file line totals;
base/head headers alone are insufficient proof for reconstructing zero scalars.
Stock Forgejo has no such headers and
can partially or entirely erase a historical proposal by comparing it with today's
base. Such observations are unavailable. Stock pure-rename pages also disagree
with native line statistics; those reads are incomplete until the server preserves
rename source paths during pagination. Nonzero native file listings must also
match both the file count and the total additions/deletions.

The real-server fixture defaults to stock behavior. Set
`FORGEJO_RETAINED_METADATA=1` only when explicitly testing a binary with the native
historical-metadata fixes. This profile asserts accurate scalar stats and files
after source-fork deletion, retained heads after source-branch reuse, and a closed
proposal whose changes reached main through a superseding PR, plus pure-rename
file metadata. The stock profile
asserts errors for unavailable history. This test switch does not change runtime
selection or activate a server capability in production.

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
statistics are unavailable for AGit and responses without a verified immutable
head and complete comparison metadata. A deleted source repository alone does
not invalidate history retained by the base repository.
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

## Qualified CI source histories

### Qualified candidate execution

The opt-in `tauceti-source-candidate-v2` event executes a retained Forgejo
candidate in the trusted GitHub `pr-build.yml` workflow. The project caller must
set `source_candidate_enabled`; generated callers obtain it from
`FORGEJO_SOURCE_CI_ENABLED`. The default is false. Receiving a source event never
selects the legacy GitHub PR path, including when the opt-in check fails.

The execution repository's immutable workflow checkout contains `tauceti.toml`
and `.tauceti/ci-source.json`. The latter uses this policy shape:

```json
{
  "schema": "tauceti-ci.source-candidate/v2",
  "source": {"forge": {"kind": "forgejo", "url": "https://git.example.org"}, "name": "team/Math", "id": 41},
  "execution": {"forge": {"kind": "github", "url": "https://github.com"}, "name": "team/Execution", "id": 42},
  "credential_origin": {"kind": "forgejo", "url": "https://git.example.org"},
  "workflow": ".github/workflows/pr-build.yml",
  "harness": {"forge": {"kind": "github", "url": "https://github.com"}, "name": "team/Tools", "id": 43},
  "harness_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "base_branch": "main",
  "execution_branch": "main",
  "require_publication": true
}
```

The example IDs and revision must be replaced with independently verified
deployment values. The policy digest includes the SHA-256 of the exact trusted
`tauceti.toml` bytes. The queue must independently pin that policy, configuration,
caller workflow SHA and shared harness SHA before sending a request. A policy
file cannot contain its own enclosing commit SHA; the request carries the caller
SHA and CI compares it with the actual GitHub workflow context. The workflow
also compares its tooling inputs with the actual reusable workflow identity
before checking out executable tooling. GitHub.com's `job.workflow_sha`,
`job.workflow_repository` and `job.workflow_file_path` identify the reusable
workflow; the `github` context identifies its caller. These properties are not
available on GitHub Enterprise Server. See the [GitHub context reference](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#job-context).

Requests bind a 128-bit queue epoch and request ID, native source and execution
repository IDs, batch version, exact base, candidate commit/tree and accepted
member heads. Inline transport bounds the request to 60 KiB and 128 members.
Larger batches use a retained member manifest, leaving the request and result
small without truncating or splitting membership. The manifest bounds are 8,192
members and 1 MiB of canonical JSON. These are transport limits; they do not
establish queue or fleet capacity.
Retained branch names are derived from the epoch and request ID:
`refs/heads/tauceti-ci/EPOCH/REQUEST/candidate` and, for inline requests,
`member-PR`. The queue must protect these refs from subsequent mutation. CI
fetches retained sources, verifies each SHA and candidate tree, and checks that the base is
the candidate's merge base. It rechecks the native repository ID before and after
source transfers. Complete histories and sandbox materialization use the existing
qualified Git helpers. The native read token is provided only to explicit host
read steps before dependency executables run; it is never a candidate sandbox
variable or merge credential. Native branch/label policy is saved with the
request digest in runner temporary storage. The later lint step reads that
observation and local Git objects without native credentials or network access.

For retained membership, the request's `members` value is
`{"manifest":{"commit":"…","sha256":"…","count":1197,"size":123456}}`.
The commit must be retained at the derived `members` branch under the same
epoch/request namespace. Its tree contains exactly one ordinary `100644` file,
`accepted-members.json`. The canonical data binds its schema
`tauceti-ci.accepted-members/v1`, epoch, request ID and exact ordered list of
`{"pr":NUMBER,"head":SHA}` members. Its parents must be exactly the accepted
heads, deduplicated in first-occurrence order. Those parent links retain all
source histories through one manifest branch, avoiding a push and a fetch
argument for every member. The queue must durably record and protect the
manifest and candidate refs before dispatch; manifested requests do not use
per-member branches.

Resolve verifies the native repository ID and retained manifest commit, reads
the Git tree locally, and checks the bounded native blob response against its
Git blob hash, SHA-256, byte size, count and request identity. It saves those exact
bytes in runner temporary storage before resolving candidate/member refs. Fetch
and member policy verify the saved bytes again; missing or changed membership
fails closed. Membership-dependent operations cannot use an unresolved manifest
as an empty list. The publisher and finalizer use the immutable descriptor and
need no extra source access. Hydrating members leaves the wire request, request
digest and result identity unchanged.

Both source transfers verify the manifest commit and exact parent list. The
complete-history store receives a commit graph before per-member checks. Member
policy has a five-minute step limit; large batches select full-library lint
without making a PR metadata request per member. Manifest output is created
exclusively, and every later read checks its bytes again. A missing or corrupt
queue-owned manifest is infrastructure evidence, not an author-policy rejection;
the consumer must stop at its recovery bound and reconcile the retained request.
It must never retry the same broken preparation indefinitely or replace its
manifest silently.

Source bootstrap materializes portable build scripts without exporting legacy
unqualified source bindings. Native PR branch/label reads select lint scope;
unavailable, changed-head or full-lint observations select full-library lint.
The GitHub helper permits only configured execution/dependency reads and refuses
source metadata or status writes before credential selection. Candidate audits,
scope and pin guards continue to run. Exact retained members receive the same
per-PR line/file/new-Lean-file caps as GitHub PRs, using local complete histories
and each member's merge base. A size rejection stops the build after saving its
evidence. Binary files retain GitHub's zero line count and still count as files;
unrelated or ambiguous member history is a policy rejection. Pin changes retain the existing line-cap exemption;
the trusted bump guard must still validate them. Missing completion markers
produce infrastructure verdicts rather than policy failures. Native restricted-path approval is not yet
integrated; a legacy Bors waiver cannot authorize a Forgejo candidate.
Source bump checks use the configured GitHub dependency guard with an explicit
GitHub host. Lookup or trusted resolver failures remain infrastructure; decided
pin-policy failures remain rejections. Late lint/report steps receive neither
the native token nor a GitHub API token in source mode.

The isolated trusted finalizer produces `source-candidate-result.json` in an
artifact named for the request, run and attempt. It binds all four verdicts
(build, scope, bump guard and publication) to the request and tested commit/tree.
Missing tested identity produces an infrastructure result. A required publication
cannot be skipped by a build output: the finalizer checks this attempt's unique
publisher job, separate hosted runner and successful exact-map confirmation step.
The publisher reads only the toolchain pin, bound through the verified commit,
root tree, ordinary blob entry and blob hash; it never checks out candidate code.

`GitHubEvidence.result()` checks fresh authenticated run and artifact metadata,
execution repository ID, expected workflow/caller SHA, pinned reusable harness,
latest attempt, artifact name, archive digest and its single bounded JSON entry.
It checks the run again after downloading. GitHub credentials go only to the API;
the signed artifact storage redirect receives no authorization header. Run titles
and commit statuses do not grant a verdict. Result verification is execution
evidence, not source approval or merge authority. Metadata fields follow the
[workflow run API](https://docs.github.com/en/rest/actions/workflow-runs) and
[artifact API](https://docs.github.com/en/rest/actions/artifacts).

`classify_verdicts()` gives decided scope or bump failures precedence over an
unrelated infrastructure failure, followed by source build failure, infrastructure
and a passed result. A skipped build cannot hide a policy rejection. The queue
must route rejected source work to review/repair; it must not repeatedly dispatch
the same rejected candidate. A completed run without valid result evidence uses
bounded infrastructure recovery rather than indefinite polling of that attempt.

The queue's qualified request producer, durable payload/configuration binding,
native approval/retention adapter and result-consumer wiring remain required.
This component is not a hosted end-to-end rehearsal, capacity proof or production
activation. Source migration, main mirroring and service retirement are separate
operations.

Cache reads, probes and publication use the explicit GitHub execution-repository
cache scope (`TAUCETI_CACHE_REPOSITORY`), separate from source repository identity.
The queue composes candidates by squashing members, so raw accepted member heads
need not be candidate ancestors. CI verifies their exact retained objects; the
queue is responsible for the composition and fresh source approval assertion.
Use a full workflow rerun when retrying publication, so build and publisher
evidence belong to the same run attempt. Required publication with missing cache
configuration remains an infrastructure failure.

The general project generator still refuses native source bindings. The execution
repository must currently be prepared with the explicit caller, policy and project
configuration described above; native installer/generator integration is follow-up
work. No source build activation follows from updating the caller template alone.

The trusted `ci_git.resolve()` and `ci_checkout.fetch_sources()` helpers accept
explicit `RepositoryRef` inputs for source and head repositories. Both must use
the same forge. Legacy string inputs keep their fixed GitHub meaning and schema-1
checkout identities. Qualified fetches write schema-2 identities with embedded
forge/repository records; materialization preserves that source in credential-free
remotes. Immutable head/base/merge-base verification, complete histories, fresh
Git metadata, candidate-config isolation and read-only object mounts stay in place.

When a token is supplied for a qualified source, the caller must independently
supply its trusted `credential_origin` as a `Forge`. A request-controlled source
cannot rebind that credential. Git receives only a source-origin-scoped header in
a clean child environment; qualified transfers refuse redirects. Neither the
identity file, persistent Git configuration nor command arguments contain the
token. A different source/head forge, mixed legacy/qualified identities or a
credential-origin mismatch fails before creating a store or starting Git.

Both helper CLIs opt in with `--source-kind`, `--source-origin`, and, for
authenticated reads, `--credential-origin`. This mode reads only
`CI_SOURCE_GIT_TOKEN`, never the execution credentials `CI_GIT_TOKEN` or `GH_TOKEN`.
Qualified operations require `tauceti_tools.forge` from the same pinned Tools
package. The current reusable workflow exposes its trusted `.tauceti-tools`
checkout through `PYTHONPATH` before invoking these helpers; standalone consumers
must provide that dependency too. Never import it from candidate content.
The workflow must obtain the credential origin from trusted configuration, not
copy it from the dispatched request. Source arguments and these helpers do not
validate queue admission, dispatch authorization or execution results.
Existing workflows keep their legacy behavior until their trusted configuration,
request validation and explicit source arguments are integrated.

The real-server fixture fetches a private Forgejo repository using a read-only
source token. It resolves a retained candidate, advances main, fetches the exact
previous head/base histories and materializes isolated checkouts. The loopback
redirect test verifies a second origin receives no request or credential. These
are checkout proofs; the hosted HTTPS, real GitHub CI and complete queue cycle
remain required.
