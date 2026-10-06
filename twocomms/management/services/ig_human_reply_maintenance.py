"""Bounded human delivery housekeeping with no provider I/O or retry authority."""
from collections import Counter

from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import CharField, Exists, OuterRef, Value
from django.db.models.functions import Cast, Concat, Replace
from django.utils import timezone

from management.ig_bot_models import HumanReplyCommand
from management.ig_human_reply_models import HumanReplyPart, human_payload_digest
from management.models import AdminAuditLog, IgClient, IgFollowUpTask, InstagramBotMessage
from management.services.ig_human_reply import HUMAN_REPLY_UNKNOWN_REASON
from management.services.ig_human_reply_delivery import (
    _part_rows, _valid_plan, classify_human_receipt, human_reaper_candidates, reap_human_part,
)
from management.services.ig_human_reply_transport import (
    HumanReceiptCheckpointError, ensure_planned_human_reconciliation, project_human_part_receipt,
)

MAINTENANCE_LIMIT = 25
RECEIPT_RESOLUTION_VERSION = "human-receipt-resolution.v1"
RECEIPT_AUDIT_ACTION = "ig_human_reply_receipts_reconciled"


def _part_key(part):
    return f"human:{part.operation_id_snapshot}:part:{part.ordinal}"


def _exact_int(value, expected):
    return type(value) is int and value == expected


def _same_transcript(message, part):
    return bool(message and message.client_id == part.client_id
        and message.sender_id == part.recipient_igsid and message.provider_namespace == part.provider_namespace
        and message.provider_message_id == part.provider_message_id and message.text == part.text
        and message.role == "manager" and message.source == "human_reply"
        and message.status == "done" and message.send_state == "sent"
        and message.delivery_provider_message_ids == [part.provider_message_id]
        and message.delivery_planned_chunk_count == 1 and message.delivery_delivered_chunk_count == 1)


def close_planned_human_reconciliation(command_id, *, now=None):
    """Close an existing case from exact receipts, not an operator assertion.

    Lock ordering matches projection and the operator resolver: client, command,
    parts, task. Historical original receipts may reconcile after reset/epoch
    changes; erasure never creates new transcript evidence or a resolution.
    """
    checked_at = now or timezone.now()
    with transaction.atomic():
        identity = HumanReplyCommand.objects.filter(pk=command_id).values("client_id").first()
        if identity is None:
            return {"ok": False, "reason": "human_command_missing"}
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        if client is None or client.privacy_erasure_started_at or client.hidden_at:
            return {"ok": False, "reason": "client_unavailable"}
        command = HumanReplyCommand.objects.select_for_update().filter(
            pk=command_id, client_id=client.pk).first()
        if command is None:
            return {"ok": False, "reason": "human_command_missing"}
        parts = _part_rows(command)
        task = IgFollowUpTask.objects.select_for_update().filter(
            event_key=f"human-reply-unknown:{command.pk}").first()
        if task is None:
            return {"ok": False, "reason": "human_reconciliation_missing"}
        if not _valid_plan(command, parts, current_owner=False):
            return {"ok": False, "reason": "human_plan_changed"}
        if command.state != command.State.SENT or any(
                part.state != part.State.SENT or not part.provider_started_at
                or classify_human_receipt(http_status=200, provider_message_id=part.provider_message_id).state != part.State.SENT
                for part in parts):
            return {"ok": False, "reason": "human_receipts_incomplete"}
        mids = [part.provider_message_id for part in parts]
        if command.provider_message_ids != mids or len(set(mids)) != len(mids):
            return {"ok": False, "reason": "human_receipt_aggregate_conflict"}
        payload = task.event_payload if isinstance(task.event_payload, dict) else {}
        context = task.manager_context if isinstance(task.manager_context, dict) else {}
        case_parts = payload.get("parts")
        if (task.client_id != command.client_id or task.kind != IgFollowUpTask.Kind.MANAGER_TASK
                or task.reason != HUMAN_REPLY_UNKNOWN_REASON
                or context.get("case_kind") != "human_reply_delivery_unknown"
                or not _exact_int(context.get("command_id"), command.pk) or context.get("operation_id") != str(command.operation_id)
                or not _exact_int(context.get("actor_id"), parts[0].actor_id_snapshot)
                or not _exact_int(context.get("source_message_id"), parts[0].context_message_id_snapshot)
                or payload.get("origin") != "human_reply" or payload.get("source") != "human_reply_command"
                or not _exact_int(payload.get("command_id"), command.pk) or payload.get("operation_id") != str(command.operation_id)
                or payload.get("provider_namespace") != command.provider_namespace
                or not _exact_int(payload.get("source_message_id"), parts[0].context_message_id_snapshot)
                or not _exact_int(payload.get("part_count"), len(parts)) or not isinstance(case_parts, list)
                or len(case_parts) != len(parts)):
            return {"ok": False, "reason": "human_reconciliation_scope_changed"}
        for recorded, part in zip(case_parts, parts):
            if (not isinstance(recorded, dict) or not _exact_int(recorded.get("part_id"), part.pk)
                    or not _exact_int(recorded.get("part_index"), part.ordinal) or recorded.get("payload_digest") != part.payload_digest
                    or recorded.get("provider_message_id") not in {None, "", part.provider_message_id}):
                return {"ok": False, "reason": "human_reconciliation_part_conflict"}
        messages = {row.send_idempotency_key: row for row in InstagramBotMessage.objects.select_for_update().filter(
            send_idempotency_key__in=[_part_key(part) for part in parts])}
        if any(not _same_transcript(messages.get(_part_key(part)), part) for part in parts):
            return {"ok": False, "reason": "human_transcript_unconfirmed"}
        proof = {"version": RECEIPT_RESOLUTION_VERSION, "command_id": command.pk,
            "operation_id": str(command.operation_id), "provider_namespace": command.provider_namespace,
            "client_id": command.client_id, "actor_id": parts[0].actor_id_snapshot,
            "source_message_id": parts[0].context_message_id_snapshot,
            "permission_epoch": parts[0].permission_epoch, "plan_digest": parts[0].plan_digest,
            "parts": [{"part_id": part.pk, "ordinal": part.ordinal, "provider_message_id": part.provider_message_id,
                       "transcript_message_id": messages[_part_key(part)].pk, "payload_digest": part.payload_digest}
                      for part in parts]}
        digest = human_payload_digest(proof)
        existing = context.get("receipt_resolution")
        if task.status == IgFollowUpTask.Status.COMPLETED:
            if isinstance(context.get("resolution"), dict):
                return {"ok": True, "idempotent": True, "operator_resolution_preserved": True, "task_id": task.pk}
            if isinstance(existing, dict) and existing.get("proof_digest") == digest:
                return {"ok": True, "idempotent": True, "task_id": task.pk}
            return {"ok": False, "reason": "human_reconciliation_resolution_conflict"}
        if task.status not in {IgFollowUpTask.Status.SKIPPED, IgFollowUpTask.Status.PENDING}:
            return {"ok": False, "reason": "human_reconciliation_not_open"}
        if existing is not None or context.get("resolution") is not None:
            return {"ok": False, "reason": "human_reconciliation_resolution_conflict"}
        before = {"status": task.status, "command_id": command.pk}
        task.status = IgFollowUpTask.Status.COMPLETED
        task.skip_reason = "exact_human_part_receipts_reconciled"
        task.manager_context = {**context, "receipt_resolution": {
            **proof, "proof_digest": digest, "at": checked_at.isoformat(), "automatic_provider_retry": False}}
        # Do not invent an approving manager or overwrite operator decisions.
        task.save(update_fields=["status", "skip_reason", "manager_context", "updated_at"])
        AdminAuditLog.objects.create(actor=None, actor_role="system", action=RECEIPT_AUDIT_ACTION,
            entity_type="IgFollowUpTask", entity_id=str(task.pk), before=before,
            after={"status": task.status, "receipt_resolution": task.manager_context["receipt_resolution"]},
            reason="All immutable human parts have exact receipts and matching transcripts")
        return {"ok": True, "idempotent": False, "task_id": task.pk}


def _projection_candidates(limit):
    # UUID storage differs between SQLite/MariaDB. Normalize both sides of the
    # existing idempotency key; no extra ledger/cursor is needed for done parts.
    transcripts = InstagramBotMessage.objects.annotate(
        normalized_key=Replace("send_idempotency_key", Value("-"), Value(""))).filter(
            normalized_key=OuterRef("projection_key"))
    return tuple(HumanReplyPart.objects.filter(state=HumanReplyPart.State.SENT,
        provider_started_at__isnull=False, provider_message_id__isnull=False,
        client__pk__isnull=False,
        client__privacy_erasure_started_at__isnull=True, client__hidden_at__isnull=True)
        .exclude(provider_message_id="").annotate(projection_key=Concat(Value("human:"),
            Replace(Cast("operation_id_snapshot", output_field=CharField()), Value("-"), Value("")),
            Value(":part:"), Cast("ordinal", output_field=CharField())), projected=Exists(transcripts))
        .filter(projected=False).order_by("pk").values_list("pk", flat=True)[:limit])


def _reconcile_command(command_id, checked_at):
    # Refresh UNKNOWN evidence without granting a retry or touching operator
    # outcomes. The closure facade separately checks every original receipt.
    with transaction.atomic():
        identity = HumanReplyCommand.objects.filter(pk=command_id).values("client_id").first()
        if identity is None:
            return {"ok": False, "reason": "human_command_missing"}
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        if client is None or client.privacy_erasure_started_at or client.hidden_at:
            return {"ok": False, "reason": "client_unavailable"}
        command = HumanReplyCommand.objects.select_for_update().filter(pk=command_id, client_id=client.pk).first()
        if command is None:
            return {"ok": False, "reason": "human_command_missing"}
        if command.state == command.State.UNKNOWN:
            _part_rows(command)  # Same lock order before the case owner reads.
            task = ensure_planned_human_reconciliation(command, now=checked_at)
            return {"ok": task is not None, "refreshed": task is not None,
                    "reason": "" if task else "human_plan_changed"}
    return close_planned_human_reconciliation(command_id, now=checked_at)


def maintain_human_reply_delivery(*, now=None, limit=MAINTENANCE_LIMIT):
    """One periodic hook: three independently bounded scans, each at most 25.

    Unstarted reclaim remains PLANNED for an authenticated user's retry. UNKNOWN
    never enters a dispatcher. All writes use existing part/transcript/case owners.
    """
    bounded = max(0, min(int(limit), MAINTENANCE_LIMIT))
    checked_at = now or timezone.now()
    counts = {"expired_seen": 0, "reclaimed": 0, "unknown": 0, "projected": 0,
              "projection_existing": 0, "cases_refreshed": 0, "cases_closed": 0, "reasons": {}}
    reasons, commands = Counter(), set()
    for part_id in human_reaper_candidates(now=checked_at, limit=bounded):
        counts["expired_seen"] += 1
        try:
            result = reap_human_part(part_id, now=checked_at)
            if not result.ready:
                reasons[result.reason or "human_reap_failed"] += 1
                continue
            if result.part.state == HumanReplyPart.State.PLANNED:
                counts["reclaimed"] += 1
            elif result.part.state == HumanReplyPart.State.UNKNOWN:
                counts["unknown"] += 1
                commands.add(result.command.pk)
        except (DatabaseError, ValueError, ValidationError) as exc:
            reasons[type(exc).__name__] += 1
    for part_id in _projection_candidates(bounded):
        try:
            projected = project_human_part_receipt(part_id)
            if projected.reason:
                reasons[projected.reason] += 1
            else:
                counts["projected" if projected.created else "projection_existing"] += 1
        except (DatabaseError, ValueError, ValidationError, HumanReceiptCheckpointError) as exc:
            reasons[type(exc).__name__] += 1
    sent_commands = HumanReplyCommand.objects.filter(pk=OuterRef("event_payload__command_id"), state="sent")
    cases = IgFollowUpTask.objects.filter(kind=IgFollowUpTask.Kind.MANAGER_TASK,
        reason=HUMAN_REPLY_UNKNOWN_REASON, status__in=[IgFollowUpTask.Status.SKIPPED, IgFollowUpTask.Status.PENDING])
    candidates = cases.annotate(receipt_ready=Exists(sent_commands)).order_by("-receipt_ready", "pk").values_list(
        "event_payload", flat=True)[:bounded]
    for payload in candidates:
        command_id = payload.get("command_id") if isinstance(payload, dict) else None
        if type(command_id) is int and command_id > 0:
            commands.add(command_id)
        else:
            reasons["human_reconciliation_scope_changed"] += 1
    # Reaper-created UNKNOWNs join this bounded case pass; no customer send.
    for command_id in sorted(commands):
        try:
            result = _reconcile_command(command_id, checked_at)
            if not result.get("ok"):
                reasons[result.get("reason") or "human_reconciliation_failed"] += 1
            elif result.get("refreshed"):
                counts["cases_refreshed"] += 1
            elif not result.get("idempotent"):
                counts["cases_closed"] += 1
        except (DatabaseError, ValueError, ValidationError, HumanReceiptCheckpointError) as exc:
            reasons[type(exc).__name__] += 1
    counts["reasons"] = dict(reasons)
    return counts
