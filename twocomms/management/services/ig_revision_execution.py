"""Dormant preparation and completion coordinator for revision execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from datetime import timedelta
import secrets

from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from management.models import (
    IgClient,
    IgCustomerTurn,
    IgCustomerTurnRevision,
    IgRevisionDeliveryEffect,
    IgFollowUpTask,
    IgTurnRevisionSource,
    InstagramBotMessage,
    GeminiRequest,
)
from management.services.ig_turn_revisions import (
    RevisionClaim,
    claim_revision_preparation,
    claim_sealed_revision,
    replay_snapshot,
    seal_revision,
)


MAX_DUE_SCAN = 50


@dataclass(frozen=True)
class RevisionPreparationResult:
    revision_id: int = 0
    source_message_ids: tuple[int, ...] = ()
    preparation_token: str = ""
    execution_token: str = ""
    media_prepare_deadline: object | None = None
    snapshot: dict = field(default_factory=dict)
    ready: bool = False
    reason: str = ""
    callback_failures: tuple[int, ...] = ()


@dataclass(frozen=True)
class RevisionCompletionResult:
    revision_id: int
    completed: bool
    reconciliation_required: bool
    reason: str
    states: tuple[str, ...] = ()


def _source_owned_revision_q() -> Q:
    from management.services.ig_revision_source_coverage import source_transfer_owned_q
    from management.services.ig_revision_commerce import source_producer_owned_q

    return source_transfer_owned_q() | source_producer_owned_q()


def _execution_owned_revision_q() -> Q:
    return (
        Q(media_prepare_deadline__isnull=False)
        | Q(sealed_at__isnull=False)
        | Q(snapshot_digest__gt="")
        | Q(generation_proposal_digest__gt="")
        | Q(has_delivery_effect=True)
    )


def _owned_revision_q() -> Q:
    return _execution_owned_revision_q() | _source_owned_revision_q()


def _rollout_eligible_q(cutover_at) -> Q:
    sticky = _owned_revision_q() | Q(
        origin__in=("manual_resume", "auto_refresh", "outage_recovery")
    )
    if cutover_at is None:
        return sticky | Q(origin="inbound")
    return sticky | Q(origin="inbound", created_at__gte=cutover_at)


def due_revision_ids(*, now=None, limit: int = 25, cutover_at=None, source_producer_only=False) -> list[int]:
    """Bounded DB-only selector for quiet-due or reclaimable preparation heads."""
    now = now or timezone.now()
    bounded = max(1, min(int(limit), MAX_DUE_SCAN))
    from django.db.models import F
    from management.services.ig_revision_commerce import source_producer_owned_q

    active_opt_out = Q(client__opted_out_at__isnull=False) & (
        Q(client__opted_in_at__isnull=True)
        | Q(client__opted_out_at__gt=F("client__opted_in_at"))
    )
    return list(
        IgCustomerTurnRevision.objects.annotate(
            has_delivery_effect=Exists(
                IgRevisionDeliveryEffect.objects.filter(revision_id=OuterRef("pk"))
            )
        ).filter(active_slot=1)
        .filter(overall_deadline__gt=now)
        .filter(
            Q(
                state=IgCustomerTurnRevision.State.COLLECTING,
                quiet_deadline__lte=now,
            )
            | Q(
                state=IgCustomerTurnRevision.State.PREPARING,
                lease_until__lte=now,
            )
            | Q(state=IgCustomerTurnRevision.State.SEALED)
        )
        .filter(
            Q(origin="manual_resume")
            | Q(origin__in=("auto_refresh", "outage_recovery"), action_receipts__has_key="manual_resume_authorization")
            | (
                ~Q(turn__claim_state__in=(IgCustomerTurn.ClaimState.PROCESSED, IgCustomerTurn.ClaimState.SUPERSEDED))
                & ~Q(turn__terminal_reason__gt="")
            )
        )
        .filter(
            client__hidden_at__isnull=True,
            client__is_blocked=False,
            client__bot_paused=False,
            client__manager_takeover=False,
            client__privacy_erasure_started_at__isnull=True,
            client__reply_permission_epoch=F("permission_epoch"),
        )
        .exclude(active_opt_out)
        .filter(_rollout_eligible_q(cutover_at))
        .filter(source_producer_owned_q() if source_producer_only else Q())
        .order_by("quiet_deadline", "revision", "id")
        .values_list("id", flat=True)[:bounded]
    )


def prepare_revision(
    revision_id: int,
    capture_callback: Callable,
    *,
    preparation_token: str = "",
    now=None,
) -> RevisionPreparationResult:
    """Capture exact sources outside transactions, then seal/claim if ready.

    Callback signature: ``callback(message_id=..., deadline_at=...,
    remaining_seconds=...)``. It may update only the existing source capture
    state. This coordinator never sleeps, resets a source, or performs I/O
    while a database transaction is open.
    """
    now = now or timezone.now()
    if connection.in_atomic_block:
        return RevisionPreparationResult(
            revision_id=int(revision_id or 0), reason="caller_transaction_active"
        )
    if not callable(capture_callback):
        return RevisionPreparationResult(
            revision_id=int(revision_id or 0), reason="capture_callback_missing"
        )
    sealed_revision = IgCustomerTurnRevision.objects.filter(
        pk=revision_id,
        active_slot=1,
        state=IgCustomerTurnRevision.State.SEALED,
        snapshot_digest__gt="",
    ).first()
    if sealed_revision is not None:
        source_ids = tuple(
            sealed_revision.sources.order_by("ordinal", "id")
            .values_list("message_id", flat=True)
        )
        if len(source_ids) != sealed_revision.source_count:
            return RevisionPreparationResult(
                revision_id=sealed_revision.pk,
                source_message_ids=source_ids,
                reason="revision_sources_changed",
            )
        execution, invalid_reason = _claim_current_sealed_revision(
            sealed_revision.pk,
            now=now,
        )
        if not execution.token or execution.revision is None:
            return RevisionPreparationResult(
                revision_id=sealed_revision.pk,
                source_message_ids=source_ids,
                reason=invalid_reason or execution.reason or "execution_claim_failed",
            )
        snapshot = replay_snapshot(execution.revision.pk) or {}
        if not snapshot:
            return RevisionPreparationResult(
                revision_id=sealed_revision.pk,
                source_message_ids=source_ids,
                execution_token=execution.token,
                reason="snapshot_unavailable",
            )
        return RevisionPreparationResult(
            revision_id=sealed_revision.pk,
            source_message_ids=source_ids,
            execution_token=execution.token,
            snapshot=snapshot,
            ready=True,
            reason="ready",
        )
    if preparation_token:
        revision = IgCustomerTurnRevision.objects.filter(
            pk=revision_id,
            active_slot=1,
            state=IgCustomerTurnRevision.State.PREPARING,
            claim_token=preparation_token,
            lease_until__gt=now,
        ).first()
        if revision is None:
            return RevisionPreparationResult(
                revision_id=int(revision_id or 0), reason="preparation_claim_stale"
            )
        token = preparation_token
        media_deadline = revision.media_prepare_deadline
    else:
        claim = claim_revision_preparation(revision_id, now=now)
        if not claim.token or claim.revision is None:
            return RevisionPreparationResult(
                revision_id=int(revision_id or 0),
                reason=claim.reason or "preparation_claim_failed",
            )
        revision = claim.revision
        token = claim.token
        media_deadline = claim.media_prepare_deadline
    source_ids = tuple(
        revision.sources.order_by("ordinal", "id")
        .values_list("message_id", flat=True)
    )
    if len(source_ids) != revision.source_count:
        return RevisionPreparationResult(
            revision_id=revision.pk,
            source_message_ids=source_ids,
            preparation_token=token,
            media_prepare_deadline=media_deadline,
            reason="revision_sources_changed",
        )

    callback_failures: list[int] = []
    for message_id in source_ids:
        current = timezone.now()
        remaining = max(
            0.0,
            (media_deadline - current).total_seconds()
            if media_deadline is not None else 0.0,
        )
        if remaining <= 0:
            break
        try:
            capture_callback(
                message_id=int(message_id),
                deadline_at=media_deadline,
                remaining_seconds=remaining,
            )
        except Exception:
            callback_failures.append(int(message_id))

    sealed = seal_revision(
        revision.pk,
        token,
        now=timezone.now(),
    )
    if not sealed.sealed or sealed.revision is None:
        return RevisionPreparationResult(
            revision_id=revision.pk,
            source_message_ids=source_ids,
            preparation_token=token,
            media_prepare_deadline=media_deadline,
            reason=sealed.reason or "seal_deferred",
            callback_failures=tuple(callback_failures),
        )
    execution, invalid_reason = _claim_current_sealed_revision(
        sealed.revision.pk,
        now=timezone.now(),
    )
    if not execution.token or execution.revision is None:
        return RevisionPreparationResult(
            revision_id=revision.pk,
            source_message_ids=source_ids,
            media_prepare_deadline=media_deadline,
            reason=invalid_reason or execution.reason or "execution_claim_failed",
            callback_failures=tuple(callback_failures),
        )
    snapshot = replay_snapshot(execution.revision.pk) or {}
    if not snapshot:
        return RevisionPreparationResult(
            revision_id=revision.pk,
            source_message_ids=source_ids,
            execution_token=execution.token,
            media_prepare_deadline=media_deadline,
            reason="snapshot_unavailable",
            callback_failures=tuple(callback_failures),
        )
    return RevisionPreparationResult(
        revision_id=revision.pk,
        source_message_ids=source_ids,
        execution_token=execution.token,
        media_prepare_deadline=media_deadline,
        snapshot=snapshot,
        ready=True,
        reason="ready",
        callback_failures=tuple(callback_failures),
    )


def _claim_current_sealed_revision(revision_id: int, *, now):
    """Claim an immutable snapshot while holding its client's permission fence."""
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values(
        "client_id", "permission_epoch", "erasure_started_at_snapshot"
    ).first()
    if identity is None:
        return RevisionClaim(None, reason="revision_missing"), "revision_missing"
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(
            pk=identity["client_id"]
        ).first()
        if (
            client is None
            or client.hidden_at is not None
            or client.is_blocked
            or client.bot_paused
            or client.manager_takeover
            or client.privacy_erasure_started_at is not None
            or client.privacy_erasure_started_at
            != identity["erasure_started_at_snapshot"]
            or int(client.reply_permission_epoch or 0)
            != int(identity["permission_epoch"] or 0)
            or (
                client.opted_out_at
                and (
                    not client.opted_in_at
                    or client.opted_out_at > client.opted_in_at
                )
            )
        ):
            return (
                RevisionClaim(None, reason="client_invalidated_before_execution"),
                "client_invalidated_before_execution",
            )
        return claim_sealed_revision(revision_id, now=now), ""


def prepare_next_due_revision(
    capture_callback: Callable,
    *,
    now=None,
    limit: int = 25,
) -> RevisionPreparationResult:
    """Claim the first bounded due candidate; races return a typed no-op."""
    ids = due_revision_ids(now=now, limit=limit)
    if not ids:
        return RevisionPreparationResult(reason="no_due_revision")
    return prepare_revision(ids[0], capture_callback, now=now)


def _verified_replaced_failure_ids(effects):
    """Pure receipt proof for an exact, fully delivered conditional replacement.

    Rebuild the immutable native plan rather than treating an unrelated SENT
    row, a mutable activation pointer, or a failed-state label as coverage.
    Source-preference recovery is a separate revision lane and is not involved.
    """
    from management.services.ig_revision_outbox import _digest, _normalize_specs

    state = IgRevisionDeliveryEffect.State
    if (not effects or any(type(row) is not IgRevisionDeliveryEffect or row.get_deferred_fields() for row in effects)
            or not any(row.state == state.DEFINITE_FAILED for row in effects)):
        return frozenset()
    try:
        rows = sorted(effects, key=lambda row: (row.order_index, row.pk))
        first = rows[0]
        positive = ("pk", "revision_id", "source_message_id", "settings_id_snapshot", "publication_id", "publication_version", "part_count")
        nonnegative = ("order_index", "part_index", "settings_permission_epoch", "client_permission_epoch")
        if any(any(type(getattr(row, key)) is not int or getattr(row, key) <= 0 for key in positive)
                or any(type(getattr(row, key)) is not int or getattr(row, key) < 0 for key in nonnegative) for row in rows):
            return frozenset()
        shared = ("revision_id", "source_message_id", "actor", "purpose", "recipient_igsid",
            "provider_namespace", "settings_id_snapshot", "settings_permission_epoch",
            "client_permission_epoch", "revision_snapshot_digest", "plan_digest", "publication_id",
            "publication_version", "publication_hash", "authority_context_digest", "fact_bindings", "offer_bindings")
        if (first.actor != "bot" or first.purpose != "normal_reply" or not first.recipient_igsid
            or not first.provider_namespace or len(first.plan_digest) != 64
            or any(any(getattr(row, key) != getattr(first, key) for key in shared) for row in rows)
            or [row.order_index for row in rows] != list(range(len(rows)))):
            return frozenset()
        specs = []
        for row in rows:
            activation = {}
            if row.group == "template_fallback":
                if (not row.activation_group or type(row.activation_part_index) is not int
                    or row.activation_part_index < 0
                    or row.activation_failure_code not in {"provider_rejected", "link_rejected"}):
                    return frozenset()
                activation = {"group": row.activation_group, "part_index": row.activation_part_index,
                    "failure_code": row.activation_failure_code}
            elif row.activation_group or row.activation_part_index is not None or row.activation_failure_code:
                return frozenset()
            specs.append({"group": row.group, "kind": row.kind, "payload": row.payload,
                "projection_metadata": row.projection_metadata, "activation": activation})
        normalized = _normalize_specs(specs, first.recipient_igsid)
        for row, spec in zip(rows, normalized, strict=True):
            if (any(getattr(row, key) != spec[key] for key in ("part_index", "part_count", "payload_digest",
                    "activation_group", "activation_part_index", "activation_failure_code"))
                or row.projection_digest != spec["projection_digest"]):
                return frozenset()
            manifest = (row.projection_metadata or {}).get("catalog_choice")
            if manifest is not None:
                scope = manifest["scope"]
                if (manifest["invitation_id"] != row.revision_id
                    or row.source_message_id not in scope["source_message_ids"]
                    or any(scope[key] != getattr(row, field) for key, field in (
                        ("igsid", "recipient_igsid"), ("namespace", "provider_namespace"),
                        ("revision_snapshot_digest", "revision_snapshot_digest"),
                        ("settings_id", "settings_id_snapshot"), ("settings_epoch", "settings_permission_epoch"),
                        ("client_epoch", "client_permission_epoch"), ("publication_id", "publication_id"),
                        ("publication_version", "publication_version"), ("publication_hash", "publication_hash")))):
                    return frozenset()
        material = {"revision_id": first.revision_id, "revision_snapshot_digest": first.revision_snapshot_digest,
            "actor": first.actor, "purpose": first.purpose, "effects": normalized,
            "publication": {"id": first.publication_id, "version": first.publication_version, "hash": first.publication_hash},
            "authority_context_digest": first.authority_context_digest,
            "fact_bindings": first.fact_bindings, "offer_bindings": first.offer_bindings}
        if _digest(material) != first.plan_digest:
            return frozenset()
        fallback = [row for row in rows if row.group == "template_fallback"]
        if not fallback or any(row.state != state.SENT or not row.provider_message_id
                or len(row.provider_message_id) > 255
                or any(ord(char) < 32 or ord(char) == 127 for char in row.provider_message_id) or row.terminal_at is None
                or row.provider_started_at is None for row in fallback):
            return frozenset()
        if len({row.provider_message_id for row in rows if row.state == state.SENT}) != sum(row.state == state.SENT for row in rows):
            return frozenset()
        replaced = set()
        for row in fallback:
            parent = next((candidate for candidate in rows if candidate.group == row.activation_group
                and candidate.part_index == row.activation_part_index), None)
            if (parent is None or parent.state != state.DEFINITE_FAILED
                or parent.failure_code != row.activation_failure_code
                or type(parent.provider_http_status) is not int or not 400 <= parent.provider_http_status < 500
                or parent.provider_started_at is None or parent.terminal_at is None or parent.provider_message_id
                or not parent.provider_started_at <= parent.terminal_at <= row.provider_started_at <= row.terminal_at):
                return frozenset()
            parent_manifest = (parent.projection_metadata or {}).get("catalog_choice")
            if parent_manifest is not None and (row.projection_metadata or {}).get("catalog_choice") != parent_manifest:
                return frozenset()
            replaced.add(parent.pk)
        return frozenset(replaced)
    except (AttributeError, KeyError, TypeError, ValueError, IndexError):
        return frozenset()


def _completion_decision(effects) -> tuple[bool, bool, str]:
    states = {effect.state for effect in effects}
    state = IgRevisionDeliveryEffect.State
    if state.UNKNOWN in states:
        return False, True, "delivery_unknown"
    if state.PROVIDER_STARTED in states:
        return False, True, "provider_started_unreconciled"
    if state.CLAIMED in states or state.PLANNED in states:
        return False, False, "effects_pending"
    sent = sum(effect.state == state.SENT for effect in effects)
    replaced = _verified_replaced_failure_ids(effects)
    failed = sum(effect.state == state.DEFINITE_FAILED and effect.pk not in replaced for effect in effects)
    if failed:
        return (
            False,
            True,
            "partial_definite_failure" if sent else "definite_failure",
        )
    cancelled_owed = any(
        effect.state in {state.CANCELLED, state.SUPERSEDED}
        and effect.group != "template_fallback"
        for effect in effects
    )
    if sent and cancelled_owed:
        return False, True, "partial_cancelled_delivery"
    if sent and any(effect.purpose == "technical_holding" for effect in effects):
        return False, True, "technical_holding_sent"
    if sent:
        return True, False, "delivered"
    if states and states <= {state.CANCELLED, state.SUPERSEDED}:
        return True, False, "cancelled_before_provider"
    return False, True, "effect_state_invalid"


def complete_revision_from_effects(
    revision_id: int,
    revision_token: str,
    *,
    now=None,
) -> RevisionCompletionResult:
    """Finalize only conclusive effect aggregates; preserve every debt state."""
    now = now or timezone.now()
    with transaction.atomic():
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(
            pk=revision_id
        ).first()
        if revision is None:
            return RevisionCompletionResult(
                int(revision_id or 0), False, True, "revision_missing"
            )
        effects = list(
            IgRevisionDeliveryEffect.objects.select_for_update()
            .filter(revision=revision)
            .order_by("order_index", "id")
        )
        states = tuple(effect.state for effect in effects)
        if not effects:
            return RevisionCompletionResult(
                revision.pk, False, False, "effects_missing", states
            )
        completed, debt, reason = _completion_decision(effects)
        if completed and _verified_replaced_failure_ids(effects):
            proof, proof_reason = _whole_sent_proof(revision, effects)
            if not proof:
                return RevisionCompletionResult(revision.pk, False, True, proof_reason, states)
        from management.services.ig_response_debt import response_coverage

        if completed and response_coverage(revision).get("remaining"):
            return RevisionCompletionResult(
                revision.pk, False, True, "semantic_reply_incomplete", states
            )
        if revision.state == revision.State.PROCESSED:
            return RevisionCompletionResult(
                revision.pk, completed, debt, reason, states
            )
        if (
            revision.state != revision.State.CLAIMED
            or not revision_token
            or revision.claim_token != revision_token
        ):
            return RevisionCompletionResult(
                revision.pk, False, True, "revision_claim_stale", states
            )
        if not completed:
            return RevisionCompletionResult(
                revision.pk, False, debt, reason, states
            )
        revision.state = revision.State.PROCESSED
        revision.processed_at = now
        revision.claim_token = ""
        revision.claimed_at = None
        revision.lease_until = None
        revision.save(update_fields=[
            "state", "processed_at", "claim_token", "claimed_at",
            "lease_until", "updated_at",
        ])
        return RevisionCompletionResult(
            revision.pk, True, False, reason, states
        )


FINALIZATION_LEASE = timedelta(seconds=20)
FINALIZATION_PREFIX = "finalize:"
DEBT_PREFIX = "debt:"


def incomplete_revision_deliveries():
    """Conclusive partial/failed delivery, distinct from ambiguous HTTP debt."""
    parts = IgRevisionDeliveryEffect.objects.filter(revision_id=OuterRef("pk"))
    return IgCustomerTurnRevision.objects.annotate(
        has_sent_part=Exists(parts.filter(state="sent")),
        has_failed_part=Exists(parts.filter(state="definite_failed")),
        has_cancelled_reply_part=Exists(parts.filter(
            state__in=("cancelled", "superseded"),
        ).exclude(group="template_fallback")),
        has_unsettled_part=Exists(parts.filter(
            state__in=("planned", "claimed", "provider_started", "unknown"),
        )),
    ).filter(has_unsettled_part=False).filter(
        Q(has_failed_part=True)
        | Q(has_sent_part=True, has_cancelled_reply_part=True)
    )


def reconcile_incomplete_revision_deliveries(*, now=None, limit=100, dry_run=True):
    """Give stranded delivery a durable operator owner; never retry transport.

    Terminal receipts are authoritative even after a newer head replaces the
    revision. A stale execution lease cannot make that reply debt disappear.
    """
    from management.services.ig_response_debt import park_manual_revision
    from management.services.ig_revision_outbox import project_legacy_message

    now = now or timezone.now()
    bounded = max(1, min(int(limit), 500))
    candidates = incomplete_revision_deliveries().filter(
        state="claimed", client__privacy_erasure_started_at__isnull=True,
    ).exclude(action_receipts__has_key="response_debt").filter(
        Q(lease_until__isnull=True) | Q(lease_until__lte=now)
    )
    ids = list(candidates.order_by("id").values_list("id", flat=True)[:bounded])
    entries = []
    for revision_id in ids:
        identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
        if identity is None:
            continue
        with transaction.atomic():
            client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
            revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
            if (client is None or revision is None or client.privacy_erasure_started_at
                or revision.state != "claimed" or (revision.action_receipts or {}).get("response_debt")
                or (revision.lease_until and revision.lease_until > now)):
                continue
            effects = list(revision.delivery_effects.select_for_update().order_by("order_index", "id"))
            complete, debt, reason = _completion_decision(effects)
            if complete or not debt or reason not in {"partial_cancelled_delivery", "partial_definite_failure", "definite_failure"}:
                continue
            task = None
            if not dry_run:
                task = park_manual_revision(revision, reason, now=now)
                project_legacy_message(revision.pk)
            entries.append({"revision_id": revision.pk, "client_id": client.pk,
                            "reason": reason, "task_id": getattr(task, "pk", None)})
    return {"dry_run": bool(dry_run), "scanned": len(ids), "entries": entries,
            "writes": len(entries) if not dry_run else 0, "provider_calls": 0}


@dataclass(frozen=True)
class RevisionFinalizationResult:
    revision_id: int
    completed: bool = False
    retryable: bool = False
    reason: str = ""
    sent_parts: int = 0


def _whole_sent_proof(revision, effects):
    from management.services.ig_revision_outbox import _digest

    completed, _debt, reason = _completion_decision(effects)
    if not effects or not any(row.state == row.State.SENT for row in effects):
        return False, reason
    source_ids = {row["message_id"] for row in revision.bundle_snapshot.get("sources", ())}
    plans = {row.plan_digest for row in effects}
    if len(plans) != 1 or "" in plans or _digest(revision.bundle_snapshot) != revision.snapshot_digest:
        return False, "delivery_plan_proof_invalid"
    settings_ids = {row.settings_id_snapshot for row in effects}
    namespaces = {row.provider_namespace for row in effects}
    sent = [row for row in effects if row.state == row.State.SENT]
    if len(settings_ids) != 1 or len(namespaces) != 1 or "" in namespaces:
        return False, "delivery_receipt_scope_invalid"
    if _verified_replaced_failure_ids(effects):
        if (namespaces != {row.get("source_namespace") for row in revision.bundle_snapshot.get("sources", ())}
            or any(row.revision_id != revision.pk for row in effects)
            or any((row.projection_metadata or {}).get("catalog_choice", {}).get("scope", {}).get("client_id", revision.client_id)
                != revision.client_id for row in effects)):
            return False, "delivery_receipt_scope_invalid"
    if len({row.provider_message_id for row in sent}) != len(sent):
        return False, "delivery_provider_receipt_duplicate"
    groups = {}
    for effect in effects:
        groups.setdefault(effect.group, []).append(effect)
        if (effect.source_message_id not in source_ids
            or effect.recipient_igsid != revision.client.igsid
            or effect.revision_snapshot_digest != revision.snapshot_digest
            or _digest(effect.payload) != effect.payload_digest
            or (effect.state == effect.State.SENT and (not effect.provider_message_id or effect.terminal_at is None))):
            return False, "delivery_receipt_proof_invalid"
    for parts in groups.values():
        if [row.part_index for row in parts] != list(range(len(parts))) or any(row.part_count != len(parts) for row in parts):
            return False, "delivery_parts_proof_invalid"
    return True, reason


def finalization_due_ids(*, now=None, limit=25):
    """Conclusive receipts have their own queue, independent of active head/deadline."""
    now = now or timezone.now()
    from django.db.models import Exists, OuterRef
    from management.services.ig_revision_reply_projection import VERSION as PROJECTION_VERSION

    owed = IgRevisionDeliveryEffect.objects.filter(revision_id=OuterRef("pk")).exclude(state=IgRevisionDeliveryEffect.State.SENT).exclude(group="template_fallback", state__in=("cancelled", "superseded"))
    # Candidate discovery only. A prior erroneous manual hold can be repaired
    # after a conditional group has actual receipts; every transition below
    # still requires the pure immutable-plan and whole-source proof under lock.
    failed_parent = IgRevisionDeliveryEffect.objects.filter(revision_id=OuterRef("revision_id"),
        group=OuterRef("activation_group"), part_index=OuterRef("activation_part_index"),
        failure_code=OuterRef("activation_failure_code"), state="definite_failed",
        provider_http_status__gte=400, provider_http_status__lt=500,
        failure_code__in=("provider_rejected", "link_rejected"))
    possible_replacement = IgRevisionDeliveryEffect.objects.filter(revision_id=OuterRef("pk"),
        group="template_fallback", state="sent", provider_message_id__gt="", terminal_at__isnull=False
    ).annotate(exact_failed_parent=Exists(failed_parent)).filter(exact_failed_parent=True)
    candidates = IgCustomerTurnRevision.objects.filter(
        state__in=(IgCustomerTurnRevision.State.CLAIMED, IgCustomerTurnRevision.State.PROCESSED),
        client__privacy_erasure_started_at__isnull=True,
        delivery_effects__state=IgRevisionDeliveryEffect.State.SENT,
    ).exclude(action_receipts__has_key="technical_holding_delivery").exclude(
        action_receipts__has_key="semantic_delivery_finalized",
    ).annotate(owed_parts=Exists(owed), possible_replacement=Exists(possible_replacement)).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now)).filter(
        (~Q(claim_token__startswith=DEBT_PREFIX) & ~Q(action_receipts__has_key="response_debt"))
        | Q(owed_parts=False)
        | Q(possible_replacement=True)
    ).filter(
        Q(state=IgCustomerTurnRevision.State.CLAIMED)
        | Q(sources__message__status__in=("pending", "processing"))
        # Only newly admitted plans own automatic counter projection. Missing
        # receipts on older plans are not evidence that nobody counted them.
        | (~Q(action_receipts__has_key="sent_reply_projection") & Q(owed_parts=False)
           & Q(action_receipts__reply_projection_admission__version=PROJECTION_VERSION))
        | (~Q(action_receipts__has_key="normal_followups") & (Q(generation_proposal_digest__gt="") | Q(action_receipts__input_decision__origin="static_reply")))
    ).order_by("id").values_list("id", flat=True).distinct()
    return list(candidates[:max(1, min(int(limit), MAX_DUE_SCAN))])


def finalize_sent_revision_effects(revision_id, *, execution_token="", now=None):
    """Project confirmed delivery and settle optional followups, never call HTTP."""
    if connection.in_atomic_block:
        return RevisionFinalizationResult(revision_id, reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionFinalizationResult(revision_id, reason="revision_missing")
    now = now or timezone.now()
    finalization_token = ""
    try:
        with transaction.atomic():
            client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
            revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
            if client is None or revision is None:
                return RevisionFinalizationResult(revision_id, reason="revision_missing")
            if client.privacy_erasure_started_at is not None:
                return RevisionFinalizationResult(revision_id, reason="erasure_private_projection_suppressed")
            revision.client = client
            effects = list(revision.delivery_effects.select_for_update().order_by("order_index", "id"))
            proof, reason = _whole_sent_proof(revision, effects)
            if not proof:
                return RevisionFinalizationResult(revision_id, reason=reason)
            if revision.state not in {revision.State.CLAIMED, revision.State.PROCESSED}:
                return RevisionFinalizationResult(revision_id, reason="revision_not_finalizable")
            semantic_receipt = (revision.action_receipts or {}).get("semantic_delivery_finalized")
            if semantic_receipt:
                from management.services.ig_response_debt import response_coverage

                coverage = response_coverage(revision)
                if coverage.get("remaining"):
                    return RevisionFinalizationResult(
                        revision_id,
                        reason=("waiting_on_customer" if semantic_receipt.get("disposition") == "waiting_on_customer"
                                else "semantic_reply_incomplete"),
                        sent_parts=sum(row.state == row.State.SENT for row in effects),
                    )
            owned = bool(execution_token and revision.claim_token == execution_token)
            if revision.lease_until and revision.lease_until > now and not owned:
                return RevisionFinalizationResult(revision_id, retryable=True, reason="finalization_lease_active")
            finalization_token = FINALIZATION_PREFIX + secrets.token_hex(16)
            revision.claim_token = finalization_token
            revision.claimed_at = now
            revision.lease_until = now + FINALIZATION_LEASE
            revision.save(update_fields=["claim_token", "claimed_at", "lease_until", "updated_at"])
        from management.services.ig_revision_live import _project_sent_history, _project_completed_sources
        from management.services.ig_revision_outbox import project_legacy_message
        from management.services.ig_revision_followups import settle_revision_normal_followups

        projected_reply = _project_sent_history(revision_id)
        if isinstance(projected_reply, dict):
            from management.services.instagram_bot import _enqueue_memory_source_event

            for message_id in projected_reply.get("reply_message_ids", ()):
                _enqueue_memory_source_event(message_id)
        project_legacy_message(revision_id)
        complete, _debt, aggregate_reason = _completion_decision(effects)
        if aggregate_reason == "technical_holding_sent":
            from management.services.ig_revision_holding import finalize_technical_holding

            with transaction.atomic():
                client = IgClient.objects.select_for_update().get(pk=identity["client_id"])
                locked = IgCustomerTurnRevision.objects.select_for_update().get(pk=revision_id, claim_token=finalization_token)
                finalize_technical_holding(locked, effects)
            return RevisionFinalizationResult(revision_id, reason=aggregate_reason,
                                              sent_parts=sum(row.state == row.State.SENT for row in effects))
        if not complete or aggregate_reason != "delivered":
            # Known SENT parts become visible even when another part is
            # unresolved. Keep owed state and stop reprojecting unchanged debt.
            from management.services.ig_response_debt import park_manual_revision

            with transaction.atomic():
                IgClient.objects.select_for_update().get(pk=identity["client_id"])
                locked = IgCustomerTurnRevision.objects.select_for_update().get(pk=revision_id, claim_token=finalization_token)
                park_manual_revision(locked, aggregate_reason, now=now)
            return RevisionFinalizationResult(revision_id, reason=aggregate_reason, sent_parts=sum(row.state == row.State.SENT for row in effects))
        from management.services.ig_response_debt import (
            delivered_selector_wait, park_manual_revision, response_coverage,
        )

        with transaction.atomic():
            IgClient.objects.select_for_update().get(pk=identity["client_id"])
            locked = IgCustomerTurnRevision.objects.select_for_update().get(
                pk=revision_id, claim_token=finalization_token,
            )
            coverage = response_coverage(locked)
            if coverage.get("remaining"):
                waiting_on_customer = delivered_selector_wait(locked, effects=effects)
                # A conclusive transport receipt proves the delivered parts,
                # not that every source question received a useful answer.
                # Record a bounded disposition once, retaining pending sources
                # and their original revision for a customer/manager successor.
                locked.action_receipts = {
                    **(locked.action_receipts or {}),
                    "semantic_delivery_finalized": {
                        "coverage_digest": coverage.get("digest", ""),
                        "disposition": "waiting_on_customer" if waiting_on_customer else "manual",
                        "recorded_at": now.isoformat(),
                    },
                }
                locked.save(update_fields=["action_receipts", "updated_at"])
                park_manual_revision(locked, "semantic_reply_incomplete", now=now)
                return RevisionFinalizationResult(
                    revision_id,
                    reason=("waiting_on_customer" if waiting_on_customer
                            else "semantic_reply_incomplete"),
                    sent_parts=sum(row.state == row.State.SENT for row in effects),
                )
        original_input = (revision.action_receipts or {}).get("input_decision") or {}
        if revision.generation_proposal_digest or original_input.get("origin") == "static_reply":
            followup = settle_revision_normal_followups(revision_id, finalization_token, now=timezone.now())
            if not followup.ready:
                return RevisionFinalizationResult(revision_id, retryable=True, reason=followup.reason)
        with transaction.atomic():
            client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
            locked = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id, claim_token=finalization_token).first()
            if client is None or locked is None:
                return RevisionFinalizationResult(revision_id, retryable=True, reason="finalization_claim_changed")
            completion = complete_revision_from_effects(revision_id, finalization_token)
            if not completion.completed:
                return RevisionFinalizationResult(revision_id, retryable=True, reason=completion.reason)
            # Completion and source acknowledgement commit together. A crash
            # cannot leave terminal revision state with unacknowledged sources.
            _project_completed_sources(revision_id)
            from management.services.ig_response_debt import resolve_delivered_reply_debt

            resolve_delivered_reply_debt(locked)
            IgCustomerTurnRevision.objects.filter(pk=revision_id, state=IgCustomerTurnRevision.State.PROCESSED).update(claim_token="", claimed_at=None, lease_until=None, updated_at=timezone.now())
        return RevisionFinalizationResult(revision_id, True, reason="receipt_finalized", sent_parts=sum(row.state == row.State.SENT for row in effects))
    except Exception as exc:
        # The committed short lease is durable retry debt. No plaintext error,
        # source rewrite, new request graph or physical retransmit is produced.
        return RevisionFinalizationResult(revision_id, retryable=True, reason=f"finalization_{type(exc).__name__.lower()}")


def expired_revision_debt_ids(
    *, now=None, limit=25, owned_only=False, cutover_at=None,
):
    from django.db.models import Case, CharField, Count, F, IntegerField, Subquery, Value, When
    from django.db.models.functions import Cast, Concat, Collate

    now = now or timezone.now()
    source_rows = IgTurnRevisionSource.objects.filter(revision_id=OuterRef("pk"))
    ineligible_sources = source_rows.exclude(
        message__status=InstagramBotMessage.Status.PENDING,
        message__send_state="",
    )
    queue = IgCustomerTurnRevision.objects.annotate(
        has_delivery_effect=Exists(
            IgRevisionDeliveryEffect.objects.filter(revision_id=OuterRef("pk"))
        ),
        has_source=Exists(source_rows),
        has_ineligible_source=Exists(ineligible_sources),
        actual_source_count=Subquery(
            source_rows.order_by().values("revision_id")
            .annotate(total=Count("pk")).values("total")[:1]
        ),
        pending_source_count=Subquery(
            source_rows.filter(message__status=InstagramBotMessage.Status.PENDING, message__send_state="")
            .order_by().values("revision_id").annotate(total=Count("pk")).values("total")[:1]
        ),
    ).filter(
        overall_deadline__lte=now, client__privacy_erasure_started_at__isnull=True,
        state__in=("collecting", "preparing", "sealed", "claimed"),
    ).filter(
        Q(origin="manual_resume")
        | Q(origin__in=("auto_refresh", "outage_recovery"), action_receipts__has_key="manual_resume_authorization")
        | ~Q(turn__terminal_reason__gt="")
    ).filter(
        # A cancelled recovery is terminal only when its worker lease is gone.
        # Older releases could leave the claim token behind after cancellation;
        # admit that bounded cleanup case so it cannot remain technical debt.
        ~Q(recovery_state__in=("waiting", "spawned"))
        | Q(recovery_state="cancelled", lease_until__isnull=False, lease_until__lte=now)
    )
    ordinary = Q(
        origin="inbound",
        turn__claim_state=IgCustomerTurn.ClaimState.OPEN,
        turn__terminal_reason="",
        has_source=True,
        has_ineligible_source=False,
        actual_source_count=F("source_count"),
        pending_source_count=F("source_count"),
    )
    # Ingress receipts retain ownership across cutover/rollback, but do not
    # prove an unsealed source is still owed. Apply the writer's exact source
    # checks before LIMIT so answered/malformed shadows cannot occupy every
    # bounded pass without ever recording a debt case.
    unsealed_sources = ordinary
    source_owned = ordinary & _source_owned_revision_q()
    if cutover_at is not None:
        ordinary &= Q(created_at__gte=cutover_at)
    # A crashed preparation claim retains its pending sources across rollback
    # or cutover. An expired lease does not prove already answered or malformed
    # unsealed sources are owed, so retain the source checks for that admission.
    expired_claim = Q(
        state__in=("preparing", "claimed"),
        lease_until__isnull=False,
        lease_until__lte=now,
    )
    queue = queue.filter(
        _execution_owned_revision_q()
        | Q(origin__in=("manual_resume", "auto_refresh", "outage_recovery"))
        | ordinary
        | source_owned
        | (unsealed_sources & expired_claim)
    )
    # A permission/takeover cancellation releases the execution claim. Once
    # that terminal recovery disposition is persisted, do not rediscover the
    # old sealed row as expired technical debt on every worker cycle.
    queue = queue.exclude(
        recovery_state="cancelled", claim_token="", lease_until__isnull=True,
    )
    if owned_only:
        from management.services.ig_revision_live import _owned_revisions

        queue = queue.filter(
            Q(pk__in=_owned_revisions().values("pk")) | expired_claim
        )
    event_key = Concat(Value("ig-revision-debt:"), Cast(OuterRef("pk"), CharField()))
    if connection.vendor == "mysql":
        event_key = Collate(event_key, "utf8mb4_unicode_ci")
    recorded = IgFollowUpTask.objects.filter(event_key=event_key)
    # A prior pass may already have created the operator debt task but crashed
    # before releasing the revision lease. Re-admit only that expired claim so
    # reconciliation can clear the stale lease; the task's unique event key
    # prevents duplicate operator cases and no provider action is performed.
    debt_recorded = Exists(recorded)
    return list(
        queue.annotate(debt_recorded=debt_recorded)
        .filter(Q(debt_recorded=False) | expired_claim)
        .alias(debt_priority=Case(
            When(expired_claim | Q(generation_proposal_digest__gt="") | Q(has_delivery_effect=True), then=Value(0)),
            default=Value(1), output_field=IntegerField(),
        ))
        .order_by("debt_priority", "overall_deadline", "id")
        .values_list("id", flat=True)[:max(1, min(int(limit), MAX_DUE_SCAN))]
    )


def record_expired_revision_debt(revision_id, *, now=None, cutover_at=None):
    """Classify an expired owed reply once; do not mark it answered or retry HTTP."""
    now = now or timezone.now()
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return "revision_missing"
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id, overall_deadline__lte=now).first()
        if client is None or revision is None or client.privacy_erasure_started_at is not None:
            return "debt_not_applicable"
        effects = list(revision.delivery_effects.order_by("order_index", "id"))
        owned = bool(
            revision.media_prepare_deadline
            or revision.sealed_at
            or revision.snapshot_digest
            or revision.generation_proposal_digest
            or effects
        )
        sticky_origin = revision.origin in {
            "manual_resume", "auto_refresh", "outage_recovery",
        }
        if not owned and not sticky_origin:
            source_owned = IgCustomerTurnRevision.objects.filter(pk=revision.pk).filter(
                _source_owned_revision_q()
            ).exists()
            expired_claim = (
                revision.state in {"preparing", "claimed"}
                and revision.lease_until is not None and revision.lease_until <= now
            )
            turn = IgCustomerTurn.objects.select_for_update().filter(
                pk=revision.turn_id,
                claim_state=IgCustomerTurn.ClaimState.OPEN,
                terminal_reason="",
            ).first()
            sources = list(
                revision.sources.select_for_update().order_by("ordinal", "id")
            )
            source_messages = list(
                InstagramBotMessage.objects.select_for_update()
                .filter(pk__in=[source.message_id for source in sources])
                .order_by("pk")
            )
            messages_by_id = {message.pk: message for message in source_messages}
            if (
                revision.origin != "inbound"
                or (cutover_at is not None and revision.created_at < cutover_at and not (source_owned or expired_claim))
                or turn is None
                or not sources
                or len(sources) != revision.source_count
                or len(source_messages) != revision.source_count
                or any(
                    messages_by_id[source.message_id].status
                    != InstagramBotMessage.Status.PENDING
                    or bool(messages_by_id[source.message_id].send_state)
                    for source in sources
                )
            ):
                return "legacy_shadow_not_owed"
        if effects:
            complete, _debt, reason = _completion_decision(effects)
            if complete and reason == "delivered":
                return "receipt_finalization_pending"
        else:
            graph = GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{revision.pk}").order_by("-pk").first()
            if revision.generation_proposal_digest:
                reason = "proposal_not_dispatched"
            elif graph and graph.terminal_resolution == "succeeded":
                reason = "generation_result_missing"
            elif graph:
                reason = "generation_outcome_unresolved" if not graph.terminal_resolution else "generation_failed"
            else:
                reason = "preparation_expired" if not revision.snapshot_digest else "generation_not_started"
        from management.services.ig_response_debt import record_reply_debt

        record_reply_debt(revision, reason, effects=effects, now=now)
        # The claim has reached a terminal operator-debt disposition. Release
        # the expired worker lease so the same row is not reported as an active
        # technical debt on every health poll; the response-debt task remains
        # the durable operator case and no customer/provider action is retried.
        revision.claim_token = DEBT_PREFIX + secrets.token_hex(16)
        revision.claimed_at = None
        revision.lease_until = None
        revision.save(update_fields=["claim_token", "claimed_at", "lease_until", "updated_at"])
        return reason


__all__ = [
    "RevisionCompletionResult", "RevisionPreparationResult",
    "complete_revision_from_effects", "due_revision_ids", "prepare_next_due_revision",
    "prepare_revision",
    "finalization_due_ids", "finalize_sent_revision_effects",
    "expired_revision_debt_ids", "record_expired_revision_debt",
]
