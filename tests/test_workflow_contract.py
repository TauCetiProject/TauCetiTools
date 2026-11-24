"""Tests for tests/workflow_contract.py, and the live contract when its checkouts are given.

The live check runs in workflow-contract.yml with TAUCETI_CONTRACT_PROJECT (a TauCeti checkout)
and TAUCETI_CONTRACT_FLEET (a TauCetiFleet checkout) set; elsewhere it is skipped.
"""

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workflow_contract  # noqa: E402

FLEET = """\
const TAUCETI='TauCetiProject/TauCeti/.github/workflows/';
const HARNESS='TauCetiProject/TauCetiTools/.github/workflows/';
export const CHECKS={
  3:expect('TauCetiFleet',main(TAUCETI,['pr-build.yml']),
    [...main(TAUCETI,['api-docs.yml']),...main(HARNESS,['build.yml'])]),
  6:expect('TauCetiQueueControl',main(TAUCETI,['pr-build.yml']),
    main(HARNESS,['build.yml']),'TauCetiProject/TauCeti'),
  [INFRA_GROUP]:{name:'TauCetiInfra',workflows:false,repositories:INFRA_REPOSITORIES},
};
"""

CONTRACT = """\
workflows:
  build.yml:
    callers:
      TauCetiProject/TauCeti:
        - {file: stub.yml, job: build, name: build, triggers: [pull_request_target]}
    jobs: [compile]
    runner_groups: [TauCetiFleet]
  inner.yml:
    internal: true
runner_labels:
  tauceti-fleet-pr: [TauCetiFleet]
  tauceti-fleet-queue-control: [TauCetiQueueControl]
"""

CALLEE = """\
name: build
on:
  workflow_call:
    inputs:
      runner: {type: string, required: true}
      fast: {type: boolean, default: false}
    secrets:
      TOKEN: {required: true}
      OPTIONAL: {required: false}
    outputs:
      verdict: {value: ok}
permissions:
  contents: read
concurrency:
  group: build-${{ github.ref }}
jobs:
  compile:
    runs-on: ${{ fromJSON(inputs.runner) }}
    permissions:
      contents: read
      statuses: write
    steps: [{run: 'true'}]
  nested:
    permissions:
      contents: read
    uses: ./.github/workflows/inner.yml
"""

INNER = """\
on:
  workflow_call: {}
jobs:
  pick:
    runs-on: ubuntu-24.04
    steps: [{run: 'true'}]
"""

STUB = """\
name: build
on:
  pull_request_target:
permissions:
  contents: read
  statuses: write
jobs:
  build:
    uses: TauCetiProject/TauCetiTools/.github/workflows/build.yml@main
    with:
      runner: '{"group":"TauCetiFleet","labels":["self-hosted","tauceti-fleet-pr"]}'
      fast: true
    secrets:
      TOKEN: ${{ secrets.TOKEN }}
  report:
    needs: build
    runs-on: ubuntu-24.04
    steps:
      - run: echo ${{ needs.build.outputs.verdict }}
"""


class Fixture:
    def __init__(self, base):
        self.base = Path(base)
        self.write("tools/.github/workflow-contract.yml", CONTRACT)
        self.write("tools/.github/workflows/build.yml", CALLEE)
        self.write("tools/.github/workflows/inner.yml", INNER)
        self.write("project/.github/workflows/stub.yml", STUB)
        self.write("fleet/services/ci-broker/src/github.mjs", FLEET)

    def write(self, name, text):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))

    def edit(self, name, old, new):
        path = self.base / name
        text = path.read_text()
        assert old in text, (name, old)
        path.write_text(text.replace(old, new))

    def errors(self):
        checker = workflow_contract.Checker(self.base / "tools", self.base / "project", self.base / "fleet")
        return checker.run()


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.fixture = Fixture(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def assertViolation(self, fragment):
        errors = self.fixture.errors()
        self.assertTrue(any(fragment in error for error in errors), errors)

    def test_matching_caller_and_callee_pass(self):
        self.assertEqual(self.fixture.errors(), [])

    def test_fleet_policy_is_read_from_the_broker_table(self):
        policy = workflow_contract.fleet_policy(self.fixture.base / "fleet")
        self.assertIsNone(policy["TauCetiInfra"])
        self.assertIn("TauCetiProject/TauCetiTools/.github/workflows/build.yml@refs/heads/main",
                      policy["TauCetiQueueControl"])
        self.assertIn("TauCetiProject/TauCeti/.github/workflows/api-docs.yml@refs/heads/main",
                      policy["TauCetiFleet"])

    def test_nested_job_asking_beyond_the_ceiling_fails(self):
        # Tools #288: a callee job asked for issues: write, which the caller did not grant.
        self.fixture.edit("tools/.github/workflows/build.yml", "      statuses: write\n",
                          "      statuses: write\n      issues: write\n")
        self.assertViolation("issues: write beyond the caller's ceiling")

    def test_job_inside_a_nested_call_is_held_to_the_calling_job(self):
        self.fixture.write("tools/.github/workflows/inner.yml", INNER.replace(
            "    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-24.04\n    permissions:\n      statuses: write\n"))
        self.assertViolation("inner.yml job pick: asks for statuses: write")

    def test_callee_workflow_permissions_beyond_the_ceiling_fail(self):
        self.fixture.edit("tools/.github/workflows/build.yml", "permissions:\n  contents: read\n",
                          "permissions:\n  contents: write\n")
        self.assertViolation("contents: write beyond")

    def test_write_all_is_bounded_by_the_ceiling(self):
        self.fixture.edit("tools/.github/workflows/build.yml", "permissions:\n  contents: read\nconcurrency",
                          "permissions: write-all\nconcurrency")
        self.assertViolation("beyond the caller's ceiling")

    def test_caller_without_permissions_fails(self):
        self.fixture.edit("project/.github/workflows/stub.yml", "permissions:\n  contents: read\n  statuses: write\n", "")
        self.assertViolation("sets no permissions")

    def test_missing_required_input_fails(self):
        self.fixture.edit("project/.github/workflows/stub.yml", "      runner: '", "      other: '")
        errors = self.fixture.errors()
        self.assertTrue(any("does not pass required input 'runner'" in e for e in errors), errors)
        self.assertTrue(any("passes input 'other'" in e for e in errors), errors)

    def test_literal_of_the_wrong_type_fails(self):
        self.fixture.edit("project/.github/workflows/stub.yml", "fast: true", "fast: 'yes'")
        self.assertViolation("to boolean input 'fast'")

    def test_secrets_are_checked_both_ways(self):
        self.fixture.edit("project/.github/workflows/stub.yml", "      TOKEN: ${{ secrets.TOKEN }}\n",
                          "      EXTRA: ${{ secrets.EXTRA }}\n")
        errors = self.fixture.errors()
        self.assertTrue(any("passes secret 'EXTRA'" in e for e in errors), errors)
        self.assertTrue(any("required secret 'TOKEN'" in e for e in errors), errors)

    def test_inherited_secrets_fail(self):
        self.fixture.edit("project/.github/workflows/stub.yml", "    secrets:\n      TOKEN: ${{ secrets.TOKEN }}\n",
                          "    secrets: inherit\n")
        self.assertViolation("secrets: inherit")

    def test_undeclared_output_fails(self):
        self.fixture.edit("project/.github/workflows/stub.yml", "outputs.verdict", "outputs.result")
        self.assertViolation("reads output 'result'")

    def test_shared_concurrency_group_fails(self):
        self.fixture.edit("project/.github/workflows/stub.yml", "jobs:\n  build:\n",
                          "concurrency:\n  group: build-${{ github.ref }}\njobs:\n  build:\n")
        self.assertViolation("wait on itself")

    def test_restricted_group_must_admit_the_defining_file(self):
        self.fixture.edit("fleet/services/ci-broker/src/github.mjs", "...main(HARNESS,['build.yml'])", "")
        self.assertViolation("runner group TauCetiFleet, whose Fleet policy does not admit "
                             "TauCetiProject/TauCetiTools/.github/workflows/build.yml")

    def test_restricted_label_through_a_nested_input_is_traced(self):
        self.fixture.write("tools/.github/workflows/inner.yml", INNER.replace(
            "on:\n  workflow_call: {}\n", "on:\n  workflow_call:\n    inputs:\n      where: {type: string}\n").replace(
            "runs-on: ubuntu-24.04", "runs-on: ${{ fromJSON(inputs.where) }}"))
        self.fixture.edit("tools/.github/workflows/build.yml", "    uses: ./.github/workflows/inner.yml\n",
                          "    uses: ./.github/workflows/inner.yml\n    with:\n      where: ${{ inputs.runner }}\n")
        self.assertViolation("inner.yml job pick: can run in runner group TauCetiFleet")

    def test_runtime_runner_needs_declared_groups(self):
        self.fixture.edit("tools/.github/workflows/build.yml", "runs-on: ${{ fromJSON(inputs.runner) }}",
                          "runs-on: ${{ fromJSON(needs.nested.outputs.runner) }}")
        self.fixture.edit("tools/.github/workflow-contract.yml", "    runner_groups: [TauCetiFleet]\n", "")
        self.assertViolation("picks its runner at run time")

    def test_unknown_group_fails(self):
        self.fixture.edit("project/.github/workflows/stub.yml", '"group":"TauCetiFleet"', '"group":"Elsewhere"')
        self.assertViolation("runner group Elsewhere, which Fleet does not know")

    def test_caller_must_keep_its_triggers_and_name(self):
        self.fixture.edit("project/.github/workflows/stub.yml", "on:\n  pull_request_target:\n",
                          "on:\n  workflow_dispatch:\n")
        self.fixture.edit("project/.github/workflows/stub.yml", "name: build\n", "name: renamed\n")
        errors = self.fixture.errors()
        self.assertTrue(any("lacks the pull_request_target trigger" in e for e in errors), errors)
        self.assertTrue(any("workflow name 'renamed'" in e for e in errors), errors)

    def test_contracted_job_must_exist(self):
        self.fixture.edit("tools/.github/workflows/build.yml", "  compile:\n", "  compiled:\n")
        self.assertViolation("job 'compile', which other repositories depend on, is gone")

    def test_every_reusable_workflow_needs_an_entry(self):
        self.fixture.write("tools/.github/workflows/new.yml", INNER)
        self.assertViolation("reusable workflow new.yml has no entry")

    def test_unlisted_caller_fails(self):
        self.fixture.write("project/.github/workflows/other.yml", STUB)
        self.assertViolation("other.yml job build: is not listed as a caller")

    def test_pending_caller_is_checked_once_it_calls(self):
        self.fixture.edit("tools/.github/workflow-contract.yml", "triggers: [pull_request_target]}",
                          "triggers: [pull_request_target], pending: true}")
        self.assertEqual(self.fixture.errors(), [])
        (self.fixture.base / "project/.github/workflows/stub.yml").unlink()
        self.assertEqual(self.fixture.errors(), [])

    def test_dropped_caller_must_be_marked_pending(self):
        (self.fixture.base / "project/.github/workflows/stub.yml").unlink()
        self.assertViolation("no longer calls build.yml")

    def test_caller_must_use_main(self):
        self.fixture.edit("project/.github/workflows/stub.yml", "build.yml@main", "build.yml@v1")
        self.assertViolation("projects call Tools workflows at @main")


@unittest.skipUnless(os.environ.get("TAUCETI_CONTRACT_PROJECT"), "needs TauCeti and TauCetiFleet checkouts")
class LiveContract(unittest.TestCase):
    def test_tauceti_main_and_fleet_main_meet_the_contract(self):
        checker = workflow_contract.Checker(
            workflow_contract.TOOLS_ROOT, os.environ["TAUCETI_CONTRACT_PROJECT"], os.environ["TAUCETI_CONTRACT_FLEET"])
        errors = checker.run()
        self.assertEqual(errors, [], "\n" + "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
