#!/usr/bin/env python3
"""Start or inspect a supervised S2 migration (no GitHub queries).

Defaults to read-only preflight. Start requires deployed acceptance evidence, a
standby target and explicit environment selection. Cron owns crash recovery.
Receipts contain identities, checkpoints and checksums; never SQL rows or connection strings.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tauceti_tools.system import ensure_ssl_cert_file  # noqa: E402

ERROR_CODES = frozenset(
    [
        "invalid_generation",
        "migration_acceptance_missing",
        "migration_acceptance_stale",
        "migration_acceptance_incomplete",
        "migration_acceptance_changed",
        "migration_application_grants_missing",
        "migration_application_role_unsafe",
        "migration_capacity_restore_unverified",
        "migration_environment_mismatch",
        "migration_forward_recovery_required",
        "migration_freeze_lease_expired",
        "migration_health_mismatch",
        "migration_retention_not_pinned",
        "migration_shadow_mismatch",
        "migration_shadow_unavailable",
        "migration_shadow_queries_missing",
        "migration_source_fence_lost",
        "migration_target_configuration",
        "migration_target_data_mismatch",
        "migration_target_identity_mismatch",
        "migration_target_not_empty",
        "migration_target_owner_mismatch",
        "migration_target_schema_mismatch",
        "migration_target_unavailable",
        "migration_latency_regression",
        "migration_queue_dlq_unavailable",
        "migration_retention_policy_invalid",
        "migration_constraint_incompatible",
        "migration_shadow_request_budget",
        "migration_held_input_pending",
        "migration_cell_incompatible",
        "migration_step_limit_invalid",
        "migration_rowid_sequence_exhausted",
        "invalid_target_identity",
        "migration_apply_required",
        "migration_blob_missing",
        "migration_bookmark_unavailable",
        "migration_checkpoint_conflict",
        "migration_checkpoint_unavailable",
        "migration_checksum_mismatch",
        "migration_control_corrupt",
        "migration_deployment_identity_required",
        "migration_drain_pending",
        "migration_dispatch_drain_unavailable",
        "migration_frozen",
        "migration_generation_abandoned",
        "migration_generation_used",
        "migration_identity_conflict",
        "migration_job_checkpoint_invalid",
        "migration_not_started",
        "migration_page_corrupt",
        "migration_page_missing",
        "migration_production_prerequisites_unavailable",
        "migration_reference_mismatch",
        "migration_revision_changed",
        "migration_schema_changed",
        "migration_schema_incompatible",
        "migration_source_count_changed",
        "migration_source_identity_changed",
        "migration_source_mismatch",
        "migration_staging_blob_missing",
        "migration_storage_failure",
        "migration_table_manifest_mismatch",
        "migration_column_manifest_mismatch",
        "migration_observation_not_ready",
        "migration_receipt_conflict",
        "migration_staging_reset_mismatch",
        "migration_target_backend_unavailable",
        "repository_unconfigured",
        "repository_not_allowed",
        "unauthorized",
    ]
)


def service(environment):
    suffix = "-test" if environment == "test" else ""
    return "https://github-state" + suffix + ".taucetiproject.org", Path.home() / (
        ".config/tauceti-github-state" + suffix
    )


def call(environment, body, *, service_url=None, credentials_dir=None):
    url, directory = service(environment)
    url = service_url or url
    directory = credentials_dir or directory
    request = urllib.request.Request(
        url + "/admin/migration",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + (directory / "admin-token").read_text().strip(),
            "Content-Type": "application/json",
            "User-Agent": "TauCetiWorker/1.0 (+https://github.com/TauCetiProject/TauCetiWorker)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Only fixed server reason codes can reach the terminal. Proxy errors,
        # payloads and credentials must never be echoed, even in JSON responses.
        reason = None
        try:
            detail = json.loads(error.read(4096))
            value = detail.get("error") if isinstance(detail, dict) else None
            if isinstance(value, str) and value in ERROR_CODES:
                reason = value
        except (ValueError, OSError):
            pass
        suffix = ": " + reason if reason else "; inspect authenticated status"
        raise SystemExit(f"migration request failed: HTTP {error.code}{suffix}") from None
    except (urllib.error.URLError, OSError):
        raise SystemExit("migration transport failed; retry the same generation") from None


def main():
    ensure_ssl_cert_file()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=["test", "production"], required=True)
    parser.add_argument("--service-url", help="override the service HTTPS origin")
    parser.add_argument("--credentials-dir", type=Path, help="directory containing the private admin-token")
    parser.add_argument("--repository", default="TauCetiProject/TauCeti")
    parser.add_argument("--generation", help="explicit UUID v4; reuse it after interruption")
    parser.add_argument("--target", help="opaque project/branch/database label; never a DSN")
    parser.add_argument("--apply", action="store_true", help="perform the selected operation (requires deployed acceptance)")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--retention-policy", type=Path, help="explicit post-observation retention policy JSON")
    parser.add_argument("--batch-steps", type=int, default=1, help="requested supervised steps per request (1..100; server caps each request at five steps, time and I/O)")
    parser.add_argument("--steps", type=int, default=1, help="bounded export/verification steps per invocation")
    parser.add_argument(
        "command", choices=["preflight", "status", "start", "freeze", "resume", "abandon", "finish-observation"], nargs="?", default="preflight"
    )
    args = parser.parse_args()
    options = {}
    if args.service_url:
        from urllib.parse import urlsplit

        parsed = urlsplit(args.service_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            parser.error("service URL must be HTTPS without credentials, a query or a fragment")
        options["service_url"] = args.service_url.rstrip("/")
    if args.credentials_dir:
        options["credentials_dir"] = args.credentials_dir.expanduser()
    if not 1 <= args.limit <= 250 or not 1 <= args.steps <= 10000 or not 1 <= args.batch_steps <= 100:
        parser.error("limit must be 1..250, steps 1..10000 and batch-steps 1..100")
    mutating = args.command in {"start", "freeze", "resume", "abandon", "finish-observation"} and args.apply
    if mutating and (not args.generation or not args.target):
        parser.error("--apply requires --generation and --target")
    body = {"repository": args.repository, "environment": args.environment}
    if args.generation:
        body["generation"] = args.generation
    if args.target:
        body["target"] = args.target
    if not mutating:
        body["operation"] = "status" if args.command == "status" else "preflight"
    else:
        body.update(
            operation={"start": "start", "freeze": "freeze", "resume": "step", "abandon": "abandon", "finish-observation": "finish-observation"}[args.command],
            generation=args.generation,
            target=args.target,
            apply=True,
            limit=args.limit,
            batch_steps=args.batch_steps,
        )
    if mutating and args.command == "finish-observation":
        if not args.retention_policy:
            parser.error("finish-observation requires an explicit --retention-policy")
        try:
            body["retention_policy"] = json.loads(args.retention_policy.read_text())
        except (OSError, ValueError):
            parser.error("cannot read retention policy JSON")
    for _ in range(args.steps if mutating and args.command == "resume" else 1):
        result = call(args.environment, body, **options)
        print(json.dumps(result, sort_keys=True), flush=True)
        if result.get("phase") in {"complete", "abandoned", "replaying"} or (result.get("phase") == "source_verified" and not result.get("cutover")):
            break


if __name__ == "__main__":
    main()
