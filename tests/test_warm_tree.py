"""The warm staging tree must fall back to the cold path on anything unexpected."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts/warm_tree.py"
spec = importlib.util.spec_from_file_location("warm_tree", SCRIPT)
warm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(warm)

SOURCE = {"forge": {"kind": "forgejo", "url": "https://forge.example"}, "name": "o/r", "id": 1}


class WarmTreeKey(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "pr"
        self.project.mkdir()
        (self.project / "lean-toolchain").write_text("leanprover/lean4:v4.36.0\n")
        (self.project / "lake-manifest.json").write_text('{"packages": []}\n')
        self.members = [{"pr": 2, "head": "b" * 40}, {"pr": 1, "head": "a" * 40}]

    def key(self, members=None, batch=7, retained=False):
        request = {"batch_id": batch, "batch_version": 3, "source": SOURCE,
                   "members": {"manifest": {"sha256": "c" * 64}} if retained else (members or self.members)}
        (self.root / "request.json").write_text(json.dumps({"request": request}))
        (self.root / "members.json").write_text(json.dumps({"members": members or self.members}))
        return warm.tree_key(self.project, self.root / "request.json", self.root / "members.json")

    def test_key_follows_batch_members_and_pins_not_versions_or_member_order(self):
        key = self.key()
        self.assertEqual(self.key(list(reversed(self.members))), key)
        self.assertEqual(self.key(retained=True), key)
        self.assertNotEqual(self.key(batch=8), key)
        self.assertNotEqual(self.key(self.members[:1]), key)
        self.assertNotEqual(self.key([{"pr": 2, "head": "d" * 40}, self.members[1]]), key)
        (self.project / "lean-toolchain").write_text("leanprover/lean4:v4.37.0\n")
        self.assertNotEqual(self.key(), key)
        (self.project / "lean-toolchain").write_text("leanprover/lean4:v4.36.0\n")
        (self.project / "lake-manifest.json").write_text('{"packages": [1]}\n')
        self.assertNotEqual(self.key(), key)


class WarmTreeCleanup(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.lake = Path(temporary.name) / "lake"
        for name in ("build/lib/lean/TauCeti", "cache/artifacts", "packages/mathlib/.lake/build/bin",
                     "publication-stage", "telemetry"):
            (self.lake / name).mkdir(parents=True)
        (self.lake / "build/lib/lean/TauCeti/A.olean").write_text("olean")
        (self.lake / "cache/artifacts/0123.art").write_text("art")
        (self.lake / "packages/mathlib/.lake/build/bin/cache").write_text("planted")
        (self.lake / "outputs.jsonl").write_text("{}")

    def test_only_build_and_cache_survive(self):
        self.assertTrue(warm.keep_only_outputs(self.lake))
        self.assertEqual(sorted(p.name for p in self.lake.iterdir()), ["build", "cache"])
        self.assertTrue((self.lake / "build/lib/lean/TauCeti/A.olean").is_file())

    def test_symlinks_and_special_files_refuse_the_tree(self):
        for planted in ("build/lib/lean/TauCeti/B.olean", "cache/tmp"):
            with self.subTest(planted=planted):
                path = self.lake / planted
                os.symlink("/home/runner/.elan/bin/lake", path)
                self.assertFalse(warm.keep_only_outputs(self.lake))
                path.unlink()
        os.mkfifo(self.lake / "cache/fifo")
        self.assertFalse(warm.keep_only_outputs(self.lake))

    def test_a_symlinked_kept_directory_is_removed_not_followed(self):
        target = self.lake.parent / "outside"
        target.mkdir()
        (target / "keep").write_text("x")
        (self.lake / "build").rename(self.lake / "old-build")
        os.symlink(target, self.lake / "build")
        self.assertTrue(warm.keep_only_outputs(self.lake))
        self.assertFalse((self.lake / "build").exists())
        self.assertTrue((target / "keep").exists())


class FakeDisk:
    """Stands in for sudo: a directory for the disk, mkfs empties it, binds are recorded."""

    def __init__(self, root):
        self.root = root
        self.formats = 0
        self.binds = []

    def __call__(self, *args, check=True, timeout=600):
        args = [str(a) for a in args]
        if args[0] == "mkfs.ext4":
            self.formats += 1
            for entry in warm.MOUNT.iterdir():
                shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
        elif args[:2] == ["mount", "--bind"]:
            self.binds.append(args[2:])
        return subprocess.CompletedProcess(args, 0, "")


class WarmTreeLifecycle(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "device").write_text("")
        (self.root / "disk").mkdir()
        self.workspace = self.root / "work"
        self.project = self.workspace / "pr"
        (self.project / ".lake").mkdir(parents=True)
        (self.project / "lean-toolchain").write_text("leanprover/lean4:v4.36.0\n")
        (self.project / "lake-manifest.json").write_text("{}\n")
        request = {"batch_id": 7, "source": SOURCE, "members": [{"pr": 1, "head": "a" * 40}]}
        (self.root / "request.json").write_text(json.dumps({"request": request}))
        self.disk = FakeDisk(self.root)
        for name, value in (("DEVICE", self.root / "device"), ("MOUNT", self.root / "disk"),
                            ("sudo", self.disk), ("room_left", lambda path: True)):
            patcher = patch.object(warm, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def prepare(self):
        return warm.prepare(self.workspace, "pr", self.root / "request.json", self.root / "members.json")

    def bind(self):
        """A symlink stands in for the bind of setup/ over the candidate's .lake."""
        lake = self.project / ".lake"
        os.rmdir(lake)
        os.symlink(warm.MOUNT / "setup", lake)

    def unbind(self):
        lake = self.project / ".lake"
        lake.unlink()
        lake.mkdir()

    def attempt(self, build_files):
        """One staging attempt from prepare to retain."""
        result = self.prepare()
        setup = warm.MOUNT / "setup"
        self.assertEqual(self.disk.binds[-1], [str(setup), str(self.project / ".lake")])
        # Trusted setup writes packages and the repair outputs into the fresh tree.
        (setup / "packages/mathlib").mkdir(parents=True)
        (setup / "cache/outputs").mkdir(parents=True)
        (setup / "cache/outputs/fresh.json").write_text("fresh")
        self.bind()
        with patch.object(warm, "bound_to_setup", return_value=True):
            self.assertEqual(warm.activate(self.project, result["WARM_TREE"] == "1"), {"WARM_ACTIVE": "1"})
            lake = self.project / ".lake"
            for name, text in build_files.items():
                (lake / "build").mkdir(exist_ok=True)
                (lake / "build" / name).write_text(text)
            (lake / "cache/outputs/built.json").write_text("built")
            warm.retain(self.project, result["WARM_TREE_KEY"])
        self.unbind()
        return result

    def test_first_attempt_formats_and_second_reuses_only_build_and_cache(self):
        first = self.attempt({"A.olean": "one"})
        self.assertEqual((first["WARM_VOLUME"], first["WARM_TREE"]), ("1", "0"))
        self.assertEqual(self.disk.formats, 1)
        self.assertEqual(json.loads((warm.MOUNT / "state.json").read_text())["complete"], True)
        second = self.prepare()
        self.assertEqual(second["WARM_TREE"], "1")
        self.assertEqual(self.disk.formats, 1)
        # Hidden from trusted setup, and only outputs survive; the marker is incomplete again.
        self.assertEqual(sorted(p.name for p in (warm.MOUNT / "lake").iterdir()), ["build", "cache"])
        self.assertEqual(list((warm.MOUNT / "setup").iterdir()), [])
        self.assertEqual(json.loads((warm.MOUNT / "state.json").read_text())["complete"], False)

    def test_activation_moves_build_and_merges_cache_with_fresh_files_winning(self):
        self.attempt({"A.olean": "one"})
        (warm.MOUNT / "lake/cache/outputs/fresh.json").write_text("stale")
        result = self.prepare()
        setup = warm.MOUNT / "setup"
        (setup / "cache/outputs").mkdir(parents=True)
        (setup / "cache/outputs/fresh.json").write_text("fresh")
        self.bind()
        with patch.object(warm, "bound_to_setup", return_value=True):
            warm.activate(self.project, result["WARM_TREE"] == "1")
        self.assertEqual((setup / "build/A.olean").read_text(), "one")
        self.assertEqual((setup / "cache/outputs/fresh.json").read_text(), "fresh")
        self.assertEqual((setup / "cache/outputs/built.json").read_text(), "built")
        self.assertFalse((warm.MOUNT / "lake").exists())

    def test_other_key_unsealed_attempt_or_planted_link_starts_afresh(self):
        def new_key():
            (self.project / "lean-toolchain").write_text("leanprover/lean4:v4.37.0\n")

        def unsealed():
            self.prepare()  # an attempt that never reached retain

        def planted():
            os.symlink("/usr/bin/curl", warm.MOUNT / "lake/build/curl")

        for change in (new_key, unsealed, planted):
            with self.subTest(change=change.__name__):
                (self.project / "lean-toolchain").write_text("leanprover/lean4:v4.36.0\n")
                self.attempt({"A.olean": "one"})
                formats = self.disk.formats
                change()
                self.assertEqual(self.prepare()["WARM_TREE"], "0")
                self.assertEqual(self.disk.formats, formats + 1)
                self.assertEqual(sorted(p.name for p in warm.MOUNT.iterdir()), ["setup", "state.json"])
                shutil.rmtree(warm.MOUNT / "setup")

    def test_retain_refuses_a_tree_that_is_not_the_bound_setup(self):
        self.prepare()
        with patch.object(warm, "bound_to_setup", return_value=False):
            self.assertEqual(warm.main(["retain", "--project", str(self.project), "--key", "k"]), 0)
        self.assertFalse(json.loads((warm.MOUNT / "state.json").read_text())["complete"])

    def test_a_non_empty_lake_is_left_alone(self):
        (self.project / ".lake/leftover").write_text("x")
        self.assertEqual(self.prepare(), {})
        self.assertEqual(self.disk.formats, 0)


class WarmTreeCommands(unittest.TestCase):
    def test_no_disk_is_the_cold_path_with_nothing_exported(self):
        with tempfile.TemporaryDirectory() as root, patch.object(warm, "DEVICE", Path(root) / "absent"):
            env = Path(root) / "env"
            self.assertEqual(warm.main(["prepare", "--workspace", root, "--project", "pr", "--request", "r",
                                        "--members", "m", "--github-env", str(env)]), 0)
            self.assertFalse(env.exists())

    def test_failures_never_fail_the_job_and_undo_mounts(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(warm, "prepare", side_effect=OSError("mount failed")), \
             patch.object(warm.subprocess, "run") as run:
            env = Path(root) / "env"
            self.assertEqual(warm.main(["prepare", "--workspace", root, "--project", "pr", "--request", "r",
                                        "--members", "m", "--github-env", str(env)]), 0)
            self.assertFalse(env.exists())
        unmounted = [call.args[0][-1] for call in run.call_args_list]
        self.assertEqual(unmounted, [str(Path(root) / "pr/.lake"), str(warm.MOUNT)])
        for command in (["activate", "--project", "pr", "--warm", "1"], ["retain", "--project", "pr", "--key", "k"]):
            with patch.object(warm, command[0], side_effect=OSError("busy")):
                self.assertEqual(warm.main(command), 0)


if __name__ == "__main__":
    unittest.main()
