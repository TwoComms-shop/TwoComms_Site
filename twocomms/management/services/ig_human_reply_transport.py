"""One physical request per immutable human part, with immediate exact receipts."""
from contextlib import contextmanager
from dataclasses import dataclass
import re
from django.db import connection, transaction
from django.utils import timezone

from management.ig_bot_models import HumanReplyCommand
from management.ig_human_reply_models import HumanReplyPart
from management.models import IgClient, IgFollowUpTask, InstagramBotMessage, InstagramBotSettings
from management.services.ig_delivery_plan import DEFAULT_MAX_CHUNKS, build_delivery_plan
from management.services.ig_human_reply_delivery import (
    PLAN_KEY, _part_rows, _valid_plan, check_human_part_boundary,
    claim_next_human_part, fail_human_part_before_request, halt_human_delivery_invalid_plan, record_human_part_started,
    settle_human_part, stop_human_delivery_tail,
)


class HumanReceiptCheckpointError(RuntimeError):
    pass


@dataclass(frozen=True)
class HumanBoundaryDecision:
    allowed: bool
    reason: str = ""

    def __bool__(self):
        return self.allowed


@dataclass(frozen=True)
class HumanProjectionResult:
    message: object = None
    created: bool = False
    reason: str = ""


def has_planned_human_delivery(command):
    """A malformed plan still belongs to this lane; never fall through to legacy."""
    return (isinstance(command.operation_context, dict) and PLAN_KEY in command.operation_context
        or HumanReplyPart.objects.filter(command_id=command.pk).exists())


def _transport_namespace_reason(transport_settings, namespace):
    from management.services.instagram_bot import ingress_provider_namespace
    if transport_settings is None or ingress_provider_namespace(transport_settings) != namespace:
        return "human_transport_namespace_changed"
    return ""


@contextmanager
def human_part_send_boundary(part_id, token, *, transport_settings):
    # Reuse the same physical lock as bot sends/takeover, with human-owned
    # permission checks. Bot pause is expected after authenticated takeover.
    from management.services.ig_reply_boundary import REPLY_PERMISSION_LOCK_FILE
    from management.services.ig_maintenance import _exclusive_file_lock
    with _exclusive_file_lock(REPLY_PERMISSION_LOCK_FILE):
        checked = check_human_part_boundary(part_id, token)
        reason = checked.reason
        if checked.ready:
            reason = _transport_namespace_reason(transport_settings, checked.part.provider_namespace)
        yield HumanBoundaryDecision(checked.ready and not reason, reason)


@contextmanager
def human_part_request_boundary(part_id, token, *, delivered_chunk_count, provider_message_ids, planned_chunk_count):
    # Every prepared part is exactly one immutable provider request. There is
    # no replacement, URL fallback, additional chunk, or manager bot admission.
    if planned_chunk_count != 1 or delivered_chunk_count != 0 or provider_message_ids:
        yield HumanBoundaryDecision(False, "human_physical_plan_changed")
        return
    checked = check_human_part_boundary(part_id, token, require_started=True)
    yield HumanBoundaryDecision(checked.ready, checked.reason)


def project_human_part_receipt(part_id):
    """Historical exact receipt -> one namespace-bound transcript; no resend."""
    with transaction.atomic():
        identity = HumanReplyPart.objects.filter(pk=part_id).values("client_id", "command_id").first()
        if identity is None:
            return HumanProjectionResult(reason="human_part_missing")
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        if client is None or client.privacy_erasure_started_at or client.hidden_at:
            return HumanProjectionResult(reason="client_unavailable")
        command = HumanReplyCommand.objects.select_for_update().filter(pk=identity["command_id"]).first()
        if command is None:
            return HumanProjectionResult(reason="human_command_missing")
        rows = _part_rows(command)
        part = next((row for row in rows if row.pk == part_id), None)
        if (part is None or part.state != part.State.SENT or not part.provider_message_id
            or not part.provider_started_at or not _valid_plan(command, rows, current_owner=False)):
            return HumanProjectionResult(reason="human_receipt_unconfirmed")
        key = f"human:{part.operation_id_snapshot}:part:{part.ordinal}"
        message = InstagramBotMessage.objects.select_for_update().filter(send_idempotency_key=key).first()
        if message is not None:
            if (message.client_id != part.client_id or message.sender_id != part.recipient_igsid
                or message.provider_namespace != part.provider_namespace or message.provider_message_id != part.provider_message_id
                or message.text != part.text or message.role != "manager" or message.source != "human_reply"
                or message.status != "done" or message.send_state != "sent"
                or message.delivery_provider_message_ids != [part.provider_message_id]):
                return HumanProjectionResult(reason="human_transcript_conflict")
            return HumanProjectionResult(message)
        finished = part.terminal_at or timezone.now()
        message = InstagramBotMessage.objects.create(client=client, sender_id=part.recipient_igsid,
            provider_namespace=part.provider_namespace, role="manager", source="human_reply", text=part.text,
            status="done", send_state="sent", send_idempotency_key=key,
            provider_message_id=part.provider_message_id, delivery_provider_message_ids=[part.provider_message_id],
            delivery_planned_chunk_count=1, delivery_delivered_chunk_count=1,
            processing_started_at=part.provider_started_at, send_started_at=part.provider_started_at,
            send_completed_at=finished, processed_at=finished)
        if command.reply_message_id is None:
            command.reply_message = message
            command.save(update_fields=["reply_message", "updated_at"])
        from management.services.instagram_bot import _enqueue_memory_source_event
        transaction.on_commit(lambda message_id=message.pk: _enqueue_memory_source_event(message_id))
        return HumanProjectionResult(message, True)


def checkpoint_human_part_receipt(part_id, token, *, provider_namespace, provider_message_id, now=None):
    """Commit part, transcript, and one memory enqueue together after exact MID."""
    with transaction.atomic():
        identity = HumanReplyPart.objects.filter(pk=part_id).values("client_id").first()
        if identity is None:
            raise HumanReceiptCheckpointError("human_part_missing")
        # Match projection/store lock order; receipt settlement never grants IO.
        IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        result = settle_human_part(part_id, token, provider_namespace=provider_namespace,
            http_status=200, provider_message_id=provider_message_id, now=now)
        if not result.ready:
            raise HumanReceiptCheckpointError(result.reason)
        projected = project_human_part_receipt(part_id)
        if projected.reason and projected.reason != "client_unavailable":
            raise HumanReceiptCheckpointError(projected.reason)

        def reconcile_early_echoes():
            from management.services.ig_revision_echo_integration import reconcile_human_part_echoes
            return reconcile_human_part_echoes(part_id)

        # Echo reconciliation may lock settings first. It must execute after
        # receipt/projection commit, and failure cannot undo that exact receipt.
        transaction.on_commit(reconcile_early_echoes, robust=True)
        return result


def ensure_planned_human_reconciliation(command, *, now=None):
    """Immutable original case; fresh ledger observation stays in private metadata."""
    from management.services.ig_human_reply import HUMAN_REPLY_UNKNOWN_REASON
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=command.client_id).first()
        if client is None:
            return None
        command = HumanReplyCommand.objects.select_for_update().filter(pk=command.pk, client_id=client.pk).first()
        if command is None or command.state != command.State.UNKNOWN:
            return None
        rows = _part_rows(command)
        if not _valid_plan(command, rows, current_owner=False):
            return None
        checked_at = now or timezone.now()
        occurred = command.provider_started_at or command.terminal_at or checked_at
        payload = dict(origin="human_reply", source="human_reply_command", command_id=command.pk,
            operation_id=str(command.operation_id), source_message_id=rows[0].context_message_id_snapshot,
            reply_message_id=command.reply_message_id, part_count=len(rows), provider_namespace=rows[0].provider_namespace,
            provider_message_ids=list(command.provider_message_ids or []),
            parts=[dict(part_id=part.pk, part_index=part.ordinal, state=part.state,
                payload_digest=part.payload_digest, provider_message_id=part.provider_message_id or "") for part in rows])
        task, _ = IgFollowUpTask.objects.get_or_create(event_key=f"human-reply-unknown:{command.pk}", defaults=dict(
            client_id=command.client_id, due_at=command.terminal_at or checked_at, status=IgFollowUpTask.Status.SKIPPED,
            kind=IgFollowUpTask.Kind.MANAGER_TASK, reason=HUMAN_REPLY_UNKNOWN_REASON,
            manager_approval_status=IgFollowUpTask.ManagerApprovalStatus.PENDING,
            manager_approval_requested_at=command.terminal_at or checked_at,
            skip_reason="manual_delivery_reconciliation_required", trigger=IgFollowUpTask.Trigger.EVENT,
            event_occurred_at=occurred, policy_started_at=occurred, policy_version="human-reply-parts-v1",
            message_text="Звірте ручну відповідь з Meta Inbox: результат доставки невідомий, автоматичний повтор заборонено.",
            event_payload=payload, manager_context=dict(case_kind="human_reply_delivery_unknown", command_id=command.pk,
                operation_id=str(command.operation_id), actor_id=rows[0].actor_id_snapshot,
                source_message_id=rows[0].context_message_id_snapshot, reply_message_id=command.reply_message_id,
                disposition="reconcile_delivery", automatic_http_retry=False)))
        task = IgFollowUpTask.objects.select_for_update().get(pk=task.pk)
        original = task.event_payload if isinstance(task.event_payload, dict) else {}
        context = task.manager_context if isinstance(task.manager_context, dict) else {}
        proof_parts = original.get("parts")
        if (task.client_id != command.client_id or task.kind != IgFollowUpTask.Kind.MANAGER_TASK
            or task.reason != HUMAN_REPLY_UNKNOWN_REASON or original.get("command_id") != command.pk
            or context.get("command_id") != command.pk or original.get("operation_id") != str(command.operation_id)
            or context.get("operation_id") != str(command.operation_id)
            or original.get("source_message_id") != rows[0].context_message_id_snapshot
            or original.get("provider_namespace") != rows[0].provider_namespace
            or not isinstance(proof_parts, list) or len(proof_parts) != len(rows)
            or any(not isinstance(proof, dict) or proof.get("part_id") != part.pk
                or proof.get("part_index") != part.ordinal or proof.get("payload_digest") != part.payload_digest
                for proof, part in zip(proof_parts, rows))):
            raise HumanReceiptCheckpointError("human_reconciliation_identity_conflict")
        observation = dict(schema_version="human-delivery-observation.v1", command_id=command.pk,
            operation_id=str(command.operation_id), provider_namespace=rows[0].provider_namespace,
            plan_digest=rows[0].plan_digest, state=command.state, reply_message_id=command.reply_message_id,
            provider_message_ids=list(command.provider_message_ids or []), parts=payload["parts"])
        if context.get("latest_delivery") != observation:
            task.manager_context = {**context, "latest_delivery": observation}
            task.save(update_fields=["manager_context", "updated_at"])
        return task


def _current_command(command_id):
    return HumanReplyCommand.objects.filter(pk=command_id).first()


def _preserve_cancellation_reason(command_id, reason):
    """Expose the verified no-request boundary cause, never provider/raw text."""
    if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
        return
    with transaction.atomic():
        command = HumanReplyCommand.objects.select_for_update().filter(pk=command_id).first()
        if command is not None and command.state == command.State.CANCELLED and command.provider_started_at is None:
            command.failure_code = reason
            command.save(update_fields=["failure_code", "updated_at"])


def dispatch_planned_human_reply(command_id, *, now=None):
    """Bounded part pipeline; UNKNOWN/started/terminal attempts cannot post again."""
    from management.services.instagram_bot import send_text
    from management.services.ig_human_reply import HumanReplyRejected
    if connection.in_atomic_block and any(not getattr(block, "_from_testcase", False) for block in connection.atomic_blocks):
        raise HumanReplyRejected("human_dispatch_requires_commit")
    command = _current_command(command_id)
    if command is None:
        return None
    if command.state == command.State.UNKNOWN:
        ensure_planned_human_reconciliation(command, now=now)
        return command
    for _ in range(DEFAULT_MAX_CHUNKS):
        claim = claim_next_human_part(command_id, now=now)
        if not claim.ready:
            if claim.reason not in {"human_command_terminal", "human_part_already_claimed", "human_provider_result_unresolved", "human_part_not_claimable"}:
                stopped = stop_human_delivery_tail(command_id, reason=claim.reason, now=now)
                if not stopped.ready and stopped.reason == "human_plan_changed":
                    halt_human_delivery_invalid_plan(command_id, now=now)
                _preserve_cancellation_reason(command_id, claim.reason)
            break
        part, token = claim.part, claim.token
        local = dict(started=False, checkpointed=False, mid="", boundary_reason="")
        transport_settings = InstagramBotSettings.objects.order_by("pk").first()
        exact = build_delivery_plan(part.text)
        if transport_settings is None or not exact.complete or exact.chunks != (part.text,):
            fail_human_part_before_request(part.pk, token, reason="human_physical_plan_changed", now=now)
            break

        def mark_started():
            reason = _transport_namespace_reason(transport_settings, part.provider_namespace)
            if reason:
                local["boundary_reason"] = reason
                return False
            result = record_human_part_started(part.pk, token)
            local["started"] = result.ready
            if not result.ready:
                local["boundary_reason"] = result.reason
            return result.ready

        @contextmanager
        def permission_boundary():
            with human_part_send_boundary(part.pk, token, transport_settings=transport_settings) as decision:
                if not decision:
                    local["boundary_reason"] = decision.reason
                yield decision

        def checkpoint(mid):
            checkpoint_human_part_receipt(part.pk, token, provider_namespace=part.provider_namespace, provider_message_id=mid)
            local.update(checkpointed=True, mid=mid)

        try:
            receipt = send_text(transport_settings, part.recipient_igsid, part.text, return_receipt=True,
                outgoing_actor="manager", allow_url_fallback=False,
                permission_boundary_factory=permission_boundary,
                provider_io_started_callback=mark_started,
                provider_request_boundary_factory=lambda **kwargs: human_part_request_boundary(part.pk, token, **kwargs),
                provider_message_callback=checkpoint)
        except Exception:
            receipt = None
        physical = HumanReplyPart.objects.filter(pk=part.pk).first()
        if physical is None:
            break
        ids = tuple(getattr(receipt, "provider_message_ids", ()) or ())
        success = (local["checkpointed"] and physical.state == physical.State.SENT
            and bool(getattr(receipt, "ok", False)) and not getattr(receipt, "kind", "")
            and ids == (local["mid"],) and getattr(receipt, "planned_chunk_count", None) == 1
            and getattr(receipt, "delivered_chunk_count", None) == 1
            and getattr(receipt, "request_text", None) == part.text)
        if success:
            continue
        if physical.state == physical.State.SENT:
            stop_human_delivery_tail(command_id, reason="human_transport_after_receipt_failed", now=now)
        elif physical.state == physical.State.PROVIDER_STARTED:
            kind = str(getattr(receipt, "kind", "") or "")
            definite = receipt is not None and kind in {"permanent", "link_restricted"}
            settle_human_part(part.pk, token, provider_namespace=part.provider_namespace,
                transport_outcome="definite_rejection" if definite else "unknown", now=now)
        elif physical.state == physical.State.CLAIMED:
            fail_human_part_before_request(part.pk, token, reason=local["boundary_reason"] or "human_transport_preflight_failed",
                cancelled=getattr(receipt, "kind", "") == "cancelled", now=now)
        if getattr(receipt, "kind", "") == "cancelled":
            _preserve_cancellation_reason(command_id, local["boundary_reason"])
        break
    command = _current_command(command_id)
    if command is not None and command.state == command.State.UNKNOWN:
        ensure_planned_human_reconciliation(command, now=now)
    return command
