"""Doctor checks merge settings, without learning policy from PR history."""

import unittest

from tauceti_tools.doctor import merge_findings
from tauceti_tools.project import ConfigError, parse
from tauceti_tools.scaffold import initial_configuration


class MergeSetupTests(unittest.TestCase):
    def setUp(self):
        self.project = parse(initial_configuration("one/Project", "human").encode())
        self.rules = [
            {"type": "pull_request", "ruleset_id": 11, "parameters": {"require_code_owner_review": True}},
            {"type": "required_status_checks", "ruleset_id": 11, "parameters": {
                "required_status_checks": [{"context": name} for name in ("build", "scope", "bump-guard")]}},
            {"type": "merge_queue", "ruleset_id": 11},
        ]
        self.actors = [{"actor_type": "RepositoryRole", "actor_id": 5, "bypass_mode": "pull_request"}]
        self.responses = {
            "repos/one/Project": {"permissions": {"admin": True}},
            "repos/one/Project/rules/branches/main": self.rules,
            "repos/one/Project/rulesets/11": {"bypass_actors": self.actors},
            "apps/project-bors": {"id": 88, "permissions": {"contents": "write"}},
        }
        self.calls = []

    def read(self, path):
        self.calls.append(path)
        value = self.responses.get(path, "GitHub HTTP 404")
        return (None, value) if isinstance(value, str) else (value, "")

    def finding(self):
        return merge_findings(self.project, self, "doctor")[0]

    def use_bors(self):
        self.project.raw["merge"]["backend"] = "bors"
        self.project.raw["services"]["bors"].update(mode="custom", url="https://bors.example.org", repository_id=7)
        self.rules.pop()
        self.actors[:] = [{"actor_type": "Integration", "actor_id": 88, "bypass_mode": "always"}]

    def test_queue_needs_no_bors_and_does_not_inspect_prs(self):
        finding = self.finding()
        self.assertEqual(finding.status, "ready", finding.summary)
        self.assertIn("admin merge", " ".join(finding.steps))
        self.assertFalse(any("/pulls" in p or "/statuses" in p or p.startswith("apps/") for p in self.calls))

    def test_queue_requires_scope_and_queue_rules(self):
        self.rules.pop()
        self.rules[1]["parameters"]["required_status_checks"] = [{"context": "build"}]
        finding = self.finding()
        self.assertEqual(finding.status, "failed")
        self.assertIn("merge_queue", finding.summary)
        self.assertIn("scope", finding.summary)
        self.assertIn("bump-guard", finding.summary)

    def test_queue_requires_admin_override_in_every_applicable_ruleset(self):
        self.rules.append({"type": "required_status_checks", "ruleset_id": 12, "parameters": {}})
        self.responses["repos/one/Project/rulesets/12"] = {"bypass_actors": []}
        finding = self.finding()
        self.assertEqual(finding.status, "failed")
        self.assertIn("ruleset 12", finding.summary)

    def test_deletion_and_force_push_protection_do_not_need_bypasses(self):
        for backend in ("queue", "bors"):
            with self.subTest(backend=backend):
                if backend == "bors":
                    self.use_bors()
                self.rules.append({"type": "deletion", "ruleset_id": 12})
                self.rules.append({"type": "non_fast_forward", "ruleset_id": 12})
                self.assertEqual(self.finding().status, "ready")
                self.assertNotIn("repos/one/Project/rulesets/12", self.calls)
                self.rules[:] = [r for r in self.rules if r.get("ruleset_id") != 12]

    def test_only_administrators_and_the_selected_bors_app_can_bypass_scope(self):
        outsiders = [
            {"actor_type": "RepositoryRole", "actor_id": 4, "bypass_mode": "pull_request"},
            {"actor_type": "RepositoryRole", "actor_id": 2, "bypass_mode": "always"},
            {"actor_type": "Team", "actor_id": 44, "bypass_mode": "always"},
            {"actor_type": "DeployKey", "actor_id": None, "bypass_mode": "always"},
            {"actor_type": "Integration", "actor_id": 99, "bypass_mode": "always"},
        ]
        for backend in ("queue", "bors"):
            if backend == "bors":
                self.use_bors()
            for outsider in outsiders:
                with self.subTest(backend=backend, actor=outsider):
                    self.actors.append(outsider)
                    self.assertEqual(self.finding().status, "failed")
                    self.assertIn("Restrict the bypass list", self.finding().summary)
                    self.actors.pop()
            if backend == "bors":
                self.actors.append({"actor_type": "RepositoryRole", "actor_id": 5, "bypass_mode": "always"})
                self.assertEqual(self.finding().status, "failed")

    def test_unreadable_rules_are_unknown_not_invented_missing_rules(self):
        self.responses["repos/one/Project/rules/branches/main"] = "GitHub HTTP 403"
        finding = self.finding()
        self.assertEqual(finding.status, "unknown")
        self.assertNotIn("Missing rule", finding.summary)
        self.responses["repos/one/Project/rules/branches/main"] = []
        self.responses["repos/one/Project/branches/main/protection"] = "GitHub HTTP 403"
        self.responses["repos/one/Project"]["permissions"]["admin"] = False
        finding = self.finding()
        self.assertEqual(finding.status, "unknown")
        self.assertNotIn("Missing rule", finding.summary)

    def test_queue_rejects_app_bypass_and_classic_enforced_admins(self):
        self.actors.append({"actor_type": "Integration", "actor_id": 88, "bypass_mode": "pull_request"})
        self.responses["repos/one/Project/branches/main/protection"] = {"enforce_admins": {"enabled": True}}
        finding = self.finding()
        self.assertEqual(finding.status, "failed")
        self.assertIn("remove automation Apps", finding.summary)
        self.assertIn("Classic protection", finding.summary)

    def test_hidden_bypass_list_or_classic_protection_is_not_reported_ready(self):
        self.responses["repos/one/Project/rulesets/11"] = {}
        finding = self.finding()
        self.assertEqual(finding.status, "unknown")
        self.assertIn("hides its bypass list", finding.summary)
        self.responses["repos/one/Project/rulesets/11"] = {"bypass_actors": self.actors}
        self.responses["repos/one/Project"]["permissions"]["admin"] = False
        finding = self.finding()
        self.assertEqual(finding.status, "unknown")
        self.assertIn("repository administrator", finding.summary)

    def test_bors_requires_its_own_app_authority_and_repository_settings(self):
        self.use_bors()
        finding = self.finding()
        self.assertEqual(finding.status, "ready", finding.summary)
        self.assertIn("bors merge", " ".join(finding.steps))
        self.assertIn("GitHub App manager", finding.role)
        self.actors[0]["bypass_mode"] = "pull_request"
        self.assertIn("Always allow", self.finding().summary)
        self.actors[0]["bypass_mode"] = "always"
        del self.project.raw["services"]["bors"]["repository_id"]
        self.assertIn("services.bors.repository_id", self.finding().summary)

    def test_bors_and_queue_cannot_both_be_selected(self):
        self.use_bors()
        self.rules.append({"type": "merge_queue", "ruleset_id": 11})
        self.assertIn("Disable the GitHub merge queue", self.finding().summary)

    def test_custom_bors_slug_and_inaccessible_settings(self):
        self.use_bors()
        self.project.raw["services"]["bors"]["app_slug"] = "existing-bors"
        self.responses["apps/existing-bors"] = {"id": 88, "permissions": {"contents": "write"}}
        self.assertEqual(self.finding().status, "ready")
        self.responses["repos/one/Project/rulesets/11"] = "GitHub HTTP 403"
        finding = self.finding()
        self.assertEqual(finding.status, "unknown")
        self.assertIn("ruleset 11", finding.summary)
        self.responses["repos/one/Project/rulesets/11"] = {"bypass_actors": self.actors}
        self.responses["apps/existing-bors"] = "GitHub HTTP 403"
        self.assertEqual(self.finding().status, "unknown")

    def test_bors_app_needs_permission_to_update_contents(self):
        self.use_bors()
        self.responses["apps/project-bors"]["permissions"]["contents"] = "read"
        self.assertIn("Contents: write", self.finding().summary)

    def test_old_direct_config_explains_the_two_choices(self):
        text = initial_configuration("one/Project", "human").replace('backend = "github-queue"', 'backend = "github-direct"')
        with self.assertRaises(ConfigError) as error:
            parse(text.encode())
        self.assertIn('merge.backend = "github-queue"', str(error.exception))
        self.assertIn('merge.backend = "bors"', str(error.exception))
        self.assertIn("does not change", str(error.exception))


if __name__ == "__main__":
    unittest.main()
