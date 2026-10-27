"""Pinned project templates and shared script execution."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

from .project import CANONICAL, ConfigError, Project, bootstrap, checkout_root

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"

# Resolve tags when updating Tools, never when generating a downstream setup PR.
# Verified against each action's GitHub commits API on 2026-10-21.
ACTION_PINS = {
    "actions/checkout@v4": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/cache@v4": "0057852bfaa89a56745cba8c7296529d2fc39830",
    "actions/cache/restore@v4": "0057852bfaa89a56745cba8c7296529d2fc39830",
    "actions/cache/save@v4": "0057852bfaa89a56745cba8c7296529d2fc39830",
    "actions/deploy-pages@v4": "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e",
    "actions/download-artifact@v4": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "actions/upload-artifact@v4": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/upload-pages-artifact@v3": "56afc609e74202658d3ffba0e8f6dda462b719fa",
    "actions/create-github-app-token@v1": "d72941d797fd3113feb6b93fd0dec494b13a2547",
    "leanprover/lean-action@v1": "24581d74d674803a25a81b10b059c5c0cdd344c6",
}


def pin_actions(text: str) -> str:
    for action, commit in ACTION_PINS.items():
        text = re.sub(r"(\buses:\s*)" + re.escape(action) + r"(?=\s|$)",
                      lambda m, ref=action.split("@")[0] + "@" + commit: m[1] + ref, text)
    return text


def guard_app_jobs(text: str) -> str:
    """Skip App-dependent jobs during setup; preserve their event filters."""
    heading = re.search(r"(?m)^jobs:\n", text)
    if not heading or "\n  app-credentials:\n" in text:
        return text
    before, jobs = text[:heading.start()], text[heading.end():]
    blocks = list(re.finditer(r"(?m)^  ([A-Za-z0-9_-]+):\n", jobs))
    originals = {match[1]: jobs[match.start():blocks[i + 1].start() if i + 1 < len(blocks) else len(jobs)]
                 for i, match in enumerate(blocks)}

    def needs(block):
        match = re.search(r"(?m)^    needs:[^\n]*(?:\n      - [^\n]+)*", block)
        if not match:
            return None, []
        value = match[0].split(":", 1)[1].strip()
        names = value.strip("[]").split(",") if not value.startswith("-") else [line.strip()[2:] for line in value.splitlines()]
        return match, [name.strip().strip("\"'") for name in names]

    def condition(block):
        match = re.search(r"(?m)^    if: (.*)(?:\n(?:      .*)?)*", block)
        if not match:
            return None, ""
        value = match[0].split(":", 1)[1].strip()
        if value.startswith((">", "|")):
            value = value.split("\n", 1)[1].strip()
        # Strip YAML comments while preserving quoted strings containing '#'.
        value = re.sub(r"'(?:[^']|'')*'|\"[^\"]*\"|#[^\n]*", lambda m: "" if m[0].startswith("#") else m[0], value)
        value = " ".join(line.strip() for line in value.splitlines()).strip()
        if value.startswith("${{") and value.endswith("}}"):
            value = value[3:-2].strip()
        return match, value

    def event_filter(block, dependencies):
        _, own = condition(block)
        if re.search(r"\b(?:always|success|failure|cancelled)\s*\(", own):
            return "true"
        values = [own] + [condition(originals.get(name, ""))[1] for name in dependencies]
        values = [value for value in values if value and not re.search(r"\b(?:needs|steps|env|runner|job|secrets)\.", value)
                  and not re.search(r"\b(?:always|success|failure|cancelled)\s*\(", value)]
        return " && ".join("(" + value + ")" for value in values) or "true"

    credentials, filters = set(), set()
    for index in reversed(range(len(blocks))):
        match = blocks[index]
        end = blocks[index + 1].start() if index + 1 < len(blocks) else len(jobs)
        block = jobs[match.start():end]
        refs = set(re.findall(r"\$\{\{\s*((?:secrets|vars)\.[A-Z0-9_]*(?:APP_ID|APP_PRIVATE_KEY))\s*\}\}", block))
        if not refs:
            continue
        credentials.update(refs)
        dependency, dependencies = needs(block)
        filters.add(event_filter(block, dependencies))
        if dependency:
            block = block[:dependency.start()] + "    needs: [app-credentials, " + ", ".join(dependencies) + "]" + block[dependency.end():]
        else:
            block = block.replace(match[0], match[0] + "    needs: app-credentials\n", 1)
        match_if, original = condition(block)
        expression = "needs.app-credentials.outputs.ready == 'true'"
        if match_if:
            expression += " && (" + original + ")"
            block = block[:match_if.start()] + "    if: >-\n      " + expression + "\n" + block[match_if.end():].lstrip("\n")
        else:
            block = block.replace(match[0], match[0] + "    if: " + expression + "\n", 1)
        jobs = jobs[:match.start()] + block + jobs[end:]
    if not credentials:
        return text
    # Only a boolean leaves this job. Secret values never reach outputs or logs.
    names = [ref.split(".", 1)[1] for ref in sorted(credentials)]
    env = "".join("          " + ref.split(".", 1)[1] + ": ${{ " + ref + " }}\n" for ref in sorted(credentials))
    preflight_if = "" if "true" in filters else "    if: >-\n      " + " || ".join("(" + value + ")" for value in sorted(filters)) + "\n"
    preflight = (
        "  app-credentials:\n" + preflight_if +
        "    runs-on: ubuntu-latest\n"
        "    permissions: {}\n"
        "    outputs:\n"
        "      ready: ${{ steps.check.outputs.ready }}\n"
        "    steps:\n"
        "      - name: Check App settings for setup\n"
        "        id: check\n"
        "        env:\n" + env +
        "        shell: bash\n"
        "        run: |\n"
        "          ready=true\n"
        "          for name in " + " ".join(names) + "; do\n"
        "            if [ -z \"${!name}\" ]; then\n"
        "              ready=false\n"
        "              printf 'Missing %s. Finish App credential setup with tauceti-tools doctor.\\n' \"$name\" >> \"$GITHUB_STEP_SUMMARY\"\n"
        "            fi\n"
        "          done\n"
        "          printf 'ready=%s\\n' \"$ready\" >> \"$GITHUB_OUTPUT\"\n\n"
    )
    return before + "jobs:\n" + preflight + jobs


def substitutions(project: Project) -> list[tuple[str, str]]:
    """Render only trusted tool templates, never contributor/agent content."""
    root = project.roots[0]
    if len(project.roots) != 1:
        raise ValueError(
            "This release's build harness supports one Lean library root; split the library or keep its existing custom build workflow"
        )
    pairs = [
        ("TauCetiProject/TauCetiRoadmap", project.repos["roadmap"]),
        ("TauCetiProject/TauCetiReview", project.tool("review").get("repo", "TauCetiProject/TauCetiReview")),
        ("TauCetiProject/TauCetiProgress", project.tool("progress").get("repo", "TauCetiProject/TauCetiProgress")),
        ("TauCetiProject/TauCetiData", project.repos.get("data", "")),
        ("TauCetiProject/TauCetiCI", project.repos.get("ci", "")),
        ("TauCetiProject/tauceti-claims", project.repos.get("claims", "")),
        (CANONICAL, project.content),
        ("owner: TauCetiProject\n", "owner: " + project.content.split("/")[0] + "\n"),
        ("repositories: TauCeti\n", "repositories: " + project.content.split("/")[1] + "\n"),
        ("repositories: tauceti-claims\n", "repositories: " + project.repos.get("claims", "/").split("/")[1] + "\n"),
        ("https://bors.taucetiproject.org", project.service("bors").get("url", "")),
        ("/repositories/1/active-batches", "/repositories/" + str(project.service("bors").get("repository_id", 1 if project.legacy else "")) + "/active-batches"),
        ("https://taucetiproject.github.io/TauCeti/docs", project.raw.get("publication", {}).get("docs_url", "")),
        ("roadmap_dir: TauCetiRoadmap", "roadmap_dir: " + project.roadmap_dir),
        ("lean_root: TauCeti", "lean_root: " + project.roots[0]),
        ("TauCetiRoadmap", project.roadmap_dir),
        ("TauCeti.lean", root.replace(".", "/") + ".lean"),
        ("TauCeti/", root.replace(".", "/") + "/"),
        ("TauCeti\\.", root.replace(".", r"\.") + r"\."),
        ("TauCeti.*", root + ".*"),
        ("`TauCeti", "`" + root),
        ('"TauCeti"', json.dumps(root)),
        ("'TauCeti'", repr(root)),
        ("https://leanprover.zulipchat.com", project.raw.get("publication", {}).get("zulip_site", "")),
        (
            "ZULIP_CHANNEL: Tau Ceti",
            "ZULIP_CHANNEL: " + json.dumps(project.raw.get("publication", {}).get("zulip_channel", "")),
        ),
        ('"Tau Ceti"', json.dumps(project.raw.get("publication", {}).get("zulip_channel", ""))),
        ("'Tau Ceti'", repr(project.raw.get("publication", {}).get("zulip_channel", ""))),
        ("TauCeti", root),
        ("origin/main", "origin/" + project.branch),
        ("branches: [main]", "branches: [" + project.branch + "]"),
        ("ref: main", "ref: " + project.branch),
        ("--base main", "--base " + project.branch),
        ('"main"', json.dumps(project.branch)),
        ("'main'", repr(project.branch)),
    ]
    # Single substitution pass: names appearing inside a replacement are data.
    return pairs


def render(text: str, project: Project) -> str:
    import re

    pairs = dict(substitutions(project))
    pattern = re.compile("|".join(
        r"\bTauCeti\b" if key == "TauCeti" else
        re.escape(key) + r"(?![A-Za-z0-9_-])" if key == CANONICAL else re.escape(key)
        for key in sorted(pairs, key=len, reverse=True)))
    return pattern.sub(lambda m: pairs[m.group()], text)


def materialize(project: Project, destination: Path, *, policy_dir: Path | None = None):
    # Read policy first: local callers may materialize directly over scripts/.
    policies = (
        [
            (source.name, source.read_bytes())
            for source in policy_dir.iterdir()
            if source.suffix in (".txt", ".json") and source.is_file()
        ]
        if policy_dir and policy_dir.is_dir()
        else []
    )
    destination.mkdir(parents=True, exist_ok=True)
    for source in (ASSETS / "scripts").rglob("*"):
        if not source.is_file() or "__pycache__" in source.parts:
            continue
        target = destination / source.relative_to(ASSETS / "scripts")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            content = render(source.read_text(), project)
        except UnicodeError:
            shutil.copyfile(source, target)
        else:
            target.write_text(content)
        target.chmod(source.stat().st_mode & 0o777)
    for name, contents in policies:
        (destination / name).write_bytes(contents)


def run_script(name: str, args: list[str], project: Project | None = None) -> int:
    if not name or ".." in Path(name).parts or Path(name).is_absolute():
        raise ValueError("Script name must be a relative shared script path")
    source = ASSETS / "scripts" / name
    if not source.is_file():
        raise ValueError(f"No shared script named {name}")
    if project is None:
        project = bootstrap([])
    if project is None:
        # Legacy Tau Ceti operation needs no new configuration.
        executable = source
    else:
        root = checkout_root()
        destination = Path(tempfile.mkdtemp(prefix="tauceti-scripts-"))
        materialize(project, destination, policy_dir=root / "scripts")
        executable = destination / name
    command = ["python3", str(executable)] if executable.suffix == ".py" else ["bash", str(executable)]
    try:
        env = dict(os.environ)
        if project is not None:
            env.update(TAUCETI_PROJECT_ROOT=str(root), TAUCETI_TRUSTED_SCRIPTS=str(executable.parent))
        return subprocess.call([*command, *args], env=env)
    finally:
        if project is not None:
            shutil.rmtree(destination, ignore_errors=True)


@lru_cache(maxsize=1)
def shared_import_directory() -> Path:
    project = bootstrap([])
    if project is None:
        return ASSETS / "scripts"
    directory = Path(tempfile.mkdtemp(prefix="tauceti-imports-"))
    materialize(project, directory, policy_dir=checkout_root() / "scripts")
    return directory


def import_shared(name: str, module_name: str):
    directory = shared_import_directory()
    sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location(module_name, directory / name)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def python_wrapper(name: str) -> str:
    return f'''#!/usr/bin/env python3
"""Compatibility entrypoint; implementation is pinned through TauCetiTools."""
import sys
from tauceti_tools.templates import import_shared, run_script

if __name__ == "__main__":
    raise SystemExit(run_script({name!r}, sys.argv[1:]))
else:
    implementation = import_shared({name!r}, __name__)
    globals().update(vars(implementation))
    sys.modules[__name__] = implementation
'''


def _write_gitignore(checkout: Path, patterns: list[str]) -> bool:
    ignore = checkout / ".gitignore"
    original = ignore.read_text() if ignore.exists() else ""
    lines = original.splitlines()
    missing = [pattern for pattern in patterns if not any(
        entry in lines for entry in (pattern, pattern.lstrip("/"), pattern.rstrip("/"), pattern.strip("/"))
    )]
    if not missing:
        return False
    ignore.write_text(original + ("\n" if original and not original.endswith("\n") else "") + "\n".join(missing) + "\n")
    return True


def write_project(project: Project, checkout: Path, role: str = "content") -> list[str]:
    """Install generated callers and project policy, preserving existing policy files."""
    if role != "content":
        return write_auxiliary(project, checkout, role)
    written = []
    ignored = ["/.lake/"]
    if project.feature("docs") and not project.legacy:
        ignored.append("/docbuild/")
    if _write_gitignore(checkout, ignored):
        written.append(".gitignore")
    owners = project.raw.get("project", {}).get("codeowners", [])
    codeowners = checkout / ".github/CODEOWNERS"
    if owners and not codeowners.exists():
        codeowners.parent.mkdir(parents=True, exist_ok=True)
        codeowners.write_text(
            "\n".join(
                path + " " + " ".join(owners)
                for path in (
                    "/tauceti.toml",
                    "/.github/",
                    "/scripts/",
                    "/lakefile.*",
                    "/lean-toolchain",
                    "/lake-manifest.json",
                )
            )
            + "\n"
        )
        written.append(".github/CODEOWNERS")
    for source in (ASSETS / "project").rglob("*"):
        if not source.is_file() or "__pycache__" in source.parts or source.suffix == ".pyc":
            continue
        relative = source.relative_to(ASSETS / "project")
        if not project.legacy and relative.name == "infrastructure-tests.yml":
            continue  # The shared harness regression suite belongs to Tools, not every downstream.
        if not project.legacy and relative.name in ("publication_capacity_fixture.json", "stack_policy_fixtures.json"):
            continue  # Infrastructure test fixtures belong to Tools, not downstream policy.
        if not project.legacy and relative.parts[0] == "scripts" and source.suffix not in (".lean", ".txt", ".json"):
            # CI obtains implementations from pinned Tools. Local operators can
            # use `tauceti-tools script NAME`; only Lean sources and policy live here.
            continue
        optional = {
            "claims-access.yml": "claims",
            "api-docs.yml": "docs",
            "pages.yml": "docs",
            "prepare_docs.py": "docs",
            "stuck-alerts.yml": "zulip",
            "zulip-healthcheck.yml": "zulip",
            "toolchain-tags.yml": "security_audit",
            "nightly-verify.yml": "security_audit",
            "roadmap-label.yml": "progress",
        }
        if relative.name in optional and not project.feature(optional[relative.name]):
            continue
        if relative.name == "pages.yml" and not project.legacy:
            continue  # The downstream API workflow also deploys its Pages artifact.
        if relative.name == "prepare_docs.py" and project.legacy:
            continue
        if relative.name == "pick-runner.yml" and not (project.feature("fleet") or project.feature("security_audit")):
            continue
        if relative.name == "update.yml" and not project.legacy:
            continue
        if relative.name == "update-upstreams.yml" and project.legacy:
            continue
        if relative.parts[0] == "scripts" and source.suffix in (".txt", ".json") and (checkout / relative).exists():
            continue
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative.name == "api-docs.yml" and not project.legacy:
            text = render((ASSETS / "downstream/api-docs.yml").read_text(), project)
        else:
            text = render(source.read_text(), project)
        if relative.name == "pr-status.yml" and not project.feature("zulip"):
            text = text.replace('args=(--since', 'args=(--labels-only --since')
            text = text.replace("Reconcile labels and Zulip posts", "Reconcile PR status labels")
        if relative.name == "claims-access.yml" and project.repos.get("claims"):
            text = text.replace(
                "owner: " + project.content.split("/")[0], "owner: " + project.repos["claims"].split("/")[0]
            )
        if relative.name == "nightly-verify.yml" and not project.legacy:
            import re

            text = re.sub(r"(?m)^    runs-on: >-\n(?:      .*\n)+", "    runs-on: ubuntu-24.04\n", text)
        if (
            relative.suffix in (".yml", ".yaml")
            and ".github/workflows" in relative.as_posix()
            and "__TOOLS_" not in text
        ):
            text = add_setup_steps(text)
        tools = project.tool("tools")
        if tools.get("ref"):
            text = text.replace("__TOOLS_REPO__", tools["repo"]).replace("__TOOLS_REF__", tools["ref"])
        review = project.tool("review")
        if review.get("ref"):
            import re

            text = re.sub(
                rf"({re.escape(review['repo'])}/\.github/workflows/[^@\s]+@)[0-9a-f]{{40}}",
                lambda m, ref=review["ref"]: m[1] + ref,
                text,
            )
        if relative.name in ("auto-merge.yml", "merge-sweep.yml") and review.get("ref"):
            text = text.replace("__REVIEW_REF__", review["ref"])
        if relative.as_posix() == ".github/actions/checkout-merge-policy/action.yml" and review.get("ref"):
            text = re.sub(r"(?m)^(        ref: )[0-9a-f]{40}$", lambda m, ref=review["ref"]: m[1] + ref, text)
        if relative.suffix in (".yml", ".yaml"):
            if relative.name not in ("pr-build.yml", "retry-ci-status.yml"):
                text = guard_app_jobs(text)
            text = pin_actions(text)
        target.write_text(text)
        target.chmod(source.stat().st_mode & 0o777)
        written.append(str(relative))
    return written


def add_setup_steps(text: str) -> str:
    """Attach shared setup to trusted project checkout steps in small caller workflows."""
    import re

    blocks = list(
        re.finditer(
            r"(?m)^(?P<indent> +)- (?:name:.*|uses: actions/checkout@.*)\n(?:(?P=indent) +.*\n|[ \t]*\n|(?P=indent)#[^\n]*\n)*",
            text,
        )
    )
    for match in reversed(blocks):
        block = match[0]
        indent = match["indent"]
        if text[match.end() :].startswith(indent + "- name: Prepare pinned shared tools"):
            continue
        if "uses: actions/checkout@" not in block:
            continue
        if any(
            token in block
            for token in ("head.sha", "headRef", "steps.pr.outputs", "path: pr\n", "path: base\n", "path: mergebase\n")
        ):
            continue
        if "path:" in block and "path: gate\n" not in block:
            continue
        step = """      - name: Prepare pinned shared tools
        uses: __TOOLS_REPO__/.github/actions/setup@__TOOLS_REF__
        with:
          tools-repository: __TOOLS_REPO__
          tools-ref: __TOOLS_REF__
          project-directory: .
"""
        if "path: gate\n" in block:
            step = step.replace("project-directory: .", "project-directory: gate")
        step = "".join(
            indent + line[6:] if line.startswith("      ") else line for line in step.splitlines(keepends=True)
        )
        text = text[: match.end()] + step + text[match.end() :]
    return text


def configuration_pr(project: Project, *, apply=False, role="content"):
    pin = project.tool("tools")
    if not pin.get("ref"):
        raise ValueError("Select [tools.tools] repo and ref before installing workflows")
    repository = project.repos.get(role)
    if not repository:
        raise ValueError(f"Set repositories.{role} before preparing this repository")
    existing = subprocess.check_output([
        "gh", "api", "--paginate", f"repos/{repository}/pulls?state=open&per_page=100", "--jq",
        f'.[] | select((.head.repo.full_name // "" | ascii_downcase) == {json.dumps(repository.lower())} and (.head.ref | startswith("setup/tauceti-tools"))) | .html_url',
    ], text=True).splitlines()
    if existing:
        print("An existing setup PR is open: " + existing[0])
        return
    # Preserve a maintainer's checkout-local identity in the temporary setup clone.
    identity = {}
    for key in ("user.name", "user.email"):
        result = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True)
        if result.returncode == 0:
            identity[key] = result.stdout.strip()
    print(f"Install pinned Tools helpers and workflow callers in {repository}; open a human-reviewed PR.")
    directory = Path(tempfile.mkdtemp(prefix="tauceti-setup-"))
    retain = False
    try:
        subprocess.run(["gh", "repo", "clone", repository, str(directory), "--", "--depth", "1"], check=True)
        for key, value in identity.items():
            subprocess.run(["git", "-C", str(directory), "config", key, value], check=True)
        base_branch = subprocess.check_output(["git", "-C", str(directory), "branch", "--show-current"], text=True).strip()
        base_sha = subprocess.check_output(["git", "-C", str(directory), "rev-parse", "HEAD"], text=True).strip()
        write_project(project, directory, role)
        from .project import read_source

        if role == "content":
            (directory / "tauceti.toml").write_bytes(read_source(project.source))
        subprocess.run(["git", "-C", str(directory), "add", "--all"], check=True)
        diff = subprocess.check_output(["git", "-C", str(directory), "diff", "--cached", "--binary"])
        if not diff:
            print("Already configured; no PR needed.")
            return
        # Identical generated changes against the same base use the same branch.
        # A new base after a squash merge gets a new name; no force-push is needed.
        fingerprint = hashlib.sha256(base_sha.encode() + b"\0" + diff).hexdigest()[:12]
        branch = "setup/tauceti-tools-" + fingerprint
        print(f"Setup branch: {branch} (base: {base_branch}@{base_sha})")
        if not apply:
            subprocess.run(["git", "-C", str(directory), "diff", "--cached"], check=True)
            print("Review every generated workflow replacement against your existing build and review policy; retain any project-specific requirements before applying.")
            print("Preview only; the temporary checkout is removed. Add --apply to regenerate, commit, push and open the setup PR. The branch is unchanged if the base and generated diff are unchanged.")
            return
        subprocess.run(["git", "-C", str(directory), "switch", "-c", branch], check=True)
        author = subprocess.run(["git", "-C", str(directory), "var", "GIT_AUTHOR_IDENT"], capture_output=True, text=True)
        if author.returncode:
            raise ConfigError(
                "Git needs your commit identity before creating the setup PR. In your original content checkout run:\n"
                '  git config user.name "YOUR FULL NAME"\n'
                '  git config user.email "YOUR VERIFIED GITHUB EMAIL"\n'
                "Replace both values with your own identity; find your verified or GitHub-provided noreply address at "
                "https://github.com/settings/emails . Rerun the same doctor --fix command from that checkout. "
                "The setup clone inherits this local identity."
            )
        subprocess.run(
            ["git", "-C", str(directory), "commit", "-m", "Configure shared Tau Ceti project tooling"], check=True
        )
        # Retain the prepared commit if pushing or PR creation fails, for recovery.
        retain = True
        pushed = subprocess.run(["git", "-C", str(directory), "push", "-u", "origin", branch])
        if pushed.returncode:
            raise ConfigError(
                f"Could not push the setup branch; your prepared commit is in {directory}. "
                "For browser-based GitHub CLI authentication run:\n"
                "  gh auth refresh --hostname github.com --scopes workflow\n"
                "  gh auth setup-git\n"
                "If GH_TOKEN or GITHUB_TOKEN is set, it overrides saved credentials; unset it to use browser login. "
                "Confirm your account has repository write access; token scopes do not grant that access. "
                "Then retry git push from the saved directory. If the branch was already pushed by an earlier attempt, "
                f"open its PR with gh pr create --repo {repository} --base {base_branch} --head {branch}."
            )
        body = directory / ".git/tauceti-pr-body"
        body.write_text("Configure the project with pinned shared TauCetiTools helpers and workflows.\n\nRoadmap: none\n")
        subprocess.run(
            ["gh", "pr", "create", "--repo", repository, "--base", base_branch, "--head", branch,
             "--title", "Configure shared Tau Ceti tools", "--body-file", str(body)],
            cwd=directory, check=True,
        )
        retain = False
    finally:
        if retain:
            print(f"Prepared setup checkout retained at {directory} for recovery.")
        else:
            shutil.rmtree(directory)


def write_auxiliary(project: Project, checkout: Path, role: str) -> list[str]:
    import re

    source = ASSETS / role
    if not source.is_dir():
        raise ValueError(f"No auxiliary repository template for {role}")
    pin = project.tool("tools")
    conf_url = f"https://raw.githubusercontent.com/{project.content}/{project.branch}/tauceti.toml"
    written = []
    if role == "roadmap" and _write_gitignore(checkout, ["/.lake/"]):
        written.append(".gitignore")
    for original in source.rglob("*"):
        if not original.is_file() or "__pycache__" in original.parts:
            continue
        relative = original.relative_to(source)
        if relative.name == "progress-announce.yml" and not project.feature("zulip"):
            continue
        if role == "roadmap" and relative.name.startswith("progress-") and not project.feature("progress"):
            continue
        text = render(original.read_text(), project)
        if role == "ci":
            text = text.replace("owner: ${{ github.repository_owner }}", "owner: " + project.content.split("/")[0])
        if original.suffix == ".yml":
            # Fetch the one canonical config without adding a second policy file.
            setup = f"""      - name: Checkout pinned shared tools
        uses: actions/checkout@v4
        with:
          repository: {pin["repo"]}
          ref: {pin["ref"]}
          path: .tauceti-tools
          persist-credentials: false
      - name: Load canonical project configuration
        env:
          PYTHONPATH: ${{{{ github.workspace }}}}/.tauceti-tools
          TAUCETI_CONF: {conf_url}
        run: |
          python3 -m tauceti_tools config environment --conf "$TAUCETI_CONF" --github-env "$GITHUB_ENV"
          echo "PYTHONPATH=$PYTHONPATH" >> "$GITHUB_ENV"
          echo "TAUCETI_CI_ARCHIVE_DIR=$GITHUB_WORKSPACE" >> "$GITHUB_ENV"
"""
            matches = list(re.finditer(r"(?m)^      - (?:(?!^      - ).*\n)*", text))
            for match in reversed(matches):
                if "uses: actions/checkout@" in match[0] and "repository:" not in match[0] and "path:" not in match[0]:
                    text = text[: match.end()] + setup + text[match.end() :]
            progress_ref = project.tool("progress").get("ref")
            if progress_ref:
                text = text.replace("REPLACE_WITH_FULL_SHA", progress_ref)
                text = re.sub(r"(TauCetiProgress[^@\s]*@)[0-9a-f]{40}", lambda m, ref=progress_ref: m[1] + ref, text)
        if role == "roadmap" and relative.suffix == ".yml":
            text = text.replace(
                "      progress_ref: ",
                f"      tools_repository: {pin['repo']}\n      tools_ref: {pin['ref']}\n      project_conf: {conf_url}\n      progress_ref: ",
            )
            publication = project.raw.get("publication", {})
            text = text.replace("channel: Tau Ceti", "channel: " + json.dumps(publication.get("zulip_channel", "")))
            text = text.replace("topic: Progress logs", "topic: " + json.dumps(publication.get("zulip_topic", "")))
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if role == "data" and target.exists():
            continue
        if relative.suffix in (".yml", ".yaml"):
            text = guard_app_jobs(text)
            text = pin_actions(text)
        target.write_text(text)
        written.append(str(relative))
    if role == "ci":
        destination = checkout / "collector"
        destination.mkdir(exist_ok=True)
        (destination / "__init__.py").write_text('"""Compatibility namespace for shared CI collectors."""\n')
        for implementation in (HERE / "collector").glob("*.py"):
            if implementation.stem == "__init__":
                continue
            module = implementation.stem
            (destination / implementation.name).write_text(f"""import importlib
import runpy
import sys
if __name__ == "__main__":
    runpy.run_module("tauceti_tools.collector.{module}", run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module("tauceti_tools.collector.{module}")
""")
    return written
