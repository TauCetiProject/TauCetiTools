"""Exact Git snapshots reuse objects and handle filenames through machine output."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from tauceti_tools.git_inputs import _git, changed_paths, snapshot


class GitInputs(unittest.TestCase):
    def test_snapshots_are_shallow_reusable_and_keep_named_comparison_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            cache = root / "cache"

            def git(*args):
                return subprocess.check_output(["git", *map(str, args)], stderr=subprocess.DEVNULL, text=True).strip()

            git("init", "-q", source)
            git("-C", source, "config", "user.name", "fixture")
            git("-C", source, "config", "user.email", "fixture@example.org")
            for i in range(80):
                (source / f"module-{i}.lean").write_text(f"def value{i} := {i}\n")
            git("-C", source, "add", ".")
            git("-C", source, "commit", "-qm", "base")
            base = git("-C", source, "rev-parse", "HEAD")
            target = source / "module-0.lean"
            target.write_text("def value0 := 1\n")
            git("-C", source, "commit", "-qam", "first head")
            first = git("-C", source, "rev-parse", "HEAD")
            with snapshot(str(source), base, first, cache_dir=cache, complete=True) as (repo, env):
                self.assertEqual(_git(repo, ["rev-parse", "refs/tauceti/review/head"], env, 10).decode().strip(), first)
                self.assertEqual(_git(repo, ["rev-list", "--count", "refs/tauceti/review/base"], env, 10).strip(), b"1")

            def objects():
                values = dict(line.split(": ", 1) for line in git("-C", cache, "count-objects", "-v").splitlines())
                return int(values["count"]) + int(values["in-pack"])

            before = objects()
            target.write_text("def value0 := 2\n")
            git("-C", source, "commit", "-qam", "second head")
            second = git("-C", source, "rev-parse", "HEAD")
            with snapshot(str(source), base, second, cache_dir=cache, complete=True) as (repo, env):
                self.assertEqual(
                    _git(repo, ["rev-parse", "refs/tauceti/review/head"], env, 10).decode().strip(), second
                )
            self.assertLess(objects() - before, 10, "second review transferred the unchanged snapshot again")

    def test_changed_paths_preserve_quoted_names_and_detect_renames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(root), *args], stderr=subprocess.DEVNULL, text=True
                ).strip()

            git("init", "-q")
            git("config", "user.name", "fixture")
            git("config", "user.email", "fixture@example.org")
            git("config", "uploadpack.allowFilter", "true")
            old = root / "old.lean"
            old.write_text("example : True := trivial\n")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            old.rename(root / "space and\nnewline.lean")
            git("add", "-A")
            git("commit", "-qm", "rename")
            head = git("rev-parse", "HEAD")
            self.assertEqual(changed_paths(str(root), base, head), ["space and\nnewline.lean"])


if __name__ == "__main__":
    unittest.main()
