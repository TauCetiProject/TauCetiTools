"""One complete, head-bound PR snapshot for the hourly watchdog.

Fetching statuses and branch tips separately for every PR made the watchdog
exceed its ten-minute job timeout before it could reconcile any alerts.
"""

import json

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
        self.review_prs = {}
        self.error = None
        self._timelines = None
        self._timeline_error = None
        self.readiness_prs = {}

    def _connections(self, numbers, selections, scalars=""):
        """Batch complete PR connections, binding every page to the observed head.

        Each alias has independent cursors; partial pages and moving heads fail
        closed rather than turning absent evidence into a cleared alert.
        """
        owner, name = self.repo.split("/", 1)
        heads = {row["number"]: row["head"] for row in self.pulls()}
        numbers = sorted(set(numbers))
        if any(type(number) is not int or number <= 0 or number not in heads for number in numbers):
            raise ValueError("Unknown watchdog PR identity")
        pending = {number: dict.fromkeys(selections) for number in numbers}
        seen = {(number, field): set() for number in numbers for field in selections}
        result = {number: {field: [] for field in selections} for number in numbers}
        for _ in range(100):
            # Freeze this round's aliases: deleting completed connections must
            # not shift later chunks past an unobserved PR.
            batch_numbers = list(pending)
            for start in range(0, len(batch_numbers), 20):
                batch = batch_numbers[start:start + 20]
                aliases = []
                for number in batch:
                    fields = []
                    for field, cursor in pending[number].items():
                        arguments = "first:100" + (",after:" + json.dumps(cursor) if cursor else "")
                        if field == "timelineItems":
                            arguments += ",itemTypes:[LABELED_EVENT,UNLABELED_EVENT,REMOVED_FROM_MERGE_QUEUE_EVENT]"
                        fields.append(f"{field}({arguments}) {{ nodes {{ {selections[field]} }} "
                                      "pageInfo { hasNextPage endCursor } }")
                    aliases.append(f"pr{number}:pullRequest(number:{number}) {{ number state headRefOid "
                                   + scalars + " " + " ".join(fields) + " }")
                query = ("query($owner:String!, $name:String!) { repository(owner:$owner,name:$name) { "
                         + " ".join(aliases) + " } }")
                data = self.client.graphql(query, dict(owner=owner, name=name))["repository"]
                if not isinstance(data, dict) or any(f"pr{number}" not in data for number in batch):
                    raise RuntimeError("Incomplete watchdog evidence batch")
                for number in batch:
                    node = data[f"pr{number}"]
                    if (not isinstance(node, dict) or node.get("number") != number
                            or node.get("state") not in {"OPEN", "CLOSED", "MERGED"}):
                        raise RuntimeError("Missing watchdog PR evidence")
                    if node["state"] != "OPEN":
                        result[number] = {**{field: [] for field in selections}, "state": "closed"}
                        del pending[number]
                        continue
                    if node.get("headRefOid") != heads[number]:
                        raise RuntimeError("Watchdog PR head changed during evidence collection")
                    result[number]["state"] = "open"
                    if scalars:
                        if node.get("mergeable") not in {"MERGEABLE", "CONFLICTING", "UNKNOWN"}:
                            raise RuntimeError("Missing watchdog mergeability")
                        result[number]["mergeable"] = node["mergeable"]
                    for field in list(pending[number]):
                        connection = node.get(field)
                        if not isinstance(connection, dict):
                            raise RuntimeError("Missing watchdog evidence connection")
                        rows, page = connection.get("nodes"), connection.get("pageInfo")
                        if (not isinstance(rows, list) or not isinstance(page, dict)
                                or type(page.get("hasNextPage")) is not bool):
                            raise RuntimeError("Incomplete watchdog evidence page")
                        result[number][field].extend(rows)
                        if not page["hasNextPage"]:
                            del pending[number][field]
                            continue
                        cursor = page.get("endCursor")
                        if not isinstance(cursor, str) or not cursor or cursor in seen[number, field]:
                            raise RuntimeError("Watchdog evidence pagination did not advance")
                        seen[number, field].add(cursor)
                        pending[number][field] = cursor
                    if not pending[number]:
                        del pending[number]
            if not pending:
                return result
        raise RuntimeError("Watchdog evidence was truncated")

    def timeline_events(self, number):
        """Reuse complete ready-PR label/eviction histories across both detectors."""
        if self._timeline_error:
            raise RuntimeError("Watchdog timelines unavailable") from self._timeline_error
        if self._timelines is None:
            try:
                numbers = [row["number"] for row in self.pulls()
                           if not row.get("draft") and "ready-to-merge" in row.get("labels", [])]
                selection = ("__typename ... on LabeledEvent { createdAt label { name } } "
                             "... on UnlabeledEvent { createdAt label { name } } "
                             "... on RemovedFromMergeQueueEvent { createdAt }")
                self._timelines = self._connections(numbers, {"timelineItems": selection})
                for record in self._timelines.values():
                    for event in record["timelineItems"]:
                        if (event.get("__typename") not in {"LabeledEvent", "UnlabeledEvent",
                                                            "RemovedFromMergeQueueEvent"}
                                or not isinstance(event.get("createdAt"), str) or not event["createdAt"]):
                            raise RuntimeError("Incomplete watchdog timeline event")
            except Exception as error:
                self._timeline_error = error
                raise
        return self._timelines[number]["timelineItems"]

    def load_readiness(self, numbers):
        """Fetch complete comments, paths and mergeability for aged green candidates."""
        self.readiness_prs.update(self._connections(numbers, {
            "comments": "body updatedAt",
            "files": "path",
        }, "mergeable"))

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

    def load_review_prs(self, numbers):
        """Tracking issues outlive closed PRs; batch their explicit state reads.

        Absence from the main-branch inventory is insufficient: it could be an
        open PR against another branch, or a number that never named a PR.
        """
        owner, name = self.repo.split("/", 1)
        numbers = sorted(set(int(number) for number in numbers))
        for start in range(0, len(numbers), 50):
            batch = numbers[start:start + 50]
            if any(number <= 0 for number in batch):
                raise ValueError("Invalid review tracking PR number")
            fields = " ".join(f"pr{number}:pullRequest(number:{number}) {{ state headRefOid }}" for number in batch)
            query = "query($owner:String!, $name:String!) { repository(owner:$owner,name:$name) { " + fields + " } }"
            data = self.client.graphql(query, dict(owner=owner, name=name))["repository"]
            for number in batch:
                node = data[f"pr{number}"]
                self.review_prs[number] = (dict(state="closed" if node["state"] == "MERGED" else node["state"].lower(),
                                               head=node["headRefOid"])
                                           if node else {})
