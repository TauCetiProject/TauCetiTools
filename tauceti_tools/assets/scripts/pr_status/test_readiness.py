"""Contract tests using the actual pinned Auto-merge engine, without network calls."""

import copy
from contextlib import nullcontext
import time
import importlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import readiness


HEAD = "current"
NOW = 1700000000
PR = {"number": 1, "state": "open", "head": {"sha": HEAD}, "base": {"ref": "main", "sha": "tip"},
      "draft": False, "labels": []}
STATUSES = {"build": "success", "scope": "success", "bump-guard": "success"}
MERGE_BASE = "mergebase"
PATHS = ["TauCeti/X.lean"]


def board(states=None, head=HEAD, mode="commit", updated="2026-09-16T01:00:00Z", extra=None):
    if states is None:
        states = {r: "green" for r in readiness.engine().DEFAULT_RUBRICS}
    meta = {"head_sha": head, "mode": mode, "states": states, "merge_base_sha": MERGE_BASE}
    meta.update(extra or {})
    return {"body": "<!--tauceti-scoreboard--><!--tauceti-meta:v1 " + json.dumps(meta) + "-->",
            "updated_at": updated}


def marker(head=HEAD, expires=NOW + 60):
    return {"body": "<!--tauceti-review-in-progress " +
            json.dumps({"head": head, "expires_at": expires}) + "-->"}


class GateContract(unittest.TestCase):
    def check(self, expected, comments=None, statuses=None, paths=PATHS, pr=None,
              merge_base=MERGE_BASE):
        result = readiness.classify(pr or PR, [board()] if comments is None else comments,
                                    STATUSES if statuses is None else statuses, paths,
                                    merge_base, NOW)
        self.assertEqual(result["category"], expected, result)
        self.assertEqual(result["eligible"], expected == "ready-to-merge")
        if result["eligible"]:
            self.assertTrue(result["gate"]["merge"])
        return result

    def test_complete_current_review_and_all_guards_are_ready(self):
        self.check("ready-to-merge")

    def test_queue_refusal_of_this_head_needs_the_author(self):
        def request(head=HEAD, login="tauceti-queue[bot]"):
            return {"user": {"login": login}, "body": f"Merge-queue recovery for head `{head[:7]}`: "
                    f"this PR conflicts with `main`.\n\n<!--tauceti-rebase:v1 {head}-->\n"}
        head = "a" * 40
        pr = dict(PR, head={"sha": head})
        current = board(head=head)
        result = readiness.classify(pr, [current, request(head)], STATUSES, PATHS, MERGE_BASE, NOW)
        self.assertEqual((result["category"], result["eligible"]), ("awaiting-author", False))
        for others in ([request("b" * 40)], [request(head, login="someone")], [request(head, login="tauceti-queue")]):
            result = readiness.classify(pr, [current, *others], STATUSES, PATHS, MERGE_BASE, NOW)
            self.assertEqual(result["category"], "ready-to-merge", others)
        self.assertEqual(readiness._rest_login({"login": "tauceti-queue", "__typename": "Bot"}), "tauceti-queue[bot]")
        self.assertEqual(readiness._rest_login({"login": "kim", "__typename": "User"}), "kim")

    def test_scope_failure_for_human_files_is_not_ready(self):
        self.check("needs-human-review", statuses=dict(STATUSES, scope="failure"),
                   paths=["web/examples/Examples.lean"])

    def test_green_scope_does_not_override_forbidden_path(self):
        self.check("needs-human-review", paths=["scripts/x.py"])

    def test_missing_scope_is_pending_and_failed_scope_is_ci_failure(self):
        self.check("awaiting-CI", statuses={"build": "success"})
        self.check("merge-check-failed", statuses=dict(STATUSES, scope="failure"))

    def test_pin_requires_bump_guard(self):
        paths = ["lake-manifest.json"]
        self.check("awaiting-CI", statuses=dict(STATUSES, **{"bump-guard": ""}), paths=paths)
        self.check("merge-check-failed", statuses=dict(STATUSES, **{"bump-guard": "failure"}),
                   paths=paths)
        self.check("ready-to-merge", paths=paths)

    def test_review_of_another_merge_base_is_not_ready(self):
        self.check("awaiting-review", merge_base="retargeted")
        self.check("awaiting-review", merge_base="")

    def test_blocking_review_of_another_merge_base_awaits_review_not_author(self):
        states = {r: "green" for r in readiness.engine().DEFAULT_RUBRICS}
        states["proof-quality"] = "blocking_request"
        self.check("awaiting-author", comments=[board(states=states)])
        self.check("awaiting-review", comments=[board(states=states)], merge_base="retargeted")
        self.check("awaiting-review", comments=[board(states=states, extra={"merge_base_sha": ""})])

    def test_stale_and_incomplete_scoreboards_cannot_be_ready(self):
        self.check("awaiting-review", comments=[board(head="old")])
        self.check("awaiting-review", comments=[board(states={"correctness": "green"})])
        self.check("awaiting-review", comments=[board(mode="init")])
        self.check("awaiting-review", comments=[])

    def test_old_markers_do_not_delay_enqueue(self):
        self.check("ready-to-merge", comments=[board(), marker()])
        self.check("ready-to-merge", comments=[board(), marker(expires=NOW)])
        self.check("ready-to-merge", comments=[board(), marker(head="old")])

    def test_selects_completed_current_head_not_newer_old_head_or_init(self):
        self.check("ready-to-merge", comments=[board(), board(head="old", states={}, updated="z")])
        self.check("ready-to-merge", comments=[board(), board(mode="init", states={}, updated="z")])

    def test_newer_completed_blocking_review_supersedes_approval(self):
        states = {r: "green" for r in readiness.engine().DEFAULT_RUBRICS}
        states["proof-quality"] = "blocking_request"
        self.check("awaiting-author", comments=[board(), board(states=states, updated="z")])

    def test_legacy_runs_cannot_substitute_for_complete_table(self):
        old = board(states={}, extra={"runs": [{"verdict": "approve"}]})
        self.check("awaiting-review", comments=[old])
        old["body"] += "\n" + "\n".join(
            f"| ✅ | [{r}](url) | approved | judge | summary |"
            for r in readiness.engine().DEFAULT_RUBRICS)
        self.check("ready-to-merge", comments=[old])

    def test_a_rubric_being_phased_in_is_asked_only_of_prs_above_its_cutoff(self):
        ten = list(readiness.engine().DEFAULT_RUBRICS)
        phased = ten + ["quality-pass"]

        def rubrics_for(repo, number):
            return list(phased if repo == "Owner/Repo" and int(number) > 1 else ten)

        later = dict(PR, number=2)
        with patch.object(readiness.engine(), "rubrics_for", rubrics_for, create=True), \
                patch.object(readiness.core, "REPO", "Owner/Repo"):
            self.check("ready-to-merge")
            self.check("awaiting-review", pr=later)
            states = {r: "green" for r in phased}
            self.check("ready-to-merge", pr=later, comments=[board(states=states)])
            states["quality-pass"] = "blocking_request"
            self.check("awaiting-author", pr=later, comments=[board(states=states)])
            elsewhere = readiness.classify(later, [board()], STATUSES, PATHS, MERGE_BASE, NOW,
                                           repo="Other/Repo")
            self.assertEqual(elsewhere["category"], "ready-to-merge")

    def test_an_engine_without_phased_rubrics_asks_its_one_list_of_every_pr(self):
        with patch.object(readiness.engine(), "rubrics_for", None, create=True):
            self.check("ready-to-merge", pr=dict(PR, number=10 ** 9))

    def test_the_pinned_engines_own_table_is_followed_once_it_has_one(self):
        gate = readiness.engine()
        table = getattr(importlib.import_module("admission_policy"), "PHASED_RUBRICS", None)
        if table is None or not hasattr(gate, "rubrics_for"):
            self.skipTest("the pinned review engine predates phased rubrics")
        later = dict(PR, number=101)
        with patch.dict(table, {"quality-pass": {readiness.core.REPO: 100}}):
            self.check("ready-to-merge", pr=dict(PR, number=100))
            self.check("awaiting-review", pr=later)
            states = {r: "green" for r in gate.rubrics_for(readiness.core.REPO, 101)}
            self.check("ready-to-merge", pr=later, comments=[board(states=states)])

    def test_ci_failure_and_missing_build_are_never_ready(self):
        self.check("ci-failed", statuses=dict(STATUSES, build="failure"))
        self.check("awaiting-CI", statuses=dict(STATUSES, build="pending"))
        self.check("awaiting-CI", statuses=dict(STATUSES, build=""))

    def test_queue_independent_routing(self):
        self.check("awaiting-dependency", pr=dict(PR, base={"ref": "parent"}))
        self.check("on-hold", pr=dict(PR, draft=True))
        for label in readiness.sweep_engine().KEEP_LABELS:
            self.check("on-hold", pr=dict(PR, labels=[{"name": label.upper()}]))
        self.check(None, pr=dict(PR, state="closed"))

    def test_explicit_merge_conflict_is_not_ready(self):
        self.check("awaiting-author", pr=dict(PR, mergeable=False))


class ReadEvidence(unittest.TestCase):
    def test_shared_unchanged_decision_reuses_a_self_tested_gate(self):
        from transport import ReconciliationAPI
        import self_tested
        current = copy.deepcopy(PR)
        current.update(user={'login': 'peer'}, labels=[{'name': 'self-tested'}])
        current['head']['ref'] = 'math'
        statuses = {k: v for k, v in STATUSES.items() if k != 'build'}
        api = ReconciliationAPI()
        api.shared_evidence, api.git_cache, api.policy = MagicMock(), 'verified-git-cache', 'policy-pin'
        with patch.object(readiness.core, 'transport', api), \
             patch.object(readiness, 'evidence', side_effect=lambda *a, **kw: (copy.deepcopy(current), [board()], statuses)), \
             patch.dict('os.environ', TAUCETI_SELF_TESTED_REPO='owner/repo'), \
             patch.object(readiness.core, 'gh_api', side_effect=AssertionError('no direct API reads')), \
             patch('tauceti_tools.git_inputs.merge_base', return_value=MERGE_BASE), \
             patch.object(readiness, 'diff_engine', return_value=SimpleNamespace(git_diff=Mock(return_value=PATHS))) as diff, \
             patch.object(readiness, 'diff_guards', return_value=([], '')), \
             patch.object(self_tested, 'decide', return_value=True) as eligibility:
            first = readiness.assess(1, repo='owner/repo')
            saved_until = api.decisions['1']['valid_until']
            self.assertGreater(saved_until, time.time()+60)
            current['labels'].append({'name': 'ready-to-merge'})
            current['base']['sha'] = 'new-main-same-merge-base'
            second = readiness.assess(1, repo='owner/repo')
            self.assertEqual(api.decisions['1']['valid_until'], saved_until)
            api.decisions['1']['valid_until'] = 0
            eligibility.return_value = False
            expired = readiness.assess(1, repo='owner/repo')
        self.assertTrue(first['eligible'])
        self.assertTrue(second['eligible'])
        self.assertIn('ready-to-merge', second['labels'])
        self.assertEqual(api.cache_hits, 1)
        self.assertFalse(expired['eligible'])
        self.assertEqual(expired['category'], 'awaiting-CI')
        self.assertEqual(eligibility.call_count, 2)
        self.assertEqual(diff.return_value.git_diff.call_count, 1)

    def test_a_labelled_failing_head_is_never_answered_from_the_cache(self):
        from transport import ReconciliationAPI
        from tauceti_tools.self_tested import CUT_OFF_DESCRIPTION
        current = copy.deepcopy(PR)
        current.update(labels=[{'name': 'self-tested'}])
        current['head']['ref'] = 'math'
        statuses = dict(STATUSES, build='failure')
        api = ReconciliationAPI()
        api.shared_evidence, api.git_cache, api.policy = MagicMock(), 'verified-git-cache', 'policy-pin'
        descriptions = [CUT_OFF_DESCRIPTION, 'sandboxed build, audit, or simp-NF lint failed']
        replies = [{'repository': {'object': {'status': {'context': {
            'state': 'FAILURE', 'description': d, 'targetUrl': 'https://github.com/owner/repo/actions/runs/7',
            'createdAt': '2026-11-24T01:00:00Z'}}}}}
            for d in descriptions]
        import self_tested
        with patch.object(readiness.core, 'transport', api), \
             patch.object(readiness, 'evidence', side_effect=lambda *a, **kw: (copy.deepcopy(current), [board()], dict(statuses))), \
             patch('tauceti_tools.git_inputs.merge_base', return_value=MERGE_BASE), \
             patch.object(readiness, 'diff_engine', return_value=SimpleNamespace(git_diff=Mock(return_value=PATHS))), \
             patch.object(readiness, 'diff_guards', return_value=([], '')), \
             patch.object(readiness, 'graphql_data', side_effect=replies), \
             patch.object(readiness.core, 'gh_api', side_effect=lambda path, **kw:
                          '[]' if 'pr-build.yml/runs?' in path else '{"status":"completed","conclusion":"failure"}'), \
             patch.object(self_tested, 'decide', return_value=True):
            first = readiness.assess(1, repo='owner/repo')
            second = readiness.assess(1, repo='owner/repo')
        self.assertNotIn('drop_self_tested', first)
        self.assertTrue(second['drop_self_tested'])
        self.assertEqual(second['category'], 'ci-failed')
        self.assertEqual((api.cache_hits, api.decisions), (0, {}))

    def node(self):
        return {"number": 1, "state": "OPEN", "isDraft": False, "baseRefName": "main",
                "baseRefOid": "tip",
                "headRefOid": HEAD, "mergeable": "MERGEABLE",
                "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                "commits": {"nodes": [{"commit": {"oid": HEAD, "status": {"contexts": [
                    {"context": k, "state": v.upper()} for k, v in STATUSES.items()]}}}]},
                "comments": {"nodes": [{"body": board()["body"], "updatedAt": "z", "createdAt": "a"}],
                             "pageInfo": {"hasNextPage": False}}}

    def test_paginated_comments_and_commit_status_contexts(self):
        first, second = self.node(), self.node()
        first["comments"]["nodes"] = []
        first["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
        with patch.object(readiness, "graphql", side_effect=[first, second]) as api:
            pr, comments, statuses = readiness.evidence(1, "owner/repo")
        self.assertEqual(api.call_args.kwargs["cursor"], "next")
        self.assertEqual(statuses, STATUSES)
        self.assertEqual(comments[0]["updated_at"], "z")
        self.assertEqual(pr["head"]["sha"], HEAD)

    def test_missing_status_is_not_green_and_mismatched_head_fails(self):
        node = self.node()
        node["commits"]["nodes"][0]["commit"]["status"] = None
        with patch.object(readiness, "graphql", return_value=node):
            self.assertEqual(readiness.evidence(1, "owner/repo")[2], {})
            node["commits"]["nodes"][0]["commit"]["oid"] = "old"
            with self.assertRaisesRegex(RuntimeError, "current PR head"):
                readiness.evidence(1, "owner/repo")

    def test_truncated_labels_fail_closed(self):
        node = self.node()
        node["labels"]["pageInfo"]["hasNextPage"] = True
        with patch.object(readiness, "graphql", return_value=node):
            with self.assertRaisesRegex(RuntimeError, "truncated labels"):
                readiness.evidence(1, "owner/repo")

    @patch.object(readiness.core, "gh_api", return_value=MERGE_BASE + "\n")
    @patch.object(readiness, "diff_engine", return_value=SimpleNamespace(
        git_diff=lambda remote, merge_base, head: PATHS))
    @patch.object(readiness, "evidence")
    def test_head_change_invalidates_result(self, evidence, diff, api):
        moved = copy.deepcopy(PR)
        moved["head"]["sha"] = "moved"
        evidence.side_effect = [(PR, [board()], STATUSES), (moved, [], STATUSES)]
        result = readiness.assess(1, repo="owner/repo", now=NOW)
        self.assertTrue(result["gate"]["merge"])
        self.assertFalse(result["eligible"])
        self.assertEqual(result["head"], "moved")

    def test_local_testing_is_decided_inside_existing_assessment(self):
        current = copy.deepcopy(PR)
        current['labels'] = [{'name': 'self-tested'}]
        current['head']['ref'] = 'math'
        missing = {'scope': 'success'}
        fake = MagicMock()
        fake.git_diff.return_value = PATHS
        import self_tested
        with patch.object(readiness, 'evidence', return_value=(current, [board()], missing)), \
             patch.object(readiness.core, 'gh_api', return_value=MERGE_BASE), \
             patch.object(readiness, 'diff_engine', return_value=fake), \
             patch.dict('os.environ', SELF_TESTED_WRITE_TOKEN='workflow-token'), \
             patch.object(readiness, 'diff_guards', return_value=([], '')), \
             patch.object(self_tested, 'reconcile', return_value=True) as assess:
            result = readiness.assess(1, repo='owner/repo', now=NOW)
        self.assertTrue(result['eligible'], result)
        assess.assert_called_once_with(1, 'owner/repo', pr=current, paths=PATHS)
        fake.git_diff.assert_called_once()

    def test_old_receipt_does_not_skip_direct_eligibility(self):
        current = copy.deepcopy(PR)
        current['labels'] = [{'name': 'self-tested'}]
        statuses = {'self-tested/1': 'success', 'scope': 'success', 'bump-guard': 'success'}
        fake = MagicMock()
        fake.git_diff.return_value = PATHS
        import self_tested
        with patch.object(readiness, 'evidence', return_value=(current, [board()], statuses)), \
             patch.object(readiness.core, 'gh_api', return_value=MERGE_BASE), \
             patch.object(readiness, 'diff_engine', return_value=fake), \
             patch.object(readiness, 'diff_guards', return_value=([], '')), \
             patch.object(self_tested, 'reconcile', return_value=True) as assess:
            result = readiness.assess(1, repo='owner/repo', now=NOW)
        self.assertTrue(result['eligible'], result)
        assess.assert_called_once_with(1, 'owner/repo', pr=current, paths=PATHS)
        fake.git_diff.assert_called_once()

    def test_only_approved_active_prs_fetch_diff(self):
        for current, comments, statuses, expected in (
                (PR, [board()], STATUSES, True), (PR, [], STATUSES, False),
                (PR, [board()], dict(STATUSES, build="failure"), False),
                (dict(PR, draft=True), [board()], STATUSES, False),
                (dict(PR, base={"ref": "parent", "sha": "tip"}), [board()], STATUSES, False)):
            fake = MagicMock()
            fake.git_diff.return_value = PATHS
            with patch.object(readiness, "evidence", return_value=(current, comments, statuses)), \
                 patch.object(readiness.core, "gh_api", return_value=MERGE_BASE) as api, \
                 patch.object(readiness, "diff_engine", return_value=fake):
                result = readiness.assess(1, now=NOW)
                self.assertEqual(fake.git_diff.called, expected)
                if readiness.preliminary(current) is None:
                    self.assertIn("compare/tip...current", api.call_args.args[0])
                else:
                    api.assert_not_called()
                self.assertEqual(result["eligible"], expected)

    @patch.object(readiness, "diff_engine")
    @patch.object(readiness, "evidence", side_effect=RuntimeError("rate limited"))
    def test_failed_read_is_not_a_merge_verdict(self, evidence, diff):
        with self.assertRaisesRegex(RuntimeError, "rate limited"):
            readiness.assess(1)
        diff.return_value.git_diff.assert_not_called()

    @patch.object(readiness, "diff_engine")
    @patch.object(readiness.core, "gh_api", side_effect=readiness.core.RateLimited("limited"))
    @patch.object(readiness, "evidence", return_value=(PR, [board()], STATUSES))
    def test_rate_limited_merge_base_read_stays_a_rate_limit(self, evidence, api, diff):
        with self.assertRaises(readiness.core.RateLimited):
            readiness.assess(1)
        diff.return_value.git_diff.assert_not_called()

    def test_graphql_http_200_rate_limit_opens_circuit(self):
        from types import SimpleNamespace
        out = SimpleNamespace(returncode=1, stdout='{"errors":[{"type":"RATE_LIMITED"}]}', stderr="")
        with patch.object(readiness.core, "_BLOCKED_UNTIL", 0), \
             patch.object(readiness.subprocess, "run", return_value=out) as run:
            for _ in range(2):
                with self.assertRaises(readiness.core.RateLimited):
                    readiness.graphql("query")
            self.assertEqual(run.call_count, 1)

    def test_audit_assesses_open_prs_concurrently_and_keeps_each_result_with_its_pr(self):
        import threading
        snapshot = {"repo": "owner/repo", "prs": [
            {"number": n, "state": "OPEN" if n != 3 else "CLOSED", "is_draft": n == 4} for n in range(1, 9)]}
        lock, active, peak = threading.Lock(), [0], [0]

        def assess(number, repo=None):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            if number == 5:
                raise RuntimeError("evidence unavailable")
            return {"number": number, "eligible": True, "category": None}

        with patch.object(readiness, "engine"), patch.object(readiness, "assess", side_effect=assess), \
             patch.object(readiness, "queue_snapshot", return_value={"known": False, "reason": "x"}), \
             patch.object(readiness, "AUDIT_WORKERS", 3):
            result = readiness.audit(snapshot)
        self.assertEqual(sorted(result["prs"], key=int), ["1", "2", "5", "6", "7", "8"])
        self.assertEqual(result["prs"]["6"]["number"], 6)
        self.assertIsNone(result["prs"]["5"]["eligible"])
        self.assertEqual(result["prs"]["5"]["reason"], "evidence unavailable")
        self.assertGreater(peak[0], 1)
        self.assertLessEqual(peak[0], 3)

    def test_missing_policy_publishes_unverified_audit(self):
        with patch.object(readiness, "engine", side_effect=RuntimeError("missing policy")):
            result = readiness.audit({"repo": "owner/repo", "prs": []})
        self.assertFalse(result["queue"]["known"])
        self.assertEqual(result["prs"], {})
        self.assertEqual(result["error"], "missing policy")


def guard_repository(tmp, files):
    """A served repository with a base commit and a head adding `files` ({path: line count or bytes})."""
    import os
    import subprocess
    from pathlib import Path
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    directory = Path(tmp) / "remote"
    directory.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(directory), *args], check=True, env=env,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "uploadpack.allowFilter", "true")
    git("config", "uploadpack.allowAnySHA1InWant", "true")
    (directory / "TauCeti").mkdir()
    (directory / "TauCeti/Basic.lean").write_text("theorem t : True := trivial\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    for path, lines in files.items():
        if isinstance(lines, bytes):  # raw content rather than a line count
            (directory / path).write_bytes(lines)
            continue
        (directory / path).write_text("".join(f"-- line {i}\n" for i in range(lines)))
    git("add", "-A")
    git("commit", "-qm", "head")
    return directory.as_uri(), base, git("rev-parse", "HEAD")


class SelfTestedDiffGuards(unittest.TestCase):
    """A self-tested PR skips PR CI; its diff guards are applied and labelled as PR CI's."""

    def test_measure_is_the_shared_pr_guards_on_fetched_objects(self):
        import tempfile
        cases = (({f"TauCeti/Part{i}.lean": 700 for i in range(5)}, readiness.SCOPE_TOO_LARGE,
                  "too large to review: +3500 / -0 lines across 5 files"),
                 ({"TauCeti/Long.lean": 1251}, readiness.SCOPE_NEW_FILE,
                  "TauCeti/Long.lean: new Lean files must be at most 1250 lines (found 1251)"),
                 ({"TauCeti/Blob.lean": b"\0\xff"}, readiness.SCOPE_CONTENT,
                  "adds content a plain text diff cannot review: TauCeti/Blob.lean contains a NUL byte"),
                 ({"TauCeti/Short.lean": 1250}, "", None))
        for files, scope, sentence in cases:
            with tempfile.TemporaryDirectory() as tmp:
                remote, base, head = guard_repository(tmp, files)
                violations, found = readiness.diff_guards("owner/repo", base, head, remote=remote)
            self.assertEqual(found, scope)
            if sentence is None:
                self.assertEqual(violations, [])
            else:
                self.assertEqual(len(violations), 1, violations)
                self.assertTrue(violations[0].startswith(sentence), violations)

    def test_unreadable_statistics_fail_closed(self):
        import tempfile
        import ci_git
        with tempfile.TemporaryDirectory() as tmp:
            remote, base, head = guard_repository(tmp, {"TauCeti/Short.lean": 1})
            with patch.object(ci_git, "pr_guards", side_effect=ValueError("unreadable")):
                self.assertEqual(readiness.diff_guards("owner/repo", base, head, remote=remote),
                                 ([readiness.UNMEASURABLE], readiness.SCOPE_UNMEASURED))

    def test_descriptions_are_pr_ci_word_for_word(self):
        from pathlib import Path
        # TauCetiTools' own workflow here; TauCeti's when the scripts are materialized there.
        workflow = next(directory / ".github/workflows/pr-build.yml"
                        for directory in Path(readiness.__file__).resolve().parents
                        if (directory / ".github/workflows/pr-build.yml").is_file())
        text = workflow.read_text()
        for description, variable in ((readiness.SCOPE_TOO_LARGE, "TOO_LARGE"),
                                      (readiness.SCOPE_UNMEASURED, "DIFF_UNKNOWN"),
                                      (readiness.SCOPE_NEW_FILE, "NEWFILE_TOO_LONG"),
                                      (readiness.SCOPE_CONTENT, "CONTENT_FORBIDDEN")):
            self.assertIn(f'"${{{{ env.{variable} }}}}" = "1" ]; then\n'
                          f'            sc_state=failure; sc_desc="{description}"', text)

    def assess(self, guards, statuses=None, labels=("self-tested",), comments=None, patch_api=True):
        current = copy.deepcopy(PR)
        current["labels"] = [{"name": name} for name in labels]
        current["head"]["ref"] = "math"
        fake = MagicMock()
        fake.git_diff.return_value = PATHS
        import self_tested
        with patch.object(readiness, "evidence", return_value=(
                    current, [board()] if comments is None else comments,
                    {"scope": "success"} if statuses is None else statuses)), \
             patch.object(readiness.core, "gh_api", return_value=MERGE_BASE) if patch_api else nullcontext(), \
             patch.object(readiness, "diff_engine", return_value=fake), \
             patch.object(readiness, "diff_guards", return_value=guards) as measure, \
             patch.object(self_tested, "reconcile", return_value=True):
            return readiness.assess(1, repo="owner/repo", now=NOW), measure

    def test_oversized_self_tested_head_is_a_failed_build_with_a_failed_scope(self):
        sentence = "too large to review: +3992 / -0 lines across 5 files"
        for comments in ([board()], []):
            result, measure = self.assess(([sentence], readiness.SCOPE_TOO_LARGE), comments=comments)
            measure.assert_called_once_with("owner/repo", MERGE_BASE, HEAD)
            self.assertFalse(result["eligible"])
            self.assertEqual(result["category"], "ci-failed")
            self.assertEqual(result["reason"], "PR-level diff guards failed: " + sentence)
            self.assertEqual(result["scope_failure"], readiness.SCOPE_TOO_LARGE)

    def test_a_head_that_moves_during_assessment_gets_no_status(self):
        current = copy.deepcopy(PR)
        current["labels"] = [{"name": "self-tested"}]
        current["head"]["ref"] = "math"
        moved = copy.deepcopy(current)
        moved["head"]["sha"] = "moved"
        fake = MagicMock()
        fake.git_diff.return_value = PATHS
        import self_tested
        with patch.object(readiness, "evidence", side_effect=[(current, [board()], {}), (moved, [], {})]), \
             patch.object(readiness.core, "gh_api", return_value=MERGE_BASE), \
             patch.object(readiness, "diff_engine", return_value=fake), \
             patch.object(readiness, "diff_guards", return_value=(["too large"], readiness.SCOPE_TOO_LARGE)), \
             patch.object(self_tested, "reconcile", return_value=True):
            result = readiness.assess(1, repo="owner/repo", now=NOW)
        self.assertEqual((result["head"], result["category"]), ("moved", "awaiting-CI"))
        self.assertNotIn("scope_failure", result)

    def test_long_new_file_is_a_failed_merge_check(self):
        result, _ = self.assess((["TauCeti/Long.lean: new Lean files ..."], readiness.SCOPE_NEW_FILE))
        self.assertEqual(result["category"], "merge-check-failed")
        self.assertEqual(result["scope_failure"], readiness.SCOPE_NEW_FILE)

    def test_posted_failure_is_not_reposted_and_passing_heads_are_untouched(self):
        result, _ = self.assess((["too large"], readiness.SCOPE_TOO_LARGE), statuses={"scope": "failure"})
        self.assertEqual(result["category"], "ci-failed")
        self.assertNotIn("scope_failure", result)
        result, _ = self.assess(([], ""))
        self.assertTrue(result["eligible"], result)
        self.assertNotIn("scope_failure", result)

    def red(self, description, state="FAILURE", labels=("self-tested",), run=None,
            url="https://github.com/owner/repo/actions/runs/7", rebuilds=(), jobs=None):
        """Assess a labelled head whose `build` status failed, with `description` on GitHub.

        `rebuilds` are the successful pr-build runs a listing returns; `jobs` their jobs."""
        statuses = dict(STATUSES, build="failure")
        reply = {"repository": {"object": {"status": {"context": {
            "state": state, "description": description, "targetUrl": url,
            "createdAt": "2026-11-24T01:00:00Z"}}}}}
        run = json.dumps(dict(dict(id=7, created_at="2026-11-24T00:50:00Z", run_attempt=1),
                              **(run or {"status": "completed", "conclusion": "failure"})))
        jobs = json.dumps(jobs if jobs is not None else [{"name": "pr-build / sandboxed-build", "conclusion": "success"}])
        self.listings = []

        def api(path, **kw):
            if "/workflows/pr-build.yml/runs?" in path:
                self.listings.append(path)
                return json.dumps([r for r in rebuilds if f"event={r['event']}&" in path
                                   or ("head_sha=" in path and r["event"] == "pull_request_target")])
            if "/jobs?" in path:
                return jobs
            if "/actions/runs/" in path:
                return run
            return MERGE_BASE
        with patch.object(readiness, "graphql_data", return_value=reply) as read, \
             patch.object(readiness.core, "gh_api", side_effect=api):
            result, measure = self.assess((["too large"], readiness.SCOPE_TOO_LARGE),
                                          statuses=statuses, labels=labels, patch_api=False)
        return result, measure, read

    def test_a_later_green_rebuild_of_the_head_keeps_the_label(self):
        failed = "sandboxed build, audit, or simp-NF lint failed"
        later, earlier = "2026-11-24T02:00:00Z", "2026-11-24T00:30:00Z"
        green = [
            dict(id=9, event="workflow_dispatch", head_sha="main", display_title="pr-build 1", created_at=later, run_attempt=1),
            dict(id=8, event="pull_request_target", head_sha=HEAD, display_title="pr-build 1", created_at=later, run_attempt=2),
            dict(id=10, event="workflow_dispatch", head_sha="main", display_title=f"pr-build pr=1 head={HEAD}",
                 created_at=later, run_attempt=1),
            dict(id=11, event="repository_dispatch", head_sha="main", display_title="pr-build 1", created_at=later, run_attempt=1)]
        for run in green:
            result, _, _ = self.red(failed, rebuilds=[run])
            self.assertNotIn("drop_self_tested", result, run)
        unrelated = [
            dict(id=9, event="workflow_dispatch", head_sha="main", display_title="pr-build 2", created_at=later, run_attempt=1),
            dict(id=9, event="workflow_dispatch", head_sha="main", display_title="pr-build 1", created_at=earlier, run_attempt=1),
            dict(id=8, event="pull_request_target", head_sha="other", display_title="pr-build 1", created_at=later, run_attempt=1),
            dict(id=6, event="pull_request_target", head_sha=HEAD, display_title="pr-build 1", created_at=earlier,
                 run_started_at=earlier, run_attempt=1)]
        for run in unrelated:
            result, _, _ = self.red(failed, rebuilds=[run])
            self.assertTrue(result["drop_self_tested"], run)
        skipped = [{"name": "pr-build / sandboxed-build", "conclusion": "skipped"}]
        result, _, _ = self.red(failed, rebuilds=green[:1], jobs=skipped)
        self.assertTrue(result["drop_self_tested"])
        self.assertTrue(all("status=success" in path for path in self.listings), self.listings)

    def test_a_green_rerun_of_the_reporting_run_keeps_the_label(self):
        failed = "sandboxed build, audit, or simp-NF lint failed"
        rerun = dict(status="completed", conclusion="success", run_attempt=2, run_started_at="2026-11-24T02:00:00Z")
        result, _, _ = self.red(failed, run=rerun)
        self.assertNotIn("drop_self_tested", result)
        stale = dict(rerun, run_started_at="2026-11-24T00:50:00Z")
        result, _, _ = self.red(failed, run=stale)
        self.assertTrue(result["drop_self_tested"])

    def test_a_cancelled_or_unfinished_run_keeps_the_label(self):
        for run in ({"status": "completed", "conclusion": "cancelled"}, {"status": "in_progress", "conclusion": None}):
            result, _, _ = self.red("sandboxed build, audit, or simp-NF lint failed", run=run)
            self.assertNotIn("drop_self_tested", result, run)
        result, _, _ = self.red("sandboxed build, audit, or simp-NF lint failed", url="https://example.org/x")
        self.assertNotIn("drop_self_tested", result)

    def test_a_failed_pr_build_on_the_head_removes_the_label(self):
        result, measure, read = self.red("sandboxed build, audit, or simp-NF lint failed")
        read.assert_called_once_with(readiness.BUILD_STATUS, owner="owner", name="repo", head=HEAD)
        measure.assert_not_called()
        self.assertTrue(result["drop_self_tested"])
        self.assertEqual(result["category"], "ci-failed")
        self.assertFalse(result["eligible"])
        self.assertNotIn("scope_failure", result)

    def test_an_inert_label_is_removed_too(self):
        for labels in (("self-tested", "full-lint"), ("self-tested", "lint-repair")):
            result, _, read = self.red("sandboxed build, audit, or simp-NF lint failed", labels=labels)
            read.assert_called_once()
            self.assertTrue(result["drop_self_tested"], labels)
            self.assertEqual(result["category"], "ci-failed")

    def test_a_cut_off_build_keeps_the_label(self):
        from tauceti_tools.self_tested import CUT_OFF_DESCRIPTION
        result, measure, _ = self.red(CUT_OFF_DESCRIPTION)
        self.assertNotIn("drop_self_tested", result)
        measure.assert_called_once()

    def test_a_thread_exhaustion_failure_keeps_the_label(self):
        from tauceti_tools.self_tested import THREADS_DESCRIPTION
        result, measure, _ = self.red(THREADS_DESCRIPTION)
        self.assertNotIn("drop_self_tested", result)
        measure.assert_called_once()

    def test_a_status_that_is_no_longer_failed_keeps_the_label(self):
        for state in ("PENDING", "SUCCESS"):
            result, _, _ = self.red("sandboxed build, audit, or simp-NF lint failed", state=state)
            self.assertNotIn("drop_self_tested", result)

    def test_unlabelled_and_green_heads_read_nothing_more(self):
        for labels, statuses in (((), dict(STATUSES, build="failure")), (("self-tested",), STATUSES)):
            with patch.object(readiness, "graphql_data") as read:
                result, _ = self.assess(([], ""), statuses=statuses, labels=labels)
            read.assert_not_called()
            self.assertNotIn("drop_self_tested", result)

    def test_cut_off_description_is_pr_ci_word_for_word(self):
        from pathlib import Path
        from tauceti_tools.self_tested import CUT_OFF_DESCRIPTION
        workflow = next(directory / ".github/workflows/pr-build.yml"
                        for directory in Path(readiness.__file__).resolve().parents
                        if (directory / ".github/workflows/pr-build.yml").is_file())
        self.assertIn(f'state=failure; desc="{CUT_OFF_DESCRIPTION}"', workflow.read_text())

    def test_prs_that_ran_pr_ci_are_not_measured(self):
        for labels in ((), ("self-tested", "full-lint")):
            result, measure = self.assess((["too large"], readiness.SCOPE_TOO_LARGE),
                                          statuses=STATUSES, labels=labels)
            measure.assert_not_called()
            self.assertTrue(result["eligible"], (labels, result))


class TauCetiQueueMembership(unittest.TestCase):
    """The queue that lands PRs is TauCeti's own; GitHub's native merge queue stays empty."""

    OBSERVATION = {"batches": [{"members": [{"pr": 11, "head_sha": "a"}, {"pr": 12, "head_sha": "b"}]},
                               {"members": [{"pr": 13, "head_sha": "c"}]}]}
    STATUS = {"waiting": {"inputs": [
        {"number": 21, "source_identity": {"forge": {"kind": "github"}}},
        {"number": 22, "source_identity": None},
        {"number": 6342, "source_identity": {"forge": {"kind": "forgejo"}}},
    ]}}

    def response(self, body):
        raw = json.dumps(body).encode()
        resp = MagicMock()
        resp.__enter__.return_value.read.side_effect = lambda n=-1: raw
        return resp

    def numbers(self, status=None):
        import bors_observation
        with patch.object(bors_observation, "observation", return_value=self.OBSERVATION), \
             patch.object(readiness.urllib.request, "urlopen",
                          return_value=self.response(self.STATUS if status is None else status)):
            return readiness.tauceti_queue_numbers()

    def test_batch_members_and_github_waiting_inputs_are_queued(self):
        self.assertEqual(self.numbers(), {11, 12, 13, 21, 22})

    def test_a_malformed_status_is_an_error_not_an_empty_queue(self):
        with self.assertRaises(ValueError):
            self.numbers({"waiting": {"inputs": [{"number": "21"}]}})

    def test_snapshot_unions_both_queues_and_keeps_github_reservation_policy(self):
        sweep = SimpleNamespace(REPO=None, queue_entries=lambda: [{"number": 5}],
                                reservation_holder=lambda entries, now: None)
        with patch.object(readiness, "sweep_engine", return_value=sweep), \
             patch.object(readiness, "tauceti_queue_numbers", return_value={11, 21}), \
             patch.object(readiness.core, "_BLOCKED_UNTIL", 0):
            snap = readiness.queue_snapshot("TauCetiProject/TauCeti")
        self.assertEqual(snap, {"known": True, "numbers": [5, 11, 21], "reservation_holder": None})

    def test_an_unreadable_tauceti_queue_makes_the_queue_unknown(self):
        sweep = SimpleNamespace(REPO=None, queue_entries=lambda: [],
                                reservation_holder=lambda entries, now: None)
        with patch.object(readiness, "sweep_engine", return_value=sweep), \
             patch.object(readiness, "tauceti_queue_numbers", side_effect=OSError("down")), \
             patch.object(readiness.core, "_BLOCKED_UNTIL", 0):
            with self.assertRaises(RuntimeError):
                readiness.queue_snapshot("TauCetiProject/TauCeti")


if __name__ == "__main__":
    unittest.main()
