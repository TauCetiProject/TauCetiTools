"""Find head-bound self-tested decisions before expensive workflow-run discovery."""

import datetime as dt
import os
from pathlib import Path

from github_api import GitHub


QUERY = """query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner,name:$name) {
    pullRequests(first:100,after:$cursor,states:OPEN,labels:["self-tested"],
                 orderBy:{field:UPDATED_AT,direction:ASC}) {
      nodes { number headRefOid
        commits(last:1) { nodes { commit { oid status { contexts { context state } } } } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""


def scan(repo, previous, *, now, api=None):
    """Complete inventory, not a search capped at 1,000 PRs or check runs.

    Track age from the first observation of this head awaiting a decision. It
    is a lower bound on the request's age, never the PR's unrelated update age.
    The normal assessment still checks membership, paths and the live label.
    """
    api = api or GitHub(max_wait=0)
    owner, name = repo.split("/")
    cursor, pending = None, {}
    while True:
        page = api.graphql(QUERY, dict(owner=owner, name=name, cursor=cursor))["repository"]["pullRequests"]
        for pr in page["nodes"]:
            commit = pr["commits"]["nodes"][-1]["commit"]
            if commit["oid"] != pr["headRefOid"]:
                raise RuntimeError("Self-tested inventory contains a different commit")
            accepted = any(c["context"] == f"self-tested/{pr['number']}" and c["state"] == "SUCCESS"
                           for c in (commit["status"] or {}).get("contexts", []))
            if accepted:
                continue
            key = str(pr["number"])
            old = previous.get(key, {})
            pending[key] = (dict(old) if old.get("head") == pr["headRefOid"]
                            else dict(head=pr["headRefOid"], first_seen_at=now))
        if not page["pageInfo"]["hasNextPage"]:
            return pending
        following = page["pageInfo"]["endCursor"]
        if not following or following == cursor:
            raise RuntimeError("Self-tested inventory did not advance")
        cursor = following


def report(pending, *, observed, now):
    """Expose known pending age in logs and the workflow summary, even on quota deferral."""
    oldest = min((v["first_seen_at"] for v in pending.values()), default=None)
    age = (max(0, int((dt.datetime.fromisoformat(now.replace("Z", "+00:00")) -
                      dt.datetime.fromisoformat(oldest.replace("Z", "+00:00"))).total_seconds()))
           if oldest else 0)
    coverage = "current inventory" if observed else "retained inventory; current count unverified"
    text = (f"Self-tested decisions: {len(pending)} pending; oldest observed wait {age}s; {coverage}.\n"
            "Wait age starts when this commit was first observed awaiting assessment.\n")
    print(text, flush=True)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a") as output:
            output.write("\n### Self-tested policy assessment\n\n" + text + "\n")
