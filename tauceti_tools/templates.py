"""Pinned project templates and shared script execution."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

from .project import CANONICAL, Project, bootstrap

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"


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
    if project is None and (Path.cwd() / "tauceti.toml").is_file():
        project = bootstrap(["--conf", str(Path.cwd() / "tauceti.toml")])
    if project is None:
        # Legacy Tau Ceti operation needs no new configuration.
        executable = source
    else:
        destination = Path(tempfile.mkdtemp(prefix="tauceti-scripts-"))
        materialize(project, destination, policy_dir=Path.cwd() / "scripts")
        executable = destination / name
    command = ["python3", str(executable)] if executable.suffix == ".py" else ["bash", str(executable)]
    try:
        env = dict(os.environ)
        if project is not None:
            env.update(TAUCETI_PROJECT_ROOT=str(Path.cwd()), TAUCETI_TRUSTED_SCRIPTS=str(executable.parent))
        return subprocess.call([*command, *args], env=env)
    finally:
        if project is not None:
            shutil.rmtree(destination, ignore_errors=True)


@lru_cache(maxsize=1)
def shared_import_directory() -> Path:
    project = bootstrap([])
    if project is None and (Path.cwd() / "tauceti.toml").is_file():
        from .project import load

        project = load(str(Path.cwd() / "tauceti.toml"))
    if project is None:
        return ASSETS / "scripts"
    directory = Path(tempfile.mkdtemp(prefix="tauceti-imports-"))
    materialize(project, directory, policy_dir=Path.cwd() / "scripts")
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


def write_project(project: Project, checkout: Path, role: str = "content") -> list[str]:
    """Install generated callers and project policy, preserving existing policy files."""
    if role != "content":
        return write_auxiliary(project, checkout, role)
    written = []
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
        optional = {
            "claims-access.yml": "claims",
            "api-docs.yml": "docs",
            "pages.yml": "docs",
            "stuck-alerts.yml": "zulip",
            "zulip-healthcheck.yml": "zulip",
            "toolchain-tags.yml": "security_audit",
            "nightly-verify.yml": "security_audit",
        }
        if relative.name in optional and not project.feature(optional[relative.name]):
            continue
        if relative.name == "update.yml" and not project.legacy:
            continue
        if relative.name == "update-upstreams.yml" and project.legacy:
            continue
        if relative.parts[0] == "scripts" and source.suffix in (".txt", ".json") and (checkout / relative).exists():
            continue
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        text = render(source.read_text(), project)
        if relative.as_posix() in (".github/workflows/ci.yml", ".github/workflows/lint-full.yml") and not project.legacy:
            # Downstreams build and audit their library. The shared harness's own
            # regression fixtures belong to its source repository, not each clone.
            import re
            text = re.sub(r"(?ms)^  readiness-tests:.*?(?=^  build:)", "", text)
            text = re.sub(
                r"(?ms)^      - name: (?:Infrastructure policy unit tests|Lake archive reuse integration tests|Header audit equivalence tests)\n.*?(?=^      - |^  [a-z]|\Z)", "", text)
            text = text.replace("needs: [build, duplicate-audit-tests]", "needs: [build]")
            text = text.replace(" && needs['duplicate-audit-tests'].result == 'success'", "")
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
    print(f"Install pinned Tools helpers and workflow callers in {repository}; open a human-reviewed PR.")
    directory = Path(tempfile.mkdtemp(prefix="tauceti-setup-"))
    subprocess.run(["gh", "repo", "clone", repository, str(directory), "--", "--depth", "1"], check=True)
    base_branch = subprocess.check_output(["git", "-C", str(directory), "branch", "--show-current"], text=True).strip()
    existing = subprocess.check_output(
        ["gh", "pr", "list", "--repo", repository, "--head", "setup/tauceti-tools", "--json", "url"], text=True
    )
    if json.loads(existing):
        print("An existing setup PR is open: " + json.loads(existing)[0]["url"])
        return
    branch = "setup/tauceti-tools"
    subprocess.run(["git", "-C", str(directory), "switch", "-c", branch], check=True)
    write_project(project, directory, role)
    from .project import read_source

    if role == "content":
        (directory / "tauceti.toml").write_bytes(read_source(project.source))
    subprocess.run(["git", "-C", str(directory), "add", "--all"], check=True)
    if not apply:
        subprocess.run(["git", "-C", str(directory), "diff", "--cached"], check=True)
        print(f"Preview saved in {directory}; inspect its files. Add --apply to commit, push and open the setup PR.")
        return
    if subprocess.run(["git", "-C", str(directory), "diff", "--cached", "--quiet"]).returncode == 0:
        print("Already configured; no PR needed.")
        return
    subprocess.run(
        ["git", "-C", str(directory), "commit", "-m", "Configure shared Tau Ceti project tooling"], check=True
    )
    # gh pr create can choose the operator's fork when upstream write access is absent.
    subprocess.run(["git", "-C", str(directory), "push", "-u", "origin", branch], check=True)
    body = directory / ".git/tauceti-pr-body"
    body.write_text("Configure the project with pinned shared TauCetiTools helpers and workflows.\n\nRoadmap: none\n")
    subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repository,
            "--base",
            base_branch,
            "--head",
            branch,
            "--title",
            "Configure shared Tau Ceti tools",
            "--body-file",
            str(body),
        ],
        cwd=directory,
        check=True,
    )


def write_auxiliary(project: Project, checkout: Path, role: str) -> list[str]:
    import re

    source = ASSETS / role
    if not source.is_dir():
        raise ValueError(f"No auxiliary repository template for {role}")
    pin = project.tool("tools")
    conf_url = f"https://raw.githubusercontent.com/{project.content}/{project.branch}/tauceti.toml"
    written = []
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
