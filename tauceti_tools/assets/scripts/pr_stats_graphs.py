#!/usr/bin/env python3
"""Generate the Tau Ceti pull-request statistics SVGs.

The live data path uses GitHub's GraphQL API for every pull request and its label
timeline, plus the repository issue-comments REST endpoint for posted Tau Ceti
scoreboards, keeping only those posted on a fetched pull request and carrying the review
engine's meta block for it.  A normalized snapshot can be written with ``--dump-data``
and replayed with ``--data``; tests and local chart work therefore need no network.
The seven-day series also counts the pull requests of the project's Forgejo repository
(``--forgejo-repo``), read from its issue and comment listings.

Generated files:

* ``pr-queue-age.svg`` — total open age, awaiting-author age, then in-review age;
* ``review-cycles-reached.svg`` — PRs reaching each review cycle;
* ``rolling-seven-day-history.svg`` — merge throughput, authors, and latency;
* ``cumulative-merges-by-contributor.svg``;
* ``cumulative-reviews-by-contributor.svg``;
* ``merges-by-roadmap-and-contributor.svg`` and ``reviews-by-roadmap-and-contributor.svg``
  — who works where, over a trailing window: by the arXiv category of the roadmap each PR
  advances (``--roadmap-dir``, a TauCetiRoadmap checkout, whose roadmaps each declare one in
  ``metadata.toml``), or by roadmap without one;
* ``pr-stats.json`` — definitions, exact contributor totals, and plotted series.

Contributor charts deliberately draw only the top N contributors plus one aggregate
``Other`` line.  The cap makes the SVG bounded even if the project eventually has
thousands of contributors; exact totals for every login remain in ``pr-stats.json``.
The implementation also stores daily series only for plotted lines, avoiding a
days-times-contributors memory blow-up.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import html
import itertools
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Iterable

from arxiv_categories import ARXIV_MATH, read_topic

# Kept equal to pr_status.core.REVIEW_APP_LOGINS; this script does not import pr_status.
REVIEW_APP_LOGINS = frozenset({"tauceti-review-poster[bot]"})
from chart_style import (
    BAR_BG, BG, MUTED, PALETTE, REFERENCE_WIDTH, SUBTITLE_SIZE, TEXT, base_css, card_rect, css_px, hover_metadata,
)
# The lifecycle rules live in one module because two readers of the same label
# timelines have to agree about what they mean, and once did not.
from pr_lifecycle import (  # noqa: F401  (re-exported for existing callers)
    LIFECYCLE_EPOCH,
    LIFECYCLE_LABELS,
    STAGE_ORDER,
    STATE_AUTHOR,
    STATE_AUTHOR_ACTION,
    STATE_LABELS,
    STATE_REVIEW,
    author_episode_start,
    current_stage,
    episodes,
    iso_z,
    latest_lifecycle_label,
    parse_dt,
    review_cycle_starts,
)

SCOREBOARD_MARKER = "<!--tauceti-scoreboard-->"
# A canonical scoreboard is the review engine's own comment: besides the public marker it
# carries the machine-readable meta block, declares kind "scoreboard", and names the pull
# request it was posted on.  Requiring all three rejects comments that merely quote the
# marker or paste another PR's scoreboard.  scripts/pr_status/core.py parses the same block
# when it derives a single PR's review state.
SCOREBOARD_META_KIND = "scoreboard"
ASSET_NAMES = [
    "pr-queue-age.svg",
    "review-cycles-reached.svg",
    "rolling-seven-day-history.svg",
    "cumulative-merges-by-contributor.svg",
    "cumulative-reviews-by-contributor.svg",
    "merges-by-roadmap-and-contributor.svg",
    "reviews-by-roadmap-and-contributor.svg",
    "merge-latency.svg",
    "pr-stats.json",
]

ROADMAP_PREFIX = "roadmap/"
# `none` says the work belongs to no roadmap and `Unknown` says nobody has decided yet.
# Neither names an area, so neither earns a column.
ROADMAP_EXCLUDE = {"roadmap/none", "roadmap/Unknown"}
# Trailing window for the who-works-where charts. The questions they answer are all
# forward-looking -- who could review this, which roadmap rests on one person -- and a
# whole-history cut answers them with the project's past: a roadmap that finished in July
# would hold its column for ever while an active one could not get in.
ROADMAP_WINDOW_DAYS = 90
ROADMAP_LIMIT = 15
# Rows. Twenty keeps every cell wide enough to carry its own number, which is what makes the
# chart readable as a static image with no hover to fall back on.
ROADMAP_CONTRIBUTOR_LIMIT = 20
# One hue, dark to light, because the surface is dark: on #101936 the bright end is the loud
# one. Lightness is monotonic across the ramp (the property a sequential scale actually needs;
# the categorical CVD validator does not apply and would fail it by construction). Every step
# carries an ink choice at 4.2:1 or better -- see ROADMAP_INK.
ROADMAP_RAMP = ["#1a4c57", "#1f6b7c", "#24899c", "#3cb0ba", "#5eead4"]
# Which ink each step takes. The two dark steps take the page's text colour (8.5:1 and 5.4:1);
# the three light ones take the page background (4.2:1, 6.7:1, 11.7:1).
ROADMAP_INK = [TEXT, TEXT, BG, BG, BG]
# Sentinels for the two aggregates, in a namespace neither a GitHub login nor a roadmap label
# can occupy: a login cannot contain `/` and every roadmap key here starts with `roadmap/`.
# Deliberately NOT a NUL, which would prove uniqueness more cheaply and fail far worse: NUL is
# illegal in XML, so one missed special case downstream would make the whole SVG unparsable
# rather than merely mislabelled.
OTHER_ROADMAP = "other/roadmaps"
OTHER_CONTRIBUTOR = "other/contributors"
# The column of a PR whose roadmap declares no arXiv category, in the same sentinel namespace.
UNSORTED_CATEGORY = "unsorted/category"
# Columns when grouping by category: enough for every category in use (seventeen in 2026-10), so
# no category is folded into "Other" merely for being small.
CATEGORY_LIMIT = 20
# Minimum viewBox width for the grids. They are served at `width: 100%`, so a narrow viewBox is
# scaled UP; matching the other cards keeps a sparse grid the same size on the page as a full one.
REFERENCE_HEATMAP_WIDTH = 1500
# Rough width of a character as a fraction of the font size, for this sans face at these sizes.
# Only used to reserve space, so erring high costs a little whitespace and erring low costs a
# collision; 0.55 is measured against the longest real roadmap names rather than guessed.
HEADING_ASPECT = 0.55
# How far apart chart_frame sets the lines of a card's header, in subtitle sizes. The baselines
# are design-space positions (title 44, subtitles 72 and 94) but the type scales with the card's
# width, so on a wide card the lines would close up: at 1,939 units the two subtitle lines touch.
# Each line therefore sits at least this far below the one above. Measured in Chromium, a subtitle
# line's ink spans about 0.94 of its size and the title's descent plus a subtitle's ascent about
# 1.02, so this keeps a gap of at least a quarter of the size. Cards up to 1,500 wide are unchanged.
HEADER_LEADING = 1.3
# XML 1.0 forbids most control characters outright, and no amount of entity escaping makes them
# legal -- a NUL in a label would produce a file no parser will read. GitHub documents label
# names as general strings and explicitly allows emoji, so this is not a theoretical input.
XML_FORBIDDEN = re.compile(
    "[^\u0009\u000A\u000D\u0020-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]")
HOUR_EDGES = [0, 1, 2, 4, 8, 12, 24, 48, 72, 120, math.inf]
HOUR_LABELS = [
    "<1h", "1–2h", "2–4h", "4–8h", "8–12h", "12–24h",
    "1–2d", "2–3d", "3–5d", "5d+",
]


def atomic_write(path: Path, content: str) -> None:
    """Replace path atomically so a failed daily run cannot leave a partial asset."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


# A depleted budget is not a transient failure: retrying in one, two and four seconds cannot
# refill it, so the original three quick attempts turned an hour-long wait into an immediate
# error. The budget refills on a fixed schedule, so the only useful response is to wait for it.
RATE_LIMITED = re.compile(r"rate limit|secondary rate|was submitted too quickly", re.I)
# One GraphQL window is an hour, plus a margin for clock skew between here and GitHub. Capped so
# that a misread reset time, or a limit that is not actually going to refill, fails the job
# instead of holding a runner indefinitely.
MAX_RATE_LIMIT_WAIT = 75 * 60


def rate_limit_reset_wait(resource: str = "graphql") -> float | None:
    """Seconds until the exhausted API budget refills, or None if unknown."""
    probe = subprocess.run(
        ["gh", "api", "rate_limit", "--jq", f".resources.{resource}.reset"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if probe.returncode != 0:
        return None
    try:
        reset = datetime.fromtimestamp(int(probe.stdout.strip()), tz=timezone.utc)
    except ValueError:
        return None
    # A small margin past the reset, so a request does not land on the boundary and fail again.
    return max(0.0, (reset - datetime.now(timezone.utc)).total_seconds()) + 5


def run_gh(arguments: list[str], attempts: int = 3) -> str:
    """Run gh, with short retries for transient failures and a long one for a depleted budget."""
    last_error = None
    waited = 0.0
    attempt = 0
    while attempt < attempts:
        result = subprocess.run(
            ["gh", *arguments], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            return result.stdout
        last_error = result.stderr.strip() or f"gh exited {result.returncode}"
        if RATE_LIMITED.search(last_error):
            resource = "graphql" if arguments[:2] == ["api", "graphql"] else "core"
            wait = rate_limit_reset_wait(resource)
            if wait is None or waited + wait > MAX_RATE_LIMIT_WAIT:
                raise RuntimeError(
                    f"GitHub rate limit exhausted and not waitable: {last_error}. "
                    f"Already waited {waited / 60:.0f} min of a {MAX_RATE_LIMIT_WAIT / 60:.0f} "
                    "min budget. Pass a recent snapshot with --since-data so that unchanged "
                    "pull requests do not have to be walked again.")
            print(f"rate limit reached; waiting {wait / 60:.1f} min for the budget to refill",
                  file=sys.stderr)
            time.sleep(wait)
            waited += wait
            # Waiting is not one of the three attempts: the request never really ran.
            continue
        attempt += 1
        if attempt < attempts:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GitHub query failed after {attempts} attempts: {last_error}")


def split_repo(repo: str) -> tuple[str, str]:
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", repo)
    if not match:
        raise ValueError(f"expected OWNER/REPO, got {repo!r}")
    return match.group(1), match.group(2)


# `mergeRenames` is the one timeline read in bulk; `apply_app_merges` says why that is safe.
PR_PAGE_QUERY = r"""
query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(first:100, after:$cursor,
                 orderBy:{field:CREATED_AT, direction:ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title additions deletions createdAt updatedAt mergedAt closedAt state isDraft
        author { login }
        labels(first:100) { nodes { name } pageInfo { hasNextPage } }
        mergeRenames: timelineItems(last:5, itemTypes:[RENAMED_TITLE_EVENT]) {
          nodes { ... on RenamedTitleEvent { createdAt currentTitle actor { __typename login } } }
        }
      }
    }
  }
}
"""

MERGE_RECEIPT_QUERY = r"""
query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      comments(last:100, before:$cursor) {
        pageInfo { hasPreviousPage startCursor }
        nodes { createdAt body author { __typename login } }
      }
    }
  }
}
"""

TIMELINE_PAGE_QUERY = r"""
query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      mergedAt closedAt state isDraft
      labels(first:100) { nodes { name } pageInfo { hasNextPage } }
      timelineItems(first:100, after:$cursor, itemTypes:[LABELED_EVENT]) {
        pageInfo { hasNextPage endCursor }
        nodes { ... on LabeledEvent { createdAt label { name } } }
      }
    }
  }
}
"""


# DO NOT batch timeline queries by aliasing several `pullRequest` fields into one request.
#
# It is tempting, because GitHub prices a request from the connection sizes it asks for and fifty
# aliases score the same single point as one. It was tried, and it silently returns INCOMPLETE
# timelines. Measured against this repository on 2026-09-06: in a 50-alias batch, PR #1556 came
# back with 2 label events and `hasNextPage: false`, where the paginating single-pull-request
# query returns 4 -- the missing one being the `ci-failed` that the pull request currently
# carries. Ten and twenty-five aliases happened to be complete for the same sample, and fifty was
# not, so the limit depends on the total nodes a query asks for and cannot be pinned to a size.
#
# What makes it unusable rather than merely awkward is that `hasNextPage` is computed against the
# truncated slice, so it reports false. There is no signal in the response to detect the loss
# from, and `totalCount` counts unfiltered timeline items, so it cannot stand in for one either.
# Silently dropping label events corrupts every lifecycle number downstream.
#
# The consistency guard in fetch_prs caught this within one deploy, which is the only reason it
# was a failed Pages run rather than quietly wrong statistics. Keep that guard.
#
# The saving would have been small in any case. Reuse (see reusable_timelines) already takes a
# normal run to a few dozen timelines, so batching them into one request saves tens of points out
# of a run costing hundreds. It is not worth a class of error that has no detector.
def timeline_from_node(pull: dict) -> dict:
    """The normalized timeline record, from either the batched or the single-PR query."""
    return {
        "merged_at": pull["mergedAt"],
        "closed_at": pull["closedAt"],
        "state": pull["state"],
        "is_draft": pull["isDraft"],
        "labels": [item["name"] for item in pull["labels"]["nodes"]],
        "labeled_events": normalized_events(pull["timelineItems"]["nodes"]),
    }


def graphql(query: str, **variables) -> dict:
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        flag = "-F" if isinstance(value, int) else "-f"
        args.extend([flag, f"{key}={value}"])
    payload = json.loads(run_gh(args))
    if payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL errors: {payload['errors']}")
    return payload["data"]


def normalized_events(events: list[dict]) -> list[dict]:
    return sorted((
        {"created_at": event["createdAt"], "label": event["label"]["name"]}
        for event in events if event.get("label")
    ), key=lambda event: event["created_at"])


def fetch_timeline(owner: str, name: str, number: int) -> dict:
    """Fetch one authoritative PR state and timeline, including every event page."""
    cursor = None
    events = []
    while True:
        pull = graphql(
            TIMELINE_PAGE_QUERY, owner=owner, name=name,
            number=number, cursor=cursor,
        )["repository"]["pullRequest"]
        if pull is None:
            raise RuntimeError(f"GitHub returned no timeline for PR #{number}")
        timeline = pull["timelineItems"]
        events.extend(timeline["nodes"])
        if not timeline["pageInfo"]["hasNextPage"]:
            record = timeline_from_node(pull)
            # Every page's events, not just the last one's.
            record["labeled_events"] = normalized_events(events)
            return record
        cursor = timeline["pageInfo"]["endCursor"]


def fetch_timelines(
    owner: str, name: str, numbers: list[int], reusable: dict[int, dict] | None = None,
) -> tuple[dict[int, dict], int]:
    """Current state and label timelines, one paginating request per pull request.

    One request each is the only shape that has been shown to return complete timelines; see the
    note above the batch query that used to live here. Cost is kept proportional to the work by
    `reusable`, which maps a number to a timeline recorded while the pull request had its current
    `updatedAt`, so nothing that has not moved is fetched at all. On a normal three-hourly run
    that leaves a few dozen pull requests rather than every one the project has ever had.

    Returns the timelines and how many pull requests had to be fetched.
    """
    reusable = reusable or {}
    result = {}
    fetched = 0
    for index, number in enumerate(numbers, start=1):
        cached = reusable.get(number)
        if cached is not None:
            result[number] = cached
            continue
        result[number] = fetch_timeline(owner, name, number)
        fetched += 1
        if fetched % 100 == 0 or index == len(numbers):
            print(f"fetched {fetched:,} PR timelines "
                  f"({len(numbers) - fetched:,} reused of {len(numbers):,})", file=sys.stderr)
    return result, fetched


def reusable_timelines(previous: dict | None, prs: list[dict]) -> dict[int, dict]:
    """Timelines from an earlier snapshot that the current `updatedAt` proves are still current.

    A pull request's `updatedAt` moves when it is labelled, so an unchanged one is a sound
    certificate that its label timeline is unchanged too. Anything the previous snapshot does
    not carry a matching `updated_at` and a recorded timeline for is simply refetched, so a
    snapshot written before this field existed, a truncated one, or none at all all degrade to
    the original full walk rather than to wrong data.
    """
    if not previous:
        return {}
    recorded = {}
    for entry in previous.get("prs") or []:
        if entry.get("updated_at") and "labeled_events" in entry:
            recorded[entry.get("number")] = entry
    reusable = {}
    for pr in prs:
        entry = recorded.get(pr["number"])
        if entry is None or entry["updated_at"] != pr["updated_at"]:
            continue
        # Shaped like a fetch_timeline result, because that is what the caller substitutes it
        # for. Only the event list comes from the previous snapshot: the state fields are taken
        # from this run's page query, which was read later than the cached copy and so is the
        # fresher of the two. They must agree anyway -- an unchanged `updatedAt` says so -- but
        # preferring the newer read keeps a reused pull request no staler than a fetched one.
        reusable[pr["number"]] = {
            "merged_at": pr["merged_at"],
            "closed_at": pr["closed_at"],
            "state": pr["state"],
            "is_draft": pr["is_draft"],
            "labels": pr["labels"],
            "labeled_events": entry.get("labeled_events") or [],
        }
    return reusable


def fetch_pr_pages(repo: str) -> list[dict]:
    """Every PR as the page query reads it: no label timeline, merge-app landings not yet applied."""
    owner, name = split_repo(repo)
    cursor = None
    prs = []
    while True:
        data = graphql(PR_PAGE_QUERY, owner=owner, name=name, cursor=cursor)
        connection = data["repository"]["pullRequests"]
        for raw in connection["nodes"]:
            labels = [item["name"] for item in raw["labels"]["nodes"]]
            prs.append({
                "number": raw["number"],
                "title": raw.get("title", ""),
                "additions": raw.get("additions", 0),
                "deletions": raw.get("deletions", 0),
                "created_at": raw["createdAt"],
                "updated_at": raw["updatedAt"],
                "merged_at": raw["mergedAt"],
                "closed_at": raw["closedAt"],
                "state": raw["state"],
                "is_draft": raw["isDraft"],
                "author": (raw.get("author") or {}).get("login") or "unknown",
                "labels": labels,
                "labeled_events": [],
                "merge_rename": merge_app_rename(raw),
            })
        if not connection["pageInfo"]["hasNextPage"]:
            return prs
        cursor = connection["pageInfo"]["endCursor"]


def fetch_prs(repo: str, previous: dict | None = None) -> list[dict]:
    """Fetch every PR and its authoritative, directly queried label timeline.

    `previous` is an earlier snapshot from this same repository, used only to skip refetching
    the timeline of a pull request that has not been touched since. See `reusable_timelines`.
    """
    owner, name = split_repo(repo)
    prs = fetch_pr_pages(repo)
    timeline_numbers = [
        pr["number"] for pr in prs
        if pr["closed_at"] is None or parse_dt(pr["closed_at"]) >= LIFECYCLE_EPOCH
    ]
    timelines, _ = fetch_timelines(
        owner, name, timeline_numbers, reusable_timelines(previous, prs))
    for pr in prs:
        if pr["number"] not in timelines:
            continue
        current = timelines[pr["number"]]
        pr.update(current)
        labeled_events = current["labeled_events"]
        current_state_labels = set(pr["labels"]) & STATE_LABELS
        event_labels = {event["label"] for event in labeled_events}
        missing_current = current_state_labels - event_labels
        if pr["state"] == "OPEN" and missing_current:
            missing = ", ".join(sorted(missing_current))
            raise RuntimeError(
                f"PR #{pr['number']} has current state label(s) without "
                f"matching timeline events: {missing}"
            )
        pr["labeled_events"] = labeled_events
    return prs


def names_scoreboard_for(metas: Iterable[str], pr_number: int) -> bool:
    """Whether one of a comment's meta blocks is this PR's own scoreboard block.

    The block is parsed rather than pattern-matched: a substring test for the PR number
    collides on prefixes, so a comment on #18 carrying #185's metadata would pass.  The kind
    must be exactly "scoreboard" and the number an integer equal to the PR the comment sits
    on.  ``True``/``False`` are ints in Python but never a PR number, so they are excluded.
    """
    for text in metas:
        try:
            meta = json.loads(text)
        except ValueError:
            continue
        if not isinstance(meta, dict) or meta.get("kind") != SCOREBOARD_META_KIND:
            continue
        number = meta.get("pr")
        if isinstance(number, int) and not isinstance(number, bool) and number == pr_number:
            return True
    return False


# The repository comment listing serves at most 300 pages (30,000 comments, about a week of
# TauCeti in 2026-11) from any one starting point, and `gh api --paginate` stops there without an
# error. So comments are read as a cursor walk in `updated_at` order, each request starting
# `since` the last comment the one before it returned. An edit moves a comment to the end of that
# order, so a walk that reaches the end has seen every live comment as it now reads. A deletion
# is invisible to it, which is what the weekly full walk from the beginning is for.
SCOREBOARD_FULL_RESCAN_DAYS = 7
# Re-read a little before the last walk's end, so a comment written while that walk was running
# cannot fall into the gap between the two.
SCOREBOARD_OVERLAP = timedelta(hours=1)
# Requests per run, shared by the walk that keeps up and a full walk in progress. A full walk
# (1,147 pages on 2026-11-10) therefore takes several runs, within the Actions token's 1,000
# REST requests an hour; the scoreboards it replaces stay published until it finishes.
SCOREBOARD_PAGES_PER_RUN = 300
COMMENT_PAGE_SIZE = 100
# One compact line per comment, parsed by jq so that review prose never reaches Python. Every
# comment carries its id and `updated_at`: the cursor needs the time, and a comment edited to
# drop the marker must leave the store. Marker comments also carry what their verdict needs.
SCOREBOARD_JQ = (
    '.[] | . as $comment | {id: $comment.id, updated_at: $comment.updated_at}'
    f' + if (($comment.body // "") | contains({json.dumps(SCOREBOARD_MARKER)})) | not'
    ' then {marker: false} else'
    ' ($comment.issue_url | split("/") | last) as $number'
    ' | ([try ($comment.body | capture("<!--tauceti-meta:v1\\\\s+'
    '(?<json>\\\\{[\\\\s\\\\S]*\\\\})\\\\s*-->").json'
    ' | fromjson) catch null][0] // null) as $meta'
    ' | {marker: true, number: $number, created_at: $comment.created_at,'
    '    user: $comment.user.login,'
    '    canonical: (($meta | type == "object")'
    '      and $meta.kind == "scoreboard"'
    '      and (($meta.pr | type) == "number")'
    '      and (($meta.pr | floor) == $meta.pr)'
    '      and ($meta.pr == ($number | tonumber)))} end'
)


def fetch_comment_page(repo: str, since: datetime | None, page: int) -> list[dict]:
    query = f"per_page={COMMENT_PAGE_SIZE}&sort=updated&direction=asc&page={page}"
    if since is not None:
        query += f"&since={iso_z(since)}"
    raw = run_gh(["api", f"repos/{repo}/issues/comments?{query}", "--jq", SCOREBOARD_JQ])
    return [json.loads(line) for line in raw.splitlines()]


def observe_comment(store: dict[str, dict], row: dict) -> None:
    identifier = str(row["id"])
    if not row.get("marker"):
        store.pop(identifier, None)
        return
    store[identifier] = {
        "id": identifier, "number": row.get("number"), "created_at": row["created_at"],
        "updated_at": row["updated_at"], "user": row.get("user"),
        "canonical": bool(row.get("canonical")),
    }


def walk_comments(
    repo: str, since: datetime | None, pages: int | None, store: dict[str, dict],
) -> tuple[datetime | None, int, bool]:
    """Fold the comments updated at or after `since` into `store`, oldest update first.

    Spends at most `pages` requests, or as many as it takes when `pages` is None. Returns where
    to resume, how many requests it spent, and whether it reached the end.
    """
    page = 1
    for spent in itertools.count(1) if pages is None else range(1, pages + 1):
        rows = fetch_comment_page(repo, since, page)
        for row in rows:
            observe_comment(store, row)
        if len(rows) < COMMENT_PAGE_SIZE:
            return (parse_dt(rows[-1]["updated_at"]) if rows else since), spent, True
        last = parse_dt(rows[-1]["updated_at"])
        # `since` is inclusive, so a full page ending where it began holds comments sharing one
        # timestamp; step through them by page number until the timestamp moves on.
        page = page + 1 if last == since else 1
        since = last
    return since, pages, False


def scoreboard_verdicts(
    store: dict[str, dict], pr_numbers: set[int], trusted_logins: set[str],
) -> tuple[list[dict], dict[str, str]]:
    """The accepted scoreboards and {comment id: rejection reason}, from the observed comments.

    Derived afresh every run, so a contributor who becomes trusted, or a pull request that joins
    the snapshot, changes the verdict on comments already observed without reading them again.
    """
    scoreboards, rejected = [], {}
    for identifier, comment in store.items():
        try:
            pr_number = int(comment["number"])
        except (KeyError, TypeError, ValueError):
            rejected[identifier] = "unparsable_issue_number"
            continue
        if pr_number not in pr_numbers:
            rejected[identifier] = "not_a_pull_request"
        elif comment.get("user") not in trusted_logins:
            rejected[identifier] = "untrusted_author"
        elif not comment.get("canonical"):
            rejected[identifier] = "no_canonical_scoreboard_meta"
        else:
            scoreboards.append({
                "id": identifier, "pr": pr_number, "created_at": comment["created_at"],
                "updated_at": comment["updated_at"], "user": comment.get("user") or "unknown",
            })
    scoreboards.sort(key=lambda entry: (entry["created_at"], entry["id"]))
    return scoreboards, rejected


def scoreboard_scan_state(previous: dict | None) -> dict:
    """The observed comments and walk positions an earlier snapshot recorded."""
    previous = previous or {}
    if "scoreboard_comments" not in previous:
        # A snapshot from the capped scan. Its accepted scoreboards stay published, kept up to
        # date from its watermark, while a full walk rebuilds the history that scan lost.
        comments = {
            entry["id"]: {"id": entry["id"], "number": entry["pr"],
                          "created_at": entry["created_at"], "updated_at": entry["updated_at"],
                          "user": entry["user"], "canonical": True}
            for entry in previous.get("scoreboards") or [] if "id" in entry
        }
        cursor = parse_dt(previous.get("scoreboards_scanned_at")) if comments else None
        return {"comments": comments, "cursor": cursor, "rescanned_at": None, "rescan": None}
    rescan = previous.get("scoreboard_rescan")
    return {
        "comments": dict(previous["scoreboard_comments"]),
        "cursor": parse_dt(previous.get("scoreboards_scanned_at")),
        "rescanned_at": parse_dt(previous.get("scoreboards_rescanned_at")),
        "rescan": rescan and {"started_at": parse_dt(rescan["started_at"]),
                              "cursor": parse_dt(rescan.get("cursor")),
                              "comments": dict(rescan["comments"])},
    }


def fetch_scoreboards(
    repo: str,
    pr_numbers: set[int],
    trusted_logins: set[str],
    previous: dict | None = None,
    now: datetime | None = None,
    pages: int | None = SCOREBOARD_PAGES_PER_RUN,
) -> tuple[list[dict], dict[str, str], dict]:
    """Canonical review scoreboards posted on this repository's pull requests.

    The repository issue-comments endpoint also serves ordinary issues and accepts comments
    from anyone. A comment counts only when its issue number is one of the fetched pull
    requests, its author has contributed a merged PR, and its parsed engine metadata declares a
    scoreboard for that same PR. The merged-author set comes from the same PR snapshot, so this
    works with the read-only Actions token and does not trust requester-dependent
    ``author_association`` values. Rejections are published by reason.

    Returns the scoreboards, the rejected comments as {id: reason}, and the walk state for the
    snapshot: the observed marker comments, the cursor of the walk that keeps up, and any full
    walk in progress. Until a first full walk finishes there is no cursor, and the scoreboards
    are not a history; see `scoreboard_history_unfinished`. `pages` caps the requests, shared
    by both walks; None walks to the end.
    """
    now = now or datetime.now(timezone.utc)
    state = scoreboard_scan_state(previous)
    store, cursor, rescan = state["comments"], state["cursor"], state["rescan"]
    rescanned_at, budget = state["rescanned_at"], pages
    # With nothing observed there is nothing to keep up: the full walk below covers it all.
    if cursor is not None:
        cursor, spent, _ = walk_comments(repo, cursor - SCOREBOARD_OVERLAP, budget, store)
        budget = None if budget is None else budget - spent
    if rescan is None and (
            rescanned_at is None or now - rescanned_at > timedelta(days=SCOREBOARD_FULL_RESCAN_DAYS)):
        rescan = {"started_at": now, "cursor": None, "comments": {}}
    if rescan is not None and (budget is None or budget > 0):
        rescan["cursor"], _, complete = walk_comments(
            repo, rescan["cursor"], budget, rescan["comments"])
        if complete:
            store, rescanned_at = rescan["comments"], rescan["started_at"]
            cursor = rescan["cursor"] or rescan["started_at"]
            rescan = None
    print("scoreboard comments: " + ("full walk complete" if rescan is None else
          f"full walk at {iso_z(rescan['cursor']) if rescan['cursor'] else 'its start'}"),
          file=sys.stderr)
    scoreboards, rejected = scoreboard_verdicts(store, pr_numbers, trusted_logins)
    return scoreboards, rejected, {
        "scoreboard_comments": store,
        "scoreboards_scanned_at": cursor and iso_z(cursor),
        "scoreboards_rescanned_at": rescanned_at and iso_z(rescanned_at),
        "scoreboard_rescan": rescan and {
            "started_at": iso_z(rescan["started_at"]),
            "cursor": rescan["cursor"] and iso_z(rescan["cursor"]),
            "comments": rescan["comments"],
        },
    }


def scoreboard_history_unfinished(data: dict) -> bool:
    """Whether a snapshot is mid-way through its first full scoreboard walk.

    Its scoreboards are then only what that walk has reached, and rendering them would publish
    a review history that has lost everything the walk has yet to read.
    """
    return bool(data.get("scoreboard_rescan")) and not data.get("scoreboards_scanned_at")


def load_previous(path: Path | None, repo: str) -> dict | None:
    """An earlier snapshot to reuse timelines from, or None if there is nothing usable.

    Every failure here is non-fatal and reported, because this file is only ever an
    optimisation: no snapshot, an unreadable one, or one from another repository all mean the
    same thing -- walk everything, as before. Silence would be the wrong choice in the other
    direction though. A cache that has quietly stopped being restored looks exactly like a
    healthy run until the budget runs out, which is the failure this whole path exists to
    prevent, so a miss says so on stderr.
    """
    if path is None:
        return None
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"no previous snapshot at {path}; walking every timeline", file=sys.stderr)
        return None
    except (OSError, ValueError) as error:
        print(f"ignoring unreadable snapshot {path}: {error}", file=sys.stderr)
        return None
    if previous.get("repo") != repo:
        print(f"ignoring snapshot for {previous.get('repo')!r}, expected {repo!r}",
              file=sys.stderr)
        return None
    print(f"reusing timelines from the snapshot taken at {previous.get('fetched_at')}",
          file=sys.stderr)
    return previous


# Bors squash merges and TauCeti queue landings reach main, but GitHub records their PRs as
# CLOSED with mergedAt=null. The landing app renames the PR "[Merged by Bors] - ..." as it closes
# it. Only the app can be that rename's actor, whereas anyone with write access can edit a title,
# and both apps also close PRs they rejected or found superseded, without renaming them.
MERGE_APPS = ("tau-ceti-bors", "tauceti-queue")
MERGED_TITLE_PREFIX = "[Merged by Bors] - "
MERGE_RECEIPT = "Pull request successfully merged into main.\n\n"
# Recorded in the snapshot, whose landings and receipt verdicts a run reuses only under the same
# rule. Until a run has checked every receipt candidate, the next run rescans scoreboards in full,
# so the run that finishes reconsiders every contributor its landings made trusted.
MERGE_EVIDENCE = {"rule": "merge app rename, else merge app receipt", "apps": list(MERGE_APPS)}
# Every other closed PR is checked for the app's success receipt, one request each: a PR closed
# while its batch was building still lands, unrenamed. A landing, once found, is kept; a verdict
# of none is reused while `updatedAt`, which a new comment moves, is unchanged. The cap spreads a
# first backlog over several runs of a caller that keeps its snapshot.
MERGE_RECEIPT_CHECKS_PER_RUN = 500


def is_merge_app(actor: dict | None) -> bool:
    return bool(actor) and actor.get("__typename") == "Bot" and actor.get("login") in MERGE_APPS


def first_landing(landings: Iterable[dict]) -> dict | None:
    # Repeated notifications still describe one merge, at its first success.
    return min(landings, key=lambda landing: parse_dt(landing["merged_at"]), default=None)


def merge_app_rename(raw: dict) -> dict | None:
    """The landing a merge app recorded by renaming this PR_PAGE_QUERY node, if it did."""
    return first_landing(
        {"merged_at": event["createdAt"], "merge_source": event["actor"]["login"]}
        for event in (raw.get("mergeRenames") or {}).get("nodes") or []
        if is_merge_app(event.get("actor"))
        and (event.get("currentTitle") or "").startswith(MERGED_TITLE_PREFIX))


def fetch_merge_receipt(owner: str, name: str, number: int) -> dict | None:
    """The landing recorded by a merge app's success receipt among all of one PR's comments."""
    cursor = None
    landings = []
    while True:
        pull = graphql(MERGE_RECEIPT_QUERY, owner=owner, name=name, number=number,
                       cursor=cursor)["repository"]["pullRequest"]
        if pull is None:
            raise RuntimeError(f"GitHub returned no comments for PR #{number}")
        comments = pull["comments"]
        landings.extend(
            {"merged_at": comment["createdAt"], "merge_source": comment["author"]["login"]}
            for comment in comments["nodes"]
            if is_merge_app(comment.get("author"))
            and (comment.get("body") or "").startswith(MERGE_RECEIPT))
        if not comments["pageInfo"]["hasPreviousPage"]:
            return first_landing(landings)
        cursor = comments["pageInfo"]["startCursor"]


def record_landing(pr: dict, landing: dict) -> None:
    created = parse_dt(pr.get("created_at"))
    if created and parse_dt(landing["merged_at"]) < created:
        raise ValueError(f"merge app landing predates creation of PR #{pr['number']}")
    pr.update(landing, state="MERGED")


def apply_app_merges(repo: str, prs: list[dict], previous: dict | None = None,
                     max_checks: int | None = None) -> bool:
    """Record merge-app landings as merges, before any chart or reviewer-trust calculation.

    The rename comes from the bulk page query, which can drop timeline events silently (see the
    note above `timeline_from_node`), so it is used only as positive evidence: a closed PR without
    one keeps a landing the previous snapshot recorded, or else goes to the receipt check, which
    reads the PR's own comments. Every candidate is checked now unless the caller, which must
    keep its snapshot for a later run to resume from, passes `max_checks`.

    Returns whether every candidate has a verdict, rather than some being left for a later run.
    """
    owner, name = split_repo(repo)
    previous = previous or {}
    earlier = ({entry["number"]: entry for entry in previous.get("prs") or []}
               if previous.get("merge_evidence") == MERGE_EVIDENCE else {})
    pending = []
    for pr in prs:
        landing = pr.pop("merge_rename", None)
        if pr.get("merged_at") or pr.get("state") != "CLOSED":
            continue
        entry = earlier.get(pr["number"], {})
        if landing is None and entry.get("merge_source") in MERGE_APPS:
            # Neither a rename nor a receipt can be undone, so a landing outlives `updatedAt`.
            landing = {"merged_at": entry["merged_at"], "merge_source": entry["merge_source"]}
        if landing is None:
            if entry.get("receipt_checked") == pr["updated_at"]:
                pr["receipt_checked"] = entry["receipt_checked"]
            else:
                pending.append(pr)
            continue
        record_landing(pr, landing)
    # Newest closures first, so a backlog never holds back the landings of the last few hours.
    pending.sort(key=lambda pr: pr["closed_at"], reverse=True)
    limit = len(pending) if max_checks is None else max_checks
    for pr in pending[:limit]:
        landing = fetch_merge_receipt(owner, name, pr["number"])
        pr["receipt_checked"] = pr["updated_at"]
        if landing is not None:
            record_landing(pr, landing)
    if len(pending) > limit:
        print(f"checked {limit:,} closed PRs for merge receipts; "
              f"{len(pending) - limit:,} left for later runs", file=sys.stderr)
    return len(pending) <= limit


def fetch_snapshot(repo: str, previous: dict | None = None,
                   receipt_checks: int | None = None,
                   scoreboard_pages: int | None = SCOREBOARD_PAGES_PER_RUN,
                   forgejo_repo: dict | None = None,
                   queue_landings: str | None = None) -> dict:
    # First, so a Forgejo outage fails the run before the GitHub walk spends its budget.
    forgejo = (fetch_forgejo_prs(forgejo_repo, (previous or {}).get("forgejo"))
               if forgejo_repo else None)
    landings = (fetch_queue_landings(queue_landings, (previous or {}).get("queue_landings"))
                if queue_landings else None)
    prs = fetch_prs(repo, previous)
    complete = apply_app_merges(repo, prs, previous, receipt_checks)
    # The review posting App posts scoreboards but never authors a merged PR.
    trusted_logins = {
        pr["author"] for pr in prs
        if pr.get("merged_at") and pr.get("author") != "unknown"
    } | REVIEW_APP_LOGINS
    scoreboards, rejected, scan_state = fetch_scoreboards(
        repo, {pr["number"] for pr in prs}, trusted_logins, previous, pages=scoreboard_pages,
    )
    return {
        # 2: pull requests carry `updated_at` and scoreboards carry `id`, both so that the next
        # run can tell what it may keep. A version 1 snapshot is still readable -- everything
        # that reuses it checks for the field it needs and falls back to fetching -- so this
        # bump records the shape rather than gating on it.
        "schema_version": 2,
        "repo": repo,
        "fetched_at": iso_z(datetime.now(timezone.utc)),
        "prs": prs,
        "merge_evidence": MERGE_EVIDENCE,
        "merge_evidence_complete": complete,
        "scoreboards": scoreboards,
        "rejected_scoreboard_comments": dict(sorted(Counter(rejected.values()).items())),
        **scan_state,
        **({"forgejo": forgejo} if forgejo else {}),
        **({"queue_landings": landings} if landings else {}),
    }


# The TauCeti queue's per-PR landing feed: first queue approval and landing time of each pull request
# it landed, which neither forge records. Approval is the queue admitting a head of the pull request
# after an all-green review. The feed serves at most 35 days, so each run folds it into what the
# previous snapshot held, keeping QUEUE_LANDINGS_KEEP of history.
QUEUE_LANDINGS = {
    "TauCetiProject/TauCeti": "https://queue.taucetiproject.org/statistics/landings?days=35",
}
QUEUE_LANDINGS_SCHEMA = "tauceti-queue.landings/v1"
QUEUE_LANDINGS_FIELDS = ["forge", "number", "first_approved_at", "landed_at"]
QUEUE_LANDINGS_KEEP = timedelta(days=120)
QUEUE_LANDINGS_MAX_BYTES = 32 * 1024 * 1024


def read_queue_landings(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache",
                                                   "User-Agent": "TauCetiPrStats/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read(QUEUE_LANDINGS_MAX_BYTES + 1)
    if len(raw) > QUEUE_LANDINGS_MAX_BYTES:
        raise ValueError("queue landings exceed bound")
    feed = json.loads(raw)
    if (not isinstance(feed, dict) or feed.get("schema") != QUEUE_LANDINGS_SCHEMA
            or feed.get("fields") != QUEUE_LANDINGS_FIELDS
            or not isinstance(feed.get("since"), (int, float))
            or not isinstance(feed.get("observed_at"), (int, float))
            or not isinstance(feed.get("landings"), list)):
        raise ValueError("unexpected queue landings shape")
    for entry in feed["landings"]:
        if (not isinstance(entry, list) or len(entry) != 4 or not isinstance(entry[0], str)
                or type(entry[1]) is not int
                or not all(isinstance(t, (int, float)) for t in entry[2:])):
            raise ValueError("unexpected queue landing entry")
    return feed


def fetch_queue_landings(url: str, previous: dict | None) -> dict | None:
    """The queue's landings, folded into `previous` (a snapshot's `queue_landings`) where contiguous.

    A failed read keeps what the previous snapshot held, reported on stderr: the chart then ends
    where its data does rather than the whole run failing over an optional series.
    """
    try:
        feed = read_queue_landings(url)
    except (OSError, ValueError) as exc:
        print(f"queue landings: {exc}; keeping the previous snapshot's", file=sys.stderr)
        return previous
    since, observed = float(feed["since"]), float(feed["observed_at"])
    kept: dict[tuple[str, int], list] = {}
    coverage = since
    if previous and previous.get("source") == url and previous.get("observed_at", 0) >= since:
        # Contiguous: the previous coverage reaches into this feed's window, so the two join.
        coverage = min(since, previous.get("coverage_since", since))
        floor = observed - QUEUE_LANDINGS_KEEP.total_seconds()
        coverage = max(coverage, floor)
        for entry in previous.get("landings") or []:
            if entry[3] > floor:
                kept[(entry[0], entry[1])] = entry
    for entry in feed["landings"]:
        kept[(entry[0], entry[1])] = entry
    print(f"queue landings: {len(feed['landings']):,} in the feed, {len(kept):,} kept since "
          f"{iso_z(datetime.fromtimestamp(coverage, timezone.utc))}", file=sys.stderr)
    return {"source": url, "observed_at": observed, "coverage_since": coverage,
            "landings": sorted(kept.values(), key=lambda e: (e[3], e[0], e[1]))}


# Since 2026-11 most of the project's pull requests are on its Forgejo instance, and the TauCeti
# queue lands them as it lands GitHub's. Only the seven-day series counts both forges; every other
# figure still describes the GitHub repository alone. Forgejo pull-request numbers are a separate
# sequence that overlaps GitHub's, so its pull requests stay in their own part of the snapshot and
# never meet a GitHub number.
#
# Each Forgejo repository is paired with the logins its queue posts as. The queue posts the same
# "Pull request successfully merged into main." receipt there as on GitHub, and that receipt, by
# one of these logins, is the evidence of a landing: the "[Merged by Bors]" title that comes with
# it is only a hint, since its author can edit it.
FORGEJO_REPOS = {
    "TauCetiProject/TauCeti": {
        "url": "https://git.taucetiproject.org/TauCetiProject/TauCeti",
        "merge_logins": ["kim-em"],
    },
}
# Forgejo's largest page (its `max_response_items`). A smaller server cap only costs requests: the
# walks stop at an empty page, never at a short one.
FORGEJO_PAGE_SIZE = 50
# Anything edited while a walk runs is read again by the next one, which asks for everything
# updated since a little before this walk began.
FORGEJO_WALK_OVERLAP = timedelta(minutes=10)
# Both listings page by offset, so a pull request or comment deleted mid-walk can shift another
# past the reader for good. Every so often a walk therefore starts over, replacing what it knew.
FORGEJO_FULL_WALK_EVERY = timedelta(days=7)
FORGEJO_EVIDENCE = "merge app receipt by a merge login, else Forgejo merged_at"


def split_forgejo_repo(url: str) -> tuple[str, str]:
    """The API root and OWNER/REPO of a Forgejo repository URL."""
    match = re.fullmatch(r"(https://[^/\s]+)/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/?", url)
    if not match:
        raise ValueError(f"expected https://HOST/OWNER/REPO, got {url!r}")
    return f"{match.group(1)}/api/v1", match.group(2)


def forgejo_get(url: str, attempts: int = 3) -> list[dict]:
    last_error = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 ** attempt)
        request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": "tauceti-pr-stats"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                page = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as error:
            last_error = error
            continue
        if not isinstance(page, list):
            raise RuntimeError(f"Forgejo returned {type(page).__name__}, not a list, for {url}")
        return page
    raise RuntimeError(f"Forgejo request failed after {attempts} attempts: {url}: {last_error}")


def forgejo_walk(api: str, path: str, query: dict) -> list[dict]:
    """Every row of one Forgejo listing, page by page until an empty one."""
    rows, page = [], 1
    while True:
        batch = forgejo_get(f"{api}/{path}?"
                            + urllib.parse.urlencode({**query, "limit": FORGEJO_PAGE_SIZE,
                                                      "page": page}))
        if not batch:
            return rows
        rows.extend(batch)
        page += 1


def forgejo_pull(row: dict) -> dict:
    """The normalized record of one row of Forgejo's issue listing, which is a pull request."""
    pull = row.get("pull_request") or {}
    closed_at = row.get("closed_at")
    return {
        "number": row["number"],
        "title": row.get("title") or "",
        "created_at": iso_z(parse_dt(row["created_at"])),
        "updated_at": row.get("updated_at"),
        "closed_at": iso_z(parse_dt(closed_at)) if closed_at else None,
        "native_merged_at": (iso_z(parse_dt(pull["merged_at"]))
                             if pull.get("merged") and pull.get("merged_at") else None),
        "state": "OPEN" if row.get("state") == "open" else "CLOSED",
        "author": (row.get("user") or {}).get("login") or "unknown",
        "labels": [label.get("name") for label in row.get("labels") or [] if label.get("name")],
    }


FORGEJO_PULL_NUMBER = re.compile(r"/(?:issues|pulls)/(\d+)$")


def forgejo_receipt(comment: dict, logins: set[str]) -> tuple[int, str] | None:
    """(pull request number, landing time) if this comment is a merge login's success receipt."""
    if ((comment.get("user") or {}).get("login") not in logins
            or not (comment.get("body") or "").startswith(MERGE_RECEIPT)):
        return None
    match = FORGEJO_PULL_NUMBER.search(comment.get("pull_request_url") or comment.get("issue_url") or "")
    return (int(match.group(1)), iso_z(parse_dt(comment["created_at"]))) if match else None


def record_receipt(receipts: dict[str, str], number: int, merged_at: str) -> None:
    # Repeated receipts describe one landing, at its first.
    key = str(number)
    if key not in receipts or parse_dt(merged_at) < parse_dt(receipts[key]):
        receipts[key] = merged_at


def fetch_forgejo_prs(source: dict, previous: dict | None = None) -> dict:
    """Every pull request of a Forgejo repository and the landings its queue recorded.

    `source` is a FORGEJO_REPOS entry. Two listings are read, each only for what changed since
    `previous` began its walk: the issue listing for the pull requests, and the repository's
    comments for the queue's receipts, about a page per fifty changes plus one empty page each. A
    landing, once its receipt is found, is kept, as on GitHub. A closed pull request carrying the
    merged title without a known receipt has its own comments read, in case its receipt was
    shifted past the walk; normally there is none. A cold start, and a walk a week after the last
    full one, read both listings whole: about 75 and 190 pages in 2026-11.
    """
    url, logins = source["url"], set(source["merge_logins"])
    api, repo = split_forgejo_repo(url)
    started = datetime.now(timezone.utc)
    previous = previous or {}
    same_rule = (previous.get("repo") == url and previous.get("merge_evidence") == FORGEJO_EVIDENCE
                 and sorted(previous.get("merge_logins") or []) == sorted(logins))
    resumable = (same_rule and previous.get("walk_started_at") and previous.get("full_walk_at")
                 and started - parse_dt(previous["full_walk_at"]) < FORGEJO_FULL_WALK_EVERY)
    known: dict[int, dict] = {}
    # Receipts outlive a full walk too: one deleted since cannot undo its landing.
    receipts: dict[str, str] = dict(previous.get("receipts") or {}) if same_rule else {}
    query: dict = {}
    if resumable:
        known = {entry["number"]: entry for entry in previous.get("prs") or []}
        query["since"] = iso_z(parse_dt(previous["walk_started_at"]) - FORGEJO_WALK_OVERLAP)
    # A full walk replaces the set, but an unchanged pull request keeps its verdict of no receipt.
    checked = ({entry["number"]: entry.get("receipt_checked") for entry in previous.get("prs") or []}
               if same_rule else {})
    rows = forgejo_walk(api, f"repos/{repo}/issues", {"type": "pulls", "state": "all", **query})
    for row in rows:
        if row.get("pull_request") is not None:
            record = forgejo_pull(row)
            if checked.get(record["number"]) == record["updated_at"]:
                record["receipt_checked"] = record["updated_at"]
            known[record["number"]] = record
    comments = forgejo_walk(api, f"repos/{repo}/issues/comments", query)
    for comment in comments:
        receipt = forgejo_receipt(comment, logins)
        if receipt:
            record_receipt(receipts, *receipt)
    # A verdict of none is kept while the pull request's `updatedAt`, which a comment moves, is.
    hinted = [pr for pr in known.values()
              if pr["state"] == "CLOSED" and str(pr["number"]) not in receipts
              and pr["title"].startswith(MERGED_TITLE_PREFIX)
              and pr.get("receipt_checked") != pr["updated_at"]]
    for pr in hinted:
        pr["receipt_checked"] = pr["updated_at"]
        # One request: this endpoint ignores `page` and returns the whole history every time.
        for comment in forgejo_get(f"{api}/repos/{repo}/issues/{pr['number']}/comments"):
            receipt = forgejo_receipt(comment, logins)
            if receipt and receipt[0] == pr["number"]:
                record_receipt(receipts, *receipt)
    prs = []
    for number in sorted(known):
        pr = dict(known[number])
        landing = receipts.get(str(number)) or pr.get("native_merged_at")
        created = parse_dt(pr["created_at"])
        if landing and parse_dt(landing) < created:
            raise ValueError(f"Forgejo landing predates creation of pull request #{number}")
        pr["merged_at"] = landing
        if landing:
            pr["state"] = "MERGED"
        prs.append(pr)
    print(f"forgejo: read {len(rows):,} pull requests, {len(comments):,} comments and "
          f"{len(hinted):,} pull requests' comments "
          f"({'since ' + query['since'] if query else 'full walk'}); {len(prs):,} pull requests, "
          f"{sum(1 for pr in prs if pr['merged_at']):,} landed", file=sys.stderr)
    return {
        "repo": url,
        "merge_logins": sorted(logins),
        "merge_evidence": FORGEJO_EVIDENCE,
        "walk_started_at": iso_z(started),
        "full_walk_at": previous["full_walk_at"] if resumable else iso_z(started),
        "receipts": dict(sorted(receipts.items(), key=lambda item: int(item[0]))),
        "prs": prs,
    }


def queue_age_metrics(prs: list[dict], snapshot: datetime) -> dict:
    total, author, review = [], [], []
    missing_transition = 0
    other = 0
    for pr in prs:
        if pr["state"] != "OPEN":
            continue
        created = parse_dt(pr["created_at"])
        total.append(max(0.0, (snapshot - created).total_seconds() / 3600))
        labels = set(pr.get("labels") or [])
        if pr.get("is_draft"):
            other += 1
        elif labels & STATE_AUTHOR_ACTION:
            start = author_episode_start(pr)
            if start is None:
                start = created
                missing_transition += 1
            author.append(max(0.0, (snapshot - start).total_seconds() / 3600))
        elif labels & STATE_REVIEW:
            # The clock runs from the current cycle's start: claiming it as review-in-progress,
            # or having awaiting-review restored afterwards, is not a fresh wait.
            starts = review_cycle_starts(pr)
            start = (
                starts[-1]
                if starts and latest_lifecycle_label(pr) in STATE_REVIEW else None
            )
            if start is None:
                start = created
                missing_transition += 1
            review.append(max(0.0, (snapshot - start).total_seconds() / 3600))
        else:
            other += 1
    return {
        "total_open_hours": total,
        "awaiting_author_hours": author,
        "in_review_hours": review,
        "other_open_prs": other,
        "missing_transition_fallbacks": missing_transition,
    }


def review_cycle_metrics(prs: list[dict]) -> dict:
    starts = {pr["number"]: review_cycle_starts(pr) for pr in prs}
    counts = {
        str(number): len(cycles) for number, cycles in starts.items() if cycles
    }
    observed = list(counts.values())
    epoch = min((cycle for cycles in starts.values() for cycle in cycles), default=None)
    maximum = max(observed, default=0)
    reach = [
        {"cycle": cycle, "prs": sum(count >= cycle for count in observed)}
        for cycle in range(1, maximum + 1)
    ]
    return {
        "definition": "number of times the PR entered review from an author or CI state",
        "label_epoch": epoch.date().isoformat() if epoch else None,
        "reviewed_prs": len(observed),
        "total_cycles": sum(observed),
        "max_cycle": maximum,
        "reach": reach,
        "cycles_by_pr": counts,
    }


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def rolling_metrics(prs: list[dict], last_full_day: date, history_days: int) -> list[dict]:
    project_start = min(parse_dt(pr["created_at"]).date() for pr in prs)
    display_start = max(project_start, last_full_day - timedelta(days=history_days - 1))
    parsed = [{
        "created": parse_dt(pr["created_at"]),
        "merged": parse_dt(pr.get("merged_at")),
        "author": pr["author"],
    } for pr in prs]
    rows = []
    day = display_start
    while day <= last_full_day:
        end = datetime.combine(day + timedelta(days=1), day_time.min, tzinfo=timezone.utc)
        start = end - timedelta(days=7)
        merged = [pr for pr in parsed if pr["merged"] and start <= pr["merged"] < end]
        opened = [pr for pr in parsed if start <= pr["created"] < end]
        latencies = [
            (pr["merged"] - pr["created"]).total_seconds() / 3600
            for pr in merged
        ]
        rows.append({
            "date": day.isoformat(),
            "merges": len(merged),
            "active_authors": len({pr["author"] for pr in opened}),
            "median_merge_hours": percentile(latencies, .5),
            "p90_merge_hours": percentile(latencies, .9),
        })
        day += timedelta(days=1)
    return rows


MERGE_LATENCY_WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}
MERGE_LATENCY_MEASURES = ("open_to_land", "open_to_approval", "approval_to_land")
MERGE_LATENCY_STATS = (("p25", .25), ("median", .5), ("p75", .75), ("p90", .9))


def latency_summary(values: list[float]) -> dict:
    return {"count": len(values), **{name: percentile(values, q) for name, q in MERGE_LATENCY_STATS}}


def merge_latency_metrics(github: list[dict], forgejo: list[dict], landings: dict | None,
                          last_full_day: date, snapshot: datetime, history_days: int) -> dict | None:
    """Time to merge split at the queue's first approval, over 24-hour and seven-day windows.

    Each daily point covers pull requests the queue landed in the window ending at the end of that
    UTC day; `current` covers the window ending at the snapshot. A day appears only once the queue
    feed covers its whole window, so a seven-day point never rests on a partial week.
    """
    if not landings or not landings.get("landings"):
        return None
    created = {("github", pr["number"]): parse_dt(pr["created_at"]) for pr in github}
    created.update({("forgejo", pr["number"]): parse_dt(pr["created_at"]) for pr in forgejo})
    rows, unmatched = [], 0
    for forge, number, approved, landed in landings["landings"]:
        opened = created.get((forge, number))
        if opened is None:
            unmatched += 1
            continue
        opened_at = opened.timestamp()
        # A head can be approved before a later edit of the pull request's record, never before it
        # was opened; clamp so the two parts still add up to the whole.
        approved = min(max(approved, opened_at), landed)
        rows.append((landed, {"open_to_land": (landed - opened_at) / 3600,
                              "open_to_approval": (approved - opened_at) / 3600,
                              "approval_to_land": (landed - approved) / 3600}))
    rows.sort(key=lambda row: row[0])
    times = [row[0] for row in rows]

    def window(end: float, span: timedelta) -> dict:
        lo = bisect.bisect_left(times, end - span.total_seconds())
        hi = bisect.bisect_left(times, end)
        chosen = [row[1] for row in rows[lo:hi]]
        return {measure: latency_summary([c[measure] for c in chosen])
                for measure in MERGE_LATENCY_MEASURES}

    coverage = landings["coverage_since"]
    start = max(datetime.fromtimestamp(coverage, timezone.utc).date() + timedelta(days=1),
                last_full_day - timedelta(days=history_days - 1))
    dates, series = [], {name: {m: {k: [] for k in ("count", *dict(MERGE_LATENCY_STATS))}
                                for m in MERGE_LATENCY_MEASURES} for name in MERGE_LATENCY_WINDOWS}
    day = start
    while day <= last_full_day:
        end = datetime.combine(day + timedelta(days=1), day_time.min, tzinfo=timezone.utc).timestamp()
        dates.append(day.isoformat())
        for name, span in MERGE_LATENCY_WINDOWS.items():
            covered = end - span.total_seconds() >= coverage
            summary = window(end, span) if covered else None
            for measure in MERGE_LATENCY_MEASURES:
                for key, values in series[name][measure].items():
                    values.append(summary[measure][key] if summary else None)
        day += timedelta(days=1)
    now = min(snapshot.timestamp(), landings["observed_at"])
    return {
        "definition": {
            "population": "pull requests the TauCeti merge queue landed, on GitHub and on forgejo_repo",
            "approval": "the queue first admitting a head of the pull request, after an all-green review",
            "open_to_land": "PR creation to the queue's landing",
            "open_to_approval": "PR creation to the first approval",
            "approval_to_land": "first approval to the queue's landing",
            "windows": "24h: landed in the 24 hours ending at the point; 7d: in the seven days",
        },
        "coverage_since": iso_z(datetime.fromtimestamp(coverage, timezone.utc)),
        "unmatched_landings": unmatched,
        "dates": dates,
        "series": series,
        "current": {"as_of": iso_z(datetime.fromtimestamp(now, timezone.utc)),
                    **{name: window(now, span) for name, span in MERGE_LATENCY_WINDOWS.items()}},
    }


def utc_day(moment: datetime) -> date:
    """The UTC calendar day of an instant, whatever offset it arrived with."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date()


def cumulative_chart_series(
    events: Iterable[tuple[datetime, str]], start: date, end: date, limit: int,
    cutoff: datetime | None = None,
) -> tuple[list[str], list[str], dict[str, list[int]], Counter]:
    """Build bounded plotted series while retaining exact totals for every login.

    `cutoff` bounds what is COUNTED; `end` bounds what is PLOTTED. They are different
    questions, and collapsing them into one silently rewrites a published contract:
    pr-stats.json documents its contributor totals as exact through the snapshot instant, and
    the charts stop at the last completed UTC day so that a few hours of the current day
    cannot read as a downturn. A contributor whose only merge landed this morning is therefore
    in the totals and not yet on the line, which is what both of those promises require.
    """
    known = [
        (timestamp, name) for timestamp, name in events
        if cutoff is None or timestamp <= cutoff
    ]
    totals = Counter(name for _, name in known)
    selected = sorted(totals, key=lambda name: (-totals[name], name.casefold()))[:limit]
    selected_set = set(selected)
    omitted = len(totals) - len(selected)
    other = f"Other ({omitted:,} contributors)" if omitted else None
    names = selected + ([other] if other else [])
    daily = Counter()
    for timestamp, name in known:
        # Normalized, not `timestamp.date()`. parse_dt keeps whatever offset the snapshot
        # carried, and the day boundary this is bucketing against is a UTC one.
        day = utc_day(timestamp)
        if day > end:
            continue
        group = name if name in selected_set else other
        daily[(day, group)] += 1
    running = Counter()
    series = {name: [] for name in names}
    dates = []
    day = start
    while day <= end:
        dates.append(day.isoformat())
        for name in names:
            running[name] += daily[(day, name)]
            series[name].append(running[name])
        day += timedelta(days=1)
    return dates, names, series, totals


def roadmap_of(labels: Iterable[str]) -> str | None:
    """The single roadmap a PR belongs to, or None when that is not a settled question.

    Exactly one `roadmap/<Area>` label, ignoring `none` and `Unknown`. Two area labels is not
    a PR that counts half towards each: it is a PR nobody has decided about, and splitting it
    would put made-up numbers in a chart about who works where.

    Deliberately *not* scripts/loc_roadmap_graph.py's rule, which also drops PRs whose title
    marks them as maintenance. That chart measures mathematics landed, where a refactor is
    genuinely not new material. This one measures who works on what, and somebody who keeps
    the PDE build honest is a person the PDE roadmap depends on.
    """
    areas = [name for name in labels
             if name.startswith(ROADMAP_PREFIX) and name not in ROADMAP_EXCLUDE]
    return areas[0] if len(areas) == 1 else None


def roadmap_categories(roadmap_dir: Path) -> dict[str, str]:
    """`roadmap/<Area>` -> the arXiv category that roadmap declares in its `metadata.toml`
    (`topic = "math.NT"`), for the roadmaps (directories with a README.md) under `TauCetiRoadmap/`
    and `Completed/` of a TauCetiRoadmap checkout. Where a name is in both, the active roadmap
    decides, whatever its metadata says: an archived roadmap's category never stands in for an
    active one's, so an active roadmap with no file, an unreadable one or a value that is not an
    arXiv mathematics category is left out, and its PRs count as unsorted, like any roadmap without
    a category.

    A missing checkout gives an empty map, and a roadmap's missing, unreadable or malformed
    `metadata.toml` leaves only that roadmap out. An error listing the checkout itself (an
    unreadable `TauCetiRoadmap/`, say) propagates as `OSError` rather than being taken for an empty
    one, which would publish every PR as unsorted; the Pages step that runs this keeps the committed
    charts when generation fails."""
    out: dict[str, str] = {}
    decided: set[str] = set()
    for base in ("TauCetiRoadmap", "Completed"):
        root = roadmap_dir / base
        if not root.is_dir():
            continue
        for d in sorted(p for p in root.iterdir() if p.is_dir() and (p / "README.md").is_file()):
            if d.name in decided:
                continue
            decided.add(d.name)
            topic = read_topic(d)
            if topic is not None:
                out[f"{ROADMAP_PREFIX}{d.name}"] = topic
    return out


def roadmap_matrix(
    prs: list[dict], scoreboards: list[dict], last_full_day: date,
    window_days: int = ROADMAP_WINDOW_DAYS, roadmap_limit: int = ROADMAP_LIMIT,
    contributor_limit: int = ROADMAP_CONTRIBUTOR_LIMIT,
    column_of: dict[str, str] | None = None,
) -> dict:
    """Who merged and who reviewed, per roadmap, over the trailing window.

    With `column_of` (a roadmap label -> arXiv category map, `roadmap_categories`) the columns are
    categories instead: each PR counts under its roadmap's category, and a PR whose roadmap
    declares none under UNSORTED_CATEGORY.

    Both slices are a local join over the snapshot this module already fetches: each PR record
    carries its author and its labels, and each scoreboard carries the PR it was posted on. No
    extra API call.

    Columns are the roadmaps with the most merges in the window, so the chart tracks where the
    project is working now. Rows are the busiest contributors in that same window, counted per
    chart, because the people merging and the people reviewing are not the same set.
    """
    start = last_full_day - timedelta(days=window_days - 1)
    end = last_full_day

    def within(stamp: str | None) -> bool:
        # utc_day, not .date(): `last_full_day` is a UTC day and parse_dt keeps whatever offset
        # the snapshot carried, so an offline fixture with an offset would cross the boundary
        # in the wrong direction. Same fix as the cumulative charts and loc_roadmap_graph.
        return bool(stamp) and start <= utc_day(parse_dt(stamp)) <= end

    area_of_pr = {pr["number"]: roadmap_of(pr["labels"]) for pr in prs}
    if column_of is not None:
        area_of_pr = {number: (column_of.get(area, UNSORTED_CATEGORY) if area else None)
                      for number, area in area_of_pr.items()}

    merges = Counter()
    for pr in prs:
        area = area_of_pr[pr["number"]]
        if area and within(pr.get("merged_at")):
            merges[(pr["author"], area)] += 1

    reviews = Counter()
    for board in scoreboards:
        area = area_of_pr.get(board["pr"])
        if area and within(board.get("created_at")):
            reviews[(board["user"], area)] += 1

    # Ranked on merges alone, and used for both charts, so the two are read against the same
    # columns in the same order. Reviews follow the work rather than defining their own areas.
    by_area = Counter()
    for (_, area), count in merges.items():
        by_area[area] += count
    # Explicit tie-break. `most_common` keeps encounter order for equal counts, which at the
    # fifteen-roadmap boundary means the columns depend on the order pull requests came back in.
    ranked = sorted(by_area, key=lambda area: (-by_area[area], area.casefold(), area))
    columns = ranked[:roadmap_limit]
    bundled = set(ranked[roadmap_limit:])
    # Areas that only ever appear in reviews still belong in the remainder rather than nowhere.
    bundled |= {area for _, area in reviews if area not in columns}

    def fold(counts: Counter) -> Counter:
        folded = Counter()
        for (who, area), count in counts.items():
            folded[(who, OTHER_ROADMAP if area in bundled else area)] += count
        return folded

    return {
        "grouping": "roadmap" if column_of is None else "category",
        "window_days": window_days,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "columns": columns,
        "bundled": sorted(bundled),
        # The UNFOLDED counts, which is what the charts promise is "in JSON". Folding is a
        # rendering decision -- it exists so a grid stays legible -- and once both axes have
        # been folded the original distribution is gone: `bundled` keeps the roadmap names and
        # the row totals keep the people, but not who did what where. Published as records
        # rather than as a composite key so no consumer has to know how the key was joined.
        "exact": {
            "merges": _records(merges, "roadmap" if column_of is None else "category"),
            "reviews": _records(reviews, "roadmap" if column_of is None else "category"),
        },
        "merges": _slice(fold(merges), columns, bundled, contributor_limit),
        "reviews": _slice(fold(reviews), columns, bundled, contributor_limit),
    }


def _records(counts: Counter, column: str = "roadmap") -> list[dict]:
    """One record per (contributor, column) pair, ordered biggest first then by name; `column` names
    what a column is (`roadmap`, or `category` when grouped by arXiv category)."""
    return [
        {"contributor": who, column: area, "count": count}
        for (who, area), count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0][0].casefold(), item[0][1]))
    ]


def _slice(folded: Counter, columns: list[str], bundled: set[str],
           contributor_limit: int) -> dict:
    """Bound one matrix to its busiest contributors, keeping the rest as one row."""
    axis = columns + ([OTHER_ROADMAP] if bundled else [])
    totals = Counter()
    for (who, _), count in folded.items():
        totals[who] += count
    ranked = [who for who, _ in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0].casefold()))]
    rows = ranked[:contributor_limit]
    spare = set(ranked[contributor_limit:])
    cells = Counter()
    for (who, area), count in folded.items():
        cells[(OTHER_CONTRIBUTOR if who in spare else who, area)] += count
    return {
        "axis": axis,
        "rows": rows + ([OTHER_CONTRIBUTOR] if spare else []),
        "omitted_contributors": len(spare),
        "counts": {f"{who}\t{area}": count for (who, area), count in cells.items()},
        "totals_by_contributor": dict(totals.most_common()),
    }


def histogram(values: Iterable[float]) -> list[int]:
    counts = [0] * len(HOUR_LABELS)
    for value in values:
        for index, (low, high) in enumerate(zip(HOUR_EDGES, HOUR_EDGES[1:])):
            if low <= value < high:
                counts[index] += 1
                break
    return counts


def nice_axis_max(value: float) -> int:
    if value <= 0:
        return 5
    magnitude = 10 ** math.floor(math.log10(value))
    for multiple in (1, 2, 2.5, 5, 10):
        candidate = multiple * magnitude
        if candidate >= value:
            return max(5, int(candidate))
    return max(5, math.ceil(value))


def chart_frame(
    *, width: int, height: int, left: int, aria_label: str, title: str,
    subtitle: str | list[str], css: str,
) -> list[str]:
    """Start one chart with the site's shared card, typography, and title placement."""
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{html.escape(aria_label)}">',
        f'<style>{base_css(width)}{css}</style>',
        card_rect(width, height),
        f'<text x="{left}" y="44" class="title">{html.escape(title)}</text>',
    ]
    lines = [subtitle] if isinstance(subtitle, str) else subtitle
    for line, y in zip(lines, subtitle_baselines(width, len(lines))):
        parts.append(f'<text x="{left}" y="{y:g}" class="subtitle">{html.escape(line)}</text>')
    return parts


def subtitle_baselines(width: int, count: int) -> list[float]:
    """Where chart_frame puts `count` subtitle lines on a card `width` wide: the design positions,
    72 and then 22 apart, moved down where HEADER_LEADING needs more room for the scaled type."""
    pitch = HEADER_LEADING * SUBTITLE_SIZE * width / REFERENCE_WIDTH
    baselines, y = [], 44.0
    for index in range(count):
        y += max(28 if index == 0 else 22, pitch)
        baselines.append(round(y, 1))
    return baselines


def draw_histogram(
    parts: list[str], counts: list[int], x: int, y: int, width: int, height: int,
    color: str, heading: str, total: int,
) -> None:
    left, right, top, bottom = x + 42, x + width - 10, y + 55, y + height - 48
    axis_max = nice_axis_max(max(counts, default=0))
    parts.append(f'<text x="{x}" y="{y+20}" class="panel">{html.escape(heading)}</text>')
    parts.append(f'<text x="{x+width-10}" y="{y+20}" text-anchor="end" class="paneltotal">n={total:,}</text>')
    for index in range(6):
        fraction = index / 5
        gy = bottom - fraction * (bottom - top)
        value = round(fraction * axis_max)
        parts.append(f'<line x1="{left}" y1="{gy:.1f}" x2="{right}" y2="{gy:.1f}" class="grid"/>')
        parts.append(f'<text x="{left-8}" y="{gy+4:.1f}" text-anchor="end" class="tick">{value}</text>')
    slot = (right - left) / len(counts)
    bar_width = slot * .72
    for index, (label, count) in enumerate(zip(HOUR_LABELS, counts)):
        bar_height = count / axis_max * (bottom - top)
        bx = left + index * slot + (slot - bar_width) / 2
        by = bottom - bar_height
        parts.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" rx="4" fill="{color}"/>')
        if count:
            parts.append(f'<text x="{bx+bar_width/2:.1f}" y="{max(top-10, by-8):.1f}" text-anchor="middle" class="value">{count}</text>')
        label_x = bx + bar_width / 2 + 4
        label_y = bottom + 26
        parts.append(
            f'<text x="{label_x:.1f}" y="{label_y}" text-anchor="end" '
            f'transform="rotate(-32 {label_x:.1f} {label_y})" class="tick bin">'
            f'{html.escape(label)}</text>'
        )


def render_queue_age(path: Path, metrics: dict, snapshot: datetime) -> None:
    width, height = 1710, 620
    fallback_note = ""
    if metrics["missing_transition_fallbacks"]:
        fallback_note = (
            f' · {metrics["missing_transition_fallbacks"]:,} state clocks use PR creation '
            "because no matching transition was available"
        )
    subtitle = [
        f'Snapshot {snapshot:%Y-%m-%d %H:%M} UTC · current-state clocks begin at the '
        'label transition',
        f'{metrics["other_open_prs"]:,} PRs outside author/review states appear only in '
        f'total time open{fallback_note}',
    ]
    parts = chart_frame(
        width=width, height=height, left=45,
        aria_label="Open PR age and current review-state age",
        title="Open PR age and current review state", subtitle=subtitle,
        css=(
            f'.panel{{font-size:{css_px(width, 15)};font-weight:600}}'
            f'.paneltotal{{font-size:{css_px(width, 13)};font-weight:650;fill:{MUTED}}}'
            f'.value{{font-size:{css_px(width, 12)};font-weight:700}}'
            f'.bin{{font-size:{css_px(width, 10.5)}}}'
        ),
    )
    panels = [
        (metrics["total_open_hours"], PALETTE[0], "Total time open"),
        (metrics["awaiting_author_hours"], PALETTE[1], "Awaiting author"),
        (metrics["in_review_hours"], PALETTE[2], "In review"),
    ]
    for index, (values, color, title) in enumerate(panels):
        draw_histogram(
            parts, histogram(values), 45 + index * 555, 115, 515, 455,
            color, title, len(values),
        )
    parts.append("</svg>")
    atomic_write(path, "".join(parts) + "\n")


def render_review_cycles(path: Path, metrics: dict, max_rows: int) -> None:
    reach = metrics["reach"]
    shown = reach[:max_rows]
    truncated = len(reach) > max_rows
    if truncated:
        shown[-1] = dict(shown[-1], label=f"Review cycle {max_rows}+")
    reviewed = metrics["reviewed_prs"]
    epoch = metrics.get("label_epoch")
    epoch_note = f" · observed since {epoch}" if epoch else ""
    width = 1250
    height = 116 + max(1, len(shown)) * 52 + 35
    bar_x, bar_width = 235, 780
    parts = chart_frame(
        width=width, height=height, left=45,
        aria_label="Pull requests reaching each automated review cycle",
        title="PRs reaching each review cycle",
        subtitle=f'One cycle = one entry into review from an author or CI state · '
        f'{metrics["total_cycles"]:,} cycles across {reviewed:,} reviewed PRs{epoch_note}',
        css=f'.label{{font-size:{css_px(width, 14)};font-weight:600}}'
        f'.value{{font-size:{css_px(width, 13)};font-weight:700}}'
        f'.track{{fill:{BAR_BG}}}',
    )
    if not shown:
        parts.append('<text x="45" y="125" class="label">No review cycles observed.</text>')
    for index, item in enumerate(shown):
        cycle, count = item["cycle"], item["prs"]
        y = 98 + index * 52
        fraction = count / reviewed if reviewed else 0
        label = item.get("label", f"Review cycle {cycle}")
        color = PALETTE[min(index, len(PALETTE) - 1)]
        parts.append(f'<text x="45" y="{y+25}" class="label">{html.escape(label)}</text>')
        parts.append(f'<rect x="{bar_x}" y="{y}" width="{bar_width}" height="36" rx="7" class="track"/>')
        parts.append(f'<rect x="{bar_x}" y="{y}" width="{max(2, bar_width*fraction):.1f}" height="36" rx="7" fill="{color}"/>')
        parts.append(f'<text x="{bar_x+bar_width+22}" y="{y+25}" class="value">{count:,} · {fraction:.1%}</text>')
    parts.append("</svg>")
    atomic_write(path, "".join(parts) + "\n")


def render_rolling(path: Path, rolling: list[dict]) -> None:
    width, height = 1500, 830
    panels = [
        ("Merges in trailing 7 days", "merges", PALETTE[2], lambda x: f"{int(x)}"),
        ("Active PR authors in trailing 7 days", "active_authors", PALETTE[1], lambda x: f"{int(x)}"),
        ("Median time to merge", "median_merge_hours", PALETTE[0], lambda x: f"{x:.1f}h"),
        ("90th percentile time to merge", "p90_merge_hours", PALETTE[4], lambda x: f"{x:.1f}h"),
    ]
    panel_width, panel_height = 680, 285
    origins = [(65, 125), (785, 125), (65, 475), (785, 475)]
    parts = chart_frame(
        width=width, height=height, left=55,
        aria_label="Trailing-seven-day pull request health",
        title="Trailing-seven-day project health",
        subtitle=f'Daily UTC windows ending {rolling[0]["date"]}–{rolling[-1]["date"]} · '
        'complete days only',
        css=f'.panel{{font-size:{css_px(width, 15)};font-weight:600}}'
        f'.latest{{font-size:{css_px(width, 24)};font-weight:700}}',
    )
    plots = []
    for (title, key, color, formatter), (ox, oy) in zip(panels, origins):
        values = [row[key] or 0 for row in rolling]
        axis_max = nice_axis_max(max(values, default=0))
        left, right = ox + 44, ox + panel_width - 15
        top, bottom = oy + 68, oy + panel_height - 26
        plots.append({"bounds": [left, top, right - left, bottom - top],
                      "x": [left + i * (right - left) / max(len(rolling) - 1, 1) for i in range(len(rolling))],
                      "labels": [row["date"] for row in rolling],
                      "series": [{"label": title, "unit": "hours" if key.endswith("hours") else "",
                                  "digits": 1 if key.endswith("hours") else 0,
                                  "values": [row[key] for row in rolling]}]})
        parts.append(f'<text x="{ox}" y="{oy+20}" class="panel">{html.escape(title)}</text>')
        parts.append(f'<text x="{ox+panel_width-20}" y="{oy+25}" text-anchor="end" class="latest" style="fill:{color}">{formatter(values[-1])}</text>')
        for index in range(5):
            fraction = index / 4
            y = bottom - fraction * (bottom - top)
            value = fraction * axis_max
            parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" class="grid"/>')
            parts.append(f'<text x="{left-8}" y="{y+4:.1f}" text-anchor="end" class="tick">{formatter(value)}</text>')
        points = []
        for index, value in enumerate(values):
            x = left + index * (right - left) / max(len(values) - 1, 1)
            y = bottom - value / axis_max * (bottom - top)
            points.append(f"{x:.1f},{y:.1f}")
        parts.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="3.5" stroke-linejoin="round" stroke-linecap="round"/>')
        last_tick_x = -math.inf
        for index, row in enumerate(rolling):
            x = left + index * (right - left) / max(len(values) - 1, 1)
            if index not in (0, len(rolling) - 1) and x - last_tick_x < 100:
                continue
            if index == len(rolling) - 1 and x - last_tick_x < 70:
                continue
            day = date.fromisoformat(row["date"])
            parts.append(f'<text x="{x:.1f}" y="{bottom+18}" text-anchor="middle" class="tick">{day:%b} {day.day}</text>')
            last_tick_x = x
    parts.append(hover_metadata(plots))
    parts.append("</svg>")
    atomic_write(path, "".join(parts) + "\n")


def render_merge_latency(path: Path, latency: dict | None, window: str = "24h",
                         measure: str = "open_to_land") -> None:
    """The health page's default time-to-merge view, for readers without JavaScript."""
    width, height = 1100, 460
    title = "Time from opening to merge, trailing 24 hours"
    rows = []
    if latency:
        values = latency["series"][window][measure]
        rows = [(d, values["p25"][i], values["median"][i], values["p75"][i])
                for i, d in enumerate(latency["dates"]) if values["median"][i] is not None]
    parts = chart_frame(
        width=width, height=height, left=55, aria_label=title, title=title,
        subtitle=(f"Pull requests the merge queue landed each UTC day, {rows[0][0]} to {rows[-1][0]}: "
                  "median solid, 25th to 75th percentile shaded, in hours" if rows
                  else "No queue landing data yet"),
        css="",
    )
    left, right, top, bottom = 90, width - 30, 110, height - 50
    if rows:
        axis_max = nice_axis_max(max(r[3] for r in rows))
        x = lambda i: left + i * (right - left) / max(len(rows) - 1, 1)  # noqa: E731
        y = lambda v: bottom - v / axis_max * (bottom - top)  # noqa: E731
        for index in range(5):
            value = index / 4 * axis_max
            parts.append(f'<line x1="{left}" y1="{y(value):.1f}" x2="{right}" y2="{y(value):.1f}" class="grid"/>')
            parts.append(f'<text x="{left-8}" y="{y(value)+4:.1f}" text-anchor="end" class="tick">{value:.0f}h</text>')
        band = ([f"{x(i):.1f},{y(r[3]):.1f}" for i, r in enumerate(rows)]
                + [f"{x(i):.1f},{y(r[1]):.1f}" for i, r in reversed(list(enumerate(rows)))])
        parts.append(f'<polygon points="{" ".join(band)}" fill="{PALETTE[0]}" fill-opacity="0.18"/>')
        line = " ".join(f"{x(i):.1f},{y(r[2]):.1f}" for i, r in enumerate(rows))
        parts.append(f'<polyline points="{line}" fill="none" stroke="{PALETTE[0]}" stroke-width="3.5" '
                     'stroke-linejoin="round" stroke-linecap="round"/>')
        last_tick_x = -math.inf
        for i, r in enumerate(rows):
            if (i not in (0, len(rows) - 1) and x(i) - last_tick_x < 100) or (
                    i == len(rows) - 1 and x(i) - last_tick_x < 70):
                continue
            day = date.fromisoformat(r[0])
            parts.append(f'<text x="{x(i):.1f}" y="{bottom+20}" text-anchor="middle" class="tick">{day:%b} {day.day}</text>')
            last_tick_x = x(i)
        parts.append(f'<text x="{right}" y="80" text-anchor="end" class="tick">latest median {rows[-1][2]:.1f}h</text>')
    parts.append("</svg>")
    atomic_write(path, "".join(parts) + "\n")


def color_for(name: str, other: bool = False) -> str:
    if other:
        return MUTED
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return PALETTE[int.from_bytes(digest[:2], "big") % len(PALETTE)]


def spread_labels(items: list[tuple[str, float]], top: float, bottom: float, gap: int = 22) -> dict[str, float]:
    positioned = []
    for name, y in sorted(items, key=lambda item: item[1]):
        positioned.append([name, max(y, positioned[-1][1] + gap if positioned else top)])
    if positioned and positioned[-1][1] > bottom:
        positioned[-1][1] = bottom
        for index in range(len(positioned) - 2, -1, -1):
            positioned[index][1] = min(positioned[index][1], positioned[index + 1][1] - gap)
    return dict(positioned)


def log_ticks(maximum: int) -> list[int]:
    ticks = [0]
    power = 0
    while 10 ** power <= maximum:
        for multiplier in (1, 3):
            value = multiplier * 10 ** power
            if value <= maximum:
                ticks.append(value)
        power += 1
    if maximum not in ticks and maximum > ticks[-1] * 1.25:
        ticks.append(maximum)
    return sorted(set(ticks))


def heat_buckets(maximum: int) -> list[int]:
    """Lower bound of each ramp step, for a count distribution with a long tail.

    Geometric rather than equal-width: one person with forty merges in an area and everybody
    else with one or two is the normal shape here, and equal-width buckets would paint all of
    those the same colour. Returned lower-inclusive and strictly increasing, so a legend can
    print them and short ramps (a maximum of one or two) collapse rather than repeat.
    """
    if maximum <= 1:
        return [1]
    edges = []
    for index in range(len(ROADMAP_RAMP)):
        # Divided by the number of steps, not by one fewer: the top edge has to land BELOW the
        # maximum so the brightest bucket covers a range. Spacing the edges across the closed
        # interval instead puts the last one exactly on the maximum, which spends a whole ramp
        # step on the single busiest cell.
        edge = int(round(maximum ** (index / len(ROADMAP_RAMP))))
        if not edges or edge > edges[-1]:
            edges.append(max(edge, 1))
    return edges


def heat_step(value: int, edges: list[int]) -> int:
    step = 0
    for index, edge in enumerate(edges):
        if value >= edge:
            step = index
    return step


def render_roadmap_heatmap(
    path: Path, title: str, noun: str, matrix: dict, slice_key: str,
) -> None:
    """One contributor-by-roadmap grid: rows are people, columns are roadmaps, cells are counts.

    Deliberately a grid rather than fifteen small multiples. The questions it answers -- who
    knows this area, which roadmap rests on one person, where is somebody spending their
    effort -- are all comparisons across both axes at once, and small multiples make you scan
    between cards to do any of them. The cumulative charts already cover change over time.

    Every non-zero cell carries its own number. These are published as static <img>, so there
    is no hover to fall back on, and the printed value is what makes a cell readable when its
    colour sits mid-ramp. Zero is drawn as nothing at all: absence should be quiet, and on a
    sparse grid an empty cell is easier to skip than a dark one.
    """
    data = matrix[slice_key]
    axis, rows = data["axis"], data["rows"]
    counts = data["counts"]

    def count_of(who: str, area: str) -> int:
        return counts.get(f"{who}\t{area}", 0)

    def clip(text: str, limit: int) -> str:
        # The column headings are rotated 45 degrees, so their length is vertical as well as
        # horizontal: a long enough label climbs out of the header band and over the subtitle.
        # Today's longest roadmap is about fourteen characters and the band holds a little over
        # twenty, but nothing stops somebody naming one `roadmap/AlgebraicNumberTheory`.
        #
        # Elided in the MIDDLE rather than the tail, so two roadmaps sharing a long prefix stay
        # distinguishable -- `AlgebraicNumberTheory` and `AlgebraicTopologySeminar` differ only
        # after nine characters, and a prefix clip would render them identically.
        text = XML_FORBIDDEN.sub("", text)
        if len(text) <= limit:
            return text
        head = (limit - 1) // 2
        return text[:head] + "…" + text[len(text) - (limit - 1 - head):]

    def column_label(area: str) -> str:
        if area == OTHER_ROADMAP:
            return f"Other ({len(matrix['bundled'])})"
        if area == UNSORTED_CATEGORY:
            return "Unsorted"
        if area in ARXIV_MATH:
            # A category by its name, which readers know better than its code; the longest arXiv
            # mathematics name is 27 characters, so none is elided.
            return clip(ARXIV_MATH[area], 28)
        return clip(area[len(ROADMAP_PREFIX):] if area.startswith(ROADMAP_PREFIX) else area, 20)

    def row_label(who: str) -> str:
        return (f"Other ({data['omitted_contributors']:,})"
                if who == OTHER_CONTRIBUTOR else clip(who, 24))

    # Each subtitle line is one unwrapped <text>, so its length is bounded by the card: these fit
    # with room to spare at every grid width (the font scales with the width, so the length in
    # characters is what matters). Which roadmap a PR's category comes from is the page's to say.
    by_category = matrix.get("grouping") == "category"
    per = "arXiv category" if by_category else "roadmap"
    columns_are = "arXiv categories" if by_category else "roadmaps"
    every = "category" if by_category else "roadmap"

    left = 230
    cell_w, cell_h, gap = 74, 26, 2
    # The right reserve has to hold the LAST heading, which is rotated 45 degrees and so runs
    # up and to the right past its own column. Forty units did not, so a clipped label could
    # still cross the viewBox edge.
    right = 150
    # Floored at the reference width because the page renders these at `width: 100%`: a grid
    # with one or two columns would otherwise be scaled up into an enormous near-space card
    # of mostly whitespace.
    width = max(REFERENCE_HEATMAP_WIDTH, left + len(axis) * cell_w + right)
    # A long heading needs more than that reserve: the last one starts at its column's centre and
    # runs right by its length over root two. Category names reach 27 characters, against twenty
    # for the clipped roadmap names the reserve was sized for. The font scales with the width,
    # which grows with the reserve, so settle the two together (each round adds less: a heading
    # spans well under the width).
    longest_heading = max((len(column_label(area)) for area in axis), default=0)
    for _ in range(6):
        reach_right = longest_heading * (12 * width / REFERENCE_WIDTH) * HEADING_ASPECT / 1.414
        needed = int(math.ceil(reach_right - cell_w / 2)) + 16
        if needed <= right:
            break
        right = needed
        width = max(REFERENCE_HEATMAP_WIDTH, left + len(axis) * cell_w + right)
    # The header band is sized from the longest heading rather than fixed, because a heading
    # rotated 45 degrees reaches upward by its own length over root two -- and css_px scales a
    # design-space 12 to about 18 user units at this width, so `RepresentationTheory` reaches
    # 143 units. A fixed 232-unit band put it through the subtitle, which is what the first
    # render against real roadmap names showed; the synthetic fixtures all had shorter names.
    # The headings clear the second subtitle line, wherever chart_frame puts it at this width.
    heading_font = 12 * width / REFERENCE_WIDTH
    longest = max((len(column_label(area)) for area in axis), default=0)
    reach = longest * heading_font * HEADING_ASPECT / 1.414
    top = max(232, int(subtitle_baselines(width, 2)[-1] + 26 + reach))
    height = top + len(rows) * cell_h + 72
    maximum = max(counts.values(), default=0)
    edges = heat_buckets(maximum)

    parts = chart_frame(
        width=width, height=height, left=55, aria_label=title, title=title,
        subtitle=[
            # Not `noun.capitalize()`, which lowercases the rest and turns "merged PRs" into
            # "Merged prs".
            f"Count of {noun} per contributor per {per}, "
            f"{matrix['from']}–{matrix['to']} ({matrix['window_days']} days)",
            f"columns are the {len(matrix['columns'])} {columns_are} with the most merges in "
            f"that window; exact counts for every contributor and {every} in JSON",
        ],
        css=f'.rowlab{{font-size:{css_px(width, 12.5)};text-anchor:end}}'
            f'.collab{{font-size:{css_px(width, 12)}}}'
            f'.cellval{{font-size:{css_px(width, 11.5)};text-anchor:middle;'
            'font-variant-numeric:tabular-nums}'
            f'.scale{{font-size:{css_px(width, 11.5)};fill:{MUTED}}}',
    )

    # Column headings, rotated so a long roadmap name does not force the columns apart.
    for index, area in enumerate(axis):
        x = left + index * cell_w + cell_w / 2
        parts.append(
            f'<text x="{x:.1f}" y="{top - 10}" class="collab" '
            f'fill="{MUTED if area == OTHER_ROADMAP else TEXT}" '
            f'transform="rotate(-45 {x:.1f} {top - 10})">'
            f'{html.escape(column_label(area))}</text>'
        )

    for row_index, who in enumerate(rows):
        y = top + row_index * cell_h
        parts.append(
            f'<text x="{left - 14}" y="{y + cell_h / 2 + 4:.1f}" class="rowlab" '
            f'fill="{MUTED if who == OTHER_CONTRIBUTOR else TEXT}">'
            f'{html.escape(row_label(who))}</text>'
        )
        for col_index, area in enumerate(axis):
            value = count_of(who, area)
            if not value:
                continue
            step = heat_step(value, edges)
            x = left + col_index * cell_w
            parts.append(
                f'<rect x="{x + gap / 2:.1f}" y="{y + gap / 2:.1f}" '
                f'width="{cell_w - gap}" height="{cell_h - gap}" rx="3" '
                f'fill="{ROADMAP_RAMP[step]}"/>'
            )
            parts.append(
                f'<text x="{x + cell_w / 2:.1f}" y="{y + cell_h / 2 + 4:.1f}" '
                f'class="cellval" fill="{ROADMAP_INK[step]}">{value:,}</text>'
            )

    # The scale, so the colour is readable as magnitude rather than decoration.
    legend_y = top + len(rows) * cell_h + 28
    if not maximum:
        # Nothing was drawn, so a colour scale would describe an encoding the reader cannot see
        # anywhere -- and `heat_buckets(0)` yields a lone "1" entry, which reads as "somebody
        # has one" rather than "nobody has any". Say what is actually true instead.
        parts.append(f'<text x="{left - 14}" y="{legend_y + 12}" class="rowlab" '
                     f'fill="{MUTED}">no roadmap-attributed {html.escape(noun)} '
                     'in this window</text>')
        parts.append("</svg>")
        atomic_write(path, "".join(parts) + "\n")
        return
    parts.append(f'<text x="{left - 14}" y="{legend_y + 12}" class="rowlab" '
                 f'fill="{MUTED}">{html.escape(noun)}</text>')
    for index, edge in enumerate(edges):
        x = left + index * 92
        upper = edges[index + 1] - 1 if index + 1 < len(edges) else maximum
        label = f"{edge:,}" if upper <= edge else f"{edge:,}–{upper:,}"
        parts.append(f'<rect x="{x}" y="{legend_y}" width="20" height="14" rx="3" '
                     f'fill="{ROADMAP_RAMP[index]}"/>')
        parts.append(f'<text x="{x + 26}" y="{legend_y + 12}" class="scale">{label}</text>')

    parts.append("</svg>")
    atomic_write(path, "".join(parts) + "\n")


def render_cumulative_contributors(
    path: Path, title: str, noun: str, dates: list[str], names: list[str],
    series: dict[str, list[int]], totals: Counter, total_contributors: int,
) -> None:
    width, height = 1500, 760
    left, right, top, bottom = 90, 1030, 120, 680
    label_x = 1070
    plotted_totals = {name: values[-1] for name, values in series.items()}
    maximum = max(plotted_totals.values(), default=1)
    logmax = math.log10(maximum + 1)

    def y_for(value):
        return bottom - math.log10(value + 1) / logmax * (bottom - top)

    labels = spread_labels([(name, y_for(plotted_totals[name])) for name in names], top, bottom)
    omitted = total_contributors - min(total_contributors, len([name for name in names if not name.startswith("Other (")]))
    coverage = f"top {len(names) - bool(omitted)} + {omitted:,} others" if omitted else "every contributor"
    parts = chart_frame(
        width=width, height=height, left=55, aria_label=title, title=title,
        subtitle=f'Cumulative {noun} by UTC day, {dates[0]}–{dates[-1]} · logarithmic '
        f'count axis · {coverage}; exact totals for all {total_contributors:,} in JSON',
        css=f'.label{{font-size:{css_px(width, 12)};font-weight:600}}'
        '.leader{stroke-width:1;opacity:.55}',
    )
    for tick in log_ticks(maximum):
        y = y_for(tick)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end" class="tick">{tick:,}</text>')
    for name in reversed(names):
        color = color_for(name, name.startswith("Other ("))
        values = series[name]
        points = []
        for index, value in enumerate(values):
            x = left + index * (right - left) / max(len(values) - 1, 1)
            points.append(f"{x:.1f},{y_for(value):.1f}")
        parts.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="2.7" stroke-linejoin="round" stroke-linecap="round"/>')
    for name in names:
        color = color_for(name, name.startswith("Other ("))
        actual_y = y_for(plotted_totals[name])
        label_y = labels[name]
        parts.append(f'<line x1="{right+4}" y1="{actual_y:.1f}" x2="{label_x-8}" y2="{label_y:.1f}" stroke="{color}" class="leader"/>')
        parts.append(f'<circle cx="{right}" cy="{actual_y:.1f}" r="3.2" fill="{color}"/>')
        parts.append(f'<text x="{label_x}" y="{label_y+4:.1f}" class="label" style="fill:{color}">{html.escape(name)} · {plotted_totals[name]:,}</text>')
    last_tick_x = -math.inf
    for index, value in enumerate(dates):
        x = left + index * (right - left) / max(len(dates) - 1, 1)
        if index not in (0, len(dates) - 1) and x - last_tick_x < 95:
            continue
        if index == len(dates) - 1 and x - last_tick_x < 65:
            continue
        day = date.fromisoformat(value)
        parts.append(f'<text x="{x:.1f}" y="{bottom+24}" text-anchor="middle" class="tick">{day:%b} {day.day}</text>')
        last_tick_x = x
    parts.append(hover_metadata([{
        "bounds": [left, top, right - left, bottom - top],
        "x": [left + i * (right - left) / max(len(dates) - 1, 1) for i in range(len(dates))],
        "labels": dates,
        "series": [{"label": name, "unit": noun, "values": series[name]} for name in names],
    }]))
    parts.append("</svg>")
    atomic_write(path, "".join(parts) + "\n")


def generate(
    data: dict, out_dir: Path, contributor_limit: int = 24,
    history_days: int = 90, max_review_cycles: int = 12,
    as_of: datetime | None = None, roadmap_dir: Path | None = None,
) -> dict:
    prs = data.get("prs") or []
    if not prs:
        raise ValueError("snapshot contains no pull requests")
    snapshot = as_of or parse_dt(data["fetched_at"])
    if snapshot.tzinfo is None:
        snapshot = snapshot.replace(tzinfo=timezone.utc)
    snapshot = snapshot.astimezone(timezone.utc)
    last_full_day = snapshot.date() - timedelta(days=1)
    project_start = min(parse_dt(pr["created_at"]).date() for pr in prs)
    if last_full_day < project_start:
        raise ValueError("snapshot predates the first pull request")

    queue = queue_age_metrics(prs, snapshot)
    state_clock_count = (
        len(queue["awaiting_author_hours"]) + len(queue["in_review_hours"])
    )
    allowed_fallbacks = max(2, 0.05 * state_clock_count)
    if queue["missing_transition_fallbacks"] > allowed_fallbacks:
        raise ValueError(
            "too many open PR state clocks lack matching label transitions: "
            f"{queue['missing_transition_fallbacks']} of {state_clock_count}"
        )
    cycles = review_cycle_metrics(prs)
    # Forgejo's pull requests join only the seven-day series; see FORGEJO_REPOS.
    forgejo = data.get("forgejo") or {}
    rolling = rolling_metrics(prs + (forgejo.get("prs") or []), last_full_day, history_days)
    latency = merge_latency_metrics(prs, forgejo.get("prs") or [], data.get("queue_landings"),
                                    last_full_day, snapshot, history_days)
    # And the age of its open pull requests, kept apart from `total_open_hours`: they carry no
    # lifecycle labels, so the per-state clocks and the charts drawn from them stay GitHub's.
    queue["forgejo_open_hours"] = [
        max(0.0, (snapshot - parse_dt(pr["created_at"])).total_seconds() / 3600)
        for pr in forgejo.get("prs") or [] if pr["state"] == "OPEN"
    ]
    merge_events = [
        (parse_dt(pr["merged_at"]), pr["author"])
        for pr in prs if pr.get("merged_at")
    ]
    review_events = [
        (parse_dt(item["created_at"]), item["user"])
        for item in data.get("scoreboards") or []
    ]
    # `last_full_day`, not `snapshot.date()`: the snapshot is taken every three hours, so ending
    # the series on the current day plotted whatever had merged by then as though it were a whole
    # day. On a cumulative curve that is not visibly "incomplete" -- it is a flat final segment
    # and an end-of-line total that is simply short, both of which redraw steeper on the next run.
    # The rolling chart has always ended here for the same reason; these two now agree with it.
    merge_dates, merge_names, merge_series, merge_totals = cumulative_chart_series(
        merge_events, project_start, last_full_day, contributor_limit, snapshot,
    )
    review_dates, review_names, review_series, review_totals = cumulative_chart_series(
        review_events, project_start, last_full_day, contributor_limit, snapshot,
    )

    # The who-works-where grids: by arXiv category when there is a TauCetiRoadmap checkout to read
    # the categories from, else by roadmap.
    categories = roadmap_categories(roadmap_dir) if roadmap_dir is not None else None
    roadmaps = roadmap_matrix(
        prs, data.get("scoreboards") or [], last_full_day, column_of=categories,
        **({"roadmap_limit": CATEGORY_LIMIT} if categories is not None else {}),
    )
    if categories is not None:
        roadmaps["roadmap_categories"] = dict(sorted(categories.items()))
    grid_by = "arXiv category" if categories is not None else "roadmap"

    metrics = {
        "schema_version": 1,
        "repo": data.get("repo"),
        "forgejo_repo": forgejo.get("repo"),
        "snapshot": iso_z(snapshot),
        "last_full_day": last_full_day.isoformat(),
        "definitions": {
            "review_cycle": cycles["definition"],
            "review": "one canonical <!--tauceti-scoreboard--> comment on a pull request of this repository, identified by the tauceti-meta:v1 block naming that PR, whose posting login also authors a merged PR in the fetched snapshot",
            "active_author": "distinct PR author login opening a PR in the trailing seven-day window, on GitHub or on forgejo_repo",
            "merge": "GitHub mergedAt; for a closed PR, the installed bors or TauCeti queue app renaming it to a [Merged by Bors] title, or else that app's success receipt on it; on forgejo_repo, that app's success receipt posted by one of its merge logins, else Forgejo merged_at",
            "merge_scope": "rolling_seven_day counts pull requests on GitHub and on forgejo_repo, an author being the same contributor on both under the same login, and queue.forgejo_open_hours gives the ages of forgejo_repo's open pull requests; every other figure counts GitHub's alone",
            "merge_latency": "PR creation timestamp to merge timestamp",
            "merge_latency_by_measure": "merge_latency.series and .current split time to merge at the TauCeti queue's first approval (its first admission of a head after an all-green review), over pull requests it landed on both forges, in 24-hour and seven-day windows; see merge_latency.definition",
        },
        "queue": queue,
        "review_cycles": cycles,
        "rolling_seven_day": rolling,
        "merge_latency": latency,
        "contributor_limit": contributor_limit,
        "cumulative_dates": merge_dates,
        "cumulative_merges_plotted": merge_series,
        "cumulative_reviews_plotted": review_series,
        "merge_totals_by_contributor": dict(merge_totals.most_common()),
        "review_totals_by_contributor": dict(review_totals.most_common()),
        "by_roadmap_and_contributor": roadmaps,
        "rejected_scoreboard_comments": data.get("rejected_scoreboard_comments") or {},
    }
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".pr-stats-stage-", dir=out_dir.parent,
    ) as temporary:
        staging = Path(temporary)
        render_queue_age(staging / "pr-queue-age.svg", queue, snapshot)
        render_review_cycles(
            staging / "review-cycles-reached.svg", cycles, max_review_cycles,
        )
        render_rolling(staging / "rolling-seven-day-history.svg", rolling)
        render_merge_latency(staging / "merge-latency.svg", latency)
        render_cumulative_contributors(
            staging / "cumulative-merges-by-contributor.svg",
            "Merged PRs by contributor", "merged PRs",
            merge_dates, merge_names, merge_series, merge_totals, len(merge_totals),
        )
        render_cumulative_contributors(
            staging / "cumulative-reviews-by-contributor.svg",
            "Reviews by contributor", "reviews",
            review_dates, review_names, review_series,
            review_totals, len(review_totals),
        )
        render_roadmap_heatmap(
            staging / "merges-by-roadmap-and-contributor.svg",
            f"Merged PRs by {grid_by} and contributor", "merged PRs", roadmaps, "merges",
        )
        render_roadmap_heatmap(
            staging / "reviews-by-roadmap-and-contributor.svg",
            f"Reviews by {grid_by} and contributor", "reviews", roadmaps, "reviews",
        )
        atomic_write(
            staging / "pr-stats.json",
            json.dumps(metrics, separators=(",", ":")) + "\n",
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in ASSET_NAMES:
            os.replace(staging / name, out_dir / name)
    return metrics


def forgejo_source(args) -> dict | None:
    """The FORGEJO_REPOS-shaped entry the command line selects, or None."""
    paired = FORGEJO_REPOS.get(args.repo)
    url = args.forgejo_repo if args.forgejo_repo is not None else (paired or {}).get("url")
    if not url:
        return None
    logins = args.forgejo_merge_login or (
        paired["merge_logins"] if paired and paired["url"] == url else None)
    if not logins:
        raise SystemExit(f"--forgejo-merge-login is required for {url}")
    return {"url": url, "merge_logins": logins}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="TauCetiProject/TauCeti")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--data", type=Path, help="normalized offline snapshot")
    parser.add_argument("--dump-data", type=Path, help="write fetched normalized snapshot")
    parser.add_argument("--since-data", type=Path,
                        help="an earlier snapshot; pull requests untouched since it was written "
                             "keep their recorded timeline instead of being walked again. "
                             "A missing or unreadable file is ignored, so a cold start still "
                             "works -- it is a cost optimisation, never a source of truth.")
    parser.add_argument("--forgejo-repo",
                        help="a Forgejo repository (https://HOST/OWNER/REPO) whose pull requests the "
                             "seven-day series also counts; defaults to the one FORGEJO_REPOS pairs "
                             "with --repo, and '' disables it")
    parser.add_argument("--forgejo-merge-login", action="append",
                        help="a login the queue posts its Forgejo receipts as (repeatable); "
                             "defaults to FORGEJO_REPOS's for that repository")
    parser.add_argument("--as-of", help="override snapshot time (ISO 8601)")
    parser.add_argument("--contributor-limit", type=int, default=24)
    parser.add_argument("--history-days", type=int, default=90)
    parser.add_argument("--max-review-cycles", type=int, default=12)
    parser.add_argument("--roadmap-dir", type=Path,
                        help="a TauCetiRoadmap checkout: group the who-works-where grids by the "
                             "arXiv category each roadmap declares in its metadata.toml, rather "
                             "than by roadmap")
    args = parser.parse_args()
    if args.contributor_limit < 1 or args.history_days < 1 or args.max_review_cycles < 1:
        parser.error("numeric limits must be positive")

    if args.data:
        data = json.loads(args.data.read_text(encoding="utf-8"))
    else:
        # Without --dump-data no later run can resume a capped receipt backlog or walk, so this
        # one checks every receipt candidate and walks to the end.
        resumable = bool(args.dump_data)
        data = fetch_snapshot(
            args.repo, load_previous(args.since_data, args.repo),
            receipt_checks=MERGE_RECEIPT_CHECKS_PER_RUN if resumable else None,
            scoreboard_pages=SCOREBOARD_PAGES_PER_RUN if resumable else None,
            forgejo_repo=forgejo_source(args), queue_landings=QUEUE_LANDINGS.get(args.repo))
        if args.dump_data:
            atomic_write(args.dump_data, json.dumps(data, indent=2) + "\n")
    if scoreboard_history_unfinished(data):
        print("the first full walk of scoreboard comments is unfinished, so no assets were "
              "written; the snapshot carries it on to the next run", file=sys.stderr)
        return 3
    as_of = parse_dt(args.as_of) if args.as_of else None
    metrics = generate(
        data, args.out_dir, args.contributor_limit, args.history_days,
        args.max_review_cycles, as_of, roadmap_dir=args.roadmap_dir,
    )
    print(
        f"wrote five charts to {args.out_dir}: "
        f"{len(metrics['queue']['total_open_hours'])} open PRs; "
        f"{metrics['queue']['missing_transition_fallbacks']} state-clock fallbacks; "
        f"{metrics['review_cycles']['total_cycles']} review cycles; "
        f"{len(metrics['merge_totals_by_contributor'])} merge contributors; "
        f"{len(metrics['review_totals_by_contributor'])} review contributors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
