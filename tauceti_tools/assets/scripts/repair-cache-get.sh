#!/usr/bin/env bash
# repair-cache-get.sh: fetch the repair worker's published outputs for a staging candidate into
# the local Lake cache, on top of what lake-cache-get.sh restored from the public cache.
#
# Usage: bash scripts/repair-cache-get.sh <project-dir> [<rev> ...]
#        REPAIR_OUTPUTS_REVS="<rev> <rev> ..." bash scripts/repair-cache-get.sh <project-dir> [<rev> ...]
#
# The candidate revisions are the positional ones followed by the space-separated entries of
# $REPAIR_OUTPUTS_REVS, in that order, each a full commit SHA, duplicates dropped. At least one
# is required. A positional revision that is not a full SHA is a usage error; an entry of
# $REPAIR_OUTPUTS_REVS that is not one is skipped with a warning, since it comes from a dispatch
# payload rather than from the workflow.
#
# The queue's repair worker builds a head on its host (under the same bwrap sandbox as CI), and
# after a green build publishes the outputs under the chain tip's revision to the repair cache
# (`[services.repair_cache]` in tauceti.toml; see lake-cache-services.sh). Fetching those
# outputs first lets the offline build reuse them: Lake still checks every module's trace
# against the candidate's inputs and recompiles whatever differs, and the audits and lints run
# on the result as before, so the verdict remains CI's. The daily from-scratch build is the
# independent check. Lake reuses an output by its content trace, never by commit, so the outputs
# published for a different commit with nearly the same tree (the same head replayed onto a
# newer main, an earlier tip of the batch, main's own tip) serve as well as an exact match; a
# superset of output records is safe for the same reason.
#
# Anonymous GETs only; no token is in reach and no candidate code runs. Each lookup is for one
# exact revision (`--rev`), never an ancestor: a revision with no published outputs is the
# normal case, logged and ignored. The revisions are fetched in the order given. Artifacts are
# content-addressed and Lake downloads only those not already in the cache, so an artifact
# shared by several mappings is downloaded once; an output record placed by an earlier
# revision's mapping is kept when a later mapping has one for the same input. A partial fetch
# must not reach the offline build (an output record whose artifact never arrived is a build
# failure under --iofail, not a rebuild), so before each revision the script keeps a copy of
# the cache's output records and, when every attempt for that revision fails, puts that copy
# back and moves on to the next revision. When no revision contributes, the records are exactly
# those the base cache restored. Artifacts Lake verified and installed meanwhile are
# content-addressed and unreferenced by the restored records, so they are harmless.
#
# When lake-cache-get.sh discarded the base cache (LAKE_ARTIFACT_CACHE=false), the fetch starts
# from an empty cache and, if any revision contributes, appends LAKE_ARTIFACT_CACHE=true,
# LAKE_RESTORE_ARTIFACTS=true and LAKE_CACHE_DIR to $GITHUB_ENV so the build reads them.
#
# Reports through $GITHUB_ENV:
#   REPAIR_OUTPUTS_STATE     used | absent | failed | off
#   REPAIR_OUTPUTS_REUSED    the number of module output records the repair cache added or
#                            replaced beyond the base cache (0 unless used)
#   REPAIR_OUTPUTS_REVS_USED the space-separated revisions whose outputs were fetched (only
#                            when used)
# and one line in $GITHUB_STEP_SUMMARY. Never fails the job for a cache reason.
set -euo pipefail

USAGE="usage: repair-cache-get.sh <project-dir> [<rev> ...] (or REPAIR_OUTPUTS_REVS)"
PROJECT_DIR="${1:?$USAGE}"
shift

is_sha() {
  case "$1" in *[!0-9a-f]*|'') return 1 ;; esac
  [ "${#1}" = 40 ]
}

REVS=()
add_rev() {
  local r
  for r in "${REVS[@]+"${REVS[@]}"}"; do [ "$r" = "$1" ] && return 0; done
  REVS+=("$1")
}
for rev in "$@"; do
  is_sha "$rev" || { echo "::error::candidate revision '$rev' must be a full commit SHA"; exit 1; }
  add_rev "$rev"
done
for rev in ${REPAIR_OUTPUTS_REVS:-}; do
  if is_sha "$rev"; then add_rev "$rev"
  else echo "::warning::ignoring REPAIR_OUTPUTS_REVS entry '$rev', which is not a full commit SHA"; fi
done
[ "${#REVS[@]}" -gt 0 ] || { echo "::error::no candidate revision given; $USAGE"; exit 1; }
REV_LIST="${REVS[*]}"

# shellcheck source=lake-cache-services.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lake-cache-services.sh"

report() {
  local state="$1" reused="$2" line="$3" used="${4:-}"
  echo "$line"
  {
    echo "REPAIR_OUTPUTS_STATE=$state"
    echo "REPAIR_OUTPUTS_REUSED=$reused"
    if [ "$state" = used ]; then echo "REPAIR_OUTPUTS_REVS_USED=$used"; fi
  } >> "${GITHUB_ENV:-/dev/null}"
  printf -- '- Repair outputs: %s\n' "$line" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
}

SERVICES_FILE="$(dirname -- "${BASH_SOURCE[0]}")/lake-cache-services.sh"
if [ -z "$REPAIR_CACHE_URL" ]; then
  echo "::warning::repair cache fetch is off: REPAIR_CACHE_URL is empty in $SERVICES_FILE, which materialize renders from [services.repair_cache] in tauceti.toml (mode hosted or custom, with url); repair outputs for $REV_LIST are not fetched"
  report off 0 "no repair cache configured (services.repair_cache), building from the base cache"
  exit 0
fi

# lake-cache-get.sh discards the base cache and turns the artifact cache off for the job on any
# outcome other than a clean fetch, a plain miss included. The repair outputs are the complete
# outputs of a green build, so they serve on their own: start from an empty cache and, when a
# revision contributes, turn the artifact cache back on for the build. When none does, the
# cache stays off and its directory is removed again, exactly as lake-cache-get.sh left it.
BASE_OFF=0
FALLBACK="building from the base cache"
if [ "${LAKE_ARTIFACT_CACHE:-}" = "false" ]; then
  BASE_OFF=1
  FALLBACK="building from scratch, as the base cache was discarded"
  echo "The base cache was discarded for this run; fetching repair outputs into an empty cache"
fi

discard_cache_dir() {
  local d="$PROJECT_DIR/.lake/cache"
  [ -e "$d" ] || return 0
  mv "$d" "$d.discard.$$" 2>/dev/null || rm -rf "$d" 2>/dev/null || true
  rm -rf "$d.discard.$$" 2>/dev/null || true
}
if [ "$BASE_OFF" = 1 ]; then discard_cache_dir; fi

mkdir -p "$PROJECT_DIR/.lake/cache"
CACHE="$(cd "$PROJECT_DIR/.lake/cache" && pwd)"
OUTPUTS="$CACHE/outputs"
CFG="${RUNNER_TEMP:-/tmp}/lake-repair-cache.toml"
write_lake_cache_config "$CFG" \
  "${PUBLIC_ARTIFACT_ENDPOINT:-https://cache.taucetiproject.org/artifacts}" \
  "${PUBLIC_REVISION_ENDPOINT:-https://cache.taucetiproject.org/revisions}"

# $BEFORE: the records as the base cache left them. $PLACED: the records the repair cache added
# or replaced so far, which a later revision's mapping must not replace. $ATTEMPT: the records
# before the current revision, put back if it cannot be fetched completely.
WORK="$(mktemp -d "${RUNNER_TEMP:-/tmp}/repair-outputs.XXXXXX")"
trap 'rm -rf "$WORK" 2>/dev/null || true' EXIT
BEFORE="$WORK/before"
PLACED="$WORK/placed"
ATTEMPT="$WORK/attempt"
mkdir -p "$BEFORE" "$PLACED" "$OUTPUTS"
if [ -d "$OUTPUTS" ]; then
  cp -a "$OUTPUTS/." "$BEFORE/"
fi

# Scan and copy records in one process. Staging caches have tens of thousands of
# small files: spawning cmp, dirname, mkdir and cp per record dominates the fetch.
record_changes() {
  python3 - "$1" "$OUTPUTS" "$BEFORE" "$PLACED" <<'PYTHON'
import os
import shutil
import sys
from pathlib import Path

mode, outputs, before, placed = sys.argv[1:]
outputs, before, placed = Path(outputs), Path(before), Path(placed)
count = 0
for directory, _, names in os.walk(outputs):
    for name in names:
        source = Path(directory) / name
        if not name.endswith(".json") or source.is_symlink() or not source.is_file():
            continue
        relative = source.relative_to(outputs)
        if mode == "place" and (placed / relative).exists():
            # keep_placed already restored this record. Earlier revisions retain
            # priority; copying it back to PLACED again serves no purpose.
            continue
        try:
            prior = (before / relative).read_bytes()
        except OSError:
            prior = None
        if source.read_bytes() == prior:
            continue
        count += 1
        if mode == "place":
            target = placed / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
if mode == "count":
    print(count)
PYTHON
}

changed_records() {
  record_changes count
}

# Put back the records placed by earlier revisions, then record what this one added.
keep_placed() {
  cp -a "$PLACED/." "$OUTPUTS/"
  record_changes place
}

restore_records() {
  local from="$1" discard="$OUTPUTS.discard.$$"
  if [ -e "$OUTPUTS" ]; then
    mv "$OUTPUTS" "$discard" 2>/dev/null || rm -rf "$OUTPUTS" 2>/dev/null || true
    rm -rf "$discard" 2>/dev/null || true
  fi
  mkdir -p "$OUTPUTS"
  cp -a "$from/." "$OUTPUTS/"
}

# Same retry policy as lake-cache-get.sh: a deterministic miss stops at once, a transfer failure
# retries, keeping the artifacts Lake already verified. Lake words a miss for an exact `--rev` as
# "outputs not found for revision <sha>" and a miss over a walk as "no outputs found in ...".
MISS_RE='no outputs found|outputs not found for revision'
ATTEMPTS=3
USED=()
FAILED=()
rm -f "${RUNNER_TEMP:-/tmp}"/repair-cache-get-*.log
for i in "${!REVS[@]}"; do
  REV="${REVS[$i]}"
  rm -rf "$ATTEMPT"
  mkdir -p "$ATTEMPT"
  if [ -d "$OUTPUTS" ]; then cp -a "$OUTPUTS/." "$ATTEMPT/"; fi
  outcome=failed
  for attempt in $(seq 1 $ATTEMPTS); do
    LOG="${RUNNER_TEMP:-/tmp}/repair-cache-get-$i-$attempt.log"
    rc=0
    started=$SECONDS
    ( cd "$PROJECT_DIR" && LAKE_CONFIG="$CFG" LAKE_CACHE_DIR="$CACHE" \
        lake cache get --service "$REPAIR_CACHE_SERVICE" --repo TauCetiProject/TauCeti --rev="$REV" ) \
        > "$LOG" 2>&1 || rc=$?
    if ! python3 "$(dirname -- "${BASH_SOURCE[0]}")/ci_log_filter.py" cache "$LOG"; then
      echo '::warning::repair cache log summary failed; showing the original output'
      cat "$LOG" || true
    fi
    echo "Repair cache $REV attempt $attempt: exit=$rc, elapsed=$((SECONDS-started))s"
    if [ "$rc" = 0 ]; then outcome=used; break; fi
    if grep -qE "$MISS_RE" "$LOG"; then outcome=absent; break; fi
    if [ "$attempt" != "$ATTEMPTS" ]; then
      echo "::notice::repair cache attempt $attempt of $ATTEMPTS for $REV did not complete cleanly; retrying missing artifacts"
      sleep 2
    fi
  done
  case "$outcome" in
    used) keep_placed; USED+=("$REV") ;;
    # A miss writes nothing useful, but put the records back anyway.
    absent) restore_records "$ATTEMPT"; echo "No repair outputs for $REV" ;;
    *)
      restore_records "$ATTEMPT"
      echo "::warning::repair cache fetch for $REV did not complete cleanly; restored the records from before it"
      FAILED+=("$REV")
      ;;
  esac
done

if [ "${#USED[@]}" -gt 0 ]; then
  used="${USED[*]}"
  reused="$(changed_records)"
  downloaded="$(cat "${RUNNER_TEMP:-/tmp}"/repair-cache-get-*.log 2>/dev/null | grep -cE 'downloaded artifact [0-9a-f]+' || true)"
  extra=""
  if [ "${#FAILED[@]}" -gt 0 ]; then extra="; the fetch for ${FAILED[*]} did not complete and was discarded"; fi
  if [ "$BASE_OFF" = 1 ]; then
    extra="$extra; the base cache was discarded, so the build starts from these outputs alone"
    {
      echo "LAKE_ARTIFACT_CACHE=true"
      echo "LAKE_RESTORE_ARTIFACTS=true"
      echo "LAKE_CACHE_DIR=$CACHE"
    } >> "${GITHUB_ENV:-/dev/null}"
  fi
  report used "$reused" "reused $reused module outputs from the repair cache for $used (${downloaded:-0} artifacts downloaded)$extra; Lake checks their traces and rebuilds what differs" "$used"
elif [ "${#FAILED[@]}" -gt 0 ]; then
  if [ "$BASE_OFF" = 1 ]; then discard_cache_dir; else restore_records "$BEFORE"; fi
  report failed 0 "repair outputs for ${FAILED[*]} could not be fetched completely, $FALLBACK"
else
  if [ "$BASE_OFF" = 1 ]; then discard_cache_dir; else restore_records "$BEFORE"; fi
  report absent 0 "no repair outputs for $REV_LIST, $FALLBACK"
fi
