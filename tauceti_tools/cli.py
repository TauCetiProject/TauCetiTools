"""Standalone administration; no worker harness is required."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .project import ConfigError, bootstrap, conf_argument, configuration_source, load, save_snapshot


def parser():
    p = argparse.ArgumentParser(prog="tauceti-tools", description=__doc__)
    p.add_argument("--conf", help="Project TOML file or HTTPS URL (default: TAUCETI_CONF, current directory, Git root)")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Write the full project configuration template")
    init.add_argument("--repo", required=True)
    init.add_argument("--output", type=Path, default=Path("tauceti.toml"))
    init.add_argument("--owner-login", required=True, help="Human CODEOWNER GitHub login")
    config = sub.add_parser("config")
    config.add_argument("action", choices=("show", "refresh", "environment"))
    config.add_argument("--resolved", action="store_true")
    config.add_argument("--github-env", type=Path)
    doctor = sub.add_parser("doctor")
    extras = doctor.add_mutually_exclusive_group()
    extras.add_argument("--no-extras", dest="extras", action="store_false", help="Report only core setup checks")
    extras.add_argument("--extras", dest="extras", action="store_true", help=argparse.SUPPRESS)
    doctor.set_defaults(extras=True)
    doctor.add_argument("--online", action="store_true", help="Allow explicitly named existing workflow dispatch")
    doctor.add_argument("--workflow", help="Existing workflow_dispatch workflow to run with --online")
    doctor.add_argument(
        "--field", action="append", default=[], help="Existing workflow input NAME=VALUE; used with --online"
    )
    doctor.add_argument("--check")
    doctor.add_argument("--fix")
    doctor.add_argument("--apply", action="store_true")
    doctor.add_argument("--json", action="store_true")
    upgrade = sub.add_parser("upgrade")
    upgrade.add_argument("--apply", action="store_true")
    upgrade.add_argument("--tool", choices=("tools", "review", "progress", "docgen", "bors"), required=True)
    upgrade.add_argument("--ref", required=True)
    script = sub.add_parser("script")
    script.add_argument("name")
    script.add_argument("args", nargs=argparse.REMAINDER)
    mat = sub.add_parser("materialize", help="Prepare trusted shared scripts for a project checkout")
    mat.add_argument("destination", type=Path)
    mat.add_argument("--policy-dir", type=Path)
    deploy = sub.add_parser("deploy")
    deploy.add_argument("service", choices=("bors", "github-state", "cache"))
    deploy.add_argument("--output", type=Path, default=Path("deployment"))
    apps = sub.add_parser("apps")
    apps.add_argument("action", choices=("manifest",))
    apps.add_argument("--role", choices=("automation", "claims", "collector", "bors", "github-state"), required=True)
    apps.add_argument("--output", type=Path, required=True)
    neon = sub.add_parser("neon", help="Operator-only approved downstream database administration")
    neon.add_argument("action", choices=("provision", "inspect", "rotate", "handover"))
    neon.add_argument("--registry", type=Path, required=True)
    neon.add_argument("--token-file", type=Path, required=True)
    neon.add_argument("--output", type=Path)
    neon.add_argument("--apply", action="store_true")
    upstreams = sub.add_parser("upstreams", help="Inspect consistent forward dependency targets")
    upstreams.add_argument("action", choices=("inspect", "update", "guard"))
    upstreams.add_argument("--checkout", type=Path, default=Path.cwd())
    upstreams.add_argument("--base", type=Path)
    upstreams.add_argument("--merge-base", type=Path)
    upstreams.add_argument("--apply", action="store_true")
    return p


def init_config(args):
    from .scaffold import initial_configuration

    if args.output.exists():
        raise ConfigError(f"Refusing to overwrite {args.output}; edit it or choose --output")
    text = initial_configuration(args.repo, args.owner_login)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text)
    print(f"Wrote {args.output}. Core and optional integrations are documented in the file.")
    print(f"Next: tauceti-tools doctor --conf {args.output}")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        conf, argv = conf_argument(argv)
        args = parser().parse_args(argv)
        if args.command == "init":
            return init_config(args)
        source = configuration_source(conf)
        if args.command == "script" and not source:
            from .templates import run_script

            return run_script(args.name, args.args)
        if not source:
            raise ConfigError(
                "Pass --conf PATH_OR_URL. To start: tauceti-tools init --repo OWNER/PROJECT --owner-login HUMAN"
            )
        project = (
            bootstrap(["--conf", source]) if args.command == "config" and args.action == "environment" else load(source)
        )
        if args.command == "upstreams":
            from .upstreams import guard, update

            if args.action == "guard":
                if not args.base or not args.merge_base:
                    raise ConfigError("upstreams guard requires --base, --merge-base and --checkout")
                guard(project, args.base, args.merge_base, args.checkout)
            else:
                update(project, args.checkout, apply=args.action == "update" and args.apply)
            return 0
        if args.command == "neon":
            from .neon import run

            run(project, args)
            return 0
        if args.command == "materialize":
            from .templates import materialize

            materialize(project, args.destination, policy_dir=args.policy_dir)
            return 0
        if args.command == "config":
            if args.action == "refresh":
                path = save_snapshot(source)
                print(
                    f"Saved {path}. Restart the worker with --conf {path} to adopt it; running rounds retain their current configuration."
                )
            elif args.action == "environment":
                env = project.environment()
                env.update(
                    {key: os.environ[key] for key in ("TAUCETI_CONF", "TAUCETI_CONF_SOURCE", "TAUCETI_CONF_SNAPSHOT")}
                )
                if args.github_env:
                    with args.github_env.open("a") as output:
                        for key, value in env.items():
                            if "\n" in value or "\r" in value:
                                raise ConfigError(f"{key} contains a line break")
                            output.write(f"{key}={value}\n")
                else:
                    print(json.dumps(env, indent=2))
            else:
                print(json.dumps(project.raw, indent=2))
            return 0
        if args.command == "script":
            from .templates import run_script

            bootstrap(["--conf", source])
            return run_script(args.name, args.args, project)
        if args.command == "doctor":
            from .doctor import diagnose, render, repair

            if args.apply and not args.fix:
                raise ConfigError("--apply requires --fix CHECK_ID")
            if args.fix:
                repair(project, args.fix, apply=args.apply)
                if not args.apply:
                    return 0
            if args.online:
                if not args.workflow:
                    raise ConfigError(
                        "--online requires --workflow NAME: select an existing workflow with workflow_dispatch"
                    )
                dispatch = ["gh", "workflow", "run", args.workflow, "--repo", project.content, "--ref", project.branch]
                for value in args.field:
                    if "=" not in value:
                        raise ConfigError("--field requires NAME=VALUE")
                    dispatch.extend(["--field", value])
                subprocess.run(dispatch, check=True)
            findings = diagnose(project, extras=args.extras)
            if args.check:
                findings = [f for f in findings if f.id == args.check]
                if not findings:
                    raise ConfigError(f"Unknown check {args.check}; run doctor without --check to list checks")
            render(findings, json_output=args.json, show_ready=bool(args.check))
            bad = any(f.status in ("failed", "unknown") for f in findings)
            if not args.json:
                core_bad = any(f.status in ("failed", "unknown") and not f.optional for f in findings)
                print(
                    "Core setup needs attention."
                    if core_bad
                    else "Core configured; pending functional checks are listed above."
                )
                if bad and not core_bad:
                    print("Optional extras need attention.")
            return 1 if bad else 0
        if args.command == "upgrade":
            from .scaffold import upgrade

            return upgrade(project, args.tool, args.ref, apply=args.apply)
        if args.command == "deploy":
            from .deployment import write_deployment

            write_deployment(project, args.service, args.output)
            return 0
        if args.command == "apps":
            from .deployment import app_manifest

            if args.output.exists():
                raise ConfigError(f"Refusing to overwrite {args.output}")
            args.output.write_text(json.dumps(app_manifest(project, args.role), indent=2) + "\n")
            print(
                f"App manifest: {args.output}. See the manual for manifest registration and the complete manual form fields."
            )
            return 0
    except (ConfigError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"tauceti-tools: {error}", file=sys.stderr)
        return 2
    return 0
