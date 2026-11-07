"""The guard resolves trusted inputs and rejects candidate-controlled policy changes."""

import copy
import io
import json
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tauceti_tools.project import ConfigError, Project
from tauceti_tools.upstreams import ResolutionUnavailable, api, guard, release, sandbox_command, toolchain_for, update

PIN = "leanprover/lean4:v4.36.0-rc1"


class UpstreamTests(unittest.TestCase):
    def test_candidate_manifest_shape_is_rejected_before_external_lookups(self):
        direct = {"name": "mathlib", "type": "git", "url": "https://github.com/leanprover-community/mathlib4",
                  "rev": "a" * 40, "inputRev": "master"}
        malformed = [{}, [], {"packages": {}}, {"packages": [{}]},
                     {"packages": [dict(direct, rev=17)]}, {"packages": [direct, direct]}]
        with tempfile.TemporaryDirectory() as tmp:
            base, candidate = (Path(tmp) / name for name in ("base", "candidate"))
            for root in (base, candidate):
                root.mkdir()
                (root / "lakefile.toml").write_text('name = "Project"\n')
                (root / "lean-toolchain").write_text(PIN + "\n")
            (base / "lake-manifest.json").write_text(json.dumps({"packages": [direct]}))
            for raw in [*(json.dumps(value) for value in malformed), "[" * 2000 + "]" * 2000]:
                with self.subTest(raw=raw[:80]), patch("tauceti_tools.upstreams.api") as lookup:
                    (candidate / "lake-manifest.json").write_text(raw)
                    with self.assertRaises(ConfigError) as error:
                        guard(SimpleNamespace(github_upstreams=[]), base, base, candidate)
                    self.assertNotIsInstance(error.exception, ResolutionUnavailable)
                    lookup.assert_not_called()
            (candidate / "lake-manifest.json").write_text(json.dumps({"packages": [direct]}))
            (base / "lake-manifest.json").write_text("[]")
            with self.assertRaises(ResolutionUnavailable):
                guard(SimpleNamespace(github_upstreams=[]), base, base, candidate)

    def test_dependency_lookup_distinguishes_public_missing_pins_from_unavailable_reads(self):
        def reply(status, value):
            return subprocess.CompletedProcess([], 0 if status == 200 else 1,
                                               f"HTTP/2 {status}\r\nContent-Type: application/json\r\n\r\n{json.dumps(value)}",
                                               "credential detail must not appear in errors")

        target = "repos/owner/dependency/compare/old...new"
        for status in (404, 422):
            for metadata in ({"private": False}, {"private": True}, {}):
                with self.subTest(status=status, metadata=metadata), patch(
                    "tauceti_tools.upstreams.subprocess.run",
                    side_effect=[reply(status, {"message": "Not Found"}), reply(200, metadata)],
                ) as run:
                    expected = ConfigError if metadata.get("private") is False else ResolutionUnavailable
                    with self.assertRaises(expected) as error:
                        api(target)
                    if expected is ConfigError:
                        self.assertNotIsInstance(error.exception, ResolutionUnavailable)
                    self.assertNotIn("credential detail", str(error.exception))
                    self.assertEqual(run.call_args_list[0].args[0],
                                     ["gh", "api", "--hostname", "github.com", "--include", target])
                    self.assertEqual(run.call_args_list[1].args[0][-1], "repos/owner/dependency")
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status), patch("tauceti_tools.upstreams.subprocess.run",
                                                   return_value=reply(status, {"message": "unavailable"})):
                with self.assertRaisesRegex(ResolutionUnavailable, f"HTTP {status}"):
                    api(target)
        for failure in (OSError("credential detail"), subprocess.TimeoutExpired("gh", 30)):
            with self.subTest(failure=failure), patch("tauceti_tools.upstreams.subprocess.run", side_effect=failure):
                with self.assertRaises(ResolutionUnavailable) as error:
                    api(target)
                self.assertNotIn("credential detail", str(error.exception))
        with patch("tauceti_tools.upstreams.subprocess.run", return_value=reply(200, {"status": "ahead"})):
            self.assertEqual(api(target), {"status": "ahead"})

    def test_missing_sandbox_is_infrastructure(self):
        with patch("tauceti_tools.upstreams.shutil.which", return_value=None):
            with self.assertRaises(ResolutionUnavailable):
                sandbox_command(Path("work"), Path("sysroot"), Path("driver"))

    def test_guard_reproduces_pins_from_base_and_rejects_tampering(self):
        direct = {"name": "cslib", "url": "https://github.com/leanprover/cslib", "type": "git",
                  "rev": "a" * 40, "inputRev": "main", "inherited": False}
        shared = {"name": "mathlib", "url": "https://github.com/leanprover-community/mathlib4",
                  "type": "git", "rev": "b" * 40, "inherited": True}
        before = {"packages": [direct, shared], "name": "Project"}
        expected = {"packages": [dict(direct, rev="c" * 40), dict(shared, rev="d" * 40)], "name": "Project"}
        project = Project({"repositories": {"content": "one/Project", "roadmap": "one/Roadmap"},
                           "upstreams": [{"package": "cslib", "repo": "leanprover/cslib",
                                          "branch": "main", "update": True}]})
        with tempfile.TemporaryDirectory() as tmp:
            base, candidate = (Path(tmp) / name for name in ("base", "candidate"))
            for root in (base, candidate):
                root.mkdir()
                (root / "lakefile.toml").write_text('name = "Project"\n')
                (root / "lean-toolchain").write_text(PIN + "\n")
            (base / "lake-manifest.json").write_text(json.dumps(before))
            (candidate / "lake-manifest.json").write_text(json.dumps(expected))
            with patch("tauceti_tools.upstreams.api", return_value={"status": "ahead"}), \
                    patch("tauceti_tools.upstreams.toolchain_for", return_value=PIN), \
                    patch("tauceti_tools.upstreams.resolve", return_value=expected) as resolver, \
                    redirect_stdout(io.StringIO()):
                guard(project, base, base, candidate)
                resolver.assert_called_once_with(base, [dict(direct, rev="c" * 40)], PIN)
                reordered = dict(expected, packages=list(reversed(expected["packages"])))
                (candidate / "lake-manifest.json").write_text(json.dumps(reordered))
                guard(project, base, base, candidate)
                for key, value in (("rev", "e" * 40), ("url", "https://github.com/attacker/fork"),
                                   ("configFile", "payload.lean")):
                    tampered = copy.deepcopy(expected)
                    tampered["packages"][1][key] = value
                    (candidate / "lake-manifest.json").write_text(json.dumps(tampered))
                    with self.assertRaisesRegex(ConfigError, "differs from Lake"):
                        guard(project, base, base, candidate)
                (candidate / "lake-manifest.json").write_text(json.dumps(expected))
                resolver.reset_mock()
                project.raw["upstreams"][0]["update"] = False
                with self.assertRaisesRegex(ConfigError, "not configured"):
                    guard(project, base, base, candidate)
                resolver.assert_not_called()
                (candidate / "lakefile.toml").write_text('name = "ChangedPolicy"\n')
                with self.assertRaisesRegex(ConfigError, "human-owned"):
                    guard(project, base, base, candidate)
                resolver.assert_not_called()
                # A source-only PR behind a base pin update still passes without resolution.
                guard(project, candidate, base, base)

    def test_failed_resolution_preserves_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lakefile.toml").write_text('name = "Project"\n')
            (root / "lean-toolchain").write_text(PIN + "\n")
            (root / "lake-manifest.json").write_text('{"packages": []}\n')
            for args in (("init", "-q"), ("add", "."),
                         ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Fixture")):
                subprocess.run(["git", "-C", str(root), *args], check=True)
            original = {name: (root / name).read_bytes() for name in ("lake-manifest.json", "lean-toolchain")}
            with patch("tauceti_tools.upstreams.inspect", return_value=([{"package": "cslib", "new": "c" * 40}], PIN)), \
                    patch("tauceti_tools.upstreams.resolve", side_effect=ConfigError("Lake failure")), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ConfigError, "Lake failure"):
                    update(None, root, apply=True)
            self.assertEqual(original, {name: (root / name).read_bytes() for name in original})

    def test_newest_official_toolchain_without_requiring_equality_or_downgrading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lean-toolchain").write_text(PIN)
            rows = [{"name": "cslib", "url": "https://github.com/leanprover/cslib", "type": "git", "rev": "a" * 40},
                    {"name": "TauCeti", "url": "https://github.com/TauCetiProject/TauCeti", "type": "git", "rev": "b" * 40}]
            with patch("tauceti_tools.upstreams.file_at", side_effect=["leanprover/lean4:v4.35.0", PIN]):
                self.assertEqual(toolchain_for(root, rows), PIN)
        self.assertLess(release(PIN), release("leanprover/lean4:v4.36.0"))
        with self.assertRaises(ConfigError):
            release("attacker/lean:v4.36.0")

    def test_updater_and_guard_install_the_same_pinned_runtime(self):
        root = Path(__file__).resolve().parents[1]
        pins = re.compile(r"^\s*((?:BWRAP|ELAN)_[A-Z0-9_]+):\s*(\S+)\s*$", re.MULTILINE)
        shared = dict(pins.findall((root / ".github/workflows/pr-build.yml").read_text()))
        updater = dict(pins.findall((root / "tauceti_tools/assets/project/.github/workflows/update-upstreams.yml").read_text()))
        self.assertEqual(len(shared), 5)
        self.assertEqual(updater, shared)


if __name__ == "__main__":
    unittest.main()
