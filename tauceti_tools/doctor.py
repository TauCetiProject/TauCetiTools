"""Repository diagnostics and repeatable repairs. Checking never writes remotely."""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .project import DOCS, Project


@dataclass
class Finding:
    id: str
    status: str
    summary: str
    expected: str
    role: str = "Repository administrator"
    commands: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    verify: str = ""
    section: str = "repositories"
    repair: str = ""
    optional: bool = False

    def json(self):
        result = asdict(self)
        result["manual"] = DOCS + "#" + self.section
        return result


class GitHub:
    def __init__(self):
        self.cache = {}

    def read(self, path: str, *, paginate=True):
        if path not in self.cache:
            try:
                proc = subprocess.run(
                    ["gh", "api", *(["--paginate", "--slurp"] if paginate else []), path],
                    capture_output=True,
                    text=True,
                )
            except FileNotFoundError:
                self.cache[path] = (None, "gh is not installed")
            else:
                if proc.returncode:
                    # No response bodies: an endpoint can include credentials or private data.
                    code = next(
                        (x for x in ("401", "403", "404", "429", "500", "502", "503") if x in proc.stderr), "unknown"
                    )
                    self.cache[path] = (None, f"GitHub HTTP {code}; inaccessible does not establish absence")
                else:
                    try:
                        pages = json.loads(proc.stdout) if paginate else [json.loads(proc.stdout)]
                        self.cache[path] = (pages[0] if len(pages) == 1 else pages, "")
                    except (ValueError, IndexError):
                        self.cache[path] = (None, "GitHub returned an unreadable response")
        return self.cache[path]


def _pages(value):
    return value if isinstance(value, list) else [value]


def _items(value, key):
    if value is None:
        return []
    return [item for page in _pages(value) for item in (page.get(key, []) if isinstance(page, dict) else page)]


def _cmd(*args):
    return shlex.join(str(x) for x in args)


def diagnose(project: Project, *, extras=True, github=None) -> list[Finding]:
    if github is None:
        try:
            auth = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True)
            authenticated = auth.returncode == 0
            installed = True
        except FileNotFoundError:
            installed = authenticated = False
        if not authenticated:
            return [Finding(
                "github.auth", "failed", "GitHub CLI is not signed in" if installed else "GitHub CLI is not installed",
                "Authenticated GitHub CLI with access to administer this project",
                urls=[] if installed else ["https://cli.github.com/"],
                commands=["gh auth login --hostname github.com --git-protocol https --web --scopes workflow", "gh auth setup-git", "gh api user --jq .login"],
                steps=(["Install GitHub CLI using the command for your operating system at the linked page."] if not installed else []) + [
                    "Sign in as the project's human maintainer using the browser login command. Then run the status command to confirm the active account.",
                    "If GH_TOKEN or GITHUB_TOKEN is set, it overrides saved CLI credentials. Unset it in this terminal to use the account you just signed in to.",
                    "Run doctor again after signing in; it will then inspect repositories, workflows and credentials.",
                ], section="repositories")]
    gh = github or GitHub()
    findings = []
    conf = project.source
    again = _cmd("tauceti-tools", "doctor", "--conf", conf)
    owners = project.raw.get("project", {}).get("codeowners", [])
    if not isinstance(owners, list) or not owners or any(not isinstance(x, str) or not x.startswith("@") for x in owners):
        findings.append(Finding(
            "configuration.ownership", "failed", "No human CODEOWNER selected",
            "Human maintainers own configuration and workflow policy",
            commands=["gh api user --jq .login"],
            steps=[
                f"Run the command below as a human maintainer. In {conf}, under [project], set codeowners = [\"@LOGIN\"], replacing LOGIN with that returned GitHub login. Add other authorized human maintainers or teams to the array as needed.",
                "The workflow setup generates .github/CODEOWNERS for tauceti.toml, .github/, scripts/ and Lake configuration when that file is absent. If CODEOWNERS already exists, edit it to cover those paths while retaining existing ownership. Require code-owner review in the branch rule.",
            ], verify=again, section="configuration-and-ownership"))
    repo_info = {}
    wanted = {"content": project.content, "roadmap": project.repos["roadmap"]}
    if extras:
        wanted.update({k: v for k, v in project.repos.items() if k in ("data", "ci", "claims")})
    for role, repo in wanted.items():
        data, error = gh.read(f"repos/{repo}")
        repo_info[role] = data
        commands = [_cmd("gh", "auth", "status"), _cmd("gh", "repo", "view", repo)]
        if error:
            findings.append(
                Finding(
                    f"repositories.{role}",
                    "unknown",
                    f"{repo}: {error}",
                    "Repository exists and is accessible",
                    commands=commands,
                    urls=[f"https://github.com/{repo}", "https://github.com/new"],
                    steps=[
                        "Ask the repository owner to confirm the exact URL and give your GitHub account access. A 404 does not distinguish an absent repository from one you cannot access.",
                        "Repository write access permits setup PRs; changing branch rules and secrets requires administration permission. Organization App installation may require an organization owner. Broader token scopes cannot grant permissions your account does not hold.",
                        f"First check repository access with gh repo view. A 404 can mean missing access. Only if the owner confirms it is absent, create it:\n$ {_cmd('gh', 'repo', 'create', repo, '--public', '--add-readme')}",
                        f"For creation choose Owner: {repo.split('/')[0]}; Repository name: {repo.split('/')[1]}; Visibility: Public; initialize a README.",
                    ],
                    verify=_cmd("gh", "repo", "view", repo, "--json", "nameWithOwner,defaultBranchRef"),
                    optional=role not in ("content", "roadmap"),
                )
            )
            continue
        findings.append(Finding(f"repositories.{role}", "ready", repo, "Repository accessible",
                                optional=role not in ("content", "roadmap")))
        if data.get("private") or data.get("visibility") in ("private", "internal"):
            findings.append(Finding(
                f"repositories.{role}.visibility", "failed", f"{repo} is not public; private projects are not currently supported",
                "Public project repositories", urls=[f"https://github.com/{repo}/settings"],
                steps=[
                    "Stop before installing workflows or enrolling hosted services. The supported setup uses public project repositories: configuration fetching, worker forks, reusable workflows, publication and hosted services have not been adapted for private projects.",
                    f"Ask the owner whether to use a separate public repository. If so, edit repositories.{role} in {conf} to its owner/name, then rerun doctor. Do not publish the existing repository merely to clear this check; a visibility change requires the owner's deliberate review of its contents and history.",
                ], verify=again, optional=role not in ("content", "roadmap")))
        if data.get("archived") or data.get("disabled"):
            findings.append(
                Finding(
                    f"repositories.{role}.writable",
                    "failed",
                    "Repository archived or disabled",
                    "Active repository",
                    commands=[_cmd("gh", "repo", "unarchive", repo)] if data.get("archived") else [],
                    urls=[f"https://github.com/{repo}/settings"],
                    steps=[
                        "Under General → Danger Zone, select Unarchive this repository. A disabled repository requires its owner to contact GitHub support."
                    ],
                    verify=again,
                    optional=role not in ("content", "roadmap"),
                )
            )
        if role == "content" and data.get("default_branch") != project.branch:
            findings.append(
                Finding(
                    "project.branch",
                    "failed",
                    f"GitHub default branch is {data.get('default_branch')}",
                    f"Configuration branch is {project.branch}",
                    commands=[_cmd("gh", "repo", "view", repo, "--json", "defaultBranchRef")],
                    urls=[f"https://github.com/{repo}/settings"],
                    steps=[
                        f"Set project.branch in {conf} to {data.get('default_branch')!r}, or intentionally change General → Default branch to the configured branch. Do not target another PR's branch."
                    ],
                    verify=again,
                )
            )

    repo = project.content
    for tool in ("tools", "review", "progress", "docgen"):
        if not extras and tool in ("progress", "docgen"):
            continue
        pin = project.tool(tool)
        required = (tool not in ("progress", "docgen")
                    or tool == "progress" and project.feature("progress")
                    or tool == "docgen" and project.feature("docs") and not project.legacy)
        if not required:
            continue
        status, summary = "failed", "No version selected"
        if pin.get("ref"):
            resolved, pin_error = gh.read(f"repos/{pin['repo']}/commits/{pin['ref']}")
            status = "unknown" if pin_error else "ready" if resolved.get("sha") == pin["ref"] else "failed"
            summary = pin_error or pin["ref"]
        default_repo = "leanprover/doc-gen4" if tool == "docgen" else (
            "TauCetiProject/"
            + {"tools": "TauCetiTools", "review": "TauCetiReview", "progress": "TauCetiProgress"}[tool]
        )
        findings.append(
            Finding(
                "pins." + tool,
                status,
                summary,
                "An accessible immutable commit in the common configuration",
                commands=[_cmd("gh", "api", f"repos/{pin.get('repo', default_repo)}/commits/HEAD", "--jq", ".sha")],
                urls=[f"https://github.com/{pin.get('repo', default_repo)}/commits"],
                steps=[
                    f"Run the gh api command to obtain a full commit SHA. In {conf}, uncomment and edit the existing [tools.{tool}] table if present; otherwise add it once. Set its fields as follows, replacing PASTE_FULL_SHA with that output:\n[tools.{tool}]\nrepo = \"{pin.get('repo', default_repo)}\"\nref = \"PASTE_FULL_SHA\"",
                    "Select all required tool versions before installing workflows. The generated callers use these exact SHAs; rerun doctor after editing the configuration.",
                    *(["For doc-gen4, select a commit whose lean-toolchain matches this project's lean-toolchain; the latest commit may require a different Lean version."] if tool == "docgen" else []),
                ],
                verify=again,
                section="versions",
                optional=tool in ("progress", "docgen"),
            )
        )

    findings.extend(package_findings(project, gh, repo_info, again))
    if any(f.id == "lean.content" and f.status == "ready" for f in findings):
        findings.extend(dependency_findings(project, gh, again))
    if repo_info.get("content"):
        value, error = gh.read(f"repos/{repo}/actions/permissions")
        status = "unknown" if error else "ready" if value.get("enabled") else "failed"
        findings.append(
            Finding(
                "actions.enabled",
                status,
                error or f"Actions enabled: {value.get('enabled')}",
                "GitHub Actions enabled",
                commands=[
                    _cmd("gh", "api", "--method", "PUT", f"repos/{repo}/actions/permissions", "-F", "enabled=true")
                ],
                urls=[f"https://github.com/{repo}/settings/actions"],
                steps=[
                    "Open Settings → Actions → General. Enable Actions and allow the pinned actions and reusable workflows named in the generated workflow files. An organization policy may need its owner to allow these repositories."
                ],
                verify=_cmd("gh", "api", f"repos/{repo}/actions/permissions"),
                section="actions",
                repair="actions",
            )
        )

        for path in (
            "tauceti.toml",
            ".github/CODEOWNERS",
            ".github/workflows/pr-build.yml",
            ".github/workflows/auto-merge.yml",
            ".github/workflows/pr-status.yml",
        ):
            value, error = gh.read(f"repos/{repo}/contents/{path}?ref={project.branch}")
            status = "unknown" if error else "ready"
            summary = error or path
            if not error and value.get("encoding") == "base64":
                text = base64.b64decode(value["content"]).decode("utf-8")
                pin = project.tool("tools" if path.endswith("pr-build.yml") else "review")
                if (
                    path.endswith(("pr-build.yml", "auto-merge.yml"))
                    and pin.get("ref")
                    and "@" + pin["ref"] not in text
                ):
                    status, summary = "failed", path + ": workflow version differs from the canonical pin"
                if path == "tauceti.toml":
                    from .project import parse

                    try:
                        if parse(text.encode()).raw != project.raw:
                            status, summary = (
                                "failed",
                                "Default-branch tauceti.toml differs from selected configuration",
                            )
                    except ValueError:
                        status, summary = "failed", "Default-branch tauceti.toml is invalid"
            findings.append(
                Finding(
                    "files." + path,
                    status,
                    summary,
                    "Trusted default-branch configuration and workflow",
                    commands=[],
                    urls=[f"https://github.com/{repo}/tree/{project.branch}/.github"],
                    steps=[
                        "First select the Tools and Review versions reported above, and create/access the content repository. The preview command clones it into a temporary checkout; your working tree is preserved.",
                        f"Preview the configuration and workflow changes:\n$ {_cmd('tauceti-tools', 'doctor', '--conf', conf, '--fix', 'workflows')}\nInspect the printed diff and files, then open the setup PR:\n$ {_cmd('tauceti-tools', 'doctor', '--conf', conf, '--fix', 'workflows', '--apply')}",
                        "Open the PR URL printed by the command, review and merge it as a human. On a new repository, install these workflows before requiring their status checks. Then rerun doctor.",
                    ],
                    verify=again,
                    section="workflows",
                    repair="workflows",
                )
            )

        direct = project.raw.get("merge", {}).get("backend") == "github-direct"
        rules_endpoint = f"repos/{repo}/rules/branches/{project.branch}"
        rules_url = f"https://github.com/{repo}/settings/rules/new?target=branch"
        rules, error = gh.read(rules_endpoint)
        active_rules = [r for page in _pages(rules or []) for r in (page if isinstance(page, list) else [page])]
        # The rules API omits classic branch protection. Personal projects often
        # use it, and its checks/review requirements count just like rulesets.
        classic = False
        if not active_rules and not error:
            endpoint = f"repos/{repo}/branches/{project.branch}/protection"
            protection, error = gh.read(endpoint)
            if protection:
                classic = True
                rules_endpoint = endpoint
                rules_url = f"https://github.com/{repo}/settings/branches"
                reviews = protection.get("required_pull_request_reviews")
                checks = protection.get("required_status_checks")
                if reviews is not None:
                    active_rules.append({"type": "pull_request", "parameters": {
                        "require_code_owner_review": reviews.get("require_code_owner_reviews", False)}})
                if checks is not None:
                    active_rules.append({"type": "required_status_checks", "parameters": {
                        "strict_required_status_checks_policy": checks.get("strict", False),
                        "required_status_checks": [{"context": c} for c in checks.get("contexts", [])]}})
        types = {r.get("type") for r in active_rules if isinstance(r, dict)}
        if direct:
            strict = any(r.get("parameters", {}).get("strict_required_status_checks_policy")
                         for r in active_rules if r.get("type") == "required_status_checks")
            findings.append(Finding(
                "merge.direct", "unknown" if error else "failed" if strict or "merge_queue" in types else "ready",
                error or ("Disable strict checks and merge queue" if strict or "merge_queue" in types else "Direct merging permits branches behind main"),
                "Required checks without requiring an up-to-date branch or a merge queue",
                commands=[_cmd("gh", "api", rules_endpoint)],
                urls=[rules_url],
                steps=["Edit the active default-branch rule. Under Require status checks to pass, clear Require branches to be up to date before merging. Clear Require merge queue if present. Click Save changes."],
                verify=again, section="branch-rules"))
        needed = {"pull_request", "required_status_checks"}
        if project.raw.get("merge", {}).get("backend", "github-queue") == "github-queue":
            needed.add("merge_queue")
        missing = sorted(needed - types)
        required_checks = set(project.raw.get("merge", {}).get("checks", ["build", "bump-guard", "scope"]))
        actual_checks = {
            check["context"]
            for rule in active_rules
            if rule.get("type") == "required_status_checks"
            for check in rule.get("parameters", {}).get("required_status_checks", [])
        }
        missing.extend("check:" + name for name in sorted(required_checks - actual_checks))
        if not any(
            rule.get("type") == "pull_request" and rule.get("parameters", {}).get("require_code_owner_review")
            for rule in active_rules
        ):
            missing.append("code-owner review")
        findings.append(
            Finding(
                "rules.content",
                "unknown" if error else "failed" if missing else "ready",
                error
                or (
                    "Missing rules: " + ", ".join(missing)
                    if missing
                    else "Required rule types present; inspect configured checks"
                ),
                "PR review and required build, bump-guard, scope checks; merge queue when selected",
                commands=[_cmd("gh", "api", rules_endpoint)],
                urls=[rules_url],
                steps=[
                    f"Edit the existing protection rule for {project.branch}." if classic else f"Name: Tau Ceti content. Enforcement status: Active. Target branches → Add target → Include by pattern → {project.branch}.",
                    "After merging setup, open a normal content PR so the newly installed pr-build workflow runs. Then enable Require a pull request before merging and Require review from Code Owners. Under Require status checks to pass, Add checks: " + ", ".join(sorted(required_checks)) + ". Select their GitHub Actions producer. If a name is not listed yet, let that content PR's build finish, then return to the rule editor and add the check.",
                    ("Clear Require branches to be up to date before merging and Require merge queue. Keep the automation App out of the bypass list: it merges reviewed content through the normal PR checks." if direct else
                     "Enable Require merge queue." if project.raw.get("merge", {}).get("backend", "github-queue") == "github-queue" else
                     "Bors is the selected merger. Keep the existing merge authority until its pilot staging build succeeds; then use the Bors branch rules in the manual."),
                    "Keep configuration changes subject to human code-owner review. Click Save changes (classic protection) or Create/Save changes with Enforcement Active (ruleset).",
                ],
                verify=again,
                section="branch-rules",
            )
        )

        expected_variables = {
            "MERGE_BACKEND": {"bors": "bors", "github-direct": "direct"}.get(project.raw.get("merge", {}).get("backend"), "queue")
        }
        if extras and project.service("cache").get("mode", "off") != "off":
            cache = project.service("cache")
            expected_variables.update(
                {
                    "LAKE_CACHE_ARTIFACT_ENDPOINT_PUBLIC": cache.get("artifact_url", ""),
                    "LAKE_CACHE_REVISION_ENDPOINT_PUBLIC": cache.get("revision_url", ""),
                    "LAKE_CACHE_ARTIFACT_ENDPOINT": cache.get("artifact_write_url", ""),
                    "LAKE_CACHE_REVISION_ENDPOINT": cache.get("revision_write_url", ""),
                }
            )
        for name, expected in expected_variables.items():
            value, error = gh.read(f"repos/{repo}/actions/variables/{name}")
            correct = bool(expected) and not error and value.get("value") == expected
            findings.append(
                Finding(
                    "variables." + name,
                    "ready" if correct else "unknown" if error else "failed",
                    error or f"{name}: {'matches' if correct else 'does not match'} configuration",
                    f"Actions variable {name} = {expected or 'the configured endpoint'}",
                    commands=[
                        _cmd(
                            "gh",
                            "variable",
                            "set",
                            name,
                            "--repo",
                            repo,
                            "--body",
                            expected or "ENTER_CONFIGURED_ENDPOINT",
                        )
                    ],
                    urls=[f"https://github.com/{repo}/settings/variables/actions"],
                    steps=[
                        f"Choose Variables → New repository variable. Name: {name}; Value: {expected or 'the corresponding endpoint in services.cache'}.",
                        "For a new project, set MERGE_BACKEND now to the configured value so automation uses the intended merger. When switching an existing merge backend, wait for its pilot build and corresponding merge-rule setup before changing authority.",
                    ],
                    verify=again,
                    section="actions",
                    optional=name != "MERGE_BACKEND",
                )
            )

        metadata = []
        errors = []
        secret_endpoints = ("secrets", "organization-secrets") if repo_info["content"]["owner"]["type"] == "Organization" else ("secrets",)
        for endpoint in secret_endpoints:
            value, error = gh.read(f"repos/{repo}/actions/{endpoint}")
            metadata.extend(_items(value, "secrets"))
            if error:
                errors.append(error)
        names = {row["name"] for row in metadata}
        required_secrets = ["APP_ID", "APP_PRIVATE_KEY"]
        if extras and project.feature("claims"):
            required_secrets += ["CLAIMS_APP_ID", "CLAIMS_APP_PRIVATE_KEY"]
        if extras and project.service("cache").get("mode", "off") != "off":
            required_secrets += ["LAKE_CACHE_KEY"]
        if extras and project.feature("zulip"):
            required_secrets += ["ZULIP_EMAIL", "ZULIP_API_KEY"]
        if extras and project.feature("fleet"):
            required_secrets += ["FLEET_READ_TOKEN"]
        environments, environment_error = gh.read(f"repos/{repo}/environments")
        for environment in _items(environments, "environments"):
            value, env_error = gh.read(
                f"repos/{repo}/environments/{__import__('urllib.parse', fromlist=['quote']).quote(environment['name'], safe='')}/secrets"
            )
            overrides = sorted({row["name"] for row in _items(value, "secrets")} & set(required_secrets))
            if overrides or env_error:
                findings.append(
                    Finding(
                        "secrets.environment." + environment["name"],
                        "unknown",
                        env_error or "Environment overrides: " + ", ".join(overrides),
                        "Consuming jobs use the intended credential scope",
                        commands=[_cmd("gh", "api", f"repos/{repo}/environments")],
                        urls=[f"https://github.com/{repo}/settings/environments"],
                        steps=[
                            f"Open environment {environment['name']}. Under Environment secrets inspect the named overrides; replace their value with the intended App/provider credential or delete the override to use the repository secret.",
                            "Check Required reviewers and Deployment branches and tags. Approve the waiting job if intentional; allow the configured trusted branch. Inspect the consuming workflow's environment field before changing access.",
                        ],
                        verify=again,
                        section="apps-and-credentials",
                        optional=bool(overrides) and not env_error and not set(overrides) & {"APP_ID", "APP_PRIVATE_KEY"},
                    )
                )
        for name in required_secrets:
            present = name in names
            app_role = "claims" if name.startswith("CLAIMS_") else "automation"
            folder = (
                app_role
                if "APP_" in name
                else "cache"
                if name == "LAKE_CACHE_KEY"
                else "zulip"
                if name.startswith("ZULIP_")
                else "fleet"
            )
            secret_file = Path.home() / ".config/tauceti/projects" / project.key / folder / name
            from .credentials import credential_setup

            urls, steps = credential_setup(project, name, repo, secret_file,
                organization=repo_info["content"]["owner"]["type"] == "Organization")
            findings.append(
                Finding(
                    "secrets." + name,
                    "ready" if present else "unknown" if errors else "failed",
                    f"{name}: present, not functionally verified"
                    if present
                    else "; ".join(errors) or f"{name} unavailable",
                    "Selected feature credential available to its trusted consuming workflow",
                    commands=[f"{_cmd('gh', 'secret', 'set', name, '--repo', repo)} < {shlex.quote(str(secret_file))}"],
                    urls=urls,
                    steps=steps,
                    verify=again,
                    section="apps-and-credentials",
                    optional=name not in ("APP_ID", "APP_PRIVATE_KEY"),
                )
            )

        for consumer, workflow in (("pr-build", "pr-build.yml"), ("Auto-merge", "auto-merge.yml")):
            runs, error = gh.read(
                f"repos/{repo}/actions/workflows/{workflow}/runs?per_page=1",
                paginate=False,
            )
            candidates = _items(runs, "workflow_runs")
            latest = candidates[0] if candidates else None
            status = "pending"
            summary = error or (
                f"Latest run {latest['id']}: {latest.get('conclusion') or latest.get('status')}; inspect the consuming job"
                if latest
                else "No consuming workflow run observed"
            )
            if latest:
                if latest.get("conclusion") in ("failure", "timed_out", "action_required"):
                    status = "failed"
                jobs, job_error = gh.read(f"repos/{repo}/actions/runs/{latest['id']}/jobs")
                token_steps = [
                    step
                    for job in _items(jobs, "jobs")
                    for step in job.get("steps", [])
                    if any(
                        label in step.get("name", "").lower()
                        for label in (
                            "create-github-app-token",
                            "mint merge token",
                            "mint comment token",
                            "mint repo-scoped app token",
                            "mint the app token",
                        )
                    )
                ]
                if consumer == "pr-build" and not job_error and latest.get("conclusion") == "success":
                    steps = [step for job in _items(jobs, "jobs") for step in job.get("steps", [])]
                    if all(
                        any(
                            label in step.get("name", "").lower() and step.get("conclusion") == "success"
                            for step in steps
                        )
                        for label in ("build exact candidate", "report build and bump-guard")
                    ):
                        status, summary = (
                            "ready",
                            f"Run {latest['id']} built the candidate and reported its statuses using GITHUB_TOKEN; App credentials are checked by Auto-merge",
                        )
                changed = max(
                    (row.get("updated_at", "") for row in metadata if row["name"] in ("APP_ID", "APP_PRIVATE_KEY")),
                    default="",
                )
                if (
                    not job_error
                    and latest.get("conclusion") == "success"
                    and token_steps
                    and all(step.get("conclusion") == "success" for step in token_steps)
                    and latest.get("created_at", "") >= changed
                ):
                    status, summary = (
                        "ready",
                        f"Run {latest['id']} succeeded and its App-token steps succeeded after the recorded credential updates",
                    )
            findings.append(
                Finding(
                    "evidence." + consumer.lower(),
                    status,
                    summary,
                    "The actual build/status steps succeed; App-token creation is verified separately in Auto-merge"
                    if consumer == "pr-build"
                    else "Relevant credential-consuming step succeeds without skipping or fallback",
                    commands=(
                        [_cmd("gh", "run", "view", str(latest["id"]), "--repo", repo, "--log")]
                        if latest
                        else [_cmd("gh", "workflow", "list", "--repo", repo)]
                    ),
                    urls=[latest["html_url"] if latest else f"https://github.com/{repo}/actions"],
                    steps=[
                        "Use the existing workflow's normal trigger. pr-build runs on a content pull request; Auto-merge runs after a build or review update.",
                        f"For pr-build, list open content PRs and choose the number to build:\n$ {_cmd('gh', 'pr', 'list', '--repo', repo, '--state', 'open')}\nThen run:\n$ {_cmd('gh', 'workflow', 'run', 'pr-build.yml', '--repo', repo, '-f', 'pr=PR_NUMBER')}\nReplace PR_NUMBER with that open PR's number. The setup workflows must already be merged into the default branch.",
                        "Auto-merge has no manual-dispatch trigger. It runs after a normal content build or review update; use the Actions run linked above to inspect its App-token step. It only merges content that passes the configured review and build policy.",
                        "A successful overall run is not proof that an optional credential was used. Inspect the App-token and consuming steps; skipped/fallback steps leave the credential unverified.",
                    ],
                    verify=again,
                    section="verification",
                )
            )

    if extras:
        optional_findings = auxiliary_findings(project, gh, again) + extra_findings(project, gh, again)
        for finding in optional_findings:
            finding.optional = True
        findings.extend(optional_findings)
    return findings


def package_findings(project, gh, repo_info, again):
    """A workflow cannot build a newly created repository until its Lake package exists."""
    findings = []
    for role in ("content", "roadmap"):
        if not repo_info.get(role):
            continue
        repo = project.repos[role]
        branch = project.branch if role == "content" else repo_info[role].get("default_branch", project.branch)
        missing = []
        for path in ("lean-toolchain", "lake-manifest.json"):
            _, error = gh.read(f"repos/{repo}/contents/{path}?ref={branch}")
            if error:
                missing.append(path)
        _, toml_error = gh.read(f"repos/{repo}/contents/lakefile.toml?ref={branch}")
        if toml_error:
            missing.append("lakefile.toml")
        _, lean_error = gh.read(f"repos/{repo}/contents/lakefile.lean?ref={branch}")
        lean_format = not lean_error
        root = project.roots[0] if role == "content" else project.roadmap_dir
        if role == "content":
            lakefile = (f'name = "{root}"\ndefaultTargets = ["{root}"]\nplatformIndependent = true\n'
                        '[[require]]\nname = "mathlib"\ngit = "https://github.com/leanprover-community/mathlib4"\nrev = "master"\n'
                        f'[[lean_lib]]\nname = "{root}"\nglobs = ["{root}.*"]\n'
                        'leanOptions = { weak.linter.mathlibStandardSet = true, warningAsError = true }\n'
                        '[[lean_exe]]\nname = "axioms"\nroot = "scripts.Axioms"\n'
                        '[[lean_exe]]\nname = "module-system"\nroot = "scripts.ModuleSystem"')
            setup = [
                f"In a local checkout of {repo}, keep the existing Lean package if it has one. Commit and publish lean-toolchain, lakefile.toml, and lake-manifest.json. API inaccessibility does not establish that files are absent.",
                f"Set lean.roots in {project.source} to the package's actual Lean module root ({root} is currently selected). In the checkout run lake update and lake build; commit the resolved lake-manifest.json with the reviewed package configuration.",
                "Only if starting a new Mathlib-based package, obtain its matching toolchain:\n$ gh api repos/leanprover-community/mathlib4/contents/lean-toolchain?ref=master --jq '.content | @base64d' > lean-toolchain\n$ elan toolchain install \"$(cat lean-toolchain)\"\nFor a different direct dependency use that dependency's selected toolchain; keep its existing package dependencies.",
                f"For that new package, write lakefile.toml with:\n{lakefile}\nThe axioms and module-system sources arrive with the project workflow setup; install that setup before building these executables.",
                f"For a new package create its source directory and empty root module:\n$ mkdir -p {shlex.quote(root.replace('.', '/'))}\n$ printf 'module\\n' > {shlex.quote(root.replace('.', '/') + '.lean')}\nAdd your mathematical source modules there, with a Mathlib-style copyright/license/Authors header followed by module. After the workflow setup supplies scripts/Axioms.lean and scripts/ModuleSystem.lean:\n$ lake update\n$ lake exe cache get\n$ lake build\nCommit the toolchain, lakefile, manifest and Lean sources through a human-reviewed PR.",
            ]
        else:
            lakefile = (f'name = "{root}"\ndefaultTargets = ["{root}", "{project.completed_dir}"]\n'
                        f'[[require]]\nname = "{project.roots[0]}"\ngit = "https://github.com/{project.content}"\nrev = "{project.branch}"\n'
                        f'[[lean_lib]]\nname = "{root}"\nglobs = ["{root}.*"]\n'
                        f'[[lean_lib]]\nname = "{project.completed_dir}"\nglobs = ["{project.completed_dir}.+"]')
            setup = [
                f"In a local checkout of {repo}, keep an existing working roadmap package. For a new one, copy the content repository's toolchain:\n$ gh api repos/{project.content}/contents/lean-toolchain?ref={project.branch} --jq '.content | @base64d' > lean-toolchain",
                f"For a new package write lakefile.toml with:\n{lakefile}\nUse the actual name from the content repository's lakefile for the require name if it differs from {project.roots[0]}.",
                f"For a new package create the empty root modules and source directories:\n$ mkdir -p {shlex.quote(root)} {shlex.quote(project.completed_dir)}\n$ touch {shlex.quote(root + '.lean')} {shlex.quote(project.completed_dir + '.lean')}\n$ lake update\n$ lake build\nCommit the Lake files and root modules through a human-reviewed PR. Add human-written target statements under the configured roadmap directory; they may contain sorry.",
            ]
        findings.append(Finding(
            "lean." + role, "failed" if lean_format else "unknown" if missing else "ready",
            "Project root uses lakefile.lean; lakefile.toml is required" if lean_format else "Cannot read package files: " + ", ".join(missing) if missing else f"Lean package files are published in {repo}",
            "Published Lean toolchain, TOML Lake package and resolved dependencies", steps=([
                "In the repository checkout, if lakefile.toml is absent run:\n$ lake translate-config toml\nThis writes lakefile.toml and renames the original to lakefile.lean.bak. Review both files, run lake update and lake build, then commit the conversion through human review. If both formats already exist, compare them and retain the reviewed TOML configuration; move the Lean file to an unused backup name so it is no longer active. Only the project root requires TOML: upstream dependencies, including Mathlib, can keep Lean lakefiles.",
            ] if lean_format else setup),
            urls=[f"https://github.com/{repo}"], verify=again, section="lean-and-upstream-dependencies" if role == "content" else "roadmap-lean-package"))
    if repo_info.get("roadmap"):
        repo = project.repos["roadmap"]
        branch = repo_info["roadmap"].get("default_branch", project.branch)
        _, error = gh.read(f"repos/{repo}/contents/.github/workflows/ci.yml?ref={branch}")
        findings.append(Finding(
            "roadmap.workflows", "unknown" if error else "ready",
            error or "Roadmap build workflow installed", "Roadmap targets build in their own Lean package",
            steps=[
                "First publish the content configuration with its selected Tools version and prepare the roadmap Lake package.",
                f"Preview, then install the roadmap CI workflow through a reviewed setup PR:\n$ {_cmd('tauceti-tools', 'doctor', '--conf', project.source, '--fix', 'roadmap.workflows')}\n$ {_cmd('tauceti-tools', 'doctor', '--conf', project.source, '--fix', 'roadmap.workflows', '--apply')}",
                "Merge the setup PR as a human. After its build check has run, open Settings → Rules → Rulesets and require pull-request review, code-owner review and the build check on the roadmap's default branch.",
            ], urls=[f"https://github.com/{repo}/actions"], verify=again, section="roadmaps"))
    return findings


def dependency_findings(project, gh, again):
    """Compare published pins with updater policy, without resolving or executing Lake."""
    def read(repo, revision, name):
        value, error = gh.read(f"repos/{repo}/contents/{name}?ref={revision}")
        if error:
            raise ValueError(f"Cannot read {repo}@{revision}/{name}: {error}")
        return base64.b64decode(value["content"]).decode().strip()

    policy = project.raw.get("upstreams", [])
    if not any(u.get("update") for u in policy):
        return []
    problems = []
    status = "ready"
    try:
        manifest = json.loads(read(project.content, project.branch, "lake-manifest.json"))
        toolchain = read(project.content, project.branch, "lean-toolchain")
        packages = {p["name"]: p for p in manifest["packages"]}
        for upstream in policy:
            if not upstream.get("update"):
                continue
            package = packages.get(upstream["package"])
            if not package or package.get("inherited", False):
                problems.append(f"{upstream['package']} has update = true but is {'inherited' if package else 'absent'} in the published manifest.")
            elif package.get("url", "").rstrip("/").removesuffix(".git") != f"https://github.com/{upstream['repo']}":
                problems.append(f"{upstream['package']}'s published URL differs from configured {upstream['repo']}.")
            elif package.get("inputRev") != upstream.get("branch", "main"):
                problems.append(f"{upstream['package']} has inputRev = {package.get('inputRev')!r}, but its configured update branch is {upstream.get('branch', 'main')!r}. Set branch in its [[upstreams]] table to that nominated inputRev; selecting a different Lake requirement is a human-owned change.")
        from .upstreams import release

        release(toolchain)
        status = "failed" if problems else "ready"
    except (ValueError, KeyError, TypeError, UnicodeError) as error:
        status = "failed" if problems else "unknown"
        problems.append(str(error))
    return [Finding(
        "dependencies.updates", status,
        "\n".join(problems) or "Direct dependency update policy is configured. Lake resolves shared pins; the required build decides compatibility. This check does not resolve newer branch heads or prove a build.",
        "Explicit direct-dependency update policy", role="Human Lean maintainer",
        commands=[_cmd("gh", "repo", "clone", project.content),
                  _cmd("tauceti-tools", "upstreams", "inspect", "--conf", project.source,
                       "--checkout", project.content.split("/")[1])],
        steps=[
            "Use an existing clean content checkout, or clone it with the command below. Compare lakefile.toml, lake-manifest.json and [[upstreams]] in tauceti.toml. Keep the project's existing Lake dependencies; the Mathlib-only starter is not a replacement for a CSLib project.",
            "For each inherited or absent package reported above, set update = false in its existing [[upstreams]] table. Keep overlap and upstream_destination as desired. To update a direct dependency, add its own table with repo = \"OWNER/REPOSITORY\", package = \"LAKE_PACKAGE_NAME\", branch = \"NOMINATED_BRANCH\", update = true, using the exact URL, package name and inputRev in the manifest. Do not make transitive Mathlib an independent update target.",
            "Different upstream Mathlib pins do not prove incompatibility. The updater and bump guard use Lake to resolve the project with its direct dependencies pinned to authorized commits. Existing direct overrides are respected; the required build and reviews decide whether the combination works. Lakefiles remain human-owned.",
            "Run the inspect command to preview the proposed direct pins without executing Lake or changing files. To prepare the resolved manifest, run the same command with update instead of inspect and add --apply, from a clean committed content checkout on Linux with elan and bubblewrap installed. Alternatively open the content repository → Actions → Update configured upstreams → Run workflow. If resolution or the build fails, read its log and adapt the source in the dependency PR while preserving the Lake requirements. To pause bumps, set update = false for every enabled [[upstreams]] entry and merge that configuration change. An installed guard still rejects lakefile edits; there is no approval-based path for changing the dependency policy itself.",
        ], verify=again, section="lean-and-upstream-dependencies")]


def extra_findings(project, gh, again):
    from .github_state_setup import client_token_file, public_configuration

    results = []
    owner = project.content.split("/")[0]
    for name in ("github_state", "bors", "cache"):
        service = project.service(name)
        mode = service.get("mode", "off")
        results.append(
            Finding(
                "services." + name,
                "disabled" if mode == "off" else "pending",
                f"{name}: {mode}",
                "Selected optional integration",
                commands=[
                    _cmd(
                        "tauceti-tools",
                        "deploy",
                        name.replace("_", "-"),
                        "--conf",
                        project.source,
                        "--output",
                        "deployment/" + name,
                    )
                ],
                urls=([service["url"]] if service.get("url") else []),
                steps=(
                    ["This integration is intentionally disabled. Change its mode in the configuration to set it up."]
                    if mode == "off"
                    else [
                        "Follow the deployment section for this selected provider; the deploy command writes local templates and never switches merge authority."
                    ]
                ),
                verify=again,
                section=name.replace("_", "-"),
            )
        )
        runtime = results[-1]
        if mode == "custom" and name == "github_state":
            pin = project.tool("tools")
            arguments = (" --repository " + shlex.quote(project.content) +
                         " --name " + shlex.quote(project.key.replace("__", "-") + "-state") +
                         " --url " + shlex.quote(service.get("url") or "https://YOUR_STATE_DOMAIN") +
                         " --account-id YOUR_CLOUDFLARE_ACCOUNT_ID --credentials-dir " +
                         shlex.quote(str(Path.home() / ".config/tauceti/projects" / project.key / "github_state")))
            runtime.commands = []
            runtime.steps = [
                f"Select [tools.tools] first. Open https://dash.cloudflare.com/, select your own account, and copy its Account ID from the account/Worker overview. Set services.github_state.url in {project.source} to the public HTTPS domain you control. Replace YOUR_CLOUDFLARE_ACCOUNT_ID and any YOUR_STATE_DOMAIN placeholder in the commands below.",
                f"In a new deployment directory, check out the selected Tools revision:\n$ gh repo clone {pin.get('repo', 'TauCetiProject/TauCetiTools')} state-deployment\n$ git -C state-deployment checkout --detach {pin.get('ref', 'PASTE_SELECTED_TOOLS_SHA')}\n$ cd state-deployment/services/github-state\n$ npm ci\n$ npx wrangler login",
                "Prepare the private credentials and App registration form:\n$ python3 scripts/setup.py prepare" + arguments + "\nOpen the local HTML form printed by prepare in a browser signed in as your account's App manager. Click Register dedicated read-only App, confirm the permissions and public installation setting, then Create GitHub App. Copy the code parameter from the resulting redirect URL, then run:\n$ python3 scripts/setup.py exchange" + arguments + "\nPaste that one-use code when the command waits for input and press Enter. The returned App key stays private; do not put the code in a shell argument.",
                "Install that App on the content repository with Only select repositories. Copy NUMBER from the resulting /settings/installations/NUMBER URL. Run these initial-installation commands with that number:\n$ python3 scripts/setup.py inspect" + arguments + " --installation NUMBER\n$ python3 scripts/setup.py deploy" + arguments + " --installation NUMBER\n$ python3 scripts/setup.py bootstrap" + arguments + " --installation NUMBER\nFor later code updates run npx wrangler deploy; do not rerun initial deploy after adding clients because it resets their grants.",
            ]
        elif mode == "custom" and name == "cache":
            runtime.commands = []
            runtime.steps = [
                f"In https://dash.cloudflare.com/ select your account → R2 object storage → Create bucket. Name it {project.key.replace('__', '-')}-cache, choose the intended jurisdiction, and Create bucket. Under its Settings → Custom Domains → Add, enter your cache domain in this account's Cloudflare zone and wait for Active.",
                "R2 overview → Manage API Tokens → Create Account API token. Name it for this project's cache writer; Object Read & Write → Apply to specific buckets only → your new bucket → Create. Save the Access Key ID, Secret Access Key and HTTPS S3 endpoint from the confirmation page.",
                f"In {project.source}, under [services.cache], set name to a unique cache name, artifact_url=PUBLIC_DOMAIN/artifacts, revision_url=PUBLIC_DOMAIN/revisions, artifact_write_url=S3_ENDPOINT/BUCKET/artifacts and revision_write_url=S3_ENDPOINT/BUCKET/revisions. Replace PUBLIC_DOMAIN and S3_ENDPOINT with full HTTPS URLs and BUCKET with the actual bucket name.",
                f"In https://github.com/{project.content}/settings/variables/actions create LAKE_CACHE_ARTIFACT_ENDPOINT_PUBLIC=artifact_url, LAKE_CACHE_REVISION_ENDPOINT_PUBLIC=revision_url, LAKE_CACHE_ARTIFACT_ENDPOINT=artifact_write_url and LAKE_CACHE_REVISION_ENDPOINT=revision_write_url using those actual values. Under Secrets → New repository secret set LAKE_CACHE_KEY to ACCESS_KEY_ID:SECRET_ACCESS_KEY with the returned credentials.",
                "Run a normal post-merge CI build. Inspect its publish-lake-cache upload and public confirmation steps; ordinary PR builds never receive the writer credential.",
            ]
        elif mode != "off" and name == "bors":
            runtime.commands = []
            runtime.steps = [
                f"Choose the downstream runtime's public HTTPS domain and set services.bors.url in {project.source}. Hosted mode supplies Neon only; the Bors process runs in your account.",
                "For Docker, select a released Bors image pinned with @sha256:DIGEST in services.bors.image. Obtain releases at https://github.com/TauCetiProject/bors-ng/releases. For Cloudflare set deployment=cloudflare, account_id to your paying account ID, and select [tools.bors] repo=TauCetiProject/bors-ng with a full commit ref.",
                f"Generate local deployment files in a new directory (choose another --output if it exists):\n$ {_cmd('tauceti-tools', 'deploy', 'bors', '--conf', project.source, '--output', 'deployment/bors')}",
                f"Register a Bors App at https://github.com/settings/apps/new (personal owner) or https://github.com/organizations/{project.content.split('/')[0]}/settings/apps/new (organization owner). Name it {project.content.split('/')[1]}-bors; Homepage: your runtime URL; Callback URL: RUNTIME_URL/auth/github/callback; Webhook URL: RUNTIME_URL/webhook/github; Webhook Active: checked; installation: Any account. Repository permissions: Contents, Pull requests, Issues, Commit statuses: Read and write; Checks, Actions, Variables: Read-only. Subscribe to Pull request, Issue comment, Pull request review, Check run, Check suite, Status and Push. Click Create GitHub App.",
                "On the App General page record the numeric App ID and Client ID; Generate a new client secret, and Generate a private key. Under Install App choose your account → Only select repositories → the content repository → Install. Generate a separate random webhook secret with openssl rand -hex 32 and enter it in the App's Webhook secret field, then Save changes.",
                "For Docker, cd deployment/bors, copy bors.private.env.example to bors.private.env and chmod 600 bors.private.env. Set DATABASE_URL to your private direct PostgreSQL URI; remove its libpq sslmode/channel_binding query suffix because Compose sets DATABASE_USE_SSL=true. Set GITHUB_INTEGRATION_ID=the Bors App ID; GITHUB_INTEGRATION_PEM=base64 PEM (openssl base64 -A -in /PRIVATE/BORS_KEY.pem); GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET=the App OAuth values; GITHUB_WEBHOOK_SECRET=the same random webhook value; SECRET_KEY_BASE=output of openssl rand -base64 64; TAUCETI_REVIEW_APP_ID=the automation App ID. Keep all values in this private file.",
                "For Docker, run docker compose up -d. Point DNS to your host and serve Caddyfile with your HTTPS proxy; block public /internal/* routes. Open the Bors URL, sign in with GitHub and synchronize its repository installation. Install heartbeat.cron's line using crontab -e from the host deployment directory. For Cloudflare, deployment/bors/README.md contains the generated exact resource and deploy commands; its secret upload helper accepts these same private credentials from files.",
                "Check the runtime's /health/ endpoint and complete a pilot staging build with required checks before selecting merge.backend=bors and MERGE_BACKEND=bors. Keep the project's existing merge backend until then.",
            ]
        if mode == "hosted":
            title = f"Infrastructure request: {project.content} ({name})"
            results.append(
                Finding(
                    f"services.{name}.enrollment",
                    "pending",
                    "Hosted access needs operator approval",
                    "An explicit operator grant for this repository",
                    role="Tau Ceti infrastructure operator",
                    urls=["https://github.com/TauCetiProject/TauCetiTools/issues/new"],
                    steps=[
                        f"Open the issue form. Title: {title}. Body: Repository: {project.content}; Service: {name}; Configuration: {public_configuration(project)}; Requested operator: {owner}. Click Submit new issue. Include no credentials.",
                        *(["For Bors, hosted means a separate Neon project only. Operate the Bors runtime in your own account."] if name == "bors" else []),
                        "The operator records approval and supplies scoped credentials privately; do not put credentials in the issue.",
                    ],
                    verify=again,
                    section=name.replace("_", "-"),
                )
            )
            if name == "bors":
                enrollment = results[-1]
                provision = ("tauceti-tools neon provision --conf " + shlex.quote(public_configuration(project)) +
                             " --registry ~/.config/tauceti/neon-projects.json --token-file ~/.config/tauceti/neon-api-key")
                output = "~/.config/tauceti/projects/" + project.key + "/bors/database-url"
                enrollment.steps += [
                    "Service operator: open https://console.neon.tech, select the paying organization → Settings → General information, and copy its Organization ID. Under Settings → API keys create an organization key and save its value privately at ~/.config/tauceti/neon-api-key; chmod 600 that file.",
                    'Service operator: edit ~/.config/tauceti/neon-projects.json, preserving existing entries. Add "' + project.content + '": {"approved":true,"org_id":"ORGANIZATION_ID","region":"aws-us-east-1","pg_version":18,"min_cu":0.25,"max_cu":0.25,"suspend_timeout_seconds":-1,"history_retention_seconds":604800} inside its outer object. Replace ORGANIZATION_ID with the copied ID, confirm the paid capacity/region/retention settings, and chmod 600 the file.',
                    f"Service operator: preview the project, then provision after confirming its settings:\n$ {provision}\n$ {provision} --apply --output {output}\nIf a project is already recorded, use neon inspect with the same arguments. In that project's Neon dashboard choose Connect → Direct connection and retrieve its URI privately; do not create another project.",
                    "Service operator: send only the database URI to the downstream through a private channel. Keep the management key and registry private. The downstream installs that URI as DATABASE_URL in its own Bors runtime; a database grant does not change merge.backend.",
                ]
                registry = Path.home() / ".config/tauceti/neon-projects.json"
                try:
                    grants = json.loads(registry.read_text())
                    grant = grants.get(project.content, {}) if isinstance(grants, dict) else {}
                except (OSError, ValueError):
                    grant = {}
                if isinstance(grant, dict) and grant.get("approved") is True and grant.get("project_id"):
                    enrollment = results[-1]
                    enrollment.status = "ready"
                    enrollment.summary = "Approved Neon project recorded in the private operator registry: " + grant["project_id"]
                    enrollment.urls = ["https://console.neon.tech/app/projects/" + grant["project_id"]]
                    enrollment.commands = [_cmd(
                        "tauceti-tools", "neon", "inspect", "--conf", project.source,
                        "--registry", registry, "--token-file", Path.home() / ".config/tauceti/neon-api-key",
                    )]
                    enrollment.steps = [
                        f"The operator grant is recorded in {registry}. Run the inspect command; if the operator's API key is elsewhere, replace --token-file with its actual private file. No second infrastructure request is needed.",
                        "This records the database grant, not Bors runtime health. The database URI stays private and the downstream still operates its own runtime.",
                    ]
                runtime = next(f for f in results if f.id == "services.bors")
                runtime.summary = "Hosted Neon selected; Bors runtime setup is separate"
                runtime.steps += [
                    f"Set services.bors.url in {project.source} to the public HTTPS URL of the runtime you operate. Hosted means Neon only, not a shared Bors runtime.",
                    "For Docker, set services.bors.image to a released image with @sha256:DIGEST, then run the deploy command to generate Compose, HTTPS proxy and heartbeat files. For Cloudflare, set deployment=cloudflare, account_id and an immutable [tools.bors] repo/ref, then generate that provider's installation commands.",
                    "Keep the current merge.backend until Bors has consumed its real credentials and passed a pilot staging build. Provisioning a database does not switch merge authority.",
                ]
                if (enrollment.status == "ready" and not service.get("url")
                        and project.raw.get("merge", {}).get("backend") != "bors"):
                    runtime.status = "ready"
                    runtime.summary = "Hosted Neon database approved; Bors is not the selected merger"
            elif name == "cache":
                results[-1].steps += [
                    f"Service operator: in https://dash.cloudflare.com/ select the paying account → R2 object storage. Create or select a dedicated bucket for {project.content}. Open its Settings → Custom Domains → Add and enter a domain in that account's Cloudflare zone. Wait for Active; that HTTPS domain serves public reads.",
                    "Service operator: R2 overview → Manage API Tokens → Create Account API token. Name it for this project's cache writer; choose Object Read & Write → Apply to specific buckets only → this project's bucket → Create. Save the Access Key ID, Secret Access Key and S3 endpoint from the confirmation page privately.",
                    "Service operator: supply the downstream with artifact_url=PUBLIC_DOMAIN/artifacts, revision_url=PUBLIC_DOMAIN/revisions, artifact_write_url=S3_ENDPOINT/BUCKET/artifacts and revision_write_url=S3_ENDPOINT/BUCKET/revisions, replacing PUBLIC_DOMAIN with its HTTPS URL, S3_ENDPOINT with the confirmation page's HTTPS endpoint and BUCKET with the selected bucket. Send ACCESS_KEY_ID:SECRET_ACCESS_KEY privately; never put it in the issue.",
                    f"Downstream administrator: enter those four endpoint values under services.cache in {project.source}. In https://github.com/{project.content}/settings/variables/actions, create LAKE_CACHE_ARTIFACT_ENDPOINT_PUBLIC=artifact_url, LAKE_CACHE_REVISION_ENDPOINT_PUBLIC=revision_url, LAKE_CACHE_ARTIFACT_ENDPOINT=artifact_write_url and LAKE_CACHE_REVISION_ENDPOINT=revision_write_url. In Settings → Secrets and variables → Actions → Secrets → New repository secret, Name: LAKE_CACHE_KEY; Secret: the supplied ACCESS_KEY_ID:SECRET_ACCESS_KEY line. Inspect the next post-merge CI run's publish-lake-cache upload and confirmation steps.",
                ]
                runtime.commands = []
                runtime.steps = list(results[-1].steps)
            if name == "bors" and enrollment.status != "ready":
                runtime.steps += enrollment.steps
        if name == "github_state" and mode != "off":
            token_file = client_token_file(project)
            service_finding = next(f for f in results if f.id == "services.github_state")
            if mode == "hosted":
                enrollment = results[-1]
                operator = "~/.config/tauceti-github-state/client-tokens.json"
                request_conf = (project.source if project.source.startswith("https://") else
                                os.path.relpath(Path(project.source).expanduser()))
                request_command = _cmd("tauceti-tools", "github-state", "request", "--conf", request_conf,
                                       "--installation", "INSTALLATION_ID")
                initial = _cmd("tauceti-tools", "github-state", "initial-grants")
                bootstrap = _cmd("tauceti-tools", "github-state", "bootstrap", "--conf", public_configuration(project))
                info, _ = gh.read(f"repos/{project.content}")
                repository_id = str((info or {}).get("id", "REPOSITORY_ID"))
                enrollment.summary = "Install the read-only App and request hosted access."
                enrollment.urls = []
                repository_lookup = (" If REPOSITORY_ID is shown below, obtain it with:\n$ " +
                                     _cmd("gh", "api", f"repos/{project.content}", "--jq", ".id")
                                     if repository_id == "REPOSITORY_ID" else "")
                enrollment.steps = [
                    f"Open https://github.com/apps/tauceti-github-state/installations/new. Choose {owner} → Only select repositories → {project.content.split('/')[1]} → Install. Copy the number from the resulting /settings/installations/NUMBER URL.",
                    f"Create your access request, replacing INSTALLATION_ID with that number:\n$ {request_command}\nReview the saved request, then run the gh issue create command it prints. Only the token hash is shared; the raw token stays on your machine.",
                    f'Service operator: in https://dash.cloudflare.com/ open Workers & Pages → tauceti-github-state → Settings → Variables and Secrets. Append {{"id":"{repository_id}","name":"{project.content}","installation":"INSTALLATION_ID"}} to ALLOWED_REPOSITORIES, using the installation ID from the request. Keep all existing entries and deploy the change.{repository_lookup}',
                    f"Service operator: first restore the complete grant array from your private backup at {operator}. Cloudflare cannot recover the old secret. Only if the service has no clients beyond its original setup, recover that original grant into a new file with:\n$ {initial}\nAppend the request's grant object to the complete array. Preserve its private permissions:\n$ chmod 600 {operator}\nEdit the CLIENT_TOKENS secret in the same panel, paste the entire array, and deploy.",
                    f"Service operator: initialize the approved repository:\n$ {bootstrap}\nFor later code updates use npx wrangler deploy. The initial scripts/setup.py deploy command resets the grants and must not be rerun after enrollment.",
                    "Once the operator confirms access, run tauceti doctor again. The token's repository scope is enforced by the service; its raw value stays on your machine.",
                ]
                service_finding.commands = []
                # Each check is actionable on its own: --check only selects findings.
                enrollment.steps.append(
                    f"For workers pass --github-state-token-file {shlex.quote(str(token_file))}; the service URL comes from this project configuration."
                )
                service_finding.steps = list(enrollment.steps)
            else:
                service_finding.commands.append(_cmd("chmod", "600", token_file))
                service_finding.steps += [
                    f"Save your service's client token at {token_file}, or set TAUCETI_GITHUB_STATE_TOKEN_FILE to its actual private file. The deployment helper generates this token; it is not a GitHub token. Re-run this check for an authenticated, read-only health request.",
                    f"For workers pass --github-state-token-file {shlex.quote(str(token_file))}; the service URL comes from this project configuration.",
                ]
            if not service.get("url"):
                service_finding.status = "failed"
                service_finding.summary = "Set services.github_state.url to the operator's actual HTTPS endpoint"
                service_finding.steps.insert(0, f"Edit {project.source}: set services.github_state.url = \"https://github-state.taucetiproject.org\" for Tau Ceti's hosted service." if mode == "hosted" else f"Edit {project.source}: set services.github_state.url to your service's public HTTPS endpoint, then rerun doctor.")
            elif not token_file.is_file():
                service_finding.summary = f"Client token file missing: {token_file}"
            else:
                try:
                    request = urllib.request.Request(
                        service["url"].rstrip("/") + "/v1/health?" + urllib.parse.urlencode({"repository": project.content}),
                        headers={"Authorization": "Bearer " + token_file.read_text().strip(),
                                 "User-Agent": "TauCetiWorker/1.0"},
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        health = json.load(response)["state"]
                    identity = health.get("repository", {}).get("name")
                    writable = health.get("storage", {}).get("mode") == "writable"
                    ready = identity == project.content and writable and bool(health.get("bootstrap_inventory"))
                    service_finding.status = "ready" if ready else "pending"
                    service_finding.summary = "Repository-scoped health and bootstrap verified" if ready else "Authenticated; repository bootstrap/storage is not ready"
                    if mode == "hosted":
                        enrollment.status = "ready"
                        enrollment.summary = "Repository-scoped operator grant verified by authenticated health"
                        if not ready:
                            service_finding.steps = [
                                "Hosted access is already approved; keep the existing client token and grant. Initialization or writable storage is still pending.",
                                enrollment.steps[4],
                                "Allow the initialization to finish, then rerun doctor. If storage remains unavailable, the service operator must restore its writable backend using the service recovery runbook; another enrollment request is unnecessary.",
                            ]
                except (OSError, ValueError, KeyError) as error:
                    service_finding.status = "failed"
                    service_finding.summary = f"Health request failed (HTTP {error.code})" if isinstance(error, urllib.error.HTTPError) else "Health request unavailable or malformed; inspect the endpoint and scoped token"
    workflows = {
        "archives": ("data", []),
        "ci_archive": ("ci", ["collect.yml", "publish.yml"]),
        "claims": ("content", ["claims-access.yml"]),
        "docs": ("content", ["api-docs.yml", "pages.yml"] if project.legacy else ["api-docs.yml"]),
        "progress": ("roadmap", ["ci.yml", "progress-merge.yml"]),
        "zulip": ("content", ["zulip-healthcheck.yml"]),
        "fleet": ("content", ["pick-runner.yml"]),
        "security_audit": ("content", ["nightly-verify.yml"]),
    }
    for name, (role, paths) in workflows.items():
        enabled = project.feature(name)
        if not enabled:
            results.append(Finding("features." + name, "disabled", name + ": off", "Optional feature disabled"))
            continue
        repository = project.repos.get(role)
        missing, inaccessible = [], []
        for path in paths if repository else []:
            _, error = gh.read(f"repos/{repository}/contents/.github/workflows/{path}")
            if error:
                inaccessible.append(path + ": " + error)
        if not repository:
            proposed = project.content + {"data": "Data", "ci": "CI", "claims": "-claims"}.get(role, "Roadmap")
            missing.append(
                f"Set repositories.{role} = \"{proposed}\" in {project.source}, or use the actual existing repository. Check access with gh repo view {proposed}. Only if the owner confirms it is absent, create it:\n$ gh repo create {proposed} --public --add-readme"
            )
        repair_id = "workflows" if role == "content" else role + ".workflows"
        commands = [
            _cmd("tauceti-tools", "doctor", "--conf", project.source, "--fix", repair_id),
            _cmd("tauceti-tools", "doctor", "--conf", project.source, "--fix", repair_id, "--apply"),
        ]
        steps = missing + [
            "Preview the generated callers, run the apply command to open the setup PR, inspect its diff and merge through human review."
        ]
        if name == "docs":
            commands += [
                _cmd("gh", "api", "repos/" + project.content + "/pages"),
                _cmd("gh", "api", "--method", "POST", "repos/" + project.content + "/pages", "-f", "build_type=workflow"),
                _cmd("gh", "workflow", "run", "api-docs.yml", "--repo", project.content),
            ]
            steps += [
                "Open Settings → Pages → Build and deployment → Source: GitHub Actions. Set publication.docs_url to the published /docs URL. Run api-docs.yml from Actions → Run workflow after the content build succeeds.",
                "Use the POST command only if Pages is not created; for an existing site use gh api --method PUT with the same build_type=workflow. Select a full [tools.docgen] repository/ref compatible with lean-toolchain. Downstream API docs deploy directly; they do not require Tau Ceti's web/ or docbuild/ directories.",
            ]
        if name == "zulip":
            steps += [
                "Set publication.zulip_site, zulip_channel and zulip_topic to the intended destination. At the site's /#settings/your-bots create a Generic bot, subscribe it to that channel, and store its email and API key as ZULIP_EMAIL and ZULIP_API_KEY in the content and roadmap repositories that publish announcements.",
                "Run the existing zulip-healthcheck.yml workflow from Actions → Run workflow. Inspect its credential-consuming step; it must succeed without skipping.",
            ]
        if name == "fleet":
            steps += [
                "Register your runners in Settings → Actions → Runners → New self-hosted runner. Use the OS/architecture shown there, run the generated ./config.sh command, assign the runner labels used by your picker and start its service. Keep the GitHub-hosted picker until your own fleet picker is installed; Tools supplies no access to Tau Ceti's private fleet."
            ]
        if name == "archives":
            steps += [
                "Install the Data repository setup first. Extend the automation App installation to that Data repository: Installed GitHub Apps → the project's automation App → Configure → Repository access → Only select repositories → add Data alongside content → Save.",
                "Let a normal review worker finish a review. Its archive publisher writes the canonical review records to Data; inspect the worker's archive/publish result and the Data repository's default branch. A repository existing by itself does not establish that publication worked.",
            ]
        if name == "ci_archive":
            steps += [
                "Create the collector App and upload COLLECTOR_APP_ID and COLLECTOR_APP_PRIVATE_KEY using the CI credential tasks. Merge the CI setup PR first, then run collection:\n$ " + _cmd("gh", "workflow", "run", "collect.yml", "--repo", repository or proposed),
                "Open the CI repository's Actions → collect run. The App-token step and Collect must succeed; a GITHUB_TOKEN fallback does not prove the collector App works. Keep its collected canonical records; publication derives the archive database from those records.",
            ]
        if name == "progress":
            commands.append(_cmd("tauceti", "--conf", project.source, "work", "--only", "progress"))
            steps += [
                "Publish the content API docs first, then run a progress worker. Its generated report PR triggers progress-merge.yml; inspect Mint the App token and Land the validated tree. A successful run which skipped the merge is not functional evidence.",
                f"If Mint the App token fails, open https://github.com/settings/installations (personal account) or https://github.com/organizations/{owner}/settings/installations (organization) → the project's automation App → Configure. Under Repository access keep Only select repositories, add {repository.split('/')[1]} alongside {project.content.split('/')[1]}, then Save. Check the roadmap SYNC_BOT_APP_ID and SYNC_BOT_APP_PRIVATE_KEY findings for the exact credential commands.",
                f"Also open https://github.com/{repository}/settings/rules and create or edit the roadmap's main-branch ruleset, preserving required PRs, code-owner review and the build check. Add the automation App to Bypass list with Always allow. The progress validator independently checks build, paths and exact revisions before its direct ref update. If a classic protection rule at https://github.com/{repository}/settings/branches also covers main, activate the equivalent ruleset before removing the classic rule and relying on the App bypass.",
                f"To retry an existing report without generating new prose: gh workflow run progress-merge.yml --repo {repository} -f pr=REPORT_NUMBER, replacing REPORT_NUMBER with its PR number. In the UI open Actions → progress-merge → Run workflow, enter that PR number, then Run workflow.",
            ]
        status = "unknown" if missing or inaccessible else "pending"
        summary = "; ".join(missing + inaccessible) or "Installed; functional consumption still needs a normal workflow run"
        required_steps = {
            "docs": ("api-docs.yml", ("Generate API documentation", "Deploy to GitHub Pages")),
            "progress": ("progress-merge.yml", ("Mint the App token", "Land the validated tree")),
        }.get(name)
        failed_steps = []
        observed_run = None
        if required_steps and not missing and not inaccessible and (name != "docs" or not project.legacy):
            workflow, labels = required_steps
            runs, _ = gh.read(f"repos/{repository}/actions/workflows/{workflow}/runs?per_page=5", paginate=False)
            for run in _items(runs, "workflow_runs"):
                jobs, error = gh.read(f"repos/{repository}/actions/runs/{run['id']}/jobs")
                consumed = [step for job in _items(jobs, "jobs") for step in job.get("steps", [])]
                if (name == "progress" and run.get("conclusion") not in ("failure", "timed_out", "action_required")
                        and not all(any(label in step.get("name", "") for step in consumed) for label in labels)):
                    continue
                observed_run = run
                failed_steps = [s.get("name", "") for s in consumed if s.get("conclusion") == "failure"]
                if run.get("conclusion") == "success" and not error and all(
                    any(label in step.get("name", "") and step.get("conclusion") == "success" for step in consumed)
                    for label in labels
                ):
                    status, summary = "ready", f"Run {run['id']} succeeded and its consuming steps ran successfully"
                elif run.get("conclusion") in ("failure", "timed_out", "action_required"):
                    status, summary = "failed", f"Run {run['id']} failed; inspect the consuming steps"
                commands.append(_cmd("gh", "run", "view", str(run["id"]), "--repo", repository, "--log"))
                break
        if status == "failed" and observed_run and not missing and not inaccessible:
            commands = []
            steps = ["Open the failed run, fix the reported error, then retry the workflow."]
            urls = [observed_run.get("html_url", f"https://github.com/{repository}/actions/runs/{observed_run['id']}")]
            if name == "progress":
                pulls, _ = gh.read(f"repos/{repository}/pulls?state=open&per_page=100")
                reports = [p for page in _pages(pulls or []) for p in (page if isinstance(page, list) else [page])
                           if p.get("head", {}).get("ref", "").startswith("progress/")]
                retry = (_cmd("gh", "workflow", "run", "progress-merge.yml", "--repo", repository,
                              "-f", "pr=" + str(reports[0]["number"])) if reports else
                         _cmd("tauceti", "--conf", project.source, "work", "--only", "progress"))
                if "Mint the App token" in failed_steps:
                    from .deployment import app_manifest

                    info, _ = gh.read(f"repos/{project.content}")
                    organization = (info or {}).get("owner", {}).get("type") == "Organization"
                    installations = (f"https://github.com/organizations/{owner}/settings/installations" if organization
                                     else "https://github.com/settings/installations")
                    app = app_manifest(project, "automation")["name"]
                    summary = f"Could not create an automation App token for {repository}."
                    steps = [
                        f"Open {installations}. Choose {app} → Configure → Repository access → Only select repositories. Add {repository.split('/')[1]} alongside {project.content.split('/')[1]}, then Save.",
                        f"Retry the existing report after saving App access:\n$ {retry}" if reports else
                        f"Create a progress report after saving App access:\n$ {retry}",
                    ]
                elif "Land the validated tree" in failed_steps:
                    summary = "The report validated, but updating the roadmap branch failed."
                    steps = [
                        f"Open https://github.com/{repository}/settings/rules. In the main-branch ruleset, preserve required PRs, code-owner review and build. Add the automation App to Bypass list → Always allow.",
                        f"If a classic rule also covers main, first activate the equivalent ruleset, then remove that classic rule at https://github.com/{repository}/settings/branches so the App bypass can apply.",
                        f"Retry the report:\n$ {retry}",
                    ]
                else:
                    steps.append(f"Retry after fixing the error:\n$ {retry}")
            else:
                commands = [_cmd("gh", "workflow", "run", required_steps[0], "--repo", repository)]
        else:
            urls = [f"https://github.com/{repository or project.content}/actions"]
        results.append(
            Finding(
                "features." + name,
                status,
                summary,
                "Feature workflows installed and their actual consumers succeed",
                commands=commands,
                urls=urls,
                steps=steps,
                verify=again,
                section="extras",
                repair=repair_id,
            )
        )
    return results


def auxiliary_findings(project, gh, again):
    from .credentials import app_setup, credential_setup

    results = []
    content_info, _ = gh.read(f"repos/{project.content}")
    organization = (content_info or {}).get("owner", {}).get("type") == "Organization"
    for role, feature, names in (
        ("ci", "ci_archive", ("COLLECTOR_APP_ID", "COLLECTOR_APP_PRIVATE_KEY")),
        ("roadmap", "progress", ("SYNC_BOT_APP_PRIVATE_KEY",)),
        ("roadmap", "zulip", ("ZULIP_EMAIL", "ZULIP_API_KEY")),
    ):
        repo = project.repos.get(role)
        if not repo or not project.feature(feature) or feature == "zulip" and not project.feature("progress"):
            continue
        value, error = gh.read(f"repos/{repo}/actions/secrets")
        present = {row["name"] for row in _items(value, "secrets")}
        for name in names:
            path = (
                Path.home()
                / ".config/tauceti/projects"
                / project.key
                / ("collector" if role == "ci" else "zulip" if feature == "zulip" else "automation")
                / ("APP_PRIVATE_KEY" if feature == "progress" else name)
            )
            if feature == "zulip":
                urls, steps = credential_setup(project, name, repo, path, organization=organization)
            else:
                urls, steps = app_setup(project, "collector" if role == "ci" else "automation", organization=organization)
                urls.append(f"https://github.com/{repo}/settings/secrets/actions")
                steps += [f"Upload the existing private file {path} using the command below. In the UI: Settings → Secrets and variables → Actions → Secrets → New repository secret; Name: {name}; Secret: that file's contents; Add secret."]
            results.append(
                Finding(
                    f"secrets.{role}.{name}",
                    "ready" if name in present else "unknown" if error else "failed",
                    name + (": metadata present; consumer unverified" if name in present else ": unavailable"),
                    "Credential in the repository that consumes it",
                    commands=[f"{_cmd('gh', 'secret', 'set', name, '--repo', repo)} < {shlex.quote(str(path))}"],
                    urls=urls,
                    steps=steps,
                    verify=again,
                    section="apps-and-credentials",
                )
            )
        if role == "roadmap" and feature == "progress":
            results[-1].urls.append("https://github.com/settings/installations")
            results[-1].steps += [
                f"Open Installed GitHub Apps → the project's existing automation App → Configure. Under Repository access choose Only select repositories, add {repo.split('/')[1]} alongside {project.content.split('/')[1]}, and Save. Keep the existing installation and private key.",
                f"Upload the existing automation PEM to {repo} as SYNC_BOT_APP_PRIVATE_KEY; no second automation App is needed.",
            ]
            value, error = gh.read(f"repos/{repo}/actions/variables/SYNC_BOT_APP_ID")
            results.append(
                Finding(
                    "variables.roadmap.SYNC_BOT_APP_ID",
                    "unknown" if error else "ready" if str(value.get("value", "")).isdecimal() else "failed",
                    error or "Roadmap App ID must be the automation App's numeric ID",
                    "Numeric ID of the roadmap merge App",
                    commands=[
                        _cmd(
                            "gh",
                            "variable",
                            "set",
                            "SYNC_BOT_APP_ID",
                            "--repo",
                            repo,
                            "--body",
                            "ENTER_AUTOMATION_APP_ID",
                        )
                    ],
                    urls=[f"https://github.com/{repo}/settings/variables/actions"],
                    steps=[
                        "Replace ENTER_AUTOMATION_APP_ID with the numeric ID from the automation App's General page. Under repository Rules → Rulesets, add this App as the intended report merge bypass actor; install only on this project's repositories."
                    ],
                    verify=again,
                    section="apps-and-credentials",
                )
            )
    if project.feature("claims") and project.repos.get("claims"):
        repo = project.repos["claims"]
        value, error = gh.read(f"repos/{repo}/actions/permissions")
        results.append(
            Finding(
                "claims.actions",
                "unknown" if error else "failed" if value.get("enabled") else "ready",
                error or f"Actions enabled: {value.get('enabled')}",
                "Actions disabled on the claims repository",
                commands=[
                    _cmd("gh", "api", "--method", "PUT", f"repos/{repo}/actions/permissions", "-F", "enabled=false")
                ],
                urls=[f"https://github.com/{repo}/settings/actions"],
                steps=["In Actions → General → Actions permissions select Disable actions, then Save."],
                verify=again,
                section="claims",
                repair="claims.actions",
            )
        )
    return results


def repair(project: Project, check: str, *, apply=False):
    if check == "claims.actions":
        command = [
            "gh",
            "api",
            "--method",
            "PUT",
            f"repos/{project.repos['claims']}/actions/permissions",
            "-F",
            "enabled=false",
        ]
        print(_cmd(*command))
        if apply:
            subprocess.run(command, check=True)
        return
    if check == "actions":
        command = ["gh", "api", "--method", "PUT", f"repos/{project.content}/actions/permissions", "-F", "enabled=true"]
        print(_cmd(*command))
        if apply:
            subprocess.run(command, check=True)
        return
    if check == "workflows" or check.startswith("files."):
        from .templates import configuration_pr

        configuration_pr(project, apply=apply)
        return
    if check in ("roadmap.workflows", "data.workflows", "ci.workflows"):
        from .templates import configuration_pr

        configuration_pr(project, apply=apply, role=check.split(".")[0])
        return
    raise ValueError(f"No automatic repair for {check}. Use its printed manual commands and UI instructions.")


def _label(check):
    names = {
        "services.github_state": "GitHub state",
        "services.github_state.enrollment": "GitHub state",
        "services.bors": "Bors",
        "services.bors.enrollment": "Bors",
        "services.cache": "Build cache",
        "services.cache.enrollment": "Build cache",
        "features.progress": "Progress reports",
        "features.docs": "API documentation",
        "features.archives": "Archives",
        "features.ci_archive": "CI archives",
        "features.claims": "Claims",
        "features.zulip": "Zulip announcements",
        "features.fleet": "Runner fleet",
        "features.security_audit": "Security audit",
        "secrets.APP_ID": "Automation App credentials",
        "secrets.APP_PRIVATE_KEY": "Automation App credentials",
        "evidence.pr-build": "Content build",
        "evidence.auto-merge": "Automatic merging",
        "rules.content": "Content branch protection",
        "merge.direct": "Direct merging",
        "actions.enabled": "GitHub Actions",
        "github.auth": "GitHub access",
        "configuration.ownership": "Human configuration ownership",
        "lean.content": "Content Lean package",
        "lean.roadmap": "Roadmap Lean package",
        "dependencies.updates": "Automatic dependency updates",
        "roadmap.workflows": "Roadmap build workflow",
    }
    if check in names:
        return names[check]
    if check.startswith("files."):
        return "Project files and workflows"
    if check.startswith("repositories."):
        return check.split(".")[1].capitalize() + " repository"
    if check.startswith("pins."):
        return check.split(".")[1].capitalize() + " version"
    return check.replace(".", " / ").replace("_", " ")


def _paragraph(text, indent="  "):
    print(textwrap.fill(text, width=90, initial_indent=indent, subsequent_indent=indent,
                        break_long_words=False, break_on_hyphens=False))


def _render_tasks(findings):
    groups = {}
    for item in findings:
        groups.setdefault(_label(item.id), []).append(item)
    for title, items in groups.items():
        print("\n" + title)
        print("  Checks: " + ", ".join(item.id for item in items))
        for summary in dict.fromkeys(item.summary for item in items):
            for line in summary.splitlines():
                _paragraph(line)
        if all(item.status in ("ready", "disabled") for item in items):
            continue
        for url in dict.fromkeys(url for item in items for url in item.urls):
            print("  Open: " + url)
        steps = list(dict.fromkeys(step for item in items for step in item.steps))
        for index, step in enumerate(steps, 1):
            lines = step.splitlines()
            prefix = f"  {index}. "
            print(textwrap.fill(lines[0], width=90, initial_indent=prefix,
                                subsequent_indent=" " * len(prefix), break_long_words=False,
                                break_on_hyphens=False))
            for line in lines[1:]:
                if line.startswith("$ "):
                    print("     " + line)
                elif line.startswith(("[", "name =", "repo =", "ref =", "git =", "rev =", "defaultTargets =", "platformIndependent =", "globs =", "leanOptions =", "root =")):
                    print("     " + line)
                else:
                    _paragraph(line, "     ")
        for command in dict.fromkeys(command for item in items for command in item.commands):
            print("  $ " + command)
        print("  Guide: " + DOCS + "#" + items[0].section)


def render(findings, *, json_output=False, show_ready=False, project=None):
    if json_output:
        print(json.dumps([item.json() for item in findings], indent=2))
        return
    if project:
        print("Project: " + project.content)
        print("Config: " + project.source + "\n")
    core = [f for f in findings if not f.optional]
    extras = [f for f in findings if f.optional]
    def unresolved(rows):
        return [f for f in rows if f.status not in ("ready", "disabled")]
    if core:
        passed = sum(f.status == "ready" for f in core)
        if unresolved(core):
            print(f"Core setup: {len(unresolved(core))} checks need attention; {passed} checks passed.")
        else:
            print(f"Core setup: ready ({passed} checks passed).")
        _render_tasks(core if show_ready else unresolved(core))
    if extras:
        print()
        _paragraph("The remaining items are optional extras: you can omit these from the `tauceti doctor` report using the `--no-extras` flag.", "")
        ready = [_label(f.id) for f in extras if f.status == "ready" and
                 (f.id.startswith("features.") or f.id == "services.github_state")]
        if any(f.id == "services.bors.enrollment" and f.status == "ready" for f in extras):
            ready.append("Neon database grant")
        if ready:
            _paragraph("Ready: " + ", ".join(ready) + ".", "")
        disabled = [_label(f.id) for f in extras if f.status == "disabled"]
        if disabled:
            _paragraph("Disabled: " + ", ".join(disabled) + ".", "")
        _render_tasks(extras if show_ready else unresolved(extras))
        if not unresolved(extras):
            print("All enabled extras are ready.")
    if project and unresolved(findings):
        print("\nAfter making changes, run:")
        print("  $ " + _cmd("tauceti-tools", "doctor", "--conf", project.source))
