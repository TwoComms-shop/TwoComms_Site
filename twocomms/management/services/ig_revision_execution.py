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


def _owned_revision_q() -> Q:
    return (
        Q(media_prepare_deadline__isnull=False)
        | Q(sealed_at__isnull=False)
        | Q(snapshot_digest__gt="")
        | Q(generation_proposal_digest__gt="")
        | Q(has_delivery_effect=True)
    )


def _rollout_eligible_q(cutover_at) -> Q:
    sticky = _owned_revision_q() | Q(
        origin__in=("manual_resume", "auto_refresh", "outage_recovery")
    )
    if cutover_at is None:
        return sticky | Q(origin="inbound")
    return sticky | Q(origin="inbound", created_at__gte=cutover_at)


def due_revision_ids(*, now=None, limit: int = 25, cutover_at=None) -> list[int]:
    """Bounded DB-only selector for quiet-due or reclaimable preparation heads."""
    now = now or timezone.now()
    bounded = max(1, min(int(limit), MAX_DUE_SCAN))
    from django.db.models import F

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
    failed = sum(effect.state == state.DEFINITE_FAILED for effect in effects)
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
    groups = {}
    for effect in effects:
        groups.setdefault(effect.group, []).append(effect)
        if (effect.source_message_id not in source_ids
            or effect.recipient_igsid != revision.client.igsid
            or effect.revision_snapshot_digest != revision.snapshot_digest
            or _digest(effect.payload) != effect.payload_digest
            or (effect.state == effect.State.SENT and not effect.provider_message_id)):
            return False, "delivery_receipt_proof_invalid"
    for parts in groups.values():
        if [row.part_index for row in parts] != list(range(len(parts))) or any(row.part_count != len(parts) for row in parts):
            return False, "delivery_parts_proof_invalid"
    return True, reason


def finalization_due_ids(*, now=None, limit=25):
    """Conclusive receipts have their own queue, independent of active head/deadline."""
    now = now or timezone.now()
    from django.db.models import Exists, OuterRef

    owed = IgRevisionDeliveryEffect.objects.filter(revision_id=OuterRef("pk")).exclude(state=IgRevisionDeliveryEffect.State.SENT).exclude(group="template_fallback", state__in=("cancelled", "superseded"))
    candidates = IgCustomerTurnRevision.objects.filter(
        state__in=(IgCustomerTurnRevision.State.CLAIMED, IgCustomerTurnRevision.State.PROCESSED),
        client__privacy_erasure_started_at__isnull=True,
        delivery_effects__state=IgRevisionDeliveryEffect.State.SENT,
    ).annotate(owed_parts=Exists(owed)).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now)).filter(
        ~Q(claim_token__startswith=DEBT_PREFIX) | Q(owed_parts=False)
    ).filter(
        Q(state=IgCustomerTurnRevision.State.CLAIMED)
        | Q(sources__message__status__in=("pending", "processing"))
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

        _project_sent_history(revision_id)
        project_legacy_message(revision_id)
        complete, _debt, aggregate_reason = _completion_decision(effects)
        if not complete or aggregate_reason != "delivered":
            # Known SENT parts become visible even when another part is
            # unresolved. Keep owed state and stop reprojecting unchanged debt.
            IgCustomerTurnRevision.objects.filter(pk=revision_id, claim_token=finalization_token).update(
                claim_token=DEBT_PREFIX + secrets.token_hex(16), claimed_at=None, lease_until=None, updated_at=timezone.now(),
            )
            return RevisionFinalizationResult(revision_id, reason=aggregate_reason, sent_parts=sum(row.state == row.State.SENT for row in effects))
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
            IgCustomerTurnRevision.objects.filter(pk=revision_id, state=IgCustomerTurnRevision.State.PROCESSED).update(claim_token="", claimed_at=None, lease_until=None, updated_at=timezone.now())
        return RevisionFinalizationResult(revision_id, True, reason="receipt_finalized", sent_parts=sum(row.state == row.State.SENT for row in effects))
    except Exception as exc:
        # The committed short lease is durable retry debt. No plaintext error,
        # source rewrite, new request graph or physical retransmit is produced.
        return RevisionFinalizationResult(revision_id, retryable=True, reason=f"finalization_{type(exc).__name__.lower()}")


def expired_revision_debt_ids(
    *, now=None, limit=25, owned_only=False, cutover_at=None,
):
    from django.db.models import CharField, Value
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
    ).filter(
        overall_deadline__lte=now, client__privacy_erasure_started_at__isnull=True,
        state__in=("collecting", "preparing", "sealed", "claimed"),
    ).filter(
        Q(origin="manual_resume")
        | Q(origin__in=("auto_refresh", "outage_recovery"), action_receipts__has_key="manual_resume_authorization")
        | ~Q(turn__terminal_reason__gt="")
    ).exclude(recovery_state__in=("waiting", "spawned", "cancelled"))
    ordinary = Q(
        origin="inbound",
        turn__claim_state=IgCustomerTurn.ClaimState.OPEN,
        turn__terminal_reason="",
        has_source=True,
        has_ineligible_source=False,
    )
    if cutover_at is not None:
        ordinary &= Q(created_at__gte=cutover_at)
    queue = queue.filter(
        _owned_revision_q()
        | Q(origin__in=("manual_resume", "auto_refresh", "outage_recovery"))
        | ordinary
    )
    if owned_only:
        from management.services.ig_revision_live import _owned_revisions

        queue = queue.filter(pk__in=_owned_revisions().values("pk"))
    event_key = Concat(Value("ig-revision-debt:"), Cast(OuterRef("pk"), CharField()))
    if connection.vendor == "mysql":
        event_key = Collate(event_key, "utf8mb4_unicode_ci")
    recorded = IgFollowUpTask.objects.filter(event_key=event_key)
    return list(queue.annotate(debt_recorded=Exists(recorded)).filter(debt_recorded=False).order_by("overall_deadline", "id").values_list("id", flat=True)[:max(1, min(int(limit), MAX_DUE_SCAN))])


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
                or (cutover_at is not None and revision.created_at < cutover_at)
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
        IgFollowUpTask.objects.get_or_create(event_key=f"ig-revision-debt:{revision.pk}", defaults={
            "client": client, "due_at": now, "kind": IgFollowUpTask.Kind.MANAGER_TASK,
            "status": IgFollowUpTask.Status.SKIPPED, "reason": "revision_case:execution_debt",
            "manager_approval_status": IgFollowUpTask.ManagerApprovalStatus.PENDING,
            "manager_approval_requested_at": now, "skip_reason": "execution_recovery_required",
            "trigger": IgFollowUpTask.Trigger.EVENT, "event_occurred_at": revision.overall_deadline,
            "policy_started_at": now, "policy_version": "revision-debt-v1",
            "message_text": "Відповідь потребує перевірки: звірити підтверджені частини та незавершений запит перед відновленням.",
            "event_payload": {"revision_id": revision.pk, "reason": reason, "source_message_ids": list(revision.sources.values_list("message_id", flat=True)), "effect_ids": [row.pk for row in effects]},
            "manager_context": {"case_kind": "revision_execution_debt", "revision_id": revision.pk, "reason": reason, "automatic_http_retry": False},
        })
        return reason


__all__ = [
    "RevisionCompletionResult", "RevisionPreparationResult",
    "complete_revision_from_effects", "due_revision_ids", "prepare_next_due_revision",
    "prepare_revision",
    "finalization_due_ids", "finalize_sent_revision_effects",
    "expired_revision_debt_ids", "record_expired_revision_debt",
]
