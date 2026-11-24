"""Run this revision's script unit tests inside a TauCeti checkout.

Usage: python3 tests/project/run_unit_tests.py PROJECT

PROJECT is a TauCeti checkout whose `scripts/` already holds this revision's materialized scripts
(the `setup` action does that). The tests in `tauceti/` check workflows that stay in TauCeti; they
are copied into `PROJECT/scripts/` and run against PROJECT's own `.github/`.

Tests that need Lean, Lake or bwrap (`test_check_imports.py`, `test_ci_checkout.py`,
`test_lake_cache_publish.py`) run in later steps of the `project-fixtures` job, and
`test_lint_dot_notation.py` and `test_claims_access.py` run in the `package` job. When one of
PROJECT's workflows is a stub calling this repository's workflow of the same name (pr-build.yml,
for one), this script first puts this revision's copy in its place, so those later steps check it
as well.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

TOOLS_TESTS = """
test_attest_pr_config.py test_bors_scope_approval.py test_bors_staging_identity.py
test_bors_train_identity.py test_build_lint_driver.py test_bump_latest.py test_bump_manifest.py
test_bwrap_pin.py test_cancel_bors_candidate.py test_check_expired_mathlib_shims.py
test_check_source_options.py test_ci_graphs.py test_ci_host_resources.py test_ci_live_progress.py
test_ci_live_summary.py test_dependency_warm.py test_elan_pin.py test_finalize_batch.py
test_fleet_runner.py test_github_api_report.py test_housekeeping.py test_lake_cache_get.py
test_lake_cache_probe.py test_lint_env_locations.py test_lint_scope.py test_loc_graph.py
test_loc_roadmap_graph.py test_participant_graph.py test_pick_runner.py test_pipeline_health.py
test_prepare_pr_integration.py test_pr_lifecycle.py test_profile_workflow.py
test_pr_stats_graphs.py test_queue_release.py test_roadmap_api.py test_roadmap_completion.py
test_roadmap_label.py test_roadmap_progress.py test_stack_policy.py test_structure_nudge.py
test_toolchain_tags.py test_web_pins.py
perf/test_perf.py pr_status/test_batch.py pr_status/test_conflicts.py
pr_status/test_pr_labels.py pr_status/test_readiness.py
pr_status/test_zulip.py
""".split()

TAUCETI_TESTS = sorted(path.name for path in (HERE / "tauceti").glob("test_*.py"))

# These cases assert TauCeti workflow YAML or TauCeti source that no longer matches this
# revision's copy of the test. TauCeti's own diverged copies of these files are being deleted.
DESELECTED = {
    "test_bors_scope_approval.py": ["WorkflowTests.test_human_approval_only_waives_paths"],
    "test_check_expired_mathlib_shims.py": [
        "ExpiredMathlibShimTests.test_double_coset_exact_entry_derives_the_whole_vendored_surface"],
    "test_dependency_warm.py": [
        "Boundary.test_main_saves_mathlib_before_root_build",
        "Boundary.test_mathlib_cold_selection_preserves_publisher_key_and_directory",
        "Boundary.test_only_isolated_main_warmer_saves_toolchain"],
    "test_finalize_batch.py": [
        "WorkflowBoundaryTests.test_isolation_matcher_accepts_real_namespace_canary_label_encoding",
        "WorkflowBoundaryTests.test_separate_unprivileged_priority_runner_and_scoped_tokens"],
    "test_lint_scope.py": ["PrBuildWiringTest.test_only_the_full_lint_label_rebuilds"],
    "test_pick_runner.py": [
        "PickerWorkflow.test_exact_target_is_used_for_this_attempt_only",
        "PickerWorkflow.test_namespace_target_and_fallback_obey_the_same_attempt_fence",
        "PickerWorkflow.test_picker_is_small_control_job_and_calls_the_existing_selector"],
    "test_roadmap_api.py": [
        "Recovery.test_restore_only_selects_trusted_main_schedule_and_dispatch_runs",
        "Recovery.test_workflow_preserves_app_writes_and_filters_title_edits"],
}

# These cases assert the literal text of TauCeti's own pr-build.yml (its `TauCeti` paths, triggers,
# title and local action paths). Once that file is a stub, they would read this revision's
# parameterised copy instead, which tests/test_*.py check directly.
STUB_DESELECTED = {
    "test_bors_staging_identity.py": ["ConcurrencyTests.test_same_candidate_supersedes_across_runs"],
    "test_bors_train_identity.py": [
        "TrainIdentityTests.test_trusted_checkout_precedes_resolution_and_shared_validation_is_reused"],
    "test_check_expired_mathlib_shims.py": [
        "ExpiredMathlibShimTests.test_workflow_uses_integration_base_and_scopes_feature_probes"],
    "test_pick_runner.py": ["PickerWorkflow.test_pr_title_contains_immutable_head_and_bors_title_is_unchanged"],
    "test_prepare_pr_integration.py": [
        "WorkflowContract.test_no_new_trigger_or_pr_cache_producer_and_pinned_sandbox_stays"],
    "test_workflow_pins.py": ["ContentWorkflowCompleteness.test_long_build_refreshes_credentials_before_terminal_reporting"],
}

# Runs one test file without the deselected cases, failing if a deselected case no longer exists.
FILTERED = """
import importlib.util, pathlib, sys, unittest
path, skip = pathlib.Path(sys.argv[1]), set(sys.argv[2:])
sys.argv = [str(path)]
sys.path.insert(0, str(path.parent.resolve()))
spec = importlib.util.spec_from_file_location(path.stem, path)
module = importlib.util.module_from_spec(spec)
sys.modules[path.stem] = module
spec.loader.exec_module(module)
def flatten(suite):
    for item in suite:
        yield from flatten(item) if isinstance(item, unittest.TestSuite) else [item]
tests = list(flatten(unittest.defaultTestLoader.loadTestsFromModule(module)))
names = {test.id().split(".", 1)[1] for test in tests}
if skip - names:
    sys.exit(f"deselected tests not found: {sorted(skip - names)}")
print(f"deselected: {sorted(skip)}", flush=True)
suite = unittest.TestSuite(test for test in tests if test.id().split(".", 1)[1] not in skip)
sys.exit(not unittest.TextTestRunner().run(suite).wasSuccessful())
"""


def stubs(project):
    """Project workflows that only call this repository's workflow of the same name."""
    workflows = project / ".github/workflows"
    return sorted(path.name for path in workflows.glob("*.yml")
                  if f"TauCetiTools/.github/workflows/{path.name}@" in path.read_text())


def use_this_revisions_workflows(project):
    """Test this revision's workflows in place of the project's stubs that call them.

    The workflow tests read `.github/workflows/<name>.yml` from the project. When that file is a
    stub calling TauCetiTools' reusable workflow of the same name, put this revision's copy (and,
    for pr-build, the actions it uses) in its place, so the tests check what the project runs.
    """
    workflows = project / ".github/workflows"
    tools = HERE.parents[1]
    replaced = stubs(project)
    for name in replaced:
        # The project's own publish-lake-cache.yml stays while its post-merge ci.yml calls it.
        shutil.copyfile(tools / ".github/workflows" / name, workflows / name)
        print(f"testing this revision's {name} in place of the project's stub", flush=True)
    if "pr-build.yml" in replaced:
        for name in ("restore-lean-toolchain", "restore-mathlib-ltars"):
            shutil.copytree(tools / ".github/actions" / name, project / ".github/actions" / name, dirs_exist_ok=True)
    return replaced


def main(project):
    deselected = {name: list(cases) for name, cases in DESELECTED.items()}
    if "pr-build.yml" in use_this_revisions_workflows(project):
        for name, cases in STUB_DESELECTED.items():
            deselected[name] = deselected.get(name, []) + cases
    scripts = project / "scripts"
    for path in sorted((HERE / "tauceti").iterdir()):
        shutil.copyfile(path, scripts / path.name)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(["scripts", ".", os.environ.get("PYTHONPATH", "")]))
    commands = []
    for name in TOOLS_TESTS + TAUCETI_TESTS:
        path = f"scripts/{name}"
        skip = deselected.get(name)
        commands.append([sys.executable, "-c", FILTERED, path, *skip] if skip else [sys.executable, path])
    commands.append(["node", "--test", "scripts/test_labels_from_comments.cjs"])
    failed = []
    for command in commands:
        label = command[3] if command[1] == "-c" else " ".join(command[1:])
        print(f"::group::{label}", flush=True)
        code = subprocess.run(command, cwd=project, env=env).returncode
        print("::endgroup::", flush=True)
        if code:
            print(f"::error::{label} failed with exit code {code}", flush=True)
            failed.append(label)
    print(f"{len(commands) - len(failed)} of {len(commands)} test files passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]).resolve()))
