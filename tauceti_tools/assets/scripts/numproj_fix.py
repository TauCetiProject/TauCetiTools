#!/usr/bin/env python3
"""Replace numeric projections (`x.1`, `h.2`) with named ones, as Mathlib's numericProjections
linter demands, and rewrap the lines this pushes past 100 columns.

Detection re-elaborates each file with the linter's own logic (NumProjScan.lean, compiled once
per toolchain), so it works on any Mathlib pin, with or without the linter. Run it from a project
checkout whose imports are built (`lake build`, or the Mathlib cache plus the project's oleans):

    tauceti-tools numproj [--base REF] [--jobs N] [FILE.lean ...]

Without files it treats every Lean file changed against REF (default origin/main). For a merge
conflict: take one side of each conflicted file, then run this on those files. The build reports
the rare forms this cannot rewrite: `(s.1 : T) → E` in a binder type (write `((s.val : T)) → E`),
`congr($h.1)` (write `congr(Subtype.val $h)`), and a section variable in `z.1` (write `(z).val`).
"""

import argparse
import collections
import os
import pwd
import re
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LIMIT = 100
OPTIONS = ["-DElab.async=false", "-Dweak.linter.mathlibStandardSet=true"]
SUBSCRIPT = "₀₁₂₃₄₅₆₇₈₉"


def replace_projs(line, edits):
    """Replace the projection digits at each `(col, field)` or `(col, field, (start, end))` of
    `edits` (columns of `line`). With a span, the scanner found that the named form would not parse
    as intended, and the receiver from `start` to `end` (the `.` before the first rewritten
    projection) becomes parenthesized: `$h.1` -> `($h).val`, or `s.1` -> `(s).val` when a global
    `s.val` exists. All edits are located in the original line and applied right to left."""
    spans, ops = set(), []
    for col, field, *span in edits:
        digits = re.match(r"\d+", line[col:])
        if not digits or col == 0 or line[col - 1] != ".":
            raise ValueError(f"no numeric projection at column {col}: {line!r}")
        ops.append((col, col + len(digits.group(0)), field))
        if span and span[0]:
            spans.add(span[0])
    for start, end in spans:
        if not 0 <= start < end < len(line) or line[end] != ".":
            raise ValueError(f"no receiver at columns {start}:{end}: {line!r}")
        ops += [(start, start, "("), (end, end, ")")]
    for start, end, text in sorted(ops, reverse=True):
        line = line[:start] + text + line[end:]
    return line


def replace_proj(line, col, field, span=None):
    """`replace_projs` for a single projection."""
    return replace_projs(line, [(col, field, span)])


RAW_STRING = re.compile(r'r(#*)"')
CHAR_LITERAL = re.compile(r"'(\\.|[^'\\\n])'")
CODE, STRING, COMMENT, ESCAPED = "c", "s", "m", "i"


def lex(text):
    """For each column, its kind: CODE, or inside a STRING (string, character or raw string
    literal), a COMMENT (line, or nested block) or an ESCAPED identifier `«...»`. Also returns the
    columns where string literals and escaped identifiers start. Lexes a line or a whole file."""
    kinds, starts, i, depth, n = [CODE] * len(text), set(), 0, 0, len(text)

    def mark(j, kind):  # columns i..j inclusive (clamped) are `kind`; returns the next column
        kinds[i : j + 1] = [kind] * (min(j + 1, n) - i)
        return j + 1

    def after_identifier_char(i):
        return i > 0 and (text[i - 1].isalnum() or text[i - 1] in "_'" + SUBSCRIPT)

    while i < n:
        ch = text[i]
        if depth:
            if text.startswith("/-", i):
                depth, step = depth + 1, 2
            elif text.startswith("-/", i):
                depth, step = depth - 1, 2
            else:
                step = 1
            i = mark(i + step - 1, COMMENT)
        elif text.startswith("/-", i):
            depth, i = 1, mark(i + 1, COMMENT)
        elif text.startswith("--", i):
            eol = text.find("\n", i)
            i = mark((n if eol < 0 else eol) - 1, COMMENT)
        elif ch == "\u00ab":  # «escaped identifier», which may span lines
            close = text.find("\u00bb", i + 1)
            starts.add(i)
            i = mark(n - 1 if close < 0 else close, ESCAPED)
        elif ch == "r" and (raw := RAW_STRING.match(text, i)) and not after_identifier_char(i):
            hashes = raw.group(1)
            close = text.find('"' + hashes, raw.end())
            starts.add(i)
            i = mark(n - 1 if close < 0 else close + len(hashes), STRING)
        elif ch == '"' or (ch == "'" and CHAR_LITERAL.match(text, i) and not after_identifier_char(i)):
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            starts.add(i)
            i = mark(j, STRING)
        else:
            i += 1
    return kinds, starts


def code_mask(text):
    """For each column, whether it is code (see `lex`)."""
    return [kind == CODE for kind in lex(text)[0]]


LAYOUT = r"(?<![\w.])(by|do|let|have|show|suffices|obtain|match|calc|fun|from)(?![\w.'])"


def opens_block(line):
    """Whether `line` opens a layout-sensitive block that may continue on the next line: an inline
    `by`/`do`/`let`/`have`/`match`/... with content after it, or a `{ ... }` left unclosed."""
    mask = code_mask(line)
    for m in re.finditer(LAYOUT, line):
        if mask[m.start()] and line[m.end() :].strip():
            return True
    depth = 0
    for i, ch in enumerate(line):
        if mask[i] and ch in "{}":
            depth = depth + 1 if ch == "{" else max(0, depth - 1)
    return depth > 0


def break_points(line):
    """Spaces where a Lean line may be split: in code, outside inline `by`/`do` blocks and
    `{ ... }` structure instances, and before any inline match alternative `| pat => rhs`."""
    mask = code_mask(line)
    keywords = [m.start() for m in re.finditer(r"(?<![\w.])(by|do)(?![\w.])", line) if mask[m.start()]]
    alternatives = [m.start() for m in re.finditer(r"(?<![|<])\|(?![|>])", line) if mask[m.start()]]
    depth, points = 0, []
    for i, ch in enumerate(line):
        if not mask[i]:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch != " " or i == 0 or line[i - 1] == " " or depth:
            continue
        if line[i + 1 : i + 2] in ("|", "\u00b7", ""):
            continue
        if any(k < i and line[k:i].strip() not in ("by", "do") for k in keywords):
            continue
        if any(a < i for a in alternatives):
            continue
        points.append(i)
    return points


COMBINATOR = (
    r"(try|all_goals|any_goals|focus|repeat|repeat'|repeat1'|iterate|first|case|case'|next|on_goal"
    r"|pick_goal|classical|with_reducible|with_reducible_and_instances|with_unfolding_all"
    r"|with_unfolding_none|with_implicit|show_term|unhygienic|solve|fail_if_success|guard_target"
    r"|refine_lift|set_option|open|nofun|nomatch|conv|calc|match|induction|cases|rcases|obtain"
    r"|as_aux_lemma"
    # a parenthesized tactic sequence `(tac ...)`
    r"|\(\s*(try|exact|refine|refine'|apply|simp|simpa|rw|rwa|intro|intros|constructor|omega|linarith"
    r"|nlinarith|positivity|norm_num|ring|field_simp|aesop|grind|decide|rfl|use|exists|ext|funext"
    r"|congr|gcongr|show|have|let|unfold|dsimp|split|by_cases|by_contra|contradiction|exfalso|left"
    r"|right|trivial|assumption|specialize|subst|change|convert|apply_fun|all_goals|any_goals|first))"
    r"(?![\w'])"
)


def _head_column(line, kinds):
    """Column of the line's first code token after any bullets (`·`, `.`) and comments."""
    i = 0
    while i < len(line):
        if line[i].isspace() or kinds[i] == COMMENT:
            i += 1
        elif kinds[i] == CODE and line[i] in "·." and line[i + 1 : i + 2].isspace():
            i += 1
        else:
            return i
    return i


def _inside_inline_by(line, kinds, col):
    """Whether column `col` lies inside an inline `by` block of `line`: after a `by` whose
    enclosing bracket is not closed before `col`."""
    for m in re.finditer(r"(?<![\w.])by(?![\w.'])", line[:col]):
        if kinds[m.start()] != CODE:
            continue
        depth = 0
        for i in range(m.end(), col):
            if kinds[i] == CODE and line[i] in "([{⟨":
                depth += 1
            elif kinds[i] == CODE and line[i] in ")]}⟩":
                depth -= 1
                if depth < 0:
                    break
        else:
            return True
    return False


def wrap(line, next_indent=None):
    """Split `line` into lines of at most LIMIT columns, or return None if no safe split exists.
    Continuations are indented four columns deeper than the original line, and deeper than its
    first token after bullets. A line opening a
    `by`/`do`/`{` block that the next code (starting at column `next_indent`, more indented)
    continues is left alone: moving the block's first item would misalign the rest."""
    indent = len(line) - len(line.lstrip(" "))
    if line.lstrip().startswith(("--", "/-", "*")):
        return None
    if next_indent is not None and next_indent > indent and opens_block(line):
        return None
    kinds, _ = lex(line)
    if any(kind == STRING and ch == "{" for kind, ch in zip(kinds, line, strict=True)):
        # An interpolated string (`s!"{x.1} {"a b"}"`) may nest literals the lexer cannot see.
        return None
    code = "".join(c if k == CODE else " " for k, c in zip(kinds, line, strict=True))
    head = _head_column(line, kinds)
    if re.match(COMBINATOR, line[head:]) or "<;>" in code or re.search(r"(?<![\w.])in(?![\w.'])", code):
        # A nested tactic (`try exact f x`, `set_option o v in exact f x`) may start at or after
        # the continuation column, which would turn the continuation into a tactic of its own.
        return None
    # Continuations go deeper than the line's first tactic or term (after bullets: `· · exact`).
    cont = max(indent + 4, head + 2)
    for semi in (i for i, (kind, ch) in enumerate(zip(kinds, line, strict=True)) if kind == CODE and ch == ";"):
        if not _inside_inline_by(line, kinds, semi):
            # `stmt; stmt` in a `do` block: a split could move a statement's end into a new one.
            return None
    out, rest = [], line
    while len(rest) > LIMIT:
        floor = len(rest) - len(rest.lstrip(" ")) + 8
        points = [b for b in break_points(rest) if floor < b <= LIMIT]
        if not points:
            return None
        preferred = [
            b for b in points if rest[b - 1] == "," or rest[b + 1 : b + 2] in "(⟨[{" or rest[b + 1 : b + 3] == ":="
        ]
        cut = max(preferred) if preferred and max(preferred) > max(points) - 30 else max(points)
        out.append(rest[:cut])
        rest = " " * cont + rest[cut + 1 :]
        if re.search(r"(?<![\w.])(by|do)$", out[-1]) and len(rest) > LIMIT:
            # The rest is that block's body, which must not be split again.
            return None
    out.append(rest)
    return out


def line_lexes(lines):
    """Per line: its `lex` kinds in the context of the whole file, the columns where its string
    literals and escaped identifiers start, and whether the line begins in code."""
    kinds, starts = lex("\n".join(lines))
    out, offset = [], 0
    for line in lines:
        begins_in_code = offset == 0 or kinds[offset - 1] == CODE
        line_starts = {k - offset for k in starts if offset <= k < offset + len(line)}
        out.append((kinds[offset : offset + len(line)], line_starts, begins_in_code))
        offset += len(line) + 1
    return out


def next_token_column(lines, lexes, i):
    """Column of the first token after `lines[i]` (skipping blank lines and comments, looking past
    a comment's close on the same line, and counting literals and escaped identifiers where they
    start), or None at the end of the file."""
    for j in range(i + 1, len(lines)):
        kinds, starts, _ = lexes[j]
        for col, ch in enumerate(lines[j]):
            if not ch.isspace() and (kinds[col] == CODE or col in starts):
                return col
    return None


def apply(findings):
    """Apply `{path: {(line, col, field, span)}}` in place. Returns (replacements, unwrapped lines)."""
    count, unwrapped = 0, []
    for path, edits in findings.items():
        lines = Path(path).read_text(encoding="utf-8").split("\n")
        touched = set()
        by_line = collections.defaultdict(list)
        for line, col, field, span in edits:
            by_line[line - 1].append((col, field, span))
        for i, line_edits in by_line.items():
            lines[i] = replace_projs(lines[i], line_edits)
            touched.add(i)
            count += len(line_edits)
        lexes = line_lexes(lines)
        out = []
        for i, text in enumerate(lines):
            if i in touched and len(text) > LIMIT:
                # A line that begins inside a multi-line string or comment is left to the author.
                pieces = wrap(text, next_token_column(lines, lexes, i)) if lexes[i][2] else None
                if pieces is None:
                    unwrapped.append(f"{path}:{i + 1}")
                else:
                    out.extend(pieces)
                    continue
            out.append(text)
        Path(path).write_text("\n".join(out), encoding="utf-8")
    return count, unwrapped


def parse_findings(text):
    """`{path: {(line, col, field, span)}}` from the scanner's output (`span` a `(start, end)`
    receiver to parenthesize, or None), and the `(path:line:col, reason)` it declined."""
    findings, declined = collections.defaultdict(set), []
    for row in text.splitlines():
        parts = row.split("\t")
        if len(parts) not in (4, 5) or not (parts[1].isdigit() and parts[2].isdigit()):
            continue
        line, col, field = int(parts[1]), int(parts[2]), parts[3]
        where = f"{parts[0]}:{line}:{col + 1}"
        if field == "?":
            declined.append((where, "its elaborations disagree on the field, or its receiver is unclear"))
        elif field == "!":
            reason = parts[4] if len(parts) == 5 else "a type synonym"
            declined.append((where, f"the named field might resolve to another declaration ({reason})"))
        else:
            span = tuple(int(n) for n in parts[4].split(":")) if len(parts) == 5 else None
            findings[parts[0]].add((line, col, field, span))
    return findings, declined


def cache_root():
    configured = os.environ.get("TAUCETI_NUMPROJ_CACHE")
    if configured:
        root = Path(configured)
    else:
        try:
            home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except KeyError:
            home = Path.home()
        root = home / ".cache/tauceti/numproj"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"numproj: unsafe scanner cache (need an owned private directory): {root}")
    return root


def scanner():
    """The compiled scanner for the active toolchain, built once (local_axioms' native cache)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from local_axioms import checker

    prefix = Path(subprocess.check_output(["lean", "--print-prefix"], text=True).strip()).resolve()
    # The scanner elaborates project files, whose imports' initializers run in the interpreter;
    # it needs the Lean runtime's symbols exported, as Lake's `supportInterpreter` arranges.
    driver = Path(__file__).resolve().with_name("NumProjScan.lean")
    return checker(driver, prefix, cache_root(), link_args=("-rdynamic",))


INCOMPLETE = 3  # NumProjScan's status when the file did not elaborate cleanly


def scan(binary, files, jobs):
    """Findings for `files`, and the files that did not elaborate cleanly (their findings cover
    only what elaborated)."""

    def one(path):
        result = subprocess.run(["lake", "env", str(binary), *OPTIONS, path], capture_output=True, text=True)
        if result.returncode not in (0, INCOMPLETE):
            raise RuntimeError(f"numproj: scanning {path} failed:\n{result.stderr.strip()}")
        if result.returncode == INCOMPLETE:
            print(f"numproj: {path} has elaboration errors; its findings may be incomplete", file=sys.stderr)
            print(result.stderr.strip(), file=sys.stderr)
        return result.stdout, result.returncode == INCOMPLETE

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(one, files))
    return "".join(out for out, _ in results), [f for f, (_, bad) in zip(files, results, strict=True) if bad]


def changed_files(base):
    out = subprocess.check_output(
        ["git", "diff", "-z", "--name-only", "--diff-filter=AMR", base, "--", "*.lean"], text=True
    )
    return [f for f in out.split("\0") if f and Path(f).is_file()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*")
    parser.add_argument("--base", default="origin/main", help="with no files, scan Lean files changed against this")
    parser.add_argument("--jobs", type=int, default=int(os.environ.get("NUMPROJ_JOBS", "16")))
    args = parser.parse_args(argv)
    files = args.files or changed_files(args.base)
    if not files:
        print("numproj: no Lean files to scan")
        return 0
    output, incomplete = scan(scanner(), files, args.jobs)
    findings, declined = parse_findings(output)
    count, unwrapped = apply(findings)
    print(f"numproj: replaced {count} numeric projection(s) in {len(findings)} of {len(files)} file(s)")
    for where in unwrapped:
        print(f"numproj: {where}: could not rewrap a line over {LIMIT} columns; split it by hand")
    for where, reason in declined:
        print(f"numproj: {where}: not rewritten, as {reason}; rewrite it by hand")
    for path in incomplete:
        print(f"numproj: {path}: did not elaborate cleanly; fix it and run numproj again")
    return 1 if unwrapped or declined or incomplete else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"numproj: {error}", file=sys.stderr)
        raise SystemExit(75) from None
