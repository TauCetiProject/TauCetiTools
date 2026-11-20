# PR status and merge readiness

GitHub lifecycle labels and the pipeline-health report use
[`readiness.py`](readiness.py), which calls the **TauCetiReview merge gate**
that the project's checkout-merge-policy action checks out (TauCeti reads main). The label is a presentation of that decision, not an
independent approximation based on a green build and a scoreboard heading.

The adapter reads all comments and current commit statuses through GraphQL, fetches
the diff for otherwise approved PRs, and checks
that the PR's head, base, lifecycle and hold state have not changed before
reporting readiness. The gate selects the newest completed current-head review,
requires every configured rubric, handles legacy scoreboard tables,
and enforces build, scope, allowed paths and pin-validation rules.
Drafts, held PRs and stacked bases are reported separately.

| Label | Meaning |
| --- | --- |
| `awaiting-CI` | A required check is missing or pending |
| `ci-failed` | The build failed |
| `merge-check-failed` | A scope or pin-validation check failed |
| `awaiting-review` | Current-head reviews are absent or incomplete |
| `awaiting-author` | A current review requests changes or the PR conflicts |
| `needs-human-review` | Approved changes include human-owned files |
| `awaiting-dependency` | The PR targets a stacked branch rather than main |
| `on-hold` | The PR is a draft or has an explicit hold label |
| `ready-to-merge` | All per-PR automated merge prerequisites pass |

`on-hold` is a generated status. Use an explicit `hold` or `keep` label to hold a
PR; setting `on-hold` manually does not hold it. Failed scope/pin checks do not
receive `ci-failed`, preserving the existing failed-build housekeeping rules.

Queue membership and a Mathlib reservation **do not change** `ready-to-merge`.
The health report separately shows verified eligible PRs, which are queued,
which are not, and any reservation holder. It rechecks PRs instead of trusting
labels, reports label disagreements, and preserves failed reads as unknown.
Historical stage rates still describe label transitions; current verified depths
and recorded label depths are separate fields. A snapshot without a readiness
audit cannot establish a merge bottleneck from its ready-label count.

[`labels.py`](labels.py) is the sole label writer. It sets one lifecycle label
for an open PR and removes them on close. `pr-status.yml` handles PR changes,
review-comment creation/edit/deletion, and builds (see below). `pr-labels.yml`'s
scheduled sweep reconciles all open PRs, including stale ready labels. Dispatch it
with `pr=all` for a policy migration/backfill or with a PR number for a single
reconciliation.
The backfill updates label descriptions once, continues after individual errors,
and reports failures. A rate limit stops the remaining pass visibly.

PR-status batches checkpoint pending PR numbers and retry counts after each completed
PR. The workflow restores the latest verified `pr-status-progress` artifact, including
from failed runs, and uploads progress with `always()`. A retained quota deadline
defers the next batch before any App reads. Finishing a resumed scan advances its
watermark only to a completely read discovery boundary. Resuming also discovers
new activity, skipping only updates older than the start of a finished PR check;
changes during that check remain eligible. Build and review completions are discovered even without a PR edit. An hourly
recovery trigger and manual dispatch can resume work without a new PR event.

With `--prioritize-self-tested`, a paginated GraphQL inventory finds labelled heads
whose PR-specific acceptance status is missing before any workflow-history scan.
The normal pinned assessment still checks the live label, author membership,
changed paths and current head. Each batch assesses at most 100 priority requests;
untouched requests precede retries so a failing PR cannot monopolize the queue.
Unfinished assertions are checkpointed with their head, first observation and last
attempt. The job summary reports the count and oldest observed waiting age. This
age is a lower bound on request latency, not the PR's update age. A failed read or
quota hold reports retained, unverified inventory rather than a zero backlog.

`--budget-seconds` stops new assessments before the workflow's token expires;
the existing checkpoint retains the remaining work. Scheduled sweeps use
`--all-open`, which resumes their complete PR inventory without rereading workflow
history or restarting the completed prefix. A resumed sweep finishes its captured
inventory; the next fresh sweep also includes PRs opened in the meantime.

Restoration verifies the producing workflow and event through GitHub. A
`pull_request_target` run executes trusted base code even when its recorded head
repository is a fork; ordinary `pull_request` runs cannot supply progress. Other
accepted events (`workflow_run`, `issue_comment`, schedule and manual dispatch)
must name upstream `main`.
The former retry-only artifact is migrated at deployment. Missing/expired state
uses the existing bounded time-window fallback. Invalid state warns and rebuilds
from that fallback, then writes a replacement checkpoint rather than repeatedly
restoring the corrupt one. Lost state or a hard runner loss before upload can
repeat work since the last uploaded checkpoint.

The workflows check out the policy through `.github/actions/checkout-merge-policy`,
without persisting credentials; TauCeti's action checks out TauCetiReview main. The
workflow-pin tests require labels and the health report to read the policy through
that action. For local commands, check out the same TauCetiReview revision and set:

```sh
export TAUCETI_REVIEW_RUNNER=/path/to/TauCetiReview/runner
python3 scripts/pr_status/labels.py reconcile 123
python3 scripts/pipeline_health.py --data snapshot.json --verify-readiness
```

Without `--verify-readiness`, `--data` remains an offline replay and uses the
readiness audit stored in the snapshot, if present. A live health report always
performs the audit. Use `--dump-data` to save the evidence for offline replay.

[`core.py`](core.py) supplies GitHub-reading helpers and the separate build/review
signals used by lifecycle labels and automation-health checks. These signals
do not by themselves establish all merge prerequisites. Destructive housekeeping continues to
use its stricter OWNER/MEMBER/COLLABORATOR comment policy.

## Merge conflicts

A PR becomes conflicted because **its base moved**, not because its author did
anything, which makes it the one transition nothing else here can see. Every other
trigger is scoped to the PR (`pull_request_target`, a `pr-build` / `Review`
`workflow_run`, an `issue_comment`), and the base moving fires none of them.
Before [`merge-conflicts.yml`](../../.github/workflows/merge-conflicts.yml) a PR
could pick up a conflict and *nothing anywhere said so* — no label, no comment, no
alert. `housekeeping.py` only retires PRs that are blocking under review, so a
conflicted-but-approved PR was reaped by nothing either. It rotted silently.

[`conflicts.py`](conflicts.py) runs every fifteen minutes: one GraphQL query reads
the whole open queue, `merge-conflict` goes on a PR that has stopped merging and
comes off when it merges again, and the author is told once per episode. The label
is provisioned on first use, like the status labels.

Two properties are why this is a couple of hundred lines rather than a package.

**The label is orthogonal to the status labels.** It is deliberately *not* part of
the mutually-exclusive set [`labels.py`](labels.py) maintains, and nothing else
reads or writes it. A PR can be awaiting review *and* conflicting, and saying both
is more useful than having one hide the other; keeping them separate also means
conflict state remains separate from the lifecycle label, never has to win a precedence argument
against CI or review state, and cannot interfere with what a review harness reads.

**The label is also the state.** Whether an episode is open is just "is the label
on the PR", so there is no marker to parse and no way to fail to recognise our own
bookkeeping — a wrong label self-heals on the next run. It also makes the problem
measurable without writing anything extra, since GitHub timestamps label changes:

```bash
gh api --paginate "/repos/TauCetiProject/TauCeti/issues/N/timeline?per_page=100" \
  --jq '.[] | select(.label.name == "merge-conflict") | "\(.event) \(.created_at) \(.actor.login)"'
```

Read those as **observed label intervals**, not exact conflict durations: the
boundaries are quantised by the fifteen-minute poll and by GitHub's scheduling, a
conflict that arises and clears between two runs is never seen at all, and a PR
closed while labelled has no closing event. The actor distinguishes the bot's own
transitions from a human's.

Two ordering decisions carry the weight:

- **The comment is posted before the label.** The label is what suppresses a
  repeat, so writing it first would mean a comment that then failed was never
  retried — the notice lost silently, which is the one failure this must not have.
  This way the risk is a duplicate comment instead, which is merely annoying.
- **UNKNOWN is skipped per PR, never per run.** GitHub computes `mergeable`
  lazily, so a read just after the base moved answers UNKNOWN and only schedules
  the merge. Those are re-read a few times; whatever stays unknown is left exactly
  as it is, neither labelled nor cleared, while every PR whose state we do know is
  still processed. This is why we do not use
  [`eps1lon/actions-label-merge-conflict`](https://github.com/eps1lon/actions-label-merge-conflict),
  which mathlib4 uses for the same job: on UNKNOWN it returns from the middle of
  its loop, and once its retries are spent returns an empty result, abandoning
  that page of PRs and every later one. A PR whose head has diverged from its
  branch tip sits at `mergeable: null` indefinitely, so one such PR could stop
  every other conflict being labelled.

A PR carrying a hold label (`keep`/`hold`/`wip`/`human`/`do-not-close`/`blocked`)
is left entirely alone rather than labelled-but-silent:
the label means "the author has been told", so labelling without commenting would
leave the conflict silent for good once the hold came off. Only open PRs are read,
so a PR closed while labelled keeps the label — accurate, but it means that
episode has no closing event in the timeline.

Note that labelling and commenting both bump the PR's `updatedAt`, which
`housekeeping.py` treats as freshness — so a conflict episode resets that PR's
seven-day stale-close clock. That is the intended trade: a PR that just learned it
conflicts should get its week to act on it.

## Zulip setup

[`zulip.py`](zulip.py) is the Zulip client that `toolchain_tags.py` uses to post to
its own topic (set via `ZULIP_TOPIC`). It needs a bot and two secrets:

1. **Create a dedicated Zulip bot** (Zulip → Settings → Bots → Add a new bot,
   type *Generic*). Subscribe it to the **Tau Ceti** channel: a bot can only
   post in channels it belongs to.
2. **Add repository secrets** on `TauCetiProject/TauCeti`:
   - `ZULIP_API_KEY`: the bot's API key
   - `ZULIP_EMAIL`: the bot's email (e.g. `tauceti-alert-bot@leanprover.zulipchat.com`)

   The site is hard-coded to `https://leanprover.zulipchat.com` in the workflows.

   > **Set the key without a trailing newline.** A newline (or stray
   > whitespace) rides into the Basic-auth header and Zulip rejects the key as
   > `Malformed API key` (a 401). Use `--body`, which does not append one:
   >
   > ```bash
   > gh secret set ZULIP_API_KEY --repo TauCetiProject/TauCeti --body "$KEY"
   > ```
   >
   > Avoid `echo "$KEY" | gh secret set ...` (echo adds a newline). The script
   > also `.strip()`s both creds defensively, but set them cleanly anyway.

Label reconciliation needs no Zulip credentials: `pr-status.yml` and `pr-labels.yml` use the same GitHub App
(`APP_ID` / `APP_PRIVATE_KEY`) already configured for the roadmap and merge
workflows, scoped to this repo, and provisions lifecycle labels on first use.

## Label backfill

To reconcile GitHub labels locally with an authenticated `gh`:

```bash
python3 scripts/pr_status/labels.py reconcile-all
```

`pr-labels.yml` also supports a manual dispatch for one PR or `pr=all`.

## Unit tests

```bash
cd scripts/pr_status
python3 -m unittest test_pr_labels test_readiness test_zulip
```

`test_pr_labels` covers the derivation (`core.review_state`) and label reconciliation. `test_readiness`
runs the merge gate on current/stale/incomplete reviews, ignored old markers,
human paths, CI and pin guards, and head-move races. It needs the engine
checkout described above. `test_zulip` covers Zulip transport retry and duplicate-send prevention.
GitHub and Zulip reads and writes are stubbed in these tests.

GitHub reads require `gh` and `jq`. The read helper captures response headers,
honors `Retry-After` and exhausted-quota reset times, and applies jq filters
locally after decoding JSON response boundaries. Paginated bodies never include
HTTP headers. If the requested wait exceeds the remaining 180-second sleep
budget, it stops immediately and holds later reads until that deadline.
