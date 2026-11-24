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
#
# Usage: sandbox-build.sh [PHASE], where PHASE is one of build, audits, module-system, lint-env,
# lint-style or finish. With a PHASE this runs only that phase, so pr-build.yml can give each
# phase its own workflow step and a run's current step names its phase. Every step is a separate
# bwrap invocation with the same mounts and environment, and the phases share state only through
# the candidate's .lake, as they did within one invocation. Without a PHASE every phase runs in
# order in this one invocation, as before.
set -euxo pipefail

# leantar 0.1.21 (Lean v4.37.0-rc1) overflows the default 8 MiB stack packing a large olean
# (TauCeti.Algebra.Lie.F4.ModularLattice: exit 139); every process this script starts, Lake's
# `-o` packing and cache staging included, inherits the larger limit.
ulimit -s unlimited 2>/dev/null || ulimit -s "$(ulimit -H -s)" 2>/dev/null || true

PHASES=(build audits module-system lint-env lint-style finish)
if [ "$#" -eq 0 ]; then
  selected=("${PHASES[@]}")
elif [ "$#" -eq 1 ] && [[ " ${PHASES[*]} " == *" $1 "* ]]; then
  selected=("$1")
else
  echo "usage: sandbox-build.sh [${PHASES[*]}]" >&2
  exit 2
fi

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

# An attested landing (ATTESTED_BUILD=1, pr-build.yml's `tauceti-bors-attested`, decision 200): a trusted
# repair worker already built, audited and linted exactly this commit with main's tools and policy, and
# the queue says so. This run then elaborates nothing: the build phase restores the outputs from the
# artifact cache with `--no-build`, which fails if any module would need building, and the finish phase
# stages them for the isolated publisher. No audit or lint phase runs, so no lint driver is needed.
ATTESTED_BUILD="${ATTESTED_BUILD:-}"
if [ "$ATTESTED_BUILD" = "1" ] && [ "${STAGE_LAKE_CACHE:-}" != "1" ]; then
  echo "::error::an attested landing needs the staged artifact cache (STAGE_LAKE_CACHE=1)" >&2
  exit 1
fi

# The environment lint's compiled driver was built by pr-build.yml on the host before any candidate
# code ran, and is mounted read-only; lint-env.sh will not compile one inside the sandbox.
if [ "$ATTESTED_BUILD" != "1" ]; then
  test -x "${LINT_DRIVER_EXE:?the prebuilt environment-lint driver is required}"
fi

# CI telemetry: when each phase below starts, and the build's own per-module log, for the host's
# telemetry step to summarise (pr-build.yml, scripts/ci_telemetry.py). Written under .lake because
# that is the only place the sandbox can write, which also means candidate code could rewrite
# them: they are statistics, never an input to any decision. Every telemetry write is best-effort:
# under `set -e` a failed write (a full disk, say) must not turn a passing build red.
TELEMETRY="$PWD/.lake/telemetry"
mkdir -p "$TELEMETRY" 2>/dev/null || true
phase() { printf '%s\t%s\n' "$1" "$(date +%s.%N)" >> "$TELEMETRY/phases.tsv" 2>/dev/null || true; }

# Build the exact candidate against its attested Lake config. bwrap keeps this offline and
# confines writes to the candidate's .lake directory.
#
# --iofail requires a SILENT build, the way Mathlib does (its CI runs `lake build --iofail`): it is
# `--fail-level=info`, so the build fails if any module logs an `info:` (or warning/error) — a stray
# #check/#eval, a `simp?`/`ring_nf?`-style "Try this: …" suggestion, a linter note. A clean elaboration
# logs nothing above trace, so this is exit-code enforcement, not output scraping.
#
# An ordinary PR build runs to completion after a failure, so its log reports every independently
# broken module to the author. A queue staging build (IS_BATCH=true with STAGE_LAKE_CACHE=1: a
# landing candidate) adds --fail-fast: Lake stops scheduling new jobs after the first failure, lets
# the running ones finish, and the run ends with that failure. The staging verdict is only ever
# pass or fail, and on a failure the queue's repair worker builds the whole library itself
# (TauCetiWorker repair_stream) and then runs the audits and lints, so it finds every broken module
# however few the staging log names. Staging once ran without --fail-fast because a round exposing
# one culprit left the next for another round (about 3.3 failed rounds per large batch on
# 2026-11-03), but that predates the repair worker's own build; since then the complete build only
# delayed the repair. Measured on 23 Nov 2026, the first error came 0.1 to 25 minutes into builds
# that ran 21 to 50 minutes (7.5 to 25 minutes after it).
#
# These exports also reach the later phases' `lake env` and `lake cache stage`, so every phase
# sets them, whichever it runs.
LAKE_BUILD_FLAGS=(--iofail)
if [ "${STAGE_LAKE_CACHE:-}" = "1" ]; then
  export LAKE_ARTIFACT_CACHE=true LAKE_RESTORE_ARTIFACTS=true
  # Generate the native map in the final build, rather than walking the graph again.
  LAKE_BUILD_FLAGS+=(--rehash -o .lake/outputs.jsonl)
  if [ "${IS_BATCH:-}" = "true" ]; then
    LAKE_BUILD_FLAGS+=(--fail-fast)
  fi
  if [ "$ATTESTED_BUILD" = "1" ]; then
    # Restore and map every output, and fail (Lake exits 3) instead of elaborating anything.
    LAKE_BUILD_FLAGS+=(--no-build)
  fi
fi

build_phase() {
  # The build is the first phase, so it starts the phase record afresh.
  : > "$TELEMETRY/phases.tsv" 2>/dev/null || true
  # Keep each changed module's base main header for the declaration-names record below. Stock Lake
  # restores only outputs whose inputs still match, never the base version of a changed module, so
  # declaration-names.sh reads those from the restored cache (base-headers.py), extracting only the
  # archives whose file names could hold one. For all of TauCeti that took 2 to 5s on 96 CPUs,
  # where unpacking the whole cache for it (bulk-unpack-cache.py) took about 110s there and about
  # 400s on a 96-CPU fleet guest, against about 30s for Lake's own restore. Advisory: a failure
  # here only means no record.
  phase build
  bash "$TRUSTED_SCRIPTS/declaration-names.sh" save || true
  run_lake_build
  # A staging build's outputs are published. Lake trusts any output whose trace matches its inputs
  # without reading it, so an interrupted write or a corrupt cached archive (a truncated
  # .olean.private, an empty .ir.sig) would otherwise crash the audits and reach the public cache.
  # Check every output against the content hash its trace names; compile the damaged modules from
  # source instead of trusting them. This also runs after a failed build, which such a module can
  # cause.
  if [ "${STAGE_LAKE_CACHE:-}" = "1" ]; then
    # A failed build can hide further damaged modules it never reached, so recheck after each
    # rebuild, up to two rebuilds.
    for check_round in 1 2 3; do
      phase output-check
      check_status=0
      python3 "$TRUSTED_SCRIPTS/lake_output_check.py" discard . || check_status=$?
      case "$check_status" in
        0) break ;;
        3)
          if [ "$check_round" = 3 ]; then
            echo '::error::outputs still differ from their traces after rebuilding them twice'
            exit 1
          fi
          echo '::warning::some outputs differed from their traces; rebuilding those modules from source'
          phase build
          run_lake_build
          ;;
        *)
          if [ "$build_status" = 0 ]; then
            echo '::error::the built outputs could not be checked against their traces, so they cannot be published'
            exit 1
          fi
          break
          ;;
      esac
    done
  fi
  if [ "$build_status" != 0 ]; then exit "$build_status"; fi
}

run_lake_build() {
  # `--output-error=warn` keeps tee passing the build's output through even if writing the copy fails,
  # and `|| true` drops tee's own status, so `pipefail` reports exactly Lake's. thread_exhaustion.py
  # passes Lake's output and status through, except that a build failed only by the host running out
  # of threads exits 75, which pr-build.yml reports as an infrastructure failure.
  set +e
  # Lake restores each cached module with its own leantar, about one per CPU at once, and each leantar's rayon pool
  # defaults to one thread per CPU. On a 96-CPU native fleet guest that reached thousands of tasks per build and exhausted
  # the host's CI task limit (24 Nov 2026). leantar parallelises across the archives it is given,
  # and Lake gives it one, so the extra workers only sit idle: on a 96-CPU host a full stock
  # restore of TauCeti (39,164 modules) peaked at 695 tasks in 37s with 4, against 5,248 tasks
  # in 60s by default.
  RAYON_NUM_THREADS=4 python3 "$TRUSTED_SCRIPTS/thread_exhaustion.py" lake build "${LAKE_BUILD_FLAGS[@]}" 2>&1 \
    | { tee --output-error=warn "$TELEMETRY/lake-build.log" 2>/dev/null || true; } \
    | { python3 "$TRUSTED_SCRIPTS/ci_log_filter.py" build || { echo '::warning::build log summary failed; showing remaining output'; cat; }; }
  build_status=${PIPESTATUS[0]}
  set -e
}

audits_phase() {
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
    # The axiom check is independent of ownership: it runs and reports after a duplicate
    # failure too, and the phase then fails with the duplicate check's status.
    duplicates_status=0
    AUDIT_PHASES_FILE="$TELEMETRY/phases.tsv" lake env "$AUDIT_TIMEOUT" --signal=TERM --kill-after=30 \
      3000s "$AUDIT_DRIVER_EXE" --duplicates-only || duplicates_status=$?
    # Ordinary PRs check the changed files' own compiled declarations, including private proofs.
    # The workflow leaves this scope unset for staging, pin changes and unscopable changes.
    phase audit-axioms
    "$AUDIT_TIMEOUT" --signal=TERM --kill-after=30 3000s \
      "$(dirname "$LINT_DRIVER_EXE")/local-axioms" "$AXIOMS_ONLY_MODULES" TauCeti "$PWD/.lake/build/lib/lean"
    if [ "$duplicates_status" != 0 ]; then exit "$duplicates_status"; fi
  else
    AUDIT_PHASES_FILE="$TELEMETRY/phases.tsv" lake env "$AUDIT_TIMEOUT" --signal=TERM --kill-after=30 \
      3000s "$AUDIT_DRIVER_EXE"
  fi
}

module_system_phase() {
  # The canonical module-system check reads only the main artifact flag, without importing
  # the Mathlib environment. Keep one implementation and its existing standalone executable.
  phase audit-module-system
  lake env "$WATCHDOG_TOOLCHAIN/bin/lean" --run "$TRUSTED_SCRIPTS/ModuleSystem.lean"
}

lint_env_phase() {
  # Environment lint (the compiled driver in LINT_DRIVER_EXE): Mathlib's default `#lint` set minus
  # docBlame, plus a
  # module-system-reliable docstring scan, compared against the grandfathered baseline in
  # scripts/lint-baseline.txt. Script, baseline, and the @[nolint <linter>] allowlist
  # (scripts/lint-nolints-allowlist.txt) are workflow-pinned trusted copies, and its report parsing
  # is fail-closed (see the SECURITY MODEL in the script). Fails on new violations or
  # unaccounted nolints; fixed baseline entries print a ratchet reminder only.
  phase lint-env
  bash "$TRUSTED_SCRIPTS/lint-env.sh"
}

lint_style_phase() {
  # Source style lint. The trusted wrapper uses the shared validated TauCeti/ module list, applies
  # Mathlib's copyright/Authors checks (excluding the deliberately empty root), and generates the
  # text-linter import root under .lake/ without relying on TauCeti.lean's imports.
  phase lint-style
  bash "$TRUSTED_SCRIPTS/lint-style.sh"
}

finish_phase() {
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
}

for selected_phase in "${selected[@]}"; do
  if [ "$ATTESTED_BUILD" = "1" ] && [[ " audits module-system lint-env lint-style " == *" $selected_phase "* ]]; then
    echo "::error::an attested landing runs no $selected_phase phase" >&2
    exit 2
  fi
  case "$selected_phase" in
    build) build_phase ;;
    audits) audits_phase ;;
    module-system) module_system_phase ;;
    lint-env) lint_env_phase ;;
    lint-style) lint_style_phase ;;
    finish) finish_phase ;;
  esac
done
