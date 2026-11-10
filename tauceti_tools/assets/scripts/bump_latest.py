#!/usr/bin/env python3
"""Maintain one fixed-target Mathlib bump, selected by upstream cache availability.

The target is normally the newest master commit with a complete Mathlib cache. When a
Mathlib release commit lies on master between the current pin and that commit, the bump
stops there first, so the first TauCeti commit on the new toolchain, which
`toolchain_tags.py` tags, pins exactly Mathlib's own release commit. A project depending
on both TauCeti and another library at the same release tag then gets one Mathlib
revision. The next scheduled run bumps past the release as usual.

A release commit is a first-parent master commit that moves `lean-toolchain` to a Lean
release (`v4.36.0-rc2`, ...). Mathlib puts its `vX` tag on exactly that commit, but the
commit exists before the tag is pushed, so the bump cannot step past a release while its
tag is still missing. Mathlib's tags, read with `git ls-remote`, are also treated as
release commits and name the bump.

If the release commit is not selectable yet (its upstream build has not finished, or its
cache is incomplete), the bump waits for it rather than opening an extra bump to a commit
just before it. After `RELEASE_GRACE_SECONDS` it steps over that release, so a broken
upstream release cannot stall freshness for longer than that. A release whose bump PR was
closed without merging is stepped over at once.

Only one bump is open at a time, and its target stays fixed while workers repair TauCeti
against it. A fixed target still ages, so once the active bump's target Mathlib commit is
older than `BUMP_RETARGET_HOURS` (by upstream committer date) the bump is retargeted: a new
`bump-mathlib/<target>` branch starts from the old bump's head, so every source repair
carries over, and adds one commit moving the manifest and toolchain to the newest target
chosen as for a fresh bump but counted from the old target, so release commits already
behind the old target are passed. The new PR is opened and the old one closed. A bump in
the merge queue is never retargeted: an approved PR waiting for insertion would be
cancelled, and an inserted one lands regardless. Drafts and held PRs are left alone too.

`--select` runs without credentials and also selects a target for a stale, retargetable
bump. `--publish` repeats every staleness and queue check with fresh reads before acting.
"""

import argparse
import base64
import copy
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from bump_manifest import problems
from pr_status.core import KEEP_LABELS, automatic_bump

REPO = "TauCetiProject/TauCeti"
MATHLIB = "leanprover-community/mathlib4"
BOT = "tauceti-review-bot[bot]"
TOOLCHAIN_PREFIX = "leanprover/lean4:"
RELEASE_RE = re.compile(r"\Av\d+\.\d+\.\d+(?:-rc\d+)?\Z")
# How long a release tag commit may stay unselectable before the bump steps over it.
RELEASE_GRACE_SECONDS = 24 * 3600
# Once the active bump's target is this old, retarget it rather than keep repairing a pin
# that will already be stale when it lands.
BUMP_RETARGET_HOURS = 48
QUEUE_STATUS = "https://queue.taucetiproject.org/status"
# Added when the merge queue inserts a PR into a batch; from then on it lands regardless.
QUEUE_INSERTED_LABEL = "bors-staging"
BOT_EMAIL = "290224625+tauceti-review-bot[bot]@users.noreply.github.com"
RETARGET_MARKER = re.compile(r"<!-- tauceti-retarget-from: #(\d+) -->")


def command(args, *, cwd=None, env=None, combined=False):
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=1800)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr or result.stdout}")
    return (result.stdout + ("\n" + result.stderr if combined else "")).strip()


def api(path):
    return json.loads(command(["gh", "api", path]))


def sha(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{40}", value):
        raise ValueError("Expected a full commit SHA")
    return value


def pin(manifest):
    values = [p for p in manifest["packages"] if p.get("name") == "mathlib"]
    if len(values) != 1:
        raise ValueError("Expected exactly one Mathlib dependency")
    return sha(values[0]["rev"])


def upstream_file(commit, path):
    data = api(f"repos/{MATHLIB}/contents/{path}?ref={sha(commit)}")
    if data["encoding"] != "base64":
        raise ValueError("Upstream file encoding unavailable")
    return base64.b64decode(data["content"]).decode()


def relationship(base, target):
    if base == target:
        return "identical"
    status = api(f"repos/{MATHLIB}/compare/{sha(base)}...{sha(target)}?per_page=1")["status"]
    if status not in {"ahead", "behind", "identical"}:
        raise RuntimeError("Mathlib history diverged; refusing to change a dependency target")
    return status


def open_bumps():
    found = []
    for page in range(1, 100):
        prs = api(f"repos/{REPO}/pulls?state=open&per_page=100&page={page}")
        found.extend(p for p in prs if automatic_bump(p, repo=REPO) and p["base"]["ref"] == "main")
        if len(prs) < 100:
            return found
    raise RuntimeError("Open pull request listing was truncated")


def bump_state(current, prs):
    """A held PR is left alone; otherwise only an obsolete target may be closed."""
    active, obsolete = [], []
    for pr in prs:
        if pr.get("draft") or {p["name"].lower() for p in pr.get("labels", [])} & KEEP_LABELS:
            active.append(pr)
            continue
        data = api(f"repos/{REPO}/contents/lake-manifest.json?ref={sha(pr['head']['sha'])}")
        target = pin(json.loads(base64.b64decode(data["content"])))
        (active if relationship(current, target) == "ahead" else obsolete).append(pr)
    return active, obsolete


def ordered_runs(runs, history):
    positions = {value: i for i, value in enumerate(history)}
    eligible = {}
    for run in runs:
        target = run.get("head_sha")
        if (
            run.get("event") == "push"
            and run.get("head_branch") == "master"
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and run.get("head_repository", {}).get("full_name") == MATHLIB
            and target in positions
        ):
            eligible.setdefault(target, run)
    return sorted(eligible.values(), key=lambda r: positions[r["head_sha"]])


def verify_cache(checkout, target):
    """Only a proven missing cache falls back. Network/tool errors fail the run."""
    command(["git", "checkout", "--force", "--detach", sha(target)], cwd=checkout)
    # The subprocesses execute only trusted upstream sources, in a separate read-only
    # workflow job. Do not inherit GitHub/runner credentials or use warmed oleans.
    probe_home = checkout.parent / "probe-home"
    probe_home.mkdir(exist_ok=True)
    env = {
        k: os.environ[k]
        for k in ("PATH", "ELAN_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if k in os.environ
    }
    env["HOME"] = str(probe_home)
    env.setdefault("ELAN_HOME", str(Path.home() / ".elan"))
    env["MATHLIB_CACHE_DIR"] = str(checkout.parent / "downloads")
    env["MATHLIB_NO_CACHE_ON_UPDATE"] = "1"
    env["LAKE_NO_CACHE"] = "1"
    toolchain = (checkout / "lean-toolchain").read_text().strip()
    command(["elan", "toolchain", "install", toolchain], cwd=checkout, env=env)
    lake = ["elan", "run", toolchain, "lake"]
    output = command(
        [*lake, "exe", "cache", "--cache-from=master", "get", "Mathlib"],
        cwd=checkout,
        env=env,
        combined=True,
    )
    if "were not found in the cache" in output:
        print(f"Cache incomplete at {target}; trying the next built master commit.")
        return False
    command([*lake, "build", "--no-build", "--rehash", "Mathlib"], cwd=checkout, env=env)
    print(f"Full Mathlib cache verified at {target}.")
    return True


def release_tags(remote=f"https://github.com/{MATHLIB}", cwd=None):
    """{commit: [release names]} for Mathlib's Lean release tags, peeled to commits.

    Read with `git ls-remote`, which costs no GitHub API quota. Outside an explicit `cwd`
    it runs from the temporary directory, so a credential-bearing checkout's repository
    configuration never sends its token to Mathlib."""
    output = command(
        ["git", "ls-remote", "--tags", remote, "refs/tags/v*"], cwd=cwd or tempfile.gettempdir()
    )
    direct, peeled = {}, {}
    for line in output.splitlines():
        commit, ref = line.split("\t")
        name = ref.removeprefix("refs/tags/")
        found = peeled if name.endswith("^{}") else direct
        name = name.removesuffix("^{}")
        if RELEASE_RE.match(name):
            found[name] = sha(commit)
    by_commit = {}
    for name, commit in sorted((direct | peeled).items()):
        by_commit.setdefault(commit, []).append(name)
    return by_commit


def toolchain_entries(checkout, current):
    """{commit: [release]} for first-parent master commits after `current` that move
    `lean-toolchain` to a Lean release. Only the oldest entry per release counts."""
    commits = command(
        ["git", "log", "--first-parent", "--reverse", "--format=%H",
         f"{sha(current)}..origin/master", "--", "lean-toolchain"],
        cwd=checkout,
    ).split()
    entries, seen = {}, set()
    for commit in commits:
        toolchain = command(["git", "show", f"{sha(commit)}:lean-toolchain"], cwd=checkout).strip()
        release = toolchain.removeprefix(TOOLCHAIN_PREFIX)
        if toolchain.startswith(TOOLCHAIN_PREFIX) and RELEASE_RE.match(release) and release not in seen:
            seen.add(release)
            entries[commit] = [release]
    return entries


def declined(commit):
    """Whether a bump PR to this exact target was closed without merging."""
    prs = api(f"repos/{REPO}/pulls?state=closed&head=TauCetiProject:bump-mathlib/{sha(commit)}&per_page=100")
    return any(not pr.get("merged_at") for pr in prs)


def choose(runs, newer, releases, verify, age, refused=lambda commit: False):
    """The run to target, or None to wait. `newer` is newest first.

    Release commits in `newer` are visited oldest first. The bump stops at the oldest one
    if it verifies. Otherwise it waits, unless the release is older than the grace period,
    in which case it is stepped over and the next one considered. A release whose bump was
    `refused` (closed without merging) is stepped over at once."""
    verified = {}

    def first_verified(window):
        for run in ordered_runs(runs, window):
            target = run["head_sha"]
            if target not in verified:
                verified[target] = verify(target)
            if verified[target]:
                return run
        return None

    for index in reversed(range(len(newer))):
        commit = newer[index]
        if commit not in releases:
            continue
        names = ", ".join(releases[commit])
        if refused(commit):
            print(f"Stepping over Mathlib release {names} ({commit}): its bump PR was closed unmerged.")
            continue
        run = first_verified([commit])
        if run:
            print(f"Stopping at Mathlib release {names} ({commit}).")
            return run
        if age(commit) < RELEASE_GRACE_SECONDS:
            print(f"Waiting for Mathlib release {names} ({commit}) to become selectable.")
            return None
        print(f"Stepping over Mathlib release {names} ({commit}): unselectable past the grace period.")
    return first_verified(newer)


def select_target(current):
    with tempfile.TemporaryDirectory(prefix="tauceti-bump-") as tmp:
        checkout = Path(tmp) / "mathlib"
        command(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                "--single-branch",
                "--branch",
                "master",
                f"https://github.com/{MATHLIB}",
                str(checkout),
            ]
        )
        history = command(
            ["git", "rev-list", "--first-parent", "origin/master"], cwd=checkout
        ).splitlines()
        if current not in history:
            raise RuntimeError("Current pin is not on canonical Mathlib master history")
        newer = history[: history.index(current)]
        if not newer:
            return None
        runs = []
        for page in range(1, 101):
            batch = api(
                f"repos/{MATHLIB}/actions/workflows/build.yml/runs"
                f"?branch=master&event=push&status=success&per_page=100&page={page}"
            )["workflow_runs"]
            runs.extend(batch)
            # Walk actual Git ancestry, not completion timestamps. Before probing a
            # candidate we must know no newer successful run lives on a later page.
            if len(batch) < 100 or any(
                r["head_sha"] == current or r["head_sha"] in history[len(newer) :] for r in batch
            ):
                break
        else:
            raise RuntimeError("Upstream workflow history was truncated")
        releases = toolchain_entries(checkout, current)
        for commit, names in release_tags(cwd=checkout).items():
            if commit in newer:
                releases[commit] = sorted(set(releases.get(commit, [])) | set(names))
        for commit in releases:
            # Ask for each release commit's runs directly: the paginated listing above is
            # capped upstream and can miss an old release on a busy master.
            runs.extend(
                api(
                    f"repos/{MATHLIB}/actions/workflows/build.yml/runs"
                    f"?head_sha={commit}&branch=master&event=push&status=success&per_page=100"
                )["workflow_runs"]
            )

        def age(commit):
            stamp = command(["git", "show", "-s", "--format=%ct", sha(commit)], cwd=checkout)
            return time.time() - int(stamp)

        return choose(
            runs, newer, releases, lambda target: verify_cache(checkout, target), age, declined
        )


def derive_manifest(base, upstream, target):
    result = copy.deepcopy(base)
    mathlib = copy.deepcopy(next(p for p in base["packages"] if p["name"] == "mathlib"))
    mathlib["rev"] = sha(target)
    result["version"] = upstream["version"]
    result["packages"] = [mathlib] + [dict(p, inherited=True) for p in upstream["packages"]]
    issues = problems(result, upstream, base)
    if issues:
        raise ValueError(issues[0])
    return result


def head_target(pr):
    data = api(f"repos/{REPO}/contents/lake-manifest.json?ref={sha(pr['head']['sha'])}")
    return pin(json.loads(base64.b64decode(data["content"])))


def target_age_hours(commit):
    """Age of an upstream Mathlib commit by committer date, as `detect_stale_pin` measures."""
    stamp = api(f"repos/{MATHLIB}/git/commits/{sha(commit)}")["committer"]["date"]
    committed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if committed.tzinfo is None:
        raise ValueError("Mathlib commit timestamp has no timezone")
    return (time.time() - committed.timestamp()) / 3600


def queue_waiting():
    """PR numbers approved and waiting for insertion in the merge queue."""
    request = urllib.request.Request(
        QUEUE_STATUS, headers={"Cache-Control": "no-cache", "User-Agent": "TauCetiBump/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        status = json.loads(response.read())
    inputs = status["waiting"]["inputs"]
    if not isinstance(inputs, list) or not all(
        isinstance(i, dict) and type(i.get("number")) is int for i in inputs
    ):
        raise ValueError("unexpected waiting.inputs shape")
    return {i["number"] for i in inputs}


def hold_reason(pr, *, queue=True):
    """Why this bump must not be retargeted or closed now, or None. Fails closed."""
    labels = {label["name"].lower() for label in pr.get("labels", [])}
    if pr.get("draft"):
        return "it is a draft"
    if labels & KEEP_LABELS:
        return f"it carries {', '.join(sorted(labels & KEEP_LABELS))}"
    if QUEUE_INSERTED_LABEL in labels:
        return "the merge queue has inserted it into a batch"
    if not queue:
        return None
    try:
        waiting = queue_waiting()
    except Exception as error:  # any unreadable or malformed status fails closed
        return f"the merge queue status is unreadable ({error})"
    if pr["number"] in waiting:
        return "it is waiting in the merge queue"
    return None


def retargetable(number):
    """(fresh PR, its target) when this open bump is stale and free to retarget, else None."""
    pr = api(f"repos/{REPO}/pulls/{int(number)}")
    if pr["state"] != "open" or not pr["head"]["ref"].startswith("bump-mathlib/"):
        return None
    reason = hold_reason(pr, queue=False)
    if reason:
        print(f"Preserving active bump #{number}: {reason}.")
        return None
    old = head_target(pr)
    age = target_age_hours(old)
    if age < BUMP_RETARGET_HOURS:
        print(f"Preserving active bump #{number}: its target {old} is {age:.0f}h old.")
        return None
    reason = hold_reason(pr)
    if reason:
        print(f"Not retargeting stale bump #{number}: {reason}.")
        return None
    print(f"Active bump #{number} targets {old}, {age:.0f}h old; it may be retargeted.")
    return pr, old


def continuations(prs):
    """[(old number, new PR)] for open retargeted bumps whose predecessor is still open."""
    numbers = {pr["number"] for pr in prs}
    pairs = []
    for pr in prs:
        found = RETARGET_MARKER.search(pr.get("body") or "")
        if found and int(found.group(1)) in numbers - {pr["number"]}:
            pairs.append((int(found.group(1)), pr))
    return pairs


def close_predecessor(number, new):
    """Close a retargeted bump once its successor carries every one of its commits."""
    old = api(f"repos/{REPO}/pulls/{int(number)}")
    if old["state"] != "open":
        return
    reason = hold_reason(old)
    if reason:
        print(f"Leaving retargeted bump #{number} open: {reason}.")
        return
    carried = api(
        f"repos/{REPO}/compare/{sha(old['head']['sha'])}...{sha(new['head']['sha'])}?per_page=1"
    )["status"]
    if carried not in {"ahead", "identical"}:
        print(f"Leaving #{number} open: #{new['number']} does not contain its head commit.")
        return
    command(
        [
            "gh",
            "pr",
            "close",
            str(number),
            "--repo",
            REPO,
            "--comment",
            f"Retargeted to #{new['number']}; the adaptation commits carry over.",
        ]
    )


def verified_run(target, run_id):
    run = api(f"repos/{MATHLIB}/actions/runs/{int(run_id)}")
    if ordered_runs([run], [sha(target)]) != [run]:
        raise ValueError("Target does not match a successful canonical master push run")
    return run


def open_bump(start, base, target, run, continues=None):
    """Commit `target`'s manifest and toolchain on top of `start`, push, and open the PR.

    `base` holds the trusted files the manifest is derived from and checked against:
    the main checkout for a fresh bump, or the old bump's head when retargeting."""
    branch = f"bump-mathlib/{target}"
    releases = release_tags().get(target, [])
    subject = f"chore: bump Mathlib to {' / '.join(releases)} ({target[:8]})" if releases else (
        f"chore: bump Mathlib to {target[:8]}"
    )
    release_note = (
        f"This target is Mathlib's {' / '.join(releases)} release commit, so the first TauCeti "
        "commit on this toolchain pins the same Mathlib revision as Mathlib's release tag.\n\n"
        if releases
        else ""
    )
    continuation = (
        f"This bump continues #{continues[0]} (Mathlib {continues[1][:8]}) and carries its "
        "source repairs: its branch starts from that PR's head.\n\n"
        f"<!-- tauceti-retarget-from: #{continues[0]} -->\n\n"
        if continues
        else ""
    )
    upstream = json.loads(upstream_file(target, "lake-manifest.json"))
    manifest = derive_manifest(json.loads((base / "lake-manifest.json").read_text()), upstream, target)
    toolchain = upstream_file(target, "lean-toolchain")
    with tempfile.TemporaryDirectory(prefix="tauceti-publish-") as tmp:
        candidate = Path(tmp) / "candidate"
        command(["git", "worktree", "add", "--detach", str(candidate), start])
        try:
            (candidate / "lake-manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
            (candidate / "lean-toolchain").write_text(toolchain)
            command(
                [
                    "bash",
                    str(Path("scripts/check-bump.sh").resolve()),
                    str(base),
                    str(base),
                    str(candidate),
                ]
            )
            command(["git", "add", "lake-manifest.json", "lean-toolchain"], cwd=candidate)
            git_env = dict(
                os.environ,
                GIT_AUTHOR_NAME=BOT,
                GIT_COMMITTER_NAME=BOT,
                GIT_AUTHOR_EMAIL=BOT_EMAIL,
                GIT_COMMITTER_EMAIL=BOT_EMAIL,
            )
            command(
                ["git", "commit", "-m", subject],
                cwd=candidate,
                env=git_env,
            )
            remote = command(["git", "ls-remote", "--refs", "origin", f"refs/heads/{branch}"])
            if not remote:
                command(
                    [
                        "git",
                        "push",
                        f"--force-with-lease=refs/heads/{branch}:",
                        "origin",
                        f"HEAD:refs/heads/{branch}",
                    ],
                    cwd=candidate,
                )
            else:
                # Creation succeeded before a previous run was interrupted. Never
                # replace that branch: it may already contain source repairs.
                data = api(f"repos/{REPO}/contents/lake-manifest.json?ref={branch}")
                if pin(json.loads(base64.b64decode(data["content"]))) != target:
                    raise ValueError("Existing bump branch has a different dependency target")
            body = Path(tmp) / "body.md"
            body.write_text(
                f"Bump Mathlib to [{target[:8]}](https://github.com/{MATHLIB}/commit/{target}), "
                f"with its matching Lean toolchain and dependency manifest.\n\n"
                f"The full canonical Mathlib cache was verified before selecting this target. "
                f"Upstream build: {run['html_url']}.\n\n"
                f"{release_note}"
                f"{continuation}"
                "Workers repair TauCeti against this fixed target and may checkpoint partial "
                "source repairs. Full CI, audits, and current-head review are required to merge.\n\n"
                "Roadmap: none\n"
            )
            command(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    REPO,
                    "--base",
                    "main",
                    "--head",
                    branch,
                    "--title",
                    subject,
                    "--body-file",
                    str(body),
                ]
            )
        finally:
            command(["git", "worktree", "remove", "--force", str(candidate)])


def retarget(number, target, run_id):
    """Move a stale active bump to `target`, keeping its repairs. Every check is repeated
    here with fresh reads, since the selection job's view may be an hour old."""
    found = retargetable(number)
    if not found:
        return
    pr, old = found
    if relationship(old, target) != "ahead":
        print(f"Not retargeting #{number}: {target} is not ahead of its target {old}.")
        return
    run = verified_run(target, run_id)
    branch = f"bump-mathlib/{target}"
    previous = api(f"repos/{REPO}/pulls?state=all&head=TauCetiProject:{branch}&per_page=100")
    if previous:
        live = [p for p in previous if p["state"] == "open"]
        if live:
            close_predecessor(number, live[0])
        else:
            print(f"Leaving existing PR #{previous[0]['number']} in its current state.")
        return
    start = sha(pr["head"]["sha"])
    command(["git", "fetch", "--no-tags", "origin", f"refs/heads/{pr['head']['ref']}"])
    if command(["git", "rev-parse", "FETCH_HEAD"]) != start:
        print(f"Not retargeting #{number}: its head moved while this run read it.")
        return
    with tempfile.TemporaryDirectory(prefix="tauceti-retarget-") as tmp:
        base = Path(tmp) / "base"
        command(["git", "worktree", "add", "--detach", str(base), start])
        try:
            open_bump(start, base, target, run, continues=(number, old))
        finally:
            command(["git", "worktree", "remove", "--force", str(base)])
    created = api(f"repos/{REPO}/pulls?state=open&head=TauCetiProject:{branch}&per_page=100")
    if created:
        close_predecessor(number, created[0])


def publish(target, run_id):
    base_sha = command(["git", "rev-parse", "HEAD"])
    current = pin(json.loads(Path("lake-manifest.json").read_text()))
    active, obsolete = bump_state(current, open_bumps())
    pending = continuations(active)
    if pending:
        # A retarget opened its new PR but did not close the old one.
        for number, new in pending:
            close_predecessor(number, new)
        return
    if active:
        if target and len(active) == 1:
            retarget(active[0]["number"], target, run_id)
        else:
            print(f"Preserving active bump #{active[0]['number']} and its fixed target.")
        return
    for pr in obsolete:
        fresh = api(f"repos/{REPO}/pulls/{pr['number']}")
        if fresh["state"] != "open":
            continue
        if bump_state(current, [fresh])[0]:
            print(f"Preserving updated or held bump #{pr['number']}.")
            return
        command(
            [
                "gh",
                "pr",
                "close",
                str(pr["number"]),
                "--repo",
                REPO,
                "--comment",
                "Closing this obsolete bump: main already pins the same or a newer Mathlib commit.",
            ]
        )
    if not target or relationship(current, target) != "ahead":
        return
    run = verified_run(target, run_id)
    branch = f"bump-mathlib/{target}"
    previous = api(f"repos/{REPO}/pulls?state=all&head=TauCetiProject:{branch}&per_page=100")
    if previous:
        print(f"Leaving existing PR #{previous[0]['number']} in its current state.")
        return
    open_bump(base_sha, Path.cwd(), target, run)


def selection(current, active, obsolete):
    """(selected run or None, reconcile) for the credential-free selection job."""
    pending = continuations(active)
    if pending:
        print(f"Retargeted bumps still to close: {[number for number, _ in pending]}.")
        return None, True
    if not active:
        selected = select_target(current)
        return selected, bool(selected or obsolete)
    stale = retargetable(active[0]["number"]) if len(active) == 1 else None
    if not stale:
        if len(active) > 1:
            print(f"Preserving active bumps {[p['number'] for p in active]}.")
        return None, False
    # Select after the old target, not main's pin: a stale bump stuck on a release commit would
    # otherwise select that same release again, and release stops behind it are already passed.
    selected = select_target(stale[1])
    if selected and relationship(stale[1], selected["head_sha"]) != "ahead":
        print(f"Not retargeting: {selected['head_sha']} is not ahead of {stale[1]}.")
        selected = None
    return selected, bool(selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--select",
        action="store_true",
        help="Read-only selection for the credential-free workflow job",
    )
    parser.add_argument(
        "--publish", metavar="SHA", help="Publish the target verified by the selection job"
    )
    parser.add_argument("--run-id", type=int)
    args = parser.parse_args()
    if args.publish is not None:
        if (
            args.dry_run
            or args.select
            or args.run_id is None
            or (args.publish and args.run_id <= 0)
        ):
            parser.error("--publish requires --run-id and cannot be combined with selection")
        publish(sha(args.publish) if args.publish else "", args.run_id)
        return
    current = pin(json.loads(Path("lake-manifest.json").read_text()))
    active, obsolete = bump_state(current, open_bumps())
    selected, reconcile = selection(current, active, obsolete)
    target = selected["head_sha"] if selected else ""
    run_id = selected["id"] if selected else 0
    print(
        f"Current pin: {current}; target: {target or 'none'}; "
        f"active bumps: {[p['number'] for p in active]}; obsolete: {[p['number'] for p in obsolete]}"
    )
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            out.write(f"target={target}\nrun_id={run_id}\nreconcile={str(reconcile).lower()}\n")
    if not args.dry_run and not args.select:
        publish(target, run_id)


if __name__ == "__main__":
    main()
