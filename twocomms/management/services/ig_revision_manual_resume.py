"""Manual-resume revision successor producer.

The caller owns authorization, settings-before-client locking, pause clearing,
and the single permission-epoch advance.  This module only creates one fresh
execution identity from an already eligible original customer source.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from management.models import (
    AdminAuditLog,
    IgClient,
    IgCustomerTurn,
    IgCustomerTurnRevision,
    IgRevisionDeliveryEffect,
    IgSourceActionReceipt,
    IgTurnMessage,
    IgTurnRevisionSource,
    InstagramBotMessage,
)
from management.services.ig_turn_revisions import (
    MAX_SOURCES,
    OVERALL_DEADLINE_SECONDS,
    QUIET_CAP_SECONDS,
    _create_source_rows,
    _digest,
    _overflow_reason,
    _source_payload,
)


MANUAL_RESUME_ORIGIN = "manual_resume"
MANUAL_RESUME_REASON = "manual_resume"
MANUAL_RESUME_SOURCE_WINDOW = timedelta(hours=23)


@dataclass(frozen=True)
class ManualResumeSuccessorResult:
    revision: IgCustomerTurnRevision | None
    created: bool = False
    reason: str = ""

    @property
    def revision_id(self) -> int | None:
        return self.revision.pk if self.revision is not None else None


def _event_at(source: InstagramBotMessage):
    return source.provider_created_at or source.created_at


def _client_eligible(client: IgClient, *, expected_epoch: int) -> bool:
    return bool(
        int(client.reply_permission_epoch or 0) == int(expected_epoch)
        and not client.bot_paused
        and not client.manager_takeover
        and not client.is_blocked
        and client.hidden_at is None
        and client.privacy_erasure_started_at is None
        and not (
            client.opted_out_at
            and (not client.opted_in_at or client.opted_out_at > client.opted_in_at)
        )
    )


def _substantive_reply_owed(source: InstagramBotMessage) -> bool:
    from management.services.ig_reply_expectation import classify

    return bool(classify(source).substantive_reply_owed)


def _audit_is_current(audit, *, client, epoch: int) -> bool:
    from management.bot_access import (
        OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION,
        has_bot_capability,
    )

    before = audit.before if isinstance(audit.before, dict) else {}
    after = audit.after if isinstance(audit.after, dict) else {}
    return bool(
        audit.actor_id and getattr(audit.actor, "is_active", False)
        and has_bot_capability(audit.actor, OPERATE_IG_BOT_PERMISSION)
        and has_bot_capability(audit.actor, VIEW_IG_CONVERSATION_PII_PERMISSION)
        and audit.action == "ig_bot.manual_resume"
        and audit.entity_type == "IgClient" and audit.entity_id == str(client.pk)
        and (before.get("permission_epoch") if isinstance(before.get("permission_epoch"), int) else -1) == epoch - 1
        and bool(before.get("bot_paused") or before.get("manager_takeover"))
        and int(after.get("permission_epoch") or -1) == epoch
        and after.get("bot_paused") is False and after.get("manager_takeover") is False
    )


def manual_resume_authority_current(revision: IgCustomerTurnRevision) -> bool:
    receipt = (revision.action_receipts or {}).get("manual_resume_authorization")
    if not isinstance(receipt, dict):
        return False
    required = {"audit_id", "actor_id", "client_id", "source_message_id", "permission_epoch"}
    if set(receipt) != required:
        return False
    if revision.origin in {"auto_refresh", "outage_recovery"}:
        from management.services.ig_revision_recovery import recovery_lineage_for_authority

        lineage, reason = recovery_lineage_for_authority(revision)
        if reason or not lineage or lineage[-1].origin != MANUAL_RESUME_ORIGIN:
            return False
        if any(
            (row.action_receipts or {}).get("manual_resume_authorization") != receipt
            or row.permission_epoch != revision.permission_epoch
            for row in lineage
        ):
            return False
    elif revision.origin != MANUAL_RESUME_ORIGIN:
        return False
    client = IgClient.objects.filter(pk=revision.client_id).first()
    audit = AdminAuditLog.objects.filter(pk=receipt["audit_id"]).first()
    return bool(
        client and audit and audit.actor_id == receipt["actor_id"]
        and int(receipt["client_id"]) == client.pk
        and int(receipt["permission_epoch"]) == int(revision.permission_epoch or 0)
        and _client_eligible(client, expected_epoch=int(receipt["permission_epoch"]))
        and revision.sources.filter(message_id=receipt["source_message_id"]).exists()
        and _audit_is_current(audit, client=client, epoch=int(receipt["permission_epoch"]))
    )


def _matching_existing(client, source_id: int, epoch: int):
    candidates = list(
        IgCustomerTurnRevision.objects.select_for_update()
        .filter(
            client=client,
            origin=MANUAL_RESUME_ORIGIN,
            successor_reason=MANUAL_RESUME_REASON,
            permission_epoch=epoch,
        )
        .order_by("revision", "id")[:2]
    )
    matches = [
        row for row in candidates
        if row.sources.filter(message_id=source_id).count() == 1
    ]
    if len(matches) == 1:
        return matches[0], "existing_successor"
    if len(matches) > 1:
        return None, "manual_successor_conflict"
    return None, ""


def _unsafe_effects(revision) -> bool:
    if revision is None:
        return False
    return revision.delivery_effects.filter(
        state__in=(
            IgRevisionDeliveryEffect.State.SENT,
            IgRevisionDeliveryEffect.State.PROVIDER_STARTED,
            IgRevisionDeliveryEffect.State.UNKNOWN,
        )
    ).exists()


def create_manual_resume_successor(
    client: IgClient,
    *,
    settings_obj,
    audit_id: int,
    source_message_id: int,
    now=None,
) -> ManualResumeSuccessorResult:
    """Create at most one fresh manual successor for an unresolved source.

    ``client`` must already be locked by the authorized resume path
    after its one epoch advance.  The caller supplies a settings snapshot so
    this producer never reverses the required settings-before-client lock
    ordering.
    """
    now = now or timezone.now()
    if not getattr(client, "pk", None) or not int(source_message_id or 0):
        return ManualResumeSuccessorResult(None, reason="manual_inputs_missing")
    if not getattr(settings_obj, "pk", None) or not getattr(settings_obj, "is_enabled", False):
        return ManualResumeSuccessorResult(None, reason="manual_settings_invalid")

    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=client.pk).first()
        source = (
            InstagramBotMessage.objects.select_for_update()
            .filter(pk=source_message_id, client_id=client.pk if client else None, role=InstagramBotMessage.Role.USER)
            .first()
        )
        if client is None or source is None:
            return ManualResumeSuccessorResult(None, reason="manual_source_missing")
        epoch = int(client.reply_permission_epoch or 0)
        if not _client_eligible(client, expected_epoch=epoch):
            return ManualResumeSuccessorResult(None, reason="manual_client_ineligible")
        event_at = _event_at(source)
        if event_at is None or event_at < now - MANUAL_RESUME_SOURCE_WINDOW:
            return ManualResumeSuccessorResult(None, reason="manual_source_window_expired")
        if InstagramBotMessage.objects.filter(
            client_id=client.pk, role=InstagramBotMessage.Role.USER, id__gt=source.pk
        ).exists():
            return ManualResumeSuccessorResult(None, reason="manual_newer_inbound")
        if InstagramBotMessage.objects.filter(
            client_id=client.pk, role=InstagramBotMessage.Role.MANAGER, id__gt=source.pk
        ).exclude(status=InstagramBotMessage.Status.FAILED).exists():
            return ManualResumeSuccessorResult(None, reason="manual_manager_answered")
        if InstagramBotMessage.objects.filter(
            client_id=client.pk, role=InstagramBotMessage.Role.MODEL, id__gt=source.pk
        ).exclude(status=InstagramBotMessage.Status.FAILED).filter(
            Q(provider_message_id__gt="") | Q(send_state="sent")
        ).exists():
            return ManualResumeSuccessorResult(None, reason="manual_model_answered")
        membership = (
            IgTurnMessage.objects.select_for_update().select_related("turn")
            .filter(message_id=source.pk).first()
        )
        if membership is None:
            return ManualResumeSuccessorResult(None, reason="manual_turn_missing")
        turn = IgCustomerTurn.objects.select_for_update().filter(
            pk=membership.turn_id, client_id=client.pk
        ).first()
        if turn is None:
            return ManualResumeSuccessorResult(None, reason="manual_turn_missing")
        turn_sources = list(
            InstagramBotMessage.objects.select_for_update()
            .filter(turn_membership__turn_id=turn.pk, role=InstagramBotMessage.Role.USER)
            .order_by("turn_membership__ordinal", "turn_membership__id")[:MAX_SOURCES + 1]
        )
        if not turn_sources or not any(_substantive_reply_owed(item) for item in turn_sources):
            return ManualResumeSuccessorResult(None, reason="manual_pure_action_no_reply")
        if len(turn_sources) > MAX_SOURCES:
            return ManualResumeSuccessorResult(None, reason="manual_source_capacity_exceeded")
        from management.services.instagram_bot import ingress_provider_namespace
        expected_namespace = ingress_provider_namespace(settings_obj)
        if not expected_namespace or any(
            item.client_id != client.pk
            or item.sender_id != client.igsid
            or item.provider_namespace != expected_namespace
            for item in turn_sources
        ):
            return ManualResumeSuccessorResult(None, reason="manual_source_namespace_invalid")
        if turn.claim_state in {turn.ClaimState.CLAIMED, turn.ClaimState.SUPERSEDED}:
            return ManualResumeSuccessorResult(None, reason="manual_turn_not_eligible")
        if turn.terminal_reason in {turn.TerminalReason.REPLIED, turn.TerminalReason.SEND_UNKNOWN}:
            return ManualResumeSuccessorResult(None, reason="manual_turn_delivery_crossed")

        existing, existing_reason = _matching_existing(client, source.pk, epoch)
        if existing is not None or existing_reason:
            return ManualResumeSuccessorResult(existing, created=False, reason=existing_reason)

        audit = AdminAuditLog.objects.select_for_update().filter(pk=audit_id).first()
        if audit is None or not _audit_is_current(audit, client=client, epoch=epoch):
            return ManualResumeSuccessorResult(None, reason="manual_audit_invalid")

        source_revision = (
            IgCustomerTurnRevision.objects.select_for_update()
            .filter(client=client, turn=turn, sources__message_id=source.pk)
            .order_by("-revision", "-id").first()
        )
        head = (
            IgCustomerTurnRevision.objects.select_for_update()
            .filter(client=client, active_slot=1).first()
        )
        if head is not None and (source_revision is None or head.pk != source_revision.pk):
            return ManualResumeSuccessorResult(None, reason="manual_newer_head_exists")
        if _unsafe_effects(source_revision):
            return ManualResumeSuccessorResult(None, reason="manual_delivery_reconciliation_required")

        turn_source_ids = [item.pk for item in turn_sources]
        if IgRevisionDeliveryEffect.objects.filter(
            revision__client=client,
            revision__sources__message_id__in=turn_source_ids,
            state__in=(
                IgRevisionDeliveryEffect.State.SENT,
                IgRevisionDeliveryEffect.State.PROVIDER_STARTED,
                IgRevisionDeliveryEffect.State.UNKNOWN,
            ),
        ).exists():
            return ManualResumeSuccessorResult(None, reason="manual_delivery_reconciliation_required")

        source_rows = list(
            source_revision.sources.select_for_update().order_by("ordinal", "id")[:MAX_SOURCES + 1]
        ) if source_revision is not None else []
        sealed = bool(
            source_revision
            and source_revision.snapshot_digest
            and _digest(source_revision.bundle_snapshot) == source_revision.snapshot_digest
            and source_rows
        )
        if source_revision is not None and (
            source_revision.snapshot_digest or source_revision.bundle_snapshot
        ) and not sealed:
            return ManualResumeSuccessorResult(None, reason="manual_snapshot_invalid")
        if source_rows and (
            len(source_rows) > MAX_SOURCES
            or len(source_rows) != source_revision.source_count
            or [row.message_id for row in source_rows] != turn_source_ids
            or any(row.role != "user" or row.source_namespace != expected_namespace for row in source_rows)
        ):
            return ManualResumeSuccessorResult(None, reason="manual_source_envelopes_invalid")
        if sealed:
            snapshot_sources = source_revision.bundle_snapshot.get("sources")
            if (
                not isinstance(snapshot_sources, list)
                or len(snapshot_sources) != len(source_rows)
                or len(source_rows) != source_revision.source_count
                or any(
                    item.get("message_id") != row.message_id
                    or item.get("ordinal") != row.ordinal
                    or item.get("source_digest") != row.source_digest
                    for item, row in zip(snapshot_sources, source_rows, strict=True)
                )
            ):
                return ManualResumeSuccessorResult(None, reason="manual_snapshot_invalid")
        payloads = []
        if source_rows:
            payloads = [
                {
                    "message_id": row.message_id, "ordinal": row.ordinal, "role": row.role,
                    "source_namespace": row.source_namespace, "provider_message_id": row.provider_message_id,
                    "synthetic_event_key": row.synthetic_event_key, "text": row.text,
                    "provider_created_at": row.provider_created_at.isoformat() if row.provider_created_at else "",
                    "reply_to_provider_message_id": row.reply_to_provider_message_id,
                    "quick_reply_payload": row.quick_reply_payload, "referral": row.referral,
                    "discovered_media": row.discovered_media, "text_chars": row.text_chars,
                    "media_part_count": row.media_part_count, "source_digest": row.source_digest,
                }
                for row in source_rows
            ]
        else:
            payloads = [
                _source_payload(item, ordinal=index)
                for index, item in enumerate(turn_sources, start=1)
            ]
        overflow_reason = _overflow_reason(payloads)
        if overflow_reason:
            return ManualResumeSuccessorResult(None, reason="manual_" + overflow_reason)
        if source_revision is not None:
            source_revision.delivery_effects.select_for_update().filter(
                state__in=(
                    IgRevisionDeliveryEffect.State.PLANNED,
                    IgRevisionDeliveryEffect.State.CLAIMED,
                )
            ).update(
                state=IgRevisionDeliveryEffect.State.CANCELLED,
                failure_code="manual_resume", terminal_at=now,
                claim_token="", lease_until=None,
            )
            source_revision.active_slot = None
            source_revision.state = source_revision.State.SUPERSEDED
            source_revision.claim_token = ""
            source_revision.claimed_at = None
            source_revision.lease_until = None
            source_revision.save(update_fields=[
                "active_slot", "state", "claim_token", "claimed_at", "lease_until", "updated_at",
            ])

        latest_number = int(
            IgCustomerTurnRevision.objects.filter(client=client).order_by("-revision")
            .values_list("revision", flat=True).first() or 0
        )
        deadline = now + timedelta(seconds=OVERALL_DEADLINE_SECONDS)
        child = IgCustomerTurnRevision.objects.create(
            client=client, turn=turn, parent=source_revision,
            revision=latest_number + 1, origin=MANUAL_RESUME_ORIGIN,
            successor_reason=MANUAL_RESUME_REASON, active_slot=1,
            state=IgCustomerTurnRevision.State.SEALED if sealed else IgCustomerTurnRevision.State.COLLECTING,
            quiet_started_at=now, quiet_deadline=now,
            quiet_cap_at=now + timedelta(seconds=QUIET_CAP_SECONDS), overall_deadline=deadline,
            source_count=len(payloads), text_chars=sum(item["text_chars"] for item in payloads),
            media_part_count=sum(item["media_part_count"] for item in payloads),
            permission_epoch=epoch, erasure_started_at_snapshot=client.privacy_erasure_started_at,
            bundle_snapshot=source_revision.bundle_snapshot if sealed else {},
            snapshot_digest=source_revision.snapshot_digest if sealed else "",
            sealed_at=now if sealed else None,
            action_receipts={
                "manual_resume_authorization": {
                    "audit_id": int(audit.pk), "actor_id": int(audit.actor_id),
                    "client_id": int(client.pk), "source_message_id": int(source.pk),
                    "permission_epoch": epoch,
                }
            },
        )
        _create_source_rows(child, payloads)
        return ManualResumeSuccessorResult(child, created=True, reason="created")
