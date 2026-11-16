"""One shared-harness revision for project CI, and the runner-group policy derived from it.

The project's `.tauceti/ci-source.json` (`harness.name`, `harness_revision`) is the only
source of truth for the TauCetiTools revision its CI runs. GitHub requires literal SHAs in
`uses:`, so the literals in the bound workflows are derived from it (`set`) and verified
against it (`literals`). A bound workflow is one named by the caller or one that calls a
harness reusable workflow.

Organization runner groups restricted to selected workflows admit a job only when the
workflow that defines the job is listed at the exact ref it was called with. Every
harness-defined job reached from the project's callers is evaluated here: its `runs-on`
is computed from the caller's `with:` values and the reusable workflow's input defaults,
and mapped to the restricted runner groups that could serve it. The resulting
`<harness>/.github/workflows/<file>@<sha>` entries are the only ones `reconcile` adds or
removes; every other allow-list entry is left alone.

`reconcile --pr N` admits a pull request's pins before it can merge and reports whether
they are admitted. `reconcile --prune` (post-merge and scheduled) admits main's pins and
removes harness entries that are older than main's revision, unless main, its previous
revision or a run that has not completed still needs them.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HEX40 = re.compile(r"[0-9a-f]{40}")
SOURCE = ".tauceti/ci-source.json"
WORKFLOWS = ".github/workflows"
HOSTED = re.compile(r"^(?:ubuntu|windows|macos)-[A-Za-z0-9_.-]+$", re.IGNORECASE)
UNFINISHED_RUNS = ("queued", "in_progress", "waiting", "pending", "requested")


class DerivationError(Exception):
    """The required runner-group entries cannot be derived; never guess."""


# ---------------------------------------------------------------------------
# Source of truth and literal pins


def harness(source_text):
    """Return (harness repository, revision) from the project's ci-source.json text."""
    try:
        value = json.loads(source_text)
        repository = value["harness"]["name"]
        revision = value["harness_revision"]
    except (ValueError, TypeError, KeyError) as error:
        raise DerivationError(f"{SOURCE} does not name a harness repository and revision") from error
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise DerivationError(f"{SOURCE} harness.name is not an owner/repository name")
    if not isinstance(revision, str) or not HEX40.fullmatch(revision):
        raise DerivationError(f"{SOURCE} harness_revision is not a full lowercase commit SHA")
    return repository, revision


def _site_patterns(repository):
    return (
        re.compile(rf"({re.escape(repository)}/\.github/(?:actions|workflows)/[^@\s'\"]+@)([0-9A-Za-z._/-]+)"),
        re.compile(r"^(\s*tools[-_]ref:\s*['\"]?)([0-9A-Za-z._/-]+)", re.MULTILINE),
    )


def literal_sites(text, repository):
    """Every harness pin literal in a workflow: (line number, value)."""
    sites = []
    for pattern in _site_patterns(repository):
        for match in pattern.finditer(text):
            sites.append((text.count("\n", 0, match.start(2)) + 1, match.group(2)))
    return sorted(sites)


def _reusable_call(uses, repository):
    match = re.fullmatch(rf"{re.escape(repository)}/(\.github/workflows/[^@]+)@(\S+)", str(uses or "").strip())
    return match and (match.group(1), match.group(2))


def _load_yaml(text, name):
    import yaml  # PyYAML is present on GitHub-hosted runners and in the test extra.

    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise DerivationError(f"{name} is not valid YAML") from error
    if not isinstance(value, dict):
        raise DerivationError(f"{name} is not a workflow mapping")
    return value


def callers(workflows, repository):
    """Project workflows with a job that calls a harness reusable workflow."""
    found = set()
    for name, text in workflows.items():
        jobs = _load_yaml(text, name).get("jobs") or {}
        if any(isinstance(job, dict) and _reusable_call(job.get("uses"), repository) for job in jobs.values()):
            found.add(name)
    return found


def check_literals(source_text, workflows, bound):
    """Errors for bound workflows whose harness literals differ from the source of truth."""
    repository, revision = harness(source_text)
    errors = []
    required = set(bound) | callers(workflows, repository)
    for name in sorted(required):
        if name not in workflows:
            errors.append(f"{WORKFLOWS}/{name}: bound workflow is missing")
            continue
        sites = literal_sites(workflows[name], repository)
        if not sites:
            errors.append(f"{WORKFLOWS}/{name}: no {repository} pin found in a bound workflow")
        for line, value in sites:
            if value != revision:
                errors.append(f"{WORKFLOWS}/{name}:{line}: pins {value}, but {SOURCE} harness_revision is {revision}")
    return errors


def set_revision(root, revision, bound):
    """Rewrite the source of truth and derive every bound literal from it. Returns changed paths."""
    if not HEX40.fullmatch(revision):
        raise DerivationError("the harness revision must be a full lowercase commit SHA")
    root = Path(root)
    source = root / SOURCE
    text = source.read_text()
    repository, _ = harness(text)
    updated, count = re.subn(r'("harness_revision"\s*:\s*")[0-9a-f]{40}(")', rf"\g<1>{revision}\g<2>", text)
    if count != 1:
        raise DerivationError(f"{SOURCE} must contain exactly one harness_revision")
    changed = []
    if updated != text:
        source.write_text(updated)
        changed.append(source)
    workflows = {p.name: p.read_text() for p in sorted((root / WORKFLOWS).glob("*.y*ml"))}
    for name in sorted(set(bound) | callers(workflows, repository)):
        path = root / WORKFLOWS / name
        text = new = path.read_text()
        for pattern in _site_patterns(repository):
            new = pattern.sub(lambda m: m.group(1) + revision, new)
        if new != text:
            path.write_text(new)
            changed.append(path)
    return changed


# ---------------------------------------------------------------------------
# GitHub expression evaluation over sets of possible values


class Unknown:
    """A value the static derivation cannot know, with the truthiness it may have."""

    def __init__(self, truthy=True, falsy=True):
        self.truthy, self.falsy = truthy, falsy

    def __repr__(self):
        return f"Unknown(truthy={self.truthy}, falsy={self.falsy})"


ANY, TRUTHY, FALSY = Unknown(), Unknown(True, False), Unknown(False, True)


def _key(value):
    if isinstance(value, Unknown):
        return ("?", value.truthy, value.falsy)
    return ("=", json.dumps(value, sort_keys=True))


def values(*items):
    return {_key(item): item for item in items}


def _truthy(value):
    if isinstance(value, float) and value != value:
        return False
    return value not in (False, 0, "", None) or isinstance(value, (list, dict))


_TOKEN = re.compile(
    r"\s*(?:(?P<string>'(?:[^']|'')*')|(?P<number>-?(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?))"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_-]*)|(?P<op>&&|\|\||==|!=|<=|>=|[<>!()\[\],.*]))"
)


def _tokens(expression):
    position, found = 0, []
    while position < len(expression):
        if expression[position:].strip() == "":
            break
        match = _TOKEN.match(expression, position)
        if not match:
            raise DerivationError(f"unsupported expression syntax: {expression!r}")
        kind = match.lastgroup
        found.append((kind, match.group(kind)))
        position = match.end()
    return found


class _Parser:
    def __init__(self, expression):
        self.tokens, self.index = _tokens(expression), 0

    def peek(self, *ops):
        if self.index < len(self.tokens) and self.tokens[self.index][0] == "op" and self.tokens[self.index][1] in ops:
            return self.tokens[self.index][1]
        return None

    def take(self, op):
        if not self.peek(op):
            raise DerivationError(f"expected {op!r} in expression")
        self.index += 1

    def parse(self):
        node = self.disjunction()
        if self.index != len(self.tokens):
            raise DerivationError("unexpected trailing expression tokens")
        return node

    def disjunction(self):
        node = self.conjunction()
        while self.peek("||"):
            self.index += 1
            node = ("or", node, self.conjunction())
        return node

    def conjunction(self):
        node = self.equality()
        while self.peek("&&"):
            self.index += 1
            node = ("and", node, self.equality())
        return node

    def equality(self):
        node = self.comparison()
        while op := self.peek("==", "!="):
            self.index += 1
            node = ("cmp", op, node, self.comparison())
        return node

    def comparison(self):
        node = self.unary()
        while op := self.peek("<", ">", "<=", ">="):
            self.index += 1
            node = ("cmp", op, node, self.unary())
        return node

    def unary(self):
        if self.peek("!"):
            self.index += 1
            return ("not", self.unary())
        return self.postfix()

    def postfix(self):
        node = self.primary()
        while True:
            if self.peek("."):
                self.index += 1
                kind, text = self.tokens[self.index] if self.index < len(self.tokens) else (None, None)
                if kind != "name" and text != "*":
                    raise DerivationError("expected a property name")
                self.index += 1
                node = ("member", node, text)
            elif self.peek("["):
                self.index += 1
                index = self.disjunction()
                self.take("]")
                node = ("index", node, index)
            else:
                return node

    def primary(self):
        if self.index >= len(self.tokens):
            raise DerivationError("incomplete expression")
        kind, text = self.tokens[self.index]
        self.index += 1
        if kind == "string":
            return ("lit", text[1:-1].replace("''", "'"))
        if kind == "number":
            return (
                "lit",
                int(text, 16)
                if text.lower().startswith(("0x", "-0x"))
                else float(text)
                if "." in text or "e" in text.lower()
                else int(text),
            )
        if kind == "op" and text == "(":
            node = self.disjunction()
            self.take(")")
            return node
        if kind == "name":
            lowered = text.lower()
            if lowered in ("true", "false"):
                return ("lit", lowered == "true")
            if lowered == "null":
                return ("lit", None)
            if self.peek("("):
                self.index += 1
                arguments = []
                if not self.peek(")"):
                    arguments.append(self.disjunction())
                    while self.peek(","):
                        self.index += 1
                        arguments.append(self.disjunction())
                self.take(")")
                return ("call", lowered, arguments)
            return ("context", text)
        raise DerivationError(f"unexpected expression token {text!r}")


def _evaluate(node, context):
    kind = node[0]
    if kind == "lit":
        return values(node[1])
    if kind == "context":
        # A whole context object is never a usable runner target.
        return values(ANY)
    if kind in ("member", "index"):
        names = values(node[2]) if kind == "member" else _evaluate(node[2], context)
        if node[1][0] == "context":
            scope = context.get(node[1][1].lower())
            found = {}
            for name in names.values():
                if scope is None or not isinstance(name, str):
                    found.update(values(ANY))
                    continue
                match = [v for k, v in scope.items() if k.lower() == name.lower()]
                found.update(match[0] if match else values(None))
            return found
        found = {}
        for parent in _evaluate(node[1], context).values():
            for name in names.values():
                if isinstance(parent, Unknown) or isinstance(name, Unknown):
                    found.update(values(ANY))
                elif isinstance(parent, dict) and isinstance(name, str):
                    match = [v for k, v in parent.items() if k.lower() == name.lower()]
                    found.update(values(match[0] if match else None))
                elif isinstance(parent, list) and isinstance(name, (int, float)) and 0 <= int(name) < len(parent):
                    found.update(values(parent[int(name)]))
                else:
                    found.update(values(None))
        return found
    if kind == "not":
        found = {}
        for value in _evaluate(node[1], context).values():
            if isinstance(value, Unknown):
                found.update(values(*([False] if value.truthy else []), *([True] if value.falsy else [])))
            else:
                found.update(values(not _truthy(value)))
        return found
    if kind in ("and", "or"):
        found = {}
        for value in _evaluate(node[1], context).values():
            if isinstance(value, Unknown):
                passes = value.truthy if kind == "and" else value.falsy
                keeps = value.falsy if kind == "and" else value.truthy
                if keeps:
                    found.update(values(FALSY if kind == "and" else TRUTHY))
                if passes:
                    found.update(_evaluate(node[2], context))
            elif _truthy(value) == (kind == "and"):
                found.update(_evaluate(node[2], context))
            else:
                found.update(values(value))
        return found
    if kind == "cmp":
        found = {}
        op, left, right = node[1], _evaluate(node[2], context), _evaluate(node[3], context)
        for a in left.values():
            for b in right.values():
                if isinstance(a, Unknown) or isinstance(b, Unknown):
                    found.update(values(True, False))
                    continue
                if isinstance(a, str) and isinstance(b, str):
                    a, b = a.casefold(), b.casefold()
                try:
                    result = {"==": a == b, "!=": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
                except TypeError:
                    result = op == "!="
                found.update(values(result))
        return found
    if kind == "call":
        name, arguments = node[1], [_evaluate(argument, context) for argument in node[2]]
        if name == "fromjson" and len(arguments) == 1:
            found = {}
            for value in arguments[0].values():
                if isinstance(value, Unknown) or not isinstance(value, str):
                    found.update(values(ANY))
                    continue
                try:
                    found.update(values(json.loads(value)))
                except ValueError as error:
                    raise DerivationError(f"fromJSON of invalid JSON {value!r}") from error
            return found
        if name == "tojson" and len(arguments) == 1:
            return {
                **{_key(json.dumps(v)): json.dumps(v) for v in arguments[0].values() if not isinstance(v, Unknown)},
                **({_key(TRUTHY): TRUTHY} if any(isinstance(v, Unknown) for v in arguments[0].values()) else {}),
            }
        return values(ANY)
    raise DerivationError(f"unsupported expression node {kind}")


def evaluate_expression(expression, context):
    return _evaluate(_Parser(expression).parse(), context)


_EMBEDDED = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def evaluate_value(raw, context):
    """All values a workflow field may take. Lists and mappings are evaluated element-wise."""
    if isinstance(raw, list):
        combined = [[]]
        for item in raw:
            options = list(evaluate_value(item, context).values())
            combined = [prefix + [option] for prefix in combined for option in options]
            if len(combined) > 256:
                raise DerivationError("too many possible runner targets")
        return {_key(v): v for v in combined if not any(isinstance(x, Unknown) for x in v)} | (
            values(ANY) if any(any(isinstance(x, Unknown) for x in v) for v in combined) else {}
        )
    if isinstance(raw, dict):
        combined = [{}]
        for name, item in raw.items():
            options = list(evaluate_value(item, context).values())
            combined = [{**prefix, name: option} for prefix in combined for option in options]
            if len(combined) > 256:
                raise DerivationError("too many possible runner targets")
        known = [v for v in combined if not any(isinstance(x, Unknown) for x in v.values())]
        return {_key(v): v for v in known} | (values(ANY) if len(known) != len(combined) else {})
    if not isinstance(raw, str) or "${{" not in raw:
        return values(raw)
    whole = re.fullmatch(r"\s*\$\{\{(.*?)\}\}\s*", raw, re.DOTALL)
    if whole and "${{" not in whole.group(1):
        return evaluate_expression(whole.group(1), context)
    pieces = [[""]]
    for index, part in enumerate(_EMBEDDED.split(raw)):
        if index % 2 == 0:
            pieces = [[prefix[0] + part] for prefix in pieces]
            continue
        options = evaluate_expression(part, context).values()
        if any(isinstance(v, Unknown) for v in options):
            return values(TRUTHY if raw.replace("${{" + part + "}}", "").strip() else ANY)
        pieces = [[prefix[0] + _as_string(v)] for prefix in pieces for v in options]
    return values(*(p[0] for p in pieces))


def _as_string(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def _coerce(options, kind):
    found = {}
    for value in options.values():
        if isinstance(value, Unknown):
            found.update(values(value))
        elif kind == "boolean" and isinstance(value, str) and value.lower() in ("true", "false"):
            found.update(values(value.lower() == "true"))
        elif kind == "string" and not isinstance(value, str):
            found.update(values(_as_string(value)))
        else:
            found.update(values(value))
    return found


# ---------------------------------------------------------------------------
# Runner targets reached from the project's harness callers


@dataclass(frozen=True)
class Need:
    """A harness-defined job (by its defining workflow entry) and one runner target it may use."""

    entry: str
    job: str
    target: tuple  # ("group", name, labels) | ("labels", labels)


def _target(value, where):
    if isinstance(value, Unknown):
        raise DerivationError(f"{where}: runs-on depends on values that cannot be derived statically")
    group, labels = None, value
    if isinstance(value, dict):
        if set(value) - {"group", "labels"}:
            raise DerivationError(f"{where}: unsupported runs-on keys {sorted(set(value) - {'group', 'labels'})}")
        group, labels = value.get("group"), value.get("labels", [])
    if isinstance(labels, str):
        labels = [labels]
    if not isinstance(labels, list) or not all(isinstance(label, str) and label for label in labels):
        raise DerivationError(f"{where}: runs-on labels are not strings")
    if group is not None:
        if not isinstance(group, str) or not group:
            raise DerivationError(f"{where}: runs-on group is not a name")
        return ("group", group, tuple(labels))
    if not labels:
        raise DerivationError(f"{where}: runs-on is empty")
    if all(HOSTED.fullmatch(label) for label in labels):
        return None
    return ("labels", tuple(labels))


def workflow_needs(fetch, repository, path, revision, provided, *, depth=0):
    """Needs of every job in a harness reusable workflow called with `provided` input value sets."""
    if depth > 4:
        raise DerivationError("reusable workflow nesting is deeper than GitHub permits")
    entry = f"{repository}/{path}@{revision}"
    document = _load_yaml(fetch(repository, path, revision), entry)
    trigger = document.get("on", document.get(True))
    reusable = trigger == "workflow_call" or (isinstance(trigger, (list, dict)) and "workflow_call" in trigger)
    if not reusable:
        raise DerivationError(f"{entry} is not a reusable workflow")
    call = trigger.get("workflow_call") if isinstance(trigger, dict) else None
    declared = (call or {}).get("inputs") or {}
    inputs = {}
    for name, spec in declared.items():
        spec = spec or {}
        if name in provided:
            inputs[name] = _coerce(provided[name], spec.get("type", "string"))
        elif "default" in spec:
            inputs[name] = _coerce(evaluate_value(spec["default"], {}), spec.get("type", "string"))
        else:
            inputs[name] = values(ANY)
    context = {"inputs": inputs}
    needs = set()
    for job_name, job in (document.get("jobs") or {}).items():
        if not isinstance(job, dict):
            raise DerivationError(f"{entry} job {job_name} is not a mapping")
        if "uses" in job:
            uses = str(job["uses"]).strip()
            if not uses.startswith("./.github/workflows/"):
                raise DerivationError(
                    f"{entry} job {job_name} calls {uses}; only same-revision nested calls are derivable"
                )
            nested = {name: evaluate_value(value, context) for name, value in (job.get("with") or {}).items()}
            needs |= workflow_needs(fetch, repository, uses[2:], revision, nested, depth=depth + 1)
            continue
        if "runs-on" not in job:
            raise DerivationError(f"{entry} job {job_name} has no runs-on")
        for value in evaluate_value(job["runs-on"], context).values():
            target = _target(value, f"{entry} job {job_name}")
            if target:
                needs.add(Need(entry, job_name, target))
    return needs


def project_needs(workflows, repository, fetch):
    """Needs reached from every project workflow job that calls a harness reusable workflow."""
    needs = set()
    for name in sorted(workflows):
        for job_name, job in (_load_yaml(workflows[name], name).get("jobs") or {}).items():
            call = isinstance(job, dict) and _reusable_call(job.get("uses"), repository)
            if not call:
                continue
            path, revision = call
            if not HEX40.fullmatch(revision):
                raise DerivationError(
                    f"{WORKFLOWS}/{name} job {job_name} calls {repository} at {revision}, not a full SHA"
                )
            # A caller is a top-level workflow: none of its own contexts are known statically.
            provided = {key: evaluate_value(value, {}) for key, value in (job.get("with") or {}).items()}
            needs |= workflow_needs(fetch, repository, path, revision, provided)
    return needs


# ---------------------------------------------------------------------------
# Runner groups


@dataclass
class Group:
    id: int
    name: str
    workflows: list
    runner_labels: list = field(default_factory=list)


def managed_entry(entry, repository):
    match = re.fullmatch(rf"{re.escape(repository)}/(\.github/workflows/[^@]+)@([0-9a-f]{{40}})", entry)
    return match and (match.group(1), match.group(2))


def resolve(needs, groups, repository):
    """Map each need to the restricted groups that may serve it: {group id: {entry}}."""
    required = {}
    by_name = {group.name.casefold(): group for group in groups}
    for need in sorted(needs, key=lambda n: (n.entry, n.job, n.target)):
        if need.target[0] == "group":
            group = by_name.get(need.target[1].casefold())
            # An unrestricted or inaccessible group needs no allow-list entry.
            chosen = [group] if group else []
        else:
            labels = {label.casefold() for label in need.target[1]}
            chosen = [g for g in groups if any(labels <= {x.casefold() for x in r} for r in g.runner_labels)]
            if not chosen:
                # No runner is registered right now: keep serving the groups that already
                # admit this harness workflow at some revision.
                path = managed_entry(need.entry, repository)[0]
                chosen = [
                    g for g in groups if any((managed_entry(w, repository) or ("",))[0] == path for w in g.workflows)
                ]
            if not chosen:
                raise DerivationError(
                    f"{need.entry} job {need.job} targets labels {list(need.target[1])} that no restricted "
                    "runner group serves; name the group in runs-on or admit it once by hand"
                )
        for group in chosen:
            required.setdefault(group.id, set()).add(need.entry)
    return required


def missing(required, groups):
    current = {group.id: set(group.workflows) for group in groups}
    return {
        gid: sorted(entries - current.get(gid, set()))
        for gid, entries in required.items()
        if entries - current.get(gid, set())
    }


# ---------------------------------------------------------------------------
# GitHub access


class ApiError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class GitHub:
    def __init__(self, token, base="https://api.github.com"):
        self.token, self.base = token, base.rstrip("/")

    def request(self, method, path, data=None):
        body = None if data is None else json.dumps(data).encode()
        url = path if path.startswith("https://") else f"{self.base}/{path.lstrip('/')}"
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "tauceti-harness-pins")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as error:
            raise ApiError(error.code, f"GitHub {method} {path} returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ApiError(0, f"GitHub {method} {path} failed: {error}") from None
        return json.loads(text) if text else None

    def get(self, path):
        return self.request("GET", path)

    def patch(self, path, data):
        return self.request("PATCH", path, data)


def pages(api, path, key=None, *, limit=50):
    found = []
    for page in range(1, limit + 1):
        value = api.get(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
        rows = value if key is None else (value or {}).get(key)
        if not isinstance(rows, list):
            raise ApiError(0, f"incomplete listing from {path}")
        found.extend(rows)
        if len(rows) < 100:
            return found
    raise ApiError(0, f"listing from {path} exceeds {limit} pages")


class Repositories:
    """Cached reads of project and harness files at immutable revisions."""

    def __init__(self, api, project, repository):
        self.api, self.project, self.repository = api, project, repository
        self.blobs, self.files, self.trees, self.order, self.on_branch = {}, {}, {}, {}, {}

    def blob(self, repository, oid):
        if (repository, oid) not in self.blobs:
            value = self.api.get(f"repos/{repository}/git/blobs/{oid}")
            self.blobs[repository, oid] = base64.b64decode(value["content"]).decode()
        return self.blobs[repository, oid]

    def file(self, repository, path, revision):
        if (repository, path, revision) not in self.files:
            quoted = urllib.parse.quote(path)
            try:
                value = self.api.get(f"repos/{repository}/contents/{quoted}?ref={revision}")
            except ApiError as error:
                if error.status == 404:
                    raise DerivationError(f"{repository}/{path}@{revision} does not exist") from None
                raise
            if not isinstance(value, dict) or value.get("type") != "file":
                raise DerivationError(f"{repository}/{path}@{revision} is not a file")
            self.files[repository, path, revision] = self.blob(repository, value["sha"])
        return self.files[repository, path, revision]

    def harness_file(self, repository, path, revision):
        return self.file(repository, path, revision)

    def workflows(self, revision):
        if revision not in self.trees:
            listing = self.api.get(f"repos/{self.project}/contents/{WORKFLOWS}?ref={revision}")
            if not isinstance(listing, list):
                raise DerivationError(f"{WORKFLOWS} at {revision} is not a directory")
            self.trees[revision] = {
                item["name"]: self.blob(self.project, item["sha"])
                for item in listing
                if item.get("type") == "file" and item["name"].endswith((".yml", ".yaml"))
            }
        return self.trees[revision]

    def source(self, revision):
        return self.file(self.project, SOURCE, revision)

    def needs(self, revision):
        return project_needs(self.workflows(revision), self.repository, self.harness_file)

    def compare(self, base, head):
        if (base, head) not in self.order:
            try:
                self.order[base, head] = self.api.get(f"repos/{self.repository}/compare/{base}...{head}?per_page=1")[
                    "status"
                ]
            except ApiError as error:
                if error.status != 404:
                    raise
                self.order[base, head] = "unknown"
        return self.order[base, head]

    def reviewed(self, revision):
        """Whether a harness revision is on the harness repository's default branch."""
        if revision not in self.on_branch:
            branch = self.api.get(f"repos/{self.repository}")["default_branch"]
            self.on_branch[revision] = self.compare(revision, branch) in ("ahead", "identical")
        return self.on_branch[revision]


def load_groups(api, organization, project):
    """Restricted runner groups that can serve the project repository, with runner labels."""
    repository = api.get(f"repos/{project}")
    public = not repository.get("private")
    groups = []
    for row in pages(api, f"orgs/{organization}/actions/runner-groups", "runner_groups"):
        if not row.get("restricted_to_workflows"):
            continue
        if public and not row.get("allows_public_repositories"):
            continue
        if row.get("visibility") == "private" and public:
            continue
        if row.get("visibility") == "selected":
            selected = pages(api, f"orgs/{organization}/actions/runner-groups/{row['id']}/repositories", "repositories")
            if repository["id"] not in {r.get("id") for r in selected}:
                continue
        runners = pages(api, f"orgs/{organization}/actions/runner-groups/{row['id']}/runners", "runners")
        groups.append(
            Group(
                id=row["id"],
                name=row["name"],
                workflows=list(row.get("selected_workflows") or []),
                runner_labels=[[label["name"] for label in runner.get("labels") or []] for runner in runners],
            )
        )
    return groups


def apply(api, organization, changes, *, dry_run=False):
    """Re-read each group immediately before writing so concurrent edits are not overwritten."""
    written = {}
    for gid, (add, remove) in sorted(changes.items()):
        if not add and not remove:
            continue
        fresh = api.get(f"orgs/{organization}/actions/runner-groups/{gid}")
        if not fresh.get("restricted_to_workflows"):
            raise DerivationError(f"runner group {gid} is no longer restricted to selected workflows")
        current = list(fresh.get("selected_workflows") or [])
        updated = [w for w in current if w not in remove] + sorted(set(add) - set(current))
        if updated == current:
            continue
        written[gid] = updated
        if not dry_run:
            api.patch(
                f"orgs/{organization}/actions/runner-groups/{gid}",
                {"name": fresh["name"], "restricted_to_workflows": True, "selected_workflows": updated},
            )
            check = set(api.get(f"orgs/{organization}/actions/runner-groups/{gid}").get("selected_workflows") or [])
            if not set(add) <= check:
                raise ApiError(0, f"runner group {gid} did not retain the admitted entries")
    return written


# ---------------------------------------------------------------------------
# Reconciliation


def _admit(needs, groups, repos, *, untrusted):
    """Entries to add so that every need is admitted, refusing anything unsafe to add."""
    repository = repos.repository
    required = resolve(needs, groups, repository)
    managed = {g.id for g in groups if any(managed_entry(w, repository) for w in g.workflows)}
    names = {g.id: g.name for g in groups}
    add, errors = {}, []
    for gid, entries in missing(required, groups).items():
        if gid not in managed:
            errors.append(
                f"runner group {names[gid]} has no {repository} entry yet; an organization owner must admit "
                f"{entries[0]} once by hand before it is managed here"
            )
            continue
        for entry in entries:
            path, revision = managed_entry(entry, repository)
            if not repos.reviewed(revision):
                errors.append(
                    f"{entry} is not on the {repository} default branch"
                    + (" (untrusted pull request)" if untrusted else "")
                )
                continue
            add.setdefault(gid, set()).add(entry)
    return required, add, errors


def unfinished_heads(api, project, workflows, repository):
    """Project revisions of unfinished runs of every harness caller workflow."""
    heads = set()
    for name in sorted(callers(workflows, repository)):
        for status in UNFINISHED_RUNS:
            rows = pages(
                api, f"repos/{project}/actions/workflows/{name}/runs?status={status}", "workflow_runs", limit=10
            )
            heads |= {row["head_sha"] for row in rows if HEX40.fullmatch(str(row.get("head_sha")))}
    return heads


def reconcile(api, project, *, branch="main", pr=None, prune=False, bound=(), dry_run=False):
    organization = project.split("/")[0]
    head = api.get(f"repos/{project}/commits/{branch}")
    main, parents = head["sha"], [p["sha"] for p in head.get("parents") or []]
    probe = Repositories(api, project, "")
    repository, revision = harness(probe.source(main))
    repos = Repositories(api, project, repository)
    repos.blobs, repos.files, repos.trees = probe.blobs, probe.files, probe.trees
    report = {"main": main, "harness": repository, "revision": revision, "errors": [], "written": {}}
    main_workflows = repos.workflows(main)
    report["errors"] += [f"main: {e}" for e in check_literals(repos.source(main), main_workflows, bound)]
    groups = load_groups(api, organization, project)
    needs = repos.needs(main)
    required, add, errors = _admit(needs, groups, repos, untrusted=False)
    report["errors"] += errors
    retain = {gid: set(entries) for gid, entries in required.items()}

    if pr is not None:
        pull = api.get(f"repos/{project}/pulls/{int(pr)}")
        head_sha = pull["head"]["sha"]
        report["pr"] = {"number": int(pr), "head": head_sha}
        problems = []
        try:
            pr_repository, _ = harness(repos.source(head_sha))
            if pr_repository != repository:
                raise DerivationError(f"the pull request changes the harness repository to {pr_repository}")
            problems += check_literals(repos.source(head_sha), repos.workflows(head_sha), bound)
            base = api.get(f"repos/{project}/compare/{pull['base']['sha']}...{head_sha}?per_page=1")[
                "merge_base_commit"
            ]["sha"]
            pr_needs = repos.needs(head_sha)
            changed = pr_needs != repos.needs(base)
            report["pr"]["changes_pins"] = changed
            if changed:
                pr_required, pr_add, pr_errors = _admit(pr_needs, groups, repos, untrusted=True)
                problems += pr_errors
                for gid, entries in pr_add.items():
                    add.setdefault(gid, set()).update(entries)
                for gid, entries in pr_required.items():
                    retain.setdefault(gid, set()).update(entries)
                report["pr"]["required"] = {str(gid): sorted(e) for gid, e in pr_required.items()}
        except DerivationError as error:
            problems.append(str(error))
        report["pr"]["errors"] = problems

    remove = {}
    if prune:
        for commit in set(parents[:1]) | unfinished_heads(api, project, main_workflows, repository):
            try:
                for gid, entries in resolve(repos.needs(commit), groups, repository).items():
                    retain.setdefault(gid, set()).update(entries)
            except DerivationError as error:
                # An unknown retention requirement must never cause pruning.
                report["errors"].append(f"cannot derive the needs of {commit}, not pruning: {error}")
                retain = None
                break
        if retain is not None:
            for group in groups:
                for entry in group.workflows:
                    managed = managed_entry(entry, repository)
                    if not managed or entry in retain.get(group.id, set()):
                        continue
                    # Pull requests move the pin forward; keep anything newer than main.
                    if repos.compare(revision, managed[1]) == "ahead":
                        continue
                    remove.setdefault(group.id, set()).add(entry)
    changes = {gid: (add.get(gid, set()), remove.get(gid, set())) for gid in set(add) | set(remove)}
    report["written"] = {str(k): v for k, v in apply(api, organization, changes, dry_run=dry_run).items()}
    report["removed"] = {str(k): sorted(v) for k, v in remove.items()}
    report["added"] = {str(k): sorted(v) for k, v in add.items()}

    # Verify against the live groups, not against what was intended.
    if not dry_run:
        groups = load_groups(api, organization, project)
    else:
        for group in groups:
            if str(group.id) in report["written"]:
                group.workflows = report["written"][str(group.id)]
    report["unadmitted_main"] = {str(k): v for k, v in missing(required, groups).items()}
    if pr is not None and report["pr"].get("required"):
        pr_required = {int(k): set(v) for k, v in report["pr"]["required"].items()}
        report["pr"]["unadmitted"] = {str(k): v for k, v in missing(pr_required, groups).items()}
    return report


def pr_state(report):
    """Commit status (state, description) for the pull request in a reconcile report."""
    pr = report.get("pr") or {}
    if pr.get("errors"):
        return "failure", pr["errors"][0][:140]
    if pr.get("unadmitted"):
        return "failure", "Runner groups do not admit this PR's harness pins yet"
    if pr.get("changes_pins"):
        return "success", "Runner groups admit this PR's harness pins"
    return "success", "Harness pins unchanged"


# ---------------------------------------------------------------------------
# Command line


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m tauceti_tools.harness_pins", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("literals", "set"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, default=Path("."))
        command.add_argument(
            "--bound-workflow", action="append", default=[], help="Workflow file bound to the harness pin"
        )
        if name == "set":
            command.add_argument("revision")
    command = sub.add_parser("reconcile")
    command.add_argument("--project", required=True, help="owner/repository whose CI calls the harness")
    command.add_argument("--branch", default="main")
    command.add_argument("--pr", type=int)
    command.add_argument("--prune", action="store_true")
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--bound-workflow", action="append", default=[])
    command.add_argument("--token-env", default="GH_TOKEN")
    command.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "literals":
            root = args.root
            workflows = {p.name: p.read_text() for p in sorted((root / WORKFLOWS).glob("*.y*ml"))}
            errors = check_literals((root / SOURCE).read_text(), workflows, args.bound_workflow)
            for error in errors:
                print(f"::error::{error}")
            return 1 if errors else 0
        if args.command == "set":
            for path in set_revision(args.root, args.revision, args.bound_workflow):
                print(path)
            return 0
        api = GitHub(os.environ.get(args.token_env, ""))
        report = reconcile(
            api,
            args.project,
            branch=args.branch,
            pr=args.pr,
            prune=args.prune,
            bound=args.bound_workflow,
            dry_run=args.dry_run,
        )
    except DerivationError as error:
        print(f"::error::{error}")
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    for error in report["errors"]:
        print(f"::error::{error}")
    for gid, entries in report["unadmitted_main"].items():
        print(f"::error::runner group {gid} does not admit main's {', '.join(entries)}")
    if args.github_output and args.pr is not None:
        state, description = pr_state(report)
        with args.github_output.open("a") as output:
            output.write(f"state={state}\ndescription={description}\n")
    if args.pr is not None:
        return 0 if pr_state(report)[0] == "success" else 1
    return 1 if report["errors"] or report["unadmitted_main"] else 0


if __name__ == "__main__":
    sys.exit(main())
