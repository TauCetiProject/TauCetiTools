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


def _antiquotation_start(line, dot):
    """Column of the `$` when the projection whose `.` is at `dot` applies to an antiquotation
    such as `$h`, `$«h»`, `$h.eq` or `$h:term` (possibly after earlier projections), else None."""
    start = dot
    while start > 0:
        if line[start - 1].isalnum() or line[start - 1] in "_'!?.:" + SUBSCRIPT:
            start -= 1
        elif line[start - 1] == "\u00bb":  # «escaped identifier component»
            opening = line.rfind("\u00ab", 0, start - 1)
            if opening < 0:
                break
            start = opening
        else:
            break
    return start - 1 if start > 0 and line[start - 1] == "$" and start < dot else None


def replace_projs(line, edits):
    """Replace the projection digits at each `(col, field)` of `edits` (columns of `line`).

    An antiquotation receiver becomes parenthesized: `$h.1.2` -> `($h).val.property`, since
    `$h.val` would parse as `$(h.val)`. Every edit, digits and parentheses alike, is located in
    the original line and they are applied right to left."""
    receivers, ops = {}, []
    for col, field in edits:
        digits = re.match(r"\d+", line[col:])
        if not digits or col == 0 or line[col - 1] != ".":
            raise ValueError(f"no numeric projection at column {col}: {line!r}")
        ops.append((col, col + len(digits.group(0)), field))
        dollar = _antiquotation_start(line, col - 1)
        if dollar is not None:
            # The receiver ends at the first numeric projection of the chain.
            receivers[dollar] = min(receivers.get(dollar, col - 1), col - 1)
    for dollar, dot in receivers.items():
        ops += [(dollar, dollar, "("), (dot, dot, ")")]
    for start, end, text in sorted(ops, reverse=True):
        line = line[:start] + text + line[end:]
    return line


def replace_proj(line, col, field):
    """`replace_projs` for a single projection."""
    return replace_projs(line, [(col, field)])


RAW_STRING = re.compile(r'r(#*)"')
CHAR_LITERAL = re.compile(r"'(\\.|[^'\\\n])'")


def code_mask(line):
    """For each column, whether it is code: not inside a string or character literal, a block
    comment (nested), a line comment or an escaped identifier. Also lexes a whole file."""
    mask, i, depth, n = [True] * len(line), 0, 0, len(line)

    def skip(j):  # mask columns i..j inclusive (clamped), return the next column
        mask[i : j + 1] = [False] * (min(j + 1, n) - i)
        return j + 1

    def after_identifier_char(i):
        return i > 0 and (line[i - 1].isalnum() or line[i - 1] in "_'" + SUBSCRIPT)

    while i < n:
        ch = line[i]
        if depth:
            if line.startswith("/-", i):
                depth, step = depth + 1, 2
            elif line.startswith("-/", i):
                depth, step = depth - 1, 2
            else:
                step = 1
            i = skip(i + step - 1)
        elif line.startswith("/-", i):
            depth, i = 1, skip(i + 1)
        elif line.startswith("--", i):
            eol = line.find("\n", i)
            i = skip((n if eol < 0 else eol) - 1)
        elif ch == "\u00ab":  # «escaped identifier», never spanning lines
            eol = line.find("\n", i)
            eol = n if eol < 0 else eol
            close = line.find("\u00bb", i, eol)
            i = skip(eol - 1 if close < 0 else close)
        elif ch == "r" and (raw := RAW_STRING.match(line, i)) and not after_identifier_char(i):
            hashes = raw.group(1)
            close = line.find('"' + hashes, raw.end())
            i = skip(n - 1 if close < 0 else close + len(hashes))
        elif ch == '"' or (ch == "'" and CHAR_LITERAL.match(line, i) and not after_identifier_char(i)):
            j = i + 1
            while j < n and line[j] != ch:
                j += 2 if line[j] == "\\" else 1
            i = skip(j)
        else:
            i += 1
    return mask


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


def wrap(line, next_indent=None):
    """Split `line` into lines of at most LIMIT columns, or return None if no safe split exists.
    Continuations are indented four columns deeper than the original line. A line opening a
    `by`/`do`/`{` block that the next code (starting at column `next_indent`, more indented)
    continues is left alone: moving the block's first item would misalign the rest."""
    indent = len(line) - len(line.lstrip(" "))
    if line.lstrip().startswith(("--", "/-", "*")):
        return None
    if next_indent is not None and next_indent > indent and opens_block(line):
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
        rest = " " * (indent + 4) + rest[cut + 1 :]
    out.append(rest)
    return out


def line_masks(lines):
    """Per line, its `code_mask` lexed in the context of the whole file, and whether the line
    begins in code (not inside a multi-line string or block comment)."""
    text = "\n".join(lines)
    mask, out, offset = code_mask(text), [], 0
    for line in lines:
        begins_in_code = offset == 0 or mask[offset - 1]
        out.append((mask[offset : offset + len(line)], begins_in_code))
        offset += len(line) + 1
    return out


def next_code_column(lines, masks, i):
    """Column of the first code token after `lines[i]` (skipping blank lines and comments, and
    looking past a comment's close on the same line), or None at the end of the file."""
    for j in range(i + 1, len(lines)):
        mask = masks[j][0]
        for col, ch in enumerate(lines[j]):
            if mask[col] and not ch.isspace():
                return col
    return None


def apply(findings):
    """Apply `{path: {(line, col, field)}}` in place. Returns (replacements, unwrapped lines)."""
    count, unwrapped = 0, []
    for path, edits in findings.items():
        lines = Path(path).read_text(encoding="utf-8").split("\n")
        touched = set()
        by_line = collections.defaultdict(list)
        for line, col, field in edits:
            by_line[line - 1].append((col, field))
        for i, line_edits in by_line.items():
            lines[i] = replace_projs(lines[i], line_edits)
            touched.add(i)
            count += len(line_edits)
        masks = line_masks(lines)
        out = []
        for i, text in enumerate(lines):
            if i in touched and len(text) > LIMIT:
                # A line that begins inside a multi-line string or comment is left to the author.
                pieces = wrap(text, next_code_column(lines, masks, i)) if masks[i][1] else None
                if pieces is None:
                    unwrapped.append(f"{path}:{i + 1}")
                else:
                    out.extend(pieces)
                    continue
            out.append(text)
        Path(path).write_text("\n".join(out), encoding="utf-8")
    return count, unwrapped


def parse_findings(text):
    """`{path: {(line, col, field)}}` from the scanner's output, and the `path:line:col`
    positions it could not name a field for (an empty field)."""
    findings, conflicts = collections.defaultdict(set), []
    for row in text.splitlines():
        parts = row.split("\t")
        if len(parts) == 4 and parts[1].isdigit() and parts[2].isdigit():
            if parts[3]:
                findings[parts[0]].add((int(parts[1]), int(parts[2]), parts[3]))
            else:
                conflicts.append(f"{parts[0]}:{parts[1]}:{int(parts[2]) + 1}")
    return findings, conflicts


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
    findings, conflicts = parse_findings(output)
    count, unwrapped = apply(findings)
    print(f"numproj: replaced {count} numeric projection(s) in {len(findings)} of {len(files)} file(s)")
    for where in unwrapped:
        print(f"numproj: {where}: could not rewrap a line over {LIMIT} columns; split it by hand")
    for where in conflicts:
        print(f"numproj: {where}: this projection names different fields in different elaborations; rewrite it by hand")
    for path in incomplete:
        print(f"numproj: {path}: did not elaborate cleanly; fix it and run numproj again")
    return 1 if unwrapped or conflicts or incomplete else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"numproj: {error}", file=sys.stderr)
        raise SystemExit(75) from None
