"""One shared-harness revision for a project's CI, with every workflow literal derived from it.

The project's `.tauceti/ci-source.json` (`harness.name`, `harness_revision`) is the only source
of truth for the harness revision its CI runs. GitHub requires a literal ref in every `uses:`,
so those literals are derived from it (`set`) and verified against it (`check`, and the
merge-policy scope guard in `lint_scope_files.py --policy`).

A workflow is bound to the source when it is the policy's caller, calls a harness reusable
workflow, or pins any harness literal at the source revision. Every literal in a bound workflow
must equal the source revision. Other workflows keep their own, independently bumped pins.
Binding is evaluated at both the base and the head of a change, so a change cannot move the
source without its bound workflows, or a bound workflow without the source.

Runner-group allow-lists are not edited here: the hosted fleet broker derives them from the
same protected main.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HEX40 = re.compile(r"[0-9a-f]{40}")
SOURCE = ".tauceti/ci-source.json"
WORKFLOWS = ".github/workflows/"


class PinError(ValueError):
    """The source of truth cannot be read; never guess a revision."""


def harness(source_text):
    """(harness repository, revision, caller workflow file) from the policy text."""
    try:
        value = json.loads(source_text)
        repository = value["harness"]["name"]
        revision = value["harness_revision"]
        workflow = value.get("workflow", "")
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise PinError(f"{SOURCE} does not name a harness repository and revision") from error
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise PinError(f"{SOURCE} harness.name is not an owner/repository name")
    if not isinstance(revision, str) or not HEX40.fullmatch(revision):
        raise PinError(f"{SOURCE} harness_revision is not a full lowercase commit SHA")
    if not isinstance(workflow, str):
        raise PinError(f"{SOURCE} workflow is not a path")
    return repository, revision, workflow.removeprefix(WORKFLOWS) if workflow.startswith(WORKFLOWS) else ""


def _site_patterns(repository):
    return (
        re.compile(rf"({re.escape(repository)}/\.github/(?:actions|workflows)/[^@\s'\"]+@)([^\s'\"#]+)"),
        re.compile(r"^(\s*tools[-_]ref:\s*['\"]?)([^\s'\"#]+)", re.MULTILINE),
    )


def literal_sites(text, repository):
    """Every harness pin literal in a workflow: (line number, value)."""
    sites = []
    for pattern in _site_patterns(repository):
        for match in pattern.finditer(text):
            sites.append((text.count("\n", 0, match.start(2)) + 1, match.group(2)))
    return sorted(sites)


def _calls_reusable(text, repository):
    return re.search(rf"^\s*-?\s*uses:\s*['\"]?{re.escape(repository)}/\.github/workflows/", text, re.MULTILINE)


def bound(source_text, workflows):
    """Names of the workflows bound to the source revision in one snapshot."""
    repository, revision, caller = harness(source_text)
    names = {name for name, text in workflows.items() if _calls_reusable(text, repository)}
    names |= {name for name, text in workflows.items() if any(v == revision for _, v in literal_sites(text, repository))}
    if caller:
        names.add(caller)
    return names


def check(head_source, head_workflows, base_source=None, base_workflows=None):
    """Errors for bound workflows whose literals differ from the head's source revision."""
    repository, revision, _ = harness(head_source)
    names = bound(head_source, head_workflows)
    if base_source is not None:
        base_repository, _, _ = harness(base_source)
        if base_repository == repository:
            names |= bound(base_source, base_workflows or {}) & set(head_workflows)
    errors = []
    for name in sorted(names):
        if name not in head_workflows:
            errors.append(f"{WORKFLOWS}{name}: the {SOURCE} caller workflow is missing")
            continue
        sites = literal_sites(head_workflows[name], repository)
        if not sites:
            errors.append(f"{WORKFLOWS}{name}: no {repository} pin found in a bound workflow")
        for line, value in sites:
            if value != revision:
                errors.append(f"{WORKFLOWS}{name}:{line}: pins {value}, but {SOURCE} harness_revision is {revision}")
    return errors


def _snapshot(root):
    root = Path(root)
    workflows = {p.name: p.read_text() for p in sorted((root / WORKFLOWS).glob("*.y*ml"))}
    return (root / SOURCE).read_text(), workflows


def set_revision(root, revision):
    """Rewrite the source of truth and derive every bound literal from it. Returns changed paths."""
    if not HEX40.fullmatch(revision):
        raise PinError("the harness revision must be a full lowercase commit SHA")
    root = Path(root)
    text, workflows = _snapshot(root)
    repository, _, _ = harness(text)
    names = bound(text, workflows)
    updated, count = re.subn(r'("harness_revision"\s*:\s*")[0-9a-f]{40}(")', rf"\g<1>{revision}\g<2>", text)
    if count != 1:
        raise PinError(f"{SOURCE} must contain exactly one harness_revision")
    changed = []
    if updated != text:
        (root / SOURCE).write_text(updated)
        changed.append(root / SOURCE)
    for name in sorted(names & set(workflows)):
        new = workflows[name]
        for pattern in _site_patterns(repository):
            new = pattern.sub(lambda m: m.group(1) + revision, new)
        if new != workflows[name]:
            (root / WORKFLOWS / name).write_text(new)
            changed.append(root / WORKFLOWS / name)
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tauceti_tools.harness_pins", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    checker = sub.add_parser("check", help="verify every bound literal against the source revision")
    checker.add_argument("--root", default=".")
    setter = sub.add_parser("set", help="set the source revision and derive every bound literal")
    setter.add_argument("revision")
    setter.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    try:
        if args.command == "set":
            for path in set_revision(args.root, args.revision):
                print(path)
            return 0
        errors = check(*_snapshot(args.root))
    except (PinError, OSError) as error:
        print(f"harness-pins: {error}", file=sys.stderr)
        return 1
    for error in errors:
        print(f"harness-pins: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
