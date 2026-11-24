"""Check this repository's reusable workflows against the project workflows that call them.

Projects call TauCetiTools workflows at @main, so a change here reaches their CI with no project
review. GitHub rejects a run at startup when caller and callee disagree (an undeclared input or
secret, a missing required one, a nested job asking for a permission the caller did not grant,
as Tools #288 did), and a job routed to a workflow-restricted runner group queues forever unless
the group's policy admits the file that defines the job. This module finds those disagreements
from the workflow text, before a merge.

The contract each workflow offers is in `.github/workflow-contract.yml`.

Usage: python3 tests/workflow_contract.py PROJECT_ROOT FLEET_ROOT
  PROJECT_ROOT  a TauCeti checkout (only `.github/` is read)
  FLEET_ROOT    a TauCetiFleet checkout (only services/ci-broker/src/github.mjs is read)
"""

import json
import re
import sys
from pathlib import Path

import yaml

TOOLS_ROOT = Path(__file__).resolve().parents[1]
TOOLS_REPOSITORY = "TauCetiProject/TauCetiTools"
PROJECT_REPOSITORY = "TauCetiProject/TauCeti"
CONTRACT = ".github/workflow-contract.yml"
FLEET_POLICY = "services/ci-broker/src/github.mjs"
LEVELS = {"none": 0, "read": 1, "write": 2}
# A permissions value is held as {scope: level}, with "*" the level of every scope not named, so
# `read-all` and `write-all` need no list of scopes. `write-all` grants models only read.
OTHERS = "*"
CAPPED = {"models": 1}
REMOTE_CALL = re.compile(r"^([\w.-]+/[\w.-]+)/\.github/workflows/([\w.-]+\.ya?ml)@(.+)$")
LOCAL_CALL = re.compile(r"^\./\.github/workflows/([\w.-]+\.ya?ml)$")
INPUT_REF = re.compile(r"\binputs(?:\.([A-Za-z_][\w-]*)|\[\s*'([^']+)'\s*\])")
GROUP_REF = re.compile(r"""["']?group["']?\s*:\s*["']?([A-Za-z][\w-]*)""")
EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.S)


def load(path):
    return yaml.safe_load(Path(path).read_text())


def triggers(workflow):
    """The `on:` block as a dict (PyYAML reads a bare `on` key as True)."""
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {event: None for event in on}
    return on or {}


def permissions(value):
    """A permissions value as {scope: level, "*": others}; None when not set."""
    if value is None:
        return None
    if value == "read-all":
        return {OTHERS: 1}
    if value == "write-all":
        return {OTHERS: 2, **CAPPED}
    if value == {} or value == "":
        return {OTHERS: 0}
    if not isinstance(value, dict):
        raise ValueError(f"unreadable permissions {value!r}")
    return {OTHERS: 0, **{scope: LEVELS[level] for scope, level in value.items()}}


def level(perms, scope):
    return perms.get(scope, perms[OTHERS])


def show(perms):
    names = {level: name for name, level in LEVELS.items()}
    shown = [f"{scope}: {names[level]}" for scope, level in sorted(perms.items()) if level and scope != OTHERS]
    if perms[OTHERS]:
        shown.append(f"every other scope: {names[perms[OTHERS]]}")
    return ", ".join(shown) or "none"


def exceeding(asked, ceiling):
    extra = {scope: level(asked, scope) for scope in {*asked, *ceiling} - {OTHERS}
             if level(asked, scope) > level(ceiling, scope)}
    if extra or asked[OTHERS] > ceiling[OTHERS]:
        return {OTHERS: asked[OTHERS] if asked[OTHERS] > ceiling[OTHERS] else 0, **extra}
    return {}


def concurrency_group(value, env=None):
    """A concurrency group, normalised for comparison: GitHub compares groups case-insensitively.

    With `env`, the callee's `inputs.<name>` references are replaced by what its caller passed, so
    one spelling read in two input contexts is not mistaken for one group.
    """
    if value is None:
        return None
    group = value if isinstance(value, str) else value.get("group")
    if not group:
        return None
    if env is not None:
        group = INPUT_REF.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), group)
    group = EXPRESSION.sub(lambda m: "${{" + "".join(m.group(0)[3:-2].split()) + "}}", group)
    return group.lower()


def runs_on_text(value):
    return value if isinstance(value, str) else json.dumps(value)


def fleet_policy(fleet_root):
    """Runner group name -> admitted workflow paths, or None for a group bounded by repository only.

    Read from the broker's CHECKS table, which the broker enforces on every registration: a group
    whose selected workflows fall outside it is refused, so it bounds what GitHub can admit.
    """
    text = (Path(fleet_root) / FLEET_POLICY).read_text()
    # Comments must not keep a deleted admission alive.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)(^|[^:'\\])//.*$", r"\1", text)
    start = text.index("export const CHECKS={")
    body = text[start : text.index("\n};", start)]
    prefixes = dict(re.findall(r"const (\w+)='([^']+)'", text))
    entries = re.split(r"\n\s*(?=\d+:expect\(|\[\w+\]:\{)", body)
    groups = {}
    for entry in entries[1:]:
        if match := re.match(r"\d+:expect\('([^']+)',", entry):
            files = set()
            lists = re.compile(r"main\((\w+),\[((?:\s*'[^']+'\s*,?)*)\]\)")
            for prefix, names in lists.findall(entry):
                for name in re.findall(r"'([^']+)'", names):
                    files.add(prefixes[prefix] + name + "@refs/heads/main")
            # Anything besides those lists, spreads, brackets and the repository argument is a
            # form this reader does not understand; refuse it rather than guess.
            rest = re.sub(r"\s+", "", lists.sub("", entry[match.end():]))
            rest = re.sub(r"'TauCetiProject/[\w.-]+'", "", rest)
            if re.sub(r"[\[\],.)]", "", rest):
                raise ValueError(f"unreadable Fleet runner policy entry: {entry[:80]!r}")
            groups[match.group(1)] = files
        elif match := re.match(r"\[\w+\]:\{name:'([^']+)',workflows:false", entry):
            groups[match.group(1)] = None
        else:
            raise ValueError(f"unreadable Fleet runner policy entry: {entry[:80]!r}")
    if not groups:
        raise ValueError("no runner groups found in the Fleet policy")
    return groups


class Checker:
    def __init__(self, tools_root, project_root, fleet_root, project=PROJECT_REPOSITORY):
        self.tools = Path(tools_root)
        self.project_root = Path(project_root)
        self.project = project
        self.contract = load(self.tools / CONTRACT)
        self.policy = fleet_policy(fleet_root)
        self.labels = self.contract.get("runner_labels") or {}
        self.errors = []
        self._cache = {}

    def fail(self, where, message):
        self.errors.append(f"{where}: {message}")

    def tools_workflow(self, name):
        if name not in self._cache:
            path = self.tools / ".github/workflows" / name
            self._cache[name] = load(path) if path.exists() else None
        return self._cache[name]

    def reusable(self):
        found = []
        for path in sorted((self.tools / ".github/workflows").glob("*.y*ml")):
            if "workflow_call" in triggers(load(path)):
                found.append(path.name)
        return found

    def check_manifest(self):
        entries = self.contract.get("workflows") or {}
        for name in self.reusable():
            if name not in entries:
                self.fail(CONTRACT, f"reusable workflow {name} has no entry")
        for name, entry in entries.items():
            workflow = self.tools_workflow(name)
            if workflow is None or "workflow_call" not in triggers(workflow):
                self.fail(CONTRACT, f"{name} is not a reusable workflow here")
                continue
            jobs = workflow.get("jobs") or {}
            for job in entry.get("jobs") or []:
                if job not in jobs:
                    self.fail(name, f"job {job!r}, which other repositories depend on, is gone")
            for group in entry.get("runner_groups") or []:
                if group not in self.policy:
                    self.fail(CONTRACT, f"{name} names runner group {group}, which Fleet does not know")
        for label, groups in self.labels.items():
            for group in groups:
                if group not in self.policy:
                    self.fail(CONTRACT, f"label {label} names runner group {group}, which Fleet does not know")

    def project_calls(self):
        """(caller file, job id, callee file) for every project job calling a Tools workflow."""
        calls = []
        for path in sorted((self.project_root / ".github/workflows").glob("*.y*ml")):
            workflow = load(path) or {}
            for job_id, job in (workflow.get("jobs") or {}).items():
                match = REMOTE_CALL.match(str((job or {}).get("uses", "")))
                if match and match.group(1) == TOOLS_REPOSITORY:
                    calls.append((path.name, job_id, match.group(2), match.group(3)))
        return calls

    def check_project(self):
        entries = self.contract.get("workflows") or {}
        seen = set()
        for caller_file, job_id, callee, ref in self.project_calls():
            where = f"{self.project} {caller_file} job {job_id}"
            seen.add((callee, caller_file))
            if ref != "main":
                self.fail(where, f"calls {callee}@{ref}; projects call Tools workflows at @main")
            entry = entries.get(callee)
            if entry is None or self.tools_workflow(callee) is None:
                self.fail(where, f"calls {callee}, which is not a contracted Tools workflow")
                continue
            declared = [c for c in (entry.get("callers") or {}).get(self.project, []) if c["file"] == caller_file]
            if not declared:
                self.fail(where, f"is not listed as a caller of {callee} in {CONTRACT}")
                continue
            self.check_call(caller_file, job_id, callee, declared[0])
        for callee, entry in entries.items():
            for caller in (entry.get("callers") or {}).get(self.project, []):
                if (callee, caller["file"]) not in seen and not caller.get("pending"):
                    self.fail(f"{self.project} {caller['file']}",
                              f"no longer calls {callee}; mark the entry pending or remove it")

    def check_call(self, caller_file, job_id, callee, declared):
        caller = load(self.project_root / ".github/workflows" / caller_file)
        job = caller["jobs"][job_id]
        where = f"{self.project} {caller_file} job {job_id}"
        if declared.get("job") != job_id:
            self.fail(where, f"the contract expects job {declared.get('job')!r} to call {callee}")
        if "name" in declared and caller.get("name") != declared["name"]:
            self.fail(where, f"workflow name {caller.get('name')!r}; the contract expects {declared['name']!r}")
        events = triggers(caller)
        for event in declared.get("triggers") or []:
            if event not in events:
                self.fail(where, f"lacks the {event} trigger the contract requires")
        ceiling = permissions(job.get("permissions"))
        if ceiling is None:
            ceiling = permissions(caller.get("permissions"))
        if ceiling is None:
            self.fail(where, "sets no permissions; a caller must state the ceiling it grants")
            ceiling = {OTHERS: 0}
        caller_groups = {g for g in (concurrency_group(caller.get("concurrency")),
                                     concurrency_group(job.get("concurrency"))) if g}
        inputs = {name: str(value) for name, value in (job.get("with") or {}).items()}
        self.check_interface(where, job, callee)
        # `needs.<job>.outputs.<name>` read by the caller must be declared outputs of the callee.
        outputs = (triggers(self.tools_workflow(callee))["workflow_call"] or {}).get("outputs") or {}
        text = (self.project_root / ".github/workflows" / caller_file).read_text()
        for name in re.findall(rf"needs\.{re.escape(job_id)}\.outputs\.([\w-]+)", text):
            if name not in outputs:
                self.fail(where, f"reads output {name!r}, which {callee} does not declare")
        self.walk(callee, ceiling, inputs, caller_groups, [where])

    def check_interface(self, where, job, callee):
        """Inputs and secrets a calling job passes against what the callee declares."""
        call = triggers(self.tools_workflow(callee))["workflow_call"] or {}
        declared_inputs = call.get("inputs") or {}
        passed = job.get("with") or {}
        for name, value in passed.items():
            spec = declared_inputs.get(name)
            if spec is None:
                self.fail(where, f"passes input {name!r}, which {callee} does not declare")
                continue
            kind = spec.get("type")
            literal = not (isinstance(value, str) and "${{" in value)
            if literal and kind == "boolean" and not isinstance(value, bool):
                self.fail(where, f"passes {value!r} to boolean input {name!r}")
            if literal and kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                self.fail(where, f"passes {value!r} to number input {name!r}")
        for name, spec in declared_inputs.items():
            if (spec or {}).get("required") and "default" not in spec and name not in passed:
                self.fail(where, f"does not pass required input {name!r} of {callee}")
        declared_secrets = call.get("secrets") or {}
        secrets = job.get("secrets")
        if secrets == "inherit":
            self.fail(where, "uses `secrets: inherit`; pass each secret the callee declares")
            secrets = {}
        secrets = secrets or {}
        for name in secrets:
            if name not in declared_secrets:
                self.fail(where, f"passes secret {name!r}, which {callee} does not declare")
        for name, spec in declared_secrets.items():
            if (spec or {}).get("required") and name not in secrets:
                self.fail(where, f"does not pass required secret {name!r} of {callee}")

    def walk(self, callee, ceiling, inputs, outer_groups, trail):
        """Check a Tools workflow called with `ceiling` and `inputs`, and every workflow it calls."""
        if len(trail) >= 10:  # the caller plus nine levels of reusable workflows
            self.fail(" -> ".join(trail), "nests reusable workflows more than GitHub allows")
            return
        workflow = self.tools_workflow(callee)
        here = " -> ".join([*trail, callee])
        top = permissions(workflow.get("permissions"))
        if top is not None and (extra := exceeding(top, ceiling)):
            self.fail(here, f"asks for {show(extra)} beyond the caller's ceiling ({show(ceiling)})")
        call = triggers(workflow)["workflow_call"] or {}
        defaults = {name: str(spec.get("default", "")) for name, spec in (call.get("inputs") or {}).items()}
        env = {**defaults, **inputs}
        own = concurrency_group(workflow.get("concurrency"), env)
        if own and own in outer_groups:
            self.fail(here, "shares its workflow concurrency group with a caller; the run would wait on itself")
        groups = outer_groups | ({own} if own else set())
        entry = (self.contract.get("workflows") or {}).get(callee) or {}
        for job_id, job in (workflow.get("jobs") or {}).items():
            job = job or {}
            where = f"{here} job {job_id}"
            asked = permissions(job.get("permissions"))
            effective = asked if asked is not None else (top if top is not None else ceiling)
            if asked is not None and (extra := exceeding(asked, ceiling)):
                self.fail(where, f"asks for {show(extra)} beyond the caller's ceiling ({show(ceiling)})")
            group = concurrency_group(job.get("concurrency"), env)
            if group and group in groups:
                self.fail(where, "shares a concurrency group with a calling workflow; the run would wait on itself")
            uses = job.get("uses")
            if uses:
                local = LOCAL_CALL.match(str(uses))
                remote = REMOTE_CALL.match(str(uses))
                if remote and remote.group(1) == TOOLS_REPOSITORY:
                    nested = remote.group(2)
                elif local:
                    nested = local.group(1)
                else:
                    continue  # another repository's workflow; its own CI owns that contract
                if self.tools_workflow(nested) is None:
                    self.fail(where, f"calls {nested}, which does not exist")
                    continue
                self.check_interface(where, job, nested)
                passed = {name: self.substitute(str(value), env) for name, value in (job.get("with") or {}).items()}
                held = groups | ({group} if group else set())
                self.walk(nested, effective, passed, held, [*trail, f"{callee} job {job_id}"])
                continue
            self.check_runner(where, callee, entry, job.get("runs-on"), env)

    @staticmethod
    def substitute(text, env):
        """Text with each `inputs.<name>` it reads replaced by that input's value, when known."""
        def value(match):
            name = match.group(1) or match.group(2)
            return env[name] if env.get(name) else match.group(0)
        # `${{ inputs.x }}` alone is the value itself; inside a larger expression, its text.
        whole = re.compile(r"\$\{\{\s*(" + INPUT_REF.pattern + r")\s*\}\}")
        text = whole.sub(lambda m: value(INPUT_REF.match(m.group(1))) if env.get(
            m.group(2) or m.group(3)) else m.group(0), text)
        return INPUT_REF.sub(value, text)

    def check_runner(self, where, callee, entry, runs_on, env):
        if runs_on is None:
            self.fail(where, "has no runs-on")
            return
        if isinstance(runs_on, dict):
            group = self.substitute(str(runs_on.get("group", "")), env)
            runs_on = {**runs_on, "group": group}
        text = self.substitute(runs_on_text(runs_on), env)
        reachable = set(GROUP_REF.findall(text))
        for label, groups in self.labels.items():
            if re.search(rf"(?<![\w-]){re.escape(label)}(?![\w-])", text):
                reachable.update(groups)
        # A runner chosen from data the text does not show: a picker's output, a matrix value, a
        # variable parsed as JSON or used as a group, or an input with no known value.
        unresolved = (
            re.search(r"\bneeds\.[\w-]+\.outputs\b|\bneeds\[|\bmatrix\b|fromJSON\(\s*vars\b", text)
            or INPUT_REF.search(text)
            or isinstance(runs_on, dict) and "${{" in runs_on["group"]
        )
        if unresolved:
            declared = entry.get("runner_groups")
            if declared is None:
                self.fail(where, f"picks its runner at run time; list the groups it may reach as "
                                 f"runner_groups of {callee} in {CONTRACT}")
            reachable.update(declared or [])
        path = f"{TOOLS_REPOSITORY}/.github/workflows/{callee}@refs/heads/main"
        for group in sorted(reachable):
            if group not in self.policy:
                self.fail(where, f"can run in runner group {group}, which Fleet does not know")
            elif self.policy[group] is not None and path not in self.policy[group]:
                self.fail(where, f"can run in runner group {group}, whose Fleet policy does not admit {path}; "
                                 "the job would queue forever (add it to the Fleet CHECKS and the group first)")

    def run(self):
        self.check_manifest()
        self.check_project()
        return self.errors


def main(argv):
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    errors = Checker(TOOLS_ROOT, argv[1], argv[2]).run()
    for error in errors:
        print(f"::error::{error}")
    print(f"{len(errors)} contract violation(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
