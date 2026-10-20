"""Editable full-profile configuration; optional services require explicit choices."""

from __future__ import annotations

import re
from pathlib import Path

from .project import ConfigError, load, repository


def initial_configuration(repo: str, owner: str) -> str:
    repository(repo, "--repo")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", owner):
        raise ConfigError("--owner-login must be a GitHub login")
    org, name = repo.split("/")
    return f'''# Canonical, human-owned project configuration. No credentials belong here.
schema_version = 1

[project]
name = "{name}"
branch = "main"
codeowners = ["@{owner}"]

[repositories]
content = "{repo}"
roadmap = "{org}/{name}Roadmap"
review = "TauCetiProject/TauCetiReview"
# Enable and create these repositories when choosing the corresponding extras.
# data = "{org}/{name}Data"
# ci = "{org}/{name}CI"
# claims = "{org}/{name}-claims"

[lean]
roots = ["{name}"]
root_module = "empty"

[roadmap]
active_dir = "{name}Roadmap"
completed_dir = "Completed"

# Select full commit SHAs before installing the core workflows.
# Discover a version with: gh api repos/TauCetiProject/TauCetiTools/commits/main --jq .sha
# [tools.tools]
# repo = "TauCetiProject/TauCetiTools"
# ref = "FULL_COMMIT_SHA"
# [tools.review]
# repo = "TauCetiProject/TauCetiReview"
# ref = "FULL_COMMIT_SHA"
# [tools.progress]
# repo = "TauCetiProject/TauCetiProgress"
# ref = "FULL_COMMIT_SHA"
# [tools.docgen]
# repo = "leanprover/doc-gen4"
# ref = "FULL_COMMIT_SHA_COMPATIBLE_WITH_YOUR_LEAN_TOOLCHAIN"

[merge]
backend = "github-queue"
checks = ["build", "bump-guard", "scope"]

[[upstreams]]
repo = "leanprover-community/mathlib4"
package = "mathlib"
branch = "master"
update = true
overlap = true
upstream_destination = true

# Everything below is optional. Run doctor --extras to continue beyond core.
[features]
archives = false
ci_archive = false
claims = false
docs = false
progress = false
zulip = false
fleet = false
security_audit = false

[services.github_state]
mode = "off" # off | hosted | custom
# url = "https://github-state.example.org"

[services.bors]
mode = "off" # hosted supplies Neon only; YOU operate the runtime
deployment = "docker" # docker | cloudflare
# url = "https://bors.example.org"
# image = "ghcr.io/taucetiproject/bors-ng@sha256:SELECT_RELEASE_DIGEST"

[services.cache]
mode = "off"
# name = "{name.lower()}-public"
# artifact_url = "https://cache.example.org/artifacts"
# revision_url = "https://cache.example.org/revisions"
# artifact_write_url = "https://ACCOUNT_ID.r2.cloudflarestorage.com/PROJECT-cache/artifacts"
# revision_write_url = "https://ACCOUNT_ID.r2.cloudflarestorage.com/PROJECT-cache/revisions"

[publication]
# docs_url = "https://{org.lower()}.github.io/{name}/docs"
# zulip_site = "https://leanprover.zulipchat.com"
# zulip_channel = "Project channel"
# zulip_topic = "Progress logs"
'''


def upgrade(project, tool, ref, *, apply=False):
    from .project import REF_RE
    from .templates import write_project

    if not REF_RE.fullmatch(ref):
        raise ConfigError("--ref must be a full 40-character commit SHA")
    if project.source.startswith("https:"):
        raise ConfigError("Clone the content repository and pass --conf ./tauceti.toml to prepare a version update")
    path = Path(project.source).resolve()
    text = path.read_text()
    section = re.compile(rf"(?ms)(^\[tools\.{re.escape(tool)}\]\s*\n)(.*?)(?=^\[|\Z)")
    match = section.search(text)
    if not match:
        raise ConfigError(f"Add [tools.{tool}] with repo and ref before upgrading")
    body, count = re.subn(r'(?m)^ref\s*=\s*"[0-9a-f]{40}"', f'ref = "{ref}"', match[2], count=1)
    if not count:
        raise ConfigError(f"No immutable ref in [tools.{tool}]")
    updated = text[: match.start()] + match[1] + body + text[match.end() :]
    print(f"{tool}: {project.tool(tool).get('ref')} → {ref}; regenerate matching workflow callers in {path.parent}")
    if not apply:
        print("Preview only. Add --apply to edit the local checkout, then review and open a PR.")
        return 0
    path.write_text(updated)
    write_project(load(str(path)), path.parent)
    print("Updated local files. Inspect git diff and open a human-reviewed PR against the default branch.")
    return 0
