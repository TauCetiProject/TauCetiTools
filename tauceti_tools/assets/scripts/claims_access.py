#!/usr/bin/env python3
"""Keep the shared claim namespace open to everyone who has landed a PR here, and sweep dead leases.

Workers run by different people must not spend two subscriptions on the same job, so before starting
one they take a lease: a `refs/tauceti-claims/<key>` ref, acquired by compare-and-swap, in a repository
every worker can push to. That repository is TauCetiProject/tauceti-claims, and it holds nothing else:
no code, no Actions, no relationship to this one. Coordinating workers must never need write access to
the library itself, which is the whole reason the leases do not live here.

So somebody has to be let in, and this decides who: the author of any pull request that landed in this
repository. A maintainer looked at their work and took it, which is a better statement than org
membership and is already recorded on GitHub, so there is no list to keep. Landed means merged by
GitHub, or landed by the merge queue, which pushes the commit, records the landing in a comment on the
PR (LANDED_RECORD) and closes it under LANDED_TITLE: GitHub records that PR as closed, not merged. A
worker whose operator has not landed anything yet is not stuck waiting — it claims in its own fork
until then, and so still coordinates with the rest of its own fleet.

  grant  Invite every landed-PR author who is not already a collaborator or already invited. Grants
         are monotone: nothing is revoked automatically, because a quiet operator silently losing
         coordination is a confusing thing to debug from the worker side. Remove abuse by hand.
  sweep  Delete leases that expired more than SWEEP_GRACE_MINUTES ago. Releasing a lease deletes it,
         so these are the leavings of workers that died holding one; the grace period keeps the sweep
         clear of a live takeover, which is itself a compare-and-swap.

Reconciling (rather than reacting to a merge event) is what makes this self-healing: a missed webhook,
a failed API call, or an invitation that expired unaccepted after seven days is simply picked up on the
next run.

Env: GH_TOKEN (reads PRs here), CLAIMS_TOKEN (administers the claims repo: `Administration: write` to
invite, `Contents: write` to sweep), REPO (owner/name), optional CLAIMS_REPO, LANDERS, DRY_RUN=1,
SWEEP_GRACE_MINUTES.
"""

import json
import os
import re
import subprocess
import sys
import time

REPO = os.environ.get("REPO", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
CLAIMS_TOKEN = os.environ.get("CLAIMS_TOKEN", "")
CLAIMS_REPO = os.environ.get("CLAIMS_REPO", "TauCetiProject/tauceti-claims")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
# A lease is swept only this long AFTER it expired. Taking over an expired lease is a CAS, so a
# worker may legitimately be reviving one at the moment we look at it; the grace period means the
# sweep cannot race that and delete a lease somebody is actively holding.
SWEEP_GRACE_MINUTES = int(os.environ.get("SWEEP_GRACE_MINUTES", "60"))
NS = "tauceti-claims"
# The merge queue lands a PR by pushing its commit to the default branch, then renames the PR with this
# prefix and closes it, as bors-ng did. It also closes the PRs it rejects, under their own titles, which
# their authors can then edit, so the title only picks out the PRs worth checking for LANDED_RECORD.
LANDED_TITLE = "[Merged by Bors] - "
# How the comment the queue posts on a PR it landed ends: a marker naming the PR (`owner/name#number`), the
# batch and the commit that landed it, then that marker again inside the wrapper the queue's comment adapter
# appends to everything it posts (TauCetiWorker `tauceti_queue`: `_enqueue_landing_effects` in
# controller.py, `post_comment` in github_adapter.py). Only that ending counts. The queue quotes what people
# type in its replies to commands, so a marker can appear anywhere in its comments, but a reply ends with
# its own wrapper (`reply …`), and a PR it rejected with `rejected:`.
LANDED_RECORD = re.compile(
    r"(?P<record><!-- tauceti-queue landed:(?P<pr>[^\s:]+#\d+):\d+:(?P<sha>[0-9a-f]{40}) -->)"
    r"\r?\n\r?\n<!-- tauceti-queue (?P=record) -->\s*\Z"
)
# The accounts whose comments can carry that record. Anyone can paste it into a comment of their own; only
# the queue's App posts as its bot.
LANDERS = {name.strip().lower() for name in os.environ.get("LANDERS", "tauceti-queue[bot]").split(",") if name.strip()}
# How many closed PRs `grant` reads, most recently updated first. Merging or landing a PR updates it, so
# these hold every PR that landed since the oldest of their updates: four days back in November 2026, for
# a job scheduled hourly.
WINDOW = 5000
CLOSED_PRS = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: [CLOSED, MERGED], first: 100, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes { number title merged author { __typename login } }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def gh(args, token, check=True):
    """Run `gh` with an explicit token: this script holds two, and they are not interchangeable."""
    p = subprocess.run(["gh", *args], capture_output=True, text=True, env={**os.environ, "GH_TOKEN": token})
    if check and p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
    return p


def gh_lines(args, token):
    """Lines from a `--paginate`d `gh api`. The filter must emit one record per LINE, not an array:
    with --paginate, gh runs the jq filter per page and concatenates, so an array filter yields
    `[...][...]` across pages, which is not JSON and would silently lose everyone past the first."""
    return [line.strip() for line in gh(args, token).stdout.splitlines() if line.strip()]


def human_authors(prs):
    """The distinct human authors among these PRs. Bots are excluded: the review and bump machinery
    opens PRs of its own, and no automation coordinates through the claim namespace. A deleted
    account arrives with no login and is skipped."""
    authors = set()
    for pr in prs or []:
        author = pr.get("author") or {}
        login = (author.get("login") or "").strip()
        if login and not author.get("is_bot") and not login.endswith("[bot]"):
            authors.add(login)
    return authors


def is_sweepable(lease_json, cutoff):
    """Is this lease expired long enough to delete? A lease we cannot read or parse is left alone:
    an unfamiliar ref in this namespace is not ours to delete."""
    try:
        expires = (json.loads(lease_json) or {}).get("expires_at")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return False
    if not isinstance(expires, (int, float)) or isinstance(expires, bool):
        return False
    return 0 < expires <= cutoff


def closed_prs():
    """The WINDOW closed and merged PRs updated most recently, each as {number, title, merged, author}, the
    author in the shape `human_authors` reads. Not the most recently opened, which is what `gh pr list`
    lists: a PR can land weeks after it was opened, behind thousands of newer PRs. A PR updated while this
    pages can be listed twice, and is kept once, or be passed over, and is read on the next run."""
    owner, _, name = REPO.partition("/")
    prs, seen, cursor = [], set(), None
    while len(prs) < WINDOW:
        args = ["api", "graphql", "-f", f"query={CLOSED_PRS}", "-f", f"owner={owner}", "-f", f"name={name}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        page = json.loads(gh(args, GH_TOKEN).stdout)["data"]["repository"]["pullRequests"]
        for node in page["nodes"] or []:
            if not node or node["number"] in seen:
                continue
            seen.add(node["number"])
            author = node.get("author")  # None for a deleted account
            prs.append(
                {
                    "number": node["number"],
                    "title": node.get("title") or "",
                    "merged": bool(node.get("merged")),
                    "author": author and {"login": author.get("login"), "is_bot": author.get("__typename") == "Bot"},
                }
            )
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return prs[:WINDOW]


def merged_pr_authors(prs):
    return human_authors(pr for pr in prs if pr["merged"])


def queue_landed_prs(prs):
    """{author: [PR numbers, most recently updated first]} for those of these PRs titled as the merge queue
    titles a PR it landed. Whether the queue really landed one is checked only for an author not yet
    admitted (`landed_by_queue`), because that costs requests per PR."""
    landed = {}
    for pr in prs:
        if pr["title"].startswith(LANDED_TITLE):
            for login in human_authors([pr]):
                landed.setdefault(login, []).append(pr["number"])
    return landed


def landing_record(number):
    """The commit named by this PR's landing record, or None. The record is a comment by one of LANDERS
    that ends with LANDED_RECORD naming this repository and PR. Neither who closed the PR nor its title
    will do: the queue closes the PRs it rejects too, and a title is the author's to edit. An unreadable
    answer is None: a missed grant is picked up on a later run, a wrong one is not undone."""
    p = gh(
        [
            "api",
            "--paginate",
            f"/repos/{REPO}/issues/{number}/comments?per_page=100",
            "--jq",
            ".[] | [.user.login, .body] | @json",
        ],
        GH_TOKEN,
        check=False,
    )
    if p.returncode != 0:
        return None
    this_pr = f"{REPO}#{number}".lower()
    for line in p.stdout.splitlines():
        try:
            login, body = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if str(login or "").lower() not in LANDERS:
            continue
        record = LANDED_RECORD.search(str(body or ""))
        if record and record["pr"].lower() == this_pr:
            return record["sha"]
    return None


def default_branch():
    """The repository's default branch, or "" if GitHub does not say, which confirms no queue landing."""
    p = gh(["api", f"/repos/{REPO}", "--jq", ".default_branch"], GH_TOKEN, check=False)
    return p.stdout.strip() if p.returncode == 0 else ""


def landed_by_queue(number, branch):
    """Did the merge queue land this PR? It did if the PR has a landing record and the commit the record
    names is on `branch`, the default branch: `compare/<branch>...<commit>` is `behind` or `identical`
    exactly when the branch contains the commit. An unreadable answer is no, as in `landing_record`."""
    sha = landing_record(number)
    if not sha or not branch:
        return False
    p = gh(["api", f"/repos/{REPO}/compare/{branch}...{sha}", "--jq", ".status"], GH_TOKEN, check=False)
    return p.returncode == 0 and p.stdout.strip() in ("behind", "identical")


def invitations():
    """{login: (invitation id, expired)} for the claims repository. GitHub keeps an invitation that
    expired unaccepted, and inviting the same login again only returns it, still expired, so `grant`
    cancels it first. An invitation sent to an email address has no login and is skipped."""
    rows = gh_lines(
        [
            "api",
            "--paginate",
            f"/repos/{CLAIMS_REPO}/invitations?per_page=100",
            "--jq",
            r'.[] | "\(.invitee.login) \(.id) \(.expired)"',
        ],
        CLAIMS_TOKEN,
    )
    found = {}
    for row in rows:
        login, number, expired = (row.split() + ["", "", ""])[:3]
        if login and login != "null":
            found[login] = (number, expired == "true")
    return found


def already_admitted(pending):
    """Logins that already have access, or have been asked: direct collaborators plus people who have
    not clicked yet (workers accept their own invitation, but an operator who has not started one
    since the grant still has it pending). `pending` is `invitations()`; one that expired does not
    count, so its login is invited again.

    This does NOT see people whose access comes from the organization. The collaborators endpoint
    resolves those only for a caller that can read org membership, and this App is installed on one
    repository and has no organization permissions at all, by design. `has_access` covers them."""
    collaborators = gh_lines(
        ["api", "--paginate", f"/repos/{CLAIMS_REPO}/collaborators?per_page=100", "--jq", ".[].login"],
        CLAIMS_TOKEN,
    )
    return set(collaborators) | {login for login, (_, expired) in pending.items() if not expired}


def has_access(login):
    """Can this login already push, however they got it? Asked per candidate rather than read off the
    collaborator list, because that list omits organization members (see `already_admitted`), and
    without this an org member is a candidate on every single run, forever."""
    p = gh(
        ["api", f"/repos/{CLAIMS_REPO}/collaborators/{login}/permission", "--jq", ".permission"],
        CLAIMS_TOKEN,
        check=False,
    )
    return p.returncode == 0 and p.stdout.strip() in ("write", "maintain", "admin")


def grant():
    """Invite every author who is not in yet. A failure on one login does not stop the others, but
    the job still ends red so somebody looks (an org that forbids outside collaborators, or requires
    2FA of them, fails exactly here)."""
    pending = invitations()
    prs = closed_prs()
    merged = merged_pr_authors(prs)
    landed = queue_landed_prs(prs)
    candidates = sorted((merged | set(landed)) - already_admitted(pending))
    invited = 0
    failures = 0
    branch = None  # read once, and only if a queue landing needs confirming
    for login in candidates:
        if has_access(login):
            continue  # an organization member; nothing to grant and nothing to report
        if login not in merged:
            branch = default_branch() if branch is None else branch
            if not any(landed_by_queue(number, branch) for number in landed[login][:3]):
                print(f"grant: skipped {login}: could not confirm that the merge queue landed one of their PRs")
                continue
        number, expired = pending.get(login, (None, False))
        if DRY_RUN:
            print(f"grant: would invite {login}" + (" (cancelling their expired invitation)" if expired else ""))
            invited += 1
            continue
        if expired:
            p = gh(["api", "-X", "DELETE", f"/repos/{CLAIMS_REPO}/invitations/{number}"], CLAIMS_TOKEN, check=False)
            if p.returncode != 0:
                failures += 1
                reason = (p.stderr or p.stdout).strip()
                print(f"grant: FAILED to cancel the expired invitation of {login}: {reason}", file=sys.stderr)
                continue
        p = gh(
            ["api", "-X", "PUT", f"/repos/{CLAIMS_REPO}/collaborators/{login}", "-f", "permission=push"],
            CLAIMS_TOKEN,
            check=False,
        )
        if p.returncode != 0:
            failures += 1
            print(f"grant: FAILED to invite {login}: {(p.stderr or p.stdout).strip()}", file=sys.stderr)
            continue
        # 201 answers with the invitation; 204 (empty) means they could already push after all, so
        # say what happened rather than claiming an invitation that was never sent.
        print(f"grant: invited {login} to {CLAIMS_REPO}" if p.stdout.strip() else f"grant: {login} already had access")
        invited += 1
    if not invited:
        print("grant: every landed-PR author already has the claim namespace")
    return failures


def sweep():
    """Delete long-expired leases. The lease is the ref's commit MESSAGE (an orphan commit over an
    empty tree), so reading one is a single API call and no clone is needed."""
    refs = gh_lines(
        [
            "api",
            "--paginate",
            f"/repos/{CLAIMS_REPO}/git/matching-refs/{NS}?per_page=100",
            "--jq",
            '.[] | .ref + " " + .object.sha',
        ],
        CLAIMS_TOKEN,
    )
    cutoff = time.time() - SWEEP_GRACE_MINUTES * 60
    failures = 0
    for entry in refs:
        ref, _, sha = entry.partition(" ")
        lease = gh(["api", f"/repos/{CLAIMS_REPO}/git/commits/{sha}", "--jq", ".message"], CLAIMS_TOKEN, check=False)
        if lease.returncode != 0 or not is_sweepable(lease.stdout, cutoff):
            continue
        if DRY_RUN:
            print(f"sweep: would delete expired {ref}")
            continue
        # `/git/<ref>` is `/git/refs/tauceti-claims/<key>`: the ref name already carries its prefix.
        deleted = gh(["api", "-X", "DELETE", f"/repos/{CLAIMS_REPO}/git/{ref}"], CLAIMS_TOKEN, check=False)
        if deleted.returncode == 0:
            print(f"sweep: deleted expired {ref}")
        else:
            failures += 1
            print(f"sweep: FAILED to delete {ref}: {(deleted.stderr or deleted.stdout).strip()}", file=sys.stderr)
    return failures


if __name__ == "__main__":
    for name, value in (("GH_TOKEN", GH_TOKEN), ("CLAIMS_TOKEN", CLAIMS_TOKEN), ("REPO", REPO)):
        if not value:
            raise SystemExit(f"claims_access: ${name} is required")
    sys.exit(1 if grant() + sweep() else 0)
