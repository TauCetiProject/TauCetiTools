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
        self.write("TauCeti/Other.lean", 2001, start=9000)
        size = self.measure()
        self.assertEqual((size["additions"], size["deletions"]), (2001, 10))
        self.assertFalse(size["pins_changed"])
        self.assertTrue(size["too_large"])
        (self.repo / "lean-toolchain").write_text("leanprover/lean4:v4.24.0\n")
        size = self.measure()
        self.assertTrue(size["pins_changed"])
        self.assertFalse(size["too_large"])

    def test_file_cap_applies_to_pin_bumps(self):
        (self.repo / "lean-toolchain").write_text("leanprover/lean4:v4.24.0\n")
        for index in range(ci_git.REVIEW_FILE_CAP):
            (self.repo / "TauCeti" / f"F{index}.lean").write_text(f"-- {index}\n")
        size = self.measure()
        self.assertEqual(size["files"], ci_git.REVIEW_FILE_CAP + 1)
        self.assertTrue(size["too_large"])

    def test_added_file_length_uses_line_count(self):
        self.write("TauCeti/Exact.lean", 1000, start=7000)
        self.write("TauCeti/Long.lean", 1001, start=8000)
        self.write("TauCeti/notes.txt", 1500, start=9000)
        size = self.measure()
        self.assertEqual(size["new_lean_files"], [("TauCeti/Exact.lean", 1000), ("TauCeti/Long.lean", 1001)])
        self.assertEqual(size["new_files_too_long"], [("TauCeti/Long.lean", 1001)])

    def test_unusual_filenames_and_cli(self):
        self.git("mv", "TauCeti/Other.lean", "TauCeti/Tab\tand\nnewline.lean")
        head = self.commit("head")
        output = subprocess.check_output(
            [sys.executable, str(SCRIPT), "review-size", "--checkout", str(self.repo), "--base", self.base, "--head", head]
        )
        size = json.loads(output)
        self.assertEqual((size["additions"], size["deletions"], size["files"]), (0, 0, 1))
        self.assertEqual(size["new_lean_files"], [])

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


if __name__ == "__main__":
    unittest.main()
