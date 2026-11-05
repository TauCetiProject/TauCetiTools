#!/usr/bin/env bash
# repair-cache-get.sh: fetch the repair worker's published outputs for one staging head into
# the local Lake cache, on top of what lake-cache-get.sh restored from the public cache.
#
# Usage: bash scripts/repair-cache-get.sh <project-dir> <staging-head-sha>
#
# The queue's repair worker builds a head on its host (under the same bwrap sandbox as CI), and
# after a green build publishes the outputs under the chain tip's revision to the repair cache
# (`[services.repair_cache]` in tauceti.toml; see lake-cache-services.sh). When the staging
# candidate CI is about to build is exactly that tip, fetching those outputs first lets the
# offline build reuse them: Lake still checks every module's trace against the candidate's
# inputs and recompiles whatever differs, and the audits and lints run on the result as
# before, so the verdict remains CI's. The daily from-scratch build is the independent check.
#
# Anonymous GETs only; no token is in reach and no candidate code runs. The lookup is for the
# exact revision (`--rev`), never an ancestor: a head with no published outputs is the normal
# case, logged and ignored, and the build proceeds from the base cache exactly as without this
# step. A partial fetch must not reach the offline build either (an output record whose
# artifact never arrived is a build failure under --iofail, not a rebuild), so the script keeps
# a copy of the cache's output records from before the fetch and, when every attempt fails,
# puts that copy back. Artifacts Lake verified and installed meanwhile are content-addressed
# and unreferenced by the restored records, so they are harmless.
#
# Reports through $GITHUB_ENV:
#   REPAIR_OUTPUTS_STATE  used | absent | failed | off
#   REPAIR_OUTPUTS_REUSED the number of module output records the repair cache added or
#                         replaced beyond the base cache (0 unless used)
# and one line in $GITHUB_STEP_SUMMARY. Never fails the job for a cache reason.
set -euo pipefail

PROJECT_DIR="${1:?usage: repair-cache-get.sh <project-dir> <staging-head-sha>}"
REV="${2:?usage: repair-cache-get.sh <project-dir> <staging-head-sha>}"
case "$REV" in
  *[!0-9a-f]*) echo "::error::staging head must be a full commit SHA"; exit 1 ;;
esac
[ "${#REV}" = 40 ] || { echo "::error::staging head must be a full commit SHA"; exit 1; }

# shellcheck source=lake-cache-services.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lake-cache-services.sh"

report() {
  local state="$1" reused="$2" line="$3"
  echo "$line"
  {
    echo "REPAIR_OUTPUTS_STATE=$state"
    echo "REPAIR_OUTPUTS_REUSED=$reused"
  } >> "${GITHUB_ENV:-/dev/null}"
  printf -- '- Repair outputs: %s\n' "$line" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
}

if [ -z "$REPAIR_CACHE_URL" ]; then
  report off 0 "no repair cache configured (services.repair_cache), building from the base cache"
  exit 0
fi
if [ "${LAKE_ARTIFACT_CACHE:-}" = "false" ]; then
  report off 0 "the base cache was discarded for this run, so repair outputs for $REV are not fetched"
  exit 0
fi

mkdir -p "$PROJECT_DIR/.lake/cache"
CACHE="$(cd "$PROJECT_DIR/.lake/cache" && pwd)"
OUTPUTS="$CACHE/outputs"
CFG="${RUNNER_TEMP:-/tmp}/lake-repair-cache.toml"
write_lake_cache_config "$CFG" \
  "${PUBLIC_ARTIFACT_ENDPOINT:-https://cache.taucetiproject.org/artifacts}" \
  "${PUBLIC_REVISION_ENDPOINT:-https://cache.taucetiproject.org/revisions}"

BEFORE="$(mktemp -d "${RUNNER_TEMP:-/tmp}/repair-outputs-before.XXXXXX")"
trap 'rm -rf "$BEFORE" 2>/dev/null || true' EXIT
if [ -d "$OUTPUTS" ]; then
  cp -a "$OUTPUTS/." "$BEFORE/"
fi

# Output records that the fetch added or changed, relative to the copy in $BEFORE.
changed_records() {
  [ -d "$OUTPUTS" ] || { echo 0; return 0; }
  local n=0 rel
  while IFS= read -r -d '' rel; do
    rel="${rel#"$OUTPUTS"/}"
    if ! cmp -s "$OUTPUTS/$rel" "$BEFORE/$rel"; then n=$((n + 1)); fi
  done < <(find "$OUTPUTS" -type f -name '*.json' -print0)
  echo "$n"
}

restore_records() {
  local discard="$OUTPUTS.discard.$$"
  if [ -e "$OUTPUTS" ]; then
    mv "$OUTPUTS" "$discard" 2>/dev/null || rm -rf "$OUTPUTS" 2>/dev/null || true
    rm -rf "$discard" 2>/dev/null || true
  fi
  mkdir -p "$OUTPUTS"
  cp -a "$BEFORE/." "$OUTPUTS/"
}

# Same retry policy as lake-cache-get.sh: a deterministic miss stops at once, a transfer failure
# retries, keeping the artifacts Lake already verified.
MISS_RE='no outputs found'
ATTEMPTS=3
outcome=failed
rm -f "${RUNNER_TEMP:-/tmp}"/repair-cache-get-*.log
for attempt in $(seq 1 $ATTEMPTS); do
  LOG="${RUNNER_TEMP:-/tmp}/repair-cache-get-$attempt.log"
  rc=0
  started=$SECONDS
  ( cd "$PROJECT_DIR" && LAKE_CONFIG="$CFG" LAKE_CACHE_DIR="$CACHE" \
      lake cache get --service "$REPAIR_CACHE_SERVICE" --repo TauCetiProject/TauCeti --rev="$REV" ) \
      > "$LOG" 2>&1 || rc=$?
  if ! python3 "$(dirname -- "${BASH_SOURCE[0]}")/ci_log_filter.py" cache "$LOG"; then
    echo '::warning::repair cache log summary failed; showing the original output'
    cat "$LOG" || true
  fi
  echo "Repair cache attempt $attempt: exit=$rc, elapsed=$((SECONDS-started))s"
  if [ "$rc" = 0 ]; then outcome=used; break; fi
  if grep -qE "$MISS_RE" "$LOG"; then outcome=absent; break; fi
  if [ "$attempt" != "$ATTEMPTS" ]; then
    echo "::notice::repair cache attempt $attempt of $ATTEMPTS did not complete cleanly; retrying missing artifacts"
    sleep 2
  fi
done

case "$outcome" in
  used)
    reused="$(changed_records)"
    downloaded="$(cat "${RUNNER_TEMP:-/tmp}"/repair-cache-get-*.log 2>/dev/null | grep -cE 'downloaded artifact [0-9a-f]+' || true)"
    report used "$reused" "reused $reused module outputs from the repair cache for $REV (${downloaded:-0} artifacts downloaded); Lake checks their traces and rebuilds what differs"
    ;;
  absent)
    # A miss writes nothing useful, but put the records back anyway so the build sees exactly
    # what the base cache restored.
    restore_records
    report absent 0 "no repair outputs for $REV, building from the base cache"
    ;;
  *)
    restore_records
    echo "::warning::repair cache fetch for $REV did not complete cleanly; restored the base cache records"
    report failed 0 "repair outputs for $REV could not be fetched completely, building from the base cache"
    ;;
esac
