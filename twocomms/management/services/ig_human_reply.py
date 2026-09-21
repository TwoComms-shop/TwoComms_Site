"""Actor-aware manual Instagram replies with a durable idempotency boundary."""
from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from management.models import (
    AdminAuditLog,
    IgClient,
    IgFollowUpTask,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.ig_bot_models import HumanReplyCommand


MAX_HUMAN_REPLY_CHARS = 4000
HUMAN_REPLY_UNKNOWN_REASON = "human_reply:delivery_unknown"


class HumanReplyRejected(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _BoundaryDecision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True)
class HumanReplyResult:
    command: HumanReplyCommand
    idempotent: bool = False


@contextmanager
def _human_send_boundary(command_id: int):
    """Recheck the human command/epoch at the physical request edge."""
    with transaction.atomic():
        command = HumanReplyCommand.objects.select_for_update().select_related(
            "client", "actor", "context_message"
        ).get(pk=command_id)
        client = IgClient.objects.select_for_update().get(pk=command.client_id)
        from management.services.instagram_bot import ingress_provider_namespace

        settings_obj = InstagramBotSettings.load()
        namespace = str(ingress_provider_namespace(settings_obj) or "")[:128]
        reason = _command_boundary_reason(
            command,
            client,
            namespace=namespace,
            now=timezone.now(),
            require_provider_started=True,
        )
    yield _BoundaryDecision(not reason, reason)


def _window_deadline(message: InstagramBotMessage):
    from management.services.ig_ai_reply_recovery import RESPONSE_WINDOW

    anchor = message.provider_created_at or message.created_at
    return anchor + RESPONSE_WINDOW if anchor else None


def _latest_inbound(client: IgClient):
    return client.messages.filter(
        role=InstagramBotMessage.Role.USER,
    ).order_by("-id").first()


def _check_actor(actor) -> None:
    from management.bot_access import OPERATE_IG_BOT_PERMISSION, has_bot_capability

    if not has_bot_capability(actor, OPERATE_IG_BOT_PERMISSION):
        raise HumanReplyRejected("actor_not_authorized")


def _command_boundary_reason(
    command,
    client,
    *,
    namespace: str,
    now: datetime,
    require_provider_started: bool = False,
) -> str:
    """Return a fail-closed reason for the immutable human-send boundary."""
    if require_provider_started and command.state != HumanReplyCommand.State.PROVIDER_STARTED:
        return "command_not_provider_started"
    if command.client_id != client.pk or command.recipient_igsid != client.igsid:
        return "recipient_changed"
    if command.provider_namespace != namespace:
        return "provider_namespace_changed"
    if client.reply_permission_epoch != command.permission_epoch:
        return "permission_epoch_changed"
    if client.hidden_at or client.privacy_erasure_started_at:
        return "client_unavailable"
    if client.opted_out_at and (
        not client.opted_in_at or client.opted_out_at > client.opted_in_at
    ):
        return "opted_out"
    if getattr(client, "is_blocked", False):
        return "client_blocked"
    try:
        _check_actor(command.actor)
    except HumanReplyRejected as exc:
        return exc.code
    context = command.context_message or InstagramBotMessage.objects.filter(
        pk=command.context_message_id
    ).first()
    if (
        not context
        or context.client_id != client.pk
        or context.role != InstagramBotMessage.Role.USER
        or context.sender_id != client.igsid
        or (context.provider_namespace and context.provider_namespace != namespace)
    ):
        return "context_changed"
    latest = _latest_inbound(client)
    if not latest or command.context_message_id != latest.pk:
        return "newer_inbound"
    deadline = _window_deadline(latest)
    if (
        deadline is None
        or command.window_deadline is None
        or command.window_deadline != deadline
    ):
        return "context_window_changed"
    if command.window_deadline <= now:
        return "reply_window_closed"
    return ""


def _validate_existing_operation(command, *, client_id: int, actor, text: str,
                                 context_message_id: int | None) -> None:
    if (
        command.client_id != int(client_id)
        or command.actor_id != getattr(actor, "pk", None)
        or command.text != text
        or (
            context_message_id is not None
            and command.context_message_id != int(context_message_id)
        )
    ):
        raise HumanReplyRejected("operation_conflict")


def _ensure_unknown_reconciliation(command, *, now: datetime) -> IgFollowUpTask:
    """Create the operator-only reconciliation case for one ambiguous send."""
    from management.services.ig_delivery_plan import build_delivery_plan

    plan = build_delivery_plan(command.text)
    event_key = f"human-reply-unknown:{command.pk}"
    occurred_at = command.provider_started_at or command.terminal_at or now
    payload = {
        "origin": "human_reply",
        "source": "human_reply_command",
        "command_id": command.pk,
        "operation_id": str(command.operation_id),
        "source_message_id": command.context_message_id,
        "reply_message_id": command.reply_message_id,
        "part_count": len(plan.chunks),
        "parts": [
            {
                "part_index": index,
                "payload_digest": hashlib.sha256(part.encode("utf-8")).hexdigest(),
                "provider_message_id": (
                    command.provider_message_ids[index]
                    if index < len(command.provider_message_ids or [])
                    else ""
                ),
            }
            for index, part in enumerate(plan.chunks)
        ],
        "provider_message_ids": list(command.provider_message_ids or []),
        "provider_namespace": command.provider_namespace,
    }
    task, _created = IgFollowUpTask.objects.get_or_create(
        event_key=event_key,
        defaults={
            "client_id": command.client_id,
            "due_at": command.terminal_at or now,
            "status": IgFollowUpTask.Status.SKIPPED,
            "kind": IgFollowUpTask.Kind.MANAGER_TASK,
            "reason": HUMAN_REPLY_UNKNOWN_REASON,
            "manager_approval_status": IgFollowUpTask.ManagerApprovalStatus.PENDING,
            "manager_approval_requested_at": command.terminal_at or now,
            "skip_reason": "manual_delivery_reconciliation_required",
            "trigger": IgFollowUpTask.Trigger.EVENT,
            "event_occurred_at": occurred_at,
            "policy_started_at": occurred_at,
            "policy_version": "human-reply-unknown-v1",
            "message_text": (
                "Звірте ручну відповідь з Meta Inbox: результат доставки "
                "невідомий, автоматичний повтор заборонено."
            ),
            "event_payload": payload,
            "manager_context": {
                "case_kind": "human_reply_delivery_unknown",
                "command_id": command.pk,
                "operation_id": str(command.operation_id),
                "actor_id": command.actor_id,
                "source_message_id": command.context_message_id,
                "reply_message_id": command.reply_message_id,
                "disposition": "reconcile_delivery",
                "automatic_http_retry": False,
            },
        },
    )
    if (
        task.client_id != command.client_id
        or task.kind != IgFollowUpTask.Kind.MANAGER_TASK
        or task.reason != HUMAN_REPLY_UNKNOWN_REASON
        or task.event_payload != payload
    ):
        raise RuntimeError("human reply reconciliation identity mismatch")
    return task


def create_human_reply_command(
    client_id: int,
    *,
    actor,
    text: str,
    operation_id: str | uuid.UUID | None = None,
    context_message_id: int | None = None,
    expected_permission_epoch: int | None = None,
    now: datetime | None = None,
) -> HumanReplyResult:
    """Take over one client and persist one private, idempotent send intent."""
    _check_actor(actor)
    text = str(text or "").strip()
    if not text:
        raise HumanReplyRejected("empty_text")
    if len(text) > MAX_HUMAN_REPLY_CHARS:
        raise HumanReplyRejected("text_too_long")
    try:
        op = uuid.UUID(str(operation_id)) if operation_id else uuid.uuid4()
    except (TypeError, ValueError, AttributeError):
        raise HumanReplyRejected("invalid_operation_id")
    now = now or timezone.now()
    settings_obj = InstagramBotSettings.load()
    from management.services.instagram_bot import ingress_provider_namespace
    namespace = str(ingress_provider_namespace(settings_obj) or "")[:128]
    from management.services.ig_delivery_plan import build_delivery_plan

    if not build_delivery_plan(text).complete:
        raise HumanReplyRejected("delivery_plan_incomplete")

    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=client_id).first()
        if not client:
            raise HumanReplyRejected("client_not_found")

        # The client lock serializes operation lookup, competing commands, and
        # takeover.  A nested savepoint below handles the unique operation race
        # without leaving the outer transaction broken.
        existing = (
            HumanReplyCommand.objects.select_for_update()
            .filter(operation_id=op)
            .first()
        )
        if existing:
            _validate_existing_operation(
                existing,
                client_id=client.pk,
                actor=actor,
                text=text,
                context_message_id=context_message_id,
            )
            return HumanReplyResult(existing, idempotent=True)
        if client.hidden_at or client.privacy_erasure_started_at:
            raise HumanReplyRejected("client_unavailable")
        if client.opted_out_at and (not client.opted_in_at or client.opted_out_at > client.opted_in_at):
            raise HumanReplyRejected("opted_out")
        if getattr(client, "is_blocked", False):
            raise HumanReplyRejected("client_blocked")
        if expected_permission_epoch is not None and int(expected_permission_epoch) != int(client.reply_permission_epoch or 0):
            raise HumanReplyRejected("permission_epoch_changed")
        latest = _latest_inbound(client)
        if not latest:
            raise HumanReplyRejected("no_inbound_context")
        if context_message_id is not None:
            context = client.messages.filter(pk=context_message_id, role=InstagramBotMessage.Role.USER).first()
            if not context:
                raise HumanReplyRejected("context_not_found")
            if client.messages.filter(role=InstagramBotMessage.Role.USER, pk__gt=context.pk).exists():
                raise HumanReplyRejected("newer_inbound")
        else:
            context = latest
        deadline = _window_deadline(latest)
        if deadline is None or deadline <= now:
            raise HumanReplyRejected("reply_window_closed")

        # Keep one actor's active command as the client-level ownership fence.
        # The context filter used here previously allowed a different manager
        # to claim the same client after a newer inbound changed the context.
        active_commands = (
            HumanReplyCommand.objects.select_for_update()
            .filter(
                client=client,
                state__in=[
                    HumanReplyCommand.State.PENDING,
                    HumanReplyCommand.State.CLAIMED,
                    HumanReplyCommand.State.PROVIDER_STARTED,
                    HumanReplyCommand.State.UNKNOWN,
                ],
            )
            .exclude(operation_id=op)
        )
        unresolved = active_commands.filter(
            state=HumanReplyCommand.State.UNKNOWN
        ).first()
        if unresolved:
            if unresolved.actor_id != getattr(actor, "pk", None):
                raise HumanReplyRejected("command_owned_by_other_actor")
            raise HumanReplyRejected("competing_command")

        competing = active_commands.first()
        if competing:
            if competing.actor_id != getattr(actor, "pk", None):
                raise HumanReplyRejected("command_owned_by_other_actor")
            if competing.context_message_id == context.pk:
                raise HumanReplyRejected("competing_command")

        # Confirm takeover before exposing the send button. Manual sends are
        # allowed while bot automation is paused; the epoch fences old workers.
        if not client.manager_takeover or not client.bot_paused:
            client.manager_takeover = True
            client.bot_paused = True
            client.paused_reason = "manager_takeover"
            client.paused_at = now
            client.reply_permission_epoch = int(client.reply_permission_epoch or 0) + 1
            client.save(update_fields=[
                "manager_takeover", "bot_paused", "paused_reason", "paused_at",
                "reply_permission_epoch", "updated_at",
            ])
            try:
                from management.services.ig_permission_transitions import cancel_client_unstarted_automation
                cancel_client_unstarted_automation(client.pk, reason="human_reply_takeover")
            except Exception:
                pass
        epoch = int(client.reply_permission_epoch or 0)
        try:
            with transaction.atomic():
                command = HumanReplyCommand.objects.create(
                    operation_id=op,
                    client=client,
                    actor=actor,
                    context_message=context,
                    recipient_igsid=client.igsid,
                    text=text,
                    provider_namespace=namespace,
                    permission_epoch=epoch,
                    window_deadline=deadline,
                    draft_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    context_revision=str(context.pk),
                    operation_context={
                        "client_id": client.pk,
                        "context_message_id": context.pk,
                        "recipient_igsid": client.igsid,
                        "permission_epoch": epoch,
                        "window_deadline": deadline.isoformat(),
                        "provider_namespace": namespace,
                    },
                )
                AdminAuditLog.objects.create(
                    actor=actor,
                    actor_role="staff",
                    action="ig_bot.human_reply_command_created",
                    entity_type="HumanReplyCommand",
                    entity_id=str(command.pk),
                    before={"bot_paused": False, "manager_takeover": False},
                    after={"bot_paused": True, "manager_takeover": True, "permission_epoch": epoch},
                    reason="authenticated_manual_reply",
                )
        except IntegrityError:
            with transaction.atomic():
                command = HumanReplyCommand.objects.select_for_update().get(operation_id=op)
            _validate_existing_operation(
                command,
                client_id=client.pk,
                actor=actor,
                text=text,
                context_message_id=context.pk,
            )
            return HumanReplyResult(command, idempotent=True)
    return HumanReplyResult(command)


def dispatch_human_reply_command(command_id: int, *, now: datetime | None = None) -> HumanReplyCommand:
    """Claim and send once; an ambiguous provider outcome is terminal UNKNOWN."""
    now = now or timezone.now()
    with transaction.atomic():
        command = HumanReplyCommand.objects.select_for_update().select_related("client").get(pk=command_id)
        if command.state != HumanReplyCommand.State.PENDING:
            if command.state == HumanReplyCommand.State.UNKNOWN:
                _ensure_unknown_reconciliation(command, now=now)
            return command
        client = IgClient.objects.select_for_update().get(pk=command.client_id)
        from management.services.instagram_bot import ingress_provider_namespace

        settings_obj = InstagramBotSettings.load()
        namespace = str(ingress_provider_namespace(settings_obj) or "")[:128]
        reason = _command_boundary_reason(
            command, client, namespace=namespace, now=now
        )
        if reason == "reply_window_closed":
            command.state = HumanReplyCommand.State.DEFINITE_FAILED
            command.failure_code = reason
            command.terminal_at = now
            command.save(update_fields=["state", "failure_code", "terminal_at", "updated_at"])
            return command
        if reason:
            command.state = HumanReplyCommand.State.CANCELLED
            command.failure_code = reason
            command.terminal_at = now
            command.save(update_fields=["state", "failure_code", "terminal_at", "updated_at"])
            return command
        from management.services.ig_delivery_plan import build_delivery_plan

        if not build_delivery_plan(command.text).complete:
            command.state = HumanReplyCommand.State.DEFINITE_FAILED
            command.failure_code = "delivery_plan_incomplete"
            command.terminal_at = now
            command.save(update_fields=["state", "failure_code", "terminal_at", "updated_at"])
            return command
        reply_message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.MANAGER,
            text=command.text,
            status=InstagramBotMessage.Status.PROCESSING,
            source="human_reply",
            send_state="sending",
            send_idempotency_key=f"human:{command.operation_id}",
            processing_started_at=now,
        )
        command.reply_message = reply_message
        command.state = HumanReplyCommand.State.PROVIDER_STARTED
        command.provider_started_at = now
        command.save(update_fields=["reply_message", "state", "provider_started_at", "updated_at"])

    try:
        from management.services.instagram_bot import send_text

        receipt = send_text(
            settings_obj,
            client.igsid,
            command.text,
            return_receipt=True,
            outgoing_actor="manager",
            allow_url_fallback=False,
            permission_boundary_factory=lambda: _human_send_boundary(command.pk),
        )
        ok = bool(getattr(receipt, "ok", False))
        kind = str(getattr(receipt, "kind", "") or "")
        hint = str(getattr(receipt, "hint", "") or "")[:96]
        ids = [str(value)[:255] for value in (getattr(receipt, "provider_message_ids", ()) or ()) if str(value)]
        planned_raw = getattr(receipt, "planned_chunk_count", None)
        delivered_raw = getattr(receipt, "delivered_chunk_count", None)
        planned_count = int(planned_raw or 0)
        delivered_count = int(delivered_raw or 0)
        if ok and (
            kind
            or (
                planned_raw is not None
                and delivered_raw is not None
                and (not planned_count or delivered_count != planned_count)
            )
        ):
            ok, kind, hint = False, "permanent", "delivery_plan_incomplete"
    except Exception as exc:  # provider boundary is unknown after invocation
        ok, kind, hint, ids = False, "unknown", type(exc).__name__, []

    terminal = timezone.now()
    with transaction.atomic():
        command = HumanReplyCommand.objects.select_for_update().get(pk=command.pk)
        message = InstagramBotMessage.objects.select_for_update().get(pk=command.reply_message_id)
        command.provider_message_ids = ids
        command.failure_code = "" if ok else (hint or kind or "delivery_failed")
        command.terminal_at = terminal
        if ok:
            command.state = HumanReplyCommand.State.SENT
            message.status = InstagramBotMessage.Status.DONE
            message.send_state = "sent"
            message.send_completed_at = terminal
            message.processed_at = terminal
            message.provider_message_id = ids[0] if ids else ""
            message.delivery_provider_message_ids = ids
        elif kind == "unknown":
            command.state = HumanReplyCommand.State.UNKNOWN
            message.status = InstagramBotMessage.Status.FAILED
            message.send_state = "unknown"
            message.delivery_failure_boundary = hint or "delivery_unknown"
        elif kind == "cancelled":
            command.state = HumanReplyCommand.State.CANCELLED
            message.status = InstagramBotMessage.Status.FAILED
            message.send_state = "failed"
        else:
            command.state = HumanReplyCommand.State.DEFINITE_FAILED
            message.status = InstagramBotMessage.Status.FAILED
            message.send_state = "failed"
        message.save(update_fields=[
            "status", "send_state", "send_completed_at", "processed_at",
            "provider_message_id", "delivery_provider_message_ids", "delivery_failure_boundary",
        ])
        command.save(update_fields=["state", "failure_code", "provider_message_ids", "terminal_at", "updated_at"])
        if command.state == HumanReplyCommand.State.UNKNOWN:
            _ensure_unknown_reconciliation(command, now=terminal)
    return command
