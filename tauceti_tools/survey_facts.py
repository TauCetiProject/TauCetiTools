"""Pure survey facts shared with the service through a contract and differential tests.

Scheduling, funding, counters, reviewer affinity and priority stay in the worker.
The service materializes only these facts; direct reads use the same reductions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SPEC = json.loads(Path(__file__).with_name("survey_contract.json").read_text())
CONTRACT = SPEC["id"]


def _reject_constant(value):
    raise ValueError("invalid JSON constant: " + value)


def _json(text):
    return json.loads(text, parse_constant=_reject_constant)


def target_focuses(body):
    out = set()
    for match in re.finditer(SPEC["target_pattern"], body or ""):
        try:
            value = _json(match[1]).get("focus")
            if isinstance(value, str) and value not in ("", "any", "auto"):
                out.add(value)
        except (ValueError, AttributeError):
            pass
    return sorted(out)


def roadmap_focuses(labels, targets=(), branch=""):
    labels = {label for label in labels if label.startswith("roadmap/")}
    areas = labels - {"roadmap/", "roadmap/none", "roadmap/Unknown"}
    if areas:
        return {label.removeprefix("roadmap/") for label in areas}
    if "roadmap/Unknown" in labels:
        return None
    if "roadmap/none" in labels:
        return set()
    if targets:
        return set(targets)
    return None if not labels and branch.startswith("roadmap/") else set()


def in_scope(labels, targets=(), branch="", roadmap="any", skip=()):
    areas = roadmap_focuses(labels, targets, branch)
    # Unassigned maintenance/user PRs remain in the shared pool. Unknown area
    # metadata must not strand work while the labeler catches up.
    if areas is None or not areas:
        return True
    if roadmap not in ("", "any", "auto"):
        return roadmap in areas
    return bool(areas - set(skip))


def scoreboard(comments):
    marked = sorted(
        (c for c in comments or [] if SPEC["scoreboard"] in (c.get("body") or "")),
        key=lambda c: c.get("updated_at") or "",
        reverse=True,
    )
    for c in marked:
        matches = re.findall(SPEC["meta_pattern"], c.get("body") or "")
        try:
            parsed = _json(matches[-1]) if matches else None
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("mode") != "init":
            return parsed
    return None


def newest_contest(comments):
    roots = {}
    for c in comments or []:
        if c.get("in_reply_to_id") is None:
            match = re.search(SPEC["rubric_pattern"], c.get("body") or "")
            if match:
                try:
                    roots[int(c["id"])] = match[1]
                except (ValueError, TypeError):
                    pass
    best = None
    for c in comments or []:
        try:
            rubric = roots.get(int(c.get("in_reply_to_id") or 0))
            cid = int(c.get("id"))
        except (ValueError, TypeError):
            continue
        body = c.get("body") or ""
        if not rubric or "tauceti-reply:" in body or "tauceti-rubric:" in body:
            continue
        if best is None or cid > best["id"]:
            best = {"id": cid, "rubric": rubric, "created_at": c.get("created_at")}
    return best


def compact_meta(meta):
    if meta is None:
        return None
    out = {k: meta[k] for k in SPEC["meta_fields"] if k in meta}
    if isinstance(out.get("runs"), list):
        out["runs"] = [{k: r[k] for k in SPEC["run_fields"] if k in r} for r in out["runs"] if isinstance(r, dict)]
    return out


def rebase_heads(comments):
    heads = []
    for c in comments or []:
        if not isinstance(c, dict) or not isinstance(c.get("body"), str):
            continue
        body = c.get("body") or ""
        if (c.get("user") or {}).get("login") in SPEC["rebase_authors"] and body.startswith(
            "Merge-queue recovery for head `"
        ):
            for line in body.splitlines():
                if match := re.fullmatch(r"<!--tauceti-rebase:v1 ([a-f0-9]{40})-->", line):
                    heads.append(match[1])
    return sorted(set(heads))


def deprecation_repair_finished(comments, head):
    """Only our old authenticated request may clear after a new head passes CI.

    A current-head request from any existing trusted producer takes precedence.
    The caller supplies fresh PR/label/CI state; this function is pure bookkeeping.
    """
    if head in rebase_heads(comments):
        return False
    for comment in comments or []:
        for old in rebase_heads([comment]):
            if old != head and f"<!--tauceti-deprecation:v1 {old}-->" in comment["body"].splitlines():
                return True
    return False


def summarize(issue_comments, review_comments):
    return {
        "meta": compact_meta(scoreboard(issue_comments)),
        "contest": newest_contest(review_comments),
        "rebase_heads": rebase_heads(issue_comments),
    }
