import base64
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "tauceti_tools/assets/scripts"
sys.path.insert(0, str(SCRIPTS))
import deprecated_prs as sweep
from tauceti_tools.survey_facts import rebase_heads

HEAD, MAIN, OLD, NEW = (c * 40 for c in "abcd")
REPO = "TauCetiProject/TauCeti"
DISTINCTIVE = "AnalyticAt.exists_eventuallyEq_pow_smul_nonzero_iff"
NAMES = {DISTINCTIVE, "BoundedContinuousFunction.add_apply"}


def encoded(text):
    return dict(encoding="base64", content=base64.b64encode(text.encode()).decode())


def manifest(pin):
    return json.dumps(dict(packages=[dict(name="mathlib", rev=pin,
        url="https://github.com/leanprover-community/mathlib4")]))


def pr():
    return dict(number=17, headRefOid=HEAD, baseRefName="main", isDraft=False,
                labels=dict(nodes=[], pageInfo=dict(hasNextPage=False)))


def live():
    return dict(state="open", draft=False, head=dict(sha=HEAD),
                base=dict(ref="main", sha=MAIN), labels=[])


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.live = live()
        self.comments = []
        self.files = [dict(filename="TauCeti/A.lean", status="added",
                           patch="+hA.exists_eventuallyEq_pow_smul_nonzero_iff.mpr hgn")]
        self.source = "hA.exists_eventuallyEq_pow_smul_nonzero_iff.mpr hgn\n"
        self.pin = NEW

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if method == "GET":
            if path.endswith("/pulls/17"):
                return copy.deepcopy(self.live)
            if "/contents/lake-manifest.json?ref=" in path:
                return encoded(manifest(self.pin))
            if "/contents/TauCeti/A.lean?ref=" in path:
                return encoded(self.source)
            raise AssertionError(path)
        return {}

    def items(self, path):
        self.calls.append(("GET", path, None))
        return self.files if path.endswith("/files") else self.comments


class DeprecatedPRTests(unittest.TestCase):
    def test_inventory_handles_namespaces_aliases_attributes_and_existing_deprecations(self):
        source = '''namespace AnalyticAt
@[deprecated (since := "2026-10-13")]
alias exists_eventuallyEq_pow_smul_nonzero_iff := exists_eventuallyEq_pow_smul_ne_zero_iff
section Inner
@[simp, deprecated new_name (since := "2026-10-13")]
theorem old_name : True := by trivial
attribute [deprecated new_name] older_name
end Inner
@[deprecated] @[simp] protected theorem other_name : True := by trivial
@[deprecated] alias _root_.global_old := new_name
end AnalyticAt
-- @[deprecated] alias nope := yep
/- nested /- @[deprecated] -/ alias nope := yep -/
def text := "@[deprecated] alias nope := yep"
@[to_additive (attr := deprecated foo)] theorem current : True := by trivial
'''
        self.assertEqual(sweep.deprecated_names(source), {
            DISTINCTIVE, "AnalyticAt.old_name", "AnalyticAt.older_name", "AnalyticAt.other_name", "global_old"})

    def test_matches_real_dot_notation_and_qualified_uses_without_generic_leaf_false_positives(self):
        source = '''hA.exists_eventuallyEq_pow_smul_nonzero_iff.mpr hgn
BoundedContinuousFunction.add_apply f g x
_root_.BoundedContinuousFunction.add_apply f g x
add_apply f g x
f.add_apply
-- BoundedContinuousFunction.add_apply
/- AnalyticAt.exists_eventuallyEq_pow_smul_nonzero_iff -/
"BoundedContinuousFunction.add_apply"
'''
        self.assertEqual(sweep.matches(source, NAMES), sorted(NAMES))
        self.assertEqual(sweep.matches("add_apply f g x\nf.add_apply", NAMES), [])
        self.assertEqual(sweep.matches("algebraMap_apply x", {"AbsoluteValue.Completion.algebraMap_apply"}), [])
        self.assertEqual(sweep.matches('r#"BoundedContinuousFunction.add_apply"#', NAMES), [])
        ambiguous = NAMES | {"Other.exists_eventuallyEq_pow_smul_nonzero_iff"}
        self.assertEqual(sweep.matches("hA.exists_eventuallyEq_pow_smul_nonzero_iff", ambiguous), [])

    def test_git_inventory_compares_complete_immutable_sources_and_ignores_old_deprecations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            def commit(text):
                (root / "Mathlib").mkdir(exist_ok=True)
                (root / "Mathlib/A.lean").write_text(text)
                subprocess.run(["git", "-C", str(root), "add", "."], check=True)
                subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.org",
                                "commit", "-qm", "fixture"], check=True)
                return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
            old = commit("namespace X\n@[deprecated] alias old := replacement\nend X\n")
            new = commit("namespace X\n@[deprecated] alias old := replacement\n@[deprecated] alias fresh := replacement\nend X\n")
            self.assertEqual(sweep.new_deprecations(root, old, new), {"X.fresh"})

    def test_scan_reads_exact_head_and_masks_full_file_comments_not_just_patch_context(self):
        api = FakeAPI()
        found = sweep.scan_pr(api, REPO, pr(), NAMES)
        self.assertEqual(found[0]["declarations"], [DISTINCTIVE])
        self.assertTrue(any(f"?ref={HEAD}" in c[1] for c in api.calls))
        api.source = "/-\nhA.exists_eventuallyEq_pow_smul_nonzero_iff.mpr hgn\n-/"
        self.assertEqual(sweep.scan_pr(api, REPO, pr(), NAMES), [])
        api.files[0].pop("patch")
        api.source = "BoundedContinuousFunction.add_apply f g x"
        self.assertEqual(sweep.scan_pr(api, REPO, pr(), NAMES)[0]["declarations"], ["BoundedContinuousFunction.add_apply"])
        api.files.append(dict(filename="scripts/a.py", status="added"))
        self.assertEqual(sweep.scan_pr(api, REPO, pr(), NAMES), [])

    def test_handoff_is_the_existing_marker_then_label_without_bors_or_build_calls(self):
        api = FakeAPI()
        self.assertEqual(sweep.handoff(api, REPO, pr(), "main", [], dry_run=False), "labelled")
        writes = [c for c in api.calls if c[0] != "GET"]
        self.assertEqual([c[1] for c in writes], [f"repos/{REPO}/issues/17/comments", f"repos/{REPO}/issues/17/labels"])
        self.assertEqual(writes[1][2], {"labels": ["needs-rebase"]})
        comment = dict(body=writes[0][2]["body"], user=dict(login="tauceti-review-bot[bot]"))
        self.assertEqual(rebase_heads([comment]), [HEAD])
        api = FakeAPI()
        api.comments = [comment]
        sweep.handoff(api, REPO, pr(), "main", [], dry_run=False)
        self.assertEqual([c[1] for c in api.calls if c[0] != "GET"], [f"repos/{REPO}/issues/17/labels"])

    def test_dry_run_moved_head_pause_draft_and_closed_do_not_mutate(self):
        for field, value in [("head", dict(sha=MAIN)), ("state", "closed"), ("draft", True),
                             ("labels", [dict(name="keep")]), ("labels", [dict(name="needs-rebase")])]:
            api = FakeAPI()
            api.live[field] = value
            self.assertEqual(sweep.handoff(api, REPO, pr(), "main", [], dry_run=False), "stale_or_parked")
            self.assertFalse(any(c[0] != "GET" for c in api.calls))
        api = FakeAPI()
        self.assertEqual(sweep.handoff(api, REPO, pr(), "main", [], dry_run=True), "would_label")
        self.assertFalse(any(c[0] != "GET" for c in api.calls))

    def test_push_or_hold_arriving_during_comment_creation_cannot_label_a_new_head(self):
        api = FakeAPI()
        original = api.request
        def changed(method, path, data=None):
            result = original(method, path, data)
            if method == "POST":
                api.live["head"]["sha"] = MAIN
            return result
        api.request = changed
        self.assertEqual(sweep.handoff(api, REPO, pr(), "main", [], dry_run=False), "stale_or_parked")
        self.assertFalse(any(c[1].endswith("/labels") for c in api.calls))

    def test_eligibility_respects_holds_drafts_other_branches_and_truncated_labels(self):
        self.assertTrue(sweep.eligible(pr(), "main"))
        for changes in [dict(isDraft=True), dict(baseRefName="other"),
                        dict(labels=dict(nodes=[], pageInfo=dict(hasNextPage=True))),
                        dict(labels=dict(nodes=[dict(name="human")], pageInfo=dict(hasNextPage=False)))]:
            candidate = pr() | changes
            self.assertFalse(sweep.eligible(candidate, "main"))

    def test_main_noop_pin_and_quota_exit_report_actual_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            args = ["--before", HEAD, "--after", MAIN, "--mathlib", directory, "--report", str(report)]
            api = Mock()
            with patch.object(sweep, "GitHub", return_value=api), patch.object(sweep, "git", return_value=manifest(OLD).encode()):
                self.assertEqual(sweep.main(args), 0)
                self.assertTrue(json.loads(report.read_text())["complete"])
                api.request.assert_not_called()
            with patch.object(sweep, "GitHub", return_value=api), patch.object(sweep, "git", side_effect=[manifest(OLD).encode(), manifest(NEW).encode(), b"", b"", b""]), patch.object(sweep, "new_deprecations", return_value=NAMES), patch.object(sweep, "open_prs", side_effect=sweep.Deferred("workflow-read", 123)):
                with self.assertRaises(sweep.Deferred):
                    sweep.main(args)
                self.assertFalse(json.loads(report.read_text())["complete"])

    def test_open_pr_pagination_keeps_all_heads_and_partial_labels_are_never_actionable(self):
        api = Mock()
        def page(rows, more, cursor):
            return dict(repository=dict(pullRequests=dict(nodes=rows, pageInfo=dict(hasNextPage=more, endCursor=cursor))))
        api.graphql.side_effect = [page([pr()], True, "next"), page([pr() | {"number": 18}], False, "last")]
        self.assertEqual([p["number"] for p in sweep.open_prs(api, REPO)], [17, 18])
        self.assertEqual(api.graphql.call_args_list[1].args[1]["cursor"], "next")

    def test_workflow_only_fires_after_main_manifest_changes_and_reuses_existing_automation(self):
        text = (SCRIPTS.parent / "project/.github/workflows/deprecated-prs.yml").read_text()
        workflow = yaml.safe_load(text)
        events = workflow.get("on", workflow.get(True))
        self.assertEqual(events["push"], {"branches": ["main"], "paths": ["lake-manifest.json"]})
        self.assertEqual(set(events), {"push", "workflow_dispatch"})
        self.assertTrue(events["workflow_dispatch"]["inputs"]["dry-run"]["default"])
        self.assertEqual(set(workflow["jobs"]), {"reconcile"})
        self.assertNotIn("needs", workflow["jobs"]["reconcile"])
        self.assertIn("github.sha", workflow["concurrency"]["group"])
        self.assertFalse(workflow["concurrency"]["cancel-in-progress"])
        self.assertIn("github.workflow_sha", text)
        self.assertNotIn("refs/pull", text)
        self.assertNotIn("bors", text.lower())
        app = next(s for s in workflow["jobs"]["reconcile"]["steps"] if s.get("id") == "app")
        self.assertEqual(app["with"]["app-id"], "${{ secrets.APP_ID }}")
        self.assertEqual({k:v for k,v in app["with"].items() if k.startswith("permission-")},
                         {"permission-issues": "write", "permission-pull-requests": "write"})


if __name__ == "__main__":
    unittest.main()
