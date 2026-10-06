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
    """Carry a safe reason and whether retry can repair the observation."""

    def __init__(self, reason, *, retryable=True):
        self.reason = str(reason)[:64]
        self.retryable = bool(retryable)
        super().__init__(self.reason)


def uses_revision_echo_scope(namespace, recipient=None, *, mid=""):
    from management.services.ig_revision_live import revision_execution_enabled
    from management.ig_human_reply_models import HumanReplyPart

    if not namespace:
        return False
    human = HumanReplyPart.objects.filter(provider_namespace=namespace)
    if mid and human.filter(provider_message_id=mid, state="sent").exists():
        # Also route foreign-recipient exact identities through the finite
        # guard, rather than legacy's unscoped cache shortcut.
        return True
    if (human.filter(recipient_igsid=recipient) if recipient else human).exists():
        return True
    pending_scope = IgDeferredEcho.objects.filter(provider_namespace=namespace)
    if (pending_scope.filter(recipient_igsid=recipient) if recipient else pending_scope).exists():
        return True
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
    from management.services.instagram_bot import (
        _echo_media_metadata, _historicalize_provider_media,
        _stage_permission_message, ingress_provider_namespace,
    )

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
        # Receipt completion can precede this projection after observation.
        # A human checkpoint owns the client prefix, so this final recheck
        # prevents an extra manager transcript/takeover from an early echo.
        from management.services.ig_revision_echo import _own_human_part, _reconcile_event
        if _own_human_part(client, event.provider_namespace, event.recipient_igsid, event.provider_message_id) is not None:
            event = _reconcile_event(event, client, timezone.now())
            if event.state != event.State.MANAGER_PENDING:
                return False
        historical = event.payload.get("historical") is True
        attachments = event.payload.get("attachments") or []
        urls = [item["url"] for item in attachments if item.get("url")]
        story = next((item for item in attachments if item.get("type") == "story"), None)
        story_reply = story and story.get("context_kind") != "shared_story"
        reply_to_provider_message_id = str((story or {}).get("provider_id") or "").strip()[:255] if story_reply else ""
        metadata = _echo_media_metadata(attachments)
        if historical:
            metadata = _historicalize_provider_media(metadata)
        fallback_text = (
            "" if historical else
            "(відповідь менеджера на сторіс)" if story_reply else
            "(сторіс менеджера)" if story else "(зображення менеджера)"
        )
        message, _created = _stage_permission_message(
            sender_id=client.igsid, role=InstagramBotMessage.Role.MANAGER,
            text=event.payload.get("text") or fallback_text,
            mid=event.provider_message_id, source="poll_history" if historical else "echo",
            provider_namespace=event.provider_namespace,
            attachments=json.dumps(urls, ensure_ascii=False) if urls else "",
            provider_created_at=event.provider_created_at,
            reply_to_provider_message_id=reply_to_provider_message_id,
            attachment_metadata=metadata,
            allow_media_capture=not historical,
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
        if not historical:
            from management.services.instagram_bot import _enqueue_memory_source_event

            _enqueue_memory_source_event(message.pk)
        return True


def _project_attribution(result):
    if not result.accepted:
        if result.reason == "echo_erasure_active":
            return True
        raise RevisionEchoDeferred(result.reason, retryable=result.retryable)
    if result.classification == "manager_pending":
        _project_manager_event(result.event_id)
        # A late exact human receipt can replace pending generic attribution
        # before any manager projection effect is accepted.
        if IgDeferredEcho.objects.filter(pk=result.event_id, state="human_pending").exists():
            _project_human_event(result.event_id)
    elif result.classification == "human_pending":
        _project_human_event(result.event_id)
    elif result.classification == "own" and result.effect_id:
        from management.services.ig_revision_live import _project_sent_history

        revision_id = IgRevisionDeliveryEffect.objects.filter(pk=result.effect_id).values_list("revision_id", flat=True).first()
        if revision_id:
            _project_sent_history(revision_id)
    return True


def _project_human_event(event_id):
    from management.services.ig_human_reply_transport import project_human_part_receipt
    from management.services.ig_revision_echo import acknowledge_human_echo

    event = IgDeferredEcho.objects.filter(pk=event_id).first()
    if event is None:
        raise RevisionEchoDeferred("echo_event_missing")
    if event.state == event.State.HUMAN_APPLIED:
        return True
    if event.state != event.State.HUMAN_PENDING or not event.matched_human_part_id:
        return False
    # Projection obtains client -> command -> part locks after the observer's
    # Settings/client/event transaction committed. Never invert that prefix.
    projected = project_human_part_receipt(event.matched_human_part_id)
    if projected.reason == "client_unavailable":
        return False
    if projected.reason or projected.message is None:
        raise RevisionEchoDeferred(projected.reason or "echo_human_projection_missing")
    acknowledged = acknowledge_human_echo(event_id=event.pk,
        settings_id=event.settings_id_snapshot, human_part_id=event.matched_human_part_id,
        manager_message_id=projected.message.pk)
    if not acknowledged.accepted:
        raise RevisionEchoDeferred(acknowledged.reason, retryable=acknowledged.retryable)
    return True


def observe_and_project_echo(settings_row, *, namespace, recipient, mid, text="", attachments=None, received_at=None, historical=False):
    from management.services.ig_revision_echo import observe_revision_echo
    from management.services.ig_outgoing_registry import is_our_outgoing
    from management.ig_human_reply_models import HumanReplyPart

    human_scope = HumanReplyPart.objects.filter(provider_namespace=namespace)
    if (human_scope.filter(recipient_igsid=recipient).exists()
        or human_scope.filter(provider_message_id=mid, state="sent").exists()):
        result = observe_revision_echo(
            settings_id=settings_row.pk, namespace=namespace, recipient=recipient,
            mid=mid, text=text, attachments=attachments, received_at=received_at,
            historical=historical,
        )
        return _project_attribution(result)

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
        urls = [item["url"] for item in payload["attachments"] if item.get("url")]
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
            handled += int(result.classification in {"own", "manager_pending", "manager_applied", "human_pending", "human_applied"})
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


def reconcile_human_part_echoes(part_id):
    """After-commit hook: reconcile early echoes using the original receipt.

    Callers must use on_commit(robust=True). Projection failure keeps durable
    pending work and cannot downgrade the already committed receipt or resend.
    """
    from management.ig_human_reply_models import HumanReplyPart
    from management.services.ig_revision_echo import BLOCKING_STATES, reconcile_revision_echoes

    part = HumanReplyPart.objects.filter(pk=part_id).first()
    settings = InstagramBotSettings.objects.order_by("pk").first()
    if part is None or settings is None:
        return 0
    if not IgDeferredEcho.objects.filter(client_id=part.client_id,
            provider_namespace=part.provider_namespace, state__in=BLOCKING_STATES).exists():
        return 0
    handled = 0
    for result in reconcile_revision_echoes(settings_id=settings.pk, client_id=part.client_id,
            namespace=part.provider_namespace, limit=32):
        _project_attribution(result)
        handled += int(result.classification in {"human_pending", "human_applied"})
    return handled
