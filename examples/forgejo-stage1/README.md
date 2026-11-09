# Stage 1 protected GitHub callers

Copy these two workflows into the protected GitHub execution repository alongside
its trusted `tauceti.toml` and `.tauceti/ci-source.json`. They connect the
human-operated Forgejo candidate path to the existing reusable sandboxed CI.
They do not install an autonomous queue or mirror source branches before testing.

`pr-build.yml` runs only on the source-candidate repository-dispatch event. It pins
the reusable workflow and Tools checkout to the same immutable revision and uses
the protected caller SHA for project policy. Its explicit `id-token: write`
permission is required by the nested reusable workflow. Candidate execution never
receives source merge credentials or the cache publisher secret.

`stage1-dispatch.yml` accepts the frozen request JSON produced by Worker's Stage 1
operator. It validates source/execution/policy identities and the exact caller SHA,
retains an intent artifact, and posts once with the execution repository's native
job token. This is useful when the external dispatch App is unavailable. Submit
one parent workflow per request. New manual invocations are distinct sends; the
operator ledger and run receipt verifier select a single owning child but cannot
make two separate parent invocations into one HTTP request.

The same parent can be rerun only if every earlier attempt's completed jobs
metadata proves its send step was skipped. A successful, failed, cancelled,
missing, or incompletely observed send refuses retransmission. Observe an uncertain
original instead. Retrying a known failed child is separate: result verification
requires the child's current run attempt and exact tested candidate.

Set public cache URL repository variables to match the trusted configuration.
Required publication additionally needs upload endpoints and `LAKE_CACHE_KEY`
passed only to the isolated reusable publisher. Do not set `require_publication`
to false to work around a requirement for cache publication.

The immutable Tools pin included here is an existing merged source-candidate
implementation. Update both copies together when adopting a newer reviewed
release. Build this protected caller first, then provision the operator profile
with its actual commit SHA. Updating the caller makes old frozen requests stale;
prepare a new request rather than editing its recorded workflow revision.

The operator setup, review, promotion, and recovery commands are documented in
`TauCetiWorker/docs/forgejo-stage1-operator.md`. Native branch protection, the main
push mirror, post-merge cache/docs workflows, and tested backups/rollback remain
required before production cutover; installing these callers alone is insufficient.

## Post-merge builds, cache, and docs

The shared setup action has an explicit `source-mirror-enabled: true` mode for
trusted main jobs. It verifies the protected execution repository ID, branch,
workflow and checkout commit, immutable harness pin, source repository ID, and
that exact commit's ancestry on protected source main on the configured Forgejo
server. An older mirrored main is accepted when source main has advanced; an
unmerged source commit is refused. Source reads use
the configured public origin. Unported source selectors remain invalid; the Lake
cache namespace is explicitly the GitHub execution repository. Shared state is
disabled for this path.

Render the existing canonical CI, API-docs, and Pages workflows into a local
review checkout using a policy whose harness revision contains this support.
This command requires the flagship `web/` and `docbuild/` layout, `[features]
docs = true`, and matching source/execution branch names. It refuses other project
layouts rather than installing workflows for absent projects:

```sh
python -m tauceti_tools.ci_mirror_workflow generate \
  --configuration /absolute/path/tauceti.toml \
  --policy /absolute/path/ci-source.json \
  --destination /absolute/path/review-checkout
```

This writes those three local workflow files. Inspect the generated diff and
project-specific site settings before committing/deploying them. The generator
uses the existing main build/audits/docs templates and secret-isolated reusable
publisher; it requires cache publication on this native path. It does not
install source-writing maintenance jobs or transfer live authority.
Missing upload or public endpoint configuration fails the main build; it cannot
turn required publication into a successful skipped job.
The generated Pages workflow disables its legacy GitHub `docgen` branch writer;
read the published `docs/SOURCE_SHA` for the documentation revision.

Each qualified setup uploads a `source-mirror-context-RUN-ATTEMPT-JOB` artifact
with source/execution identities, commit, configuration/policy digests, and harness
pin and captured source-main tip. This is diagnostic build context with seven-day
artifact retention, not durable publication attestation or a passing merge verdict.
The cache's exact revision map and published docs' `SOURCE_SHA` identify their
outputs. Save the context artifacts with the cutover evidence before they expire.
Candidate execution continues to use the existing versioned request/result
contract in `pr-build.yml`.

Qualified mirror setup always selects full library lint without a GitHub PR
lookup, including native default squash subjects without a trailer. Other callers
retain qualified `Source-PR` citations from local Git metadata: native, foreign,
ambiguous, or conflicting citations select full library lint before any GitHub PR
lookup. Historical local GitHub/Bors subjects and matching local GitHub citations
keep their existing scope policy. Native post-merge scope optimization is deferred.

The site may retain GitHub history charts as explicitly historical data during
Stage 1. Native analytics and remaining development automations belong to Stage
2; classify their temporary replacements in the cutover package. A generated
workflow is not proof of a working publication: the isolated rehearsal must
verify real builds, cache upload/download, docs provenance and usable HTTP output.
