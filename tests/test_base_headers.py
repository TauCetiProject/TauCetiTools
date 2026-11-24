"""base-headers.py copies each changed module's base header out of the restored Lake cache."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts"
spec = importlib.util.spec_from_file_location("base_headers", SCRIPTS / "base-headers.py")
headers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(headers)

BASE = "b" * 40
OTHER = "c" * 40


class BaseHeadersTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cache = self.root / "cache"
        (self.cache / "artifacts").mkdir(parents=True)
        (self.cache / "revisions/TauCeti").mkdir(parents=True)
        self.dest = self.root / "base-oleans"
        self.modules = self.root / "modules"
        self.contents = {}
        self.counter = 0

    def archive(self, module, olean=None, trace=None):
        """Write a fixture archive for `module`; return its (input hash, archive name)."""
        self.counter += 1
        digest = f"{self.counter:016x}"
        name = f"{self.counter + 0xa000:016x}.ltar"
        last = module.split(".")[-1]
        (self.cache / "artifacts" / name).write_bytes(
            b"LTR4\0" + (trace or last + ".trace").encode() + b"\0\x06compressed")
        self.contents[name] = (module, olean if olean is not None else module.encode())
        return digest, name

    def revision(self, rev, rows):
        lines = ['"2026-03-17"'] + [json.dumps(list(row)) for row in rows]
        (self.cache / "revisions/TauCeti" / (rev + ".jsonl")).write_text("\n".join(lines) + "\n")

    def leantar(self, *args, **kwargs):
        self.requests.append(json.loads(kwargs["input"]))
        for request in self.requests[-1]:
            module, olean = self.contents[Path(request["file"]).name]
            lib, ir = map(Path, request["base"])
            lib.mkdir(parents=True)
            ir.mkdir(parents=True)
            last = module.split(".")[-1]
            (lib / (last + ".olean")).write_bytes(olean)
            (lib / (last + ".ilean")).write_text(json.dumps({"decls": {}, "module": module, "version": 6}))

    def run_headers(self, modules, base=BASE, cache=None):
        self.modules.write_text("\n".join(modules) + "\n")
        self.requests = []
        with patch.object(headers.subprocess, "check_output", return_value="/toolchain\n"), \
                patch.object(headers.subprocess, "run", side_effect=self.leantar) as run:
            status = headers.main([str(self.modules), str(self.dest), "--cache",
                                   str(self.cache if cache is None else cache), "--base", base])
        self.run = run
        return status

    def header(self, module):
        path = self.dest / (module.replace(".", "/") + ".olean")
        return path.read_bytes() if path.exists() else None

    def test_extracts_only_archives_that_can_hold_a_changed_module(self):
        rows = [self.archive("TauCeti.A.Basic", b"base A.Basic"), self.archive("TauCeti.B.Basic"),
                self.archive("TauCeti.C"), self.archive("TauCeti.D.Map", b"base Map")]
        self.revision(BASE, rows)
        self.assertEqual(self.run_headers(["TauCeti.A.Basic", "TauCeti.D.Map", "TauCeti.New"]), 0)
        self.assertEqual(self.header("TauCeti.A.Basic"), b"base A.Basic")
        self.assertEqual(self.header("TauCeti.D.Map"), b"base Map")
        # Another module with the same last name is extracted but never copied.
        self.assertIsNone(self.header("TauCeti.B.Basic"))
        self.assertIsNone(self.header("TauCeti.New"))
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[0], ["/toolchain/bin/leantar", "-x", "-j", "-"])
        (request,) = self.requests
        self.assertEqual(sorted(Path(r["file"]).name for r in request),
                         sorted(name for _, name in (rows[0], rows[1], rows[3])))
        self.assertEqual({r["hash"] for r in request}, {rows[0][0], rows[1][0], rows[3][0]})

    def test_base_revision_map_wins(self):
        older = self.archive("TauCeti.A", b"older A")
        base = self.archive("TauCeti.A", b"base A")
        self.revision(OTHER, [older])
        self.revision(BASE, [base])
        self.assertEqual(self.run_headers(["TauCeti.A"]), 0)
        self.assertEqual(self.header("TauCeti.A"), b"base A")

    def test_nothing_to_extract_without_candidates(self):
        self.revision(BASE, [self.archive("TauCeti.A")])
        self.assertEqual(self.run_headers(["TauCeti.New"]), 0)
        self.run.assert_not_called()
        self.assertFalse(self.dest.exists())

    def test_headers_already_saved_need_no_cache(self):
        saved = self.dest / "TauCeti/A.olean"
        saved.parent.mkdir(parents=True)
        saved.write_bytes(b"from the build")
        self.assertEqual(self.run_headers(["TauCeti.A"], cache=self.root / "absent"), 0)
        self.assertEqual(saved.read_bytes(), b"from the build")
        self.run.assert_not_called()

    def test_missing_cache_or_revision_map_is_unavailable(self):
        self.assertEqual(self.run_headers(["TauCeti.A"]), 1)
        self.assertEqual(self.run_headers(["TauCeti.A"], cache=""), 1)

    def test_missing_base_archive_is_unavailable_but_another_revision_may_lack_one(self):
        present = self.archive("TauCeti.A", b"base A")
        self.revision(OTHER, [("ffffffffffffffff", "eeeeeeeeeeeeeeee.ltar")])
        self.revision(BASE, [present])
        self.assertEqual(self.run_headers(["TauCeti.A"]), 0)
        self.assertEqual(self.header("TauCeti.A"), b"base A")
        shutil.rmtree(self.dest)
        self.revision(BASE, [present, ("ffffffffffffffff", "eeeeeeeeeeeeeeee.ltar")])
        self.assertEqual(self.run_headers(["TauCeti.A"]), 1)

    def test_unknown_archive_format_or_malformed_input_is_unavailable(self):
        digest, name = self.archive("TauCeti.A")
        self.revision(BASE, [(digest, name)])
        for header in (b"LTR5\0A.trace\0", b"LTR4\0A.olean\0", b"LTR4\0A.trace"):
            with self.subTest(header=header):
                (self.cache / "artifacts" / name).write_bytes(header)
                self.assertEqual(self.run_headers(["TauCeti.A"]), 1)
        self.archive("TauCeti.A")
        for module in ("Mathlib.A", "TauCeti", "TauCeti.A/B"):
            with self.subTest(module=module):
                self.assertEqual(self.run_headers([module]), 1)
        self.revision(BASE, [("short", name)])
        self.assertEqual(self.run_headers(["TauCeti.B"]), 1)

    def test_symlinked_archive_is_unavailable(self):
        digest, name = self.archive("TauCeti.A")
        target = self.root / "elsewhere.ltar"
        (self.cache / "artifacts" / name).rename(target)
        (self.cache / "artifacts" / name).symlink_to(target)
        self.revision(BASE, [(digest, name)])
        self.assertEqual(self.run_headers(["TauCeti.A"]), 1)

    def test_failed_extraction_is_unavailable(self):
        self.revision(BASE, [self.archive("TauCeti.A")])
        self.modules.write_text("TauCeti.A\n")
        with patch.object(headers.subprocess, "check_output", return_value="/toolchain\n"), \
                patch.object(headers.subprocess, "run",
                             side_effect=subprocess.CalledProcessError(101, "leantar")):
            self.assertEqual(headers.main([str(self.modules), str(self.dest), "--cache", str(self.cache)]), 1)


@unittest.skipUnless(os.environ.get("BASE_HEADERS_INTEGRATION") and shutil.which("lake"),
                     "set BASE_HEADERS_INTEGRATION=1 with the pinned Lake installed")
class StockLakeTests(unittest.TestCase):
    def test_reads_the_base_header_from_a_real_cache(self):
        lake = shutil.which("lake")
        with tempfile.TemporaryDirectory(prefix="base-headers-test-") as tmp:
            root = Path(tmp)

            def project(name):
                path = root / name
                (path / "TauCeti/X").mkdir(parents=True)
                (path / "lean-toolchain").write_text(
                    os.environ.get("ELAN_TOOLCHAIN", "leanprover/lean4:v4.37.0-rc1") + "\n")
                (path / "lakefile.toml").write_text(
                    'name = "TauCeti"\nplatformIndependent = true\ndefaultTargets = ["TauCeti"]\n'
                    '[[lean_lib]]\nname = "TauCeti"\nglobs = ["TauCeti.*"]\n')
                for file, text in {"TauCeti.lean": "module\n",
                                   "TauCeti/Basic.lean": "module\npublic def b : Nat := 1\n",
                                   "TauCeti/X/Basic.lean": "module\npublic def a : Nat := 42\n",
                                   "TauCeti/Y.lean": "module\npublic import TauCeti.X.Basic\n"
                                                     "public def y := a + 1\n"}.items():
                    (path / file).write_text(text)
                return path

            def run(path, *args):
                env = {**os.environ, "LAKE_CACHE_DIR": str(path / ".lake/cache"),
                       "LAKE_ARTIFACT_CACHE": "true", "LAKE_RESTORE_ARTIFACTS": "true",
                       "LAKE_NO_CACHE": "true"}
                result = subprocess.run([lake, *args], cwd=path, env=env, text=True,
                                        capture_output=True, timeout=300)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            producer = project("producer")
            run(producer, "build", "--iofail", "-o", ".lake/map.jsonl")
            run(producer, "cache", "stage", ".lake/map.jsonl", str(root / "staging"))
            candidate = project("candidate")
            (candidate / "TauCeti/X/Basic.lean").write_text(
                "module\npublic def a : Nat := 42\npublic def a' : Nat := 43\n")
            (candidate / "TauCeti/New.lean").write_text("module\npublic def n : Nat := 0\n")
            run(candidate, "cache", "unstage", str(root / "staging"))
            # `lake cache get --rev` leaves the revision's map beside the records.
            maps = candidate / ".lake/cache/revisions/TauCeti"
            maps.mkdir(parents=True)
            shutil.copyfile(root / "staging/outputs.jsonl", maps / (BASE + ".jsonl"))
            modules = root / "modules"
            modules.write_text("TauCeti.X.Basic\nTauCeti.New\n")
            dest = candidate / ".lake/telemetry/base-oleans"
            # The sandbox runs this from the project, where `lean` is the project's toolchain.
            previous = os.getcwd()
            os.chdir(candidate)
            self.addCleanup(os.chdir, previous)
            with patch.dict(os.environ, {"TMPDIR": str(root)}):
                self.assertEqual(headers.main([str(modules), str(dest), "--cache",
                                               str(candidate / ".lake/cache"), "--base", BASE]), 0)
            built = producer / ".lake/build/lib/lean/TauCeti"
            self.assertEqual((dest / "TauCeti/X/Basic.olean").read_bytes(),
                             (built / "X/Basic.olean").read_bytes())
            self.assertEqual(sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()),
                             ["TauCeti/X/Basic.olean"])


if __name__ == "__main__":
    unittest.main()
