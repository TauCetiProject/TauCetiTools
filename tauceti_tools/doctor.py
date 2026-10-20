"""Repository diagnostics and repeatable repairs. Checking never writes remotely."""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
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
    gh = github or GitHub()
    findings = []
    conf = project.source
    again = _cmd("tauceti-tools", "doctor", "--conf", conf)
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
                    commands=commands + [_cmd("gh", "repo", "create", repo, "--public", "--add-readme")],
                    urls=[f"https://github.com/{repo}", "https://github.com/new"],
                    steps=[
                        "Sign in as an organization owner or repository administrator.",
                        "First check repository access with gh repo view. Create the repository only if the owner confirms it is absent.",
                        f"For creation choose Owner: {repo.split('/')[0]}; Repository name: {repo.split('/')[1]}; Visibility: Public; initialize a README.",
                    ],
                    verify=_cmd("gh", "repo", "view", repo, "--json", "nameWithOwner,defaultBranchRef"),
                    optional=role not in ("content", "roadmap"),
                )
            )
            continue
        findings.append(Finding(f"repositories.{role}", "ready", repo, "Repository accessible",
                                optional=role not in ("content", "roadmap")))
        if data.get("archived") or data.get("disabled"):
            findings.append(
                Finding(
                    f"repositories.{role}.writable",
                    "failed",
                    "Repository archived or disabled",
                    "Active repository",
                    commands=[_cmd("gh", "repo", "unarchive", repo)],
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
                    f"Put the selected repository and full SHA in [tools.{tool}], then regenerate the workflow callers with the workflows repair. Review and generator versions must agree with their corresponding merge workflows."
                ],
                verify=again,
                section="versions",
                optional=tool in ("progress", "docgen"),
            )
        )

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
                    commands=[_cmd("tauceti-tools", "doctor", "--conf", conf, "--fix", "workflows", "--apply")],
                    urls=[f"https://github.com/{repo}/tree/{project.branch}/.github"],
                    steps=[
                        "Install the pinned project templates in a local checkout, inspect the diff, and open the configuration PR. Merge through the normal human review process."
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
                    f"Edit the existing protection rule for {project.branch}." if classic else f"Name: Tau Ceti content. Enforcement: Active. Target branch: {project.branch}.",
                    "Enable Require a pull request before merging; require code-owner review for human-owned configuration. Add required status checks build, bump-guard and scope from their expected GitHub Actions producer.",
                    "For github-queue enable Require merge queue. Keep human-owned changes subject to code-owner review. Set the automation App's bypass, if needed, to pull requests only; never grant content authors unconditional bypass.",
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
                        "For MERGE_BACKEND, change only after its normal pilot build succeeds and the corresponding merge rules are configured.",
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
            from .deployment import app_manifest
            from urllib.parse import urlencode

            manifest = app_manifest(project, app_role)
            owner = repo.split("/")[0]
            settings = (f"https://github.com/organizations/{owner}/settings/apps/new"
                        if repo_info["content"]["owner"]["type"] == "Organization"
                        else "https://github.com/settings/apps/new")
            registration = settings + "?" + urlencode({
                "name": manifest["name"], "url": manifest["url"], "public": "true",
                "webhook_active": "false", **manifest["default_permissions"]})
            findings.append(
                Finding(
                    "secrets." + name,
                    "ready" if present else "unknown" if errors else "failed",
                    f"{name}: present, not functionally verified"
                    if present
                    else "; ".join(errors) or f"{name} unavailable",
                    "Selected feature credential available to its trusted consuming workflow",
                    commands=[f"{_cmd('gh', 'secret', 'set', name, '--repo', repo)} < {shlex.quote(str(secret_file))}"],
                    urls=[
                        f"https://github.com/{repo}/settings/secrets/actions",
                        registration,
                    ],
                    steps=[
                        f"For App credentials, open the prefilled registration URL. Name: {manifest['name']}; Homepage: {manifest['url']}; Webhook Active: unchecked; installation: Any account. Keep the prefilled repository permissions, then click Create GitHub App. For provider/bot credentials use the source named in the inventory.",
                        f"Record the numeric App ID. Click Generate a private key. Under Install App choose {owner}, Only select repositories, and {'the configured claims repository' if app_role == 'claims' else repo.split('/')[1]}; click Install. Install on the roadmap too only if progress is enabled.",
                        f"Save the {'numeric App ID' if name.endswith('_ID') else 'downloaded PEM key' if name.endswith('_PRIVATE_KEY') else 'provider or bot credential'} in {secret_file}; chmod 600 that file.",
                        f"In Settings → Secrets and variables → Actions → New repository secret, Name: {name}; Secret: the contents of that file.",
                        "Organization secrets must include this repository in their access policy. A same-named environment secret can override the repository value.",
                    ],
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


def extra_findings(project, gh, again):
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
        if mode == "hosted":
            title = f"Infrastructure request: {project.content} ({name})"
            url = "https://github.com/TauCetiProject/TauCetiTools/issues/new?" + __import__(
                "urllib.parse", fromlist=["urlencode"]
            ).urlencode(
                {
                    "title": title,
                    "body": f"Repository: {project.content}\nService: {name}\nConfiguration: {project.source}\nRequested operator: {owner}\nPlease approve this repository and provide scoped setup instructions. No credentials are included.",
                }
            )
            results.append(
                Finding(
                    f"services.{name}.enrollment",
                    "pending",
                    "Hosted access needs operator approval",
                    "An explicit operator grant for this repository",
                    role="Tau Ceti infrastructure operator",
                    urls=[url],
                    steps=[
                        "Open the prefilled infrastructure request, confirm the repository and configuration URL, and submit it.",
                        *(["For Bors, hosted means a separate Neon project only. Operate the Bors runtime in your own account."] if name == "bors" else []),
                        "The operator records approval and supplies scoped credentials privately; do not put credentials in the issue.",
                    ],
                    verify=again,
                    section=name.replace("_", "-"),
                )
            )
            if name == "bors":
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
        if name == "github_state" and mode != "off":
            token_file = Path(os.environ.get(
                "TAUCETI_GITHUB_STATE_TOKEN_FILE",
                str(Path.home() / ".config/tauceti/projects" / project.key / "github_state/client-token"),
            )).expanduser()
            service_finding = next(f for f in results if f.id == "services.github_state")
            if mode == "hosted":
                enrollment = results[-1]
                enrollment.urls.append("https://github.com/apps/tauceti-github-state/installations/new")
                enrollment.urls += ["https://github.com/settings/installations",
                                    f"https://github.com/organizations/{owner}/settings/installations",
                                    "https://dash.cloudflare.com/"]
                grant_file = token_file.with_name("client-grant.json")
                generate = _cmd("python3", "-c", (
                    "import hashlib, json, os, secrets\nfrom pathlib import Path\n"
                    f"p = Path({str(token_file)!r})\n"
                    "p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)\np.parent.chmod(0o700)\n"
                    "if not p.exists():\n"
                    "    with os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:\n"
                    "        output.write(secrets.token_urlsafe(48) + '\\n')\n"
                    "p.chmod(0o600)\n"
                    f"grant = {{'hash': hashlib.sha256(p.read_text().strip().encode()).hexdigest(), 'repositories': [{project.content!r}]}}\n"
                    f"g = Path({str(grant_file)!r})\ng.write_text(json.dumps(grant) + '\\n')\ng.chmod(0o600)\n"
                    "print(json.dumps(grant))"
                ))
                operator_token = Path.home() / ".config/tauceti-github-state/admin-token"
                bootstrap = _cmd("python3", "-c", (
                    "import json,urllib.request; from pathlib import Path; "
                    f"r=urllib.request.Request({(service.get('url', 'https://github-state.taucetiproject.org').rstrip('/') + '/admin/bootstrap')!r}, "
                    f"data=json.dumps({{'repository':{project.content!r}}}).encode(), "
                    f"headers={{'Authorization':'Bearer '+Path({str(operator_token)!r}).read_text().strip(), "
                    "'Content-Type':'application/json','User-Agent':'TauCetiWorker/1.0'}); "
                    "response=urllib.request.urlopen(r,timeout=30); print('Bootstrap HTTP',response.status)"
                ))
                enrollment.steps += [
                    f"Open the App installation link, choose {owner}, select Only select repositories → {project.content.split('/')[1]}, then Install. Installing the App and receiving a service grant are separate steps.",
                    "The client token is a random bearer secret generated by the service operator, not a GitHub PAT or App installation token. The service scopes it by looking up its SHA-256 hash in CLIENT_TOKENS and checking the repositories list. The operator's admin token does not provide this client grant.",
                    f"Operator: generate the private token and its grant on your machine; an existing token is reused. This prints only the hash and repository scope, and saves the grant at {grant_file}:\n    $ {generate}",
                    f"Operator: get the repository ID:\n    $ {_cmd('gh', 'api', 'repos/' + project.content, '--jq', '.id')}\n    Open the account's Installed GitHub Apps → tauceti-github-state → Configure. Record INSTALLATION_ID from the /settings/installations/NUMBER URL; it must be the installation on {owner}.",
                    f'Operator: open Cloudflare → Workers & Pages → tauceti-github-state → Settings → Variables and Secrets. Edit ALLOWED_REPOSITORIES, keeping all existing entries, and append {{"id":"REPOSITORY_ID","name":"{project.content}","installation":"INSTALLATION_ID"}}. Replace REPOSITORY_ID and INSTALLATION_ID with the numbers above, keeping them as JSON strings. Deploy the change; also preserve this entry in the operator\'s deployment configuration.',
                    "Operator: in the same Variables and Secrets panel edit the Secret named CLIENT_TOKENS. Start from your privately retained complete JSON array of existing grants, append the object printed by the generation command (or reuse it if already present), and deploy the complete array. Keep the updated array privately for future enrollments. Cloudflare cannot reveal an existing secret's value: if that complete array is unavailable, recover it from the operator's private backup before proceeding; replacing it with this one grant would revoke other clients.",
                    f"Operator: after both settings are deployed, initialize this repository using the existing private admin-token file (adjust its path if stored elsewhere):\n    $ {bootstrap}\n    Expected: Bootstrap HTTP 200. Re-run doctor until the repository's bootstrap inventory and writable storage are ready.",
                    f"Operator: transfer only the raw client-token file to the downstream administrator using a private channel. Downstream administrator: download it as {Path.home() / 'Downloads/client-token'}, then install it with:\n    $ {_cmd('install', '-d', '-m', '700', token_file.parent)}\n    $ {_cmd('install', '-m', '600', Path.home() / 'Downloads/client-token', token_file)}\n    Re-run doctor. Never send the admin token or App private key, and never paste the raw client token into an issue or project TOML.",
                ]
            service_finding.commands.append(_cmd("chmod", "600", token_file))
            service_finding.steps += [
                f"Save the granted repository-scoped client token at {token_file}, or set TAUCETI_GITHUB_STATE_TOKEN_FILE to its actual private file. Re-run this check: it performs an authenticated, read-only health request for this repository.",
                f"For workers pass --github-state-token-file {shlex.quote(str(token_file))}; the service URL comes from this project configuration.",
            ]
            if not service.get("url"):
                service_finding.status = "failed"
                service_finding.summary = "Set services.github_state.url to the operator's actual HTTPS endpoint"
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
                    if ready and mode == "hosted":
                        enrollment.status = "ready"
                        enrollment.summary = "Repository-scoped operator grant verified by authenticated health"
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
            missing.append(
                f"Set repositories.{role} in {project.source} and create it with gh repo create OWNER/NAME --public --add-readme"
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
        if required_steps and not missing and not inaccessible and (name != "docs" or not project.legacy):
            workflow, labels = required_steps
            runs, _ = gh.read(f"repos/{repository}/actions/workflows/{workflow}/runs?per_page=5", paginate=False)
            for run in _items(runs, "workflow_runs"):
                jobs, error = gh.read(f"repos/{repository}/actions/runs/{run['id']}/jobs")
                consumed = [step for job in _items(jobs, "jobs") for step in job.get("steps", [])]
                if name == "progress" and not all(any(label in step.get("name", "") for step in consumed) for label in labels):
                    continue
                if run.get("conclusion") == "success" and not error and all(
                    any(label in step.get("name", "") and step.get("conclusion") == "success" for step in consumed)
                    for label in labels
                ):
                    status, summary = "ready", f"Run {run['id']} succeeded and its consuming steps ran successfully"
                elif run.get("conclusion") in ("failure", "timed_out", "action_required"):
                    status, summary = "failed", f"Run {run['id']} failed; inspect the consuming steps"
                commands.append(_cmd("gh", "run", "view", str(run["id"]), "--repo", repository, "--log"))
                break
        results.append(
            Finding(
                "features." + name,
                status,
                summary,
                "Feature workflows installed and their actual consumers succeed",
                commands=commands
                + ([_cmd("gh", "run", "list", "--repo", repository, "--limit", "10")] if repository else []),
                urls=[f"https://github.com/{repository or project.content}/actions"],
                steps=steps,
                verify=again,
                section="extras",
                repair=repair_id,
            )
        )
    return results


def auxiliary_findings(project, gh, again):
    results = []
    for role, feature, names in (
        ("ci", "ci_archive", ("COLLECTOR_APP_ID", "COLLECTOR_APP_PRIVATE_KEY")),
        ("roadmap", "progress", ("SYNC_BOT_APP_PRIVATE_KEY",)),
    ):
        repo = project.repos.get(role)
        if not repo or not project.feature(feature):
            continue
        value, error = gh.read(f"repos/{repo}/actions/secrets")
        present = {row["name"] for row in _items(value, "secrets")}
        for name in names:
            path = (
                Path.home()
                / ".config/tauceti/projects"
                / project.key
                / ("collector" if role == "ci" else "automation")
                / ("APP_PRIVATE_KEY" if role == "roadmap" else name)
            )
            results.append(
                Finding(
                    f"secrets.{role}.{name}",
                    "ready" if name in present else "unknown" if error else "failed",
                    name + (": metadata present; consumer unverified" if name in present else ": unavailable"),
                    "Credential in the repository that consumes it",
                    commands=[f"{_cmd('gh', 'secret', 'set', name, '--repo', repo)} < {shlex.quote(str(path))}"],
                    urls=[f"https://github.com/{repo}/settings/secrets/actions"],
                    steps=[
                        f"Save the {'collector' if role == 'ci' else 'automation'} App credential in {path}; chmod 600 the file. Install that App on the repositories named in the manual's inventory.",
                        f"Choose New repository secret; Name: {name}; Secret: that private file's contents. Run the existing {'collect.yml' if role == 'ci' else 'progress-merge.yml'} workflow with its required inputs and inspect the App-token step.",
                    ],
                    verify=again,
                    section="apps-and-credentials",
                )
            )
        if role == "roadmap":
            results[-1].urls.append("https://github.com/settings/installations")
            results[-1].steps += [
                f"Open Installed GitHub Apps → the project's existing automation App → Configure. Under Repository access choose Only select repositories, add {repo.split('/')[1]} alongside {project.content.split('/')[1]}, and Save. Keep the existing installation and private key.",
                f"Upload the existing automation PEM to {repo} as SYNC_BOT_APP_PRIVATE_KEY; no second automation App is needed.",
            ]
            value, error = gh.read(f"repos/{repo}/actions/variables/SYNC_BOT_APP_ID")
            results.append(
                Finding(
                    "variables.roadmap.SYNC_BOT_APP_ID",
                    "unknown" if error else "ready",
                    error or "Roadmap App ID present",
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


def render(findings, *, json_output=False, show_ready=False):
    if json_output:
        print(json.dumps([item.json() for item in findings], indent=2))
        return
    ordered = [f for f in findings if not f.optional] + [f for f in findings if f.optional]
    extras_started = False
    for item in ordered:
        if item.optional and not extras_started:
            print("\nThe remaining items are optional extras: you can omit these from the `tauceti doctor` report using the `--no-extras` flag.")
            extras_started = True
        print(f"[{item.status.upper()}] {item.id}: {item.summary}")
        if item.status in ("ready", "disabled") and not show_ready:
            continue
        print(f"  Expected: {item.expected}\n  Who: {item.role}")
        for command in item.commands:
            print(f"  $ {command}")
        for url in item.urls:
            print(f"  Open: {url}")
        for index, step in enumerate(item.steps, 1):
            print(f"  {index}. {step}")
        if item.verify:
            print(f"  Verify: {item.verify}")
        print(f"  Manual: {DOCS}#{item.section}")
