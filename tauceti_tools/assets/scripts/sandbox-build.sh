#!/usr/bin/env bash
# sandbox-build.sh — the offline, bwrap-sandboxed build + audits + environment lint of
# the candidate TauCeti sources, factored out of .github/workflows/pr-build.yml.
#
# pr-build.yml invokes this workflow-pinned copy inside the bwrap sandbox with a fixed, comment-free
# one-liner, so the workflow's `bash -c` payload
# can no longer be broken by an apostrophe or a stray quote in a comment: all the prose
# and its punctuation live here, in a file the shell reads as a script rather than as a
# single-quoted `-c` argument. (A comment reading "Mathlib's default" once closed that
# single-quoted argument early and reddened every PR build; see #694.)
#
# This is a TRUSTED workflow-pinned copy mounted separately from the exact candidate checkout.
# Candidate scripts are never invoked by the required build, even when a human-owned
# infrastructure PR changes them.
#
# cwd on entry is the exact immutable candidate checkout.
set -euxo pipefail

export TMPDIR="$PWD/.lake/tmp"
TRUSTED_SCRIPTS="${TAUCETI_TRUSTED_SCRIPTS:?trusted script directory is required}"
test -d "$TRUSTED_SCRIPTS"

# Lake normally invokes Lean directly. Route every compiler process through a
# trusted wall-clock watchdog instead. This script and the wrapper are the
# workflow-pinned copies, and the sandbox does not pass timeout-control variables,
# so candidate code cannot raise or disable the 300s
# deadline. The wrapper and Lean both remain inside the same bwrap sandbox.
test -n "${WATCHDOG_TOOLCHAIN:-}"
test -x "$WATCHDOG_TOOLCHAIN/bin/lean"
export LAKE_OVERRIDE_LEAN=true
export LEAN="$WATCHDOG_TOOLCHAIN/bin/lean"

# The environment lint's compiled driver was built by pr-build.yml on the host before any candidate
# code ran, and is mounted read-only; lint-env.sh will not compile one inside the sandbox.
test -x "${LINT_DRIVER_EXE:?the prebuilt environment-lint driver is required}"

# CI telemetry: when each phase below starts, and the build's own per-module log, for the host's
# telemetry step to summarise (pr-build.yml, scripts/ci_telemetry.py). Written under .lake because
# that is the only place the sandbox can write, which also means candidate code could rewrite
# them: they are statistics, never an input to any decision. Every telemetry write is best-effort:
# under `set -e` a failed write (a full disk, say) must not turn a passing build red.
TELEMETRY="$PWD/.lake/telemetry"
mkdir -p "$TELEMETRY" 2>/dev/null || true
: > "$TELEMETRY/phases.tsv" 2>/dev/null || true
phase() { printf '%s\t%s\n' "$1" "$(date +%s.%N)" >> "$TELEMETRY/phases.tsv" 2>/dev/null || true; }

# Build the exact candidate against its attested Lake config. bwrap keeps this offline and
# confines writes to the candidate's .lake directory.
#
# --iofail requires a SILENT build, the way Mathlib does (its CI runs `lake build --iofail`): it is
# `--fail-level=info`, so the build fails if any module logs an `info:` (or warning/error) — a stray
# #check/#eval, a `simp?`/`ring_nf?`-style "Try this: …" suggestion, a linter note. A clean elaboration
# logs nothing above trace, so this is exit-code enforcement, not output scraping.
#
# Every build, staging (IS_BATCH=true: merge-group or bors staging) and ordinary PR alike, runs to
# completion after a failure, so the log reports every independently broken module. Staging used to
# add --fail-fast so a doomed batch freed the runner sooner, but a batch failure costs a whole serial
# staging round, and a round that exposes only the first broken module leaves the next culprit for
# another round: measured on 2026-11-03, about 3.3 failed rounds per landed large batch. The extra
# runner minutes of a complete build buy the controller every attributable culprit at once.
LAKE_BUILD_FLAGS=(--iofail)
if [ "${STAGE_LAKE_CACHE:-}" = "1" ]; then
  export LAKE_ARTIFACT_CACHE=true LAKE_RESTORE_ARTIFACTS=true
  # Generate the native map in the final build, rather than walking the graph again.
  LAKE_BUILD_FLAGS+=(--rehash -o .lake/outputs.jsonl)
fi
if [ "${STAGE_LAKE_CACHE:-}" = "1" ]; then
  # Stock Lake forks once per cached module. Prepare fresh private caches in one
  # leantar call, retaining archives for native publication; Lake verifies inputs.
  python3 "$TRUSTED_SCRIPTS/bulk-unpack-cache.py" .
fi
# Keep each changed module's restored main header for the declaration-names record below.
# Advisory: a failure here only means no record.
bash "$TRUSTED_SCRIPTS/declaration-names.sh" save || true
phase build
# `--output-error=warn` keeps tee passing the build's output through even if writing the copy fails,
# and `|| true` drops tee's own status, so `pipefail` reports exactly Lake's.
set +e
lake build "${LAKE_BUILD_FLAGS[@]}" 2>&1 \
  | { tee --output-error=warn "$TELEMETRY/lake-build.log" 2>/dev/null || true; } \
  | { python3 "$TRUSTED_SCRIPTS/ci_log_filter.py" build || { echo '::warning::build log summary failed; showing remaining output'; cat; }; }
build_status=${PIPESTATUS[0]}
set -e
if [ "$build_status" != 0 ]; then exit "$build_status"; fi

# Both workflow-pinned audit executables are built beside the lint driver on the host
# and mounted read-only. The global duplicate check remains independent of the PR scope.
AUDIT_DRIVER_EXE="$(dirname "$LINT_DRIVER_EXE")/audit-driver"
test -x "$AUDIT_DRIVER_EXE"
phase audit-setup
# This is a full-library pass, so use the existing environment-lint liveness budget,
# not the 300s compiler deadline. A shared process must not shrink the former audits
# into one per-module compilation budget. Phase telemetry exposes growth before tuning
# this trusted bound; timeout remains a process/liveness failure, not an audit finding.
AUDIT_TIMEOUT="$(command -v timeout)"
if [ -n "${AXIOMS_ONLY_MODULES:-}" ]; then
  AUDIT_PHASES_FILE="$TELEMETRY/phases.tsv" lake env "$AUDIT_TIMEOUT" --signal=TERM --kill-after=30 \
    3000s "$AUDIT_DRIVER_EXE" --duplicates-only
  # Ordinary PRs check the changed files' own compiled declarations, including private proofs.
  # The workflow leaves this scope unset for staging, pin changes and unscopable changes.
  phase audit-axioms
  "$AUDIT_TIMEOUT" --signal=TERM --kill-after=30 3000s \
    "$(dirname "$LINT_DRIVER_EXE")/local-axioms" "$AXIOMS_ONLY_MODULES" TauCeti "$PWD/.lake/build/lib/lean"
else
  AUDIT_PHASES_FILE="$TELEMETRY/phases.tsv" lake env "$AUDIT_TIMEOUT" --signal=TERM --kill-after=30 \
    3000s "$AUDIT_DRIVER_EXE"
fi

# The canonical module-system check reads only the main artifact flag, without importing
# the Mathlib environment. Keep one implementation and its existing standalone executable.
phase audit-module-system
lake env "$WATCHDOG_TOOLCHAIN/bin/lean" --run "$TRUSTED_SCRIPTS/ModuleSystem.lean"

# Environment lint (the compiled driver in LINT_DRIVER_EXE): Mathlib's default `#lint` set minus
# docBlame, plus a
# module-system-reliable docstring scan, compared against the grandfathered baseline in
# scripts/lint-baseline.txt. Script, baseline, and the @[nolint <linter>] allowlist
# (scripts/lint-nolints-allowlist.txt) are workflow-pinned trusted copies, and its report parsing
# is fail-closed (see the SECURITY MODEL in the script). Fails on new violations or
# unaccounted nolints; fixed baseline entries print a ratchet reminder only.
phase lint-env
bash "$TRUSTED_SCRIPTS/lint-env.sh"

# Source style lint. The trusted wrapper uses the shared validated TauCeti/ module list, applies
# Mathlib's copyright/Authors checks (excluding the deliberately empty root), and generates the
# text-linter import root under .lake/ without relying on TauCeti.lean's imports.
phase lint-style
bash "$TRUSTED_SCRIPTS/lint-style.sh"

# Declaration names the PR adds, read from the compiled headers (TauCeti
# scripts/env-declaration-names.md). The merge queue uses the record to keep two batch-mates
# that add the same name apart before a staging build. Never affects the exit status.
phase declaration-names
set +e
bash "$TRUSTED_SCRIPTS/declaration-names.sh" record || true
set -e

# A merge-group commit that passes every audit is the exact commit GitHub will land. Pack its
# root-package outputs while still inside the bwrap sandbox, after the final operation that executes
# candidate code. After the sandbox exits, the host stages validated archive files and the complete
# map for a fresh, secret-isolated publisher job. Ordinary PRs and installations without upload endpoints leave this disabled.
if [ "${STAGE_LAKE_CACHE:-}" = "1" ]; then
  phase stage-cache
  if ! grep -Eq '^[[:space:]]*platformIndependent[[:space:]]*=[[:space:]]*true' lakefile.toml; then
    echo "lakefile.toml no longer sets platformIndependent = true" >&2
    exit 1
  fi
  if grep -Eq '^[[:space:]]*(fixedToolchain|bootstrap)[[:space:]]*=[[:space:]]*true' lakefile.toml; then
    echo "lakefile.toml changes the toolchain cache scope" >&2
    exit 1
  fi

  lake cache stage .lake/outputs.jsonl .lake/publication-stage
  echo "root-package mapping entries: $(wc -l < .lake/outputs.jsonl)"

fi

phase end
