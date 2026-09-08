"""DB-only projection of exact revision echo receipts into existing history."""
from __future__ import annotations

import json

from django.db import transaction
from django.utils import timezone

from management.models import (
    IgClient, IgDeferredEcho, IgPermissionTransitionJob,
    IgRevisionDeliveryEffect, InstagramBotMessage, InstagramBotSettings,
)


class RevisionEchoDeferred(RuntimeError):
    """The ingress event was not durably accepted and must remain retryable."""


def uses_revision_echo_scope(namespace, recipient=None):
    from management.services.ig_revision_live import revision_execution_enabled

    if not namespace:
        return False
    if revision_execution_enabled():
        return not recipient or IgClient.objects.filter(igsid=recipient).exists()
    # Rolling schema deployment and flag rollback: the old outbox table exists
    # before the new echo table. A previously owned send keeps its exact lane.
    rows = IgRevisionDeliveryEffect.objects.filter(provider_namespace=namespace)
    if recipient:
        rows = rows.filter(recipient_igsid=recipient)
    return rows.exists()


def _project_manager_event(event_id):
    from management.services.ig_revision_echo import (
        acknowledge_historical_manager_echo, acknowledge_manager_echo,
    )
    from management.services.ig_permission_transitions import create_permission_transition
    from management.services.instagram_bot import _stage_permission_message, ingress_provider_namespace

    identity = IgDeferredEcho.objects.filter(pk=event_id).values("client_id", "settings_id_snapshot").first()
    if identity is None:
        raise RevisionEchoDeferred("echo_event_missing")
    with transaction.atomic():
        settings_row = InstagramBotSettings.objects.select_for_update().filter(pk=identity["settings_id_snapshot"]).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        event = IgDeferredEcho.objects.select_for_update().get(pk=event_id)
        if client is None or client.privacy_erasure_started_at is not None:
            return False
        if settings_row is None or ingress_provider_namespace(settings_row) != event.provider_namespace or event.recipient_igsid != client.igsid:
            raise RevisionEchoDeferred("echo_projection_scope_changed")
        if event.state == event.State.MANAGER_APPLIED:
            return True
        if event.state != event.State.MANAGER_PENDING:
            return False
        historical = event.payload.get("historical") is True
        urls = [item["url"] for item in event.payload.get("attachments") or []]
        message, _created = _stage_permission_message(
            sender_id=client.igsid, role=InstagramBotMessage.Role.MANAGER,
            text=event.payload.get("text") or ("" if historical else "(зображення менеджера)"),
            mid=event.provider_message_id, source="poll_history" if historical else "echo",
            provider_namespace=event.provider_namespace,
            attachments=json.dumps(urls, ensure_ascii=False) if urls else "",
            provider_created_at=event.provider_created_at,
        )
        if message is None:
            raise RevisionEchoDeferred("echo_message_projection_conflict")
        if message.client_id is None:
            message.client = client
            message.save(update_fields=["client"])
        if historical:
            result = acknowledge_historical_manager_echo(
                event_id=event.pk, settings_id=settings_row.pk, manager_message_id=message.pk,
            )
        else:
            job = create_permission_transition(
                kind=IgPermissionTransitionJob.Kind.MANAGER_TAKEOVER,
                dedupe_key=f"permission:manager_takeover:message:{message.pk}",
                client=client, settings=settings_row, source_message=message,
            )
            result = acknowledge_manager_echo(
                event_id=event.pk, settings_id=settings_row.pk,
                manager_message_id=message.pk, permission_transition_id=job.pk,
            )
        if not result.accepted:
            raise RevisionEchoDeferred(result.reason)
        return True


def _project_attribution(result):
    if not result.accepted:
        if result.reason == "echo_erasure_active":
            return True
        raise RevisionEchoDeferred(result.reason)
    if result.classification == "manager_pending":
        _project_manager_event(result.event_id)
    elif result.classification == "own" and result.effect_id:
        from management.services.ig_revision_live import _project_sent_history

        revision_id = IgRevisionDeliveryEffect.objects.filter(pk=result.effect_id).values_list("revision_id", flat=True).first()
        if revision_id:
            _project_sent_history(revision_id)
    return True


def observe_and_project_echo(settings_row, *, namespace, recipient, mid, text="", attachments=None, received_at=None, historical=False):
    from management.services.ig_revision_echo import observe_revision_echo
    from management.services.ig_outgoing_registry import is_our_outgoing

    exact = IgRevisionDeliveryEffect.objects.filter(
        provider_namespace=namespace, recipient_igsid=recipient,
        revision__client__igsid=recipient, provider_message_id=mid, state="sent",
    ).values_list("revision_id", flat=True).first()
    if exact:
        from management.services.ig_revision_live import _project_sent_history

        _project_sent_history(exact)
        return True
    if is_our_outgoing(mid, recipient_id=recipient, provider_namespace=namespace):
        # Non-revision followups still have exact scoped provider receipts.
        # Poll may restore their missing history without inventing a manager.
        if historical:
            _project_legacy_receipt_history(namespace, recipient, mid, text, attachments, received_at)
        return True

    result = observe_revision_echo(
        settings_id=settings_row.pk, namespace=namespace, recipient=recipient,
        mid=mid, text=text, attachments=attachments, received_at=received_at,
        historical=historical,
    )
    return _project_attribution(result)


def _project_legacy_receipt_history(namespace, recipient, mid, text, attachments, received_at):
    from management.services.ig_revision_echo import _payload

    payload = _payload(text, attachments)
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(igsid=recipient).first()
        if client is None or client.privacy_erasure_started_at is not None:
            return
        if InstagramBotMessage.objects.filter(client=client, role="model", provider_message_id=mid).exists():
            return
        existing = InstagramBotMessage.objects.filter(mid=mid).first()
        if existing:
            if existing.client_id != client.pk or existing.role != "model" or existing.provider_namespace != namespace:
                raise RevisionEchoDeferred("legacy_receipt_history_conflict")
            return
        urls = [item["url"] for item in payload["attachments"]]
        InstagramBotMessage.objects.create(
            client=client, sender_id=recipient, mid=mid, provider_message_id=mid,
            provider_namespace=namespace, role="model", source="poll_history",
            status="done", send_state="sent", text=payload["text"] or "(медіа)",
            attachments=json.dumps(urls, ensure_ascii=False) if urls else "",
            provider_created_at=received_at, processed_at=timezone.now(),
        )


def reconcile_pending_revision_echoes(settings_row, *, limit=10):
    """Bounded local work runs even when generation or sends are paused."""
    from management.services.ig_revision_echo import BLOCKING_STATES, reconcile_revision_echoes
    from management.services.instagram_bot import ingress_provider_namespace

    namespace = ingress_provider_namespace(settings_row)
    if not uses_revision_echo_scope(namespace):
        return 0
    clients = list(IgDeferredEcho.objects.filter(
        provider_namespace=namespace, state__in=BLOCKING_STATES,
        client__privacy_erasure_started_at__isnull=True,
    ).order_by("client_id").values_list("client_id", flat=True).distinct()[:max(1, min(int(limit), 20))])
    handled = 0
    for client_id in clients:
        for result in reconcile_revision_echoes(settings_id=settings_row.pk, client_id=client_id, namespace=namespace, limit=8):
            _project_attribution(result)
            handled += int(result.classification in {"own", "manager_pending", "manager_applied"})
    return handled


def reconcile_effect_echoes(effect):
    """Settle an early echo before the next physical response part is admitted."""
    from management.services.ig_revision_echo import BLOCKING_STATES, reconcile_revision_echoes

    client_id = effect.revision.client_id
    if not IgDeferredEcho.objects.filter(
        client_id=client_id, provider_namespace=effect.provider_namespace,
        state__in=BLOCKING_STATES,
    ).exists():
        return
    for result in reconcile_revision_echoes(
        settings_id=effect.settings_id_snapshot, client_id=client_id,
        namespace=effect.provider_namespace, limit=32,
    ):
        _project_attribution(result)
