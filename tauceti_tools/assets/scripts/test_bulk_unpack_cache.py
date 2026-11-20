"""Bulk restoration must leave stock Lake responsible for current input validity."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("bulk_unpack", SCRIPTS / "bulk-unpack-cache.py")
bulk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bulk)
INTEGRATION = "--integration" in sys.argv
if INTEGRATION:
    sys.argv.remove("--integration")


class BulkUnpackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "lakefile.toml").write_text(
            'name = "TauCeti"\n[[lean_lib]]\nname = "TauCeti"\n')
        self.records = self.root / ".lake/cache/outputs/TauCeti"
        self.records.mkdir(parents=True)
        self.artifacts = self.root / ".lake/cache/artifacts"
        self.artifacts.mkdir()
        self.record = self.records / "0123456789abcdef.json"
        self.archive = self.artifacts / "fedcba9876543210.ltar"
        self.archive.write_bytes(b"archive fixture")
        self.record.write_text(json.dumps({"schemaVersion": bulk.SCHEMA,
                                          "data": self.archive.name}))

    def test_one_invocation_preserves_hashes_and_output_roots(self):
        (self.records / "1111111111111111.json").write_text(json.dumps(
            {"schemaVersion": bulk.SCHEMA, "data": "2222222222222222.ltar"}))
        (self.artifacts / "2222222222222222.ltar").write_bytes(b"another archive")
        def extract(*args, **kwargs):
            for index, record in enumerate(json.loads(kwargs["input"])):
                module = f"TauCeti.Fixture{index}"
                (self.root / "TauCeti").mkdir(exist_ok=True)
                (self.root / "TauCeti" / f"Fixture{index}.lean").write_text("module\n")
                lib, ir = map(Path, record["base"])
                lib.mkdir(parents=True)
                ir.mkdir(parents=True)
                (lib / f"Fixture{index}.ilean").write_text(json.dumps({"version": 5, "module": module}))
        with patch.object(bulk.subprocess, "check_output", return_value="/toolchain\n"), \
                patch.object(bulk.subprocess, "run", side_effect=extract) as run:
            self.assertEqual(bulk.unpack(self.root), 2)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["/toolchain/bin/leantar", "-x", "-j", "-"])
        records = json.loads(run.call_args.kwargs["input"])
        self.assertEqual([r["hash"] for r in records], ["0123456789abcdef", "1111111111111111"])
        self.assertEqual([Path(p).name for p in records[0]["base"]], ["lib", "ir"])
        self.assertTrue((self.root / ".lake/build/lib/lean/TauCeti/Fixture0.ilean").exists())
        # Native Lake's final -o build must reuse the downloaded archives instead of repacking.
        for record in records:
            module = "Fixture0" if record["hash"] == "0123456789abcdef" else "Fixture1"
            archive = self.root / ".lake/build/ir/TauCeti" / (module + ".ltar")
            self.assertTrue(os.path.samefile(archive, record["file"]))
        self.assertFalse(self.record.exists())

    def assert_stock_fallback(self):
        with patch.object(bulk.subprocess, "run") as run:
            self.assertEqual(bulk.unpack(self.root), 0)
        run.assert_not_called()

    def test_existing_build_is_untouched(self):
        (self.root / ".lake/build").mkdir()
        self.assert_stock_fallback()

    def test_unknown_or_malformed_records_use_stock_lake(self):
        for text in ("{", "[]", '{"schemaVersion":"future"}',
                     json.dumps({"schemaVersion": bulk.SCHEMA, "data": {"o": "artifact"}})):
            with self.subTest(text=text):
                self.record.write_text(text)
                self.assert_stock_fallback()

    def test_unsupported_layout_uses_stock_lake(self):
        for text in ('name = "Other"', 'name = "TauCeti"\nbuildDir = "out"',
                     'name = "TauCeti"\nlean_lib = "bad"',
                     'name = "TauCeti"\n[lean_lib]\nname = "TauCeti"', '{'):
            with self.subTest(text=text):
                (self.root / "lakefile.toml").write_text(text)
                self.assert_stock_fallback()

    def test_failed_extraction_leaves_cache_and_build_untouched(self):
        with patch.object(bulk.subprocess, "check_output", return_value="/toolchain\n"), \
                patch.object(bulk.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "leantar")):
            self.assertEqual(bulk.unpack(self.root), 0)
        self.assertTrue(self.record.exists())
        self.assertFalse((self.root / ".lake/build").exists())

    def test_cli_does_not_touch_a_shared_or_disabled_cache(self):
        for configured, enabled, restored in (("/shared/cache", "true", "true"),
                (str(self.root / ".lake/cache"), "", "true"),
                (str(self.root / ".lake/cache"), "true", "")):
            with self.subTest(configured=configured, enabled=enabled, restored=restored):
                env = {**os.environ, "LAKE_CACHE_DIR": configured,
                       "LAKE_ARTIFACT_CACHE": enabled, "LAKE_RESTORE_ARTIFACTS": restored}
                result = subprocess.run([sys.executable, str(SCRIPTS / "bulk-unpack-cache.py"),
                                         str(self.root)], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("private CI cache not enabled", result.stdout)
                self.assertTrue(self.record.exists())
                self.assertFalse((self.root / ".lake/build").exists())

    def test_unknown_module_leaves_all_records_and_outputs_untouched(self):
        def extract(*args, **kwargs):
            lib, ir = map(Path, json.loads(kwargs["input"])[0]["base"])
            lib.mkdir(parents=True)
            ir.mkdir()
            (lib / "Other.ilean").write_text(json.dumps({"version": 5, "module": "Other"}))
        with patch.object(bulk.subprocess, "check_output", return_value="/toolchain\n"), \
                patch.object(bulk.subprocess, "run", side_effect=extract):
            self.assertEqual(bulk.unpack(self.root), 0)
        self.assertTrue(self.record.exists())
        self.assertFalse((self.root / ".lake/build").exists())

    def test_unexpected_helper_failure_leaves_normal_build_available(self):
        env = {"LAKE_CACHE_DIR": str(self.root / ".lake/cache"),
               "LAKE_ARTIFACT_CACHE": "true", "LAKE_RESTORE_ARTIFACTS": "true"}
        with patch.dict(os.environ, env), \
                patch.object(bulk, "unpack", side_effect=OSError("unavailable toolchain")):
            bulk.main(self.root)
        self.assertTrue(self.record.exists())

    def test_missing_archive_uses_stock_lake(self):
        self.archive.unlink()
        self.assert_stock_fallback()

    def test_historical_duplicate_keeps_alternative_for_native_selection(self):
        duplicate = self.records / "1111111111111111.json"
        duplicate.write_bytes(self.record.read_bytes())
        with patch.object(bulk, "extract_records", side_effect=self.extracted_fixture):
            self.assertEqual(bulk.unpack(self.root), 1)
        self.assertFalse(self.record.exists())
        self.assertTrue(duplicate.exists())

    def prefer(self, revision, *hashes):
        revisions = self.root / ".lake/cache/revisions/TauCeti"
        revisions.mkdir(parents=True, exist_ok=True)
        rows = [json.dumps(bulk.SCHEMA)] + [json.dumps([h, "fedcba9876543210.ltar"]) for h in hashes]
        (revisions / (revision + ".jsonl")).write_text("\n".join(rows) + "\n")
        with open(self.root / ".lake/cache" / bulk.PREFERRED, "a") as stream:
            stream.write(revision + "\n")

    def test_shared_archive_is_stamped_with_the_candidate_revisions_input(self):
        # The base record arrives first; the candidate's record names the same bytes.
        os.utime(self.record, ns=(1, 1))
        candidate = self.records / "1111111111111111.json"
        candidate.write_bytes(self.record.read_bytes())
        self.prefer("a" * 40, "1111111111111111")
        selected = []
        def extracted(records, staging):
            selected.extend(record["hash"] for record in records)
            return self.extracted_fixture(records, staging)
        with patch.object(bulk, "extract_records", side_effect=extracted):
            self.assertEqual(bulk.unpack(self.root), 1)
        self.assertEqual(selected, ["1111111111111111"])
        self.assertFalse(candidate.exists())
        self.assertTrue(self.record.exists())

    def test_earlier_candidate_revision_ranks_first(self):
        later = self.records / "1111111111111111.json"
        later.write_bytes(self.record.read_bytes())
        self.prefer("a" * 40, "1111111111111111")
        self.prefer("b" * 40, "0123456789abcdef")
        ranks = bulk.preferred_inputs(self.root)
        self.assertEqual(ranks, {"1111111111111111": 0, "0123456789abcdef": 1})
        self.assertEqual([r["hash"] for r in bulk.selected_records(self.root)], ["1111111111111111"])

    def test_missing_or_malformed_preference_keeps_arrival_order(self):
        cache = self.root / ".lake/cache"
        self.assertEqual(bulk.preferred_inputs(self.root), {})
        (cache / bulk.PREFERRED).write_text("not-a-revision\n" + "c" * 40 + "\n")
        self.assertEqual(bulk.preferred_inputs(self.root), {})
        self.assertEqual([r["hash"] for r in bulk.selected_records(self.root)], ["0123456789abcdef"])

    def test_historical_records_do_not_disable_other_modules(self):
        duplicate = self.records / "1111111111111111.json"
        duplicate.write_bytes(self.record.read_bytes())
        unique = self.records / "2222222222222222.json"
        unique.write_text(json.dumps({"schemaVersion": bulk.SCHEMA, "data": "3333333333333333.ltar"}))
        (self.artifacts / "3333333333333333.ltar").write_bytes(b"another archive")
        def extracted(records, staging):
            result = []
            for index, record in enumerate(records):
                module = f"TauCeti.Fixture{index}"
                source = self.root.joinpath(*module.split(".")).with_suffix(".lean")
                source.parent.mkdir(exist_ok=True)
                source.write_text("module\n")
                output = staging / f"output{index}"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"native output fixture")
                result.append((record, module, [(output, f"lib/lean/TauCeti/Fixture{index}.olean")]))
            return result
        with patch.object(bulk, "extract_records", side_effect=extracted):
            self.assertEqual(bulk.unpack(self.root), 2)
        self.assertFalse(self.record.exists())
        self.assertTrue(duplicate.exists())
        self.assertFalse(unique.exists())

    def test_symlinked_record_or_archive_uses_stock_lake(self):
        self.archive.unlink()
        self.archive.symlink_to(self.record)
        self.assert_stock_fallback()
        self.archive.unlink()
        self.archive.write_bytes(b"archive fixture")
        self.record.unlink()
        self.record.symlink_to(self.archive)
        self.assert_stock_fallback()

    def extracted_fixture(self, records, staging):
        """Supply one private extraction to exercise publication failure recovery."""
        (self.root / "TauCeti").mkdir(exist_ok=True)
        (self.root / "TauCeti/Fixture.lean").write_text("module\n")
        staging.mkdir(parents=True)
        output = staging / "Fixture.olean"
        output.write_bytes(b"native output fixture")
        return [(records[0], "TauCeti.Fixture", [(output, "lib/lean/TauCeti/Fixture.olean")])]

    def test_interrupted_combined_publication_preserves_every_original_mapping(self):
        original = self.record.read_bytes()
        with patch.object(bulk, "extract_records", side_effect=self.extracted_fixture), \
                patch.object(bulk, "publish", side_effect=OSError("interrupted")):
            self.assertEqual(bulk.unpack(self.root), 0)
        self.assertEqual(self.record.read_bytes(), original)
        self.assertFalse((self.root / ".lake/build").exists())
        self.assertTrue(self.archive.exists())

    def test_changed_mapping_is_rejected_before_build_publication(self):
        records = bulk.selected_records(self.root)
        with tempfile.TemporaryDirectory(dir=self.root / ".lake") as temporary:
            with patch.object(bulk, "extract_records", side_effect=self.extracted_fixture):
                combined, satisfied, _, _ = bulk.assemble(self.root, records, Path(temporary))
            self.record.write_text("changed")
            with self.assertRaisesRegex(ValueError, "changed during"):
                bulk.publish(self.root, combined, satisfied)
        self.assertEqual(self.record.read_text(), "changed")
        self.assertFalse((self.root / ".lake/build").exists())

    def test_interrupt_after_tree_rename_keeps_records_for_native_restoration(self):
        unlink = Path.unlink
        def interrupted(path, *args, **kwargs):
            if path == self.record:
                raise OSError("interrupted after tree publication")
            return unlink(path, *args, **kwargs)
        with patch.object(bulk, "extract_records", side_effect=self.extracted_fixture), \
                patch.object(Path, "unlink", interrupted):
            self.assertEqual(bulk.unpack(self.root), 0)
        self.assertTrue(self.record.exists())
        self.assertTrue((self.root / ".lake/build/lib/lean/TauCeti/Fixture.olean").is_file())


    EXTENSIONS = ("olean", "olean.server", "olean.private", "ilean", "ir.sig", "ir", "c")

    def names(self, index):
        return {ext: f"{index + 1:x}00000000000000{n}.{ext}" for n, ext in enumerate(self.EXTENSIONS, 1)}

    def module_outputs(self, index):
        names = self.names(index)
        return {"m": True, "o": [names["olean"], names["olean.server"], names["olean.private"]],
                "i": names["ilean"], "rs": names["ir.sig"], "r": names["ir"], "c": names["c"]}

    def native_fixture(self, records, staging, outputs=None, extra=(), omit=()):
        """Extract each record as a module whose trace names its outputs as Lake packs them."""
        result = []
        for index, record in enumerate(records):
            module = f"Fixture{index}"
            (self.root / "TauCeti").mkdir(exist_ok=True)
            (self.root / "TauCeti" / (module + ".lean")).write_text("module\n")
            base = staging / str(index)
            base.mkdir(parents=True, exist_ok=True)
            files = []
            for extension in (*self.EXTENSIONS, *extra):
                if extension in omit:
                    continue
                directory = "ir" if extension == "c" else "lib/lean"
                source = base / (module + "." + extension)
                source.write_bytes(f"{index} {extension}".encode())
                files.append((source, f"{directory}/TauCeti/{module}.{extension}"))
            trace = base / (module + ".trace")
            trace.write_text(json.dumps({"schemaVersion": "2025-09-10", "depHash": record["hash"],
                                         "outputs": self.module_outputs(index) if outputs is None else outputs}))
            files.append((trace, f"lib/lean/TauCeti/{module}.trace"))
            result.append((record, "TauCeti." + module, files))
        return result

    def hashed(self, wrong=()):
        """Stand in for the Lake hash helper; modules listed in `wrong` hash to other bytes."""
        def value(path):
            module, extension = Path(path).name.split(".", 1)
            index = int(module.removeprefix("Fixture"))
            return 0 if index in wrong else int(self.names(index)[extension].split(".")[0], 16)
        class Helper:
            returncode = 0
            def __init__(self, args, **kwargs):
                self.paths = json.loads(Path(args[-1]).read_text())
            def communicate(self, timeout=None):
                return "\n".join(json.dumps([path, value(path)]) for path in self.paths), None
        return Helper

    def second_record(self):
        second = self.records / "1111111111111111.json"
        second.write_text(json.dumps({"schemaVersion": bulk.SCHEMA, "data": "2222222222222222.ltar"}))
        (self.artifacts / "2222222222222222.ltar").write_bytes(b"another archive")
        return second

    def unpack_native(self, helper, **fixture):
        with patch.object(bulk, "extract_records",
                          side_effect=lambda records, staging: self.native_fixture(records, staging, **fixture)), \
                patch.object(bulk.subprocess, "check_output", return_value="/toolchain\n"), \
                patch.object(bulk.subprocess, "Popen", side_effect=helper) as popen:
            restored = bulk.unpack(self.root)
        return restored, popen

    def installed(self):
        return sorted(p.name for p in self.artifacts.iterdir() if not p.name.endswith(".ltar"))

    def test_verified_outputs_are_installed_in_the_expanded_cache(self):
        restored, popen = self.unpack_native(self.hashed())
        self.assertEqual(restored, 1)
        self.assertEqual(popen.call_args.args[0][:2], ["/toolchain/bin/lean", "--run"])
        self.assertEqual(Path(popen.call_args.args[0][2]).name, "CacheHash.lean")
        build = self.root / ".lake/build"
        for extension, name in self.names(0).items():
            directory = "ir" if extension == "c" else "lib/lean"
            output = build / directory / "TauCeti" / ("Fixture0." + extension)
            # Lake resolves the trace's names here, so it never rehashes or recaches them.
            self.assertTrue(os.path.samefile(output, self.artifacts / name))
            self.assertEqual(output.stat().st_mode & 0o777, 0o444)
        # Lake's own caching leaves the reused archive read-only as well.
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o444)
        self.assertFalse(self.record.exists())

    def test_unverified_module_is_left_to_stock_lake_beside_installed_ones(self):
        second = self.second_record()
        restored, _ = self.unpack_native(self.hashed(wrong=(1,)))
        self.assertEqual(restored, 1)
        self.assertEqual(self.installed(), sorted(self.names(0).values()))
        # The rejected module could resolve through names other modules installed,
        # so it is not prepared at all: its mapping makes Lake restore and hash it.
        lib = self.root / ".lake/build/lib/lean/TauCeti"
        self.assertTrue((lib / "Fixture0.olean").is_file())
        self.assertFalse((lib / "Fixture1.olean").exists())
        self.assertFalse(self.record.exists())
        self.assertTrue(second.exists())

    def test_nothing_verified_keeps_the_previous_restoration(self):
        self.second_record()
        restored, _ = self.unpack_native(self.hashed(wrong=(0, 1)))
        self.assertEqual(restored, 2)
        self.assertEqual(self.installed(), [])
        self.assertTrue((self.root / ".lake/build/lib/lean/TauCeti/Fixture1.olean").is_file())

    def test_failed_hash_helper_installs_nothing(self):
        class Failed:
            returncode = 1
            def __init__(self, *args, **kwargs):
                pass
            def communicate(self, timeout=None):
                return "", None
            def kill(self):
                pass
            def wait(self):
                pass
        self.second_record()
        restored, _ = self.unpack_native(Failed)
        self.assertEqual(restored, 2)
        self.assertEqual(self.installed(), [])

    def test_trace_and_outputs_must_correspond_one_to_one(self):
        names = self.names(0)
        complete = self.module_outputs(0)
        cases = {
            "extra output": dict(extra=("bc",)),
            "missing descriptor": dict(outputs={k: v for k, v in complete.items() if k != "c"}),
            "wrong extension": dict(outputs={**complete, "i": names["ilean"].replace("ilean", "ir")}),
            "module mode contradicts the oleans": dict(outputs={**complete, "m": False}),
            "missing module mode": dict(outputs={k: v for k, v in complete.items() if k != "m"}),
            # The outputs below are absent from the archive as well as from the trace.
            "IR without its signature": dict(outputs={k: v for k, v in complete.items() if k != "rs"},
                                             omit=("ir.sig",)),
            "postponed compilation": dict(outputs={k: v for k, v in complete.items() if k not in ("rs", "r", "c")},
                                          omit=("ir.sig", "ir", "c")),
            "module without IR": dict(outputs={k: v for k, v in complete.items() if k not in ("rs", "r")},
                                      omit=("ir.sig", "ir")),
            "other archive": dict(outputs={**complete, "l": "0000000000000000.ltar"}),
            "unknown key": dict(outputs={**complete, "x": 1}),
            "not an object": dict(outputs=[names["olean"]]),
        }
        record = dict(hash="0123456789abcdef", archive=self.archive.name)
        for label, fixture in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                ((record, module, files),) = self.native_fixture([record], Path(tmp), **fixture)
                self.assertEqual(bulk.expanded_artifacts(record, module, files), [])
        with tempfile.TemporaryDirectory() as tmp:
            ((record, module, files),) = self.native_fixture(
                [record], Path(tmp), outputs={**complete, "l": self.archive.name})
            self.assertEqual(len(bulk.expanded_artifacts(record, module, files)), 7)
        with tempfile.TemporaryDirectory() as tmp:
            plain = {"m": False, "o": [names["olean"]], "i": names["ilean"], "c": names["c"]}
            ((record, module, files),) = self.native_fixture(
                [record], Path(tmp), outputs=plain, omit=("olean.server", "olean.private", "ir.sig", "ir"))
            self.assertEqual(len(bulk.expanded_artifacts(record, module, files)), 3)
            self.assertEqual(bulk.expanded_artifacts(dict(record, hash="ffffffffffffffff"), module, files), [])

@unittest.skipUnless(INTEGRATION, "pass --integration with the pinned Lake installed")
class StockLakeTests(unittest.TestCase):
    def test_replay_and_changes_match_stock_lake(self):
        lake = shutil.which("lake")
        with tempfile.TemporaryDirectory(prefix="bulk-unpack-test-") as tmp:
            root = Path(tmp)

            def project(name):
                path = root / name
                path.mkdir()
                (path / "lean-toolchain").write_text(os.environ.get("ELAN_TOOLCHAIN", "leanprover/lean4:v4.36.0-rc2") + "\n")
                (path / "lakefile.toml").write_text(
                    'name = "TauCeti"\nplatformIndependent = true\ndefaultTargets = ["TauCeti"]\n'
                    '[[lean_lib]]\nname = "TauCeti"\nglobs = ["TauCeti.*"]\n')
                (path / "TauCeti").mkdir()
                for file, text in {"TauCeti.lean": "module\n",
                                   "TauCeti/A.lean": "module\npublic def a : Nat := 42\n",
                                   "TauCeti/B.lean": "module\npublic import TauCeti.A\npublic def b := a + 1\n",
                                   "TauCeti/C.lean": "module\npublic def c : Nat := 7\n"}.items():
                    (path / file).write_text(text)
                return path

            def run(path, *args, expected=0):
                env = {**os.environ, "LAKE_CACHE_DIR": str(path / ".lake/cache"),
                       "LAKE_ARTIFACT_CACHE": "true", "LAKE_RESTORE_ARTIFACTS": "true",
                       "LAKE_NO_CACHE": "true"}
                result = subprocess.run([lake, *args], cwd=path, env=env,
                                        text=True, capture_output=True, timeout=120)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                return result.stdout + result.stderr

            producer = project("producer")
            run(producer, "build", "--iofail", "-o", ".lake/map.jsonl")
            run(producer, "cache", "stage", ".lake/map.jsonl", str(root / "staging"))
            for change in (False, True):
                maps = []
                for mode in ("stock", "bulk"):
                    path = project(f"{mode}-{change}")
                    run(path, "cache", "unstage", str(root / "staging"))
                    if change:
                        (path / "TauCeti/A.lean").write_text("module\npublic def a : Int := 43\n")
                        (path / "TauCeti/C.lean").unlink()
                        (path / "TauCeti/D.lean").write_text("module\npublic def d : Nat := 8\n")
                    restored = bulk.unpack(path) if mode == "bulk" else 0
                    if mode == "bulk":
                        # Future toolchain layouts may safely use stock restoration.
                        self.assertEqual(restored, 3 if change else 4)
                        if change:
                            self.assertFalse((path / ".lake/build/lib/lean/TauCeti/C.olean").exists())
                        # Lake finds each downloaded archive at its `Module.ltarFile`.
                        archive = (path / ".lake/build/ir/TauCeti/A.ltar").stat().st_ino
                        downloaded = (path / ".lake/cache/artifacts").glob("*.ltar")
                        self.assertIn(archive, {p.stat().st_ino for p in downloaded})
                        # Each restored trace resolves in the expanded cache to the restored file.
                        for trace in (path / ".lake/build/lib/lean/TauCeti").glob("*.trace"):
                            name = json.loads(trace.read_text())["outputs"]["o"][0]
                            self.assertTrue(os.path.samefile(trace.with_suffix(".olean"),
                                                             path / ".lake/cache/artifacts" / name))
                    if change:
                        run(path, "build", "--no-build", expected=3)
                    log = run(path, "build", "--iofail", "--rehash", "-o", ".lake/map.jsonl")
                    if restored:
                        self.assertNotIn("Unpacked", log)
                        if change:
                            self.assertIn("Built TauCeti.A", log)
                            self.assertIn("Built TauCeti.B", log)
                            self.assertIn("Built TauCeti.D", log)
                        else:
                            self.assertNotIn("Built TauCeti", log)
                            # Lake took its cached-artifact path: it did not rehash and
                            # recache the restored outputs, which writes these hash files.
                            self.assertFalse((path / ".lake/build/lib/lean/TauCeti/A.olean.hash").exists())
                            # The output map reused the downloaded archive instead of repacking it.
                            self.assertEqual((path / ".lake/build/ir/TauCeti/A.ltar").stat().st_ino, archive)
                    lines = (path / ".lake/map.jsonl").read_text().splitlines()
                    maps.append((json.loads(lines[0]), dict(map(json.loads, lines[1:]))))
                self.assertEqual(maps[0], maps[1])
            # Stale artifacts for deleted sources must not make a broken import pass.
            for mode in ("stock", "bulk"):
                path = project(f"deleted-import-{mode}")
                run(path, "cache", "unstage", str(root / "staging"))
                (path / "TauCeti/C.lean").unlink()
                (path / "TauCeti/B.lean").write_text(
                    "module\npublic import TauCeti.C\npublic def b := c + 1\n")
                if mode == "bulk":
                    self.assertEqual(bulk.unpack(path), 3)
                run(path, "build", "--iofail", expected=1)

            # Real repair caches retain historical versions. Prepare the older
            # version first and prove native selection still restores the current
            # cached version without compilation and with the identical final map.
            (producer / "TauCeti/A.lean").write_text("module\npublic def a : Int := 43\n")
            run(producer, "build", "--iofail", "--rehash", "-o", ".lake/map.jsonl")
            run(producer, "cache", "stage", ".lake/map.jsonl", str(root / "new-staging"))
            maps = []
            for mode in ("stock", "bulk"):
                path = project(f"history-{mode}")
                (path / "TauCeti/A.lean").write_text("module\npublic def a : Int := 43\n")
                run(path, "cache", "unstage", str(root / "staging"))
                run(path, "cache", "unstage", str(root / "new-staging"))
                if mode == "bulk":
                    self.assertEqual(bulk.unpack(path), 4)
                log = run(path, "build", "--iofail", "--rehash", "-o", ".lake/map.jsonl")
                self.assertNotIn("Built TauCeti", log)
                lines = (path / ".lake/map.jsonl").read_text().splitlines()
                maps.append((json.loads(lines[0]), dict(map(json.loads, lines[1:]))))
            self.assertEqual(maps[0], maps[1])


if __name__ == "__main__":
    unittest.main()
