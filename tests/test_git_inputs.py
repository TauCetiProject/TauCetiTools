"""Exact Git snapshots reuse objects and handle filenames through machine output."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.git_inputs import _git, changed_paths, snapshot, merge_base


class GitInputs(unittest.TestCase):
    def test_merge_bases_use_complete_cached_ancestry_without_warm_fetches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache = root/'source', root/'cache'
            source.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(source), *args],
                                               stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'fixture')
            git('config', 'user.email', 'fixture@example.org')
            git('config', 'uploadpack.allowFilter', 'true')
            def commit(name):
                (source/name).write_text(name)
                git('add', '.')
                git('commit', '-qm', name)
                return git('rev-parse', 'HEAD')
            base = commit('base')
            main = commit('main')
            git('checkout', '-qb', 'feature', base)
            first = commit('first')
            head = commit('head')
            self.assertEqual(merge_base(source, main, head, cache_dir=cache), base)
            def warm(repo, args, *a, **kw):
                self.assertNotEqual(args[0], 'fetch', 'verified cached ancestry must need no network')
                return _git(repo, args, *a, **kw)
            with patch('tauceti_tools.git_inputs._git', side_effect=warm):
                self.assertEqual(merge_base(source, main, head, cache_dir=cache), base)
                self.assertEqual(merge_base(source, first, head, cache_dir=cache), first)
            git('checkout', '-q', 'main')
            moved = commit('moved')
            self.assertEqual(merge_base(source, moved, head, cache_dir=cache), base)

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
