#!/usr/bin/env bash
# lint-scope.sh OUT_DIR — decide what the environment lint (scripts/lint-env.sh) covers.
#
# Environment lint normally checks only the TauCeti modules a change touches: not the modules
# they import, and not the modules that import them. A change can still break lint elsewhere (a
# new simp lemma can take an existing one out of simp normal form); the daily full lint
# (.github/workflows/lint-full.yml) catches that and opens a repair PR on `lint-repair/main`.
#
# The whole library is linted when any PR in scope carries the `full-lint` label or is a repair PR
# (head branch `lint-repair/...`), or when the PRs in scope cannot be determined.
#
# Inputs (environment):
#   GH_TOKEN, REPO   GitHub API access and the repository
#   EVENT            pull_request_target | workflow_dispatch | merge_group | repository_dispatch | push
#   IS_BATCH         trusted workflow's canonical batch classification; false selects PR scope
#                    for repository_dispatch, otherwise dispatches keep the batch fallback
#   NUM              the PR number (pull_request_target, workflow_dispatch, non-batch dispatch)
#   BASE, HEAD       commit SHAs whose three-dot diff is the change: for a PR its base commit and
#                    the exact head being built, so the scope is bound to that immutable commit
#   SCOPE_REPO       tested candidate checkout, with its .git/objects available locally
#   TESTED_BASE, TESTED_HEAD  optional local B/T endpoints for a prospective integration;
#                    BASE/HEAD remain the immutable raw-head identity
# Output: if scoped, OUT_DIR/modules.txt lists the changed TauCeti modules (possibly none) and
# `LINT_ONLY_MODULES=OUT_DIR/modules.txt` is appended to $GITHUB_ENV; if not,
# `LINT_ONLY_MODULES=` is. OUT_DIR/unmatched.txt lists every changed `.lean` file that is not one of
# those modules (the root file, a name outside the module pattern such as a non-ASCII one): the lint
# can ignore them, but a check that must see every changed declaration (the axiom audit) cannot, and
# falls back to the whole library when this file is non-empty. It reads GitHub branch/label
# policy and immutable local Git objects through trusted tooling; never candidate code or Git configuration.
set -euo pipefail

OUT_DIR="${1:?usage: lint-scope.sh OUT_DIR}"
mkdir -p "$OUT_DIR"
LIST="$OUT_DIR/modules.txt"
UNMATCHED="$OUT_DIR/unmatched.txt"
rm -f "$LIST" "$UNMATCHED"
: "${GITHUB_ENV:?GITHUB_ENV is required}"

full() {
  echo "lint-scope: linting the whole library: $1"
  echo "LINT_ONLY_MODULES=" >> "$GITHUB_ENV"
  exit 0
}

# Human approval dispatches CI for one PR, not a staging batch. Ordinary PR commit
# titles need not contain squash-style (#N) suffixes; requiring those here would
# incorrectly fall back to whole-library lint and report unrelated inherited errors.
if [ "$EVENT" = "repository_dispatch" ] && [ "${IS_BATCH:-}" = "false" ]; then
  EVENT=workflow_dispatch
fi

tool_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
metadata() {
  python3 "$tool_dir/ci_git.py" lint-metadata --checkout "${SCOPE_REPO:-}" --base "$BASE" --head "$HEAD"
}

case "$EVENT" in
  pull_request_target|workflow_dispatch)
    [[ "${NUM:-}" =~ ^[0-9]+$ ]] || full "no PR number"
    [[ "${BASE:-}" =~ ^[0-9a-f]{40}$ ]] || full "no base commit"
    [[ "${HEAD:-}" =~ ^[0-9a-f]{40}$ ]] || full "no head commit"
    prs="$NUM"
    # Read the exact commits already fetched by trusted tooling. A REST quota
    # failure must not turn an available local diff into whole-library lint.
    if ! compare=$(metadata); then
      full "the immutable local commit metadata is unavailable"
    fi
    ;;
  merge_group|repository_dispatch|push)
    [[ "${BASE:-}" =~ ^[0-9a-f]{40}$ && ! "$BASE" =~ ^0+$ ]] || full "no base commit"
    [[ "${HEAD:-}" =~ ^[0-9a-f]{40}$ ]] || full "no head commit"
    if ! compare=$(metadata); then
      full "the immutable local commit metadata is unavailable"
    fi
    # GitHub merge groups and bors staging both use one squash commit per PR,
    # titled `... (#N)`. The trusted workflow validates bors staging and its
    # immutable base/head before calling this script. Unknown titles stay full lint.
    prs=$(jq -r '.commits[].commit.message | split("\n")[0]' <<<"$compare" \
      | sed -nE 's/.*\(#([0-9]+)\)$/\1/p' | sort -u)
    ncommits=$(jq '.commits | length' <<<"$compare")
    [ "$(jq '.total_commits' <<<"$compare")" = "$ncommits" ] || full "the local batch history exceeds the policy limit"
    nprs=$(grep -c . <<<"$prs" || true)
    [ "$nprs" -eq "$ncommits" ] || full "$ncommits commit(s) but $nprs PR number(s) in their titles"
    ;;
  *) full "event $EVENT" ;;
esac

# Only an entirely validated policy response may select the local module scope.
# Pass the attributed numbers on stdin, not through shell evaluation or Git config.
if ! reason=$(python3 "$tool_dir/lint_scope_policy.py" "$REPO" <<<"$prs"); then
  full "${reason:-PR lint policy metadata is unavailable}"
fi

# The merge base and complete tree diff both come from immutable local objects.
# Do not consult live PR files or the size-limited REST compare response.
merge_base=$(jq -r '.merge_base_commit.sha // empty' <<<"$compare")
[[ "$merge_base" =~ ^[0-9a-f]{40}$ ]] || full "no exact merge base"
[ -n "${SCOPE_REPO:-}" ] || full "no local candidate checkout"
# Label/review identity above stays raw H. The local module list follows T vs B
# when the build prepared an integration; deleted/renamed modules then match the
# actual checked-out sources. Other callers keep their existing exact-head contract.
if ! python3 "$tool_dir/lint_scope_files.py" "$SCOPE_REPO" "${TESTED_BASE:-$merge_base}" "${TESTED_HEAD:-$HEAD}" \
    > "$OUT_DIR/files.nul"; then
  full "the immutable local diff is unavailable"
fi

module_re="^TauCeti(/[A-Za-z_][A-Za-z0-9_']*)+\.lean$"
: > "$LIST"
: > "$UNMATCHED"
while IFS= read -r -d '' file; do
  if ! [[ "$file" =~ $module_re ]]; then
    [[ "$file" == *.lean ]] && printf '%s\n' "$file" >> "$UNMATCHED"
    continue
  fi
  module="${file%.lean}"
  printf '%s\n' "${module//\//.}" >> "$LIST"
done < "$OUT_DIR/files.nul"
LC_ALL=C sort -u -o "$LIST" "$LIST"
echo "lint-scope: linting the $(grep -c . "$LIST" || true) changed TauCeti module(s):"
sed 's/^/  /' "$LIST"
echo "LINT_ONLY_MODULES=$LIST" >> "$GITHUB_ENV"
