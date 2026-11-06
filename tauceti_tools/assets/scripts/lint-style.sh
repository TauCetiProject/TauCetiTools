#!/usr/bin/env bash
# Run source style checks over all sources, or validated changed modules in PR preflight.
#
# lint-style treats its module arguments as import roots and lints only their imports. The
# library root TauCeti.lean is intentionally empty, so generate a temporary import-all module
# instead. It lives under .lake because that is the writable area in the PR build sandbox. Source
# discovery and validation are shared with lint-env.sh so the two whole-library checks cannot
# drift apart.
set -euo pipefail

PROJECT_ROOT="${TAUCETI_PROJECT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
TRUSTED_SCRIPTS="${TAUCETI_TRUSTED_SCRIPTS:-$PROJECT_ROOT/scripts}"
cd "$PROJECT_ROOT"
case "${LINT_CHANGED_ONLY:-0}" in
  0|1) ;; *) echo 'lint-style: invalid LINT_CHANGED_ONLY' >&2; exit 1 ;;
esac

lint_src="$(mktemp -d "$PWD/.lake/lint-style-src.XXXXXX")"
trap 'rm -rf "$lint_src"' EXIT
lint_module="$lint_src/TauCetiLint/All.lean"
mkdir -p "$(dirname "$lint_module")"

. "$TRUSTED_SCRIPTS/source-modules.sh"
tauceti_source_modules "$lint_src/files" "$lint_src/modules"
if [ "${LINT_CHANGED_ONLY:-0}" = 1 ]; then
  [ -f "${LINT_ONLY_MODULES:-}" ] || { echo 'lint-style: missing scope file' >&2; exit 1; }
  LC_ALL=C comm -12 <(LC_ALL=C sort -u "$LINT_ONLY_MODULES") \
    <(LC_ALL=C sort -u "$lint_src/modules") > "$lint_src/selected"
  mv "$lint_src/selected" "$lint_src/modules"
  # Convert only names validated by source discovery into file paths. Deleted files drop out.
  sed 's@\.@/@g; s@$@.lean@' "$lint_src/modules" | tr '\n' '\0' > "$lint_src/files"
fi
mapfile -d '' files < "$lint_src/files"
printf 'import TauCeti\n' > "$lint_module"
sed 's/^/import /' "$lint_src/modules" >> "$lint_module"
expected_imports=$((${#files[@]} + 1))
emitted_imports="$(grep -c '^import ' "$lint_module")"
if ((emitted_imports != expected_imports)); then
  echo 'lint-style: generated import root is incomplete; refusing to run.' >&2
  exit 1
fi

# Use the validated discovery list for the copyright/Authors audit. Keep going after a header
# failure so contributors also see Mathlib's text-style diagnostics in the same CI round.
status=0
if ((${#files[@]} == 0)); then
  echo "lint-style: no remaining changed source files; skipping header audit."
elif ! lake env lean --run "$TRUSTED_SCRIPTS/HeaderStyle.lean" "${files[@]}"; then
  status=1
fi

# TauCetiLint.All supplies every TauCeti/ import plus TauCeti itself, so the intentionally empty
# root also receives text checks. The TauCeti argument makes lint-style's package-root filter retain
# those imports, while contributing none of its own. The copyright audit is separate because the
# pinned Mathlib header linter is a command linter that skips modules absent from the empty root.
if ! LEAN_SRC_PATH="$lint_src${LEAN_SRC_PATH:+:$LEAN_SRC_PATH}" \
    lake exe lint-style "$@" TauCetiLint.All TauCeti; then
  status=1
fi

exit "$status"
