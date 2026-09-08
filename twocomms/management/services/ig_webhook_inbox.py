"""Bounded, namespace-aware durable ingress for Instagram webhooks."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from dataclasses import dataclass

from django.conf import settings
from django.db import DatabaseError, IntegrityError, models, transaction
from django.db.models import Count, Min
from django.utils import timezone

DEFAULT_MAX_BODY_BYTES = 256 * 1024
DEFAULT_MAX_ENTRIES = 50
DEFAULT_MAX_EVENTS = 200
_ID_RE = re.compile(r"^[A-Za-z0-9:_-]{1,64}$")


class WebhookRejected(ValueError):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class InboxAcceptance:
    accepted: int
    rejected: int
    duplicates: int


def _limit(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def max_body_bytes() -> int:
    return _limit("IG_WEBHOOK_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES, 1024, 1024 * 1024)


def max_entries() -> int:
    return _limit("IG_WEBHOOK_MAX_ENTRIES", DEFAULT_MAX_ENTRIES, 1, 200)


def max_events() -> int:
    return _limit("IG_WEBHOOK_MAX_EVENTS", DEFAULT_MAX_EVENTS, 1, 1000)


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _namespace(settings_obj) -> tuple[str, str]:
    from management.services import instagram_bot as bot

    owner = str(bot._provider_account_id(settings_obj) or "").strip()
    return f"{bot.provider_transport(settings_obj)}:{owner or 'unconfigured'}", owner


def _event_key(owner_id: str, event: dict, *, kind: str, decision: str) -> str:
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    postback = event.get("postback") if isinstance(event.get("postback"), dict) else {}
    provider_id = str(message.get("mid") or postback.get("mid") or "").strip()
    if provider_id:
        digest = hashlib.sha256(f"{kind}\x1f{provider_id}".encode()).hexdigest()
        return f"{decision}:{owner_id}:{kind}:{digest}"
    return f"{decision}:{owner_id}:{kind}:{hashlib.sha256(_canonical(event)).hexdigest()}"


def _receipt(expected_owner: str, entry_owner: str, event: dict, *, kind: str, change_field: str = "") -> dict:
    if not _ID_RE.fullmatch(entry_owner):
        raise WebhookRejected("invalid_owner", 400)
    if event.get("message") is not None and not isinstance(event.get("message"), dict):
        raise WebhookRejected("invalid_message_shape", 400)
    if event.get("postback") is not None and not isinstance(event.get("postback"), dict):
        raise WebhookRejected("invalid_postback_shape", 400)
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    recipient = event.get("recipient") if isinstance(event.get("recipient"), dict) else {}
    sender_id, recipient_id = str(sender.get("id") or ""), str(recipient.get("id") or "")
    if not _ID_RE.fullmatch(sender_id) or not _ID_RE.fullmatch(recipient_id):
        raise WebhookRejected("invalid_participant", 400)
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    postback = event.get("postback") if isinstance(event.get("postback"), dict) else {}
    if not message and not postback:
        return {
            "owner_id": entry_owner[:64], "decision": "rejected", "reason": "ignored_nonmessage",
            "event_key": _event_key(entry_owner, event, kind=kind, decision="rejected"),
            "payload": {"object": "instagram", "entry": [{"id": entry_owner, "messaging": [event]}]},
        }
    if not str(message.get("mid") or postback.get("mid") or "").strip() and event.get("timestamp") is None:
        raise WebhookRejected("missing_event_identity", 400)
    attachments = message.get("attachments")
    if attachments is not None and (not isinstance(attachments, list) or len(attachments) > 8):
        raise WebhookRejected("attachment_limit", 413)
    is_echo = bool(message.get("is_echo"))
    owned = bool(expected_owner and entry_owner == expected_owner and ((is_echo and sender_id == expected_owner and recipient_id) or (not is_echo and recipient_id == expected_owner and sender_id)))
    decision = "accepted" if owned else "rejected"
    fragment_entry = {"id": entry_owner}
    if change_field:
        fragment_entry["changes"] = [{"field": change_field, "value": event}]
    else:
        fragment_entry["messaging"] = [event]
    return {
        "owner_id": entry_owner[:64], "customer_igsid": (recipient_id if is_echo else sender_id)[:64] if owned else "", "decision": decision,
        "reason": "" if owned else "owner_mismatch",
        "event_key": _event_key(entry_owner, event, kind=kind, decision=decision),
        "payload": {"object": "instagram", "entry": [fragment_entry]},
    }


def _receipts(payload: dict, expected_owner: str, *, transport: str) -> list[dict]:
    allowed_objects = {"instagram"} if transport == "instagram_login" else {"instagram", "page"}
    if payload.get("object") not in allowed_objects:
        raise WebhookRejected("invalid_object", 400)
    if not expected_owner:
        raise WebhookRejected("owner_unconfigured", 503)
    entries = payload.get("entry")
    if not isinstance(entries, list):
        raise WebhookRejected("invalid_shape", 400)
    if len(entries) > max_entries():
        raise WebhookRejected("entry_limit", 413)
    output = []
    for entry_index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise WebhookRejected("invalid_entry", 400)
        owner_id = str(entry.get("id") or "").strip()
        messaging = entry.get("messaging")
        if messaging is not None and not isinstance(messaging, list):
            raise WebhookRejected("invalid_event_shape", 400)
        for event_index, event in enumerate(messaging or []):
            if not isinstance(event, dict):
                raise WebhookRejected("invalid_event", 400)
            output.append(_receipt(expected_owner, owner_id, event, kind="message"))
        changes = entry.get("changes")
        if changes is not None and not isinstance(changes, list):
            raise WebhookRejected("invalid_event_shape", 400)
        for change_index, change in enumerate(changes or []):
            if not isinstance(change, dict) or not isinstance(change.get("value"), dict):
                raise WebhookRejected("invalid_event", 400)
            output.append(_receipt(
                expected_owner, owner_id, change["value"], kind=f"change:{str(change.get('field') or '')[:32]}",
                change_field=str(change.get("field") or "")[:64],
            ))
        if len(output) > max_events():
            raise WebhookRejected("event_limit", 413)
    return output


def accept_webhook(raw_body: bytes, settings_obj) -> InboxAcceptance:
    if len(raw_body) > max_body_bytes():
        raise WebhookRejected("body_too_large", 413)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise WebhookRejected("invalid_json", 400) from exc
    if not isinstance(payload, dict):
        raise WebhookRejected("invalid_shape", 400)
    namespace, owner = _namespace(settings_obj)
    transport = namespace.split(":", 1)[0]
    receipts = _receipts(payload, owner, transport=transport)
    accepted = rejected = duplicates = 0
    from management.models import IgWebhookInboxEvent
    with transaction.atomic():
        for receipt in receipts:
            digest = hashlib.sha256(_canonical(receipt["payload"])).hexdigest()
            _row, created = IgWebhookInboxEvent.objects.get_or_create(
                namespace=namespace, event_key=receipt["event_key"],
                    defaults={"owner_id": receipt["owner_id"], "customer_igsid": receipt.get("customer_igsid", ""), "decision": receipt["decision"], "reason": receipt["reason"], "payload": receipt["payload"], "payload_digest": digest},
            )
            if not created:
                duplicates += 1
            elif receipt["decision"] == "accepted":
                accepted += 1
            else:
                rejected += 1
    return InboxAcceptance(accepted, rejected, duplicates)


def drain_webhook_inbox(settings_obj, *, limit: int = 25) -> int:
    """Consume one namespace under a short DB transaction; root wires daemon."""
    from management.models import IgWebhookInboxEvent
    from management.services import instagram_bot as bot
    from management.services.ig_db_circuit import (
        DbActiveCapacityError, DbCircuitOpen, db_active_slot, record_db_failure, release_idle_connection,
    )

    namespace, _owner = _namespace(settings_obj)
    processed = 0
    for _ in range(max(1, min(int(limit), 100))):
        now = timezone.now()
        row = None
        try:
            with db_active_slot():
                with transaction.atomic():
                    row = (IgWebhookInboxEvent.objects.select_for_update(skip_locked=True).filter(
                        namespace=namespace,
                        decision=IgWebhookInboxEvent.Decision.ACCEPTED,
                        processed_at__isnull=True,
                    ).filter(
                        models.Q(next_attempt_at__isnull=True) | models.Q(next_attempt_at__lte=now),
                    ).order_by("attempts", "id").first())
                    if row is None:
                        break
                    if _mid_namespace_state(row, namespace) == "blocked":
                        row.decision = IgWebhookInboxEvent.Decision.BLOCKED
                        row.reason = "provider_mid_namespace_unproven"
                        row.next_attempt_at = None
                        row.last_error = ""
                        row.save(update_fields=["decision", "reason", "next_attempt_at", "last_error"])
                        _queue_identity_reconciliation_notification(namespace)
                        continue
                    # persistence_only is verified to do only durable DB work:
                    # no provider, model, media, or notification transport calls.
                    bot.handle_webhook_payload(settings_obj, row.payload, persistence_only=True)
                    if _mid_namespace_state(row, namespace, require_materialized=True) == "blocked":
                        row.decision = IgWebhookInboxEvent.Decision.BLOCKED
                        row.reason = "provider_mid_namespace_unproven"
                        row.next_attempt_at = None
                        row.last_error = ""
                        row.save(update_fields=["decision", "reason", "next_attempt_at", "last_error"])
                        _queue_identity_reconciliation_notification(namespace)
                        continue
                    row.processed_at = timezone.now()
                    row.attempts = models.F("attempts") + 1
                    row.last_error = ""
                    row.next_attempt_at = None
                    row.payload = _processed_payload(row.payload)
                    row.save(update_fields=["processed_at", "attempts", "last_error", "next_attempt_at", "payload"])
        except Exception as exc:
            if isinstance(exc, DbActiveCapacityError):
                break
            if record_db_failure(exc, lane="webhook_inbox"):
                # Materialization and processed_at rolled back together. The
                # receipt remains durable; do not attempt another DB write.
                release_idle_connection()
                raise DbCircuitOpen("webhook database materialization deferred") from exc
            if row is None:
                raise
            retry_at = timezone.now() + timedelta(seconds=min(300, 15 * (2 ** min(int(row.attempts), 4))))
            IgWebhookInboxEvent.objects.filter(pk=row.pk, processed_at__isnull=True).update(attempts=models.F("attempts") + 1, last_error=type(exc).__name__[:64], next_attempt_at=retry_at)
            continue
        processed += 1
    return processed


def has_pending_ingress(settings_obj, customer_igsid: str) -> bool:
    """Fence one customer's effects until accepted or blocked receipts resolve."""
    from management.models import IgWebhookInboxEvent

    namespace, _owner = _namespace(settings_obj)
    customer_igsid = str(customer_igsid or "").strip()
    return bool(customer_igsid and IgWebhookInboxEvent.objects.filter(
        namespace=namespace,
        customer_igsid=customer_igsid,
        decision__in=[IgWebhookInboxEvent.Decision.ACCEPTED, IgWebhookInboxEvent.Decision.BLOCKED],
        processed_at__isnull=True,
    ).exists())


def inbox_status(settings_obj) -> dict:
    """Read-only, redacted ingress health for the current provider namespace."""
    from management.models import IgWebhookInboxEvent

    namespace, _owner = _namespace(settings_obj)
    current = IgWebhookInboxEvent.objects.filter(namespace=namespace)
    pending = current.filter(
        decision=IgWebhookInboxEvent.Decision.ACCEPTED,
        processed_at__isnull=True,
    )
    reasons = list(current.filter(
        decision=IgWebhookInboxEvent.Decision.BLOCKED,
    ).values("reason").annotate(total=Count("id")).order_by("reason")[:10])
    return {
        "namespace_fingerprint": hashlib.sha256(namespace.encode()).hexdigest()[:16],
        "pending": pending.count(),
        "blocked": current.filter(decision=IgWebhookInboxEvent.Decision.BLOCKED).count(),
        "rejected": current.filter(decision=IgWebhookInboxEvent.Decision.REJECTED).count(),
        "processed": current.filter(processed_at__isnull=False).count(),
        "stale_namespace_pending": IgWebhookInboxEvent.objects.exclude(namespace=namespace).filter(
            decision=IgWebhookInboxEvent.Decision.ACCEPTED,
            processed_at__isnull=True,
        ).count(),
        "oldest_pending_at": pending.aggregate(value=Min("received_at"))["value"],
        "blocked_reasons": [
            {"reason": str(item.get("reason") or "")[:64], "count": int(item.get("total") or 0)}
            for item in reasons
        ],
    }


def _queue_identity_reconciliation_notification(namespace: str) -> None:
    """One operator task per namespace; no customer or provider payload leaks."""
    from management.services.ig_alerts import management_base_url
    from management.services.instagram_bot import notify_manager

    fingerprint = hashlib.sha256(str(namespace or "").encode()).hexdigest()[:24]
    try:
        notify_manager(
            "⚠️ Перевірка вхідних повідомлень\n\n"
            "Повідомлення збережені, але їх автоматичну обробку призупинено: "
            "потрібно перевірити прив’язку до Instagram-акаунта. "
            "Повторних відповідей клієнтам не надсилаємо.",
            dedupe_key=f"ig-webhook-inbox-identity:{fingerprint}",
            event_type="generic",
            metadata={"requires_human_review": True, "namespace_fingerprint": fingerprint},
            reply_markup={"inline_keyboard": [[{"text": "Відкрити CRM", "url": f"{management_base_url()}/bot/"}]]},
            deliver_immediately=False,
        )
    except Exception:
        return


def _row_event(payload):
    try:
        entry = payload["entry"][0]
        messaging = entry.get("messaging")
        if isinstance(messaging, list) and messaging and isinstance(messaging[0], dict):
            return messaging[0]
        changes = entry.get("changes")
        if isinstance(changes, list) and changes and isinstance(changes[0], dict):
            value = changes[0].get("value")
            return value if isinstance(value, dict) else {}
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    return {}


def _mid_namespace_state(row, namespace: str, *, require_materialized: bool = False) -> str:
    """Block cross-namespace MID collisions without changing provider IDs."""
    from management.models import IgClient, InstagramBotMessage

    if not hasattr(InstagramBotMessage, "provider_namespace"):
        return "blocked"
    try:
        event = _row_event(row.payload)
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        postback = event.get("postback") if isinstance(event.get("postback"), dict) else {}
        mid = str(message.get("mid") or postback.get("mid") or "").strip()
        sender = str((event.get("sender") or {}).get("id") or "").strip()
        recipient = str((event.get("recipient") or {}).get("id") or "").strip()
        is_echo = bool(message.get("is_echo"))
    except (TypeError, AttributeError):
        return "blocked"
    if IgClient.objects.filter(
        igsid=recipient if is_echo else sender,
        privacy_erasure_started_at__isnull=False,
    ).exists():
        return "ignored"
    if not mid:
        return "ok"
    if message.get("is_deleted") or message.get("is_unsupported"):
        return "ignored"
    if is_echo:
        from management.services.ig_outgoing_registry import is_our_outgoing
        from management.services import instagram_bot as bot
        from management.services.ig_revision_echo_integration import uses_revision_echo_scope

        if uses_revision_echo_scope(namespace, recipient):
            from management.models import IgDeferredEcho, IgRevisionDeliveryEffect

            if is_our_outgoing(mid, recipient_id=recipient, provider_namespace=namespace):
                return "ignored"
            if IgRevisionDeliveryEffect.objects.filter(
                provider_namespace=namespace, recipient_igsid=recipient,
                revision__client__igsid=recipient, provider_message_id=mid, state="sent",
            ).exists():
                return "ignored"
            echo = IgDeferredEcho.objects.filter(provider_namespace=namespace, provider_message_id=mid).first()
            if echo is not None:
                # An unresolved attribution is itself durable materialization;
                # forcing a manager message here would invent its author.
                return "ok" if echo.recipient_igsid == recipient and echo.client.igsid == recipient else "blocked"
        elif is_our_outgoing(mid) or (message.get("text") and bot.cache.get(bot._bot_sent_key(recipient, message.get("text")))):
            return "ignored"
    existing = InstagramBotMessage.objects.filter(mid=mid).first()
    if existing is None:
        return "blocked" if require_materialized else "ok"
    expected_sender = recipient if is_echo else sender
    expected_role = InstagramBotMessage.Role.MANAGER if is_echo else InstagramBotMessage.Role.USER
    return "ok" if (
        str(existing.provider_namespace or "") == namespace
        and existing.sender_id == expected_sender
        and existing.role == expected_role
    ) else "blocked"


def _processed_payload(payload) -> dict:
    """Retain only source identifiers after materialization, never attachment URLs."""
    try:
        event = payload["entry"][0]["messaging"][0]
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        mid = str(message.get("mid") or "").strip()
        sender = str((event.get("sender") or {}).get("id") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return {}
    return {key: value for key, value in {"mid": mid, "sender_id": sender}.items() if value}
