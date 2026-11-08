"""Authenticated native Forgejo publications shared by Review and the queue.

Callers persist intents and allocate sequences in their existing durable ledger
before remote writes. This client owns no store, inference, or admission policy.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import quote

from .ci_candidate import RepositoryIdentity, read_json
from .forge import ForgeError, Incomplete, PrincipalRef
from .source_identity import SourceActor

META_RE = re.compile(r"<!--tauceti-meta:v1 (.*?)-->", re.S)


def comment_marker(marker):
    return "<!-- tauceti-queue " + marker + " -->"


BINDING = re.compile(r"<!--tauceti-source:v2 (.*?)-->", re.S)
PUBLICATION_SCHEMA = "tauceti-source-publication/v2"
INTENT_SCHEMA = "tauceti-source-publication-intent/v3"
PUB_DESCRIPTION = "tauceti-pub/v3:"
REVIEW_CONTEXT = "tauceti-review/"
RECOVERY_CONTEXT = "tauceti-recovery/"


class PublicationSuperseded(RuntimeError):
    """Another intent owns this sequence or a newer one; reconcile before resending."""

    def __init__(self, message, *, sequence=None, nonce=None, actor=None):
        super().__init__(message)
        self.sequence, self.nonce, self.actor = sequence, nonce, actor


class PublisherNotGranted(PermissionError):
    """Trusted publisher configuration refuses this operation; operator reconciliation is required."""


class PublicationDamaged(Incomplete):
    """Confirmed publication was moderated; prepare a new intent, never rerun inference."""

    def __init__(self, message, *, number=None, nonce=None, publication_kind=None):
        super().__init__(message)
        self.number, self.nonce, self.publication_kind = number, nonce, publication_kind
        self.head = None


def principal(actor):
    return f"{actor.principal.forge.key}/users/{actor.principal.native_id}"


@dataclass(frozen=True)
class NativePublishers:
    source: RepositoryIdentity
    reviews: frozenset[PrincipalRef]
    statuses: frozenset[PrincipalRef]
    recoveries: frozenset[PrincipalRef] = frozenset()
    generation: str = field(kw_only=True)

    def __post_init__(self):
        if not isinstance(self.source, RepositoryIdentity) or self.source.repository.forge.kind != "forgejo":
            raise ValueError("native publisher policy requires a qualified source")
        if (
            not isinstance(self.generation, str)
            or re.fullmatch(r"[0-9a-f]{32}", self.generation) is None
            or self.generation == "0" * 32
        ):
            raise ValueError("native publisher policy requires a fresh random authority generation")
        for name in ("reviews", "statuses", "recoveries"):
            values = getattr(self, name)
            if (
                type(values) is not frozenset
                or len(values) > 256
                or (name != "recoveries" and not values)
                or any(
                    not isinstance(p, PrincipalRef)
                    or p.forge != self.source.repository.forge
                    or re.fullmatch(r"[1-9][0-9]*", p.native_id) is None
                    for p in values
                )
            ):
                raise ValueError("native publishers require bounded source-local principal IDs")

    def record(self):
        return {
            "source": self.source.record(),
            "generation": self.generation,
            **{
                name: sorted(int(p.native_id) for p in getattr(self, name))
                for name in ("reviews", "statuses", "recoveries")
            },
        }


class NativePublicationPolicy:
    """Provenance configuration; domain review/admission rules stay with callers."""

    def __init__(self, publishers, *, source_revision):
        if not isinstance(publishers, NativePublishers):
            raise ValueError("native publication requires explicit publisher identities")
        if (
            not isinstance(source_revision, str)
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64}|sha256:[0-9a-f]{64})", source_revision) is None
        ):
            raise ValueError("native publication requires an immutable producer policy revision")
        self.publishers, self.source_revision = publishers, source_revision

    def binding(self, actor):
        if actor.principal.forge != self.publishers.source.repository.forge:
            raise ValueError("publication actor belongs to another source")
        return {
            "schema": PUBLICATION_SCHEMA,
            "source": self.publishers.source.record(),
            "issuer": principal(actor),
            "generation": self.publishers.generation,
            "policy_revision": self.source_revision,
        }

    @staticmethod
    def append_provenance(body, block):
        # Review's existing provenance block remains the final line, as its
        # readers require. Native source authority is additional provenance.
        metadata = list(META_RE.finditer(body))
        if metadata and not body[metadata[-1].end() :].strip():
            last = metadata[-1]
            return body[: last.start()].rstrip() + "\n\n" + block + "\n" + last.group(0) + "\n"
        return body.rstrip() + "\n\n" + block + "\n"

    def bind_comment(self, body, actor):
        # Model/user quotations are prose, never another authority block.
        body = body.replace("<!--tauceti-source:", "&lt;!--tauceti-source:")
        block = (
            "<!--tauceti-source:v2 " + json.dumps(self.binding(actor), sort_keys=True, separators=(",", ":")) + "-->"
        )
        return self.append_provenance(body, block)


class NativePublicationClient:
    """Publish already persisted decisions using append-only native receipts.

    ``store`` passed to configure_publisher_policy supplies transaction(),
    get_setting() and set_setting() over the caller's existing durable state.
    Source ACL reads for other actors require a repository-admin credential.
    """

    def __init__(self, identity, client, *, publication_policy):
        if not isinstance(identity, RepositoryIdentity) or identity.repository.forge.kind != "forgejo":
            raise ValueError("native publication requires a qualified Forgejo repository")
        if getattr(client, "forge", None) != identity.repository.forge:
            raise ValueError("native publication client belongs to another origin")
        if (
            not isinstance(publication_policy, NativePublicationPolicy)
            or publication_policy.publishers.source != identity
        ):
            raise ValueError("native publication policy belongs to another repository")
        self.repository_identity, self.repository, self.client = identity, identity.repository, client
        self._publication_policy = publication_policy
        self._publisher_configured = False
        self._configured_record = None

    @property
    def publication_policy(self):
        return self._publication_policy

    def _bound(self, repo):
        if repo not in (self.repository.name, self.repository.key):
            raise ValueError("native publication repository differs from its trusted binding")
        row = self.client.repository(self.repository)
        if (
            not isinstance(row, dict)
            or type(row.get("id")) is not int
            or row["id"] != self.repository_identity.native_id
            or not isinstance(row.get("full_name"), str)
            or row["full_name"].casefold() != self.repository.name.casefold()
        ):
            raise Incomplete("native publication repository identity changed")

    @staticmethod
    def _sha(value):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None or value == "0" * 40:
            raise ValueError("native publication requires a nonzero immutable commit")
        return value

    def configure_publisher_policy(self, store):
        """Fence trust changes in the existing queue settings, before serving work."""
        self._publisher_configured = False
        self._configured_record = None
        key = "state:native-publisher-policy:" + self.repository.key + ":" + str(self.repository_identity.native_id)
        current = self.publication_policy.publishers.record()
        with store.transaction():
            saved = store.get_setting(key)
            if saved is None:
                saved = {"current": current, "retired_generations": [], "recovery_grants": {}}
            if (
                not isinstance(saved, dict)
                or set(saved) != {"current", "retired_generations", "recovery_grants"}
                or not isinstance(saved["current"], dict)
                or not isinstance(saved["retired_generations"], list)
                or len(saved["retired_generations"]) > 4096
                or not isinstance(saved["recovery_grants"], dict)
                or len(saved["recovery_grants"]) > 4097
            ):
                raise ValueError("invalid native publisher configuration history")
            previous, retired = saved["current"], saved["retired_generations"]
            if (
                set(previous) != set(current)
                or previous.get("source") != current["source"]
                or not self._valid_generation(previous.get("generation"))
                or any(not self._valid_generation(g) for g in retired)
                or len(set(retired)) != len(retired)
                or previous["generation"] in retired
            ):
                raise ValueError("invalid native publisher configuration history")
            for role in ("reviews", "statuses", "recoveries"):
                ids = previous[role]
                if (
                    not isinstance(ids, list)
                    or len(ids) > 256
                    or (role != "recoveries" and not ids)
                    or any(type(i) is not int or i < 1 for i in ids)
                    or ids != sorted(set(ids))
                ):
                    raise ValueError("invalid native publisher configuration history")
            if previous != current:
                if current["generation"] == previous.get("generation") or current["generation"] in retired:
                    raise ValueError("publisher changes require a fresh, never reused authority generation")
                retired = [*retired, previous["generation"]]
                if len(retired) > 4096:
                    raise ValueError("native publisher configuration history exceeds its bound")
            # Preserve the persisted settings schema and operator inventory.
            # Historical grants are not approval authority: retired recovery
            # rows deny even when a restored store has lost that inventory.
            grants = saved["recovery_grants"]
            for record in (previous, current):
                grants[record["generation"]] = record["recoveries"]
            if len(grants) > 4097:
                raise ValueError("native recovery publisher history exceeds its bound")
            for generation, ids in grants.items():
                if (
                    not isinstance(generation, str)
                    or re.fullmatch(r"[0-9a-f]{32}", generation) is None
                    or not isinstance(ids, list)
                    or len(ids) > 256
                    or any(type(i) is not int or i < 1 for i in ids)
                ):
                    raise ValueError("invalid native recovery publisher history")
            store.set_setting(key, {"current": current, "retired_generations": retired, "recovery_grants": grants})
        # A transaction may fail while leaving its context (including ENOSPC).
        # Publish the fence only after the existing ledger has committed it.
        self._configured_record = current
        self._publisher_configured = True
        return previous != current

    @staticmethod
    def _valid_generation(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None and value != "0" * 32

    def _require_publisher_policy(self):
        if not self._publisher_configured or self.publication_policy.publishers.record() != self._configured_record:
            raise Incomplete("native publication requires the persisted current publisher-policy binding")

    @staticmethod
    def _positive(value):
        if type(value) is not int or value < 1:
            raise Incomplete("native record has no positive ID")
        return value

    def _permission(self, actor):
        row = self.client.request(
            "GET", f"repos/{self.repository.name}/collaborators/{quote(actor.login, safe='')}/permission"
        )
        if not isinstance(row, dict) or SourceActor.from_native(self.repository.forge, row.get("user")) != actor:
            raise Incomplete("publisher permission belongs to another native actor")
        permission = row.get("permission")
        if permission not in {"none", "read", "write", "admin", "owner"}:
            raise Incomplete("native actor permission is unavailable")
        return permission

    def _binding_matches(self, body, actor):
        bindings = BINDING.findall(body)
        if len(bindings) != 1:
            return False
        try:
            bound = read_json(bindings[0].encode(), max_bytes=16 * 1024)
        except ValueError:
            return False
        if not isinstance(bound, dict) or set(bound) != {"schema", "source", "issuer", "policy_revision", "generation"}:
            return False
        reported = bound["policy_revision"]
        return (
            bound["schema"] == PUBLICATION_SCHEMA
            and bound["source"] == self.repository_identity.record()
            and isinstance(bound["generation"], str)
            and bound["generation"] == self.publication_policy.publishers.generation
            and bound["issuer"] == principal(actor)
            and isinstance(reported, str)
            and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64}|sha256:[0-9a-f]{64})", reported) is not None
        )

    def _publication_latest(self, rows, context):
        """Select current publishers' semantic order before checking their live ACL.

        Unresolved issuers deny older registrations without resetting the highest
        trusted sequence. A fresh registration of that intent or a newer one can
        reconcile the barrier; no unverified sequence can grant or poison order.
        """
        self._require_publisher_policy()
        entries = []
        retired_recovery = False
        unknown_barrier = 0
        seen = set()
        generation = self.publication_policy.publishers.generation
        for row in rows:
            if not isinstance(row, dict):
                raise Incomplete("native statuses are incomplete")
            if row.get("context") != context:
                continue
            status_id = self._positive(row.get("id"))
            if status_id in seen:
                raise Incomplete("native publication statuses are ambiguous")
            seen.add(status_id)
            description = row.get("description") or ""
            try:
                actor = SourceActor.from_native(self.repository.forge, row.get("creator"))
            except Incomplete:
                actor = None
            if actor is None:
                # Missing/deleted issuers deny older facts, without letting an
                # unverified sequence poison the namespace forever. A fresh
                # trusted registration after this row can reconcile the barrier.
                if description.startswith("tauceti-pub/"):
                    unknown_barrier = max(unknown_barrier, status_id)
                continue
            record_generation = description.split(":", 2)[1:2]
            if (
                context.startswith(RECOVERY_CONTEXT)
                and description.startswith("tauceti-pub/")
                and record_generation
                and record_generation[0] != generation
            ):
                # A restored backup may have lost both a generation and its
                # publisher account. Unattributable retired facts can only deny,
                # until a fresh explicit current-authority decision reconciles
                # them. No old record can override current-generation evidence.
                retired_recovery = True
            allowed = (
                self.publication_policy.publishers.recoveries
                if context.startswith(RECOVERY_CONTEXT)
                else self.publication_policy.publishers.reviews
            )
            if actor.principal not in allowed:
                continue
            if not description.startswith(PUB_DESCRIPTION + str(generation) + ":"):
                if description.startswith("tauceti-pub/") and description.split(":", 2)[1:2] == [generation]:
                    raise Incomplete("unsupported native publication protocol for the current authority generation")
                continue  # Old authority generations and unsupported protocols grant nothing.
            parts = description[len(PUB_DESCRIPTION) :].split(":")
            if (
                len(parts) != 5
                or re.fullmatch(r"[1-9][0-9]*", parts[1]) is None
                or int(parts[1]) >= 2**63
                or re.fullmatch(r"[0-9a-f]{32}", parts[2]) is None
                or re.fullmatch(r"pending|clear-pending|clear|[1-9][0-9]*", parts[3]) is None
                or re.fullmatch(r"[0-9a-f]{64}", parts[4]) is None
            ):
                raise Incomplete("native publication receipt is malformed")
            entries.append((int(parts[1]), parts[2], parts[3], parts[4], actor, row))
        floor = max((r[0] for r in entries), default=0)
        floor_rows = [r for r in entries if r[0] == floor]
        if len({(r[1], r[3], principal(r[4])) for r in floor_rows}) > 1:
            raise Incomplete("native publication sequence names different intents")
        if unknown_barrier:
            entries = [r for r in entries if r[5]["id"] > unknown_barrier and r[0] >= floor]
            if not any(r[2] in {"pending", "clear-pending"} for r in entries):
                row = floor_rows[0] if floor_rows else None
                return (
                    unknown_barrier,
                    {
                        "_stage": "unverified",
                        "_sequence": floor,
                        "_nonce": row[1] if row else "",
                        "_digest": row[3] if row else "",
                    },
                    row[4] if row else None,
                )
        if not entries:
            if retired_recovery:
                return 0, {"_stage": "retired", "_sequence": 0, "_nonce": "", "_digest": ""}, None
            return None
        sequence = max(r[0] for r in entries)
        selected = [r for r in entries if r[0] == sequence]
        identities = {(r[1], r[3], principal(r[4]) if r[4] is not None else None) for r in selected}
        if len(identities) != 1:
            raise Incomplete("native publication sequence names different intents")
        registrations = [r for r in selected if r[2] in {"pending", "clear-pending"}]
        if not registrations:
            raise Incomplete("native publication has no intent registration")
        if len({r[2] for r in registrations}) != 1:
            raise Incomplete("native publication operation changed")
        first = min(registrations, key=lambda r: r[5]["id"])
        completed = [r for r in selected if r[2] not in {"pending", "clear-pending"}]
        if completed:
            if len({r[2] for r in completed}) != 1 or (first[2] == "clear-pending") != (completed[0][2] == "clear"):
                raise Incomplete("native publication completion is ambiguous")
            chosen = max(completed, key=lambda r: r[5]["id"])
            if (chosen[5].get("status") or chosen[5].get("state")) != "success":
                raise Incomplete("native publication completion is unavailable")
        else:
            chosen = first
        # Status IDs only reconcile duplicate deliveries of ONE immutable intent.
        # They never decide whether an older or newer verdict has precedence.
        stamp = dict(
            chosen[5],
            _sequence=sequence,
            _nonce=first[1],
            _digest=first[3],
            _stage=chosen[2],
            _started_at=first[5].get("created_at"),
        )
        return first[5]["id"], stamp, first[4]

    def _publication_comment(self, number, rows, context, allowed, comments, *, own_actor=None):
        latest = self._publication_latest(rows, context)
        if latest is None:
            return None, "absent"
        _id, stamp, actor = latest
        if stamp["_stage"] in {"unverified", "retired"}:
            return None, "revoked"
        if actor is None or actor.principal not in allowed:
            return None, "revoked"
        try:
            writable = actor == own_actor or self._permission(actor) in {"write", "admin", "owner"}
        except ForgeError as error:
            if error.status != 404:
                raise
            writable = False
        if not writable:
            return None, "revoked"
        stage = stamp["_stage"]
        if stage in {"pending", "clear-pending"}:
            return None, "pending"
        if stage == "clear":
            if not context.startswith(RECOVERY_CONTEXT):
                raise Incomplete("native review cannot be a recovery clear")
            return None, "clear"
        selected = [row for row in comments if isinstance(row, dict) and row.get("id") == int(stage)]
        if len(selected) != 1:
            raise PublicationDamaged(
                "native publication comment was removed or is unavailable",
                number=number,
                nonce=stamp["_nonce"],
                publication_kind="recovery" if context.startswith(RECOVERY_CONTEXT) else "review",
            )
        row = selected[0]
        body = row.get("body")
        if (
            not isinstance(body, str)
            or SourceActor.from_native(self.repository.forge, row.get("user")) != actor
            or hashlib.sha256(body.encode()).hexdigest() != stamp["_digest"]
            or not self._binding_matches(body, actor)
        ):
            raise PublicationDamaged(
                "native publication content no longer matches its issuer's receipt",
                number=number,
                nonce=stamp["_nonce"],
                publication_kind="recovery" if context.startswith(RECOVERY_CONTEXT) else "review",
            )
        return row, "success"

    def _upsert_comment(self, number, full, marker, actor):
        hidden = comment_marker(marker)
        existing = []
        for row in self.client.comments(self.repository, number):
            if not isinstance(row, dict):
                raise Incomplete("native discussion is incomplete")
            if (
                hidden in (row.get("body") or "")
                and SourceActor.from_native(self.repository.forge, row.get("user")) == actor
            ):
                existing.append(row)
        if len(existing) > 1:
            raise Incomplete("native publication marker is ambiguous")
        if existing:
            row = existing[0]
            if row.get("body") == full:
                return str(self._positive(row.get("id")))
            result = self.client.edit_comment(self.repository, self._positive(row.get("id")), full)
        else:
            result = self.client.post_comment(self.repository, number, full)
        return str(self._positive(result.get("id")) if isinstance(result, dict) else self._positive(None))

    def prepare_publication(self, repo, number, head, body, marker, *, sequence, kind="review", clear=False):
        """Freeze this record in the existing outbox before any remote write.

        sequence is a durable, strictly increasing number for this PR, kind and
        authority generation, shared by its publishers. It orders creation of
        intents, including intents not yet delivered. Never derive it from the
        observed remote state at retry time.
        """
        self._require_publisher_policy()
        if kind not in {"review", "recovery"} or type(clear) is not bool or clear and kind != "recovery":
            raise ValueError("unknown native publication operation")
        if type(sequence) is not int or not 0 < sequence < 2**63:
            raise ValueError("native publication requires a durable monotonic outbox sequence")
        if not isinstance(body, str) or not isinstance(marker, str) or not marker:
            raise ValueError("native publication needs a body and marker")
        self._sha(head)
        self._positive(number)
        self._bound(repo)
        actor = SourceActor.from_native(self.repository.forge, self.client.current_user())
        allowed = getattr(self.publication_policy.publishers, "reviews" if kind == "review" else "recoveries")
        if actor.principal not in allowed:
            raise PublisherNotGranted("native actor is not a configured publisher")
        nonce = secrets.token_hex(16)
        unique_marker = marker + ":" + nonce
        full = (
            ""
            if clear
            else self.publication_policy.bind_comment(
                self.publication_policy.append_provenance(body, comment_marker(unique_marker)), actor
            )
        )
        return {
            "schema": INTENT_SCHEMA,
            "source": self.repository_identity.record(),
            "generation": self.publication_policy.publishers.generation,
            "issuer": int(actor.principal.native_id),
            "number": number,
            "head": head,
            "body": full,
            "digest": hashlib.sha256(full.encode()).hexdigest(),
            "marker": unique_marker,
            "kind": kind,
            "nonce": nonce,
            "sequence": sequence,
            "clear": clear,
        }

    @staticmethod
    def _intent_prefix(intent):
        return PUB_DESCRIPTION + f"{intent['generation']}:{intent['sequence']}:{intent['nonce']}:"

    def publish(self, intent):
        required = {
            "schema",
            "source",
            "generation",
            "issuer",
            "number",
            "head",
            "body",
            "digest",
            "marker",
            "kind",
            "nonce",
            "sequence",
            "clear",
        }
        if (
            not isinstance(intent, dict)
            or set(intent) != required
            or intent["schema"] != INTENT_SCHEMA
            or intent["source"] != self.repository_identity.record()
            or not isinstance(intent["generation"], str)
            or intent["generation"] != self.publication_policy.publishers.generation
            or intent["kind"] not in {"review", "recovery"}
            or type(intent["clear"]) is not bool
            or intent["clear"]
            and intent["kind"] != "recovery"
            or type(intent["sequence"]) is not int
            or not 0 < intent["sequence"] < 2**63
            or not isinstance(intent["body"], str)
            or len(intent["body"].encode()) > 16 * 1024 * 1024
            or not isinstance(intent["marker"], str)
            or not isinstance(intent["nonce"], str)
            or re.fullmatch(r"[0-9a-f]{32}", intent["nonce"]) is None
            or not intent["marker"].endswith(":" + intent["nonce"])
            or hashlib.sha256(intent["body"].encode()).hexdigest() != intent["digest"]
            or intent["clear"]
            and intent["body"] != ""
            or not intent["clear"]
            and comment_marker(intent["marker"]) not in intent["body"]
        ):
            raise ValueError("invalid frozen native publication")
        self._positive(intent["number"])
        self._sha(intent["head"])
        self._bound(self.repository.name)
        actor = SourceActor.from_native(self.repository.forge, self.client.current_user())
        allowed = getattr(self.publication_policy.publishers, "reviews" if intent["kind"] == "review" else "recoveries")
        if (
            type(intent["issuer"]) is not int
            or intent["issuer"] != int(actor.principal.native_id)
            or actor.principal not in allowed
            or not intent["clear"]
            and not self._binding_matches(intent["body"], actor)
        ):
            raise PublisherNotGranted("frozen native publication belongs to another issuer or source")
        # Facts belong to the accepted commit, including while a PR is closed or
        # temporarily points elsewhere. Dropping a red intent would revive old
        # green evidence if that PR later returned to the same head.
        context = (REVIEW_CONTEXT if intent["kind"] == "review" else RECOVERY_CONTEXT) + str(intent["number"])
        prefix = self._intent_prefix(intent)
        pending = prefix + ("clear-pending:" if intent["clear"] else "pending:") + intent["digest"]
        rows = self.client.statuses(self.repository, intent["head"])
        latest = self._publication_latest(rows, context)
        if latest is not None:
            _id, stamp, issuer = latest
            if stamp["_sequence"] > intent["sequence"]:
                raise PublicationSuperseded(
                    "newer native publication superseded this intent",
                    sequence=stamp["_sequence"],
                    nonce=stamp["_nonce"],
                    actor=issuer,
                )
            if stamp["_sequence"] == intent["sequence"]:
                if issuer != actor or stamp["_nonce"] != intent["nonce"] or stamp["_digest"] != intent["digest"]:
                    raise PublicationSuperseded(
                        "native publication sequence was reused for another intent",
                        sequence=stamp["_sequence"],
                        nonce=stamp["_nonce"],
                        actor=issuer,
                    )
                if stamp["_stage"] not in {"pending", "clear-pending", "unverified"}:
                    comment, state = self._publication_comment(
                        intent["number"],
                        rows,
                        context,
                        allowed,
                        self.client.comments(self.repository, intent["number"]),
                        own_actor=actor,
                    )
                    if state == "clear":
                        return "clear"
                    if state == "success" and comment is not None:
                        return str(comment["id"])
        if latest is None or latest[1]["_sequence"] < intent["sequence"] or latest[1]["_stage"] == "unverified":
            self.client.request(
                "POST",
                f"repos/{self.repository.name}/statuses/{intent['head']}",
                {"context": context, "state": "pending", "description": pending},
            )
        # Each intent owns its comment; late writes cannot corrupt newer bodies.
        # Even a success POST racing a new registration remains an older sequence.
        self._check_publication_current(intent, context)
        comment_id = (
            "clear"
            if intent["clear"]
            else self._upsert_comment(intent["number"], intent["body"], intent["marker"], actor)
        )
        self._check_publication_current(intent, context)
        self.client.request(
            "POST",
            f"repos/{self.repository.name}/statuses/{intent['head']}",
            {"context": context, "state": "success", "description": prefix + comment_id + ":" + intent["digest"]},
        )
        self._check_publication_current(intent, context)
        return comment_id

    def _check_publication_current(self, intent, context):
        latest = self._publication_latest(self.client.statuses(self.repository, intent["head"]), context)
        if latest is None or latest[1]["_stage"] in {"unverified", "retired"}:
            raise Incomplete("native publication registration needs observation or exact-intent reconciliation")
        stamp = latest[1]
        if stamp["_nonce"] != intent["nonce"]:
            raise PublicationSuperseded(
                "native publication intent changed", sequence=stamp["_sequence"], nonce=stamp["_nonce"], actor=latest[2]
            )

    def read_publications(self, number, head, rows=None):
        self._positive(number)
        self._sha(head)
        self._bound(self.repository.name)
        rows = self.client.statuses(self.repository, head) if rows is None else rows
        comments = self.client.comments(self.repository, number)
        result = self._read_publications(number, head, rows, comments)
        self._bound(self.repository.name)
        return result

    def _read_publications(self, number, head, rows, comments, *, allow_damaged_review=False):
        """Share one observation across standalone reads and source snapshots.

        The source snapshot caller brackets this core with repository checks.
        It can carry a damaged review as a negative state, never as approval.
        Recovery damage still requires reconciliation before returning facts.
        """
        self._require_publisher_policy()
        if type(allow_damaged_review) is not bool:
            raise ValueError("damaged-review handling must be explicit")
        if not isinstance(comments, list):
            raise Incomplete("native discussion is unavailable")
        publishers = self.publication_policy.publishers
        review_error = None
        try:
            review, review_state = self._publication_comment(
                number, rows, REVIEW_CONTEXT + str(number), publishers.reviews, comments
            )
        except PublicationDamaged as error:
            review_error = error
            review, review_state = None, "damaged"
        try:
            recovery, recovery_state = self._publication_comment(
                number, rows, RECOVERY_CONTEXT + str(number), publishers.recoveries, comments
            )
        except PublicationDamaged as error:
            error.head = head
            raise
        if review_error is not None and not allow_damaged_review:
            raise review_error
        return tuple(row for row in (review, recovery) if row is not None), review_state, recovery_state
