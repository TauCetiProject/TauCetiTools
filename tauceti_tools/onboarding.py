"""Repair instructions usable before a project configuration can be loaded."""

import shlex
import shutil
from pathlib import Path

from .project import DOCS, checkout_root


def configuration_problem(source, reason, *, fresh=False):
    remote = source.startswith("https://")
    target = checkout_root() / "tauceti.toml" if not source else Path("tauceti.toml") if remote else Path(source).expanduser()
    output = target if fresh and not remote else target.with_name(target.name + ".example")
    if output != target:
        index = 2
        while output.exists():
            output = target.with_name(target.name + f".example.{index}")
            index += 1
    quote = shlex.quote
    lines = [f"Configuration needs attention: {reason}", f"Config: {source or target}", ""]
    if not shutil.which("tauceti-tools"):
        lines += ["Install the setup command:",
                  "  uv tool install git+https://github.com/TauCetiProject/TauCetiTools", ""]
    if not shutil.which("gh"):
        lines += ["Install GitHub CLI using the instructions at https://cli.github.com/.", ""]
    lines += [
        "From a checkout of the content repository, sign in as the human maintainer:",
        "  gh auth login --hostname github.com --git-protocol https --web --scopes workflow",
        "  gh auth setup-git",
        "If already signed in, grant permission to push workflow files:",
        "  gh auth refresh --hostname github.com --scopes workflow",
        "Choose your own GitHub account; it will become the initial human CODEOWNER.",
        "If this checkout has several remotes, select the upstream content repository once:",
        "  gh repo set-default",
        "Generate a starter configuration using this checkout's repository and your login:",
        '  tauceti-tools init --repo "$(gh repo view --json nameWithOwner --jq .nameWithOwner)" '
        '--owner-login "$(gh api user --jq .login)" --output ' + quote(str(output)),
    ]
    if not fresh:
        lines += [f"The starter is written separately at {output}; your configuration is preserved."]
        if not remote:
            lines += ["Compare it with your configuration and fix the reported setting:",
                      f"  diff -u {quote(str(target))} {quote(str(output))}"]
    lines += [
        f"Open {output} in your editor. Check the content and roadmap repository names,",
        "project.branch, lean.roots and project.codeowners. The starter uses direct",
        "automatic merging, without a queue or requiring branches to be up to date.",
    ]
    if remote:
        lines += [f"Use the reviewed starter to update the published configuration at {source}",
                  "through a configuration PR in the content repository."]
    elif not fresh:
        lines += [f"Keep your existing settings and correct {target} using the example."]
    lines += ["Then run:", f"  tauceti-tools doctor --conf {quote(source or str(target))}",
              "Doctor will give commands for selecting tool versions and the remaining setup.",
              "Manual: " + DOCS + "#configuration-and-ownership"]
    return "\n".join(lines)
