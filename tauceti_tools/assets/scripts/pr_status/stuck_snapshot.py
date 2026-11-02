"""One complete, head-bound PR snapshot for the hourly watchdog.

Fetching statuses and branch tips separately for every PR made the watchdog
exceed its ten-minute job timeout before it could reconcile any alerts.
"""

from github_api import GitHub

QUERY = """query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:OPEN, baseRefName:"main", first:50, after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number headRefOid headRefName isDraft updatedAt
        author { login }
        headRepository { nameWithOwner }
        headRef { target { oid } }
        labels(first:100) { nodes { name } pageInfo { hasNextPage } }
        commits(last:1) { nodes { commit { oid
          status { contexts { context state createdAt } }
          statusCheckRollup { contexts(first:100) {
            pageInfo { hasNextPage }
            nodes { __typename ... on CheckRun {
              name status conclusion completedAt detailsUrl
            } }
          } }
        } } }
      }
    }
  }
}"""


class Snapshot:
    def __init__(self, repo, client=None):
        self.repo = repo
        self.client = client or GitHub(max_wait=0)
        self._rows = None
        self.error = None

    def pulls(self):
        if self.error:
            raise RuntimeError("Watchdog PR snapshot unavailable") from self.error
        if self._rows is None:
            try:
                self._rows = self._load()
            except Exception as error:
                self.error = error
                raise
        return self._rows

    def _load(self):
        owner, name = self.repo.split("/", 1)
        cursor, seen, rows = None, set(), []
        for _ in range(100):
            data = self.client.graphql(QUERY, dict(owner=owner, name=name, cursor=cursor))
            connection = data["repository"]["pullRequests"]
            for node in connection["nodes"]:
                if node["labels"]["pageInfo"]["hasNextPage"]:
                    raise RuntimeError("Truncated watchdog PR labels")
                commits = node["commits"]["nodes"]
                if len(commits) != 1 or commits[0]["commit"]["oid"] != node["headRefOid"]:
                    raise RuntimeError("Watchdog commit does not match the PR head")
                commit = commits[0]["commit"]
                rollup = commit["statusCheckRollup"]
                checks = rollup["contexts"] if rollup else {"nodes": [], "pageInfo": {"hasNextPage": False}}
                rows.append(dict(
                    number=node["number"], head=node["headRefOid"],
                    head_ref=node["headRefName"],
                    head_repo=(node["headRepository"] or {}).get("nameWithOwner", ""),
                    branch_tip=((node["headRef"] or {}).get("target") or {}).get("oid", ""),
                    draft=node["isDraft"], updated_at=node["updatedAt"],
                    author=(node["author"] or {}).get("login", ""),
                    labels=[item["name"] for item in node["labels"]["nodes"]],
                    statuses=(commit["status"] or {}).get("contexts", []),
                    checks=checks["nodes"], checks_truncated=checks["pageInfo"]["hasNextPage"],
                ))
            page = connection["pageInfo"]
            if not page["hasNextPage"]:
                return rows
            cursor = page["endCursor"]
            if not cursor or cursor in seen:
                raise RuntimeError("Watchdog pagination did not advance")
            seen.add(cursor)
        raise RuntimeError("Watchdog PR listing was truncated")

    def status(self, head, context):
        rows = [row for row in self.pulls() if row["head"] == head]
        if not rows:
            raise RuntimeError("Head is absent from the watchdog snapshot")
        contexts = [item for item in rows[0]["statuses"] if item["context"] == context]
        newest = max(contexts, key=lambda item: item["createdAt"], default=None)
        return (newest["state"].lower(), newest["createdAt"]) if newest else (None, None)
