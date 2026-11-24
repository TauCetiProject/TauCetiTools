# Main-integration builds

The queue controller (TauCetiWorker, `tauceti_queue/main_integration.py`) tests queued but
unsealed batch members against current `main` while PR CI is idle, so a member that main has
broken since its base leaves the batch before it seals (TauCeti decision 175 B). It does not push
anything: the PR head, its reviews and its approval stay as they are.

## Dispatch

The controller sends a `repository_dispatch` of type `tauceti-main-integration` to the project:

```json
{"event_type": "tauceti-main-integration",
 "client_payload": {"pr": 123, "head_sha": "<H>", "main_sha": "<M>"}}
```

The project's `.github/workflows/main-integration.yml` (name `main-integration`) calls this
repository's `pr-build.yml@main` with the same routing inputs as its `pr-build.yml`. Because it is
a separate workflow, nothing that matches `pr-build` runs or titles (status retries, Fleet
recovery, `ci_recovery.py`, `pr-ci-cleanup.yml`, review and PR status) sees these runs. The caller
is inert until the repository variable `TAUCETI_MAIN_INTEGRATION_ENABLED` is `true`.

## What the run does

- **Placement.** The picker's `main-integration` class (`fleet_runner.main_integration_route`)
  selects the Fleet PR class only when at least `MAIN_INTEGRATION_MIN_IDLE` (repository variable,
  default 8) online PR runners are idle and a complete queue observation shows no Fleet PR job
  waiting. It never chooses GitHub-hosted or Namespace capacity. Otherwise it posts `main-integration` = `error`
  "deferred: Fleet PR pool busy" on H and builds nothing.
- **Build.** H is read from this repository (the queue retains accepted heads under
  `refs/tauceti-queue/inputs/...`) and integrated onto exactly M, then built, audited and linted
  like an ordinary PR build. The concurrency group is per H and M and never cancels, or is
  cancelled by, the PR's own build.
- **Report.** One commit status on H, context `main-integration`, description beginning
  `B=<M prefix>`:
  - `success`: the integration passed (`B=<M> T=<tested commit> passed ...`);
  - `failure`: the PR's source fails on that main, by the same evidence staging's source verdict
    accepts: a sandboxed build, audit or lint phase failed, a completed pre-build source check
    rejected the sources, or a shim's concrete Mathlib replacement exists. The queue confirms a
    failure with a second run before acting on it;
  - `error`: no verdict (deferred, a textual conflict, infrastructure, a scope or configuration
    guard that stopped the build). The queue does not act on it.

  The run posts no `build`, `scope` or `bump-guard`, applies no label and publishes no cache. The
  status has no PR fence (`ci_status.py`), so it is posted for a `self-tested` PR and for a head
  the PR has since moved past.
