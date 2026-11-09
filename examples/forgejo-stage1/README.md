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
