"""Actor-aware manual Instagram replies with a durable idempotency boundary."""
from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from management.models import AdminAuditLog, IgClient, InstagramBotMessage, InstagramBotSettings
from management.ig_bot_models import HumanReplyCommand


MAX_HUMAN_REPLY_CHARS = 4000


class HumanReplyRejected(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class HumanReplyResult:
    command: HumanReplyCommand
    idempotent: bool = False


@contextmanager
def _human_send_boundary(command_id: int):
    """Recheck the human command/epoch at the physical request edge."""
    with transaction.atomic():
        command = HumanReplyCommand.objects.select_for_update().select_related("client").get(pk=command_id)
        client = IgClient.objects.select_for_update().get(pk=command.client_id)
        allowed = bool(
            command.state == HumanReplyCommand.State.PROVIDER_STARTED
            and client.reply_permission_epoch == command.permission_epoch
            and not client.hidden_at
            and not client.privacy_erasure_started_at
            and not (
                client.opted_out_at
                and (not client.opted_in_at or client.opted_out_at > client.opted_in_at)
            )
            and not getattr(client, "is_blocked", False)
        )
    yield allowed


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

    existing = HumanReplyCommand.objects.filter(operation_id=op).first()
    if existing:
        if existing.client_id != int(client_id) or existing.actor_id != getattr(actor, "pk", None) or existing.text != text:
            raise HumanReplyRejected("operation_conflict")
        return HumanReplyResult(existing, idempotent=True)

    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=client_id).first()
        if not client:
            raise HumanReplyRejected("client_not_found")
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
            command = HumanReplyCommand.objects.create(
                operation_id=op,
                client=client,
                actor=actor,
                context_message=context,
                recipient_igsid=client.igsid,
                text=text,
                provider_namespace=str(ingress_provider_namespace(settings_obj) or "")[:128],
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
            command = HumanReplyCommand.objects.get(operation_id=op)
            return HumanReplyResult(command, idempotent=True)
    return HumanReplyResult(command)


def dispatch_human_reply_command(command_id: int, *, now: datetime | None = None) -> HumanReplyCommand:
    """Claim and send once; an ambiguous provider outcome is terminal UNKNOWN."""
    now = now or timezone.now()
    with transaction.atomic():
        command = HumanReplyCommand.objects.select_for_update().select_related("client").get(pk=command_id)
        if command.state in {HumanReplyCommand.State.SENT, HumanReplyCommand.State.DEFINITE_FAILED, HumanReplyCommand.State.UNKNOWN, HumanReplyCommand.State.CANCELLED}:
            return command
        if command.state == HumanReplyCommand.State.CLAIMED:
            return command
        client = IgClient.objects.select_for_update().get(pk=command.client_id)
        if client.reply_permission_epoch != command.permission_epoch or client.hidden_at or client.privacy_erasure_started_at:
            command.state = HumanReplyCommand.State.CANCELLED
            command.failure_code = "permission_epoch_changed"
            command.terminal_at = now
            command.save(update_fields=["state", "failure_code", "terminal_at", "updated_at"])
            return command
        latest = _latest_inbound(client)
        if not latest or (command.context_message_id and latest.pk != command.context_message_id):
            command.state = HumanReplyCommand.State.CANCELLED
            command.failure_code = "newer_inbound"
            command.terminal_at = now
            command.save(update_fields=["state", "failure_code", "terminal_at", "updated_at"])
            return command
        deadline = _window_deadline(latest)
        if deadline is None or deadline <= now:
            command.state = HumanReplyCommand.State.DEFINITE_FAILED
            command.failure_code = "reply_window_closed"
            command.terminal_at = now
            command.save(update_fields=["state", "failure_code", "terminal_at", "updated_at"])
            return command
        settings_obj = InstagramBotSettings.load()
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
            permission_boundary_factory=lambda: _human_send_boundary(command.pk),
        )
        ok = bool(getattr(receipt, "ok", False))
        kind = str(getattr(receipt, "kind", "") or "")
        hint = str(getattr(receipt, "hint", "") or "")[:96]
        ids = [str(value)[:255] for value in (getattr(receipt, "provider_message_ids", ()) or ()) if str(value)]
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
        else:
            command.state = HumanReplyCommand.State.DEFINITE_FAILED
            message.status = InstagramBotMessage.Status.FAILED
            message.send_state = "failed"
        message.save(update_fields=[
            "status", "send_state", "send_completed_at", "processed_at",
            "provider_message_id", "delivery_provider_message_ids", "delivery_failure_boundary",
        ])
        command.save(update_fields=["state", "failure_code", "provider_message_ids", "terminal_at", "updated_at"])
    return command
