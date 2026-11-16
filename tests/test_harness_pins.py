"""One shared-harness revision for project CI, with every bound workflow literal derived from it."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tauceti_tools import harness_pins as hp

ROOT = Path(__file__).resolve().parents[1]
SCOPE = ROOT / "tauceti_tools" / "assets" / "scripts" / "lint_scope_files.py"
TOOLS = "TauCetiProject/TauCetiTools"
OLD, NEW, OTHER = "a" * 40, "b" * 40, "c" * 40


def source(revision, repository=TOOLS):
    return (
        json.dumps(
            {
                "workflow": ".github/workflows/source-candidate.yml",
                "harness": {"name": repository},
                "harness_revision": revision,
            },
            indent=2,
        )
        + "\n"
    )


def caller(revision):
    return f"""name: Mixed queue candidate
jobs:
  candidate:
    uses: {TOOLS}/.github/workflows/pr-build.yml@{revision}
    with:
      tools_repository: {TOOLS}
      tools_ref: {revision}
"""


def project_build(revision, credentials=None):
    return f"""name: pr-build
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      - uses: {TOOLS}/.github/actions/setup@{revision}
        with:
          tools-repository: {TOOLS}
          tools-ref: {revision}
      - uses: {TOOLS}/.github/actions/ci-credentials@{credentials or revision}
"""


def unrelated(revision=OTHER):
    return f"""name: housekeeping
jobs:
  sweep:
    runs-on: ubuntu-24.04
    steps:
      - uses: {TOOLS}/.github/actions/setup@{revision}
"""


def workflows(revision, **changes):
    value = {
        "source-candidate.yml": caller(revision),
        "pr-build.yml": project_build(revision),
        "housekeeping.yml": unrelated(),
    }
    value.update(changes)
    return {k: v for k, v in value.items() if v is not None}


class Literals(unittest.TestCase):
    def test_consistent_pins_pass_and_unbound_workflows_keep_their_own_pins(self):
        self.assertEqual(hp.check(source(NEW), workflows(NEW), source(OLD), workflows(OLD)), [])

    def test_bound_set_is_the_caller_harness_callers_and_workflows_at_the_revision(self):
        self.assertEqual(hp.bound(source(OLD), workflows(OLD)), {"source-candidate.yml", "pr-build.yml"})

    def test_moving_the_source_without_a_bound_workflow_is_refused(self):
        errors = hp.check(source(NEW), workflows(NEW, **{"pr-build.yml": project_build(OLD)}), source(OLD), workflows(OLD))
        self.assertEqual(len(errors), 3)
        self.assertTrue(all(e.startswith(".github/workflows/pr-build.yml:") for e in errors), errors)
        self.assertIn(f"pins {OLD}, but .tauceti/ci-source.json harness_revision is {NEW}", errors[0])

    def test_moving_a_bound_workflow_without_the_source_is_refused(self):
        errors = hp.check(source(OLD), workflows(OLD, **{"pr-build.yml": project_build(NEW)}), source(OLD), workflows(OLD))
        self.assertEqual(len(errors), 3)

    def test_a_partially_moved_workflow_is_refused(self):
        errors = hp.check(source(NEW), workflows(NEW, **{"pr-build.yml": project_build(NEW, credentials=OLD)}))
        self.assertEqual(len(errors), 1)
        self.assertIn("pr-build.yml:10", errors[0])

    def test_the_caller_must_exist_and_pin_the_harness(self):
        self.assertIn("caller workflow is missing", hp.check(source(NEW), workflows(NEW, **{"source-candidate.yml": None}))[0])
        empty = hp.check(source(NEW), workflows(NEW, **{"source-candidate.yml": "name: empty\n"}))
        self.assertIn("no TauCetiProject/TauCetiTools pin", empty[0])

    def test_source_must_name_a_full_revision(self):
        for text in ("{}", "not json", source("main"), source(NEW.upper()), source(NEW, repository="bad name")):
            with self.subTest(text=text), self.assertRaises(hp.PinError):
                hp.check(text, workflows(NEW))

    def test_set_derives_every_bound_literal_from_the_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".tauceti").mkdir()
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / hp.SOURCE).write_text(source(OLD))
            for name, text in workflows(OLD).items():
                (root / ".github" / "workflows" / name).write_text(text)
            changed = hp.set_revision(root, NEW)
            self.assertEqual(
                sorted(p.relative_to(root).as_posix() for p in changed),
                [".github/workflows/pr-build.yml", ".github/workflows/source-candidate.yml", hp.SOURCE],
            )
            self.assertEqual((root / ".github/workflows/housekeeping.yml").read_text(), unrelated())
            self.assertEqual(hp.main(["check", "--root", str(root)]), 0)
            self.assertEqual(hp.set_revision(root, NEW), [])
            with self.assertRaises(hp.PinError):
                hp.set_revision(root, "main")
            # A single snapshot binds by revision; the scope guard also binds at the base.
            (root / ".github/workflows/pr-build.yml").write_text(project_build(NEW, credentials=OLD))
            self.assertEqual(hp.main(["check", "--root", str(root)]), 1)


def git(root, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env).stdout


class ScopeGuard(unittest.TestCase):
    """`lint_scope_files.py --policy` refuses an inconsistent pin change at the PR head."""

    def commit(self, root, files, message):
        for path, text in files.items():
            target = root / path
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        git(root, "add", "-A")
        git(root, "commit", "-q", "--allow-empty", "-m", message)
        return git(root, "rev-parse", "HEAD").strip()

    def scope(self, root, base, head):
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        return subprocess.run(
            [sys.executable, str(SCOPE), str(root), base, head, "--policy"], capture_output=True, text=True, env=env
        )

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        git(self.root, "init", "-q")
        files = {hp.SOURCE: source(OLD), "Math/A.lean": "-- a\n"}
        files.update({".github/workflows/" + k: v for k, v in workflows(OLD).items()})
        self.base = self.commit(self.root, files, "base")

    def tearDown(self):
        self.directory.cleanup()

    def test_a_consistent_bump_lists_its_files(self):
        files = {hp.SOURCE: source(NEW)}
        files.update({".github/workflows/" + k: v for k, v in workflows(NEW).items()})
        result = self.scope(self.root, self.base, self.commit(self.root, files, "bump"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(".tauceti/ci-source.json\n", result.stdout)

    def test_a_source_only_bump_is_refused(self):
        result = self.scope(self.root, self.base, self.commit(self.root, {hp.SOURCE: source(NEW)}, "partial"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::shared harness pin: .github/workflows/pr-build.yml", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_workflow_only_bump_is_refused(self):
        head = self.commit(self.root, {".github/workflows/pr-build.yml": project_build(NEW)}, "partial")
        self.assertEqual(self.scope(self.root, self.base, head).returncode, 1)

    def test_changes_elsewhere_are_not_inspected(self):
        head = self.commit(self.root, {"Math/A.lean": "-- b\n"}, "lean")
        result = self.scope(self.root, self.base, head)
        self.assertEqual((result.returncode, result.stdout), (0, "Math/A.lean\n"))

    def test_projects_without_a_policy_are_unaffected(self):
        head = self.commit(self.root, {hp.SOURCE: None}, "drop policy")
        head = self.commit(self.root, {".github/workflows/pr-build.yml": project_build(NEW)}, "edit")
        parent = git(self.root, "rev-parse", "HEAD~1").strip()
        self.assertEqual(self.scope(self.root, parent, head).returncode, 0)

    def test_a_symlinked_workflow_fails_closed(self):
        target = self.root / ".github/workflows/pr-build.yml"
        target.unlink()
        target.symlink_to("housekeeping.yml")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "symlink")
        head = git(self.root, "rev-parse", "HEAD").strip()
        result = self.scope(self.root, self.base, head)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a regular file", result.stderr)


if __name__ == "__main__":
    unittest.main()
