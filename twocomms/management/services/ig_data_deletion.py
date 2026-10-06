"""Подтверждённое удаление данных DIRECT_BOT (F-SEC-002).

Разделение ответственности:

* публичная форма `/data-deletion/submit/` только **регистрирует заявку**
  в статусе `PENDING_VERIFICATION` и ничего не удаляет — владение
  Instagram-аккаунтом там не подтверждено, а username публичен;
* signed_request от Meta (`/data-deletion/callback/`) удаляет сразу:
  владение там доказано HMAC-подписью приложения;
* заявку из публичной формы исполняет менеджер после проверки владения,
  через `fulfill_deletion_request` (команда `fulfill_ig_data_deletion`).

Запись `BotDataDeletionRequest` — неудаляемый audit факта удаления:
она не входит ни в один каскад удаления клиентских данных.
"""
from __future__ import annotations

from dataclasses import dataclass
import secrets

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone


class DeletionRequestNotActionable(ValueError):
    """Заявка не в том состоянии, чтобы её можно было исполнить."""


ERASURE_LEASE_SECONDS = 300


@dataclass(frozen=True)
class ErasureClaim:
    request_id: int
    token: str
    client_ids: tuple[int, ...]
    message_ids: tuple[int, ...]
    sender_ids: tuple[str, ...]
    inbox_ids: tuple[int, ...]
    cutoff_at: object


def _assert_no_production_outer_atomic() -> None:
    """The client fence must commit before private-storage I/O starts."""
    if connection.in_atomic_block:
        raise DeletionRequestNotActionable(
            "privacy fulfillment must not run inside an outer transaction"
        )


def _frozen_targets(locked):
    """Capture exact current owners once; retries never resolve identifiers."""
    from management.models import (
        IgClient, IgWebhookInboxEvent, InstagramBotMessage, InstagramBotSettings,
    )
    from management.services.ig_webhook_inbox import _namespace

    from management.bot_views import _normalize_deletion_identifier

    identifier = _normalize_deletion_identifier(locked.normalized_identifier or locked.identifier)
    clients = list(
        IgClient.objects.select_for_update().filter(
            Q(igsid__iexact=identifier)
            | Q(username__iexact=identifier)
            | Q(display_name__iexact=identifier)
            | Q(phone_normalized__iexact=identifier)
        ).order_by("pk")
    )
    # The cutoff is deliberately sampled after the client locks are acquired,
    # then the same transaction commits the privacy fence before any new
    # ingress can create another owned message for these client IDs.
    cutoff_at = timezone.now()
    from management.services.ig_memory_producer import memory_invalidation_updates
    for client in clients:
        if client.privacy_erasure_started_at is None:
            client.privacy_erasure_started_at = cutoff_at
            memory_updates = memory_invalidation_updates(client)
            for key, value in memory_updates.items():
                setattr(client, key, value)
            client.save(update_fields=["privacy_erasure_started_at", "updated_at", *memory_updates])
    client_ids = tuple(client.pk for client in clients)
    sender_ids = tuple(sorted({client.igsid for client in clients if client.igsid}))
    scope = Q(client_id__in=client_ids)
    if sender_ids:
        scope |= Q(sender_id__in=sender_ids)
    message_ids = tuple(
        InstagramBotMessage.objects.select_for_update()
        .filter(scope, created_at__lte=cutoff_at)
        .order_by("pk").values_list("pk", flat=True)
    ) if client_ids or sender_ids else ()
    settings_obj = InstagramBotSettings.load()
    namespace, _owner = _namespace(settings_obj)
    inbox_ids = tuple(
        IgWebhookInboxEvent.objects.select_for_update().filter(
            namespace=namespace,
            customer_igsid__in=sender_ids,
            decision__in=(
                IgWebhookInboxEvent.Decision.ACCEPTED,
                IgWebhookInboxEvent.Decision.BLOCKED,
            ),
            received_at__lte=cutoff_at,
        ).order_by("pk").values_list("pk", flat=True)
    ) if sender_ids else ()
    return client_ids, message_ids, sender_ids, inbox_ids, cutoff_at


def _claim_erasure(request_id: int, *, actor: str) -> ErasureClaim:
    """Durably claim one verified request, recovering only an expired lease."""
    from datetime import timedelta
    from management.models import BotDataDeletionRequest

    now = timezone.now()
    with transaction.atomic():
        locked = (
            BotDataDeletionRequest.objects.select_for_update().filter(pk=request_id).first()
        )
        if locked is None:
            raise DeletionRequestNotActionable("deletion request no longer exists")
        recoverable = (
            locked.status == BotDataDeletionRequest.Status.ERASING
            and locked.erasure_lease_until is not None
            and locked.erasure_lease_until <= now
        )
        if locked.status != BotDataDeletionRequest.Status.PENDING_VERIFICATION and not recoverable:
            raise DeletionRequestNotActionable(
                f"request {locked.confirmation_code} is {locked.status}, not claimable"
            )
        if recoverable:
            client_ids = tuple(int(value) for value in locked.erasure_target_client_ids)
            message_ids = tuple(int(value) for value in locked.erasure_target_message_ids)
            sender_ids = tuple(str(value) for value in locked.erasure_target_sender_ids)
            inbox_ids = tuple(int(value) for value in locked.erasure_target_inbox_ids)
            cutoff_at = locked.erasure_cutoff_at
            if cutoff_at is None:
                raise DeletionRequestNotActionable("expired erasure claim has no frozen target cutoff")
        else:
            client_ids, message_ids, sender_ids, inbox_ids, cutoff_at = _frozen_targets(locked)
            locked.erasure_target_client_ids = list(client_ids)
            locked.erasure_target_message_ids = list(message_ids)
            locked.erasure_target_sender_ids = list(sender_ids)
            locked.erasure_target_inbox_ids = list(inbox_ids)
            locked.erasure_cutoff_at = cutoff_at
            locked.erasure_actor_label = actor[:150]
        token = secrets.token_hex(16)
        locked.status = BotDataDeletionRequest.Status.ERASING
        locked.erasure_lease_token = token
        locked.erasure_lease_until = now + timedelta(seconds=ERASURE_LEASE_SECONDS)
        locked.save(update_fields=[
            "status", "erasure_lease_token", "erasure_lease_until",
            "erasure_target_client_ids", "erasure_target_message_ids",
            "erasure_target_sender_ids", "erasure_cutoff_at",
            "erasure_target_inbox_ids",
            "erasure_actor_label",
        ])
        return ErasureClaim(
            request_id=locked.pk,
            token=token,
            client_ids=client_ids,
            message_ids=message_ids,
            sender_ids=sender_ids,
            inbox_ids=inbox_ids,
            cutoff_at=cutoff_at,
        )


def _settle_erasure(claim: ErasureClaim, deletion: dict) -> None:
    """Settle only the still-owned durable claim after the erase phases finish."""
    from management.models import BotDataDeletionRequest

    with transaction.atomic():
        locked = (
            BotDataDeletionRequest.objects.select_for_update().filter(pk=claim.request_id).first()
        )
        if (
            locked is None
            or locked.status != BotDataDeletionRequest.Status.ERASING
            or locked.erasure_lease_token != claim.token
        ):
            raise DeletionRequestNotActionable("erasure claim was lost before settlement")
        locked.status = deletion["status"]
        locked.deleted_clients_count = deletion["clients"]
        locked.deleted_messages_count = deletion["messages"]
        locked.deleted_raw_events_count = deletion["raw_events"]
        locked.deleted_logs_count = deletion["logs"]
        locked.detail = (
            f"{deletion['detail']} Verified and fulfilled by "
            f"{locked.erasure_actor_label or 'manager'}."
        )[:4000]
        locked.completed_at = timezone.now()
        locked.erasure_lease_token = ""
        locked.erasure_lease_until = None
        locked.save(update_fields=[
            "status", "deleted_clients_count", "deleted_messages_count",
            "deleted_raw_events_count", "deleted_logs_count", "detail",
            "completed_at", "erasure_lease_token", "erasure_lease_until",
        ])


def fulfill_deletion_request(request_row, *, actor_label: str) -> dict:
    """Исполнить заявку после подтверждения владения человеком.

    Идемпотентность: повторный вызов на уже исполненной заявке поднимает
    `DeletionRequestNotActionable` и ничего не меняет. Это осознанно:
    молчаливый no-op скрыл бы двойное нажатие, а данные уже удалены
    и «переудалить» их нельзя.
    """
    from management.bot_views import _delete_direct_bot_records
    from management.models import BotDataDeletionRequest

    actor = (actor_label or "").strip()
    if not actor:
        raise DeletionRequestNotActionable("actor_label is required for the audit trail")

    _assert_no_production_outer_atomic()
    claim = _claim_erasure(request_row.pk, actor=actor)
    # `_delete_direct_bot_records` commits its privacy fence before it touches
    # private storage.  Keep this call outside every request-row transaction.
    deletion = _delete_direct_bot_records(
        exact_client_ids=claim.client_ids,
        frozen_message_ids=claim.message_ids,
        frozen_sender_ids=claim.sender_ids,
        frozen_inbox_ids=claim.inbox_ids,
        frozen_cutoff_at=claim.cutoff_at,
    )
    if (
        deletion["status"] == BotDataDeletionRequest.Status.NO_MATCH
        and (claim.client_ids or claim.message_ids or claim.inbox_ids)
    ):
        # A crash after erasure but before settlement leaves known targets
        # already absent. Preserve exact per-run counts without calling this
        # a failed identity lookup.
        deletion["status"] = BotDataDeletionRequest.Status.COMPLETED
        deletion["detail"] = "Frozen DIRECT_BOT targets are already absent; recovery completed."
    _settle_erasure(claim, deletion)

    request_row.refresh_from_db()
    return deletion


def register_public_request(identifier: str, normalized_identifier: str):
    """Зарегистрировать заявку с публичной формы. Ничего не удаляет."""
    import secrets

    from management.models import BotDataDeletionRequest

    return BotDataDeletionRequest.objects.create(
        confirmation_code=secrets.token_hex(8).upper(),
        source=BotDataDeletionRequest.Source.MANUAL_FORM,
        identifier=(identifier or "")[:255],
        normalized_identifier=(normalized_identifier or "")[:255],
        status=BotDataDeletionRequest.Status.PENDING_VERIFICATION,
        detail=(
            "Request received. Ownership verification is required before any "
            "DIRECT_BOT record is deleted."
        ),
    )
