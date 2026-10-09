"""Receipt-bound post-purchase business consent, never a native future grant."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone as datetime_timezone
import hashlib
import hmac
import json
import secrets

from django.conf import settings
from django.core.signing import BadSignature, Signer
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, OuterRef, Q, Subquery, Window
from django.db.models.functions import RowNumber
from django.utils import timezone
from types import SimpleNamespace

from management.ig_consent_models import (
    INVITATION_IMMUTABLE, IgMarketingConsentAnswer, IgMarketingConsentInvitation,
)
from management.services.ig_message_templates import QuickReply, QuickReplyMessage, send_quick_replies
from management.services.ig_reply_boundary import capture_reply_permission, customer_send_boundary

PURPOSE = "post_purchase_marketing"
PREFIX = "twc-consent:1:"
WINDOW = timedelta(hours=23)
VALIDITY = timedelta(days=90)
LEASE = timedelta(minutes=2)
SERVICE_HOLD_BACKOFF = timedelta(minutes=5)
COPY = {
    "uk": ("Дякуємо за замовлення 💜\n\nХочеш отримувати новинки й пропозиції TwoComms, а після отримання замовлення — дізнатися умови бонусу за сторіс? Підпишися за бажанням. Відмовитися можна будь-коли.", "Так, хочу", "Не зараз", "Відмовитися"),
    "ru": ("Спасибо за заказ 💜\n\nХочешь получать новости и предложения TwoComms, а после получения заказа — узнать условия бонуса за сторис? Подпишись по желанию. Отказаться можно в любой момент.", "Да, хочу", "Не сейчас", "Отказаться"),
    "en": ("Thank you for your order 💜\n\nWant TwoComms news and offers, plus details of our story bonus after your order arrives? Subscribe if you like. You can opt out anytime.", "Yes, keep me updated", "Not now", "Opt out"),
}
ACK = {
    "uk": {"accept": "Згоду на новинки й пропозиції TwoComms зафіксовано. Відмовитися можна будь-коли.", "decline": "Добре, згоду на новинки й пропозиції не надано. Сервіс щодо замовлення доступний як і раніше.", "revoke": "Згоду на новинки й пропозиції відкликано. Сервіс щодо замовлення доступний як і раніше.", "invalid": "Це запрошення вже недоступне. Згоду не змінено."},
    "ru": {"accept": "Согласие на новости и предложения TwoComms сохранено. Отказаться можно в любой момент.", "decline": "Хорошо, согласие на новости и предложения не предоставлено. Сервис по заказу доступен как прежде.", "revoke": "Согласие на новости и предложения отозвано. Сервис по заказу доступен как прежде.", "invalid": "Это приглашение уже недоступно. Согласие не изменено."},
    "en": {"accept": "Your consent to TwoComms news and offers is saved. You can opt out anytime.", "decline": "No consent to news and offers was given. Order support remains available.", "revoke": "Your consent to news and offers is revoked. Order support remains available.", "invalid": "This invitation is no longer available. Your consent was not changed."},
}


def _enabled():
    return bool(getattr(settings, "IG_POST_PURCHASE_BUSINESS_CONSENT_ENABLED", False))


def _canonical(value):
    def encode(item):
        if isinstance(item, datetime):
            if timezone.is_naive(item):
                raise ValueError("consent_time_not_aware")
            return item.astimezone(datetime_timezone.utc).isoformat()
        return str(item)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      default=encode).encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _keys():
    return {hashlib.sha256(str(key).encode()).hexdigest()[:24]: str(key)
            for key in [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])] if key}


def _mac(domain, value, key_id):
    key = _keys().get(key_id)
    if not key:
        raise ValueError("consent_signing_key_unavailable")
    return hmac.new(key.encode(), domain.encode() + b"\0" + _canonical(value), hashlib.sha256).hexdigest()


def _snapshot(invitation):
    return {field: getattr(invitation, field) for field in INVITATION_IMMUTABLE if field != "snapshot_hmac"} | {"id": str(invitation.pk)}


def _invitation_valid(invitation):
    try:
        return hmac.compare_digest(invitation.snapshot_hmac, _mac("post-purchase-consent.v1", _snapshot(invitation), invitation.signing_key_id))
    except (ValueError, TypeError):
        return False


def _receipt_body(invitation):
    return {"id": str(invitation.pk), "snapshot_hmac": invitation.snapshot_hmac,
        "provider_message_id": invitation.provider_message_id, "sent_at": invitation.sent_at,
        "provider_started_at": invitation.provider_started_at}


def _receipt_valid(invitation):
    try:
        return bool(invitation.state == "sent" and invitation.provider_message_id and invitation.sent_at
            and hmac.compare_digest(invitation.receipt_hmac, _mac("post-purchase-consent-receipt.v1",
                _receipt_body(invitation), invitation.signing_key_id)))
    except (ValueError, TypeError):
        return False


def _current_source(client, namespace, now, *, reset_boundary=0, lock=False):
    """The optional sender requires a real provider-dated USER, not a cache."""
    from management.models import InstagramBotMessage
    rows = InstagramBotMessage.objects.filter(client_id=client.pk, sender_id=client.igsid,
        role="user", source__in=["webhook", "poll"], provider_namespace=namespace,
        pk__gt=reset_boundary, provider_created_at__gt=now - WINDOW,
        provider_created_at__lte=now).exclude(mid__isnull=True).exclude(mid="").exclude(status="failed")
    if lock:
        rows = rows.select_for_update()
    return rows.order_by("-provider_created_at", "-pk").first()


def _message_digest(row):
    return _digest({field: getattr(row, field) for field in (
        "pk", "client_id", "sender_id", "role", "source", "provider_namespace", "mid",
        "text", "quick_reply_payload", "provider_created_at", "created_at",
    )})


def _payment_digest(order):
    payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
    return _digest({"order_id": order.pk, "source": order.source, "payment_status": order.payment_status,
                    "proof": {key: payload.get(key) for key in (
                        "manual_payment_action", "provider_payment_confirmed", "legacy_payment_transition",
                        "manual_payment_evidence_confirmed", "manager_payment_decision_id", "ig_payment_reconciliation",
                    )}})


def _reset(client_id):
    from management.models import IgFunnelResetAudit
    return IgFunnelResetAudit.objects.filter(client_id=client_id).order_by("-pk").values("pk", "reset_after_message_id").first() or {"pk": 0, "reset_after_message_id": 0}


def _blocked(client):
    if client is None or client.privacy_erasure_started_at or client.hidden_at or client.is_blocked:
        return "client_unavailable"
    # Resuming ordinary bot replies cannot resurrect marketing consent.
    if client.opted_out_at:
        return "client_opted_out"
    if client.bot_paused or client.manager_takeover:
        return "client_paused"
    return ""


def _scope_reason(invitation, client, order, assignment, reset_id, *, now, verify_payment=True):
    if not _invitation_valid(invitation):
        return "invitation_integrity_invalid"
    reason = _blocked(client)
    if reason:
        return reason
    if invitation.expires_at <= now:
        return "consent_expired"
    if (client.pk != invitation.client_id or str(client.igsid) != invitation.recipient_igsid
            or order.pk != invitation.order_id or order.status == "cancelled"
            or order.payment_status != "paid" or _payment_digest(order) != invitation.payment_digest):
        return "purchase_source_changed"
    if (assignment is None or assignment.pk != invitation.assignment_id or assignment.client_id != client.pk
            or assignment.order_id != order.pk or assignment.version != invitation.assignment_version
            or assignment.unassigned_at is not None or reset_id != invitation.reset_audit_id):
        return "consent_scope_changed"
    if verify_payment:
        from management.services.ig_order_links import order_fulfillment_payment_verified
        if not order_fulfillment_payment_verified(order):
            return "payment_unverified"
    source = invitation.source_message
    if (source is None or source.client_id != client.pk or source.sender_id != invitation.recipient_igsid
            or source.role != "user" or source.source not in {"webhook", "poll"}
            or source.provider_namespace != invitation.provider_namespace or not source.mid or source.status == "failed"
            or _message_digest(source) != invitation.source_digest):
        return "invitation_source_changed"
    return ""


def _pre_shipment_reason(order):
    """Optional acquisition belongs before any shipment, including old parcels."""
    from management.ig_bot_models import IgOrderShipment
    if (order.status not in {"new", "prep"} or str(order.tracking_number or "").strip()
            or order.nova_poshta_document_ref or order.shipment_status
            or order.tracking_status_code is not None or order.tracking_terminal_at is not None
            or IgOrderShipment.objects.using(order._state.db or "default").filter(order_id=order.pk).exists()):
        return "purchase_already_shipped"
    return ""


def _promotion_send_check(invitation, client, order, reset, *, now):
    """Send-only checks must not rewrite accepted consent or old answer proofs."""
    from management.services.ig_service_complaints import promotion_service_hold_reason
    reason = _pre_shipment_reason(order)
    if reason:
        return reason, ""
    reason = promotion_service_hold_reason(client, now=now)
    if reason:
        return reason, "service_hold"
    source = _current_source(client, invitation.provider_namespace, now,
        reset_boundary=reset["reset_after_message_id"], lock=True)
    if source is None:
        return "standard_window_closed", ""
    if source.pk != invitation.source_message_id or _message_digest(source) != invitation.source_digest:
        return "invitation_source_superseded", ""
    # A later-ingested real USER may have an earlier provider timestamp. It is
    # still intervening customer activity and must not be hidden by sort order.
    if _current_source(client, invitation.provider_namespace, now,
            reset_boundary=max(reset["reset_after_message_id"], invitation.source_message_id), lock=True):
        return "invitation_source_superseded", ""
    if source.status != "done":
        return "source_reply_pending", "waiting_source"
    return "", ""


def _payload(invitation, choice):
    body = {"id": invitation.pk.hex, "choice": choice, "client": invitation.client_id,
            "order": invitation.order_id, "assignment": invitation.assignment_id,
            "version": invitation.assignment_version, "reset": invitation.reset_audit_id,
            "namespace": invitation.provider_namespace, "purpose": invitation.purpose,
            "expiry": invitation.expires_at.isoformat()}
    return PREFIX + invitation.signing_key_id + ":" + Signer(key=_keys()[invitation.signing_key_id], fallback_keys=[], salt="marketing-consent-button.v1").sign_object(body, compress=True)


def consent_quick_reply_message(invitation):
    copy = COPY[invitation.locale]
    # Old signed decline payloads remain valid, but new delivery offers one
    # affirmative action. The immutable body and all signed payloads stay intact.
    return QuickReplyMessage(invitation.message_snapshot,
        (QuickReply(copy[1], invitation.accept_payload),),
        projection_text=invitation.message_snapshot)


def queue_post_purchase_consent(client, order, assignment, *, now=None):
    """Queue only a natural, current-window, exact paid owned purchase."""
    if not _enabled():
        return None
    from management.models import IgClient, IgOrderAssignment, InstagramBotMessage, InstagramBotSettings
    from management.services.instagram_bot import ingress_provider_namespace
    from management.services.ig_order_links import order_fulfillment_payment_verified
    from management.services.ig_reply_language import resolve_own_source_reply_language
    from management.services.ig_service_complaints import promotion_service_hold_reason
    from orders.models import Order

    now = now or timezone.now()
    with transaction.atomic():
        current = IgClient.objects.select_for_update().get(pk=getattr(client, "pk", client))
        order = Order.objects.select_for_update().get(pk=getattr(order, "pk", order))
        assignment = IgOrderAssignment.objects.select_for_update().filter(pk=getattr(assignment, "pk", assignment), client_id=current.pk, order_id=order.pk, unassigned_at__isnull=True).first()
        settings_obj = InstagramBotSettings.objects.order_by("pk").first()
        if (_blocked(current) or assignment is None or order.status == "cancelled" or order.payment_status != "paid"
                or not order_fulfillment_payment_verified(order) or settings_obj is None):
            return None
        if _pre_shipment_reason(order) or promotion_service_hold_reason(current, now=now):
            return None
        namespace = ingress_provider_namespace(settings_obj)
        permission = capture_reply_permission(settings_obj.pk, current.pk)
        if not namespace or not permission:
            return None
        reset = _reset(current.pk)
        source = _current_source(current, namespace, now, reset_boundary=reset["reset_after_message_id"], lock=True)
        if source is None or source.status != "done":
            return None
        existing = IgMarketingConsentInvitation.objects.filter(client_id=current.pk, order_id=order.pk,
            assignment_id=assignment.pk, assignment_version=assignment.version, reset_audit_id=reset["pk"], purpose=PURPOSE).first()
        if existing is not None:
            return existing if _invitation_valid(existing) else None
        language = resolve_own_source_reply_language(sources=[{
            "message_id": source.pk, "role": "user", "text": source.text,
            "event_at": (source.provider_created_at or source.created_at).isoformat(),
            "source_digest": _message_digest(source),
        }], profile_language=current.language, reset_floor=reset["reset_after_message_id"] + 1,
            watermark_message_id=source.pk, watermark_event_at=source.provider_created_at or source.created_at)
        locale = language.template_family if language.template_family in COPY else language.knowledge_locale
        invitation = IgMarketingConsentInvitation(client=current, order=order, assignment=assignment,
            assignment_version=assignment.version, reset_audit_id=reset["pk"], recipient_igsid=current.igsid,
            provider_namespace=namespace, settings_id_snapshot=settings_obj.pk, locale=locale,
            language_proof=language.as_dict(), source_message=source, source_digest=_message_digest(source),
            payment_digest=_payment_digest(order), message_snapshot=COPY[locale][0], issued_at=now,
            expires_at=now + VALIDITY, signing_key_id=next(iter(_keys())), created_at=now)
        invitation.accept_payload = _payload(invitation, "accept")
        invitation.decline_payload = _payload(invitation, "decline")
        invitation.revoke_payload = _payload(invitation, "revoke")
        invitation.snapshot_hmac = _mac("post-purchase-consent.v1", _snapshot(invitation), invitation.signing_key_id)
        invitation.save(force_insert=True)
        return invitation


def _locked_invitation(invitation_id):
    from management.models import IgClient, IgOrderAssignment, InstagramBotMessage
    from orders.models import Order
    identity = IgMarketingConsentInvitation.objects.filter(pk=invitation_id).values("client_id", "order_id", "assignment_id").first()
    if not identity:
        return None
    client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
    order = Order.objects.select_for_update().filter(pk=identity["order_id"]).first()
    invitation = IgMarketingConsentInvitation.objects.select_for_update().get(pk=invitation_id)
    assignment = IgOrderAssignment.objects.select_for_update().filter(pk=identity["assignment_id"]).first()
    source = InstagramBotMessage.objects.select_for_update().filter(pk=invitation.source_message_id).first()
    invitation._state.fields_cache["source_message"] = source
    return invitation, client, order, assignment


@contextmanager
def _send_boundary(invitation_id, token, permission, result=None):
    from management.models import InstagramBotSettings
    from management.services.instagram_bot import ingress_provider_namespace
    with customer_send_boundary(permission.settings_id, permission.client_id, permission) as allowed:
        with transaction.atomic():
            locked = _locked_invitation(invitation_id)
            reason = "invitation_missing" if not locked else ""
            hold_state = ""
            if locked:
                invitation, client, order, assignment = locked
                now = timezone.now()
                if not _enabled():
                    reason = "feature_disabled"
                elif (invitation.state != "processing" or invitation.lease_token != token
                        or invitation.lease_until is None or invitation.lease_until <= now):
                    reason = "invitation_claim_changed"
                elif client is None or order is None:
                    reason = "source_missing"
                else:
                    reset = _reset(client.pk)
                    reason = _scope_reason(invitation, client, order, assignment, reset["pk"], now=now)
                settings_obj = InstagramBotSettings.objects.filter(pk=invitation.settings_id_snapshot).first()
                if not reason and (not allowed or settings_obj is None or ingress_provider_namespace(settings_obj) != invitation.provider_namespace):
                    reason = "permission_or_namespace_changed"
                if not reason:
                    reason, hold_state = _promotion_send_check(invitation, client, order, reset, now=now)
                if not reason and invitation.answers.exists():
                    reason = "consent_answer_already_present"
            if result is not None:
                result.update(reason=reason, hold_state=hold_state)
            yield not bool(reason)


def process_consent_invitation(invitation_id):
    """One physical attempt; UNKNOWN and abandoned PROCESSING never replay."""
    if not _enabled():
        return "off"
    from management.models import InstagramBotSettings
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    now = timezone.now()
    with transaction.atomic():
        locked = _locked_invitation(invitation_id)
        if not locked:
            return "missing"
        invitation, client, order, assignment = locked
        if invitation.state in {"sent", "unknown", "failed"}:
            return invitation.state
        if invitation.state == "processing":
            if invitation.lease_until and invitation.lease_until > now:
                return "busy"
            invitation.state = "unknown"
            invitation.last_error = "abandoned_provider_attempt"
            invitation.lease_token = ""
            invitation.lease_until = None
            invitation.save(update_fields=["state", "last_error", "lease_token", "lease_until", "updated_at"])
            return "unknown"
        reset = _reset(client.pk) if client is not None else {"pk": 0, "reset_after_message_id": 0}
        reason = "source_missing" if client is None or order is None else (
            _scope_reason(invitation, client, order, assignment, reset["pk"], now=now)
            or _pre_shipment_reason(order))
        if reason:
            invitation.state = "failed"
            invitation.last_error = reason
            invitation.save(update_fields=["state", "last_error", "updated_at"])
            return "failed"
        settings_obj = InstagramBotSettings.objects.filter(pk=invitation.settings_id_snapshot).first()
        permission = capture_reply_permission(invitation.settings_id_snapshot, client.pk)
        if not permission or _current_source(client, invitation.provider_namespace, now,
            reset_boundary=reset["reset_after_message_id"], lock=True) is None:
            return "waiting_window"
        reason, hold_state = _promotion_send_check(invitation, client, order, reset, now=now)
        if reason:
            invitation.last_error = reason
            if not hold_state:
                invitation.state = "failed"
            invitation.save(update_fields=["state", "last_error", "updated_at"])
            return hold_state or "failed"
        invitation.state = "processing"
        invitation.attempts += 1
        invitation.lease_token = secrets.token_hex(24)
        invitation.lease_until = now + LEASE
        invitation.provider_started_at = now
        invitation.save(update_fields=["state", "attempts", "lease_token", "lease_until", "provider_started_at", "updated_at"])
        token = invitation.lease_token
    boundary_result = {}
    try:
        delivery = send_quick_replies(settings_obj, invitation.recipient_igsid,
            consent_quick_reply_message(invitation), allow_text_fallback=False,
            permission_boundary_factory=lambda: _send_boundary(invitation.pk, token, permission, boundary_result))
        mid = normalize_provider_message_id(getattr(delivery, "provider_message_id", ""))
        outcome = "sent" if delivery.ok and mid else "unknown" if delivery.ok or delivery.kind in {"unknown", "transient", "ambiguous"} else "failed"
        error = "" if outcome == "sent" else "provider_outcome_unknown" if outcome == "unknown" else str(delivery.kind or "provider_failed")[:80]
        if delivery.kind == "cancelled" and boundary_result.get("reason"):
            error = boundary_result["reason"]
            if mid or getattr(delivery, "provider_message_id", ""):
                # A receipt claim contradicts a known unsent denial. Preserve
                # the ambiguous attempt permanently rather than reopening it.
                outcome, error = "unknown", "provider_outcome_unknown"
            # A claimed attempt cannot return to PENDING under the physical
            # ledger guard. This known unsent denial is terminal FAILED; only
            # pre-claim holds can wait and retry without creating an attempt.
    except Exception:
        mid, outcome, error = "", "unknown", "provider_exception"
    with transaction.atomic():
        invitation = IgMarketingConsentInvitation.objects.select_for_update().get(pk=invitation_id)
        if invitation.state != "processing" or invitation.lease_token != token:
            return invitation.state
        invitation.state = outcome
        invitation.provider_message_id = mid if outcome in {"sent", "unknown"} else ""
        invitation.sent_at = timezone.now() if outcome == "sent" else None
        invitation.receipt_hmac = _mac("post-purchase-consent-receipt.v1", _receipt_body(invitation), invitation.signing_key_id) if outcome == "sent" else ""
        invitation.last_error = error
        invitation.lease_token = ""
        invitation.lease_until = None
        fields = ["state", "provider_message_id", "receipt_hmac", "sent_at", "last_error", "lease_token", "lease_until", "updated_at"]
        invitation.save(update_fields=fields)
    return outcome


def _answer_body(answer):
    return {field: getattr(answer, field) for field in (
        "invitation_id", "source_message_id", "provider_namespace", "source_mid", "choice",
        "source_digest", "answered_at", "recorded_at", "signing_key_id",
    )}


def _answer_valid(answer, invitation):
    try:
        source = answer.source_message
        event_at = source.provider_created_at or source.created_at
        return bool(answer.choice in {"accept", "decline", "revoke"}
            and answer.invitation_id == invitation.pk and source.client_id == invitation.client_id
            and source.sender_id == invitation.recipient_igsid and source.role == "user"
            and source.source in {"webhook", "poll"} and source.mid == answer.source_mid
            and source.provider_namespace == answer.provider_namespace == invitation.provider_namespace
            and answer.source_mid and _message_digest(source) == answer.source_digest
            and source.quick_reply_payload == getattr(invitation, answer.choice + "_payload")
            and event_at == answer.answered_at and invitation.issued_at <= event_at < invitation.expires_at
            and hmac.compare_digest(answer.answer_hmac, _mac("post-purchase-consent-answer.v1", _answer_body(answer), answer.signing_key_id)))
    except (ValueError, TypeError, AttributeError):
        return False


def handle_consent_reply(row):
    """Only a signed live button answers a sent, exact-scoped invitation."""
    raw = str(getattr(row, "quick_reply_payload", "") or "")
    if not raw.startswith(PREFIX):
        return None
    from management.services.ig_postback_router import PostbackOutcome
    from management.models import InstagramBotMessage
    locale, choice, invitation = "uk", "invalid", None
    try:
        key_id, encoded = raw[len(PREFIX):].split(":", 1)
        key = _keys().get(key_id)
        if key is None or len(raw) > 1000:
            raise ValueError("button_unavailable")
        body = Signer(key=key, fallback_keys=[], salt="marketing-consent-button.v1").unsign_object(encoded)
        with transaction.atomic():
            locked = _locked_invitation(body["id"])
            if locked is None:
                raise ValueError("invitation_missing")
            invitation, client, order, assignment = locked
            locale = invitation.locale if invitation.locale in COPY else "uk"
            source = InstagramBotMessage.objects.select_for_update().get(pk=row.pk)
            now = timezone.now()
            selected = body.get("choice")
            if selected not in {"accept", "decline", "revoke"} or raw != getattr(invitation, selected + "_payload"):
                raise ValueError("button_changed")
            if selected == "accept" and not _enabled():
                raise ValueError("feature_disabled")
            if (not _receipt_valid(invitation)
                    or client is None or order is None or source.client_id != client.pk
                    or source.sender_id != invitation.recipient_igsid or source.role != "user"
                    or source.source not in {"webhook", "poll"} or source.provider_namespace != invitation.provider_namespace
                    or not source.mid or source.quick_reply_payload != raw):
                raise ValueError("answer_source_invalid")
            reason = _scope_reason(invitation, client, order, assignment, _reset(client.pk)["pk"], now=now)
            if reason:
                raise ValueError(reason)
            event_at = source.provider_created_at or source.created_at
            if not invitation.issued_at <= event_at <= now or event_at >= invitation.expires_at:
                raise ValueError("answer_time_invalid")
            latest = invitation.answers.select_related("source_message").order_by("-answered_at", "-id").first()
            if latest:
                if not _answer_valid(latest, invitation):
                    raise ValueError("answer_integrity_invalid")
                if latest.choice in {"decline", "revoke"} or latest.choice == selected:
                    choice = latest.choice
                elif event_at < latest.answered_at:
                    raise ValueError("answer_source_stale")
            if choice == "invalid":
                answer = IgMarketingConsentAnswer(invitation=invitation, source_message=source,
                    provider_namespace=source.provider_namespace, source_mid=source.mid, choice=selected,
                    source_digest=_message_digest(source), answered_at=event_at, recorded_at=now,
                    signing_key_id=invitation.signing_key_id)
                answer.answer_hmac = _mac("post-purchase-consent-answer.v1", _answer_body(answer), answer.signing_key_id)
                answer.save(force_insert=True)
                choice = selected
    except (BadSignature, ValueError, KeyError, TypeError, AttributeError, ValidationError, IntegrityError, InstagramBotMessage.DoesNotExist):
        choice = "invalid"
    quick_replies = (QuickReply(COPY[locale][3], invitation.revoke_payload),) if choice == "accept" else ()
    return PostbackOutcome(action="post_purchase_marketing_consent", reply_text=ACK[locale][choice],
                           reason="business_consent_" + choice, quick_replies=quick_replies)


def _empty_projection():
    return {"schema": "journey-consent.v2", "channel": "instagram", "purpose": PURPOSE,
        "native_receipts_available": False, "native_grant": "unverified", "provider_basis": "standard_window",
        "delivery": {"status": "waiting", "evidence_refs": []},
        "invitation": {"id": None, "status": "unavailable", "evidence_refs": []},
        "response": {"status": "unknown", "evidence_refs": []},
        "permission": {"status": "unconfirmed", "evidence_refs": []},
        "note": "Згода на маркетинг і дозвіл каналу — окремі факти. Нативний дозвіл на майбутні повідомлення не підтверджено."}


def business_consent_projections(client, order_ids, *, now=None):
    """Two ledger queries with eager current sources; no generation or writes."""
    from management.models import IgFunnelResetAudit, InstagramBotSettings
    from management.services.instagram_bot import ingress_provider_namespace
    now = now or timezone.now()
    ids = sorted({int(value) for value in order_ids if str(value).isdigit() and int(value) > 0})[:20]
    result = {order_id: _empty_projection() for order_id in ids}
    latest_reset = IgFunnelResetAudit.objects.filter(client_id=OuterRef("client_id")).order_by("-pk")
    current_settings = InstagramBotSettings.objects.filter(pk=OuterRef("settings_id_snapshot"))
    invitations = list(IgMarketingConsentInvitation.objects.filter(client_id=getattr(client, "pk", client), order_id__in=ids)
        .select_related("client", "order", "assignment", "source_message")
        .annotate(_consent_order_row=Window(expression=RowNumber(), partition_by=[F("order_id")],
                                          order_by=[F("issued_at").desc(), F("id").desc()]))
        .filter(_consent_order_row=1)
        .annotate(current_reset_id=Subquery(latest_reset.values("pk")[:1]),
                  current_ig_user_id=Subquery(current_settings.values("ig_user_id")[:1]),
                  current_page_id=Subquery(current_settings.values("page_id")[:1]))
        .order_by("order_id", "-issued_at", "-pk"))
    by_order = {}
    for invitation in invitations:
        by_order.setdefault(invitation.order_id, invitation)
    answers = IgMarketingConsentAnswer.objects.filter(invitation_id__in=[row.pk for row in by_order.values()]).select_related("source_message").order_by("-answered_at", "-pk")
    latest_answers = {}
    for answer in answers:
        latest_answers.setdefault(answer.invitation_id, answer)
    for order_id, invitation in by_order.items():
        view = result[order_id]
        reason = _scope_reason(invitation, invitation.client, invitation.order, invitation.assignment,
                               invitation.current_reset_id or 0, now=now, verify_payment=False)
        if reason == "consent_expired":
            # Expiry removes eligibility, while retaining the verified receipt
            # and answer facts for the visible audit. Other failures expose no
            # historical scope as current authority.
            reason = _scope_reason(invitation, invitation.client, invitation.order, invitation.assignment,
                invitation.current_reset_id or 0, now=invitation.expires_at - timedelta(microseconds=1), verify_payment=False) or "consent_expired"
        namespace = ingress_provider_namespace(SimpleNamespace(ig_user_id=invitation.current_ig_user_id, page_id=invitation.current_page_id))
        if namespace != invitation.provider_namespace:
            reason = "provider_namespace_changed"
        if reason and reason != "consent_expired":
            view["permission"]["status"] = "blocked"
            view["note"] = "Згоду не підтверджено для поточного замовлення або каналу. Сервіс щодо замовлення залишається окремим."
            continue
        refs = [{"kind": "consent_invitation", "id": str(invitation.pk)}]
        receipt_valid = _receipt_valid(invitation)
        view["invitation"] = {"id": str(invitation.pk), "status": "unknown" if invitation.state == "sent" and not receipt_valid else invitation.state,
            "provider_message_id": invitation.provider_message_id if _receipt_valid(invitation) else "",
            "evidence_refs": refs if _receipt_valid(invitation) else [],
            "sent_at": invitation.sent_at.isoformat() if invitation.sent_at else None,
            "expires_at": invitation.expires_at.isoformat()}
        answer = latest_answers.get(invitation.pk)
        if answer and _receipt_valid(invitation) and _answer_valid(answer, invitation):
            status = {"accept": "accepted", "decline": "declined", "revoke": "revoked"}[answer.choice]
            answer_refs = [{"kind": "message", "id": answer.source_message_id}]
            view["response"] = {"status": status, "answer_id": answer.pk,
                "source_message_id": answer.source_message_id, "source_mid": answer.source_mid,
                "answered_at": answer.answered_at.isoformat(), "evidence_refs": answer_refs}
            view["permission"] = {"status": "business_accepted" if status == "accepted" else status,
                "evidence_refs": refs + answer_refs, "expires_at": invitation.expires_at.isoformat()}
        if reason == "consent_expired":
            view["permission"] = {"status": "expired", "evidence_refs": refs,
                                  "expires_at": invitation.expires_at.isoformat()}
            view["note"] = "Строк цієї згоди завершився. Попередні джерела збережено; згода не дає права на нові пропозиції."
    return result


def business_consent_projection(client, order_id):
    return business_consent_projections(client, [order_id]).get(int(order_id), _empty_projection())


def post_purchase_consent_source(client, order_id, *, now=None):
    view = business_consent_projections(client, [order_id], now=now).get(int(order_id), {})
    if (view.get("permission", {}).get("status") != "business_accepted"
            or view.get("invitation", {}).get("status") != "sent" or not view.get("invitation", {}).get("provider_message_id")):
        return None
    invitation, answer = view["invitation"], view["response"]
    return {"schema": "post-purchase-business-consent-source.v1", "purpose": PURPOSE,
        "client_id": int(getattr(client, "pk", client)), "order_id": int(order_id),
        "invitation_id": invitation["id"], "invitation_mid": invitation["provider_message_id"],
        "answer_id": answer["answer_id"], "source_message_id": answer["source_message_id"],
        "source_mid": answer["source_mid"], "answered_at": answer["answered_at"],
        "expires_at": invitation["expires_at"], "provider_basis": "standard_window", "native_grant": "unverified"}


def has_post_purchase_consent(client, order_id, *, now=None):
    return post_purchase_consent_source(client, order_id, now=now) is not None


def reconcile_consent_invitations(*, limit=1):
    """Drain a bounded natural queue; terminal UNKNOWN is never reconsidered."""
    if not _enabled():
        return {"mode": "off", "considered": 0, "states": {}}
    from management.services.ig_service_complaints import HOLD_REASON
    now = timezone.now()
    rows = IgMarketingConsentInvitation.objects.filter(
        Q(state="processing", lease_until__lte=now) | Q(state="processing", lease_until__isnull=True) | Q(state="pending"))
    # Pending requests only compete for the drain while an actual inbound
    # customer window is open. Closed old windows cannot starve fresh clients.
    rows = rows.filter(Q(state="processing") | Q(client__last_user_message_at__gte=now - WINDOW))
    rows = rows.filter(Q(state="processing") | Q(
        source_message__provider_created_at__gt=now - WINDOW,
        source_message__provider_created_at__lte=now))
    rows = rows.filter(Q(state="processing") | Q(expires_at__gt=now,
        client__privacy_erasure_started_at__isnull=True, client__hidden_at__isnull=True,
        client__bot_paused=False, client__manager_takeover=False))
    # A service hold is known unsent. Its finite cooldown keeps one customer's
    # unresolved issue from taking every bounded drain slot from other clients.
    rows = rows.exclude(state="pending", last_error__in=[HOLD_REASON, "source_reply_pending"],
        updated_at__gt=now - SERVICE_HOLD_BACKOFF)
    selected = list(rows.order_by("issued_at", "pk").values_list("pk", flat=True)[:max(1, min(int(limit or 1), 20))])
    states = {}
    for invitation_id in selected:
        state = process_consent_invitation(invitation_id)
        states[state] = states.get(state, 0) + 1
    return {"mode": "standard_window", "considered": len(selected), "states": states}
