#!/usr/bin/env python3
"""Real Lean regression: PROJECT_ROOT DRIVER_EXE; uses an isolated fixture library.

An upstream API drops an instance argument. Unchanged transitive importers now
have unused instance arguments, including a handwritten private declaration.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from lint_env_locations import LOCATIONS, ORIGINS


def main():
    project, driver = (Path(p).resolve() for p in sys.argv[1:])
    with tempfile.TemporaryDirectory(prefix="downstream-unused-arguments-") as temp:
        root = Path(temp)
        (root / "lean-toolchain").write_text((project / "lean-toolchain").read_text())
        (root / "lakefile.toml").write_text(
            'name = "downstream-lint-fixture"\n[[require]]\nname = "mathlib"\n'
            f'path = "{project / ".lake/packages/mathlib"}"\n'
            '[[lean_lib]]\nname = "TauCeti"\n')
        (root / "TauCeti").mkdir()
        modules = ["Upstream", "Changed", "Bridge", "Leaf", "Sibling", "Unrelated", "Equations",
                   "EquationUser"]
        sources = {
            "Upstream": """module
public import Mathlib.Algebra.Module.Torsion.Basic
public section
namespace TauCeti
/-- Existing upstream lint debt, outside the change. -/
def upstreamUnused (n m : Nat) : Nat := n
end TauCeti
""",
            "Changed": """module
public import TauCeti.Upstream
public section
namespace TauCeti
/-- The API whose instance parameter is removed. -/
def choose {α : Type} [Inhabited α] (x : α) : α := if True then x else default
end TauCeti
""",
            "Bridge": "module\npublic import TauCeti.Changed\n",
            "Leaf": """module
public import TauCeti.Bridge
public section
namespace TauCeti
private def inherited {α : Type} [Inhabited α] (x : α) : α := choose x
/-- A different linter's downstream violation, outside its selected scope. -/
@[simp] theorem downstreamSimp (n : Nat) : n + 0 = n := Nat.add_zero n
end TauCeti
""",
            "Sibling": """module
public import TauCeti.Bridge
public section
namespace TauCeti
/-- A second transitive consumer of the API. -/
def sibling {α : Type} [Inhabited α] (x : α) : α := choose x
end TauCeti
""",
            "Unrelated": """module
public import Mathlib.Algebra.Module.Torsion.Basic
public section
namespace TauCeti
/-- Existing unrelated lint debt, outside the change. -/
def unrelatedUnused (n m : Nat) : Nat := n
end TauCeti
""",
            # Definitions whose bodies are not exposed, so Lean gives the equation lemmas it
            # generates for them private names in the module that first needs each lemma.
            "Equations": """module
public import Mathlib.Algebra.Module.Torsion.Basic
public section
namespace TauCeti
/-- Its equation lemma is generated in this module. -/
def localEqn (n : Nat) : Nat := n + 1
/-- Its equation lemma is generated downstream. -/
def remoteEqn (n : Nat) : Nat := n + 2
theorem localEqn_eq (n : Nat) : localEqn n = n + 1 := by rw [localEqn]
-- Hand-written theorems with equation-lemma names, which Lean accepts before the definition.
theorem handEqn.eq_2 : True := trivial
private theorem handEqn.eq_def : True := trivial
/-- Only gives the hand-written theorems above their names. -/
def handEqn (n : Nat) : Nat := n + 3
end TauCeti
""",
            "EquationUser": """module
public import TauCeti.Equations
import all TauCeti.Equations
public section
namespace TauCeti
theorem remoteEqn_eq (n : Nat) : remoteEqn n = n + 2 := by rw [remoteEqn]
end TauCeti
""",
        }
        for name, source in sources.items():
            (root / f"TauCeti/{name}.lean").write_text(source)
        (root / "changed").write_text("TauCeti.Changed\n")
        (root / "all").write_text("".join(f"TauCeti.{m}\n" for m in reversed(modules)))
        (root / "marker").write_text("DOWNSTREAM-TEST-COMPLETE\n")
        env = {**os.environ, "LAKE_CACHE_DIR": "", "LAKE_ARTIFACT_CACHE": "false",
               "LAKE_RESTORE_ARTIFACTS": "false", "LAKE_NO_CACHE": "true", "LEAN_NUM_THREADS": "4"}

        def build():
            subprocess.run(["lake", "build", *(f"TauCeti.{m}" for m in modules)],
                           cwd=root, env=env, check=True)

        def run(*flags, module_file="changed", linters=("unusedArguments", "simpNF")):
            return subprocess.run(
                ["lake", "env", str(driver), *flags, "/tmp/downstream-test",
                 str(root / "marker"), str(root / module_file), *linters],
                cwd=root, env=env, text=True, capture_output=True)

        def findings(result, count):
            assert result.returncode == int(count != 0), result.stdout + result.stderr
            assert len(re.findall(r"-- Found \d+ errors? in", result.stdout)) == 1, result.stdout + result.stderr
            assert result.stdout.count("DOWNSTREAM-TEST-COMPLETE") == 1, result.stdout + result.stderr
            origin = json.loads(next(line[len(ORIGINS):] for line in result.stdout.splitlines()
                                     if line.startswith(ORIGINS)))
            assert origin["total"] == count and not origin["truncated"], origin
            return origin["records"]

        build()
        # Downstream instance arguments were used by the old API. Other known
        # debt in upstream/unrelated modules stays outside the selected scope.
        initial = run("--only-listed", "--downstream-unused-arguments", str(root / "all"))
        findings(initial, 0)

        # Only this file changes. Leaf and Sibling must now fail unusedArguments.
        changed = root / "TauCeti/Changed.lean"
        changed.write_text(sources["Changed"].replace(" [Inhabited α]", "")
                           .replace("if True then x else default", "x"))
        build()
        findings(run("--only-listed"), 0)  # reproduces the old false green
        result = run("--only-listed", "--downstream-unused-arguments", str(root / "all"))
        records = findings(result, 2)
        assert {r["linter"] for r in records} == {"unusedArguments"}, records
        assert any(r["declaration"].endswith(".TauCeti.inherited") for r in records), records
        assert any(r["declaration"] == "TauCeti.sibling" for r in records), records
        assert all("Unused" not in r["declaration"] for r in records), records
        # Other linters still run when their own module is selected.
        (root / "leaf").write_text("TauCeti.Leaf\n")
        leaf_records = findings(run("--only-listed", module_file="leaf"), 2)
        assert {r["linter"] for r in leaf_records} == {"unusedArguments", "simpNF"}, leaf_records
        # A full run still uses the existing whole-library behavior.
        full = findings(run(module_file="all"), 5)
        assert {r["linter"] for r in full} == {"unusedArguments", "simpNF"}, full
        # Shards lint every declaration exactly once between them, with the same verdicts.
        def declarations(result):
            found = re.search(r"-- Found \d+ errors? in (\d+) declarations "
                              r"\(plus (\d+) automatically generated ones\)", result.stdout)
            return int(found[1]) + int(found[2])

        def origins(result):
            origin = json.loads(next(line[len(ORIGINS):] for line in result.stdout.splitlines()
                                     if line.startswith(ORIGINS)))
            return sorted((r["linter"], r["declaration"]) for r in origin["records"])

        whole = run(module_file="all")
        for n in (2, 3):
            shards = [run("--shard", f"{i}/{n}", module_file="all") for i in range(n)]
            assert sum(map(declarations, shards)) == declarations(whole), [s.stdout for s in shards]
            for i, shard in enumerate(shards):
                assert re.findall(r"^LINT-SHARD (\d+)/(\d+) declarations=(\d+)$", shard.stdout,
                                  re.M) == [(str(i), str(n), str(declarations(whole)))], shard.stdout
            assert sorted(sum(map(origins, shards), [])) == origins(whole), [s.stdout for s in shards]
        for bad in (["--shard", "2/2"], ["--shard", "0/0"], ["--shard", "1"],
                    ["--shard", "0/2", "--only-listed"]):
            result = run(*bad, module_file="all")
            assert result.returncode == 2 and "DOWNSTREAM-TEST-COMPLETE" not in result.stdout, result
        # checkType skips exactly the equation lemmas Lean generated, wherever it generated them,
        # and never a hand-written theorem with such a name.
        eqns = run(module_file="all", linters=("checkType",))
        assert "lint-env: checkType skips 2 generated equation lemmas" in eqns.stdout, eqns.stdout
        # Bad/incomplete inventory fails without a success marker.
        (root / "incomplete").write_text("TauCeti.Leaf\n")
        bad = run("--only-listed", "--downstream-unused-arguments", str(root / "incomplete"))
        assert bad.returncode != 0 and "DOWNSTREAM-TEST-COMPLETE" not in bad.stdout, bad

        # Normal and downstream results of the same linter must merge into one
        # report, accepted by the existing shell parser, baseline and diagnostics.
        changed.write_text(changed.read_text() + """
/-- A new violation in the edited module itself. -/
def TauCeti.changedUnused (n m : Nat) : Nat := n
""")
        build()
        mixed = run("--only-listed", "--downstream-unused-arguments", str(root / "all"))
        mixed_records = findings(mixed, 3)
        trusted = root / "trusted"
        trusted.mkdir()
        scripts = Path(__file__).resolve().parent
        for source in scripts.iterdir():
            if source.is_file() and source.name not in (
                    "lint-baseline.txt", "lint-nolints-allowlist.txt", "lint-docscan-sentinels.txt"):
                (trusted / source.name).symlink_to(source)
        (trusted / "lint-baseline.txt").write_text("")
        (trusted / "lint-nolints-allowlist.txt").write_text("")
        # Materialized CI tooling includes the production project's coverage policy.
        # This isolated five-declaration library needs its own floor and sentinel.
        (trusted / "lint-docscan-sentinels.txt").write_text("5\nTauCeti.choose\n")
        lint_env = {**env, "TAUCETI_PROJECT_ROOT": str(root),
                    "TAUCETI_TRUSTED_SCRIPTS": str(trusted), "LINT_DRIVER_EXE": str(driver),
                    "LINT_ONLY_MODULES": str(root / "changed")}

        def shell():
            return subprocess.run(["bash", str(scripts / "lint-env.sh")], cwd=root,
                                  env=lint_env, text=True, capture_output=True)

        # The workflow opts ordinary PRs into checking changed declarations only.
        # The pre-existing local scoped path continues checking downstream declarations.
        lint_env["LINT_CHANGED_ONLY"] = "1"
        fast = shell()
        assert fast.returncode == 1, fast.stdout + fast.stderr
        assert "LINT-ENV: FAIL — 1 new violation(s)" in fast.stdout, fast.stdout + fast.stderr
        assert "PR preflight: 1 changed module(s)" in fast.stdout, fast.stdout + fast.stderr
        assert "RATCHET" not in fast.stdout, fast.stdout + fast.stderr
        lint_env.pop("LINT_CHANGED_ONLY")
        failed = shell()
        assert failed.returncode == 1, failed.stdout + failed.stderr
        assert "LINT-ENV: FAIL — 3 new violation(s)" in failed.stdout, failed.stdout + failed.stderr
        locs = json.loads(next(line[len(LOCATIONS):] for line in failed.stdout.splitlines()
                               if line.startswith(LOCATIONS)))
        assert len(locs["findings"]) == 3, locs
        baseline = sorted(r["linter"] + " " + r["report_name"] for r in mixed_records)
        (trusted / "lint-baseline.txt").write_text("\n".join(baseline) + "\n")
        passed = shell()
        assert passed.returncode == 0 and "LINT-ENV: PASS" in passed.stdout, passed.stdout + passed.stderr
        # Whole-library runs, sharded or not, report the same violations.
        lint_env.pop("LINT_ONLY_MODULES")
        unsharded = shell()
        lint_env["LINT_SHARDS"] = "3"
        sharded = shell()
        lint_env.pop("LINT_SHARDS")
        assert "lint-env: 3 shards linted" in sharded.stdout, sharded.stdout + sharded.stderr
        assert sharded.returncode == unsharded.returncode == 1, sharded.stdout + unsharded.stdout
        new = re.compile(r"^LINT-ENV: FAIL — .*$", re.M)
        assert new.findall(sharded.stdout) == new.findall(unsharded.stdout), sharded.stdout
        locs = [json.loads(next(line[len(LOCATIONS):] for line in r.stdout.splitlines()
                                if line.startswith(LOCATIONS))) for r in (sharded, unsharded)]
        assert locs[0] == locs[1] and locs[0]["complete"], locs
        lint_env["LINT_ONLY_MODULES"] = str(root / "changed")
        # An undocumented declaration outside the selected module is deferred to staging.
        unrelated = root / "TauCeti/Unrelated.lean"
        unrelated.write_text(unrelated.read_text() + "\ndef TauCeti.undocumentedOutside : Nat := 1\n")
        build()
        lint_env["LINT_CHANGED_ONLY"] = "1"
        fast = shell()
        assert fast.returncode == 0, fast.stdout + fast.stderr
        assert "RATCHET" not in fast.stdout, fast.stdout + fast.stderr
        lint_env.pop("LINT_CHANGED_ONLY")
        lint_env.pop("LINT_ONLY_MODULES")
        staging = shell()
        assert staging.returncode == 1, staging.stdout + staging.stderr
        assert "TauCeti.undocumentedOutside" in staging.stdout, staging.stdout + staging.stderr
        # Missing docs in the selected module must fail even if it has no documented defs.
        selected = root / "TauCeti/Unrelated.lean"
        selected.write_text("module\npublic import Mathlib.Algebra.Group.Defs\npublic section\n"
                            "def TauCeti.undocumentedInside : Nat := 1\n")
        build()
        (root / "changed").write_text("TauCeti.Unrelated\n")
        lint_env["LINT_ONLY_MODULES"] = str(root / "changed")
        lint_env["LINT_CHANGED_ONLY"] = "1"
        missing = shell()
        assert missing.returncode == 1, missing.stdout + missing.stderr
        assert "[docString]" in missing.stdout and "#check TauCeti.undocumentedInside" in missing.stdout, missing.stdout + missing.stderr
        assert "driver failure" not in missing.stdout, missing.stdout + missing.stderr
        # Deleted modules leave a valid empty scope, calibrated by an imported docstring.
        (root / "changed").write_text("TauCeti.Deleted\n")
        empty = shell()
        assert empty.returncode == 0, empty.stdout + empty.stderr
        assert "0 changed module(s)" in empty.stdout, empty.stdout + empty.stderr
        # Suppressions are filtered by defining module, not import reachability.
        upstream = root / "TauCeti/Upstream.lean"
        upstream.write_text(upstream.read_text() +
                           "\nattribute [nolint unusedArguments] TauCeti.upstreamUnused\n")
        build()
        (root / "changed").write_text("TauCeti.Changed\n")
        fast = shell()
        assert fast.returncode == 0, fast.stdout + fast.stderr
        lint_env.pop("LINT_CHANGED_ONLY")
        lint_env.pop("LINT_ONLY_MODULES")
        full = shell()
        assert full.returncode == 1 and "unaccounted '@[nolint]'" in full.stdout, full.stdout + full.stderr
        changed.write_text(changed.read_text() +
                           "\nattribute [nolint unusedArguments] TauCeti.choose\n")
        build()
        lint_env["LINT_CHANGED_ONLY"] = "1"
        lint_env["LINT_ONLY_MODULES"] = str(root / "changed")
        suppression = shell()
        assert suppression.returncode == 1, suppression.stdout + suppression.stderr
        assert "unaccounted '@[nolint]'" in suppression.stdout, suppression.stdout + suppression.stderr
        assert "unusedArguments TauCeti.choose" in suppression.stdout, suppression.stdout + suppression.stderr
        print("Real Lean PR/full lint scope, docstrings and suppression regressions passed")


if __name__ == "__main__":
    main()
