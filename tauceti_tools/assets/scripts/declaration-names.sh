#!/usr/bin/env bash
# Record the declaration names a PR adds (TauCeti scripts/env-declaration-names.md).
#
# Advisory only: every path here exits 0, so the build's verdict never depends on it.
# sandbox-build.sh calls it twice from the project root:
#   declaration-names.sh save     before `lake build`: copy each changed module's base `main`
#                                 header aside, so the base names survive the build;
#   declaration-names.sh record   after the lint phases: run the workflow-pinned
#                                 DeclarationNames.lean and print its single record line.
# Inputs come from the sandbox environment: IS_BATCH, LAKE_CACHE_DIR, LINT_ONLY_MODULES (one module name per
# line), TAUCETI_TRUSTED_SCRIPTS (or TRUSTED_SCRIPTS), WATCHDOG_TOOLCHAIN, SHARED_TREE_HIT and
# CI_RESOURCE_TESTED_{HEAD,BASE,COMMIT}.
# Staging batches (IS_BATCH=true) and builds without a changed-module list record nothing.
set -u
mode="${1:-}"
[ "${IS_BATCH:-}" = "true" ] && exit 0
if [ -z "${LINT_ONLY_MODULES:-}" ] || [ ! -r "$LINT_ONLY_MODULES" ]; then exit 0; fi
base_dir="$PWD/.lake/telemetry/base-oleans"
# Written by `save` when the restored cache cannot supply the base headers.
unavailable="$base_dir/.unavailable"

case "$mode" in
  save)
    mkdir -p "$base_dir" 2>/dev/null || exit 0
    # Over the shared build tree (SHARED_TREE_HIT=1, scripts/shared_tree.py) .lake/build already
    # holds an older main's outputs, not the captured base's, so every header comes from the
    # base's own map below instead.
    [ "${SHARED_TREE_HIT:-}" = 1 ] || while IFS= read -r module; do
      module="${module//[[:space:]]/}"
      [ -n "$module" ] || continue
      rel="${module//./\/}.olean"
      src=".lake/build/lib/lean/$rel"
      if [ -f "$src" ]; then
        mkdir -p "$base_dir/$(dirname "$rel")" 2>/dev/null && cp -p "$src" "$base_dir/$rel" 2>/dev/null
      fi
    done < "$LINT_ONLY_MODULES"
    # A fresh CI build has no .lake/build yet: stock Lake restores during `lake build`, and only
    # outputs whose inputs still match. Read the remaining base headers from the cache itself.
    # Without them the record would count every name of a changed module as added, so it is
    # withheld instead.
    rm -f "$unavailable" 2>/dev/null
    if ! python3 "$(dirname -- "${BASH_SOURCE[0]}")/base-headers.py" "$LINT_ONLY_MODULES" "$base_dir" \
        --base "${CI_RESOURCE_TESTED_BASE:-}"; then
      : > "$unavailable" 2>/dev/null
    fi
    ;;
  record)
    # sandbox-build.sh keeps TRUSTED_SCRIPTS unexported; the sandbox exports TAUCETI_TRUSTED_SCRIPTS.
    script="${TAUCETI_TRUSTED_SCRIPTS:-${TRUSTED_SCRIPTS:-}}/DeclarationNames.lean"
    [ -f "$script" ] || exit 0
    if [ -e "$unavailable" ]; then
      echo "declaration-names: no base headers; no record" >&2
      exit 0
    fi
    for v in "${CI_RESOURCE_TESTED_HEAD:-}" "${CI_RESOURCE_TESTED_BASE:-}" "${CI_RESOURCE_TESTED_COMMIT:-}"; do
      [ -n "$v" ] || exit 0
    done
    lean="${WATCHDOG_TOOLCHAIN:-}/bin/lean"
    [ -x "$lean" ] || lean=lean
    out="$(mktemp 2>/dev/null)" || exit 0
    limit=""
    if command -v timeout >/dev/null 2>&1; then limit="timeout 120"; fi
    # shellcheck disable=SC2086
    $limit lake env "$lean" --run "$script" "$CI_RESOURCE_TESTED_HEAD" "$CI_RESOURCE_TESTED_BASE" \
      "$CI_RESOURCE_TESTED_COMMIT" "$LINT_ONLY_MODULES" .lake/build/lib/lean "$base_dir" \
      > "$out" 2> "$out.err"
    # At most one record line reaches stdout; everything else is diagnostic.
    grep -m1 '^DECLARATION-NAMES ' "$out" || true
    grep -v '^DECLARATION-NAMES ' "$out" >&2 || true
    cat "$out.err" >&2 2>/dev/null || true
    rm -f "$out" "$out.err"
    ;;
esac
exit 0
