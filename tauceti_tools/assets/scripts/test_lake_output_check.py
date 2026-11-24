"""A built output whose bytes differ from its trace's name is found and discarded."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("lake_output_check", SCRIPTS / "lake_output_check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
INTEGRATION = "--integration" in sys.argv
if INTEGRATION:
    sys.argv.remove("--integration")


def fake_hasher(paths, scratch):
    """Name each file by a hash of its bytes the way the fixtures below do."""
    return {str(p): fixture_hash(p.read_bytes()) for p in paths}


def fixture_hash(data):
    return f"{(len(data) * 2654435761 + sum(data)) % (1 << 64):016x}"


class OutputCheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lib = self.root / ".lake/build/lib/lean/TauCeti/Flat"
        self.ir = self.root / ".lake/build/ir/TauCeti/Flat"
        self.lib.mkdir(parents=True)
        self.ir.mkdir(parents=True)
        self.artifacts = self.root / ".lake/cache/artifacts"
        self.artifacts.mkdir(parents=True)
        self.records = self.root / ".lake/cache/outputs/TauCeti"
        self.records.mkdir(parents=True)

    def module(self, name, dep_hash, contents):
        outputs, names = {"m": True}, {}
        for extension, data in contents.items():
            names[extension] = fixture_hash(data) + "." + extension
            root = self.ir if extension in ("c", "ltar") else self.lib
            (root / f"{name}.{extension}").write_bytes(data)
        outputs["o"] = [names[e] for e in ("olean", "olean.server", "olean.private")]
        outputs.update(i=names["ilean"], rs=names["ir.sig"], r=names["ir"], c=names["c"])
        (self.lib / f"{name}.trace").write_text(json.dumps({"depHash": dep_hash, "outputs": outputs}))
        (self.records / f"{dep_hash}.json").write_text("{}")
        return names

    def contents(self, seed):
        return {e: (seed + e).encode() * 40 for e in ("olean", "olean.server", "olean.private", "ilean",
                                                         "ir.sig", "ir", "c")}

    def test_intact_tree_passes(self):
        self.module("Basic", "4366fd23170e9e25", self.contents("a"))
        modules, files, found = check.mismatches(self.root, hasher=fake_hasher)
        self.assertEqual((modules, files, found), (1, 7, []))

    def test_truncated_and_empty_outputs_are_found_and_discarded(self):
        basic = self.module("Basic", "4366fd23170e9e25", self.contents("a"))
        self.module("Deck", "c270c715af183396", self.contents("b"))
        self.module("Fiber", "1111111111111111", self.contents("c"))
        (self.lib / "Basic.olean.private").write_bytes(self.contents("a")["olean.private"][:64])
        (self.lib / "Deck.ir.sig").write_bytes(b"")
        # The artifact cache's copy of an output is checked too (Lake hard-links it into the build).
        (self.artifacts / basic["olean"]).write_bytes(b"damaged")
        (self.lib / "Basic.olean.hash").write_text("x")
        (self.lib / "Basic.Sub.olean").write_bytes(b"another module")
        _, _, found = check.mismatches(self.root, hasher=fake_hasher)
        self.assertEqual(sorted((str(stem), path.name) for stem, _, path, *_ in found),
                         [("TauCeti/Flat/Basic", basic["olean"]), ("TauCeti/Flat/Basic", "Basic.olean.private"),
                          ("TauCeti/Flat/Deck", "Deck.ir.sig")])
        self.assertEqual(check.discard(self.root, found), 2)
        for name in ("Basic", "Deck"):
            self.assertEqual([p.name for p in self.lib.glob(name + ".*") if not p.name.startswith("Basic.Sub")], [])
            self.assertFalse((self.ir / f"{name}.c").exists())
        self.assertFalse((self.records / "4366fd23170e9e25.json").exists())
        self.assertFalse((self.records / "c270c715af183396.json").exists())
        self.assertFalse((self.artifacts / basic["olean"]).exists())
        # An intact module and a module whose name merely extends the damaged one's stay.
        self.assertTrue((self.lib / "Fiber.trace").exists())
        self.assertTrue((self.records / "1111111111111111.json").exists())
        self.assertTrue((self.lib / "Basic.Sub.olean").exists())

    def test_command_exit_codes(self):
        self.module("Basic", "4366fd23170e9e25", self.contents("a"))
        original = check.lake_hashes
        check.lake_hashes = fake_hasher
        self.addCleanup(setattr, check, "lake_hashes", original)
        self.assertEqual(check.main(["check", str(self.root)]), 0)
        (self.lib / "Basic.ir.sig").write_bytes(b"")
        self.assertEqual(check.main(["check", str(self.root)]), 1)
        self.assertTrue((self.lib / "Basic.trace").exists())
        self.assertEqual(check.main(["discard", str(self.root)]), 3)
        self.assertFalse((self.lib / "Basic.trace").exists())
        self.assertEqual(check.main(["check", str(self.root)]), 0)

    def test_a_hasher_failure_is_reported_as_unavailable(self):
        self.module("Basic", "4366fd23170e9e25", self.contents("a"))
        original = check.lake_hashes

        def failing(paths, scratch):
            raise subprocess.CalledProcessError(1, "CacheHash.lean")

        check.lake_hashes = failing
        self.addCleanup(setattr, check, "lake_hashes", original)
        self.assertEqual(check.main(["check", str(self.root)]), 2)


@unittest.skipUnless(INTEGRATION, "pass --integration with the pinned Lake installed")
class StockLakeTests(unittest.TestCase):
    def test_truncated_output_is_rebuilt_after_discard(self):
        lake = shutil.which("lake")
        with tempfile.TemporaryDirectory(prefix="lake-output-check-test-") as tmp:
            path = Path(tmp)
            (path / "lean-toolchain").write_text(os.environ.get("ELAN_TOOLCHAIN", "leanprover/lean4:v4.36.0-rc2") + "\n")
            (path / "lakefile.toml").write_text(
                'name = "TauCeti"\nplatformIndependent = true\ndefaultTargets = ["TauCeti"]\n'
                '[[lean_lib]]\nname = "TauCeti"\nglobs = ["TauCeti.*"]\n')
            (path / "TauCeti").mkdir()
            for file, text in {"TauCeti.lean": "module\n",
                               "TauCeti/A.lean": "module\npublic def a : Nat := 42\n",
                               "TauCeti/B.lean": "module\npublic import TauCeti.A\npublic def b := a + 1\n"}.items():
                (path / file).write_text(text)
            env = {**os.environ, "LAKE_CACHE_DIR": str(path / ".lake/cache"),
                   "LAKE_ARTIFACT_CACHE": "true", "LAKE_RESTORE_ARTIFACTS": "true", "LAKE_NO_CACHE": "true"}

            def build():
                result = subprocess.run([lake, "build", "--iofail", "--rehash", "-o", ".lake/map.jsonl"],
                                        cwd=path, env=env, text=True, capture_output=True, timeout=300)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout + result.stderr

            build()
            self.assertEqual(check.mismatches(path)[2], [])
            private = path / ".lake/build/lib/lean/TauCeti/A.olean.private"
            good = private.read_bytes()
            # What an interrupted write leaves: the trace still names the full file.
            private.unlink()
            private.write_bytes(good[: len(good) // 2])
            self.assertNotIn("Built TauCeti.A", build())  # stock Lake trusts it
            found = check.mismatches(path)[2]
            self.assertEqual([p.name for _, _, p, *_ in found], ["A.olean.private"])
            check.discard(path, found)
            self.assertIn("Built TauCeti.A", build())
            self.assertEqual(private.read_bytes(), good)
            self.assertEqual(check.mismatches(path)[2], [])


if __name__ == "__main__":
    unittest.main()
