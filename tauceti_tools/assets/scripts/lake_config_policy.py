"""Data-only Lake configuration changes permitted by the trusted build harness.

This permits enabling one built-in diagnostic. It does not authorize merging a
restricted-path change: that still requires the existing exact-head bors approval.
Never import or execute candidate configuration while making this comparison.
"""

from __future__ import annotations

import copy
import json
import sys

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def allows_tactic_linter(base: bytes, candidate: bytes) -> bool:
    """Allow only false/absent -> true on an existing library's tactic linter.

    Parsed TOML must otherwise be identical, including all package settings,
    dependencies, compiler arguments, and other options. A second call comparing
    the integrated config to the captured base enforces the same rule after Git
    assembly. Arbitrary Lake Lean configuration is never accepted by this rule.
    """
    try:
        before = tomllib.loads(base.decode("utf-8"))
        after = tomllib.loads(candidate.decode("utf-8"))
        before_libs, after_libs = before.get("lean_lib"), after.get("lean_lib")
        if not isinstance(before_libs, list) or not isinstance(after_libs, list):
            return False
        if not before_libs or len(before_libs) != len(after_libs):
            return False
        expected = copy.deepcopy(before)
        changed = False
        for old, new, allowed in zip(before_libs, after_libs, expected["lean_lib"]):
            # Require the option structure used by Lake's TOML parser. In
            # particular, quoted dotted keys are not aliases for nested keys.
            opts = new.get("leanOptions", {})
            old_opts = old.get("leanOptions", {})
            if not isinstance(opts, dict) or not isinstance(old_opts, dict):
                return False
            new_value = opts.get("weak", {}).get("linter", {}).get("unusedVariables", {}).get("analyzeTactics")
            old_value = old_opts.get("weak", {}).get("linter", {}).get("unusedVariables", {}).get("analyzeTactics")
            if new_value is True and (old_value is None or old_value is False):
                allowed.setdefault("leanOptions", {}).setdefault("weak", {}).setdefault("linter", {}).setdefault("unusedVariables", {})["analyzeTactics"] = True
                changed = True
        # Python's equality conflates true/1 and false/0. Keep all non-allowed
        # TOML values type-sensitive as well as value-sensitive.
        return changed and json.dumps(expected, sort_keys=True) == json.dumps(after, sort_keys=True)
    except (AttributeError, TypeError, ValueError, UnicodeError):
        return False


if __name__ == "__main__":
    from pathlib import Path

    if len(sys.argv) != 3:
        raise SystemExit("usage: lake_config_policy.py BASE_TOML CANDIDATE_TOML")
    raise SystemExit(0 if allows_tactic_linter(Path(sys.argv[1]).read_bytes(), Path(sys.argv[2]).read_bytes()) else 1)
