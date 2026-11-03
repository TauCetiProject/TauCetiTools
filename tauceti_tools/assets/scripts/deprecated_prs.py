#!/usr/bin/env python3
"""After a Mathlib pin changes, send matching open heads through ordinary PR repair.

Git objects and PR sources are data only. No Lean, Lake or candidate code runs.
Matches are scheduling hints: there is deliberately no confirmation build and no
Bors API, command, status, approval or queue mutation here.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote

from github_api import APIError, Deferred, GitHub
from lean_source import IDENTIFIER, _skip_balanced, qualified_declarations, strip_comments_and_strings
from tauceti_tools.survey_facts import rebase_heads

SHA = re.compile(r"[a-f0-9]{40}\Z")
PAUSE = {"keep", "hold", "wip", "human", "on-hold", "do-not-close", "blocked"}
LABEL = "needs-rebase"
NAME = r"(?:_root_\.)?[\w']+(?:\.[\w']+)*"
DECLARATION = re.compile(
    r"(?:(?:public|private|protected|noncomputable|nonrec)\s+)*"
    r"(?:alias|theorem|lemma|def|abbrev|opaque|instance)\s+(" + NAME + r")")
OPEN_PRS = """
query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:OPEN, first:100, after:$cursor,
                 orderBy:{field:CREATED_AT,direction:ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes { number isDraft headRefOid baseRefName
        labels(first:100) { pageInfo { hasNextPage } nodes { name } }
      }
    }
  }
}
"""


def git(root, *args, input=None):
    # No checkout, filters, hooks, replacement objects, or candidate configuration.
    env = {k: v for k, v in os.environ.items()
           if k in {"PATH", "HOME", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR"}}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
               GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
                           "-c", "credential.helper=", *args], input=input,
                          capture_output=True, check=True, timeout=180, env=env).stdout


def pin(manifest):
    packages = [p for p in json.loads(manifest)["packages"] if p.get("name") == "mathlib"]
    if len(packages) != 1 or not SHA.fullmatch(packages[0].get("rev", "")):
        raise ValueError("expected one immutable Mathlib pin")
    if packages[0].get("url", "").removesuffix(".git") != "https://github.com/leanprover-community/mathlib4":
        raise ValueError("expected the official Mathlib repository")
    return packages[0]["rev"]


def deprecated_names(source):
    """Conservative command-level inventory, including attributes on existing names."""
    code = strip_comments_and_strings(source)
    extras = []
    for m in re.finditer(r"@\[|(?m:^attribute\s+\[)", code):
        start = code.index("[", m.start())
        end = _skip_balanced(code, start)
        # Only the top-level attribute matters: to_additive's nested
        # (attr := deprecated ...) does not deprecate the written declaration.
        inner = code[start + 1:end - 1]
        top, depth = [], 0
        for c in inner:
            if c in "([{":
                depth += 1
            if depth == 0:
                top.append(c)
            if c in ")]}":
                depth -= 1
        if not re.search(r"(?:^|,)\s*deprecated(?:\s|$)", "".join(top)):
            continue
        tail = end
        while tail < len(code) and code[tail].isspace():
            tail += 1
        if code[m.start()] == "@":
            while code.startswith("@[", tail):
                tail = _skip_balanced(code, tail + 1)
                while tail < len(code) and code[tail].isspace():
                    tail += 1
            if declaration := DECLARATION.match(code, tail):
                extras.append((m.start(), declaration[1]))
        else:
            line = code[tail:code.find("\n", tail) if "\n" in code[tail:] else len(code)]
            extras.extend((m.start(), n.group()) for n in IDENTIFIER.finditer(line))
    return qualified_declarations(code, extra_declarations=extras, keep=lambda _: False)


def blobs(root, ref, paths):
    """Read changed immutable blobs in one process rather than one Git call per file."""
    entries = git(root, "ls-tree", "-r", "-z", ref, "--", "Mathlib").split(b"\0")
    objects = []
    for entry in entries:
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, kind, oid = meta.split()
        if path.decode() in paths and mode == b"100644" and kind == b"blob":
            objects.append(oid)
    raw = git(root, "cat-file", "--batch", input=b"\n".join(objects) + b"\n") if objects else b""
    offset = 0
    for _ in objects:
        end = raw.index(b"\n", offset)
        _, kind, size = raw[offset:end].split()
        if kind != b"blob":
            raise ValueError("expected a source blob")
        size = int(size)
        yield raw[end + 1:end + 1 + size].decode("utf-8")
        offset = end + size + 2


def new_deprecations(root, old, new):
    paths = {p for p in git(root, "diff", "--name-only", "-z", old, new, "--", "Mathlib").decode().split("\0")
             if p.endswith(".lean")}
    before, after = set(), set()
    for source in blobs(root, old, paths):
        before.update(deprecated_names(source))
    for source in blobs(root, new, paths):
        after.update(deprecated_names(source))
    return after - before


def matches(source, names):
    # Names remain hints, not elaborated references. Generic short leaves cannot
    # distinguish e.g. a current root add_apply from a deprecated namespace alias.
    leaves = {}
    for name in names:
        leaves.setdefault(name.rsplit(".", 1)[-1], []).append(name)
    found = set()
    for token in IDENTIFIER.finditer(strip_comments_and_strings(source)):
        name = token.group().removeprefix("_root_.")
        parts = name.split(".")
        for i, leaf in enumerate(parts):
            prefix = ".".join(parts[:i + 1])
            if prefix in names:
                found.add(prefix)
            if len(leaf) >= 16 and "_" in leaf and len(leaves.get(leaf, [])) == 1:
                found.add(leaves[leaf][0])
    return sorted(found)


def open_prs(api, repo):
    owner, name = repo.split("/")
    cursor = None
    while True:
        page = api.graphql(OPEN_PRS, dict(owner=owner, name=name, cursor=cursor))["repository"]["pullRequests"]
        yield from page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]


def eligible(pr, branch):
    labels = pr["labels"]
    return (not pr["isDraft"] and pr["baseRefName"] == branch
            and SHA.fullmatch(pr["headRefOid"]) is not None
            and not labels["pageInfo"]["hasNextPage"]
            and not ({l["name"].lower() for l in labels["nodes"]} & (PAUSE | {LABEL})))


def live_matches(pr, head, branch):
    return (pr.get("state") == "open" and pr.get("draft") is False
            and pr.get("head", {}).get("sha") == head
            and pr.get("base", {}).get("ref") == branch
            and not ({l["name"].lower() for l in pr.get("labels", [])} & (PAUSE | {LABEL})))


def decode_blob(blob):
    if blob.get("encoding") != "base64":
        raise ValueError("expected a complete base64 source blob")
    return base64.b64decode(blob["content"], validate=False).decode("utf-8")


def scan_pr(api, repo, pr, names):
    files = api.items(f"repos/{repo}/pulls/{pr['number']}/files")
    # Respect human-owned infrastructure PRs. Never turn their intended changes
    # into mathematical repair work; no PR code is executed by this job.
    if any(f["filename"].startswith(("scripts/", ".github/"))
           or f["filename"] in {"lakefile.lean", "lakefile.toml"} for f in files):
        return []
    findings = []
    for file in files:
        path = file["filename"]
        if file["status"] == "removed" or not path.startswith("TauCeti/") or not path.endswith(".lean"):
            continue
        patch = file.get("patch")
        # The patch is a cheap shortlist only: comments may span the diff boundary.
        # No patch (GitHub truncation) means read the full blob, not assume a pass.
        if patch is not None and not matches(patch, names):
            continue
        source = decode_blob(api.request("GET", f"repos/{repo}/contents/{quote(path, safe='/')}?ref={pr['headRefOid']}"))
        if uses := matches(source, names):
            findings.append(dict(path=path, declarations=uses))
    return findings


def handoff(api, repo, pr, branch, findings, *, dry_run):
    """Reuse the existing repair marker and label; no custom worker instructions."""
    number, head = pr["number"], pr["headRefOid"]
    live = api.request("GET", f"repos/{repo}/pulls/{number}")
    if not live_matches(live, head, branch):
        return "stale_or_parked"
    # Main may move again during this advisory scan. Finish this bump's names
    # against unchanged PR heads rather than silently abandoning its remainder.
    print(json.dumps(dict(pr=number, head=head, findings=findings)), flush=True)
    if dry_run:
        return "would_label"
    comments = api.items(f"repos/{repo}/issues/{number}/comments")
    if head not in rebase_heads(comments):
        body = (f"Merge-queue recovery for head `{head[:7]}`: Mathlib dependency changed.\n\n"
                f"<!--tauceti-rebase:v1 {head}-->")
        api.request("POST", f"repos/{repo}/issues/{number}/comments", {"body": body})
    # Comment creation can wake another actor: recheck immediately before label.
    live = api.request("GET", f"repos/{repo}/pulls/{number}")
    if not live_matches(live, head, branch):
        return "stale_or_parked"
    api.request("POST", f"repos/{repo}/issues/{number}/labels", {"labels": [LABEL]})
    return "labelled"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--mathlib", type=Path, required=True)
    parser.add_argument("--repo", default=os.environ.get("GH_REPO", "TauCetiProject/TauCeti"))
    parser.add_argument("--branch", default="main")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not all(SHA.fullmatch(s) and s != "0" * 40 for s in [args.before, args.after]):
        parser.error("before and after must be immutable nonzero commit SHAs")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        parser.error("invalid repository")
    old, new = [pin(git(args.project, "show", f"{sha}:lake-manifest.json"))
                for sha in [args.before, args.after]]
    report = dict(before=args.before, after=args.after, old_mathlib=old, new_mathlib=new,
                  dry_run=args.dry_run, scanned=0, results=[], errors=[], complete=False)
    api = GitHub(max_wait=30)
    try:
        if old == new:
            report["complete"] = True
            return 0
        args.mathlib.mkdir(parents=True, exist_ok=True)
        if not (args.mathlib / "HEAD").exists():
            git(args.mathlib, "init", "--bare")
        for sha in [old, new]:
            try:
                git(args.mathlib, "cat-file", "-e", sha + "^{commit}")
            except subprocess.CalledProcessError:
                git(args.mathlib, "fetch", "--no-tags", "--depth=1",
                    "https://github.com/leanprover-community/mathlib4.git", sha)
        names = new_deprecations(args.mathlib, old, new)
        report["newly_deprecated"] = sorted(names)
        print(f"Mathlib {old[:12]} -> {new[:12]}: {len(names)} newly deprecated names", flush=True)
        if not names:
            report["complete"] = True
            return 0
        for pr in open_prs(api, args.repo):
            if not eligible(pr, args.branch):
                continue
            try:
                findings = scan_pr(api, args.repo, pr, names)
                report["scanned"] += 1
                if findings:
                    outcome = handoff(api, args.repo, pr, args.branch, findings, dry_run=args.dry_run)
                    report["results"].append(dict(pr=pr["number"], head=pr["headRefOid"], outcome=outcome,
                                                  findings=findings))
            except Deferred:
                raise
            except (APIError, ValueError, UnicodeError) as error:
                # Missing/deleted PR data affects this PR, not the rest of the sweep.
                report["errors"].append(dict(pr=pr["number"], reason=type(error).__name__))
                print(f"::warning::PR #{pr['number']} could not be scanned ({type(error).__name__})")
        report["complete"] = not report["errors"]
        return int(bool(report["errors"]))
    finally:
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + "\n")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as out:
                out.write(f"Mathlib deprecation sweep: {report['scanned']} PRs scanned; "
                          f"{len(report['results'])} matches; {len(report['errors'])} incomplete. "
                          f"Complete: {report['complete']}. Dry run: {args.dry_run}. "
                          f"Full findings are in the report artifact.\n")
        api.report()


if __name__ == "__main__":
    sys.exit(main())
