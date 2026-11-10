# Continuous PR CI recovery

The Actions sweep remains a fallback. Its nominal hourly schedule can be delayed
or dropped for hours, and its twenty-dispatch limit cannot drain a large backlog.
Run this systemd timer on the authoritative queue host hourly to recover missing
builds across all authors, without a PR-count cap. Systemd serializes passes. Ordinary workflow routing and runner capacity still apply. A pass stops before
consuming the last 1,500 requests of the shared installation API budget.

Install an immutable copy of this Tools checkout under
`~/TauCetiCIRecovery/releases/<revision>` and point `~/TauCetiCIRecovery/current`
at it. Copy the service and timer to `~/.config/systemd/user/`, reload the user
manager, and enable/start the timer. The wrapper requires the queue checkout and
its Python environment at `~/TauCetiQueue` (override its import path with
`TAUCETI_QUEUE_CHECKOUT`) and uses the queue host's existing GitHub App files.
Credentials are minted per pass and never written into the release or journal.

The shared sweeper checks immutable current PR heads, existing commit statuses,
all live build runs, recent dispatches and a final live observation before each
mutation. A missing event or a dead pending status may be recovered; a running
build, a completed build result, moved head, closed PR or draft is left alone.
Recovery dispatches the ordinary trusted default-branch build. It never approves
or merges a PR, and it never fabricates a successful status.

Inspect `journalctl --user -u tauceti-ci-recovery.service` and
`systemctl --user list-timers tauceti-ci-recovery.timer` for progress. The final
line records how many missing builds the pass dispatched. Keep the Actions
fallback, which shares live-run checks with the host process.
