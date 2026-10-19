"""Current customer ingress, independently of transcript/history insertion order.

Only the validated poll consumer issues a signed admission. The signature binds
the original provider identity and content; a database ``source='poll'`` label
alone never grants reply, commerce, media capture, or source-transfer authority.
"""
from datetime import datetime, timedelta
import hashlib
import json

from django.core import signing
from django.db import connection
from django.db.models import F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone


VERSION = "current-provider-poll-v1"
SALT = "management.ig.current-provider-poll.v1"
REPLY_WINDOW = timedelta(hours=23)
FUTURE_TOLERANCE = timedelta(minutes=5)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def _content(message):
    from management.services.ig_turn_revisions import _discovered_media

    return {"text": str(message.text or ""),
        "reply_to_provider_message_id": str(message.reply_to_provider_message_id or ""),
        "quick_reply_payload": str(message.quick_reply_payload or ""),
        "discovered_media": _discovered_media(message)}


def build_polled_ingress_proof(settings_row, provider_message, *, conversation_id, now=None):
    """Issue only after a successful, validated provider conversation-page read.

    Callers must pass the same provider attachment metadata to enqueue_inbound.
    A partial traversal may issue proof for each validated current source; it
    still cannot advance the conversation cursor until traversal completes.
    """
    from types import SimpleNamespace
    from management.services import instagram_bot as bot

    now = now or timezone.now()
    if (not isinstance(provider_message, dict) or not str(conversation_id or "").strip()
        or not timezone.is_aware(now)):
        return {}
    namespace = bot.ingress_provider_namespace(settings_row)
    owner = str(bot._provider_account_id(settings_row) or "")
    sender_row, recipient_row = provider_message.get("from"), provider_message.get("to")
    if not isinstance(sender_row, dict) or not isinstance(recipient_row, dict):
        return {}
    sender = str(sender_row.get("id") or "")
    recipients = recipient_row.get("data") or []
    mid = str(provider_message.get("id") or "").strip()
    at = bot._parse_ig_time(provider_message.get("created_time", ""))
    if (not namespace or not owner or not bot._SENDER_ID_RE.fullmatch(sender)
        or sender == owner or not bot._valid_message_id(mid) or at is None
        or not timezone.is_aware(at) or not now - REPLY_WINDOW < at <= now + FUTURE_TOLERANCE
        or (settings_row.reply_after and at <= settings_row.reply_after)
        or not isinstance(recipients, list)
        or {str(row.get("id") or "") for row in recipients if isinstance(row, dict)} != {owner}):
        return {}
    metadata = bot._provider_attachment_metadata(provider_message)
    attachments = bot._extract_media_urls(provider_message)
    media = bot._normalize_message_media(metadata or bot._attachment_media_metadata(attachments, source="poll"),
        message_scope=mid, identity_origin="legacy_positional")
    source = SimpleNamespace(pk=mid, text=bot._project_media_text(
        str(provider_message.get("message") or "").strip(), metadata) or bot._media_fallback_text(metadata),
        attachment_media=media, reply_to_provider_message_id=bot._reply_to_provider_message_id(provider_message),
        quick_reply_payload=bot._quick_reply_payload(provider_message))
    payload = {"version": VERSION, "source": "poll", "role": "user",
        "namespace": namespace, "sender_id": sender, "provider_message_id": mid,
        "provider_created_at": at.isoformat(), "admitted_at": now.isoformat(),
        "conversation_id": str(conversation_id), "content_digest": _digest(_content(source))}
    return {"version": VERSION, "signed": signing.dumps(payload, salt=SALT, compress=True)}


def _poll_payload(message):
    proof = getattr(message, "ingress_proof", None)
    if not isinstance(proof, dict) or proof.get("version") != VERSION or not isinstance(proof.get("signed"), str):
        return None
    try:
        payload = signing.loads(proof["signed"], salt=SALT)
        at = datetime.fromisoformat(payload["provider_created_at"])
        admitted = datetime.fromisoformat(payload["admitted_at"])
        if (payload.get("version") != VERSION or payload.get("source") != "poll"
            or payload.get("role") != "user" or not payload.get("conversation_id")
            or payload.get("namespace") != message.provider_namespace
            or payload.get("sender_id") != message.sender_id
            or payload.get("provider_message_id") != message.mid
            or message.provider_created_at is None or at != message.provider_created_at
            or not timezone.is_aware(at) or not timezone.is_aware(admitted)
            or not admitted - REPLY_WINDOW < at <= admitted + FUTURE_TOLERANCE
            or payload.get("content_digest") != _digest(_content(message))):
            return None
        return payload
    except (signing.BadSignature, ValueError, TypeError, KeyError, AttributeError):
        return None


def validate_polled_ingress_proof(settings_row, proof, *, sender_id, text, mid,
        attachments, attachment_metadata, received_at, reply_to_provider_message_id="",
        quick_reply_payload="", now=None):
    """Pure precheck before consent, observation, identity or turn side effects."""
    from types import SimpleNamespace
    from management.services import instagram_bot as bot

    now = now or timezone.now()
    if (received_at is None or not timezone.is_aware(received_at)
        or not now - REPLY_WINDOW < received_at <= now + FUTURE_TOLERANCE
        or (settings_row.reply_after and received_at <= settings_row.reply_after)):
        return False
    metadata = list(attachment_metadata or ())
    row = SimpleNamespace(pk=mid, provider_namespace=bot.ingress_provider_namespace(settings_row),
        sender_id=sender_id, mid=mid, provider_created_at=received_at, ingress_proof=proof,
        text=bot._project_media_text(text, metadata) or bot._media_fallback_text(metadata),
        reply_to_provider_message_id=reply_to_provider_message_id,
        quick_reply_payload=quick_reply_payload,
        attachment_media=bot._normalize_message_media(metadata or bot._attachment_media_metadata(
            attachments or [], source="poll"), message_scope=mid, identity_origin="legacy_positional"))
    return _poll_payload(row) is not None


def is_current_customer_source(message, *, namespace=None, now=None):
    """Admitted source identity/content; send permission/window remain separate."""
    if (message is None or message.role != "user" or not message.client_id
        or message.sender_id != message.client.igsid
        or (namespace is not None and message.provider_namespace != namespace)):
        return False
    if message.source == "webhook":
        return True
    return message.source == "poll" and _poll_payload(message) is not None


def bind_polled_ingress_proof(message, proof):
    """Persist exact current proof before turn/source capture in the ingress tx."""
    if (not connection.in_atomic_block or message.source != "poll"
        or message.role != "user" or not message.client_id
        or message.sender_id != message.client.igsid):
        raise ValueError("poll_proof_ingress_transaction_required")
    previous = message.ingress_proof
    message.ingress_proof = proof
    if not _poll_payload(message):
        message.ingress_proof = previous
        raise ValueError("poll_source_proof_invalid")
    if previous and previous != proof:
        message.ingress_proof = previous
        raise ValueError("poll_source_proof_immutable")
    message.save(update_fields=["ingress_proof"])


def newer_customer_sources(client_id, watermark, *, namespace=""):
    """Transcript backfills are context, never new customer turn fences."""
    from management.models import InstagramBotMessage

    rows = InstagramBotMessage.objects.filter(client_id=client_id, role="user",
        sender_id=F("client__igsid"), pk__gt=watermark, source__in=("webhook", "poll"))
    if namespace:
        rows = rows.filter(provider_namespace=namespace)
    # An unproved poll row cannot acquire source ownership. Validate signatures
    # in Python rather than trusting a JSON schema marker as provider authority.
    poll_ids = [row.pk for row in rows.filter(source="poll").select_related("client")
        if is_current_customer_source(row, namespace=namespace or None)]
    return rows.filter(Q(source="webhook") | Q(pk__in=poll_ids))


def newer_manager_sources(client_id, watermark, *, namespace=""):
    """Fresh Meta manager echoes or authenticated local human-reply work.

    A pending local command already owns the reply lane before its provider
    receipt exists. Imported history has neither of these live source labels.
    """
    from management.models import InstagramBotMessage

    anchor = InstagramBotMessage.objects.filter(pk=watermark, client_id=client_id).annotate(
        event_at=Coalesce("provider_created_at", "created_at")).values_list("event_at", flat=True).first()
    rows = InstagramBotMessage.objects.filter(client_id=client_id, role="manager",
        sender_id=F("client__igsid"), pk__gt=watermark).exclude(status="failed").annotate(
        event_at=Coalesce("provider_created_at", "created_at"))
    if anchor is None:
        return rows.none()
    echo = Q(source="echo")
    if namespace:
        echo &= Q(provider_namespace=namespace)
    return rows.filter(echo | Q(source="human_reply")).filter(event_at__gte=anchor)


def presence_source_current(namespace, client_id, recipient_id, watermark):
    from management.models import InstagramBotMessage
    from management.services.ig_webhook_inbox import pending_ingress_blocks

    source = InstagramBotMessage.objects.select_related("client").filter(
        pk=watermark, client_id=client_id, sender_id=recipient_id).first()
    return bool(is_current_customer_source(source, namespace=namespace)
        and not pending_ingress_blocks(namespace, recipient_id)
        and not newer_customer_sources(client_id, watermark, namespace=namespace).exists())


def is_promotable_poll_placeholder(existing, settings_row, *, received_at, now=None):
    """A richer authenticated webhook can replace one current empty poll stub."""
    from management.services import instagram_bot as bot

    now = now or timezone.now()
    at = existing.provider_created_at
    metadata = existing.attachment_media if isinstance(existing.attachment_media, list) else []
    placeholder_text = bot._media_fallback_text(metadata) if metadata else "(медіа)"
    return bool(existing.source == "poll_history" and existing.role == "user"
        and existing.status == "done"
        and existing.text in {"", placeholder_text, *bot._GENERIC_MEDIA_TEXTS}
        and not existing.attachments and not existing.media_capture_eligible
        and all(isinstance(part, dict) and not part.get("url")
            and not part.get("storage_name") and not part.get("private_storage") for part in metadata)
        and not existing.attempts and not existing.processing_started_at
        and not existing.send_state and not existing.send_started_at and not existing.send_completed_at
        and existing.provider_namespace == bot.ingress_provider_namespace(settings_row)
        and at and received_at and abs((received_at - at).total_seconds()) <= 1
        and now - REPLY_WINDOW < at <= now + FUTURE_TOLERANCE
        and (not settings_row.reply_after or at > settings_row.reply_after))
