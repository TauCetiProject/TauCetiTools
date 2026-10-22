#!/usr/bin/env python3
"""Existing runner picker: ordered fleet placement and observed cloud capacity."""

import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

LABELS = {"self-hosted", "linux", "x64", "tauceti-fleet"}


def inventory(pages):
    """Reject partial or inconsistent runner inventories before using capacity."""
    found, total = [], None
    for page in pages:
        if type(page.get("total_count")) is not int or not isinstance(page.get("runners"), list):
            raise ValueError("invalid runner inventory")
        if total is not None and total != page["total_count"]:
            raise ValueError("runner inventory changed between pages")
        total = page["total_count"]
        found.extend(page["runners"])
    if total is None or len(found) != total:
        raise ValueError("incomplete runner inventory")
    return found


def ranked_runners(pages, *, kind, machines, reserved=()):
    """Extend the existing picker with ordered, class-safe machine candidates.

    Each machine has one shared ceiling across PR and staging work. Queued jobs
    already targeting an instance count against that machine's observed capacity.
    """
    if kind not in ("pr", "staging"):
        raise ValueError("unknown CI job class")
    found = inventory(pages)
    ids = [r.get("id") for r in found]
    if any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("ambiguous runner identities")
    reserved = set(reserved)
    result, seen = [], set()
    for machine in machines:
        identity, maximum = machine["machine_id"], machine["max_jobs"]
        if not re.fullmatch(r"[a-f0-9]{32}", identity) or identity in seen:
            raise ValueError("invalid or duplicate machine identity")
        seen.add(identity)
        if type(maximum) is not int or not 1 <= maximum <= 16:
            raise ValueError("invalid shared machine limit")
        if not isinstance(machine["kinds"], list) or not machine["kinds"] or any(k not in ("pr", "staging") for k in machine["kinds"]):
            raise ValueError("invalid machine job classes")
        if kind not in machine["kinds"]:
            continue
        matching = []
        for runner in found:
            labels = {str(label.get("name", "")).lower() for label in runner.get("labels", [])}
            if "fleet-" + identity in labels:
                matching.append((runner, labels))
        occupied = {r["id"] for r, _ in matching if r.get("busy") is not False or r["id"] in reserved}
        room = max(0, maximum - len(occupied))
        ready = []
        for runner, labels in matching:
            instances = [label for label in labels if re.fullmatch(r"fleet-instance-[a-f0-9]{16}", label)]
            required = {"self-hosted", "linux", "x64", "fleet-" + identity, "tauceti-fleet-" + kind}
            if (runner.get("status") == "online" and runner.get("busy") is False
                    and runner["id"] not in reserved and required <= labels and len(instances) == 1):
                ready.append(dict(machine=machine["name"], machine_id=identity, runner_id=runner["id"],
                                  labels=sorted(required | {instances[0]})))
        result.extend(sorted(ready, key=lambda r: r["runner_id"])[:room])
    return result


def choose(pages, *, enabled, attempt):
    """Prefer an idle matching runner only for enabled first attempts.

    Pages must form a complete, consistently sized runner-group inventory.
    Disabled routing, retries, or incomplete observations return False so the
    caller retains Namespace. This function reserves no runner capacity.
    """
    if not enabled or attempt != 1:
        return False
    try:
        found = inventory(pages)
    except ValueError:
        return False
    return any(
        r.get("status") == "online"
        and r.get("busy") is False
        and LABELS
        <= {str(label.get("name", "")).lower() for label in r.get("labels", [])}
        for r in found
    )


def legacy_main():
    selected = False
    try:
        attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
        enabled = os.environ.get("FLEET_CI_ENABLED") == "true"
        token = os.environ.get("FLEET_READ_TOKEN", "")
        group = int(os.environ.get("FLEET_RUNNER_GROUP_ID") or "0")
        pages = []
        if enabled and attempt == 1 and token and group > 0:
            for page in range(1, 21):
                request = urllib.request.Request(
                    f"https://api.github.com/orgs/TauCetiProject/actions/runner-groups/{group}/runners?per_page=100&page={page}",
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "TauCetiFleetPicker/1.0",
                    },
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError("runner inventory exceeds bound")
                data = json.loads(raw)
                pages.append(data)
                if sum(len(p["runners"]) for p in pages) >= data["total_count"]:
                    break
            selected = choose(pages, enabled=enabled, attempt=attempt)
    except (OSError, ValueError, KeyError, TypeError):
        print("::notice::Fleet inventory unavailable/incomplete; retaining Namespace")
    with open(os.environ["GITHUB_OUTPUT"], "a") as out:
        out.write("fleet=" + ("true" if selected else "false") + "\n")
    print("Dedicated fleet selected" if selected else "Namespace selected")


class ObservationError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ObservationError("authenticated API redirected")


class APIPage:
    def __init__(self, body, next_page):
        self.body, self.next_page = body, next_page


def shared_capacity(repository, token):
    """Read the existing shared discovery endpoint, accepting only fresh coverage."""
    if not token:
        return None
    request = urllib.request.Request(
        "https://github-state.taucetiproject.org/v1/runner-capacity?" + urllib.parse.urlencode(dict(repository=repository)),
        headers={"Authorization": "Bearer " + token, "User-Agent": "TauCetiFleetPicker/2.0"})
    # Cold shared scans have a 15-second discovery deadline and drain their
    # in-flight requests. Let that single scan finish before direct fallback.
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ObservationError("capacity observation exceeds bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ObservationError("invalid shared capacity response")
    now = time.time() * 1000
    def numeric(n):
        return type(n) in (int, float) and math.isfinite(n)
    if (value.get("complete") is not True or value.get("organization") != repository.split("/")[0]
            or type(value.get("busy")) is not int or not 0 <= value["busy"] <= 10000
            or not numeric(value.get("observed_at")) or not -5000 <= now - value["observed_at"] <= 150000
            or not numeric(value.get("retry_at")) or value["retry_at"] <= now):
        raise ObservationError("incomplete or stale shared capacity")
    result = dict(github_busy=value["busy"], namespace_busy=None, queued_fleet=None)
    if value.get("routing_version") == 2:
        queued = value.get("queued_fleet")
        if (type(value.get("namespace_busy")) is not int or not 0 <= value["namespace_busy"] <= 10000
                or not isinstance(queued, list) or len(queued) > 10000
                or any(not isinstance(labels, list) or len(labels) > 100 or any(not isinstance(v, str) for v in labels) for labels in queued)):
            raise ObservationError("invalid shared placement capacity")
        result.update(namespace_busy=value["namespace_busy"], queued_fleet=[set(labels) for labels in queued])
    return result


class GitHubRead:
    def __init__(self, token, reserve):
        self.token, self.reserve, self.remaining = token, reserve, None
        self.lock = threading.Lock()
        self.calls = 0

    def read(self, path):
        with self.lock:
            if self.remaining is not None:
                if self.remaining <= self.reserve:
                    raise ObservationError("API quota reserve protected")
                self.remaining -= 1
            self.calls += 1
        request = urllib.request.Request("https://api.github.com/" + path, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
            "User-Agent": "TauCetiFleetPicker/2.0"})
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            remaining = response.headers.get("X-RateLimit-Remaining", "")
            if remaining.isdigit():
                with self.lock:
                    self.remaining = min(self.remaining, int(remaining)) if self.remaining is not None else int(remaining)
        if len(raw) > 4 * 1024 * 1024:
            raise ObservationError("API observation exceeds size bound")
        return APIPage(json.loads(raw), 'rel="next"' in response.headers.get("Link", ""))

    def preflight(self):
        self.remaining = self.read("rate_limit").body["resources"]["core"]["remaining"]
        if type(self.remaining) is not int or self.remaining <= self.reserve:
            raise ObservationError("API quota reserve protected")


def pages(read, path, key):
    found, total = [], None
    for page in range(1, 21):
        data = read(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
        linked = isinstance(data, APIPage)
        more = data.next_page if linked else None
        data = data.body if linked else data
        batch = data if key is None else data[key]
        if not isinstance(batch, list):
            raise ObservationError("invalid API inventory")
        if key is not None and (not linked or key == "runners"):
            count = data["total_count"]
            if type(count) is not int or count < 0 or total is not None and count != total:
                raise ObservationError("API inventory changed between pages")
            total = count
        found.extend(batch)
        if linked and key == "runners" and not more and len(found) != total:
            raise ObservationError("incomplete runner inventory")
        if linked and not more or not linked and (key is None and len(batch) < 100 or key is not None and len(found) == total):
            return found
        if not batch or total is not None and len(found) > total:
            break
    raise ObservationError("incomplete API inventory")


def namespace_labels(labels):
    return any(label.startswith(("nscloud-", "namespace-profile-")) for label in labels)


CONTROL_JOBS = {"pick", "recover", "retry-status", "publish", "finalize-merge-group-build", "finalize-batch"}


def build_job(job):
    name = str(job.get("name", "")).split(" / ")[-1]
    return name not in CONTROL_JOBS and not any("-2x4-" in label for label in job.get("labels", []))


def scan_capacity(read, *, repository):
    """The existing org-wide scan, also counting Namespace builds and queued fleet targets."""
    owner = repository.split("/")[0]
    repos = pages(read, f"orgs/{owner}/repos", None)
    names = [r["name"] for r in repos if not r.get("archived")]
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", name) for name in names):
        raise ObservationError("invalid repository identity")
    def runs(name):
        result = pages(read, f"repos/{owner}/{name}/actions/runs?status=in_progress", "workflow_runs")
        result += pages(read, f"repos/{owner}/{name}/actions/runs?status=queued", "workflow_runs")
        return [(f"{owner}/{name}", run["id"]) for run in result]
    with ThreadPoolExecutor(max_workers=8) as pool:
        active = sorted({entry for batch in pool.map(runs, names) for entry in batch})
        batches = list(pool.map(lambda entry: pages(read, f"repos/{entry[0]}/actions/runs/{entry[1]}/jobs", "jobs"), active))
    github, namespace, queued = 0, 0, []
    for batch in batches:
        for job in batch:
            labels = job.get("labels", [])
            if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
                raise ObservationError("invalid runner labels")
            status = job.get("status")
            if status == "in_progress" and not namespace_labels(labels) and "self-hosted" not in labels:
                github += 1
            if status in ("queued", "in_progress") and namespace_labels(labels) and build_job(job):
                namespace += 1
            if status == "queued" and "self-hosted" in labels:
                queued.append(set(labels))
    return dict(github_busy=github, namespace_busy=namespace, queued_fleet=queued)


def resolve_machines(config, runners):
    """Resolve an enrollment label to exactly one permanent machine identity."""
    result = {}
    for name, source in config.items():
        machine = dict(source, name=name)
        identity = machine.get("machine_id")
        if not identity:
            label = "fleet-host-" + machine["enrollment_name"].lower()
            identities = set()
            for runner in runners:
                labels = {str(label.get("name", "")).lower() for label in runner.get("labels", [])}
                if label in labels:
                    identities.update(v.removeprefix("fleet-") for v in labels if re.fullmatch(r"fleet-[a-f0-9]{32}", v))
            if len(identities) != 1:
                continue
            identity = identities.pop()
        machine["machine_id"] = identity
        result[name] = machine
    return result


def route(*, kind, order, machines, runner_pages, observed, github_budget, namespace_cap, excluded=(), selection_key=""):
    """Choose in the configured preference order using one bounded observation.

    Limits are approximate: simultaneous pickers can observe the same spare slot.
    When all pools are full or unknown, wait for a free GitHub-hosted slot.
    """
    if type(github_budget) is not int or github_budget < 1 or type(namespace_cap) is not int or namespace_cap < 1:
        raise ObservationError("invalid capacity budgets")
    if kind != "staging" and observed.get("queued_fleet", []) is None:
        runner_pages = []
    try:
        resolved = resolve_machines(machines, inventory(runner_pages)) if runner_pages else {}
        reserved = set()
        for runner in inventory(runner_pages) if runner_pages else []:
            labels = {str(label.get("name", "")).lower() for label in runner.get("labels", [])}
            if any(queued <= labels for queued in (observed.get("queued_fleet") or [])):
                reserved.add(runner["id"])
        ranked = ranked_runners(runner_pages, kind=kind, machines=[resolved[n] for n in order if n in resolved], reserved=reserved) if kind in ("pr", "staging") and runner_pages else []
    except (ValueError, KeyError, TypeError):
        print("::warning::Fleet configuration or inventory invalid; retaining cloud choices")
        resolved, ranked = {}, []
    for name in order:
        if name in excluded or name in resolved and resolved[name]["machine_id"] in excluded:
            continue
        if name == "github" and observed.get("github_busy") is not None and observed["github_busy"] <= github_budget:
            return dict(pool="github")
        if name == "namespace" and observed.get("namespace_busy") is not None and observed["namespace_busy"] < namespace_cap:
            return dict(pool="namespace")
        candidates = [r for r in ranked if r["machine"] == name]
        if candidates:
            offset = int(hashlib.sha256(selection_key.encode()).hexdigest(), 16) if selection_key else 0
            return dict(candidates[offset % len(candidates)], pool=name)
    return dict(pool="github", waiting=True)


def failed_placements(read, repository, run_id, attempt):
    """A same-run retry skips only pools whose earlier job never acquired a runner."""
    excluded = set()
    for prior in range(max(1, attempt - 3), attempt):
        jobs = pages(read, f"repos/{repository}/actions/runs/{run_id}/attempts/{prior}/jobs", "jobs")
        builds = [j for j in jobs if j.get("name") == "sandboxed-build"]
        if len(builds) > 1:
            raise ObservationError("ambiguous prior build placement")
        for job in builds:
            if job.get("status") != "completed" or job.get("conclusion") != "cancelled" or job.get("runner_id") or job.get("runner_name"):
                continue
            labels = job.get("labels", [])
            excluded.update(label.removeprefix("fleet-") for label in labels if re.fullmatch(r"fleet-[a-f0-9]{32}", label))
            if namespace_labels(labels):
                excluded.add("namespace")
    return excluded


def fleet_inventory(environment, repository):
    """Read current dedicated-runner availability, without cloud discovery."""
    runner_pages = []
    try:
        if environment.get("FLEET_CI_ENABLED") == "true" and environment.get("FLEET_READ_TOKEN"):
            reader = GitHubRead(environment["FLEET_READ_TOKEN"], 0)
            group = int(environment["FLEET_RUNNER_GROUP_ID"])
            if group <= 0:
                raise ObservationError("invalid fleet runner group")
            runners = pages(reader.read, f"orgs/{repository.split('/')[0]}/actions/runner-groups/{group}/runners", "runners")
            runner_pages = [dict(total_count=len(runners), runners=runners)]
    except (OSError, ValueError, KeyError, TypeError):
        print("::notice::Fleet inventory unavailable; retaining cloud choices")
    return runner_pages


def route_main():
    environment = os.environ
    repository = environment["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ObservationError("invalid repository")
    config = tomllib.loads(Path("tauceti.toml").read_text())["services"]["fleet"]
    kind = environment.get("FLEET_JOB_KIND", "other")
    budget = int(environment.get("BUDGET") or "18")
    namespace_cap = int(environment.get("NAMESPACE_BUILD_CAP") or config.get("namespace_build_cap", 20))
    observed = dict(github_busy=None, namespace_busy=None, queued_fleet=None)
    token = environment.get("GH_TOKEN", "")
    fallback = environment.get("FALLBACK_TOKEN", "")
    capacity_reader = None
    readers = [(token, int(environment.get("API_RESERVE") or "1500"))]
    if fallback and fallback != token:
        readers.append((fallback, int(environment.get("REPOSITORY_API_RESERVE") or "250")))
    try:
        observed = shared_capacity(repository, environment.get("GITHUB_STATE_TOKEN", "")) or observed
    except (OSError, ValueError, KeyError, TypeError):
        print("::notice::Shared capacity unavailable; retaining bounded direct discovery")
    order = config.get("staging_order" if kind == "staging" else "pr_order", ["github", "namespace"])
    excluded = set()
    attempt = int(environment["GITHUB_RUN_ATTEMPT"])
    if attempt > 1:
        try:
            if capacity_reader is None:
                for credential, reserve in readers:
                    try:
                        capacity_reader = GitHubRead(credential, reserve)
                        capacity_reader.preflight()
                        break
                    except (OSError, ValueError, KeyError, TypeError):
                        capacity_reader = None
                if capacity_reader is None:
                    raise ObservationError("retry placement history unavailable")
            excluded = failed_placements(capacity_reader.read, repository, environment["GITHUB_RUN_ID"], attempt)
        except (OSError, ValueError, KeyError, TypeError):
            excluded = None
            print("::notice::Retry placement history unavailable; retaining cloud choices")
    runner_pages = fleet_inventory(environment, repository) if kind == "staging" and excluded is not None else []
    selected = None
    # Staging can use its own complete idle-runner inventory even when unrelated
    # global queue coverage is unavailable. Honor known reservations and only
    # consider machines preceding the first cloud preference. Retry exclusions
    # have already been checked, so recovery can also avoid global scans.
    if kind == "staging":
        fleet_order = []
        for name in order:
            if name in ("github", "namespace"):
                break
            fleet_order.append(name)
        candidate = route(kind=kind, order=fleet_order, machines=config.get("machines", {}), runner_pages=runner_pages,
                          observed=observed, github_budget=budget, namespace_cap=namespace_cap, excluded=excluded or (),
                          selection_key=environment["GITHUB_RUN_ID"])
        if candidate["pool"] not in ("github", "namespace"):
            selected = candidate
    # Older shared-service deployments can still answer the first PR/cloud
    # preference without a direct scan. Full v2 snapshots cover all destinations.
    github_first = bool(order) and order[0] == "github" and observed["github_busy"] is not None and observed["github_busy"] <= budget
    scanned = False
    if selected is None and observed["namespace_busy"] is None and not github_first:
        for credential, reserve in readers:
            try:
                reader = GitHubRead(credential, reserve)
                reader.preflight()
                observed = scan_capacity(reader.read, repository=repository)
                capacity_reader = reader
                scanned = True
                break
            except (OSError, ValueError, KeyError, TypeError):
                print("::notice::Capacity scan incomplete or API reserve protected")
    # Observe queued reservations before availability: a formerly queued job may
    # acquire a runner during a slow direct scan. PRs read inventory only here.
    if kind != "staging" or scanned:
        runner_pages = fleet_inventory(environment, repository)
    if kind == "pr" and not re.fullmatch(r"[a-f0-9]{40}", environment.get("PR_HEAD", "")):
        runner_pages = []
    if attempt > 1 and excluded is None:
        runner_pages = []
    selected = selected or route(kind=kind, order=order, machines=config.get("machines", {}), runner_pages=runner_pages,
                     observed=observed, github_budget=budget, namespace_cap=namespace_cap, excluded=excluded or (),
                     selection_key=environment["GITHUB_RUN_ID"])
    target = "ubuntu-24.04"
    if selected["pool"] == "namespace":
        staging = kind == "staging"
        target = [f"nscloud-ubuntu-24.04-amd64-{'16x32' if staging else '8x16'}-with-features" + (";job.priority=1" if staging else ""),
                  "namespace-features:container.privileged=true" + (f";github.run-id={environment['GITHUB_RUN_ID']}" if staging else "")]
    elif selected["pool"] != "github":
        target = dict(group="TauCetiFleet", labels=selected["labels"])
    values = dict(runner=json.dumps(target, separators=(",", ":")), pool=selected["pool"],
                  fleet=str(selected["pool"] not in ("github", "namespace")).lower(),
                  github=str(selected["pool"] == "github").lower(), busy=observed["github_busy"],
                  namespace_busy=observed["namespace_busy"], budget=budget, namespace_cap=namespace_cap,
                  attempt=environment["GITHUB_RUN_ATTEMPT"])
    with open(environment["GITHUB_OUTPUT"], "a") as out:
        out.write("".join(f"{k}={v if v is not None else ''}\n" for k, v in values.items()))
    message = (f"CI class={kind}; pool={selected['pool']}; GitHub busy={observed['github_busy']}/{budget}; "
               f"Namespace builds queued/running={observed['namespace_busy']}/{namespace_cap} (approximate cap).")
    print(message)
    if environment.get("GITHUB_STEP_SUMMARY"):
        with open(environment["GITHUB_STEP_SUMMARY"], "a") as out:
            out.write(message + "\n")


def main():
    if sys.argv[1:] == ["--route"]:
        route_main()
    else:
        legacy_main()


if __name__ == "__main__":
    main()
