"""Check how the nightly comparison treats a differing `.olean.private`.

The fixture oleans come from `tests/fixtures/olean-private/Repro.lean` (Lean v4.37.0-rc1), compiled
at `-j8` and at `-j1`: the two private oleans differ only in how Lean numbered its compiler-internal
`_uniq` names. Further benign variants are made the same way, by renumbering a `_uniq.<k>` name
generator prefix and fixing the stored name hashes; damaged variants change anything else.
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import struct
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tauceti_tools/assets/scripts/compare-build-outputs.py"
FIXTURE = ROOT / "tests/fixtures/olean-private"
spec = importlib.util.spec_from_file_location("compare_build_outputs", SCRIPT)
cbo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cbo)

MODULE = "lib/lean/Example/Repro"
SUFFIXES = (".olean", ".olean.server", ".olean.private")


def load_parts(private="Repro.olean.private"):
    return [bytearray((FIXTURE / name).read_bytes())
            for name in ("Repro.olean", "Repro.olean.server", private)]


def base(part):
    return struct.unpack_from("<Q", part, 80)[0]


def name_nums(part):
    """{address: (offset, prefix, value)} for every aligned Name.num object in `part`."""
    out, i = {}, part.find(cbo.NAME_NUM)
    while i != -1:
        if i % 8 == 0:
            prefix, value, _ = struct.unpack_from("<QQQ", part, i + 8)
            if value & 1:
                out[base(part) + i] = (i, prefix, value >> 1)
        i = part.find(cbo.NAME_NUM, i + 1)
    return out


def uniq_root(parts):
    """Address of the `_uniq` Name.str object."""
    for part in parts:
        i = part.find(cbo.NAME_STR)
        while i != -1:
            if i % 8 == 0:
                anonymous, text, _ = struct.unpack_from("<QQQ", part, i + 8)
                for other in parts:
                    off = text - base(other)
                    if anonymous == 1 and 0 <= off < len(other) and other[off + 32:off + 38] == b"_uniq\0":
                        return base(part) + i
            i = part.find(cbo.NAME_STR, i + 1)
    raise AssertionError("no _uniq name in the fixture")


def stored_hash(parts, addr):
    for part in parts:
        off = addr - base(part)
        if 0 <= off < len(part):
            return struct.unpack_from("<Q", part, off + 24)[0]
    raise AssertionError(hex(addr))


def compiler_prefix(parts):
    """The private part's `_uniq.<k>` with the most `_uniq.<k>.<n>` names under it."""
    nums = name_nums(parts[2])
    root = uniq_root(parts)
    level1 = [a for a, (_, prefix, _) in nums.items() if prefix == root]
    return max(level1, key=lambda a: len(descendants(parts, a)))


def renumber(parts, new_value=987654, fix_hashes=True):
    """Renumber the private part's `_uniq.<k>` prefix, as a different schedule would."""
    private = parts[2]
    nums = name_nums(private)
    target = compiler_prefix(parts)
    off, prefix, _ = nums[target]
    struct.pack_into("<Q", private, off + 16, (new_value << 1) | 1)
    if fix_hashes:
        struct.pack_into("<Q", private, off + 24, cbo.mix_hash(stored_hash(parts, prefix), new_value))
        # The compactor writes an object's fields before the object, so prefixes come first.
        changed = {target}
        for addr in sorted(nums):
            o, p, v = nums[addr]
            if p in changed:
                struct.pack_into("<Q", private, o + 24, cbo.mix_hash(stored_hash(parts, p), v))
                changed.add(addr)
    return target


def descendants(parts, target):
    nums = name_nums(parts[2])
    return sorted(a for a, (_, p, _) in nums.items() if p == target)


def write_tree(root, parts, trace_hash=None):
    stem = root / MODULE
    stem.parent.mkdir(parents=True, exist_ok=True)
    for suffix, data in zip(SUFFIXES, parts, strict=True):
        Path(f"{stem}{suffix}").write_bytes(bytes(data))
    private_hash = trace_hash or f"{cbo.lake_hash(bytes(parts[2])):016x}"
    Path(f"{stem}.trace").write_text(json.dumps({
        "schemaVersion": "2025-09-10", "depHash": "0" * 16,
        "outputs": {"m": True, "o": ["1" * 16 + ".olean", "2" * 16 + ".olean.server",
                                     private_hash + ".olean.private"]}}))
    Path(f"{stem}.olean.private.hash").write_text(private_hash)
    (root / "lib/lean/Example/Other.ir").parent.mkdir(parents=True, exist_ok=True)
    (root / "lib/lean/Example/Other.ir").write_text("same")


class PrivateOleanDigest(unittest.TestCase):
    def test_hashes_match_lean(self):
        # From `#eval (Lake.Hash.ofByteArray s.toUTF8).hex` and `Name.hash` on Lean v4.37.0-rc1.
        for data, expected in ((b"", "21c42906adea99f5"), (b"abc", "4ee8255398c6e574"),
                               (b"olean private bytes!", "b4346734f5371cb3")):
            self.assertEqual(f"{cbo.lake_hash(data):016x}", expected)
        uniq = cbo.mix_hash(cbo.ANONYMOUS_HASH, cbo.murmur64a(b"_uniq", 11))
        self.assertEqual(uniq, 3978731030111751661)
        self.assertEqual(cbo.mix_hash(cbo.mix_hash(uniq, 11350), 5), 2527466631875286399)

    def test_real_schedules_differ_only_in_names(self):
        j8, j1 = load_parts(), load_parts("Repro.j1.olean.private")
        self.assertNotEqual(j8[2], j1[2])
        self.assertEqual(cbo.private_olean_digest(*map(bytes, j8)), cbo.private_olean_digest(*map(bytes, j1)))

    def test_fixture_decodes_and_names_are_counted(self):
        digest = cbo.private_olean_digest(*map(bytes, load_parts()))
        hexdigest, count = digest.split("/")
        self.assertEqual(len(hexdigest), 64)
        self.assertGreater(int(count), 10)

    def test_renumbered_prefix_has_the_same_digest(self):
        parts = load_parts()
        before = cbo.private_olean_digest(*map(bytes, parts))
        target = renumber(parts)
        self.assertTrue(descendants(parts, target), "the renumbered prefix should have children")
        self.assertNotEqual(bytes(parts[2]), (FIXTURE / "Repro.olean.private").read_bytes())
        self.assertEqual(cbo.private_olean_digest(*map(bytes, parts)), before)

    def test_stale_name_hash_changes_the_digest(self):
        parts = load_parts()
        before = cbo.private_olean_digest(*map(bytes, parts))
        renumber(parts, fix_hashes=False)
        self.assertNotEqual(cbo.private_olean_digest(*map(bytes, parts)), before)

    def test_merging_two_names_changes_the_digest(self):
        # Give one `_uniq.<k>.<n>` the number of a sibling: a one-to-many renaming, not a relabelling.
        parts = load_parts()
        before = cbo.private_olean_digest(*map(bytes, parts))
        nums = name_nums(parts[2])
        level1 = compiler_prefix(parts)
        first, second = descendants(parts, level1)[:2]
        off = nums[first][0]
        value = nums[second][2]
        struct.pack_into("<Q", parts[2], off + 16, (value << 1) | 1)
        struct.pack_into("<Q", parts[2], off + 24, cbo.mix_hash(stored_hash(parts, level1), value))
        self.assertNotEqual(cbo.private_olean_digest(*map(bytes, parts)), before)

    def test_changed_string_changes_the_digest(self):
        parts = load_parts()
        before = cbo.private_olean_digest(*map(bytes, parts))
        i = parts[2].find(b"UnreachableBranches")
        self.assertNotEqual(i, -1)
        parts[2][i] ^= 0x20
        self.assertNotEqual(cbo.private_olean_digest(*map(bytes, parts)), before)

    def assertCaught(self, parts, before, what):
        try:
            after = cbo.private_olean_digest(*map(bytes, parts))
        except cbo.Undecodable:
            return
        self.assertNotEqual(after, before, f"{what} went unnoticed")

    def test_any_flipped_byte_is_caught(self):
        original = (FIXTURE / "Repro.olean.private").read_bytes()
        before = cbo.private_olean_digest(*map(bytes, load_parts()))
        # Every byte of the header and the first 4 KiB, then the first byte of every word.
        offsets = sorted(set(range(4096)) | set(range(0, len(original), 8)))
        for offset in offsets:
            parts = load_parts()
            parts[2][offset] ^= 0x01
            self.assertCaught(parts, before, f"a flipped bit at offset {offset}")

    def test_padding_flags_and_appended_bytes_are_caught(self):
        before = cbo.private_olean_digest(*map(bytes, load_parts()))
        for offset in (241, 558):  # string padding; an array header's flag byte
            parts = load_parts()
            parts[2][offset] ^= 0x80
            self.assertCaught(parts, before, f"a change at offset {offset}")
        parts = load_parts()
        size = struct.unpack_from("<Q", parts[2], cbo.HEADER)[0]
        end = cbo.DATA + size
        parts[2][end:end] = bytes(8)
        struct.pack_into("<Q", parts[2], cbo.HEADER, size + 8)
        self.assertCaught(parts, before, "eight bytes appended to the data")

    def test_truncation_is_undecodable(self):
        parts = load_parts()
        with self.assertRaises(cbo.Undecodable):
            cbo.private_olean_digest(bytes(parts[0]), bytes(parts[1]), bytes(parts[2][:len(parts[2]) // 2]))


class Compare(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.clean = self.root / "clean"
        self.cached = self.root / "cached"
        self.output = self.root / "output"
        self.summary = self.root / "summary"
        environment = {"GITHUB_OUTPUT": str(self.output), "GITHUB_STEP_SUMMARY": str(self.summary)}
        patcher = unittest.mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)

    def compare(self, clean_parts, cached_parts, trace_hash=None, build_dir=True):
        for tree in (self.clean, self.cached):
            if tree.exists():
                shutil.rmtree(tree)
        write_tree(self.clean, clean_parts)
        write_tree(self.cached, cached_parts, trace_hash)
        if self.output.exists():
            self.output.unlink()
        for tree in ("clean", "cached"):
            with contextlib.redirect_stdout(io.StringIO()):
                cbo.do_manifest(str(self.root / tree), str(self.root / f"{tree}.txt"))
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            cbo.do_compare(str(self.root / "clean.txt"), str(self.root / "cached.txt"),
                           str(self.cached) if build_dir else None)
        outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
        return outputs["status"], outputs["detail"], log.getvalue()

    def test_identical_builds_agree(self):
        status, detail, _ = self.compare(load_parts(), load_parts())
        self.assertEqual((status, detail), ("agree", "4 artifacts identical"))

    def test_renamed_private_names_are_benign_and_reported(self):
        status, detail, log = self.compare(load_parts(), load_parts("Repro.j1.olean.private"))
        self.assertEqual(status, "agree")
        self.assertIn("1 benign private-olean nondeterminism (Example.Repro)", detail)
        self.assertIn("::notice title=Benign private-olean nondeterminism::Example.Repro:", log)
        self.assertIn("Example.Repro", self.summary.read_text())

    def test_without_the_cached_tree_a_difference_is_not_explained(self):
        cached = load_parts()
        renumber(cached)
        status, _, log = self.compare(load_parts(), cached, build_dir=False)
        self.assertEqual(status, "disagree")
        self.assertIn("no cache-restored build", log)

    def test_trace_mismatch_disagrees(self):
        cached = load_parts()
        renumber(cached)
        clean_hash = f"{cbo.lake_hash(bytes(load_parts()[2])):016x}"
        status, _, log = self.compare(load_parts(), cached, trace_hash=clean_hash)
        self.assertEqual(status, "disagree")
        self.assertIn("Lake trace records", log)

    def test_other_private_damage_disagrees(self):
        for damage in ("stale hash", "string", "truncated"):
            with self.subTest(damage=damage):
                cached = load_parts()
                if damage == "stale hash":
                    renumber(cached, fix_hashes=False)
                elif damage == "string":
                    cached[2][cached[2].find(b"UnreachableBranches")] ^= 0x20
                else:
                    cached[2] = cached[2][:len(cached[2]) - 64]
                status, detail, _ = self.compare(load_parts(), cached)
                self.assertEqual(status, "disagree")
                self.assertTrue(detail.startswith("1 differing"), detail)

    def test_a_difference_outside_private_oleans_disagrees(self):
        cached = load_parts()
        renumber(cached)
        cached[1][-1] ^= 0x01
        status, detail, _ = self.compare(load_parts(), cached)
        self.assertEqual(status, "disagree")
        self.assertTrue(detail.startswith("2 differing"), detail)

    def test_missing_artifact_disagrees(self):
        cached = load_parts()
        renumber(cached)
        for tree in (self.clean, self.cached):
            if tree.exists():
                shutil.rmtree(tree)
        write_tree(self.clean, load_parts())
        write_tree(self.cached, cached)
        (self.cached / "lib/lean/Example/Other.ir").unlink()
        for tree in ("clean", "cached"):
            with contextlib.redirect_stdout(io.StringIO()):
                cbo.do_manifest(str(self.root / tree), str(self.root / f"{tree}.txt"))
        with contextlib.redirect_stdout(io.StringIO()):
            cbo.do_compare(str(self.root / "clean.txt"), str(self.root / "cached.txt"), str(self.cached))
        self.assertIn("status=disagree", self.output.read_text())

    def test_undecodable_clean_copy_disagrees(self):
        cached = load_parts()
        renumber(cached)
        clean = load_parts()
        clean[0][:5] = b"xxxxx"
        cached[0][:5] = b"xxxxx"
        status, _, log = self.compare(clean, cached)
        self.assertEqual(status, "disagree")
        self.assertIn("could not be decoded", log)

    def test_symlinked_cached_file_is_not_followed(self):
        cached = load_parts()
        renumber(cached)
        write_tree(self.cached, cached)
        outside = self.root / "outside.private"
        stem = self.cached / MODULE
        shutil.move(f"{stem}.olean.private", outside)
        os.symlink(outside, f"{stem}.olean.private")
        reason = cbo.explain_private(MODULE + ".olean.private", "0/0", str(self.cached))[1]
        self.assertIn("not a regular file", reason)


class Manifest(unittest.TestCase):
    def test_manifest_round_trips_digests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_tree(root / "build", load_parts())
            with contextlib.redirect_stdout(io.StringIO()):
                cbo.do_manifest(str(root / "build"), str(root / "manifest.txt"))
            digests, canonical = cbo.read_manifest(str(root / "manifest.txt"))
            self.assertEqual(set(digests), {MODULE + s for s in SUFFIXES} | {"lib/lean/Example/Other.ir"})
            self.assertEqual(list(canonical), [MODULE + ".olean.private"])
            self.assertRegex(canonical[MODULE + ".olean.private"], r"^[0-9a-f]{64}/\d+$")

    def test_module_without_compiler_names_records_none(self):
        with tempfile.TemporaryDirectory() as directory:
            stem = Path(directory) / "M"
            parts = load_parts()
            for suffix, data in zip(SUFFIXES, parts, strict=True):
                data = bytes(data).replace(b"_uniq\0", b"_unix\0")
                Path(f"{stem}{suffix}").write_bytes(data)
            self.assertEqual(cbo.canonical_digest(str(stem)), "none")


if __name__ == "__main__":
    unittest.main()
