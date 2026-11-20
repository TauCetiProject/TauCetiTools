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

    def test_receiver_span_is_parenthesized(self):
        line = "congr($h.1 0 1)"
        self.assertEqual(tool["replace_proj"](line, 9, "val", (6, 8)), "congr(($h).val 0 1)")
        line = "congr($h:term.1)"
        self.assertEqual(tool["replace_proj"](line, 14, "val", (6, 13)), "congr(($h:term).val)")
        self.assertEqual(tool["replace_proj"]("f s.1 0", 4, "val", (2, 3)), "f (s).val 0")
        self.assertEqual(tool["replace_proj"]("f ℘s.1", 5, "val", (2, 4)), "f (℘s).val")
        self.assertEqual(tool["replace_proj"]("f «a.1».1", 8, "val", (2, 7)), "f («a.1»).val")

    def test_chained_projections_share_one_span(self):
        line = "congr($h.1.1)"
        edits = [(9, "val", (6, 8)), (11, "val", (6, 8))]
        self.assertEqual(tool["replace_projs"](line, edits), "congr(($h).val.val)")
        self.assertEqual(
            tool["replace_projs"]("f N.s.1.2", [(6, "val", (2, 5)), (8, "property", (2, 5))]), "f (N.s).val.property"
        )

    def test_refuses_a_span_that_is_not_a_receiver(self):
        with self.assertRaises(ValueError):
            tool["replace_proj"]("f s.1", 4, "val", (0, 2))

    def test_refuses_a_position_that_is_not_a_projection(self):
        with self.assertRaises(ValueError):
            tool["replace_proj"]("x + 1", 4, "val")

    def test_parse_findings_ignores_noise(self):
        found, declined = tool["parse_findings"]("A.lean\t3\t7\tval\nwarning: something\nA.lean\t3\t7\tval\n")
        self.assertEqual((dict(found), declined), ({"A.lean": {(3, 7, "val", None)}}, []))

    def test_parse_findings_reads_spans_and_declines(self):
        found, declined = tool["parse_findings"]("A.lean\t3\t7\tval\t2:6\nA.lean\t4\t1\t?\nA.lean\t5\t1\t!\t`T.val`\n")
        self.assertEqual(dict(found), {"A.lean": {(3, 7, "val", (2, 6))}})
        self.assertEqual([where for where, _ in declined], ["A.lean:4:2", "A.lean:5:2"])

    def test_digits_and_parentheses_use_original_columns(self):
        line = "(x.1, congr($h.1))"
        edits = [(line.index("1,"), "val"), (line.index("1)"), "val", (12, 14))]
        self.assertEqual(tool["replace_projs"](line, edits), "(x.val, congr(($h).val))")
        line = "congr($a.1 + $b.2)"
        edits = [(9, "val", (6, 8)), (line.index("2)"), "property", (13, 15))]
        self.assertEqual(tool["replace_projs"](line, edits), "congr(($a).val + ($b).property)")


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
        return tool["next_token_column"](lines, tool["line_lexes"](lines), 0)

    def test_continuation_check_skips_blank_lines_and_comments(self):
        self.assertEqual(self.next_column(["x", "", "  -- note", "/- block", "comment -/", "    exact trivial"]), 4)
        # code after a comment's close counts, at its own column
        self.assertEqual(self.next_column(["x", "  /- c -/ exact trivial"]), 10)
        # a nested comment ends at its matching close
        self.assertEqual(self.next_column(["x", "/- a /- b -/ still -/", "      exact trivial"]), 6)
        self.assertIsNone(self.next_column(["x", "-- end"]))
        # literals and escaped identifiers are tokens
        self.assertEqual(self.next_column(["x", '        "done"']), 8)
        self.assertEqual(self.next_column(["x", "        «y»"]), 8)
        # the continuation of a literal opened earlier is not
        self.assertEqual(self.next_column(["x", '  "a', 'b"', "   y"]), 2)
        self.assertEqual(self.next_column(['x "a', 'b"', "   y"]), 3)

    def test_lines_beginning_inside_a_string_or_comment_are_not_wrapped(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "A.lean"
            tail = 'tail" ++ toString (foo ' + " ".join(["arg"] * 20) + " x.1)"
            path.write_text(f'def s : String := "head\n{tail}\n')
            count, unwrapped = tool["apply"]({str(path): {(2, tail.index(".1") + 1, "val", None)}})
            self.assertEqual((count, unwrapped), (1, [f"{path}:2"]))
            self.assertEqual(path.read_text().split("\n")[1], tail.replace("x.1", "x.val"))

    def test_whole_file_lexing_tracks_multiline_literals(self):
        lexes = tool["line_lexes"](['def s := r#"a', 'b "# ++ x', "/- c", "-/ y", "z -- w", "«a", "b».1"])
        self.assertEqual([begins for _, _, begins in lexes], [True, False, True, False, True, True, False])
        self.assertEqual(lexes[1][0], ["s"] * 4 + ["c"] * 5)

    def test_statement_sequences_are_not_wrapped(self):
        line = "  let " + "n" * 86 + " := x.val; return " + "n" * 86
        self.assertIsNone(tool["wrap"](line))

    def test_nested_tactics_are_not_wrapped(self):
        args = " ".join(["x.val"] * 16)
        for head in ("try exact F", "all_goals exact F", "case _ => exact F", "· try exact F", "exact F <;> simp"):
            with self.subTest(head=head):
                self.assertIsNone(tool["wrap"](f"  {head} {args}"))
        for head in ("show_term exact F", "set_option maxHeartbeats 400 in exact F", "· /- c -/ try exact F"):
            with self.subTest(head=head):
                self.assertIsNone(tool["wrap"](f"  {head} {args}"))
        self.assertIsNotNone(tool["wrap"](f"  · exact F {args}"))
        for head in ("·   exact F", "· · exact F"):
            pieces = tool["wrap"](f"  {head} {args}")
            column = pieces[0].index("exact")
            self.assertTrue(all(len(p) - len(p.lstrip()) > column for p in pieces[1:]), pieces)

    def test_an_inline_by_body_is_split_at_most_once(self):
        line = "  let y := (by exact F " + " ".join(["x.val"] * 30) + "); return y"
        self.assertIsNone(tool["wrap"](line))
        line = "  have h : P := by exact F " + " ".join(["x.val"] * 30)
        self.assertIsNone(tool["wrap"](line))

    def test_interpolated_strings_are_not_wrapped(self):
        line = '  s!"{x.val} {"' + " ".join(["word"] * 25) + '"}"'
        self.assertIsNone(tool["wrap"](line))

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
            count, unwrapped = tool["apply"]({str(path): {(2, touched.index(".1") + 1, "val", None)}})
            self.assertEqual((count, unwrapped), (1, []))
            lines = path.read_text().split("\n")
            self.assertEqual(lines[0], long_untouched)
            self.assertTrue(lines[2].endswith("x.val"))
            self.assertTrue(all(len(line) <= 100 for line in lines[1:]))


@unittest.skipUnless(LEAN, "set LEAN to run the compiled scanner")
class Scanner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        prefix = Path(subprocess.check_output([LEAN, "--print-prefix"], text=True).strip())
        cache = Path(cls.work.name) / "cache"
        cache.mkdir(mode=0o700)
        with patch.dict(os.environ, TAUCETI_NUMPROJ_CACHE=str(cache), PATH=f"{prefix / 'bin'}:{os.environ['PATH']}"):
            cls.binary = tool["scanner"]()

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def scan(self, text, name="Fixture.lean"):
        """Scanner status, findings and declines for a file with `text`, and its path."""
        source = Path(self.work.name) / name
        source.write_text(text)
        result = subprocess.run(
            [str(self.binary), *tool["OPTIONS"], str(source)],
            capture_output=True,
            text=True,
            env=dict(os.environ, LEAN_PATH=""),
        )
        found, declined = tool["parse_findings"](result.stdout)
        return result, found.get(str(source), set()), [where for where, _ in declined], source

    def rewrite(self, text):
        """The file after scanning and rewriting `text`, after checking that it still compiles."""
        result, _, declined, source = self.scan(text)
        self.assertEqual((result.returncode, declined), (0, []), result.stderr)
        tool["apply"](tool["parse_findings"](result.stdout)[0])
        check = subprocess.run([LEAN, str(source)], capture_output=True, text=True, env=dict(os.environ, LEAN_PATH=""))
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        return source.read_text()

    def test_flags_subtype_projections_but_not_exempt_structures(self):
        text = (
            "def f (x : {n : Nat // 0 < n}) : Nat := x.1\n"
            "theorem g (x : {n : Nat // 0 < n}) : 0 < x.1 := x.2\n"
            "def h (p : Nat × Nat) : Nat := p.1\n"
            "theorem k (a b : Prop) (h : a ∧ b) : a := h.1\n"
        )
        result, found, declined, _ = self.scan(text)
        lines = text.split("\n")
        expected = {
            (1, lines[0].index("x.1") + 2, "val", None),
            (2, lines[1].index("x.1") + 2, "val", None),
            (2, lines[1].index("x.2") + 2, "property", None),
        }
        self.assertEqual((result.returncode, found, declined), (0, expected, []))

    def test_reports_projections_whose_elaborations_disagree(self):
        text = (
            "structure S where\n  first : Nat\n"
            "structure T where\n  second : Nat\n"
            'macro "both " t:term : term => `(Prod.mk (($t) : S → Nat) (($t) : T → Nat))\n'
            "def f := both (·.1)\n"
        )
        result, found, declined, source = self.scan(text)
        column = text.split("\n")[5].index("1)") + 1
        self.assertEqual((result.returncode, found, declined), (0, set(), [f"{source}:6:{column}"]))

    def test_parenthesizes_receivers_a_global_name_would_capture(self):
        prelude = "def s : {n : Nat // True} := ⟨7, trivial⟩\n"
        cases = [
            # a global `s.val`
            ("def s.val : Nat := 42\ndef a : Nat := s.1\n", "def a : Nat := (s).val"),
            # through a namespace that is open
            (
                "namespace N\ndef s.val : Nat := 42\nend N\nopen N in def a : Nat := s.1\n",
                "open N in def a : Nat := (s).val",
            ),
            # a trailing component: `s.val.succ` names a global even though `s.val` does not
            ("def s.val.succ : Nat := 42\ndef a : Nat := s.1.succ\n", "def a : Nat := (s).val.succ"),
            # a non-ASCII identifier
            ("def ℘s := s\ndef ℘s.val : Nat := 42\ndef a : Nat := ℘s.1\n", "def a : Nat := (℘s).val"),
            # an escaped identifier containing `.1`
            ("def «s.1» := s\ndef «s.1».val : Nat := 42\ndef a : Nat := «s.1».1\n", "def a : Nat := («s.1»).val"),
            # only one of a macro's elaborations sees the capturing global
            (
                "namespace N\ndef s.val : Nat := 42\nend N\n"
                'macro "both " t:term : term => `(Prod.mk $t (open N in $t))\n'
                "def a : Nat × Nat := both s.1\n",
                "def a : Nat × Nat := both (s).val",
            ),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                lines = self.rewrite(prelude + body + "example : a = a := rfl\n").split("\n")
                self.assertIn(expected, lines)
        lines = self.rewrite(prelude + "def b (t : {n : Nat // True}) : Nat := t.1\n").split("\n")
        self.assertIn("def b (t : {n : Nat // True}) : Nat := t.val", lines)

    def test_values_are_unchanged_by_parenthesized_rewrites(self):
        text = "def s : {n : Nat // True} := ⟨7, trivial⟩\ndef s.val.succ : Nat := 42\ndef a : Nat := s.1.succ\n"
        rewritten = self.rewrite(text + "example : a = 8 := rfl\n")
        self.assertIn("(s).val.succ", rewritten)

    def test_declines_a_field_a_type_synonym_redefines(self):
        text = (
            "def T := {n : Nat // True}\n"
            "def T.val (_ : T) : Nat := 42\n"
            "def a (x : T) : Nat := x.1\n"
            "def b (x : {n : Nat // True}) : Nat := x.1\n"
        )
        result, found, declined, source = self.scan(text)
        lines = text.split("\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(found, {(4, lines[3].index("x.1") + 2, "val", None)})
        self.assertEqual(declined, [f"{source}:3:{lines[2].index('x.1') + 3}"])

    def test_declines_synonym_fields_lean_would_find(self):
        cases = [
            # a private method
            "def T := {n : Nat // True}\nprivate def T.val (_ : T) : Nat := 42\ndef a (x : T) : Nat := x.1\n",
            # only one of a macro's elaborations has the synonym type
            "def T := {n : Nat // True}\ndef T.val (_ : T) : Nat := 42\n"
            'macro "both " t:term : term => `(Prod.mk (($t) : {n : Nat // True} → Nat) (($t) : T → Nat))\n'
            "def a := both (·.1)\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                result, found, declined, source = self.scan(text)
                self.assertEqual((result.returncode, found), (0, set()), result.stderr)
                self.assertEqual(len(declined), 1)

    def test_parenthesizes_receivers_a_local_name_would_capture(self):
        text = (
            "def f (x : {n : Nat // True}) : Nat := by\n  let x.val := 42\n  exact x.1\n"
            "example : f ⟨7, trivial⟩ = 7 := rfl\n"
        )
        self.assertIn("  exact (x).val", self.rewrite(text))

    def test_reports_elaboration_errors_as_incomplete(self):
        result, found, _, _ = self.scan(
            "def bad : Nat := does_not_exist\ndef f (x : {n : Nat // 0 < n}) : Nat := x.1\n"
        )
        self.assertEqual(result.returncode, tool["INCOMPLETE"])
        self.assertIn("does_not_exist", result.stderr)
        self.assertEqual(found, {(2, 42, "val", None)})


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
