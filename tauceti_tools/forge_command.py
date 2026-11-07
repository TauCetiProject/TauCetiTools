"""Parse the supported gh corpus before choosing a host or opening credentials.

Like Cobra string flags, a value-taking option consumes its following argument
even when the value looks like another flag. The original argv is retained by
the caller for native GitHub delegation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .forge import Unsupported


@dataclass(frozen=True)
class Option:
    name: str
    aliases: tuple[str, ...]
    kind: str = "value"


def option(name, *aliases, kind="value"):
    return Option(name, ("--" + name.replace("_", "-"), *aliases), kind)


REPO = option("repo", "-R")
JSON = option("json")
JQ = option("jq", "-q")
LIMIT = option("limit", "-L")
BODY = option("body", "-b")
BODY_FILE = option("body_file", "-F")
TITLE = option("title", "-t")
AUTHOR = option("author", "-A")
LABEL = option("label", "-l", kind="repeat")
BASE = option("base", "-B")
HEAD = option("head", "-H")
STATE = option("state", "-s")
NAME = option("name", "-n")
READ = (REPO, JSON, JQ)
WRITE = (REPO,)
CONTENT = (TITLE, BODY, BODY_FILE)

# This table is intentionally explicit. Unsupported commands/flags never reach
# a credential or an implicit native-GitHub fallback.
COMMANDS = {
    ("pr", "view"): READ,
    ("pr", "list"): (*READ, STATE, LIMIT, AUTHOR, LABEL, BASE, HEAD),
    ("pr", "create"): (*WRITE, *CONTENT, BASE, HEAD, LABEL, option("draft", "-d", kind="bool")),
    ("pr", "edit"): (*WRITE, *CONTENT, BASE, option("add_label", kind="repeat"), option("remove_label", kind="repeat")),
    ("pr", "comment"): (*WRITE, BODY, BODY_FILE),
    ("pr", "review"): (
        *WRITE,
        BODY,
        BODY_FILE,
        option("approve", "-a", kind="bool"),
        option("comment", "-c", kind="bool"),
        option("request_changes", "-r", kind="bool"),
    ),
    ("pr", "close"): (*WRITE, option("comment", "-c")),
    ("pr", "reopen"): (*WRITE, option("comment", "-c")),
    ("issue", "view"): READ,
    ("issue", "list"): (*READ, STATE, LIMIT, AUTHOR, LABEL),
    ("issue", "create"): (*WRITE, *CONTENT, LABEL),
    ("issue", "edit"): (*WRITE, *CONTENT, option("add_label", kind="repeat"), option("remove_label", kind="repeat")),
    ("issue", "comment"): (*WRITE, BODY, BODY_FILE),
    ("issue", "close"): (*WRITE, option("comment", "-c")),
    ("issue", "reopen"): (*WRITE, option("comment", "-c")),
    ("repo", "view"): READ,
    ("repo", "list"): (JSON, JQ, LIMIT, option("fork", kind="bool"), option("hostname")),
    ("repo", "fork"): (
        *WRITE,
        option("clone", kind="bool"),
        option("remote", kind="bool"),
        option("fork_name"),
        option("org"),
    ),
    ("label", "list"): (*READ, LIMIT),
    ("label", "create"): (
        *WRITE,
        option("color", "-c"),
        option("description", "-d"),
        option("force", "-f", kind="bool"),
    ),
    ("label", "edit"): (*WRITE, NAME, option("color", "-c"), option("description", "-d")),
    ("auth", "status"): (option("hostname", "-h"),),
    ("auth", "token"): (option("hostname", "-h"),),
}


@dataclass(frozen=True)
class Command:
    group: str
    action: str
    args: tuple[str, ...]
    options: dict

    @property
    def key(self):
        return self.group, self.action


def _consume(tokens, index, spec, values):
    token = tokens[index]
    flag, sep, attached = token.partition("=")
    definition = next((o for o in spec if flag in o.aliases), None)
    if definition is None and token.startswith("-") and not token.startswith("--"):
        # Cobra permits -Rowner/repo and -btext, but no invented boolean clusters.
        definition = next((o for o in spec if token[:2] in o.aliases and o.kind != "bool"), None)
        if definition is not None:
            attached, sep = token[2:], "="
    if definition is None:
        raise Unsupported(f"unsupported option {flag}")
    if definition.kind == "bool":
        if sep and attached not in {"true", "false"}:
            raise Unsupported(f"{flag} requires true or false")
        value = attached == "true" if sep else True
    elif sep:
        value = attached
    else:
        index += 1
        if index >= len(tokens):
            raise Unsupported(f"{flag} requires a value")
        value = tokens[index]
    name = definition.name
    if definition.kind == "repeat":
        values.setdefault(name, []).append(value)
    elif name in values and values[name] != value:
        raise Unsupported(f"conflicting values for {flag}")
    else:
        values[name] = value
    return index + 1


def parse_command(argv):
    tokens, values, index = list(argv), {}, 0
    # Only repository/JSON flags are understood before a command. Payload flags
    # are interpreted with the selected command's own arity, never globally.
    while index < len(tokens) and tokens[index].startswith("-"):
        index = _consume(tokens, index, READ, values)
    if index >= len(tokens):
        raise Unsupported("a command is required")
    group = tokens[index]
    index += 1
    while index < len(tokens) and tokens[index].startswith("-"):
        index = _consume(tokens, index, READ, values)
    if index >= len(tokens):
        raise Unsupported("a subcommand is required")
    action = tokens[index]
    index += 1
    spec = COMMANDS.get((group, action))
    if spec is None:
        raise Unsupported(f"unsupported command {group} {action}; no delegation was attempted")
    if set(values) - {o.name for o in spec}:
        raise Unsupported("option is not supported by this command")
    positional = []
    while index < len(tokens):
        if tokens[index] == "--":
            positional.extend(tokens[index + 1 :])
            break
        if tokens[index].startswith("-"):
            index = _consume(tokens, index, spec, values)
        else:
            positional.append(tokens[index])
            index += 1
    cmd = Command(group, action, tuple(positional), values)
    _validate(cmd)
    return cmd


def _validate(cmd):
    opts = cmd.options
    counts = {
        ("repo", "view"): {0, 1},
        ("repo", "list"): {0, 1},
        ("repo", "fork"): {0, 1},
        ("auth", "status"): {0},
        ("auth", "token"): {0},
    }
    allowed = counts.get(cmd.key, {0} if cmd.action in {"list", "create"} else {1})
    if cmd.group == "label" and cmd.action == "create":
        allowed = {1}
    if len(cmd.args) not in allowed:
        raise Unsupported("incorrect number of command arguments")
    if cmd.group in {"pr", "issue"} and cmd.args:
        number = cmd.args[0]
        if not number.isascii() or not number.isdigit() or int(number) < 1:
            raise Unsupported("issue/PR number must be a positive ASCII integer")
    if "limit" in opts:
        value = opts["limit"]
        if not value.isascii() or not value.isdigit() or int(value) < 1:
            raise Unsupported("limit must be a positive ASCII integer")
    states = {"open", "closed", "all"} | ({"merged"} if cmd.group == "pr" else set())
    if "state" in opts and opts["state"] not in states:
        raise Unsupported("unsupported listing state")
    for key in ["label", "add_label", "remove_label"]:
        if any(not name.strip() for group in opts.get(key, []) for name in group.split(",")):
            raise Unsupported("label names cannot be empty")
    if "body" in opts and "body_file" in opts:
        raise Unsupported("specify either --body or --body-file")
    if cmd.key == ("pr", "review") and sum(bool(opts.get(k)) for k in ["approve", "comment", "request_changes"]) != 1:
        raise Unsupported("choose one of --approve, --comment or --request-changes")
    if cmd.action == "comment" and "body" not in opts and "body_file" not in opts:
        raise Unsupported("noninteractive comments require --body or --body-file")
    if cmd.action == "create" and cmd.group in {"pr", "issue"} and not opts.get("title"):
        raise Unsupported("noninteractive creation requires --title")
    if cmd.key == ("pr", "create") and not opts.get("head"):
        raise Unsupported("Forgejo PR creation requires an explicit --head")
    if cmd.key == ("repo", "fork") and (opts.get("clone") is not False or opts.get("remote", False)):
        raise Unsupported("Forgejo fork creation currently requires --clone=false and no remote changes")
