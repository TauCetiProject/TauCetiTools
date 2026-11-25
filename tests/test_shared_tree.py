"""The shared build tree consumer: declines on any doubt, keeps writes out of the entry, prunes."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tauceti_tools/assets/scripts/shared_tree.py"
WORKFLOW = ROOT / ".github/workflows/pr-build.yml"
spec = importlib.util.spec_from_file_location("shared_tree", SCRIPT)
shared_tree = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared_tree)

DIGEST = "d" * 64
KEY = dict(schema=1, toolchain="leanprover/lean4:v4.37.0", lean_sha256="a" * 64)


def key_tool(directory, key=KEY, digest=DIGEST, status=0):
    tool = Path(tempfile.mkdtemp(dir=directory)) / "tauceti_ci_tree.py"
    tool.write_text(textwrap.dedent(f"""\
        import json, sys
        assert sys.argv[1:2] == ["key"]
        print(json.dumps(dict(key={key!r}, key_digest={digest!r})))
        sys.exit({status})
        """))
    return tool


def build_entry(root):
    """A small entry: two root modules (one the candidate deleted), a package build, a cache."""
    tree = root / "tree"
    lake = tree / "lake"
    files = {
        "build/lib/lean/TauCeti.olean": "root",
        "build/lib/lean/TauCeti.trace": "{}",
        "build/lib/lean/TauCeti/A.olean": "a",
        "build/lib/lean/TauCeti/A.olean.server": "a",
        "build/lib/lean/TauCeti/A.olean.hash": "0123456789abcdef",
        "build/lib/lean/TauCeti/A.trace": "{}",
        "build/lib/lean/TauCeti/Gone.olean": "gone",
        "build/lib/lean/TauCeti/Gone.ilean": "gone",
        "build/lib/lean/TauCeti/Gone.trace": "{}",
        "build/lib/lean/TauCeti/Old/Deep.olean": "deep",
        "build/ir/TauCeti/A.c": "a",
        "build/ir/TauCeti/Gone.c": "gone",
        "build/ir/TauCeti/Gone.c.hash": "0123456789abcdef",
        "cache/artifacts/0123456789abcdef.olean": "a",
        "packages/mathlib/.lake/build/lib/lean/Mathlib/Gone.olean": "kept: not the root package",
        "tmp/.keep": "",
    }
    for name, content in files.items():
        (lake / name).parent.mkdir(parents=True, exist_ok=True)
        (lake / name).write_text(content)
    (tree / "ready.json").write_text(json.dumps(dict(version=1, id="20261125T120000Z-cccccccccccc-00000001",
                                                     commit="c" * 40, key=KEY, key_digest=DIGEST)))
    project = root / "work/pr"
    (project / "TauCeti").mkdir(parents=True)
    (project / "TauCeti.lean").write_text("")
    (project / "TauCeti/A.lean").write_text("")
    (project / ".lake").mkdir()
    return tree, project


class FakeMounts:
    """`sudo mount -t overlay` as a copy of the lower layer into the mount point."""

    def __init__(self, test):
        self.test = test
        self.mounted = set()
        self.calls = []

    def run(self, args, timeout=60, **kwargs):
        if args[:1] == [sys.executable]:
            return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)
        self.calls.append(args)
        if args[0] == "mount":
            options = args[args.index("-o") + 1]
            lower = dict(part.split("=", 1) for part in options.split(",") if "=" in part)["lowerdir"]
            shutil.copytree(lower, args[-1], dirs_exist_ok=True)
            self.mounted.add(args[-1])
        elif args[0] == "umount":
            if getattr(self, "stuck", False):
                raise subprocess.CalledProcessError(32, args)
            for child in Path(args[-1]).iterdir():
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            self.mounted.discard(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")

    def __enter__(self):
        self.patches = [patch.object(shared_tree, "run", side_effect=self.run),
                        patch.object(shared_tree, "is_mount", side_effect=lambda p: str(p) in self.mounted),
                        patch.object(shared_tree, "mount_type", side_effect=lambda p: "overlay" if str(p) in self.mounted else None),
                        patch.object(shared_tree, "read_only", return_value=True)]
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *exc):
        for item in self.patches:
            item.stop()


class Mount(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tree, self.project = build_entry(self.root)
        self.record = self.root / "telemetry/shared-tree.json"
        self.overlay = self.root / "work/_shared-tree-overlay"

    def mount(self, **kwargs):
        if "key_tool" not in kwargs:
            kwargs["key_tool"] = key_tool(self.root)
        kwargs.setdefault("tree", self.tree)
        status = shared_tree.mount(self.project, self.root / "watchdog", self.overlay, self.record, sudo=(), **kwargs)
        return status, json.loads(self.record.read_text())

    def test_no_bind_is_recorded_as_unavailable(self):
        status, value = self.mount(tree=self.root / "absent")
        self.assertEqual((status, value["shared_tree"]), (0, "unavailable"))
        self.assertEqual(list((self.project / ".lake").iterdir()), [])

    def test_hit_mounts_the_entry_and_prunes_modules_without_a_source(self):
        with FakeMounts(self) as mounts:
            status, value = self.mount()
        self.assertEqual((status, value["shared_tree"]), (0, "hit"))
        self.assertEqual(value["entry_commit"], "c" * 40)
        self.assertEqual(value["pruned"], 6)
        options = mounts.calls[0][mounts.calls[0].index("-o") + 1]
        self.assertTrue(options.startswith("userxattr,volatile,nosuid,nodev,lowerdir="))
        self.assertIn(f"upperdir={self.overlay / 'upper'},workdir={self.overlay / 'work'}", options)
        lake = self.project / ".lake"
        remaining = sorted(str(p.relative_to(lake)) for p in lake.rglob("*") if p.is_file())
        self.assertNotIn("build/lib/lean/TauCeti/Gone.olean", remaining)
        self.assertNotIn("build/lib/lean/TauCeti/Old/Deep.olean", remaining)
        self.assertNotIn("build/ir/TauCeti/Gone.c.hash", remaining)
        for kept in ("build/lib/lean/TauCeti.olean", "build/lib/lean/TauCeti/A.olean.server",
                     "build/lib/lean/TauCeti/A.olean.hash", "build/ir/TauCeti/A.c",
                     "packages/mathlib/.lake/build/lib/lean/Mathlib/Gone.olean", "cache/artifacts/0123456789abcdef.olean"):
            self.assertIn(kept, remaining)

    def test_key_mismatch_names_the_differing_fields(self):
        with FakeMounts(self) as mounts:
            status, value = self.mount(key_tool=key_tool(self.root, key=dict(KEY, toolchain="other"), digest="e" * 64))
        self.assertEqual((status, value["shared_tree"], value["key_difference"]), (0, "miss:key", ["toolchain"]))
        self.assertEqual(mounts.calls, [])
        self.assertFalse(self.overlay.exists())

    def test_failing_key_tool_is_a_miss(self):
        with FakeMounts(self):
            self.assertEqual(self.mount(key_tool=key_tool(self.root, status=1))[1]["shared_tree"], "miss:key-failed")
            self.assertEqual(self.mount(key_tool=self.root / "absent.py")[1]["shared_tree"], "miss:no-key-tool")

    def test_ready_record_must_be_well_formed(self):
        with FakeMounts(self):
            (self.tree / "ready.json").write_text(json.dumps(dict(version=1, id="x", commit="c", key_digest="short")))
            self.assertEqual(self.mount()[1]["shared_tree"], "miss:malformed-ready-record")
            (self.tree / "ready.json").unlink()
            self.assertEqual(self.mount()[1]["shared_tree"], "miss:no-ready-record")

    def test_writable_bind_is_refused(self):
        with FakeMounts(self) as mounts, patch.object(shared_tree, "read_only", return_value=False):
            self.assertEqual(self.mount()[1]["shared_tree"], "miss:not-read-only")
        self.assertEqual(mounts.calls, [])

    def test_non_empty_or_symlinked_lake_is_refused(self):
        (self.project / ".lake/stray").write_text("")
        with FakeMounts(self) as mounts:
            self.assertEqual(self.mount()[1]["shared_tree"], "miss:lake-not-empty")
            (self.project / ".lake/stray").unlink()
            (self.project / ".lake").rmdir()
            (self.project / ".lake").symlink_to(self.root)
            self.assertEqual(self.mount()[1]["shared_tree"], "miss:lake-not-empty")
        self.assertEqual(mounts.calls, [])

    def test_existing_overlay_directory_is_not_reused(self):
        self.overlay.mkdir(parents=True)
        with FakeMounts(self) as mounts:
            self.assertEqual(self.mount()[1]["shared_tree"], "miss:error:FileExistsError")
        self.assertEqual(mounts.calls, [])

    def test_failure_after_mounting_unmounts_and_takes_todays_path(self):
        with FakeMounts(self) as mounts, patch.object(shared_tree, "prune", side_effect=OSError("boom")):
            status, value = self.mount()
        self.assertEqual((status, value["shared_tree"]), (0, "miss:error:OSError"))
        self.assertEqual([call[0] for call in mounts.calls], ["mount", "umount"])
        self.assertEqual(list((self.project / ".lake").iterdir()), [])

    def test_a_mount_command_that_fails_after_mounting_is_undone(self):
        with FakeMounts(self) as mounts:
            real = mounts.run

            def mounted_then_failed(args, timeout=60, **kwargs):
                result = real(args, timeout=timeout, **kwargs)
                if args[0] == "mount":
                    raise subprocess.TimeoutExpired(args, timeout)
                return result
            with patch.object(shared_tree, "run", side_effect=mounted_then_failed):
                status, value = self.mount()
        self.assertEqual((status, value["shared_tree"]), (0, "miss:mount-failed"))
        self.assertEqual([call[0] for call in mounts.calls], ["mount", "umount"])
        self.assertEqual(list((self.project / ".lake").iterdir()), [])

    def test_a_hit_tells_later_steps(self):
        github_env = self.root / "github-env"
        with FakeMounts(self):
            self.mount(github_env=github_env)
        self.assertEqual(github_env.read_text(), "SHARED_TREE_HIT=1\n")

    def test_an_overlay_that_cannot_be_removed_fails_the_step(self):
        with FakeMounts(self) as mounts, patch.object(shared_tree, "prune", side_effect=OSError("boom")):
            mounts.stuck = True
            status, value = self.mount()
        self.assertEqual(status, 1)
        self.assertTrue(value["unrestored"])

    def test_record_adds_the_verdict_and_sizes(self):
        with FakeMounts(self):
            self.mount()
        (self.overlay / "upper/new.olean").write_text("x" * 5000)
        shared_tree.record(self.overlay, self.record, dict(build_state="success", pr="12"))
        value = json.loads(self.record.read_text())
        self.assertEqual((value["shared_tree"], value["build_state"], value["pr"]), ("hit", "success", "12"))
        self.assertGreaterEqual(value["upper_bytes"], 4096)
        self.assertGreaterEqual(value["upper_inodes"], 2)

    def test_record_without_a_mount_record_still_writes(self):
        shared_tree.record(self.overlay, self.record, dict(build_state="failure"))
        value = json.loads(self.record.read_text())
        self.assertEqual((value["shared_tree"], value["build_state"]), ("unrecorded", "failure"))
        self.assertNotIn("upper_bytes", value)

    def test_module_stem(self):
        self.assertEqual(shared_tree.module_stem("TauCeti/A/B.olean.server.hash"), "TauCeti/A/B")
        self.assertEqual(shared_tree.module_stem("TauCeti.trace"), "TauCeti")
        self.assertFalse(shared_tree.has_source(self.project, "Other/A"))
        self.assertFalse(shared_tree.has_source(self.project, ""))
        (self.project / "TauCeti/L.lean").symlink_to(self.project / "TauCeti/A.lean")
        self.assertFalse(shared_tree.has_source(self.project, "TauCeti/L"))


REAL_OVERLAY = r"""
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import shared_tree
root = Path(sys.argv[2])
tree, project = root / "tree", root / "work/pr"
subprocess.run(["mount", "--bind", tree, tree], check=True)
subprocess.run(["mount", "-o", "remount,bind,ro", tree], check=True)
status = shared_tree.mount(project, root / "watchdog", root / "work/_shared-tree-overlay", root / "record.json",
                           tree=tree, key_tool=Path(sys.argv[3]), sudo=())
lake = project / ".lake"
olean = lake / "build/lib/lean/TauCeti/A.olean"
os.chmod(olean, 0o644)
olean.write_text("poisoned")
(lake / "build/lib/lean/TauCeti/A.trace").unlink()
(lake / "build/lib/lean/TauCeti/New.olean").write_text("new")
print(json.dumps(dict(status=status, record=json.loads((root / "record.json").read_text()),
                      merged=olean.read_text(), gone=(lake / "build/lib/lean/TauCeti/Gone.olean").exists())))
"""


class RealOverlay(unittest.TestCase):
    """The same, through a real rootless overlay, where this host allows user namespaces."""

    def test_writes_and_prunes_stay_in_the_upper_layer(self):
        probe = subprocess.run(["unshare", "--user", "--map-root-user", "--mount", "true"], capture_output=True)
        if probe.returncode != 0:
            self.skipTest("unprivileged user and mount namespaces are unavailable here")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree, project = build_entry(root)
            before = {str(p.relative_to(tree)): p.read_bytes() for p in tree.rglob("*") if p.is_file()}
            result = subprocess.run(["unshare", "--user", "--map-root-user", "--mount", sys.executable, "-c", REAL_OVERLAY,
                                     str(SCRIPT.parent), str(root), str(key_tool(root))],
                                    capture_output=True, text=True)
            if result.returncode != 0 and "overlay" in result.stderr.lower():
                self.skipTest("rootless overlay mounts are unavailable here: " + result.stderr[-200:])
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual((value["status"], value["record"]["shared_tree"]), (0, "hit"), value)
            self.assertEqual(value["merged"], "poisoned")
            self.assertFalse(value["gone"])
            after = {str(p.relative_to(tree)): p.read_bytes() for p in tree.rglob("*") if p.is_file()}
            self.assertEqual(after, before)
            upper = root / "work/_shared-tree-overlay/upper"
            self.assertEqual((upper / "build/lib/lean/TauCeti/A.olean").read_text(), "poisoned")
            self.assertTrue((upper / "build/lib/lean/TauCeti/New.olean").is_file())
            self.assertTrue((upper / "build/lib/lean/TauCeti/Gone.olean").is_char_device())


class Workflow(unittest.TestCase):
    def steps(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        return workflow["jobs"]["sandboxed-build"]["steps"]

    def test_the_tree_is_mounted_after_the_last_normalisation_and_before_anything_writes_lake(self):
        names = [step.get("name", "") for step in self.steps()]
        mount = names.index("Mount the host's shared read-only build tree (ordinary PR builds on Fleet)")
        self.assertLess(names.index("Prepare and record the tested integration (frozen batches unchanged)"), mount)
        self.assertLess(names.index("Prepare trusted read-only Lean watchdog toolchain"), mount)
        self.assertEqual(names.index("Restore Mathlib's compressed download cache from main"), mount + 1)
        for later in names[mount + 1:]:
            self.assertNotIn("Normalise", later)
        for step in self.steps()[mount + 1:]:
            self.assertNotIn("rm -rf pr/.lake", step.get("run") or "")

    def test_only_ordinary_fleet_pr_builds_use_it(self):
        step = next(s for s in self.steps() if s.get("id") == "shared_tree")
        condition = step["if"]
        for term in ("env.IS_BATCH != 'true'", "env.BUMP != '1'", "startsWith(runner.name, 'fleet-')",
                     "vars.TAUCETI_SHARED_TREE != 'off'", "env.INFRA != '1'"):
            self.assertIn(term, condition)
        self.assertIn("_shared-tree-overlay", step["run"])

    def test_the_overlay_is_outside_everything_the_sandbox_mounts(self):
        text = WORKFLOW.read_text()
        self.assertNotIn("_shared-tree-overlay\" \"", text)
        for step in self.steps():
            if "bwrap" in (step.get("run") or ""):
                self.assertNotIn("SHARED_TREE_OVERLAY", step["run"])
                self.assertNotIn("/opt/tauceti-shared-tree", step["run"])

    def test_the_sandbox_learns_of_a_hit(self):
        build = next(s for s in self.steps() if s.get("id") == "sandbox_build")
        self.assertIn('--setenv SHARED_TREE_HIT "${SHARED_TREE_HIT:-}"', build["run"])
        mount = next(s for s in self.steps() if s.get("id") == "shared_tree")
        self.assertIn('--github-env "$GITHUB_ENV"', mount["run"])

    def test_the_record_step_never_fails_the_job(self):
        step = next(s for s in self.steps() if s.get("name", "").startswith("Record the shared build tree"))
        self.assertTrue(step["continue-on-error"])
        self.assertIn("always()", step["if"])


if __name__ == "__main__":
    unittest.main()
