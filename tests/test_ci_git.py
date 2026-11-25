"""Real Git graphs exercise CI identity validation and hostile-checkout isolation."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).parents[1] / "tauceti_tools/assets/scripts/ci_git.py"
spec = importlib.util.spec_from_file_location("ci_git", SCRIPT)
ci_git = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_git)


class CiGitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "upstream"
        self.repo.mkdir()
        self.git("init", "--quiet", "-b", "main")
        self.git("config", "user.name", "CI Test")
        self.git("config", "user.email", "ci@example.invalid")
        (self.repo / "lean-toolchain").write_text("leanprover/lean4:v4.23.0\n")
        (self.repo / "lake-manifest.json").write_text("{}\n")
        self.base = self.commit("base")
        self.git("checkout", "--quiet", "-b", "feature")
        (self.repo / "feature").write_text("new work\n")
        self.head = self.commit("head")
        self.git("branch", "staging/ci/batch-1")
        self.git("checkout", "--quiet", "main")
        (self.repo / "main-only").write_text("base advanced\n")
        self.tip = self.commit("main advance")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def resolve(self, **overrides):
        args = dict(repository="Owner/Repo", head_repository="Owner/Repo", head=self.head,
                    base_ref="main", store=self.root / "store.git")
        args.update(overrides)
        real_url = ci_git.github_url
        def local_url(repository):
            real_url(repository)
            return str(self.repo if repository == "Owner/Repo" else self.root / "fork")
        with patch.object(ci_git, "github_url", side_effect=local_url):
            return ci_git.resolve(**args)

    def test_base_captured_once_and_exact_merge_base(self):
        result = self.resolve()
        self.assertEqual(result, dict(base_sha=self.tip, mergebase=self.base, staging_sha=""))
        # The later moving main branch does not change the captured identity.
        self.git("reset", "--hard", self.base)
        self.assertEqual(result["base_sha"], self.tip)

    def test_lint_metadata_uses_the_exact_local_graph(self):
        result = ci_git.lint_metadata(self.repo, self.tip, self.head)
        self.assertEqual(result, {"merge_base_commit": {"sha": self.base},
                                 "total_commits": 1,
                                 "commits": [{"commit": {"message": "head"}}]})
        self.git("checkout", "--quiet", "feature")
        (self.repo / "next").write_text("another change\n")
        next_head = self.commit("another change (#7)\ncontinued paragraph (#8)\n\nbody")
        self.assertEqual(ci_git.lint_metadata(self.repo, self.base, next_head)["commits"],
                         [{"commit": {"message": "another change (#7)"}},
                          {"commit": {"message": "head"}}])

    def test_lint_metadata_rejects_unavailable_history(self):
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            ci_git.lint_metadata(self.repo, "f" * 40, self.head)
        self.git("checkout", "--quiet", "--orphan", "unrelated")
        unrelated = self.commit("unrelated root")
        with self.assertRaises(subprocess.CalledProcessError):
            ci_git.lint_metadata(self.repo, unrelated, self.head)

    def test_lint_metadata_accepts_non_utf8_commit_text(self):
        command = ["git", "-C", str(self.repo)]
        commit = subprocess.check_output(command + ["cat-file", "commit", self.head])
        content = commit.split(b"\n\n", 1)[0] + b"\n\nchange \xff (#7)\n"
        head = subprocess.check_output(command + ["hash-object", "-t", "commit", "-w", "--stdin"],
                                       input=content).decode().strip()
        metadata = ci_git.lint_metadata(self.repo, self.base, head)
        self.assertEqual(metadata["total_commits"], 1)
        self.assertTrue(metadata["commits"][0]["commit"]["message"].endswith(" (#7)"))

    def test_staging_exact_sha_and_ancestor(self):
        self.assertEqual(self.resolve(base_ref=self.base, staging_ref="staging/ci/batch-1"),
                         dict(base_sha=self.base, mergebase=self.base, staging_sha=self.head))

    def test_staging_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "differs"):
            self.resolve(head=self.base, base_ref=self.base, staging_ref="staging/ci/batch-1")

    def test_staging_equal_or_diverged_base_is_rejected(self):
        for index, base in enumerate((self.head, self.tip)):
            with self.subTest(base=base), self.assertRaisesRegex(ValueError, "strict ancestor"):
                self.resolve(base_ref=base, staging_ref="staging/ci/batch-1", store=self.root / f"reject-{index}")

    def test_fork_head_uses_fork_objects_and_upstream_base(self):
        fork = self.root / "fork"
        subprocess.run(["git", "clone", "--quiet", str(self.repo), str(fork)], check=True, stderr=subprocess.PIPE)
        subprocess.run(["git", "-C", str(fork), "checkout", "--quiet", "feature"], check=True)
        subprocess.run(["git", "-C", str(fork), "-c", "user.name=Fork", "-c", "user.email=fork@example.invalid",
                        "commit", "--quiet", "--allow-empty", "-m", "fork-only head"], check=True)
        fork_head = subprocess.check_output(["git", "-C", str(fork), "rev-parse", "HEAD"]).decode().strip()
        self.assertEqual(self.resolve(head_repository="Fork/Repo", head=fork_head)["mergebase"], self.base)

    def test_bad_repositories_refs_and_ids_fail_closed(self):
        for repository in ("/tmp/evil", "../Repo", "Owner/Repo\n--upload-pack=evil", "https://evil.invalid/repo"):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                ci_git.github_url(repository)
        for index, branch in enumerate(("--upload-pack=evil", "/tmp/root", "main\nrefs/evil", "main:refs/evil", "../evil", "HEAD")):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                self.resolve(base_ref=branch, store=self.root / f"bad-{index}")
        for value in ("main", self.head + "\n", "0" * 40, self.head + ":file"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ci_git.sha(value)

    def test_blob_cannot_be_commit_identity(self):
        blob = self.git("rev-parse", "HEAD:lean-toolchain")
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.resolve(head=blob)

    def test_existing_store_refused(self):
        with self.assertRaisesRegex(ValueError, "must be new"):
            self.resolve(store=self.repo)

    def test_tree_entry_ignores_candidate_config_replacements_and_environment(self):
        original = ci_git.tree_entry(self.repo, self.head, "lean-toolchain")
        metadata = ci_git.lint_metadata(self.repo, self.tip, self.head)
        self.git("replace", self.head, self.tip)
        marker = self.root / "hook-ran"
        hook = self.repo / ".git/hooks/post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
        malicious = self.root / "config"
        malicious.write_text("[core]\n hooksPath=/does/not/exist\n[alias]\n ls-tree=!exit 42\n")
        self.git("config", "core.repositoryFormatVersion", "999")
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(malicious), "GIT_DIR": "/bad",
                                     "GIT_OBJECT_DIRECTORY": "/bad", "GIT_CONFIG_COUNT": "1",
                                     "GIT_CONFIG_KEY_0": "alias.ls-tree", "GIT_CONFIG_VALUE_0": "!exit 42"}):
            self.assertEqual(ci_git.tree_entry(self.repo, self.head, "lean-toolchain"), original)
            self.assertEqual(ci_git.lint_metadata(self.repo, self.tip, self.head), metadata)
        self.assertTrue(original.startswith("100644 blob "))
        self.assertFalse(marker.exists())

    def test_symlink_mode_is_preserved_for_guard(self):
        (self.repo / "lean-toolchain").unlink()
        (self.repo / "lean-toolchain").symlink_to("somewhere")
        head = self.commit("symlink")
        self.assertTrue(ci_git.tree_entry(self.repo, head, "lean-toolchain").startswith("120000 blob "))

    def test_missing_pin_is_empty_and_unsafe_paths_rejected(self):
        (self.repo / "lean-toolchain").unlink()
        head = self.commit("delete pin")
        self.assertEqual(ci_git.tree_entry(self.repo, head, "lean-toolchain"), "")
        for path in ("/etc/passwd", "../lean-toolchain", "lean-toolchain\n", ":(top)*"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                ci_git.tree_entry(self.repo, head, path)

    def test_asset_copies_match(self):
        self.assertEqual(SCRIPT.read_bytes(), (SCRIPT.parents[1] / "project/scripts/ci_git.py").read_bytes())



class ReviewSizeTests(unittest.TestCase):
    """PR CI and the staging member policy share this measure, so it must match GitHub's PR totals."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet", "-b", "main")
        self.git("config", "user.name", "CI Test")
        self.git("config", "user.email", "ci@example.invalid")
        (self.repo / "lean-toolchain").write_text("leanprover/lean4:v4.23.0\n")
        (self.repo / "lake-manifest.json").write_text("{}\n")
        (self.repo / "TauCeti").mkdir()
        # Distinct lines keep similarity meaningful for rename detection.
        self.write("TauCeti/Brenier.lean", 1200)
        self.write("TauCeti/Other.lean", 10)
        self.base = self.commit("base")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def write(self, path, lines, start=0):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(f"theorem t{start + i} : {i} = {i} := rfl\n" for i in range(lines)))

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "--quiet", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def measure(self, head=None):
        with ci_git.object_reader(self.repo) as git:
            return ci_git.review_size(git, self.base, head or self.commit("head"))

    def test_pure_rename_counts_one_file_and_no_lines(self):
        self.git("mv", "TauCeti/Other.lean", "TauCeti/Renamed.lean")
        size = self.measure()
        self.assertEqual((size["additions"], size["deletions"], size["files"]), (0, 0, 1))
        self.assertEqual(size["new_lean_files"], [])
        self.assertFalse(size["too_large"])

    def test_rename_with_edits_counts_only_the_edits(self):
        self.git("mv", "TauCeti/Brenier.lean", "TauCeti/Moved.lean")
        lines = (self.repo / "TauCeti/Moved.lean").read_text().splitlines(keepends=True)
        lines[5] = "-- edited\n"
        (self.repo / "TauCeti/Moved.lean").write_text("".join(lines) + "-- appended\n")
        size = self.measure()
        self.assertEqual((size["additions"], size["deletions"], size["files"]), (2, 1, 1))
        self.assertEqual(size["new_files_too_long"], [])

    def test_move_into_new_directory_with_new_sibling_matches_github(self):
        # The PR 40160 shape: Brenier.lean becomes Brenier/Basic.lean beside new files.
        self.git("mv", "TauCeti/Brenier.lean", "TauCeti/Brenier.lean.tmp")
        (self.repo / "TauCeti/Brenier").mkdir()
        self.git("mv", "TauCeti/Brenier.lean.tmp", "TauCeti/Brenier/Basic.lean")
        lines = (self.repo / "TauCeti/Brenier/Basic.lean").read_text().splitlines(keepends=True)
        (self.repo / "TauCeti/Brenier/Basic.lean").write_text("".join(lines[:1000] + ["-- moved\n"]))
        self.write("TauCeti/Brenier/Dual.lean", 900, start=5000)
        size = self.measure()
        self.assertEqual((size["additions"], size["deletions"], size["files"]), (901, 200, 2))
        self.assertEqual(size["new_lean_files"], [("TauCeti/Brenier/Dual.lean", 900)])
        self.assertFalse(size["too_large"])
        self.assertEqual(size["new_files_too_long"], [])

    def test_binary_files_count_as_files_with_zero_lines(self):
        (self.repo / "TauCeti/Data.lean").write_bytes(b"\0binary\n" * 3000)
        size = self.measure()
        self.assertEqual((size["additions"], size["deletions"], size["files"]), (0, 0, 1))
        self.assertEqual(size["new_lean_files"], [("TauCeti/Data.lean", 0)])

    def test_line_caps_and_lake_pin_exemption(self):
        self.write("TauCeti/Other.lean", ci_git.REVIEW_ADDED_LINE_CAP, start=9000)
        size = self.measure()
        self.assertEqual((size["additions"], size["deletions"]), (3000, 10))
        self.assertFalse(size["too_large"])
        self.write("TauCeti/Other.lean", 3001, start=9000)
        size = self.measure()
        self.assertEqual((size["additions"], size["deletions"]), (3001, 10))
        self.assertFalse(size["pins_changed"])
        self.assertTrue(size["too_large"])
        (self.repo / "lean-toolchain").write_text("leanprover/lean4:v4.24.0\n")
        size = self.measure()
        self.assertTrue(size["pins_changed"])
        self.assertFalse(size["too_large"])

    def test_removed_line_cap_is_separate_and_lower(self):
        self.write("TauCeti/Old.lean", 2001, start=20000)
        self.base = self.commit("base with a long file")
        (self.repo / "TauCeti/Old.lean").unlink()
        size = self.measure()
        self.assertEqual((size["additions"], size["deletions"]), (0, 2001))
        self.assertTrue(size["too_large"])
        self.write("TauCeti/Old.lean", 1, start=20000)
        size = self.measure()
        self.assertEqual((size["additions"], size["deletions"]), (0, 2000))
        self.assertFalse(size["too_large"])

    def test_large_refactor_lifts_the_three_caps_but_not_the_new_file_cap(self):
        for index in range(ci_git.REVIEW_FILE_CAP):
            (self.repo / "TauCeti" / f"F{index}.lean").write_text(f"-- {index}\n")
        self.write("TauCeti/Long.lean", 2001, start=8000)
        head = self.commit("head")
        with ci_git.object_reader(self.repo) as git:
            plain = ci_git.pr_guards(git, self.base, head, scope=False)
            exempt = ci_git.pr_guards(git, self.base, head, scope=False, large_refactor=True)
        self.assertTrue(plain["too_large"])
        self.assertFalse(exempt["too_large"])
        self.assertTrue(exempt["large_refactor"])
        self.assertEqual(exempt["violations"], [
            "TauCeti/Long.lean: new Lean files must be at most 1250 lines (found 2001). "
            "Split it, or grow an existing file (allowed up to 1500)."])

    def test_file_cap_applies_to_pin_bumps(self):
        (self.repo / "lean-toolchain").write_text("leanprover/lean4:v4.24.0\n")
        for index in range(ci_git.REVIEW_FILE_CAP):
            (self.repo / "TauCeti" / f"F{index}.lean").write_text(f"-- {index}\n")
        size = self.measure()
        self.assertEqual(size["files"], ci_git.REVIEW_FILE_CAP + 1)
        self.assertTrue(size["too_large"])

    def test_added_file_length_uses_line_count(self):
        self.write("TauCeti/Exact.lean", 1250, start=7000)
        self.write("TauCeti/Long.lean", 1251, start=8000)
        self.write("TauCeti/notes.txt", 1500, start=9000)
        size = self.measure()
        self.assertEqual(size["new_lean_files"], [("TauCeti/Exact.lean", 1250), ("TauCeti/Long.lean", 1251)])
        self.assertEqual(size["new_files_too_long"], [("TauCeti/Long.lean", 1251)])

    def test_unusual_filenames_and_cli(self):
        self.git("mv", "TauCeti/Other.lean", "TauCeti/Tab\tand\nnewline.lean")
        head = self.commit("head")
        output = subprocess.check_output(
            [sys.executable, str(SCRIPT), "review-size", "--checkout", str(self.repo), "--base", self.base, "--head", head]
        )
        size = json.loads(output)
        self.assertEqual((size["additions"], size["deletions"], size["files"]), (0, 0, 1))
        self.assertEqual(size["new_lean_files"], [])

    def guards(self, **kwargs):
        with ci_git.object_reader(self.repo) as git:
            return ci_git.pr_guards(git, self.base, self.commit("head"), **kwargs)

    def test_pr_guards_report_every_failed_guard_and_scope(self):
        self.assertEqual(self.guards()["violations"], [])
        self.write("TauCeti/Long.lean", 3001, start=8000)
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts/tool.py").write_text("print()\n")
        (self.repo / "lakefile.toml").write_text('name = "x"\n')
        (self.repo / "docbuild/docs").mkdir(parents=True)
        (self.repo / "docbuild/docs/references.bib").write_text("@book{x}\n")
        result = self.guards()
        self.assertEqual(result["out_of_scope"], ["lakefile.toml", "scripts/tool.py"])
        too_large, long_file, scope = result["violations"]
        self.assertIn("too large to review: +3004 / -0 lines across 4 files (cap: 3000 added lines, 2000 removed lines", too_large)
        self.assertIn("TauCeti/Long.lean: new Lean files must be at most 1250 lines (found 3001)", long_file)
        self.assertIn("lakefile.toml, scripts/tool.py", scope)
        self.assertEqual(len(self.guards(scope=False)["violations"]), 2)

    def test_scope_sees_both_sides_of_a_rename_out_of_the_library(self):
        self.git("mv", "TauCeti/Other.lean", "Other.lean")
        self.assertEqual(self.guards()["out_of_scope"], ["Other.lean"])
        self.assertEqual(ci_git.out_of_scope(["TauCeti.lean", "TauCeti/A.lean", "TauCetiX/A.lean"]),
                         ["TauCetiX/A.lean"])

    def test_pr_guards_and_scope_cli(self):
        self.write("TauCeti/Long.lean", 1251, start=8000)
        head = self.commit("head")
        output = subprocess.check_output(
            [sys.executable, str(SCRIPT), "pr-guards", "--checkout", str(self.repo), "--base", self.base, "--head", head]
        )
        result = json.loads(output)
        self.assertEqual(result["new_files_too_long"], [["TauCeti/Long.lean", 1251]])
        self.assertEqual(len(result["violations"]), 1)
        outside = subprocess.run(
            [sys.executable, str(SCRIPT), "scope"], input=b"TauCeti/A.lean\nlean-toolchain\n.github/x.yml\n",
            check=True, capture_output=True,
        ).stdout
        self.assertEqual(outside, b".github/x.yml\n")

    def forbidden(self):
        with ci_git.object_reader(self.repo) as git:
            return ci_git.forbidden_content(git, self.base, self.commit("head"))

    def test_text_content_guard_rejects_binary_and_non_utf8_content(self):
        self.assertEqual(self.forbidden(), [])
        (self.repo / "TauCeti/Nul.lean").write_bytes(b"theorem x : True := trivial\n\0\n")
        (self.repo / "TauCeti/Latin1.lean").write_bytes("-- café\n".encode("latin-1"))
        (self.repo / "web").mkdir()
        (self.repo / "web/logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR")
        (self.repo / "TauCeti/Unicode.lean").write_text("-- ∀ ε > 0, café\n", encoding="utf-8")
        (self.repo / "TauCeti/Other.lean").write_bytes(b"\xff\xfe-\0-\0\n\0")
        self.assertEqual(self.forbidden(), [
            ("TauCeti/Latin1.lean", "is not valid UTF-8"),
            ("TauCeti/Nul.lean", "contains a NUL byte"),
            ("TauCeti/Other.lean", "contains a NUL byte"),
            ("web/logo.png", "contains a NUL byte"),
        ])

    def test_text_content_guard_measures_only_added_and_modified_paths(self):
        (self.repo / "web").mkdir()
        (self.repo / "web/logo.png").write_bytes(b"\x89PNG\0")
        self.base = self.commit("binary already on the base")
        (self.repo / "TauCeti/Other.lean").write_text("-- edited\n")
        self.assertEqual(self.forbidden(), [])
        (self.repo / "web/logo.png").unlink()
        self.assertEqual(self.forbidden(), [])
        self.git("mv", "TauCeti/Brenier.lean", "TauCeti/Moved.lean")
        self.assertEqual(self.forbidden(), [])

    def test_text_content_guard_rejects_a_renamed_binary(self):
        (self.repo / "web").mkdir()
        (self.repo / "web/logo.png").write_bytes(b"\x89PNG\0" * 100)
        self.base = self.commit("binary already on the base")
        self.git("mv", "web/logo.png", "TauCeti/logo.png")
        self.assertEqual(self.forbidden(), [("TauCeti/logo.png", "contains a NUL byte")])

    def test_text_content_guard_allowlist_is_exact_and_empty(self):
        self.assertEqual(ci_git.TEXT_CONTENT_ALLOWLIST, frozenset())
        (self.repo / "web").mkdir()
        (self.repo / "web/logo.png").write_bytes(b"\x89PNG\0")
        (self.repo / "web/logo.png.txt").write_bytes(b"\0")
        with patch.object(ci_git, "TEXT_CONTENT_ALLOWLIST", frozenset({"web/logo.png"})):
            self.assertEqual(self.forbidden(), [("web/logo.png.txt", "contains a NUL byte")])

    def test_text_content_guard_rejects_lfs_pointers_submodules_and_symlinked_attributes(self):
        (self.repo / "TauCeti/Big.lean").write_text(
            "version https://git-lfs.github.com/spec/v1\noid sha256:" + "0" * 64 + "\nsize 12\n")
        os.symlink("TauCeti/Other.lean", self.repo / ".gitattributes")
        os.symlink("Other.lean", self.repo / "TauCeti/Link.lean")
        self.git("add", "--all")
        # A gitlink has no worktree directory, so `commit` (which adds everything) would drop it.
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.base},TauCeti/Sub")
        self.git("commit", "--quiet", "-m", "gitlink")
        with ci_git.object_reader(self.repo) as git:
            found = ci_git.forbidden_content(git, self.base, self.git("rev-parse", "HEAD"))
        self.assertEqual(found, [
            (".gitattributes", "is a symbolic link"),
            ("TauCeti/Big.lean", "is a Git LFS pointer"),
            ("TauCeti/Sub", "is a submodule (gitlink)"),
        ])

    def test_text_content_guard_rejects_diff_hiding_gitattributes(self):
        (self.repo / "TauCeti/.gitattributes").write_text(
            "# comment\n"
            "*.lean text eol=lf whitespace=trailing-space diff\n"
            "* text=auto\n"
            "*.md linguist-generated=false -linguist-generated\n"
            "*.olean binary\n"
            "Secret.lean -diff\n"
            "Hidden.lean !diff\n"
            "Driver.lean diff=hidden\n"
            "Raw.lean -text\n"
            "Union.lean merge=union\n"
            "Lfs.lean filter=lfs\n"
            "Enc.lean working-tree-encoding=UTF-16\n"
            "Gen.lean linguist-generated\n"
            "[attr]hide -diff -merge\n"
            '"Quoted name.lean" -text\n'
            '"Broken.lean -diff\n'
        )
        found = self.forbidden()
        self.assertEqual([reason for _, reason in found], [
            "line 10 sets `merge=union`",
            "line 11 sets `filter=lfs`",
            "line 12 sets `working-tree-encoding=UTF-16`",
            "line 13 sets `linguist-generated`",
            "line 14 defines an attribute macro",
            "line 15 sets `-text`",
            "line 16 is unreadable (unterminated quoted pattern)",
            "line 5 sets `binary`",
            "line 6 sets `-diff`",
            "line 7 sets `!diff`",
            "line 8 sets `diff=hidden`",
            "line 9 sets `-text`",
        ])
        self.assertEqual({path for path, _ in found}, {"TauCeti/.gitattributes"})

    def test_text_content_guard_rejects_control_characters(self):
        (self.repo / "TauCeti/Image.ppm").write_bytes(b"P6\n1 1\n255\n\x01\x02\x03")
        (self.repo / "TauCeti/Escape.lean").write_text("-- \x1b[2J\n")
        (self.repo / "TauCeti/C1.lean").write_text("-- \x85\n", encoding="utf-8")
        (self.repo / "TauCeti/Fine.lean").write_text("-- tab\there\r\n\x0c\n")
        self.assertEqual(self.forbidden(), [
            ("TauCeti/C1.lean", "contains the control character U+0085"),
            ("TauCeti/Escape.lean", "contains the control character U+001B"),
            ("TauCeti/Image.ppm", "contains the control character U+0001"),
        ])

    def test_lfs_pointer_variants_git_lfs_accepts(self):
        pointer = "oid sha256:" + "0" * 64 + "\nsize 12\n"
        (self.repo / "TauCeti/A.lean").write_text("\nversion https://git-lfs.github.com/spec/v1\n" + pointer)
        (self.repo / "TauCeti/B.lean").write_text("version https://hawser.github.com/spec/v1\n" + pointer)
        (self.repo / "TauCeti/C.lean").write_text("-- version https://git-lfs.github.com/spec/v1 is a URL\n")
        self.assertEqual(self.forbidden(), [
            ("TauCeti/A.lean", "is a Git LFS pointer"), ("TauCeti/B.lean", "is a Git LFS pointer")])

    def test_gitattributes_lookalike_names_and_git_whitespace(self):
        (self.repo / "TauCeti/.GitAttributes").write_text("* -diff\n")
        (self.repo / "web").mkdir()
        (self.repo / "web/.git‌attributes").write_text("* -diff\n")
        (self.repo / "web/.gitattributes.").write_text("* -diff\n")
        (self.repo / "docs").mkdir()
        (self.repo / "docs/.gitattributes").write_text(
            " #* -diff\n"            # NBSP is part of the pattern for Git, not a comment
            " \t# a real comment -diff\n"
            "* linguist-generated=0\n"
            "* linguist-generated=FALSE\n"
            "* linguist-generated=false -linguist-generated !linguist-generated\n")
        found = self.forbidden()
        self.assertEqual(found, sorted([
            ("TauCeti/.GitAttributes", "line 1 sets `-diff`"),
            ("docs/.gitattributes", "line 1 sets `-diff`"),
            ("docs/.gitattributes", "line 3 sets `linguist-generated=0`"),
            ("docs/.gitattributes", "line 4 sets `linguist-generated=FALSE`"),
            ("web/.git‌attributes", "line 1 sets `-diff`"),
            ("web/.gitattributes.", "line 1 sets `-diff`"),
        ]))

    def test_pr_guards_report_forbidden_content_without_lifting_it(self):
        (self.repo / "TauCeti/Nul.lean").write_bytes(b"\0")
        head = self.commit("head")
        with ci_git.object_reader(self.repo) as git:
            for kwargs in ({}, {"scope": False}, {"scope": False, "large_refactor": True}):
                result = ci_git.pr_guards(git, self.base, head, **kwargs)
                self.assertEqual(result["forbidden_content"], [("TauCeti/Nul.lean", "contains a NUL byte")])
                self.assertEqual(len(result["violations"]), 1)
                self.assertIn("TauCeti/Nul.lean contains a NUL byte", result["violations"][0])
        output = json.loads(subprocess.check_output(
            [sys.executable, str(SCRIPT), "pr-guards", "--no-scope", "--checkout", str(self.repo),
             "--base", self.base, "--head", head]))
        self.assertEqual(output["forbidden_content"], [["TauCeti/Nul.lean", "contains a NUL byte"]])

    def test_forbidden_content_sentence_lists_at_most_twenty_paths(self):
        for index in range(25):
            (self.repo / f"TauCeti/B{index:02}.bin").write_bytes(b"\0")
        result = self.guards(scope=False)
        self.assertEqual(len(result["forbidden_content"]), 25)
        (sentence,) = result["violations"]
        self.assertIn("TauCeti/B19.bin contains a NUL byte; and 5 more.", sentence)
        self.assertNotIn("B20", sentence)

    def test_unreadable_blob_listing_fails_closed(self):
        git = Mock()
        git.run.return_value = Mock(stdout=b"0" * 40 + b" missing\n")
        with self.assertRaises(ValueError):
            ci_git._read_blobs(git, ["0" * 40])
        with self.assertRaises(ValueError):
            ci_git._raw_records(b":100644 100644 x\0")

    def test_repository_reader_reads_worktrees(self):
        head = self.commit("head")
        worktree = Path(self.temp.name) / "worktree"
        self.git("worktree", "add", "--quiet", "--detach", str(worktree), head)
        with ci_git.repository_reader(worktree) as git:
            self.assertEqual(ci_git.pr_guards(git, self.base, head)["violations"], [])

    def test_unknown_commits_fail_closed(self):
        with ci_git.object_reader(self.repo) as git:
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                ci_git.review_size(git, self.base, "1" * 40)

    def test_malformed_output_is_rejected(self):
        for raw in (b"3\t1\tpath", b"3\t1\t\0old\0", b"x\t1\tpath\0"):
            git = Mock()
            git.run.return_value.stdout = raw
            with self.assertRaises(ValueError):
                ci_git.review_size(git, self.base, self.base)


class LargeRefactorTests(unittest.TestCase):
    LABELLED = 1_800_000_000  # 2027-01-15T08:00:00Z

    def event(self, id, event, *, label="large-refactor", login="admin", actor_id=1, at=None):
        at = self.LABELLED if at is None else at
        import datetime as dt

        stamp = dt.datetime.fromtimestamp(at, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"id": id, "event": event, "label": {"name": label}, "actor": {"login": login, "id": actor_id},
                "created_at": stamp}

    def approved(self, events, *, role="admin", user_id=1, labels=("large-refactor",), committed=None):
        reads = []

        def read(path):
            reads.append(path)
            if "/events?" in path:
                return events if path.endswith("page=1") else []
            return {"role_name": role, "user": {"id": user_id}}

        committed = self.LABELLED - 60 if committed is None else committed
        return ci_git.large_refactor_approved(read, "o/r", 7, labels, committed), reads

    def test_admin_label_after_the_head_commit_approves(self):
        verdict, reads = self.approved([self.event(1, "labeled", login="someone", actor_id=9),
                                        self.event(2, "labeled")])
        self.assertTrue(verdict)
        self.assertEqual(reads, ["repos/o/r/issues/7/events?per_page=100&page=1",
                                 "repos/o/r/collaborators/admin/permission"])

    def test_missing_label_reads_nothing(self):
        verdict, reads = self.approved([self.event(1, "labeled")], labels=("self-tested",))
        self.assertFalse(verdict)
        self.assertEqual(reads, [])

    def test_latest_labeller_must_be_an_admin_with_the_same_id(self):
        self.assertFalse(self.approved([self.event(1, "labeled")], role="maintain")[0])
        self.assertFalse(self.approved([self.event(1, "labeled")], user_id=2)[0])
        events = [self.event(1, "labeled"), self.event(2, "unlabeled"), self.event(3, "labeled", login="w", actor_id=5)]
        self.assertFalse(self.approved(events, user_id=1)[0])

    def test_a_later_head_or_force_push_ends_the_exemption(self):
        self.assertFalse(self.approved([self.event(1, "labeled")], committed=self.LABELLED + 1)[0])
        events = [self.event(1, "labeled"), {"id": 2, "event": "head_ref_force_pushed"}]
        self.assertFalse(self.approved(events)[0])
        events = [{"id": 1, "event": "head_ref_force_pushed"}, self.event(2, "labeled")]
        self.assertTrue(self.approved(events)[0])

    def test_unreadable_events_raise(self):
        with self.assertRaises(ValueError):
            ci_git.large_refactor_approved(lambda path: {"message": "x"}, "o/r", 7, ["large-refactor"], 0)


if __name__ == "__main__":
    unittest.main()
