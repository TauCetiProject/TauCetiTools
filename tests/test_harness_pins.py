"""One harness revision for project CI and the runner-group allow-lists derived from it."""

import base64
import copy
import json
import tempfile
import unittest
import unittest.mock
import urllib.parse
from pathlib import Path

from tauceti_tools import harness_pins as hp

TOOLS = "TauCetiProject/TauCetiTools"
PROJECT = "TauCetiProject/TauCeti"
OLD, NEW, NEWER, STRAY = "a" * 40, "b" * 40, "c" * 40, "d" * 40
MAIN, PREV, PR_HEAD, PR_BASE, RUNNING = "1" * 40, "2" * 40, "3" * 40, "4" * 40, "5" * 40
STAGING = '["self-hosted","linux","x64","tauceti-fleet-staging","fleet-m"]'
CONTROL = '{"group":"TauCetiQueueControl","labels":["self-hosted","linux","x64","tauceti-fleet-queue-control"]}'


def source(revision, repository=TOOLS):
    return json.dumps({"harness": {"name": repository}, "harness_revision": revision}, indent=2) + "\n"


def caller(revision, *, control=CONTROL, staging=STAGING):
    return f"""name: Mixed queue candidate
on:
  repository_dispatch:
    types: [tauceti-source-candidate-v2]
jobs:
  candidate:
    if: ${{{{ vars.ENABLED == 'true' }}}}
    uses: {TOOLS}/.github/workflows/pr-build.yml@{revision}
    with:
      tools_repository: {TOOLS}
      tools_ref: {revision}
      project_ref: ${{{{ github.workflow_sha }}}}
      source_candidate_enabled: true
      source_candidate_runner: '{staging}'
      source_candidate_control_runner: '{control}'
"""


def project_build(revision):
    return f"""name: pr-build
on: pull_request_target
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      - uses: {TOOLS}/.github/actions/setup@{revision}
        with:
          tools-repository: {TOOLS}
          tools-ref: {revision}
      - uses: {TOOLS}/.github/actions/ci-credentials@{revision}
"""


HARNESS = {
    ".github/workflows/pr-build.yml": """name: pr-build
on:
  workflow_call:
    inputs:
      tools_ref: {type: string, required: true}
      project_ref: {type: string, required: true}
      source_candidate_enabled: {type: boolean, default: false}
      source_candidate_runner: {type: string, default: '"ubuntu-24.04"'}
      source_candidate_control_runner: {type: string, default: '"ubuntu-24.04"'}
jobs:
  pick-runner:
    uses: ./.github/workflows/pick-runner.yml
  sandboxed-build:
    runs-on: ${{ fromJSON(inputs.source_candidate_enabled && inputs.source_candidate_runner || '"ubuntu-24.04"') }}
    steps: [{run: "true"}]
  publish-merge-group-cache:
    needs: sandboxed-build
    uses: ./.github/workflows/publish-lake-cache.yml
    with:
      revision: ${{ needs.sandboxed-build.outputs.status-sha }}
      source_candidate_enabled: ${{ github.event_name == 'repository_dispatch' && inputs.source_candidate_enabled }}
      source_candidate_control_runner: ${{ inputs.source_candidate_control_runner }}
  finalize-merge-group-build:
    runs-on: ${{ fromJSON(inputs.source_candidate_enabled && inputs.source_candidate_control_runner || '"ubuntu-24.04"') }}
    steps: [{run: "true"}]
""",
    ".github/workflows/pick-runner.yml": """on: workflow_call
jobs:
  select:
    runs-on: ubuntu-24.04
    steps: [{run: "true"}]
""",
    ".github/workflows/publish-lake-cache.yml": """on:
  workflow_call:
    inputs:
      revision: {type: string, required: true}
      source_candidate_enabled: {type: boolean, default: false}
      source_candidate_control_runner: {type: string, default: '"ubuntu-24.04"'}
jobs:
  publish:
    runs-on: ${{ fromJSON(inputs.source_candidate_enabled && inputs.source_candidate_control_runner || '"ubuntu-24.04"') }}
    steps: [{run: "true"}]
""",
}


def fetch(repository, path, revision):
    assert repository == TOOLS
    return HARNESS[path]


def entry(name, revision):
    return f"{TOOLS}/.github/workflows/{name}@{revision}"


STATIC3 = [f"{PROJECT}/.github/workflows/pr-build.yml@refs/heads/main"]
STATIC6 = [f"{PROJECT}/.github/workflows/publish-lake-cache.yml@refs/heads/main"]


class Literals(unittest.TestCase):
    def workflows(self, build=NEW, call=NEW):
        return {
            "pr-build.yml": project_build(build),
            "source-candidate.yml": caller(call),
            "other.yml": project_build(OLD),
        }

    def test_consistent_pins_pass_and_unbound_workflows_keep_their_own_pins(self):
        self.assertEqual(hp.check_literals(source(NEW), self.workflows(), ["pr-build.yml"]), [])

    def test_each_divergent_literal_is_reported(self):
        errors = hp.check_literals(source(NEW), self.workflows(build=OLD), ["pr-build.yml"])
        self.assertEqual(len(errors), 3)
        self.assertTrue(all("pr-build.yml" in e and OLD in e for e in errors))

    def test_harness_callers_are_bound_even_when_not_named(self):
        errors = hp.check_literals(source(NEW), self.workflows(call=OLD), [])
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("source-candidate.yml" in e for e in errors))

    def test_missing_or_empty_bound_workflow_is_an_error(self):
        self.assertIn("missing", hp.check_literals(source(NEW), self.workflows(), ["gone.yml"])[0])
        self.assertIn(
            "no TauCetiProject/TauCetiTools pin",
            hp.check_literals(source(NEW), {"x.yml": "on: push\njobs: {}\n"}, ["x.yml"])[0],
        )

    def test_source_must_name_a_full_revision(self):
        with self.assertRaises(hp.DerivationError):
            hp.harness(source("main"))
        with self.assertRaises(hp.DerivationError):
            hp.harness("{}")

    def test_set_derives_every_bound_literal_from_the_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".tauceti").mkdir()
            (root / ".github/workflows").mkdir(parents=True)
            (root / hp.SOURCE).write_text(source(OLD))
            for name, text in self.workflows(build=OLD, call=OLD).items():
                (root / hp.WORKFLOWS / name).write_text(text)
            changed = hp.set_revision(root, NEW, ["pr-build.yml"])
            self.assertEqual({p.name for p in changed}, {"ci-source.json", "pr-build.yml", "source-candidate.yml"})
            workflows = {p.name: p.read_text() for p in (root / hp.WORKFLOWS).glob("*.yml")}
            self.assertEqual(hp.check_literals((root / hp.SOURCE).read_text(), workflows, ["pr-build.yml"]), [])
            self.assertIn(OLD, workflows["other.yml"])
            self.assertEqual(json.loads((root / hp.SOURCE).read_text())["harness_revision"], NEW)


class Expressions(unittest.TestCase):
    def evaluate(self, expression, **inputs):
        context = {"inputs": {k: hp.values(v) for k, v in inputs.items()}}
        return list(hp.evaluate_expression(expression, context).values())

    def test_selector_pattern(self):
        expression = "fromJSON(inputs.on && inputs.runner || '\"ubuntu-24.04\"')"
        self.assertEqual(self.evaluate(expression, on=True, runner='["a"]'), [["a"]])
        self.assertEqual(self.evaluate(expression, on=False, runner='["a"]'), ["ubuntu-24.04"])

    def test_unknown_contexts_yield_every_branch(self):
        found = self.evaluate(
            "fromJSON(github.event_name == 'x' && inputs.runner || '\"ubuntu-24.04\"')", runner='["a"]'
        )
        self.assertCountEqual(found, [["a"], "ubuntu-24.04"])

    def test_unknown_value_cannot_become_a_target(self):
        found = hp.evaluate_value("${{ vars.RUNNER }}", {})
        with self.assertRaises(hp.DerivationError):
            for value in found.values():
                hp._target(value, "job")

    def test_literals_and_operators(self):
        self.assertEqual(self.evaluate("!('A' == 'a')"), [False])
        self.assertEqual(self.evaluate("fromJSON('{\"x\": [1]}').x[0]"), [1])
        self.assertEqual(self.evaluate("inputs.missing || 'd'"), ["d"])
        self.assertEqual(list(hp.evaluate_value("pre-${{ 'x' }}", {}).values()), ["pre-x"])


class Derivation(unittest.TestCase):
    def test_needs_follow_inputs_through_nested_calls(self):
        needs = hp.project_needs(
            {"source-candidate.yml": caller(NEW), "pr-build.yml": project_build(NEW)}, TOOLS, fetch
        )
        self.assertEqual(
            {(n.entry.rsplit("/", 1)[1], n.job, n.target[0]) for n in needs},
            {
                (f"pr-build.yml@{NEW}", "sandboxed-build", "labels"),
                (f"pr-build.yml@{NEW}", "finalize-merge-group-build", "group"),
                (f"publish-lake-cache.yml@{NEW}", "publish", "group"),
            },
        )

    def test_hosted_only_callers_need_nothing(self):
        text = caller(NEW).replace("source_candidate_enabled: true", "source_candidate_enabled: false")
        self.assertEqual(hp.project_needs({"c.yml": text}, TOOLS, fetch), set())

    def test_caller_must_pin_a_full_sha(self):
        with self.assertRaises(hp.DerivationError):
            hp.project_needs({"c.yml": caller("main")}, TOOLS, fetch)

    def test_groups_come_from_names_inventory_or_existing_admission(self):
        needs = hp.project_needs({"c.yml": caller(NEW)}, TOOLS, fetch)
        groups = [
            hp.Group(
                3,
                "TauCetiFleet",
                [entry("pr-build.yml", OLD)],
                [["self-hosted", "linux", "x64", "tauceti-fleet-staging", "fleet-m", "x"]],
            ),
            hp.Group(6, "TauCetiQueueControl", [], []),
            hp.Group(7, "TauCetiPRStatus", [], [["self-hosted", "pr-status"]]),
        ]
        expected = {
            3: {entry("pr-build.yml", NEW)},
            6: {entry("pr-build.yml", NEW), entry("publish-lake-cache.yml", NEW)},
        }
        self.assertEqual(hp.resolve(needs, groups, TOOLS), expected)
        groups[0].runner_labels = []  # staging host offline: keep the groups that already admit the workflow
        self.assertEqual(hp.resolve(needs, groups, TOOLS), expected)
        groups[0].workflows = []
        with self.assertRaises(hp.DerivationError):
            hp.resolve(needs, groups, TOOLS)


class FakeGitHub:
    """GitHub API double: project files by revision, harness files, runner groups and runs."""

    def __init__(self):
        self.project = {
            MAIN: {"pr-build.yml": project_build(NEW), "source-candidate.yml": caller(NEW)},
            PREV: {"pr-build.yml": project_build(OLD), "source-candidate.yml": caller(OLD)},
            PR_BASE: {"pr-build.yml": project_build(NEW), "source-candidate.yml": caller(NEW)},
            PR_HEAD: {"pr-build.yml": project_build(NEWER), "source-candidate.yml": caller(NEWER)},
            RUNNING: {"pr-build.yml": project_build(STRAY), "source-candidate.yml": caller(STRAY)},
        }
        self.sources = {
            MAIN: source(NEW),
            PREV: source(OLD),
            PR_BASE: source(NEW),
            PR_HEAD: source(NEWER),
            RUNNING: source(STRAY),
        }
        # Harness history on its default branch: OLD < NEW < NEWER; STRAY is off-branch unless set.
        self.history = [OLD, NEW, NEWER]
        self.groups = {
            3: {
                "id": 3,
                "name": "TauCetiFleet",
                "restricted_to_workflows": True,
                "visibility": "all",
                "allows_public_repositories": True,
                "selected_workflows": STATIC3 + [entry("pr-build.yml", OLD), entry("pr-build.yml", NEW)],
            },
            6: {
                "id": 6,
                "name": "TauCetiQueueControl",
                "restricted_to_workflows": True,
                "visibility": "selected",
                "allows_public_repositories": True,
                "selected_workflows": STATIC6
                + [entry(w, r) for w in ("pr-build.yml", "publish-lake-cache.yml") for r in (OLD, NEW)],
            },
            7: {
                "id": 7,
                "name": "TauCetiPRStatus",
                "restricted_to_workflows": True,
                "visibility": "selected",
                "allows_public_repositories": True,
                "selected_workflows": [f"{PROJECT}/.github/workflows/pr-status.yml@refs/heads/main"],
            },
            1: {
                "id": 1,
                "name": "Default",
                "restricted_to_workflows": False,
                "visibility": "all",
                "allows_public_repositories": True,
            },
        }
        self.runners = {
            3: [["self-hosted", "linux", "x64", "tauceti-fleet-staging", "fleet-m"]],
            6: [],
            7: [["self-hosted", "pr-status"]],
        }
        self.unfinished = []
        self.patches = []
        self.on_patch = None

    def blob(self, text):
        oid = format(abs(hash(text)), "x")
        self.blobs[oid] = text
        return oid

    def get(self, path):
        url = urllib.parse.urlsplit(path)
        query = dict(urllib.parse.parse_qsl(url.query))
        if int(query.get("page", 1)) > 1:
            return (
                []
                if "contents" in url.path
                else {"runner_groups": [], "repositories": [], "runners": [], "workflow_runs": []}
            )
        parts = url.path.split("/")
        route = url.path
        if route == f"repos/{PROJECT}":
            return {"id": 1, "private": False}
        if route == f"repos/{TOOLS}":
            return {"default_branch": "main"}
        if route == f"repos/{PROJECT}/commits/main":
            return {"sha": MAIN, "parents": [{"sha": PREV}]}
        if route == f"repos/{PROJECT}/pulls/9":
            return {"head": {"sha": PR_HEAD}, "base": {"sha": MAIN}}
        if route == f"repos/{PROJECT}/compare/{MAIN}...{PR_HEAD}":
            return {"merge_base_commit": {"sha": PR_BASE}}
        if route.startswith(f"repos/{TOOLS}/compare/"):
            base, head = parts[-1].split("...")
            head = self.history[-1] if head == "main" else head
            if base not in self.history or head not in self.history:
                return {"status": "diverged"}
            a, b = self.history.index(base), self.history.index(head)
            return {"status": "identical" if a == b else "ahead" if a < b else "behind"}
        if route.startswith("repos/") and "/git/blobs/" in route:
            text = self.blobs[parts[-1]]
            return {"content": base64.b64encode(text.encode()).decode()}
        if route == f"repos/{PROJECT}/contents/.github/workflows":
            files = self.project[query["ref"]]
            return [{"type": "file", "name": n, "sha": self.blob(t)} for n, t in files.items()]
        if route == f"repos/{PROJECT}/contents/{urllib.parse.quote(hp.SOURCE)}":
            return {"type": "file", "sha": self.blob(self.sources[query["ref"]])}
        if route.startswith(f"repos/{TOOLS}/contents/"):
            path = urllib.parse.unquote(route.split("/contents/", 1)[1])
            return {"type": "file", "sha": self.blob(HARNESS[path])}
        if route == "orgs/TauCetiProject/actions/runner-groups":
            return {"runner_groups": [copy.deepcopy(g) for g in self.groups.values()]}
        if route.startswith("orgs/TauCetiProject/actions/runner-groups/"):
            gid = int(parts[4])
            if route.endswith("/repositories"):
                return {"repositories": [{"id": 1}]}
            if route.endswith("/runners"):
                return {"runners": [{"labels": [{"name": n} for n in r]} for r in self.runners.get(gid, [])]}
            return copy.deepcopy(self.groups[gid])
        if route.startswith(f"repos/{PROJECT}/actions/workflows/source-candidate.yml/runs"):
            return {"workflow_runs": [{"head_sha": s} for s in self.unfinished] if query["status"] == "queued" else []}
        raise AssertionError(f"unexpected GET {path}")

    def patch(self, path, data):
        gid = int(path.rsplit("/", 1)[1])
        self.patches.append((gid, data))
        if self.on_patch:
            self.on_patch()
        self.groups[gid]["selected_workflows"] = list(data["selected_workflows"])
        assert data["restricted_to_workflows"] is True and data["name"] == self.groups[gid]["name"]


class Reconcile(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.api.blobs = {}

    def run_reconcile(self, **options):
        return hp.reconcile(self.api, PROJECT, bound=["pr-build.yml"], **options)

    def workflows(self, gid):
        return self.api.groups[gid]["selected_workflows"]

    def test_pull_request_pins_are_admitted_before_merge(self):
        report = self.run_reconcile(pr=9)
        self.assertEqual(hp.pr_state(report), ("success", "Runner groups admit this PR's harness pins"))
        self.assertIn(entry("pr-build.yml", NEWER), self.workflows(3))
        self.assertNotIn(entry("publish-lake-cache.yml", NEWER), self.workflows(3))
        self.assertIn(entry("publish-lake-cache.yml", NEWER), self.workflows(6))
        self.assertEqual(self.workflows(7), [f"{PROJECT}/.github/workflows/pr-status.yml@refs/heads/main"])
        # Static entries are never touched.
        self.assertEqual(self.workflows(3)[0], STATIC3[0])

    def test_unchanged_pins_need_no_write(self):
        self.api.project[PR_HEAD] = self.api.project[PR_BASE]
        self.api.sources[PR_HEAD] = self.api.sources[PR_BASE]
        report = self.run_reconcile(pr=9)
        self.assertEqual(hp.pr_state(report), ("success", "Harness pins unchanged"))
        self.assertEqual(self.api.patches, [])

    def test_pull_request_cannot_admit_an_unreviewed_harness_revision(self):
        self.api.project[PR_HEAD] = {"pr-build.yml": project_build(STRAY), "source-candidate.yml": caller(STRAY)}
        self.api.sources[PR_HEAD] = source(STRAY)
        report = self.run_reconcile(pr=9)
        self.assertEqual(hp.pr_state(report)[0], "failure")
        self.assertIn("not on the TauCetiProject/TauCetiTools default branch", report["pr"]["errors"][0])
        self.assertEqual(self.api.patches, [])

    def test_pull_request_cannot_extend_admission_to_an_unmanaged_group(self):
        control = CONTROL.replace("TauCetiQueueControl", "TauCetiPRStatus")
        self.api.project[PR_HEAD]["source-candidate.yml"] = caller(NEWER, control=control)
        report = self.run_reconcile(pr=9)
        self.assertEqual(hp.pr_state(report)[0], "failure")
        self.assertIn("TauCetiPRStatus has no", " ".join(report["pr"]["errors"]))
        self.assertEqual(self.workflows(7), [f"{PROJECT}/.github/workflows/pr-status.yml@refs/heads/main"])

    def test_pull_request_literals_must_follow_the_source(self):
        self.api.project[PR_HEAD]["pr-build.yml"] = project_build(NEW)
        report = self.run_reconcile(pr=9)
        self.assertEqual(hp.pr_state(report)[0], "failure")
        self.assertIn("harness_revision is " + NEWER, report["pr"]["errors"][0])

    def test_pull_request_cannot_change_the_harness_repository(self):
        self.api.sources[PR_HEAD] = source(NEWER, "Someone/Tools")
        self.assertIn("harness repository", self.run_reconcile(pr=9)["pr"]["errors"][0])

    def test_main_is_admitted_after_a_direct_push(self):
        self.api.groups[3]["selected_workflows"] = STATIC3[:]
        self.api.groups[3]["selected_workflows"].append(entry("pr-build.yml", OLD))
        report = self.run_reconcile()
        self.assertEqual(report["unadmitted_main"], {})
        self.assertIn(entry("pr-build.yml", NEW), self.workflows(3))

    def test_prune_keeps_main_previous_main_unfinished_runs_and_newer_pins(self):
        self.api.history = [STRAY, OLD, NEW, NEWER]
        older = "e" * 40
        self.api.history.insert(0, older)
        self.api.groups[3]["selected_workflows"] += [
            entry("pr-build.yml", older),
            entry("pr-build.yml", NEWER),
            entry("pr-build.yml", STRAY),
        ]
        self.api.unfinished = [RUNNING]
        self.run_reconcile(prune=True)
        kept = set(self.workflows(3))
        self.assertEqual(kept, set(STATIC3) | {entry("pr-build.yml", r) for r in (OLD, NEW, NEWER, STRAY)})
        self.api.unfinished = []
        self.api.project[PREV] = self.api.project[MAIN]
        self.api.sources[PREV] = self.api.sources[MAIN]
        self.run_reconcile(prune=True)
        self.assertEqual(set(self.workflows(3)), set(STATIC3) | {entry("pr-build.yml", r) for r in (NEW, NEWER)})
        self.assertEqual(
            set(self.workflows(6)), set(STATIC6) | {entry(w, NEW) for w in ("pr-build.yml", "publish-lake-cache.yml")}
        )

    def test_unknown_retention_never_prunes(self):
        self.api.unfinished = [RUNNING]
        self.api.project[RUNNING]["source-candidate.yml"] = caller(STRAY, control="${{ vars.X }}").replace(
            "source_candidate_control_runner: '${{ vars.X }}'", "source_candidate_control_runner: ${{ vars.X }}"
        )
        before = copy.deepcopy(self.api.groups)
        report = self.run_reconcile(prune=True)
        self.assertTrue(any("not pruning" in e for e in report["errors"]))
        self.assertEqual(self.api.groups, before)

    def test_writes_preserve_entries_added_concurrently(self):
        concurrent = f"{PROJECT}/.github/workflows/manual.yml@refs/heads/main"

        def edit():
            if concurrent not in self.api.groups[3]["selected_workflows"]:
                self.api.groups[3]["selected_workflows"].append(concurrent)

        # The edit lands after the plan was computed but before the group is re-read.
        original = self.api.get

        def get(path):
            if path == "orgs/TauCetiProject/actions/runner-groups/3":
                edit()
            return original(path)

        self.api.get = get
        self.run_reconcile(pr=9)
        self.assertIn(concurrent, self.workflows(3))
        self.assertIn(entry("pr-build.yml", NEWER), self.workflows(3))

    def test_dry_run_writes_nothing(self):
        report = self.run_reconcile(pr=9, dry_run=True)
        self.assertEqual(self.api.patches, [])
        self.assertEqual(hp.pr_state(report)[0], "success")
        self.assertIn("3", report["written"])


if __name__ == "__main__":
    unittest.main()


class PullRequestCheck(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.api.blobs = {}
        original = self.api.get

        def get(path):
            self.assertFalse(path.startswith("orgs/"), "classification must not need organization access")
            return original(path)

        self.api.get = get

    def test_classification_needs_only_repository_reads(self):
        result = hp.pull_request_check(self.api, PROJECT, 9, bound=["pr-build.yml"])
        self.assertEqual((result["head"], result["changes_pins"], result["errors"]), (PR_HEAD, True, []))
        self.api.project[PR_HEAD] = self.api.project[PR_BASE]
        self.api.sources[PR_HEAD] = self.api.sources[PR_BASE]
        self.assertFalse(hp.pull_request_check(self.api, PROJECT, 9, bound=["pr-build.yml"])["changes_pins"])

    def test_command_line_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            with (
                unittest.mock.patch.object(hp, "GitHub", lambda token: self.api),
                unittest.mock.patch("builtins.print"),
            ):
                code = hp.main(
                    [
                        "pr-check",
                        "--project",
                        PROJECT,
                        "--pr",
                        "9",
                        "--bound-workflow",
                        "pr-build.yml",
                        "--github-output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 0)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(values["changed"], "true")
            self.assertEqual(values["state"], "pending")
            self.assertEqual(values["head"], PR_HEAD)


class RoutedCredentials(unittest.TestCase):
    def test_organization_paths_use_the_runner_group_credential(self):
        calls = []

        class Recorder:
            def __init__(self, name):
                self.name = name

            def get(self, path):
                calls.append((self.name, path))

            def patch(self, path, data):
                calls.append((self.name, "PATCH " + path))

        api = hp.Routed(Recorder("repo"), Recorder("org"))
        api.get("repos/x/y")
        api.get("orgs/x/actions/runner-groups/3")
        api.patch("orgs/x/actions/runner-groups/3", {})
        with self.assertRaises(hp.ApiError):
            api.patch("repos/x/y", {})
        self.assertEqual([c[0] for c in calls], ["repo", "org", "org"])
