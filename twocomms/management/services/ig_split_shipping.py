"""Immutable full-basket delivery terms and actual customer service choices.

An offer, native delivery receipt, and customer choice are three independent
proofs. None grants inventory, price, payment, order, or shipment authority.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import re
import uuid

from django.db import transaction
from django.utils import timezone

from management.ig_split_shipping_models import (
    IgSplitShippingOffer as Offer, IgSplitShippingReceipt as Receipt,
    IgSplitShippingChoice as Choice,
)
from management.services import ig_fulfillment_reviews as reviews

PURPOSE = "split-shipping.v1"
PREFIX = "SS1:"
ORIGIN = "split_shipping_offer"
MAX_BYTES = 950


class SplitShippingError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SplitChoiceResult:
    handled: bool = False
    ready: bool = False
    reason: str = "not_handled"
    reply_text: str = ""
    quick_replies: tuple = ()
    offer_id: str = ""
    choice_id: str = ""


def _sign(kind, value, key_id):
    key = reviews._keys().get(key_id)
    return hmac.new(key.encode(), (PURPOSE + ":" + kind + ":" + reviews._json(value)).encode(), hashlib.sha256).hexdigest() if key else ""


def _seal(row, kind, payload):
    row.key_id = next(iter(reviews._keys()))
    row.envelope = {"version": 1, "purpose": PURPOSE, "columns": reviews._columns(row), **deepcopy(payload)}
    row.envelope_hmac = _sign(kind, row.envelope, row.key_id)
    row.save(force_insert=True)
    return row


def _verified(row, kind):
    expected = _sign(kind, row.envelope, row.key_id) if row and isinstance(row.envelope, dict) else ""
    return bool(expected and hmac.compare_digest(expected, row.envelope_hmac)
        and row.envelope.get("version") == 1 and row.envelope.get("purpose") == PURPOSE
        and row.envelope.get("columns") == reviews._columns(row))


def verified_offer(row):
    return _verified(row, "offer")


def verified_choice(row):
    return _verified(row, "choice")


def _actor_allowed(actor, *, _fresh_row=False):
    from django.contrib.auth import get_user_model
    from management.bot_access import has_all_bot_capabilities, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
    if not _fresh_row:
        actor = get_user_model().objects.filter(pk=getattr(actor, "pk", None)).first()
    return bool(actor and actor.is_active and has_all_bot_capabilities(actor,
        OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION))


def _options(options, request, decision):
    if not isinstance(options, dict) or set(options) != {"together", "separate"}:
        raise SplitShippingError("options_schema_invalid")
    expected = {str(line["line_id"]) for line in request.envelope["lines"]}
    split = decision.envelope.get("split_offer")
    if split is not None and (not isinstance(split, dict) or split.get("feasible") is not True):
        raise SplitShippingError("split_feasibility_unverified")
    rows = decision.envelope.get("lines") or []
    if any(row.get("availability") not in {"available", "wait"} for row in rows):
        raise SplitShippingError("basket_readiness_unverified")
    clean = {}
    for option in ("together", "separate"):
        data = options[option]
        if not isinstance(data, dict) or set(data) != {"parcels"} or not isinstance(data["parcels"], list):
            raise SplitShippingError("options_schema_invalid")
        parcels = data["parcels"]
        if not (len(parcels) == 1 if option == "together" else 2 <= len(parcels) <= 20):
            raise SplitShippingError("parcel_partition_invalid")
        flat, keys, result = [], set(), []
        for parcel in parcels:
            if not isinstance(parcel, dict) or set(parcel) != {"key", "line_ids", "payer", "customer_delivery_cap", "currency"}:
                raise SplitShippingError("parcel_schema_invalid")
            key, lines, cap = parcel["key"], parcel["line_ids"], parcel["customer_delivery_cap"]
            if not isinstance(key, str) or not re.fullmatch(r"p[1-9][0-9]?", key) or key in keys:
                raise SplitShippingError("parcel_key_invalid")
            if not isinstance(lines, list) or not lines or any(not isinstance(line, str) for line in lines):
                raise SplitShippingError("parcel_partition_invalid")
            try:
                amount = Decimal(cap) if isinstance(cap, str) and re.fullmatch(r"\d{1,6}(?:\.\d{1,2})?", cap) else None
            except InvalidOperation:
                amount = None
            if amount is None or not amount.is_finite() or not 0 <= amount <= 100000 or parcel["currency"] != "UAH":
                raise SplitShippingError("delivery_cap_invalid")
            if parcel["payer"] not in {"customer", "merchant"} or parcel["payer"] == "merchant" and amount != 0:
                raise SplitShippingError("delivery_payer_invalid")
            keys.add(key)
            flat.extend(lines)
            result.append({**parcel, "customer_delivery_cap": format(amount, ".2f")})
        if len(flat) != len(expected) or len(set(flat)) != len(flat) or set(flat) != expected:
            raise SplitShippingError("parcel_partition_invalid")
        # Issuing this separately signed offer is the manager's explicit
        # logistics approval. A legacy optional split hint is not its schema.
        clean[option] = {"parcels": result}
    return clean


def _original(offer):
    request = offer.decision.request
    decision = offer.decision
    if (not verified_offer(offer) or not reviews._verified(request, "request") or not reviews._verified(decision, "decision")
        or offer.client_id != request.client_id or request.envelope.get("scope") != "cart"
        or offer.envelope.get("request_id") != str(request.pk) or offer.envelope.get("request_digest") != request.envelope_hmac
        or offer.envelope.get("decision_id") != str(decision.pk) or offer.envelope.get("decision_digest") != decision.envelope_hmac
        or offer.envelope.get("request") != request.envelope or offer.envelope.get("decision") != decision.envelope):
        return None
    return request, decision


def current_offer_reason(offer, settings_obj=None, now=None, current_source_cart=None):
    now = now or timezone.now()
    original = _original(offer) if offer else None
    if not original:
        return "offer_unverified"
    request, decision = original
    settings_obj = reviews._settings(settings_obj)
    if not settings_obj:
        return "settings_changed"
    if offer.created_at > now or offer.expires_at <= now or offer.expires_at > decision.created_at + reviews.FRESHNESS:
        return "offer_expired"
    if not _actor_allowed(offer.actor) or not _actor_allowed(decision.actor):
        return "actor_forbidden"
    latest = reviews.Request.objects.filter(client_id=offer.client_id).exclude(envelope__scope="restock").order_by("-created_at", "-pk").first()
    newest = Offer.objects.filter(client_id=offer.client_id).order_by("-created_at", "-pk").first()
    if not latest or latest.pk != request.pk or not newest or newest.pk != offer.pk:
        return "offer_superseded"
    latest_decision = reviews.Decision.objects.filter(request=request).order_by("-version").first()
    if not latest_decision or latest_decision.pk != decision.pk:
        return "decision_changed"
    reason = reviews._current_reason(request, settings_obj, now=now, current_source_cart=current_source_cart)
    if reason:
        return reason
    reason = reviews._decision_reason(request, decision, now=now, current_source_cart=current_source_cart)
    if reason:
        return reason
    # WAIT configurations are terms too: disabling a future SKU invalidates
    # the complete basket even when the ordinary AVAILABLE guard skips it.
    from management.services.ig_restock_availability import observe_configurations
    by_id = {str(line["line_id"]): line for line in request.envelope["lines"]}
    targets = [by_id[str(row["line_id"])]["target"] for row in decision.envelope["lines"]]
    configs = observe_configurations(targets, now=now)
    if len(configs) != len(targets) or any(config.get("status") != "valid" or config.get("target") != target
        or config.get("configuration_digest") != row.get("configuration_digest")
        for config, target, row in zip(configs, targets, decision.envelope["lines"])):
        return "configuration_changed"
    return ""


def issue_offer(client_id, actor, expected_decision_digest, operation_id, options, now=None):
    from management.models import IgClient, IgFollowUpTask
    live_clock = now is None
    now = now or timezone.now()
    try:
        operation_id = uuid.UUID(str(operation_id))
    except (ValueError, TypeError, AttributeError):
        raise SplitShippingError("operation_invalid")
    if not _actor_allowed(actor):
        raise SplitShippingError("actor_forbidden")
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=client_id).first()
        if not client or client.privacy_erasure_started_at:
            raise SplitShippingError("client_erased")
        previous = Offer.objects.select_related("decision__request", "decision__actor", "actor").filter(operation_id=operation_id).first()
        if previous:
            normalized = _options(options, previous.decision.request, previous.decision)
            if not verified_offer(previous) or previous.client_id != client.pk or previous.actor_id != actor.pk or previous.envelope.get("decision_digest") != expected_decision_digest or previous.envelope.get("options") != normalized:
                raise SplitShippingError("operation_conflict")
            return previous
        request = reviews.Request.objects.select_for_update().filter(client=client).exclude(envelope__scope="restock").order_by("-created_at", "-pk").first()
        decision = reviews.Decision.objects.select_for_update().select_related("actor").filter(request=request).order_by("-version").first() if request else None
        if not decision or not reviews._verified(decision, "decision") or not hmac.compare_digest(str(expected_decision_digest), decision.envelope_hmac):
            raise SplitShippingError("decision_digest_changed")
        settings_obj = reviews._settings()
        reason = reviews._current_reason(request, settings_obj, now=now) if settings_obj else "settings_changed"
        reason = reason or reviews._decision_reason(request, decision, now=now)
        if reason or not _actor_allowed(decision.actor):
            raise SplitShippingError(reason or "actor_forbidden")
        normalized = _options(options, request, decision)
        latest = Offer.objects.select_for_update().filter(decision=decision).order_by("-version").first()
        identity = uuid.uuid4()
        version = latest.version + 1 if latest else 1
        task = IgFollowUpTask.objects.create(client=client, kind="manager_task", reason=ORIGIN,
            due_at=now, status="pending", trigger="event", event_key=f"split-shipping:{identity}",
            event_occurred_at=now, policy_started_at=now, event_payload={"offer_id": str(identity)})
        offer = _seal(Offer(id=identity, decision=decision, client=client, actor=actor,
            operation_id=operation_id, version=version, task=task, expires_at=min(now + reviews.FRESHNESS, decision.created_at + reviews.FRESHNESS)), "offer",
            {"request_id": str(request.pk), "request_digest": request.envelope_hmac,
             "decision_id": str(decision.pk), "decision_digest": decision.envelope_hmac,
             "request": request.envelope, "decision": decision.envelope, "options": normalized,
             "issued_at": now.isoformat()})
        text, _buttons = render_offer(offer)
        if not text:
            raise SplitShippingError("offer_body_too_large")
        # auto_now_add records the real insert after admission began. Validate
        # a live operation at its completed read, while an explicit as-of clock
        # keeps its original boundary and still rejects any future evidence.
        final_clock = timezone.now() if live_clock else now
        reason = current_offer_reason(offer, settings_obj, final_clock)
        if not _actor_allowed(actor) or reason:
            raise SplitShippingError(reason or "actor_forbidden")
        return offer


def encode_choice(offer, option):
    if not verified_offer(offer) or option not in {"together", "separate"}:
        raise SplitShippingError("offer_unverified")
    body = f"{offer.pk.hex}:{'t' if option == 'together' else 's'}"
    return PREFIX + body + ":" + _sign("button", {"body": body, "offer_digest": offer.envelope_hmac}, offer.key_id)[:16]


def _parse(payload):
    match = re.fullmatch(r"SS1:([a-f0-9]{32}):(t|s):([a-f0-9]{16})", str(payload or ""))
    if not match:
        return None
    offer = Offer.objects.select_related("decision__request", "decision__actor", "actor", "client", "task").filter(pk=uuid.UUID(hex=match[1])).first()
    option = "together" if match[2] == "t" else "separate"
    if not offer or not verified_offer(offer) or not hmac.compare_digest(payload, encode_choice(offer, option)):
        return None
    return offer, option


COPY = {
    "uk": ("Як відправляємо весь кошик?", "Разом", "Окремо", "позиції", "ви: до", "платить TwoComms; вам", "Строки підготовки/відправки не гарантують прибуття.", "Це максимальна погоджена плата за доставку, не оцінка перевізника.", "Обрано {option}. Умови доставки погоджено; оформлення ще окремо."),
    "ru": ("Как отправляем всю корзину?", "Вместе", "Отдельно", "позиции", "вы: до", "платит TwoComms; вам", "Сроки подготовки/отправки не гарантируют прибытие.", "Это максимальная согласованная плата за доставку, не оценка перевозчика.", "Выбрано {option}. Условия доставки согласованы; оформление ещё отдельно."),
    "en": ("How should we ship the whole basket?", "Together", "Separate", "items", "you: up to", "TwoComms pays; you pay", "Preparation/dispatch timings do not guarantee arrival.", "This is the agreed maximum delivery charge, not a carrier estimate.", "Selected {option}. Delivery terms are agreed; ordering is a separate step."),
}


def render_offer(offer):
    from management.services.ig_message_templates import QuickReply
    from management.services.ig_fulfillment_delivery import _timing, COPY as TIMING_COPY
    if not verified_offer(offer):
        return "", ()
    data, request = offer.envelope, offer.envelope["request"]
    locale = request.get("locale") or "uk"
    copy, timing_copy = COPY.get(locale, COPY["uk"]), TIMING_COPY.get(locale, TIMING_COPY["uk"])
    numbered = {str(line["line_id"]): index for index, line in enumerate(request["lines"], 1)}
    parts = [copy[0]]
    for index, line in enumerate(request["lines"], 1):
        label = re.sub(r"\s+", " ", str(line.get("label") or "")).strip()
        if not label or re.search(r"https?://|[<>\x00-\x1f]", label, re.I):
            return "", ()
        parts.append(f"{index}. {label}")
    for option, title in (("together", copy[1]), ("separate", copy[2])):
        parts.append(title + ":")
        for index, parcel in enumerate(data["options"][option]["parcels"], 1):
            payer = copy[4] if parcel["payer"] == "customer" else copy[5]
            parcel_label = {"uk": "Посилка", "ru": "Посылка", "en": "Parcel"}.get(locale, "Посилка")
            parts.append(f"{parcel_label} {index}: {copy[3]} {','.join(str(numbered[line]) for line in parcel['line_ids'])}; {payer} {parcel['customer_delivery_cap']} UAH.")
        total = sum(Decimal(parcel["customer_delivery_cap"]) for parcel in data["options"][option]["parcels"])
        total_label = {"uk": "Разом для вас: до", "ru": "Итого для вас: до", "en": "Your total: up to"}.get(locale, "Разом для вас: до")
        parts.append(f"{total_label} {total:.2f} UAH.")
    try:
        for row in data["decision"]["lines"]:
            facts = [_timing(row.get(phase), phase, timing_copy) for phase in ("preparation", "dispatch")]
            facts = [fact for fact in facts if fact]
            if facts:
                parts.append(f"{numbered[str(row['line_id'])]}. " + "; ".join(facts) + ".")
    except (KeyError, ValueError, TypeError):
        return "", ()
    parts.extend((copy[6], copy[7]))
    text = "\n".join(parts)
    if len(text.encode()) > MAX_BYTES:
        return "", ()
    return text, (QuickReply(copy[1], encode_choice(offer, "together")), QuickReply(copy[2], encode_choice(offer, "separate")))


def _body_digest(offer):
    text, buttons = render_offer(offer)
    return reviews._digest({"text": text, "quick_replies": [{"title": b.title, "payload": b.payload} for b in buttons]}) if text else ""


def _native_receipt(offer, receipt):
    task = receipt.task if receipt else None
    return bool(receipt and _verified(receipt, "receipt") and receipt.offer_id == offer.pk
        and offer.task_id == receipt.task_id and task.client_id == offer.client_id
        and task.reason == ORIGIN and task.trigger == "event" and task.event_key == f"split-shipping:{offer.pk}"
        and task.status == "sent" and task.attempt_count == 1 and task.sent_at
        and not task.claim_token and not task.claim_until
        and task.provider_message_id == receipt.native_mid and bool(receipt.native_mid)
        and task.message_text == render_offer(offer)[0] and receipt.envelope.get("body_digest") == _body_digest(offer)
        and receipt.envelope.get("offer_digest") == offer.envelope_hmac
        and receipt.envelope.get("delivered_at") == task.sent_at.isoformat()
        and task.event_payload == {"offer_id": str(offer.pk)})


def record_delivery(task, native_mid, now=None):
    """Only an already committed real FollowUpTask native checkpoint is evidence."""
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    native_mid = normalize_provider_message_id(native_mid)
    with transaction.atomic():
        offer = Offer.objects.select_for_update().select_related("task", "decision__request").filter(task_id=task.pk).first()
        existing = Receipt.objects.filter(task_id=task.pk).select_related("task").first()
        if existing:
            return existing if offer and _native_receipt(offer, existing) and existing.native_mid == native_mid else None
        actual = offer.task if offer else None
        if not offer or not verified_offer(offer) or not native_mid or actual.status != "sent" or actual.provider_message_id != native_mid or not actual.sent_at or actual.attempt_count != 1 or actual.message_text != render_offer(offer)[0]:
            return None
        return _seal(Receipt(offer=offer, task=actual, native_mid=native_mid), "receipt",
            {"offer_digest": offer.envelope_hmac, "body_digest": _body_digest(offer),
             "recipient": offer.decision.request.envelope.get("recipient") or offer.client.igsid,
             "source_namespace": offer.decision.request.envelope["source_namespace"], "delivered_at": actual.sent_at.isoformat()})


def _source_binding(offer, receipt, source, settings_obj, *, now, classification_only=False):
    from management.services.instagram_bot import ingress_provider_namespace
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    row = source.message if hasattr(source, "message") else source
    match = re.fullmatch(r"SS1:([a-f0-9]{32}):(t|s):([a-f0-9]{16})", str(row.quick_reply_payload or ""))
    if not match or not verified_offer(offer):
        return "choice_receipt_unverified"
    option = "together" if match[2] == "t" else "separate"
    request = offer.envelope["request"]
    # The caller already loaded this exact immutable offer. Recheck the actual
    # payload's HMAC against it without reloading the identical evidence row.
    if not hmac.compare_digest(row.quick_reply_payload, encode_choice(offer, option)) or not _native_receipt(offer, receipt):
        return "choice_receipt_unverified"
    _text, buttons = render_offer(offer)
    title = buttons[0 if option == "together" else 1].title
    if (row.client_id != offer.client_id or row.sender_id != offer.client.igsid or row.role != "user" or row.source != "webhook"
        or row.status == "failed" or not row.mid or not row.provider_created_at or timezone.is_naive(row.provider_created_at)
        or row.provider_created_at > now or row.provider_created_at < receipt.task.sent_at
        or (not classification_only and row.provider_created_at >= offer.expires_at)
        or row.pk < conversation_route_reset_floor(offer.client_id) or row.provider_namespace != ingress_provider_namespace(settings_obj)
        or row.provider_namespace != request["source_namespace"] or str(row.text or "").strip() not in {"", title}
        or str(row.attachments or "").strip() not in {"", "[]", "null"} or bool(row.attachment_media)
        or (row.reply_to_provider_message_id and row.reply_to_provider_message_id != receipt.native_mid)):
        return "choice_source_changed"
    return ""


def is_authenticated_choice_source(request, row, settings_obj=None, now=None, *, _permission_checked=False):
    """Narrow non-recursive requirement exemption; no choice is created here."""
    now = now or timezone.now()
    parsed = _parse(getattr(row, "quick_reply_payload", ""))
    if not parsed or not settings_obj:
        return False
    offer, _option = parsed
    receipt = Receipt.objects.filter(offer=offer).select_related("task").first()
    data = request.envelope
    from management.services.ig_reply_boundary import capture_reply_permission, _client_allowed
    if _permission_checked:
        # Only _current_reason's already completed fresh permission/transition/
        # echo gate supplies this flag. Recheck the newly joined owner epochs
        # and pause/privacy fields; standalone classifiers retain the full gate.
        permission_ok = bool(settings_obj.is_enabled and settings_obj.pk == data["settings_id"]
            and settings_obj.reply_permission_epoch == data["settings_epoch"]
            and offer.client.reply_permission_epoch == data["client_epoch"]
            and _client_allowed(offer.client)[0])
    else:
        permission = capture_reply_permission(settings_obj.pk, offer.client_id)
        permission_ok = bool(permission and permission.settings_epoch == data["settings_epoch"]
            and permission.client_epoch == data["client_epoch"])
    return bool(_original(offer) and offer.decision.request_id == request.pk
        and offer.envelope["request_digest"] == request.envelope_hmac
        and not offer.client.privacy_erasure_started_at and permission_ok
        and not _source_binding(offer, receipt, row, settings_obj, now=now, classification_only=True))


def _pure_bundle(revision, source, *, offer=None, option=None, actual_sources=None):
    from management.services.bot_sales_classifier import is_reaction_only
    selected = next((row for row in revision.bundle_snapshot.get("sources", ()) if row.get("message_id") == source.pk), None)
    parsed = (offer, option) if offer is not None and option in {"together", "separate"} else _parse(getattr(source, "quick_reply_payload", ""))
    if not selected or not parsed:
        return False
    offer, option = parsed
    title = render_offer(offer)[1][0 if option == "together" else 1].title
    actual = actual_sources if actual_sources is not None else {item.message_id: item.message for item in revision.sources.select_related("message")}
    for row in revision.bundle_snapshot.get("sources", ()):
        message = actual.get(row.get("message_id"))
        if (row.get("role") != "user" or row.get("media_parts") or not message
            or str(message.attachments or "").strip() not in {"", "[]", "null"} or bool(message.attachment_media)):
            return False
        payload, text = str(row.get("quick_reply_payload") or ""), str(row.get("text") or "").strip()
        if payload:
            if payload != source.quick_reply_payload or text not in {"", title}:
                return False
        elif text and not is_reaction_only(text):
            return False
    return True


def apply_choice(source, revision, settings_obj, now=None):
    now = now or timezone.now()
    row = source.message if hasattr(source, "message") else source
    parsed = _parse(getattr(row, "quick_reply_payload", ""))
    if not parsed:
        return SplitChoiceResult()
    offer, option = parsed
    if not revision or not _pure_bundle(revision, row):
        return SplitChoiceResult(True, False, "choice_mixed_source", offer_id=str(offer.pk))
    from management.models import IgClient
    from management.services.ig_purpose_sources import owned_source
    with transaction.atomic():
        IgClient.objects.select_for_update().get(pk=offer.client_id)
        offer = Offer.objects.select_for_update().select_related("decision__request", "decision__actor", "actor", "client", "task").get(pk=offer.pk)
        receipt = Receipt.objects.select_for_update().select_related("task").filter(offer=offer).first()
        owned, reason = owned_source(revision, row, settings_obj)
        reason = reason or _source_binding(offer, receipt, row, settings_obj, now=now)
        reason = reason or current_offer_reason(offer, settings_obj, now)
        if reason:
            return SplitChoiceResult(True, False, reason, offer_id=str(offer.pk))
        revision, row, proof = owned
        existing = Choice.objects.filter(offer=offer).first()
        if existing:
            if not verified_choice(existing) or existing.option != option or existing.source_id != row.pk or existing.envelope.get("source") != proof:
                return SplitChoiceResult(True, False, "choice_already_recorded", offer_id=str(offer.pk))
            choice = existing
        else:
            # Recheck after all locks/ownership work. The source hook above is
            # solely for this authenticated native button, not arbitrary text.
            reason = current_offer_reason(offer, settings_obj, now) or _source_binding(offer, receipt, row, settings_obj, now=now)
            if reason:
                return SplitChoiceResult(True, False, reason, offer_id=str(offer.pk))
            choice = _seal(Choice(offer=offer, receipt=receipt, client_id=offer.client_id, source=row, revision=revision, option=option), "choice",
                {"offer_id": str(offer.pk), "offer_digest": offer.envelope_hmac, "receipt_id": str(receipt.pk),
                 "receipt_digest": receipt.envelope_hmac, "option": option, "source": proof, "confirmed_at": now.isoformat()})
        locale = offer.envelope["request"].get("locale") or "uk"
        copy = COPY.get(locale, COPY["uk"])
        return SplitChoiceResult(True, True, "choice_recorded", copy[8].format(option=copy[1 if option == "together" else 2]), offer_id=str(offer.pk), choice_id=str(choice.pk))


def validated_choice_history(choice):
    if not verified_choice(choice):
        return {"ready": False, "reason": "choice_unverified"}
    offer, receipt = choice.offer, choice.receipt
    original = _original(offer)
    if (not original or choice.client_id != offer.client_id or choice.client.privacy_erasure_started_at
        or choice.envelope.get("offer_digest") != offer.envelope_hmac or choice.envelope.get("receipt_digest") != receipt.envelope_hmac
        or choice.option != choice.envelope.get("option") or not _native_receipt(offer, receipt)):
        return {"ready": False, "reason": "choice_binding_changed"}
    # History describes an actual old agreement after a later purchase/reset.
    # Validate its original immutable source, never current conversational TTL.
    from management.services.ig_turn_revisions import _source_payload
    revision, row, proof = choice.revision, choice.source, choice.envelope.get("source") or {}
    frozen_sources = list(revision.sources.select_related("message")) if revision else []
    frozen = next((item for item in frozen_sources if item.message_id == row.pk), None)
    snapshot = next((item for item in revision.bundle_snapshot.get("sources", ()) if item.get("message_id") == row.pk), None) if revision else None
    if (not revision or not frozen or not snapshot or revision.client_id != choice.client_id
        or not revision.snapshot_digest or reviews._digest(revision.bundle_snapshot) != revision.snapshot_digest
        or proof.get("revision_id") != revision.pk or proof.get("revision_digest") != revision.snapshot_digest
        or proof.get("message_id") != row.pk or proof.get("digest") != frozen.source_digest
        or proof.get("mid") != row.mid or proof.get("namespace") != row.provider_namespace
        or not row.provider_created_at or proof.get("provider_created_at") != row.provider_created_at.isoformat()
        or row.pk < proof.get("reset_floor", row.pk + 1) or row.client_id != choice.client_id
        or row.role != "user" or row.source != "webhook" or row.sender_id != choice.client.igsid
        or snapshot.get("source_digest") != frozen.source_digest
        or _source_payload(row, previous=frozen, ordinal=frozen.ordinal)["source_digest"] != frozen.source_digest
        or row.quick_reply_payload != encode_choice(offer, choice.option)
        or not _pure_bundle(revision, row, offer=offer, option=choice.option,
            actual_sources={item.message_id: item.message for item in frozen_sources})):
        return {"ready": False, "reason": "choice_source_changed"}
    return {"ready": True, "offer": deepcopy(offer.envelope), "choice": deepcopy(choice.envelope),
        "request": deepcopy(original[0].envelope), "decision": deepcopy(original[1].envelope)}


def validated_choice(choice, settings_obj=None, now=None, current_source_cart=None):
    result = validated_choice_history(choice)
    if not result.get("ready"):
        return result
    reason = current_offer_reason(choice.offer, settings_obj, now, current_source_cart)
    return {"ready": False, "reason": reason} if reason else result


def _captured_reason(offer, settings_obj, now, cart, review, catalog_readback=None):
    """Reuse the verified source/cart capture; never repeat its source walk."""
    original = _original(offer)
    request = review.get("current_request") or {}
    decision = review.get("decision") or {}
    if (not original or review.get("status") != "reviewed" or request.get("id") != str(original[0].pk)
        or review.get("request_digest") != offer.envelope["request_digest"]
        or decision.get("id") != str(original[1].pk) or _cart_identity(cart) != original[0].envelope.get("cart_identity")):
        return "review_scope_changed"
    # The caller just verified this exact review's current source, owner,
    # permissions and latest request. Its frozen settings/client epochs are
    # sufficient here; physical delivery/choice use current_offer_reason.
    if not reviews._enabled():
        return "settings_changed"
    data = original[0].envelope
    if (offer.client.reply_permission_epoch != data["client_epoch"] or offer.client.privacy_erasure_started_at
        or (settings_obj is not None and (settings_obj.pk != data["settings_id"]
            or settings_obj.reply_permission_epoch != data["settings_epoch"]))):
        return "permission_changed"
    if (offer.created_at > now or offer.expires_at <= now or original[1].created_at > now
        or now - original[1].created_at > reviews.FRESHNESS or offer.expires_at > original[1].created_at + reviews.FRESHNESS):
        return "offer_expired"
    actors = {actor.pk: actor for actor in (offer.actor, original[1].actor)}
    if any(not _actor_allowed(actor, _fresh_row=True) for actor in actors.values()):
        return "actor_forbidden"
    from management.services.ig_restock_availability import observe_configurations
    by_id = {str(line["line_id"]): line for line in data["lines"]}
    # AVAILABLE configurations were already checked in the verified review.
    # WAIT remains an exact future SKU and needs a shared-readback validation.
    rows = [row for row in original[1].envelope["lines"] if row.get("availability") != "available"]
    targets = [by_id[str(row["line_id"])]["target"] for row in rows]
    reuse = {"_catalog_readback": catalog_readback, "source_cart": cart} if catalog_readback is not None else {}
    configs = observe_configurations(targets, now=now, **reuse) if targets else []
    if len(configs) != len(targets) or any(config.get("status") != "valid" or config.get("target") != target
        or config.get("configuration_digest") != row.get("configuration_digest") for config, target, row in zip(configs, targets, rows)):
        return "configuration_changed"
    newest = Offer.objects.filter(client_id=offer.client_id).order_by("-created_at", "-pk").values_list("pk", flat=True).first()
    return "" if newest == offer.pk else "offer_superseded"


def _cart_identity(cart):
    return reviews._cart_identity(cart)


def shipping_projection(client, settings_obj=None, now=None, current_source_cart=None, current_review=None, _catalog_readback=None):
    now = now or timezone.now()
    client_id = getattr(client, "pk", client)
    offer = Offer.objects.select_related("decision__request", "decision__actor", "actor", "client", "task",
        "choice__receipt__task", "choice__client", "choice__source", "choice__revision"
    ).filter(client_id=client_id).order_by("-created_at", "-pk").first()
    review = current_review if current_review is not None else reviews.fulfillment_review_projection(client_id, now=now, current_source_cart=current_source_cart)
    reviewed = isinstance(review, dict) and review.get("status") == "reviewed"
    request, decision = (review.get("current_request") or {}, review.get("decision") or {}) if reviewed else ({}, {})
    # Passed review is a read-only frozen projection. The mutation endpoint
    # independently repeats all signed/live checks regardless of this UI hint.
    can_issue = bool(reviewed and request.get("scope") == "cart" and 2 <= len(request.get("lines", [])) <= 20
        and len(decision.get("lines", [])) == len(request["lines"])
        and all(row.get("availability") in {"available", "wait"} for row in decision.get("lines", [])))
    decisions = {str(row.get("line_id")): row for row in decision.get("lines", [])}
    lines = [{"line_id": str(line["line_id"]), "label": line.get("label", ""),
        "quantity": (line.get("target") or {}).get("quantity"),
        "availability": decisions.get(str(line["line_id"]), {}).get("availability"),
        "preparation": decisions.get(str(line["line_id"]), {}).get("preparation"),
        "dispatch": decisions.get(str(line["line_id"]), {}).get("dispatch")} for line in request.get("lines", [])]
    base = {"version": 1, "captured_at": now.isoformat(), "status": "none", "offer": None, "receipt": None, "choice": None,
        "ready": can_issue, "current": False, "request": deepcopy(request) if reviewed else None,
        "decision": deepcopy(decision) if reviewed else None, "request_lines": deepcopy(request.get("lines", [])),
        "lines": lines, "can_issue_offer": can_issue, "delivery": {"status": "absent", "mid": ""},
        "decision_digest": decision.get("digest") or decision.get("envelope_hmac") or ""}
    if reviewed and decision.get("id") and not base["decision_digest"]:
        signed = (offer.decision if offer and str(offer.decision_id) == decision["id"]
            else reviews.Decision.objects.filter(pk=decision["id"]).first())
        base["decision_digest"] = signed.envelope_hmac if reviews._verified(signed, "decision") else ""
    if not offer:
        return base
    reason = (_captured_reason(offer, settings_obj, now, current_source_cart, current_review, _catalog_readback)
        if current_review is not None and current_source_cart is not None else current_offer_reason(offer, settings_obj, now, current_source_cart))
    choice = getattr(offer, "choice", None)
    # The chosen row already joins its exact native receipt and task. Reuse
    # that evidence instead of issuing the same receipt SELECT again.
    receipt = choice.receipt if choice else Receipt.objects.filter(offer=offer).select_related("task").first()
    selected = choice if choice and validated_choice_history(choice).get("ready") else None
    if reason and not selected:
        return {**base, "status": "stale", "reason": reason}
    delivered = receipt if _native_receipt(offer, receipt) else None
    status = "chosen" if selected else "delivered" if delivered else "unknown" if offer.task and offer.task.status == "ambiguous" else "pending"
    text, buttons = render_offer(offer)
    return {**base, "status": status, "current": not bool(reason), "reason": reason,
        "delivery": {"status": "sent" if delivered else "unknown" if offer.task and offer.task.status == "ambiguous" else
            "cancelled" if offer.task and offer.task.status == "cancelled" else "pending", "mid": delivered.native_mid if delivered else ""},
        "offer": {"id": str(offer.pk), "digest": offer.envelope_hmac, "version": offer.version,
            "locale": offer.envelope["request"].get("locale") or "uk",
            "lines": [{"line_id": str(line["line_id"]), "label": line.get("label", ""),
                "quantity": (line.get("target") or {}).get("quantity")}
                for line in offer.envelope["request"]["lines"]],
            "expires_at": offer.expires_at.isoformat(), "options": deepcopy(offer.envelope["options"]),
            "issued_at": offer.envelope["issued_at"], "request_digest": offer.envelope["request_digest"],
            "decision_digest": offer.envelope["decision_digest"],
            "request_id": offer.envelope["request_id"], "decision_id": str(offer.decision_id), "body": text,
            "quick_replies": [{"title": b.title, "payload": b.payload} for b in buttons]},
        "receipt": {"id": str(delivered.pk), "digest": delivered.envelope_hmac, "native_mid": delivered.native_mid,
            "delivered_at": delivered.envelope["delivered_at"]} if delivered else None,
        "choice": {"id": str(selected.pk), "digest": selected.envelope_hmac, "option": selected.option,
            "confirmed_at": selected.envelope["confirmed_at"], "source": deepcopy(selected.envelope["source"])} if selected else None}


def context_snapshot(client, settings_obj=None, now=None, current_source_cart=None, current_review=None, _catalog_readback=None):
    """Finite read-only current offer/choice proof for one frozen truth frame."""
    if current_source_cart is None or current_review is None:
        return {"version": 1, "status": "none", "offer": None, "receipt": None, "choice": None}
    result = shipping_projection(client, settings_obj, now, current_source_cart, current_review, _catalog_readback)
    if not result.get("current") or not result.get("offer"):
        return {"version": 1, "status": "none", "offer": None, "receipt": None, "choice": None}
    result["proof"] = {"request_id": result["offer"]["request_id"], "request_digest": current_review["request_digest"],
        "decision_id": result["offer"]["decision_id"], "decision_digest": result["decision_digest"],
        "offer_id": result["offer"]["id"], "offer_digest": result["offer"]["digest"],
        "receipt": deepcopy(result["receipt"]), "choice": deepcopy(result["choice"])}
    result["fingerprint"] = reviews._digest(result["proof"])
    result["digest"] = result["fingerprint"]
    return result
