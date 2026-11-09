"""Qualified build settings for trusted post-merge jobs on the GitHub main mirror.

This produces post-merge provenance, never a candidate receipt or merge verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from . import ci_candidate as cc
from .ci_candidate_workflow import check_source_repository
from .forge import Forgejo
from .project import parse
from .system import ensure_ssl_cert_file
from .templates import materialize

WORKFLOWS = {".github/workflows/ci.yml", ".github/workflows/api-docs.yml", ".github/workflows/pages.yml"}
SCHEMA = "tauceti-ci.source-mirror/v1"


def bootstrap(policy, configuration, context, scripts, *, policy_dir=None, client=None):
    if cc.ExecutionPolicy.parse(policy.record(with_config=False), configuration=configuration) != policy:
        raise ValueError("mirror build policy differs from its trusted configuration")
    if os.environ.get("TAUCETI_SOURCE_CANDIDATE") == "true":
        raise ValueError("post-merge setup cannot replace a source-candidate CI context")
    project = parse(configuration)
    if (project.repository_ref("content"), project.ci_repository_ref(), project.branch) != (
        policy.source.repository,
        policy.execution.repository,
        policy.base_branch,
    ):
        raise ValueError("mirror project source/execution differs from policy")
    if (
        policy.source.repository.forge.kind != "forgejo"
        or policy.execution.repository.forge != cc.GITHUB
        or context.get("event_name") not in {"push", "workflow_dispatch", "schedule"}
        or context.get("repository") != policy.execution.repository.name
        or context.get("repository_id") != str(policy.execution.native_id)
        or context.get("ref") != "refs/heads/" + policy.execution_branch
        or context.get("harness_repository") != policy.harness.repository.name
        or context.get("harness_sha") != policy.harness_revision
    ):
        raise ValueError("mirror build context differs from its approved source/execution/harness")
    revision = context.get("sha")
    if (
        not isinstance(revision, str)
        or not re.fullmatch("[0-9a-f]{40}", revision)
        or revision == "0" * 40
        or context.get("workflow_sha") != revision
        or context.get("checkout_sha") != revision
    ):
        raise ValueError("mirror build must use the exact pushed protected caller revision")
    caller = context.get("workflow_ref", "")
    permitted = {
        policy.execution.repository.name + "/" + name + "@refs/heads/" + policy.execution_branch for name in WORKFLOWS
    }
    if caller not in permitted:
        raise ValueError("unsupported post-merge workflow caller")
    for field in ["run_id", "attempt"]:
        value = context.get(field)
        if not isinstance(value, str) or not re.fullmatch("[1-9][0-9]{0,18}", value):
            raise ValueError("mirror build needs an exact positive run and attempt identity")
    if client is None:
        ensure_ssl_cert_file()
        client = Forgejo(policy.credential_origin)
    check_source_repository(policy, client)
    commit = client.request("GET", f"repos/{policy.source.repository.name}/git/commits/{revision}")
    if not isinstance(commit, dict) or commit.get("sha") != revision:
        raise ValueError("mirror revision is unavailable in the approved source repository")
    branch = client.request(
        "GET", f"repos/{policy.source.repository.name}/branches/{quote(policy.base_branch, safe='')}"
    )
    source_main = branch.get("commit", {}).get("id") if isinstance(branch, dict) else None
    if (
        not isinstance(source_main, str)
        or not re.fullmatch("[0-9a-f]{40}", source_main)
        or source_main == "0" * 40
        or branch.get("name") != policy.base_branch
        or branch.get("protected") is not True
    ):
        raise ValueError("source main needs an exact protected branch identity")
    # Forgejo's Compare has no GitHub-style status field. In this direction,
    # zero commits proves revision is reachable from the captured source tip.
    # An older mirrored main is valid; an unmerged PR commit is not.
    comparison = client.request(
        "GET",
        f"repos/{policy.source.repository.name}/compare/{source_main}...{revision}?files=false&verification=false",
    )
    if (
        not isinstance(comparison, dict)
        or type(comparison.get("total_commits")) is not int
        or comparison["total_commits"] != 0
        or comparison.get("commits") != []
    ):
        raise ValueError("mirror revision is not proven to be on protected source main")
    check_source_repository(policy, client)
    # Preserve explicit native-only sentinels for unported consumers. Cache
    # scope belongs to execution and is deliberately not a native repo alias.
    env = project.qualified_environment()
    if env.get("TAUCETI_UPSTREAMS") == "qualified-only:unavailable":
        raise ValueError("mirror build upstreams need a supported explicit hosting binding")
    env.update(
        {
            "TAUCETI_SOURCE_CANDIDATE": "false",
            "TAUCETI_SOURCE_MIRROR": "true",
            "TAUCETI_SOURCE_REPOSITORY": json.dumps(policy.source.record(), sort_keys=True),
            "TAUCETI_CI_EXECUTION_REPOSITORY": json.dumps(policy.execution.record(), sort_keys=True),
            "TAUCETI_CACHE_REPOSITORY": policy.execution.repository.name,
            "TAUCETI_GITHUB_STATE_URL": "",
        }
    )
    materialize(project, Path(scripts), policy_dir=Path(policy_dir) if policy_dir else None, source_ci=True)
    provenance = {
        "schema": SCHEMA,
        "source": policy.source.record(),
        "execution": policy.execution.record(),
        "revision": revision,
        "source_main": source_main,
        "checkout_revision": context["checkout_sha"],
        "policy_digest": policy.digest,
        "configuration_digest": policy.config_digest,
        "harness": policy.harness.record(),
        "harness_revision": policy.harness_revision,
        "workflow_ref": caller,
        "run_id": int(context["run_id"]),
        "attempt": int(context["attempt"]),
    }
    return env, provenance


def write_workflows(policy, configuration, destination):
    """Render only the essential existing main/cache/docs workflows for review."""
    from .templates import ASSETS, add_setup_steps, pin_actions, render

    project = parse(configuration)
    if cc.ExecutionPolicy.parse(policy.record(with_config=False), configuration=configuration) != policy:
        raise ValueError("mirror workflow policy differs from configuration")
    if (project.repository_ref("content"), project.ci_repository_ref(), project.branch) != (
        policy.source.repository,
        policy.execution.repository,
        policy.base_branch,
    ):
        raise ValueError("mirror workflow source/execution differs from configuration")
    if policy.source.repository.forge.kind != "forgejo":
        raise ValueError("mirror workflows require an explicit Forgejo source")
    directory = Path(destination) / ".github/workflows"
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name in ["ci.yml", "api-docs.yml", "pages.yml"]:
        raw = (ASSETS / "project/.github/workflows" / name).read_text()
        if name == "ci.yml":
            raw = raw.replace(
                "uses: ./.github/workflows/publish-lake-cache.yml",
                "uses: __TOOLS_REPO__/.github/workflows/publish-lake-cache.yml@__TOOLS_REF__",
            )
        raw = add_setup_steps(raw, source_mirror=True)
        text = render(raw, project, source_ci=True)
        text = text.replace("__TOOLS_REPO__", policy.harness.repository.name).replace(
            "__TOOLS_REF__", policy.harness_revision
        )
        text = pin_actions(text)
        target = directory / name
        target.write_text(text)
        written.append(str(target))
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("command", choices=["bootstrap", "generate"])
    parser.add_argument("--scripts", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--policy-dir", type=Path)
    parser.add_argument("--github-env", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--harness-repository")
    parser.add_argument("--harness-sha")
    args = parser.parse_args(argv)
    configuration = args.configuration.read_bytes()
    policy = cc.ExecutionPolicy.parse(cc.read_json(args.policy.read_bytes()), configuration=configuration)
    if args.command == "generate":
        if args.destination is None:
            parser.error("generate requires --destination")
        print(json.dumps(write_workflows(policy, configuration, args.destination)))
        return 0
    if any(
        value is None
        for value in [args.scripts, args.github_env, args.provenance, args.harness_repository, args.harness_sha]
    ):
        parser.error("bootstrap requires scripts, github-env, provenance, harness-repository and harness-sha")
    names = {
        "event_name": "GITHUB_EVENT_NAME",
        "repository": "GITHUB_REPOSITORY",
        "repository_id": "GITHUB_REPOSITORY_ID",
        "ref": "GITHUB_REF",
        "sha": "GITHUB_SHA",
        "workflow_sha": "GITHUB_WORKFLOW_SHA",
        "workflow_ref": "GITHUB_WORKFLOW_REF",
        "run_id": "GITHUB_RUN_ID",
        "attempt": "GITHUB_RUN_ATTEMPT",
    }
    context = {name: os.environ.get(variable, "") for name, variable in names.items()}
    git_env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
    git_env.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1"
    )
    context["checkout_sha"] = subprocess.check_output(
        [
            "git",
            "--no-replace-objects",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(args.configuration.parent),
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        ],
        env=git_env,
        timeout=30,
        text=True,
    ).strip()
    context.update(harness_repository=args.harness_repository, harness_sha=args.harness_sha)
    env, provenance = bootstrap(policy, configuration, context, args.scripts, policy_dir=args.policy_dir)
    lines = []
    for name, value in env.items():
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            raise ValueError("invalid mirror build environment value")
        lines.append(name + "=" + value + "\n")
    with args.github_env.open("a") as output:
        output.writelines(lines)
    args.provenance.write_bytes(cc.canonical(provenance))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
