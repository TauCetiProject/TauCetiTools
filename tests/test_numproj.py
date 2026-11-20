#!/usr/bin/env python3
"""Numeric-projection rewriting (`tauceti-tools numproj`).

The text rewriting runs everywhere. Set LEAN to an installed toolchain to also compile the scanner
and run it on a core-only fixture."""

import os
import runpy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tauceti_tools/assets/scripts/numproj_fix.py"
LEAN = os.environ.get("LEAN") or shutil.which("lean")
tool = runpy.run_path(str(SCRIPT))


class Rewrite(unittest.TestCase):
    def test_replaces_digits_with_field(self):
        line = "  exact ⟨x, h ▸ x.2⟩"
        self.assertEqual(tool["replace_proj"](line, line.index("2⟩"), "property"), "  exact ⟨x, h ▸ x.property⟩")

    def test_chains_rewrite_right_to_left(self):
        line = "x.2.1"
        line = tool["replace_proj"](line, 4, "left")
        self.assertEqual(tool["replace_proj"](line, 2, "property"), "x.property.left")

    def test_antiquotation_is_parenthesized(self):
        line = "congr($h.1 0 1)"
        self.assertEqual(tool["replace_proj"](line, line.index("1 0"), "val"), "congr(($h).val 0 1)")
        line = "congr($h₀.2)"
        self.assertEqual(tool["replace_proj"](line, line.index("2)"), "property"), "congr(($h₀).property)")

    def test_qualified_and_typed_antiquotations_are_parenthesized(self):
        line = "congr($h.eq.1)"
        self.assertEqual(tool["replace_proj"](line, line.index("1)"), "val"), "congr(($h.eq).val)")
        line = "congr($h:term.1)"
        self.assertEqual(tool["replace_proj"](line, line.index("1)"), "val"), "congr(($h:term).val)")

    def test_chained_antiquotation_projections(self):
        line = "congr($h.1.1)"
        self.assertEqual(tool["replace_projs"](line, [(9, "val"), (11, "val")]), "congr(($h).val.val)")

    def test_refuses_a_position_that_is_not_a_projection(self):
        with self.assertRaises(ValueError):
            tool["replace_proj"]("x + 1", 4, "val")

    def test_parse_findings_ignores_noise(self):
        found, conflicts = tool["parse_findings"]("A.lean\t3\t7\tval\nwarning: something\nA.lean\t3\t7\tval\n")
        self.assertEqual((dict(found), conflicts), ({"A.lean": {(3, 7, "val")}}, []))

    def test_parse_findings_reports_conflicts(self):
        found, conflicts = tool["parse_findings"]("A.lean\t3\t7\t\n")
        self.assertEqual((dict(found), conflicts), ({}, ["A.lean:3:8"]))

    def test_digits_and_parentheses_use_original_columns(self):
        line = "(x.1, congr($h.1))"
        edits = [(line.index("1,"), "val"), (line.index("1)"), "val")]
        self.assertEqual(tool["replace_projs"](line, edits), "(x.val, congr(($h).val))")
        line = "congr($a.1 + $b.2)"
        edits = [(line.index("1 "), "val"), (line.index("2)"), "property")]
        self.assertEqual(tool["replace_projs"](line, edits), "congr(($a).val + ($b).property)")

    def test_escaped_antiquotation_is_parenthesized(self):
        line = "congr($«h».1)"
        self.assertEqual(tool["replace_proj"](line, line.index("1)"), "val"), "congr(($«h»).val)")
        line = "congr($A.«b c».1)"
        self.assertEqual(tool["replace_proj"](line, line.index("1)"), "val"), "congr(($A.«b c»).val)")


class Wrap(unittest.TestCase):
    def check(self, line):
        pieces = tool["wrap"](line)
        self.assertIsNotNone(pieces)
        self.assertTrue(all(len(p) <= 100 for p in pieces))
        self.assertEqual(" ".join(p.strip() for p in pieces), line.strip())
        return pieces

    def test_prefers_comma_breaks_and_indents_deeper(self):
        line = "  rw [" + ", ".join(f"lemma_number_{i}" for i in range(9)) + "]"
        pieces = self.check(line)
        self.assertTrue(pieces[0].endswith(","))
        self.assertTrue(pieces[1].startswith("      "))

    def test_never_splits_inside_an_inline_by_block(self):
        line = (
            "  toFun x := ⟨s.powSaturationInfClosureToGroupClosure x, by rw [powSaturation_comap]; exact x.property.1⟩"
        )
        pieces = self.check(line)
        self.assertTrue(pieces[1].lstrip().startswith("by rw"))

    def test_never_splits_inside_a_structure_instance(self):
        line = (
            "  exact (LieSubmodule.lieSpan_le (N := ({ __ := N, lie_mem := fun {x _} hm => hstab x x.property _ hm } :"
        )
        pieces = self.check(line)
        self.assertIn("{ __ := N, lie_mem := fun {x _} hm => hstab x x.property _ hm }", pieces[1])

    def test_leaves_comments_and_unsplittable_lines(self):
        self.assertIsNone(tool["wrap"]("  -- " + "word " * 30))
        self.assertIsNone(tool["wrap"]("  exact " + "a" * 120))

    def test_ignores_spaces_in_strings(self):
        self.assertNotIn(10, tool["break_points"]('  s := "x y z w" ' + "b " * 50))

    def test_string_after_escaped_backslash_and_comment_quote(self):
        line = 'def f : String × String × Nat := ("\\\\", "w w w w w w w w w w w w w w w w w w w w w w w w w w", 0)'
        inside = line.index("w w") + 1
        self.assertNotIn(inside, tool["break_points"](line))
        line = 'def f : String × Nat := (/- " -/ "w w w w w w w w w w w w w w w w w w w w w w w w w w w w", 0)'
        self.assertNotIn(line.index("w w") + 1, tool["break_points"](line))

    def test_raw_strings_and_escaped_identifiers_are_not_code(self):
        line = 'def f : String × Nat := (r#"a " b c d e f g h i j k l m n o p q r s t u v w x y z"#, 0)'
        self.assertFalse(any(line.index("a ") < b < line.index('"#') for b in tool["break_points"](line)))
        line = "def f : Nat := «a b c d e f g h i j k l m n o p q r s t u v w x y z a b c d e f g» + 1"
        self.assertFalse(any(line.index("«") < b < line.index("»") for b in tool["break_points"](line)))

    def next_column(self, lines):
        return tool["next_code_column"](lines, tool["line_masks"](lines), 0)

    def test_continuation_check_skips_blank_lines_and_comments(self):
        self.assertEqual(self.next_column(["x", "", "  -- note", "/- block", "comment -/", "    exact trivial"]), 4)
        # code after a comment's close counts, at its own column
        self.assertEqual(self.next_column(["x", "  /- c -/ exact trivial"]), 10)
        # a nested comment ends at its matching close
        self.assertEqual(self.next_column(["x", "/- a /- b -/ still -/", "      exact trivial"]), 6)
        self.assertIsNone(self.next_column(["x", "-- end"]))

    def test_lines_beginning_inside_a_string_or_comment_are_not_wrapped(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "A.lean"
            tail = 'tail" ++ toString (foo ' + " ".join(["arg"] * 20) + " x.1)"
            path.write_text(f'def s : String := "head\n{tail}\n')
            count, unwrapped = tool["apply"]({str(path): {(2, tail.index(".1") + 1, "val")}})
            self.assertEqual((count, unwrapped), (1, [f"{path}:2"]))
            self.assertEqual(path.read_text().split("\n")[1], tail.replace("x.1", "x.val"))

    def test_whole_file_lexing_tracks_multiline_literals(self):
        masks = tool["line_masks"](['def s := r#"a', 'b "# ++ x', "/- c", "-/ y", "z -- w"])
        self.assertEqual([begins for _, begins in masks], [True, False, True, False, True])
        self.assertEqual(masks[1][0], [False] * 4 + [True] * 5)

    def test_term_let_and_have_blocks_are_protected(self):
        line = "def g_with_a_rather_long_name_here (x : {n : Nat // True}) : Nat := let y := x.val + x.val"
        self.assertIsNone(tool["wrap"](line, 4))

    def test_never_splits_after_an_inline_match_alternative(self):
        line = (
            "def f (s : Option {n : Nat // True}) : Nat := match s with | some y => y.val + y.val + y.val | none => 0"
        )
        alt = line.index("| some")
        self.assertTrue(all(b < alt for b in tool["break_points"](line)))

    def test_leaves_a_line_whose_block_continues_on_the_next_line(self):
        line = "theorem t_with_a_rather_long_name_here (x : {n : Nat // True}) : True := by have h := x.val"
        self.assertIsNone(tool["wrap"](line, 4))
        self.assertIsNotNone(tool["wrap"](line, 0))

    def test_apply_rewraps_only_touched_lines(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "A.lean"
            long_untouched = "-- " + "u" * 120
            touched = "  exact foo " + " ".join(["arg"] * 21) + " x.1"
            path.write_text(f"{long_untouched}\n{touched}\n")
            count, unwrapped = tool["apply"]({str(path): {(2, touched.index(".1") + 1, "val")}})
            self.assertEqual((count, unwrapped), (1, []))
            lines = path.read_text().split("\n")
            self.assertEqual(lines[0], long_untouched)
            self.assertTrue(lines[2].endswith("x.val"))
            self.assertTrue(all(len(line) <= 100 for line in lines[1:]))


@unittest.skipUnless(LEAN, "set LEAN to run the compiled scanner")
class Scanner(unittest.TestCase):
    def test_flags_subtype_projections_but_not_exempt_structures(self):
        with tempfile.TemporaryDirectory() as name:
            work = Path(name)
            source = work / "Fixture.lean"
            source.write_text(
                "def f (x : {n : Nat // 0 < n}) : Nat := x.1\n"
                "theorem g (x : {n : Nat // 0 < n}) : 0 < x.1 := x.2\n"
                "def h (p : Nat × Nat) : Nat := p.1\n"
                "theorem k (a b : Prop) (h : a ∧ b) : a := h.1\n"
            )
            prefix = Path(subprocess.check_output([LEAN, "--print-prefix"], text=True).strip())
            cache = work / "cache"
            cache.mkdir(mode=0o700)
            with patch.dict(
                os.environ, TAUCETI_NUMPROJ_CACHE=str(cache), PATH=f"{prefix / 'bin'}:{os.environ['PATH']}"
            ):
                binary = tool["scanner"]()
                env = dict(os.environ, LEAN_PATH="")
                out = subprocess.run(
                    [str(binary), *tool["OPTIONS"], str(source)], capture_output=True, text=True, env=env, check=True
                ).stdout
            found = tool["parse_findings"](out)[0][str(source)]
            lines = source.read_text().split("\n")
            expected = {
                (1, lines[0].index("x.1") + 2, "val"),
                (2, lines[1].index("x.1") + 2, "val"),
                (2, lines[1].index("x.2") + 2, "property"),
            }
            self.assertEqual(found, expected)

    def test_reports_projections_whose_elaborations_disagree(self):
        with tempfile.TemporaryDirectory() as name:
            work = Path(name)
            source = work / "Macro.lean"
            source.write_text(
                "structure S where\n  first : Nat\n"
                "structure T where\n  second : Nat\n"
                'macro "both " t:term : term => `(Prod.mk (($t) : S → Nat) (($t) : T → Nat))\n'
                "def f := both (·.1)\n"
            )
            prefix = Path(subprocess.check_output([LEAN, "--print-prefix"], text=True).strip())
            cache = work / "cache"
            cache.mkdir(mode=0o700)
            with patch.dict(
                os.environ, TAUCETI_NUMPROJ_CACHE=str(cache), PATH=f"{prefix / 'bin'}:{os.environ['PATH']}"
            ):
                binary = tool["scanner"]()
            result = subprocess.run(
                [str(binary), *tool["OPTIONS"], str(source)],
                capture_output=True,
                text=True,
                env=dict(os.environ, LEAN_PATH=""),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            found, conflicts = tool["parse_findings"](result.stdout)
            column = source.read_text().split("\n")[5].index("1)") + 1
            self.assertEqual((dict(found), conflicts), ({}, [f"{source}:6:{column}"]))

    def test_reports_elaboration_errors_as_incomplete(self):
        with tempfile.TemporaryDirectory() as name:
            work = Path(name)
            source = work / "Bad.lean"
            source.write_text("def bad : Nat := does_not_exist\ndef f (x : {n : Nat // 0 < n}) : Nat := x.1\n")
            prefix = Path(subprocess.check_output([LEAN, "--print-prefix"], text=True).strip())
            cache = work / "cache"
            cache.mkdir(mode=0o700)
            with patch.dict(
                os.environ, TAUCETI_NUMPROJ_CACHE=str(cache), PATH=f"{prefix / 'bin'}:{os.environ['PATH']}"
            ):
                binary = tool["scanner"]()
            result = subprocess.run(
                [str(binary), *tool["OPTIONS"], str(source)],
                capture_output=True,
                text=True,
                env=dict(os.environ, LEAN_PATH=""),
            )
            self.assertEqual(result.returncode, tool["INCOMPLETE"])
            self.assertIn("does_not_exist", result.stderr)
            self.assertEqual(tool["parse_findings"](result.stdout)[0][str(source)], {(2, 42, "val")})


class Command(unittest.TestCase):
    def test_options_reach_the_script(self):
        from tauceti_tools import cli

        with patch("subprocess.call", return_value=0) as call:
            self.assertEqual(cli.main(["numproj", "--jobs", "2", "--base", "HEAD"]), 0)
        self.assertEqual(call.call_args.args[0][-4:], ["--jobs", "2", "--base", "HEAD"])

    def test_changed_files_keep_spaces_and_renames(self):
        with tempfile.TemporaryDirectory() as name:
            repo = Path(name)
            git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run([*git, "init", "-q"], check=True)
            (repo / "Old.lean").write_text("def a := 1\n" * 20)
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-qm", "base"], check=True)
            subprocess.run([*git, "mv", "Old.lean", "New.lean"], check=True)
            (repo / "New.lean").write_text("def a := 1\n" * 20 + "def b := 2\n")
            (repo / "With space.lean").write_text("def c := 3\n")
            subprocess.run([*git, "add", "-A"], check=True)
            cwd = os.getcwd()
            try:
                os.chdir(repo)
                self.assertEqual(sorted(tool["changed_files"]("HEAD")), ["New.lean", "With space.lean"])
                subprocess.run([*git, "commit", "-qm", "change"], check=True)
                self.assertEqual(sorted(tool["changed_files"]("HEAD~1")), ["New.lean", "With space.lean"])
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
