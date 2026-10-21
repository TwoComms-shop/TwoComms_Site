"""One source-proved restock interest, with native delivery and answer evidence.

Stock observation is not a fulfilment decision or a future messaging grant.
The manager admission seam deliberately fails closed until its owner is wired.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import hmac
import json
import re
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from management.ig_restock_subscription_models import (
    IgRestockInvitation as Invitation, IgRestockReceipt as Receipt,
    IgRestockAnswer as Answer, IgRestockSubscription as Subscription,
    IgRestockAvailabilityEvent as AvailabilityEvent,
)
from management.services.ig_purpose_sources import owned_source, latest_customer_source
from management.services.ig_restock_ack_copy import VERSION as ACK_COPY_VERSION, attributed_label
from management.services.ig_turn_revisions import _digest

PREFIX = RESTOCK_SUBSCRIPTION_PREFIX = "RS1:"
ORIGIN = "subscribed_restock"
PURPOSE = "restock_notification"
VALIDITY = timedelta(days=90)
WINDOW = timedelta(hours=23)
CHECK_INTERVAL = timedelta(minutes=15)


@dataclass(frozen=True)
class RestockRequest:
    handled: bool
    reply_text: str = ""
    quick_replies: tuple = ()
    invitation_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class RestockBoundary:
    ready: bool
    reason: str = ""

    def __bool__(self):
        return self.ready


def _enabled():
    return bool(getattr(settings, "IG_RESTOCK_SUBSCRIPTIONS_ENABLED", False))


def _keys():
    secrets = [str(getattr(settings, "IG_RESTOCK_HMAC_KEY", "") or settings.SECRET_KEY),
        *map(str, getattr(settings, "SECRET_KEY_FALLBACKS", ()))]
    return {hashlib.sha256(key.encode()).hexdigest()[:24]: key for key in secrets if key}


def _sign(kind, envelope, key_id):
    key = _keys().get(key_id)
    if not key:
        return ""
    body = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(key.encode(), b"ig-restock-v1:" + kind.encode() + b":" + body, hashlib.sha256).hexdigest()


def _columns(row):
    result = {}
    for field in row._meta.concrete_fields:
        if field.name in {"envelope", "envelope_hmac", "created_at"} or (field.name == "id" and not isinstance(row.pk, uuid.UUID)):
            continue
        value = getattr(row, field.attname)
        # The added alternative provenance must not change historical HMACs.
        if field.name == "followup_task" and value is None:
            continue
        result[field.attname] = value.isoformat() if hasattr(value, "isoformat") else str(value) if isinstance(value, uuid.UUID) else value
    return result


def _seal(row, kind, **proof):
    row.key_id = next(iter(_keys()))
    row.envelope = {"version": 1, "purpose": PURPOSE, "columns": _columns(row), **proof}
    row.envelope_hmac = _sign(kind, row.envelope, row.key_id)
    row.save(force_insert=True)
    return row


def _verified(row, kind):
    expected = _sign(kind, row.envelope, row.key_id) if row and isinstance(row.envelope, dict) else ""
    return bool(expected and row.envelope_hmac and hmac.compare_digest(expected, row.envelope_hmac)
        and row.envelope.get("version") == 1 and row.envelope.get("purpose") == PURPOSE
        and row.envelope.get("columns") == _columns(row))


def _payload(identity, action, key_id):
    value = f"{PREFIX}{identity}:{action}:{key_id}"
    return value + ":" + _sign("payload", {"identity": value}, key_id)


def _locale(text):
    if re.search(r"[іїєґ]", text.lower()):
        return "uk"
    if re.search(r"[а-яё]", text.lower()):
        return "ru"
    return "en"


def is_restock_interest(text):
    """Positive customer notification intent shared with manager-review routing.

    This detects intent, not a pure request: callers preserve other questions
    and independently prove the selected target and current source.
    """
    text = str(text or "").strip()
    if not text or re.search(r"[«»\"“”]|\b(?:не|нет|no|not)\b|якби|если|would have|what if", text, re.I):
        return False
    return bool(re.search(r"(?:повідом|сообщ|уведом|сповіст|notify|let me know).{0,60}(?:наяв|налич|попов|поступ|з.?яв|появ|stock|available|back)|(?:коли|когда|when).{0,40}(?:з.?яв|появ|stock).{0,40}(?:повідом|сообщ|notify)", text, re.I))


def _copy(locale, kind, label=""):
    copies = {
        "uk": {
            "invite": "💛 Уточню наявність і строк підготовки цієї позиції у менеджера:\n{label}\n\nЯкщо потрібно буде чекати, хочете одне повідомлення після підтвердження наявності? Натисніть кнопку нижче — без зайвих розсилок.\n\nЯкщо після тривалої паузи сповіщення не прийде, напишіть нам — перевіримо актуальну наявність.",
            "button": "Так, повідомте 💛", "accepted": "Домовились 💛 Збережено одне повідомлення про підтверджене поповнення саме цієї позиції:\n{label}\n\nЯкщо після довгої паузи повідомлення не прийде, напишіть нам — перевіримо наявність.",
            "cancel": "Скасувати", "revoked": "Повідомлення про поповнення скасовано 💛",
            "stale": "Це підтвердження вже не актуальне. Напишіть, для якої моделі, кольору, розміру, крою та кількості потрібне повідомлення.",
            "clarify": "Уточніть модель, колір, розмір, крій та кількість, для яких потрібне одне повідомлення про поповнення 💛",
            "review": "Потрібно уточнити наявність і можливість виконати саме цю позицію у менеджера. Передамо точні параметри на перевірку 💛",
            "notification": "💛 Ви просили повідомити про поповнення:\n{label}\n\nМенеджер перевірив і підтвердив наявність саме цієї позиції в потрібній кількості. Для оформлення й уточнення строку підготовки та відправлення напишіть нам 💛"},
        "ru": {
            "invite": "💛 Уточню наличие и срок подготовки этой позиции у менеджера:\n{label}\n\nЕсли нужно будет подождать, хотите одно сообщение после подтверждения наличия? Нажмите кнопку ниже — без лишних рассылок.\n\nЕсли после долгой паузы уведомление не придёт, напишите нам — проверим актуальное наличие.",
            "button": "Да, сообщите 💛", "accepted": "Договорились 💛 Сохранено одно сообщение о подтверждённом пополнении именно этой позиции:\n{label}\n\nЕсли после долгой паузы сообщение не придёт, напишите нам — проверим наличие.",
            "cancel": "Отменить", "revoked": "Сообщение о пополнении отменено 💛",
            "stale": "Это подтверждение уже не актуально. Напишите, для какой модели, цвета, размера, кроя и количества нужно сообщение.",
            "clarify": "Уточните модель, цвет, размер, крой и количество, для которых нужно одно сообщение о пополнении 💛",
            "review": "Нужно уточнить наличие и возможность выполнить именно эту позицию у менеджера. Передадим точные параметры на проверку 💛",
            "notification": "💛 Вы просили сообщить о пополнении:\n{label}\n\nМенеджер проверил и подтвердил наличие именно этой позиции в нужном количестве. Для оформления и уточнения срока подготовки и отправки напишите нам 💛"},
        "en": {
            "invite": "💛 I’ll check availability and preparation timing with our manager for:\n{label}\n\nIf you need to wait, would you like one message after availability is confirmed? Tap below — no extra mailing lists.\n\nIf you don’t hear from us after a long pause, message us and we’ll check the latest availability.",
            "button": "Yes, notify me 💛", "accepted": "Agreed 💛 One notification about confirmed replenishment is saved for this exact item:\n{label}\n\nIf you receive no message after a long pause, write to us and we’ll check availability.",
            "cancel": "Cancel notification", "revoked": "Your replenishment notification is cancelled 💛",
            "stale": "This confirmation is no longer current. Tell us the exact product, colour, size, fit and quantity you want an update for.",
            "clarify": "Please specify the product, colour, size, fit and quantity for your one replenishment notification 💛",
            "review": "A manager needs to verify availability and whether this exact item can be fulfilled. We’ll pass the precise requirements for review 💛",
            "notification": "💛 You asked us to tell you about replenishment:\n{label}\n\nA manager checked and confirmed this exact item is available in the requested quantity. Message us to place an order and confirm preparation and dispatch timing 💛"},
    }
    return copies[locale if locale in copies else "uk"][kind].format(label=label)


def _label(inv):
    return inv.envelope["label"]


def _copy_for(inv, kind, *, accepted=None):
    from management.services import ig_restock_waiting_list as waiting
    envelope = accepted.envelope if accepted is not None else inv.envelope
    if kind == "accepted" and "ack_copy_version" in envelope and envelope["ack_copy_version"] != ACK_COPY_VERSION:
        raise ValueError("restock_ack_copy_version_unknown")
    if kind == "accepted" and inv.envelope.get("producer") == "manager_followup.v1":
        from management.services.ig_restock_followup import accepted_copy
        return accepted_copy(inv.locale, inv.target)
    if kind == "accepted" and envelope.get("ack_copy_version") == ACK_COPY_VERSION:
        return _fresh_copy_for(inv, kind)
    return (waiting.copy_for(inv.locale, kind, _label(inv)) if waiting.is_list(inv.target)
        else _copy(inv.locale, kind, _label(inv)))


def _fresh_copy(locale, kind, label, *, all_items=False):
    from management.services import ig_restock_waiting_list as waiting
    chosen = attributed_label(locale, label) if kind in {"invite", "accepted"} else label
    return waiting.copy_for(locale, kind, chosen) if all_items else _copy(locale, kind, chosen)


def _fresh_copy_for(inv, kind):
    if kind == "accepted" and inv.envelope.get("producer") == "manager_followup.v1":
        from management.services.ig_restock_followup import accepted_copy
        return accepted_copy(inv.locale, inv.target)
    from management.services import ig_restock_waiting_list as waiting
    from management.services.ig_restock_ack_copy import list_label
    all_items = waiting.is_list(inv.target)
    label = (list_label(inv.target) if all_items else
        _safe_label(inv.envelope["initial_availability"], truncate=False))
    return _fresh_copy(inv.locale, kind, label, all_items=all_items)


def _accepted_reply_copy(inv, accepted):
    if "ack_copy_version" in accepted.envelope:
        return _copy_for(inv, "accepted", accepted=accepted)
    # A previously failed acknowledgement can resume without rewriting its
    # signed Answer. A completed historical ACK retains its exact native body.
    proof = _ack_card(inv, accepted)
    if proof:
        from management.models import IgRevisionDeliveryEffect
        return IgRevisionDeliveryEffect.objects.get(pk=proof["effect_id"]).payload["message"]["text"]
    return _fresh_copy_for(inv, "accepted")


def _observe_target(target, *, now):
    from management.services import ig_restock_waiting_list as waiting
    if waiting.is_list(target):
        return waiting.observe(target, now=now)
    from management.services.ig_restock_availability import observe_target
    return observe_target(target, now=now).as_dict()


def _safe_label(observation, *, truncate=True):
    labels, target = observation.get("labels", {}), observation.get("target", {})
    def clean(value, bound):
        text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()
        return text[:bound] if truncate else text
    parts = [clean(labels.get("product", ""), 70), clean(labels.get("color", ""), 25),
        clean(target.get("size", ""), 12), clean(labels.get("fit", ""), 25), f"× {target.get('quantity', '')}"]
    extra = [f"{clean(key, 16)}: {clean(value, 18)}" for key, value in sorted(target.get("option_values", {}).items()) if key != "fit"]
    return " · ".join(parts + extra)


def _button(inv, action="accept"):
    from management.services.ig_message_templates import QuickReply
    return QuickReply(title=inv.button_title if action == "accept" else _copy(inv.locale, "cancel"),
        payload=inv.accept_payload if action == "accept" else inv.revoke_payload)


def _native_button(inv, action="accept"):
    button = _button(inv, action)
    return {"content_type": "text", "title": button.title, "payload": button.payload}


def _selection_identity(preferences):
    return {key: deepcopy(preferences.get(key)) for key in
        ("session_id", "generation", "episode_id", "line_id", "recipient_id", "reset_floor", "values", "evidence", "cleared")}


def _quantity_basis(preferences, line):
    preferences = deepcopy(preferences)
    if "quantity" not in preferences.get("values", {}):
        basis = (line.get("defaults") or {}).get("quantity")
        if basis != {"value": 1, "authority": "existing_cart_default", "source_confirmed": False}:
            return preferences
        preferences["values"]["quantity"] = 1
        preferences["evidence"]["quantity"] = {**basis, "session_id": preferences["session_id"],
            "generation": preferences["generation"], "line_id": preferences["line_id"]}
    return preferences


def _selection_sources(preferences, client, namespace):
    """Freeze original source and immutable reducer records, independent of UI head."""
    from management.models import InstagramBotMessage, IgCommerceTurnDecision, IgCommerceSelectionTransition
    from management.services.ig_turn_revisions import _source_payload
    evidence = list(preferences.get("evidence", {}).values())
    source_ids = {item["source_message_id"] for item in evidence if isinstance(item, dict) and item.get("source_message_id")}
    decision_ids = {item["decision_id"] for item in evidence if isinstance(item, dict) and item.get("decision_id")}
    transition_ids = {item["transition_id"] for item in evidence if isinstance(item, dict) and item.get("transition_id")}
    if not source_ids or max(len(source_ids), len(decision_ids), len(transition_ids)) > 16:
        return None
    rows = list(InstagramBotMessage.objects.filter(pk__in=source_ids).order_by("pk"))
    if len(rows) != len(source_ids) or any(row.client_id != client.pk or row.sender_id != client.igsid
        or row.role != "user" or row.source != "webhook" or row.status in {"failed", "ignored"}
        or not row.mid or not row.provider_created_at or row.provider_namespace != namespace
        or row.pk < preferences["reset_floor"] for row in rows):
        return None
    decisions = list(IgCommerceTurnDecision.objects.filter(pk__in=decision_ids).order_by("pk").values(
        "id", "source_message_id", "session_id", "transition_id", "request_payload", "result_payload", "accepted", "is_stale"))
    transitions = list(IgCommerceSelectionTransition.objects.filter(pk__in=transition_ids).order_by("pk").values(
        "id", "session_id", "source_message_id", "action", "from_revision", "to_revision", "previous_snapshot", "next_snapshot"))
    if len(decisions) != len(decision_ids) or len(transitions) != len(transition_ids):
        return None
    return {"sources": [{"id": row.pk, "digest": _source_payload(row, ordinal=0)["source_digest"]} for row in rows],
        "decisions": [{"id": row["id"], "digest": _digest(row)} for row in decisions],
        "transitions": [{"id": row["id"], "digest": _digest(row)} for row in transitions]}


def _target_capture(client, row, *, explicit=False, now):
    from management.services.ig_commerce_projection import capture_current_selection_lines, source_preferences_for
    from management.services.ig_restock_availability import resolve_source_interest
    cart = capture_current_selection_lines(client.pk, now=now)
    if cart.get("status") != "captured" or not cart.get("coverage_complete"):
        return None, "source_cart_unavailable"
    candidates = []
    for line in cart.get("lines", ()):
        choice = line.get("source_selection") or {}
        evidence = choice.get("evidence", {})
        sourced = any(item.get("source_message_id") == row.pk for item in evidence.values() if isinstance(item, dict))
        if sourced or (explicit and len(cart.get("lines", ())) == 1 and line.get("line_id") == cart.get("active_line_id")):
            candidates.append(line)
    if len(candidates) != 1:
        return None, "target_ambiguous"
    preferences = source_preferences_for(client, line_id=candidates[0]["line_id"])
    if not preferences:
        return None, "target_source_unavailable"
    preferences = _quantity_basis(preferences, candidates[0])
    # Every required axis is a validated customer choice. Stock does not fill it.
    required = {"product_id", "color", "size", "fit_option_code", "quantity"}
    if not required.issubset(preferences.get("values", {})) or not required.issubset(preferences.get("evidence", {})):
        return None, "target_incomplete"
    sources = _selection_sources(preferences, client, row.provider_namespace)
    if not sources:
        return None, "target_source_unavailable"
    observation = resolve_source_interest(preferences, now=now).as_dict()
    return {"cart_digest": cart["capture_digest"], "selection": _selection_identity(preferences),
        "observation": observation, "selection_sources": sources}, ""


def _scope(client, settings_obj):
    from management.services.ig_reply_boundary import capture_reply_permission
    from management.services.bot_followups import _has_open_service_conversation
    from management.services.instagram_bot import provider_token_configured
    permission = capture_reply_permission(settings_obj.pk, client.pk)
    if not _enabled() or not permission:
        return permission.reason or "disabled"
    if client.stage in {"spam", "lead_manager"} or client.primary_objection == "no_buy" or client.lost_reason in {"no_buy", "stop"}:
        return "customer_stopped_or_manager_routed"
    if _has_open_service_conversation(client):
        return "service_case_open"
    if not provider_token_configured(settings_obj):
        return "provider_capability_missing"
    return ""


def _manager_case_binding(identity, *, client_id, source_id, revision_id, source_proof, target, line_id,
        settings_obj=None, now=None, require_current=False):
    """Read actual signed manager debt for this exact original source/target."""
    from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest
    from management.services import ig_fulfillment_reviews as reviews
    try:
        request = IgFulfillmentReviewRequest.objects.select_related("task", "client").filter(pk=identity).first()
    except (ValueError, TypeError):
        return None
    if not reviews._verified(request, "request"):
        return None
    data, task = request.envelope, request.task
    matching = [line for line in data.get("lines", ()) if isinstance(line, dict)
        and line.get("line_id") == line_id and line.get("target") == target]
    if (request.client_id != client_id or request.source_id != source_id or request.revision_id != revision_id
        or data.get("scope") != "cart" or data.get("client_id") != client_id or data.get("source_id") != source_id
        or source_proof not in data.get("sources", ()) or data.get("source_namespace") != source_proof["namespace"]
        or len(matching) != 1 or task.client_id != client_id or task.kind != "manager_task"
        or task.reason != "fulfillment_review" or task.trigger != "event" or task.policy_version != reviews.PURPOSE
        or task.event_payload != {"origin": reviews.PURPOSE}):
        return None
    payload = deepcopy(data)
    for key in ("version", "purpose", "columns"):
        payload.pop(key, None)
    for line in payload["lines"]:
        line["observation"].pop("captured_at", None)
        line["observation"].pop("digest", None)
    expected_event_key = "ig-fulfillment-review:" + reviews._digest({"client": client_id, "payload": payload})
    if task.event_key != expected_event_key:
        return None
    if require_current and (not settings_obj or reviews._current_reason(request, settings_obj, now=now or timezone.now())):
        return None
    return {"request_id": str(request.pk), "request_hmac": request.envelope_hmac,
        "task_id": task.pk, "client_id": client_id, "source_id": source_id, "revision_id": revision_id,
        "line_id": line_id, "target_digest": _digest(target)}


def _invitation_manager_case(inv, *, settings_obj=None, now=None, require_current=False):
    from management.services import ig_restock_waiting_list as waiting
    if waiting.is_list(inv.target):
        from management.services.ig_restock_waiting_list_review import invitation_review_valid
        return invitation_review_valid(inv)
    frozen = inv.envelope.get("manager_review")
    if not isinstance(frozen, dict) or not frozen.get("request_id"):
        return False
    actual = _manager_case_binding(frozen["request_id"], client_id=inv.client_id,
        source_id=inv.request_source_id, revision_id=inv.revision_id, source_proof=inv.envelope["source"],
        target=inv.target, line_id=inv.envelope["selection"]["line_id"], settings_obj=settings_obj,
        now=now, require_current=require_current)
    return bool(actual and actual == frozen)


def _prepare_manager_case(revision, row, proof, captured, settings_obj, *, now):
    from management.services.ig_fulfillment_reviews import prepare_fulfillment_review
    if not getattr(settings, "IG_FULFILLMENT_REVIEWS_ENABLED", False):
        return None
    outcome = prepare_fulfillment_review(revision, settings_obj=settings_obj, now=now)
    if not isinstance(outcome, dict) or not outcome.get("request_id"):
        return None
    return _manager_case_binding(outcome["request_id"], client_id=revision.client_id,
        source_id=row.pk, revision_id=revision.pk, source_proof=proof, target=captured["observation"]["target"],
        line_id=captured["selection"]["line_id"], settings_obj=settings_obj, now=now, require_current=True)


def _invitation_source(inv, settings_obj, *, require_head=False):
    if not _verified(inv, "invitation") or _digest(inv.target) != inv.target_digest:
        return "invitation_signature_invalid"
    from management.services import ig_restock_waiting_list as waiting
    if waiting.is_list(inv.target):
        if settings_obj is None:
            return "settings_missing"
        return waiting.invitation_reason(inv, settings_obj, require_head=require_head)
    owned, reason = owned_source(inv.revision, inv.request_source, settings_obj, require_head=require_head)
    if not owned or owned[2] != inv.envelope.get("source"):
        return reason or "request_source_changed"
    client = owned[0].client
    if (client.igsid != inv.envelope.get("recipient") or settings_obj.pk != inv.envelope.get("settings_id")
        or settings_obj.reply_permission_epoch != inv.envelope.get("settings_epoch")
        or client.reply_permission_epoch != inv.envelope.get("client_epoch")):
        return "permission_scope_changed"
    if _selection_sources(inv.envelope["selection"], client, inv.envelope["source"]["namespace"]) != inv.envelope.get("selection_sources"):
        return "original_selection_source_changed"
    if not _invitation_manager_case(inv, settings_obj=settings_obj, require_current=require_head):
        return "manager_review_binding_changed"
    return _scope(client, settings_obj)


def _current_target(inv, *, now):
    from management.services import ig_restock_waiting_list as waiting
    if waiting.is_list(inv.target):
        return waiting.current_target_reason(inv, now=now)
    from management.services.ig_commerce_projection import source_preferences_for, capture_current_selection_lines
    from management.services.ig_restock_availability import resolve_source_interest
    stored = inv.envelope["selection"]
    current = source_preferences_for(inv.client, line_id=stored["line_id"])
    if current and "quantity" not in current.get("values", {}):
        cart = capture_current_selection_lines(inv.client_id, now=now)
        lines = [line for line in cart.get("lines", ()) if line.get("line_id") == stored["line_id"]]
        if cart.get("status") != "captured" or not cart.get("coverage_complete") or len(lines) != 1:
            return "selection_changed"
        current = _quantity_basis(current, lines[0])
    if not current or _selection_identity(current) != stored:
        return "selection_changed"
    observation = resolve_source_interest(current, now=now).as_dict()
    if observation.get("target") != inv.target or observation.get("status") != "unavailable":
        return "target_no_longer_unavailable"
    return ""


def prepare_request(revision, source, settings_obj=None, now=None):
    from management.models import InstagramBotSettings
    if not _enabled():
        return RestockRequest(False, reason="disabled")
    now = now or timezone.now()
    settings_obj = settings_obj or InstagramBotSettings.load()
    settings_obj = InstagramBotSettings.objects.filter(pk=settings_obj.pk).first()
    if not settings_obj:
        return RestockRequest(False, reason="settings_missing")
    owned, reason = owned_source(revision, source, settings_obj)
    if not owned:
        return RestockRequest(False, reason=reason)
    revision, row, proof = owned
    text = str(row.text or "").strip()
    locale = _locale(text)
    if (row.quick_reply_payload or row.attachments or row.status == "ignored" or row.attachment_media
        or not text or re.search(r"[«»\"“”]|\b(?:не|нет|no|not)\b|якби|если|would have|what if", text, re.I)):
        return RestockRequest(False, reason="not_pure_interest")
    explicit = is_restock_interest(text)
    if re.search(r"(?:і|и|and)\s+(?:чи|есть|what|how|можно|можна)|[;\n]|"
            r"скільки|сколько|how much|what.{0,15}price|ц[іе]н[ауи]|варт[іо]ст|"
            r"успе[еє]|встиг|arriv|достав|shipping|delivery|подар|gift|birthday|"
            r"сроч|термінов|urgent|today|tomorrow|завтра|сьогодні|сегодня|"
            r"окремо|отдельно|split", text, re.I):
        return RestockRequest(False, reason="mixed_request")
    if _scope(revision.client, settings_obj):
        return RestockRequest(False, reason="permission_blocked")
    if not (row.provider_created_at <= now + timedelta(minutes=1) and now < row.provider_created_at + WINDOW):
        return RestockRequest(False, reason="request_window_expired")
    from management.services import ig_restock_waiting_list as waiting
    try:
        list_request = waiting.prepare_request(revision, row, proof, settings_obj, now=now)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        if isinstance(error, ValueError) and str(error) == "waiting_list_no_unavailable_items":
            list_request = None  # Every resolved catalog item is already available.
        elif isinstance(error, ValueError) and str(error) in {
            "waiting_list_manager_source_required", "waiting_list_source_cart_unavailable"}:
            # Unknown stock or an unavailable source frame remains with the
            # ordinary manager-review response plan. It is never a partial
            # one-item fallback or customer subscription.
            return RestockRequest(False, reason="manager_review_required")
        else:
            # An incomplete multi/custom capture cannot silently subscribe the
            # active single item instead of the customer's requested list.
            return RestockRequest(explicit, _copy(locale, "clarify") if explicit else "",
                reason="waiting_list_capture_unverified")
    if list_request is not None:
        return list_request
    captured, reason = _target_capture(revision.client, row, explicit=explicit, now=now)
    if not captured:
        return RestockRequest(explicit, _copy(locale, "clarify") if explicit else "", reason=reason)
    observation = captured["observation"]
    if observation.get("status") == "unknown":
        # The ordinary response path owns a real manager case and preserves
        # every customer question. Never claim a handoff from this read alone.
        return RestockRequest(False, reason="manager_review_required")
    if observation.get("status") != "unavailable":
        return RestockRequest(False, reason="target_available")
    manager_case = _prepare_manager_case(revision, row, proof, captured, settings_obj, now=now)
    if not manager_case:
        return RestockRequest(False, reason="manager_review_required")
    with transaction.atomic():
        existing = Invitation.objects.filter(revision_id=revision.pk, request_source_id=row.pk).first()
        if existing:
            if _invitation_source(existing, settings_obj, require_head=True) or _current_target(existing, now=now):
                return RestockRequest(True, _copy(locale, "stale"), reason="invitation_changed")
            return RestockRequest(True, existing.body, (_button(existing),), str(existing.pk), "existing_invitation")
        identity, key_id = uuid.uuid4(), next(iter(_keys()))
        label = _safe_label(observation, truncate=False)
        body = _fresh_copy(locale, "invite", label)
        if any(len(_fresh_copy(locale, kind, label).encode()) > 1000
            for kind in ("invite", "accepted", "notification")):
            return RestockRequest(True, _copy(locale, "clarify"), reason="target_copy_too_long")
        inv = Invitation(id=identity, client=revision.client, request_source=row, revision=revision,
            target=deepcopy(observation["target"]), target_digest=_digest(observation["target"]),
            expires_at=now + VALIDITY, acceptance_deadline=row.provider_created_at + WINDOW,
            locale=locale, body=body, button_title=_copy(locale, "button"),
            accept_payload=_payload(identity, "accept", key_id), revoke_payload=_payload(identity, "revoke", key_id))
        _seal(inv, "invitation", source=proof, selection=captured["selection"], source_cart_digest=captured["cart_digest"],
            selection_sources=captured["selection_sources"], initial_availability=observation, label=label, recipient=revision.client.igsid,
            settings_id=settings_obj.pk, settings_epoch=settings_obj.reply_permission_epoch,
            client_epoch=revision.client.reply_permission_epoch, manager_review=manager_case,
            native_future_capability="unverified", ack_copy_version=ACK_COPY_VERSION)
        return RestockRequest(True, inv.body, (_button(inv),), str(inv.pk))


def _effect_matches(inv, effect, *, body=None, action="accept", source_id=None, revision_id=None, sent=True):
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    valid_state = effect.state == "sent" and normalize_provider_message_id(effect.provider_message_id) if sent else effect.state in {"planned", "claimed", "provider_started"}
    return bool(valid_state and effect.revision_id == (revision_id or inv.revision_id)
        and effect.source_message_id == (source_id or inv.request_source_id) and effect.actor == "bot"
        and effect.purpose == "normal_reply" and effect.part_count == 1 and effect.part_index == 0
        and effect.recipient_igsid == inv.envelope["recipient"] and effect.provider_namespace == inv.envelope["source"]["namespace"]
        and effect.settings_id_snapshot == inv.envelope["settings_id"]
        and effect.settings_permission_epoch == inv.envelope["settings_epoch"]
        and effect.client_permission_epoch == inv.envelope["client_epoch"]
        and _digest(effect.payload) == effect.payload_digest
        and effect.payload.get("recipient") == {"id": inv.envelope["recipient"]}
        and effect.payload.get("message") == {"text": body or inv.body, "quick_replies": [_native_button(inv, action)]})


def _valid_receipt(inv, receipt, *, read_case=None):
    if read_case is not None:
        from management.services.ig_restock_followup import ASSISTED_PROVENANCE
        if inv.envelope.get("producer") != ASSISTED_PROVENANCE or not receipt or not receipt.followup_task_id:
            return False
    if receipt and getattr(receipt, "followup_task_id", None):
        from management.services.ig_restock_followup import valid_task_receipt
        return bool(_verified(inv, "invitation") and valid_task_receipt(inv, receipt, read_case=read_case))
    return bool(_verified(inv, "invitation") and _verified(receipt, "receipt")
        and receipt.effect_id
        and _invitation_manager_case(inv)
        and receipt.invitation_id == inv.pk and receipt.envelope.get("invitation_hmac") == inv.envelope_hmac
        and _effect_matches(inv, receipt.effect) and receipt.effect.provider_message_id == receipt.provider_message_id
        and receipt.effect.revision_snapshot_digest == inv.envelope["source"]["revision_digest"]
        and receipt.envelope.get("effect_payload_digest") == receipt.effect.payload_digest
        and receipt.envelope.get("effect_plan_digest") == receipt.effect.plan_digest)


def record_delivery(revision_id, *, now=None):
    from management.models import IgRevisionDeliveryEffect, InstagramBotSettings
    now = now or timezone.now()
    with transaction.atomic():
        inv = Invitation.objects.select_for_update().select_related("client", "request_source", "revision").filter(revision_id=revision_id).first()
        if not inv:
            return False
        settings_obj = InstagramBotSettings.objects.filter(pk=inv.envelope.get("settings_id")).first()
        if not settings_obj or _invitation_source(inv, settings_obj):
            return False
        existing = Receipt.objects.select_related("effect", "followup_task").filter(invitation_id=inv.pk).first()
        if existing:
            return _valid_receipt(inv, existing)
        effects = list(IgRevisionDeliveryEffect.objects.filter(revision_id=revision_id))
        if len(effects) != 1 or not _effect_matches(inv, effects[0]):
            return False
        effect = effects[0]
        delivered = effect.terminal_at or now
        if delivered >= inv.acceptance_deadline or effect.revision_snapshot_digest != inv.envelope["source"]["revision_digest"]:
            return False
        _seal(Receipt(invitation=inv, effect=effect, provider_message_id=effect.provider_message_id, delivered_at=delivered), "receipt",
            invitation_hmac=inv.envelope_hmac, effect_payload_digest=effect.payload_digest, effect_plan_digest=effect.plan_digest)
        return True


@contextmanager
def provider_boundary_for_revision(revision_id, *, now=None):
    from management.models import InstagramBotSettings, IgRevisionDeliveryEffect
    now = now or timezone.now()
    reason = ""
    with transaction.atomic():
        inv = Invitation.objects.select_for_update().select_related("client", "revision", "request_source").filter(revision_id=revision_id).first()
        if inv:
            settings_obj = InstagramBotSettings.objects.filter(pk=inv.envelope.get("settings_id")).first()
            reason = _invitation_source(inv, settings_obj, require_head=True) if settings_obj else "settings_missing"
            if not reason and now >= inv.acceptance_deadline:
                reason = "invitation_expired"
            if not reason:
                reason = _current_target(inv, now=now)
            if not reason:
                effects = list(IgRevisionDeliveryEffect.objects.filter(revision_id=revision_id))
                if len(effects) != 1 or not _effect_matches(inv, effects[0], sent=False):
                    reason = "invitation_physical_plan_changed"
    # No network may run in the preceding transaction.
    yield RestockBoundary(not reason, reason)


def _ack_copy_version_valid(accepted):
    return ("ack_copy_version" not in accepted.envelope
        or accepted.envelope["ack_copy_version"] == ACK_COPY_VERSION)


def _ack_copy_bodies(inv, accepted):
    """Finite pure reconstruction; the caller verifies the actual native effect."""
    if not _ack_copy_version_valid(accepted):
        return ()
    bodies = (_copy_for(inv, "accepted", accepted=accepted),)
    if "ack_copy_version" not in accepted.envelope:
        bodies += (_fresh_copy_for(inv, "accepted"),)
    return bodies


def _ack_card(inv, accepted, *, reply_to=""):
    from management.models import IgRevisionDeliveryEffect
    source = accepted.envelope["source"]
    effects = list(IgRevisionDeliveryEffect.objects.filter(revision_id=source["revision_id"], source_message_id=accepted.source_id))
    bodies = _ack_copy_bodies(inv, accepted)
    matches = [effect for effect in effects if any(_effect_matches(inv, effect, body=body,
        action="revoke", source_id=accepted.source_id, revision_id=source["revision_id"]) for body in bodies)
        and effect.revision_snapshot_digest == source["revision_digest"]
        and (not reply_to or effect.provider_message_id == reply_to)]
    if len(effects) != 1 or len(matches) != 1:
        return None
    effect = matches[0]
    return {"effect_id": effect.pk, "payload_digest": effect.payload_digest,
        "plan_digest": effect.plan_digest, "provider_message_id": effect.provider_message_id}


def _valid_answer(answer, settings_obj, *, read_case=None, accepted_answer=None):
    from types import SimpleNamespace
    if not _verified(answer, "answer") or not _valid_receipt(answer.invitation, answer.receipt, read_case=read_case) or answer.envelope.get("receipt_hmac") != answer.receipt.envelope_hmac:
        return False
    if answer.kind == "accept" and not _ack_copy_version_valid(answer):
        return False
    revision = SimpleNamespace(pk=answer.envelope.get("source", {}).get("revision_id"))
    if read_case is not None:
        owned, _ = read_case.owned_source(revision, answer.source, settings_obj)
    else:
        # The canonical owner reader reloads this exact revision and client;
        # an earlier fetch adds no validation or source freshness.
        owned, _ = owned_source(revision, answer.source, settings_obj, require_head=False)
    if not owned or owned[2] != answer.envelope.get("source") or owned[1].quick_reply_payload != answer.envelope.get("payload"):
        return False
    if answer.kind == "revoke":
        accepted = (accepted_answer if accepted_answer is not None else
            Answer.objects.select_related("invitation", "receipt__effect", "receipt__followup_task", "source").filter(invitation_id=answer.invitation_id, kind="accept").first())
        proof = answer.envelope.get("ack_card")
        return bool(accepted and accepted.kind == "accept" and accepted.invitation_id == answer.invitation_id
            and _valid_answer(accepted, settings_obj, read_case=read_case) and proof
            and _ack_card(answer.invitation, accepted, reply_to=proof.get("provider_message_id", "")) == proof)
    return answer.kind == "accept" and answer.envelope.get("payload") == answer.invitation.accept_payload


def handle_reply(row, revision=None, settings_obj=None, now=None):
    from management.models import InstagramBotSettings, IgCustomerTurnRevision
    if not _enabled():
        return RestockRequest(False, reason="disabled")
    payload = str(getattr(row, "quick_reply_payload", "") or "")
    if not payload.startswith(PREFIX):
        return RestockRequest(False, reason="not_signed_quick_reply")
    now = now or timezone.now()
    settings_obj = settings_obj or InstagramBotSettings.load()
    settings_obj = InstagramBotSettings.objects.filter(pk=settings_obj.pk).first()
    if not settings_obj:
        return RestockRequest(True, _copy("uk", "stale"), reason="settings_missing")
    match = re.fullmatch(r"RS1:([0-9a-f-]{36}):(accept|revoke):([0-9a-f]{24}):([0-9a-f]{64})", payload)
    if not match:
        return RestockRequest(True, _copy("uk", "stale"), reason="invalid_payload")
    identity, action, key_id, _ = match.groups()
    try:
        if str(uuid.UUID(identity)) != identity:
            raise ValueError()
    except ValueError:
        return RestockRequest(True, _copy("uk", "stale"), reason="invalid_payload")
    with transaction.atomic():
        inv = Invitation.objects.select_for_update().select_related("client", "revision", "request_source").filter(pk=identity).first()
        locale = inv.locale if inv else "uk"
        invalid = RestockRequest(True, _copy(locale, "stale"), reason="answer_not_current")
        if not inv or key_id != inv.key_id or payload != _payload(identity, action, key_id) or _invitation_source(inv, settings_obj):
            return invalid
        if row.client_id != inv.client_id or row.pk <= inv.request_source_id:
            return invalid
        revision = revision or IgCustomerTurnRevision.objects.filter(client_id=inv.client_id, active_slot=1).first()
        if not revision:
            return invalid
        owned, _ = owned_source(revision, row, settings_obj)
        receipt = Receipt.objects.select_related("effect", "followup_task").filter(invitation_id=inv.pk).first()
        if (not owned or owned[1].quick_reply_payload != payload or owned[1].attachments or owned[1].attachment_media
            or not _valid_receipt(inv, receipt) or row.provider_created_at < receipt.delivered_at
            or row.provider_created_at > now + timedelta(minutes=1) or row.status == "ignored"):
            return invalid
        revision, row, proof = owned
        previous = Answer.objects.select_related("invitation", "receipt__effect", "receipt__followup_task", "source").filter(invitation_id=inv.pk, kind=action).first()
        if previous:
            if not _valid_answer(previous, settings_obj):
                return invalid
            if action == "accept" and (now >= inv.expires_at or not Subscription.objects.filter(accepted_answer_id=previous.pk, state="active").exists()
                or Answer.objects.filter(invitation_id=inv.pk, kind="revoke").exists()):
                return invalid
            if (action == "accept" and "ack_copy_version" in previous.envelope
                and previous.envelope["ack_copy_version"] != ACK_COPY_VERSION):
                return invalid
            body = (_accepted_reply_copy(inv, previous) if action == "accept"
                else _copy_for(inv, "revoked", accepted=previous))
            if len(body.encode()) > 1000:
                return RestockRequest(True, _copy(locale, "clarify"), reason="target_copy_too_long")
            return RestockRequest(True, body,
                (_button(inv, "revoke"),) if action == "accept" and row.pk == previous.source_id else (), reason="duplicate_answer")
        ack = None
        if action == "accept":
            if now >= inv.acceptance_deadline or now >= inv.expires_at or _current_target(inv, now=now):
                return invalid
            if row.reply_to_provider_message_id and row.reply_to_provider_message_id != receipt.provider_message_id:
                return invalid
            if Answer.objects.filter(invitation_id=inv.pk, kind="revoke").exists():
                return invalid
        else:
            accepted = Answer.objects.select_related("invitation", "receipt__effect", "receipt__followup_task", "source").filter(invitation_id=inv.pk, kind="accept").first()
            if not accepted or not _valid_answer(accepted, settings_obj):
                return invalid
            ack = _ack_card(inv, accepted, reply_to=row.reply_to_provider_message_id)
            if not ack:
                return invalid
        if action == "accept" and len(_fresh_copy_for(inv, "accepted").encode()) > 1000:
            return RestockRequest(True, _copy(locale, "clarify"), reason="target_copy_too_long")
        version = {"ack_copy_version": ACK_COPY_VERSION} if action == "accept" else {}
        answer = _seal(Answer(invitation=inv, source=row, receipt=receipt, kind=action, answered_at=row.provider_created_at), "answer",
            source=proof, payload=payload, receipt_hmac=receipt.envelope_hmac, ack_card=ack, **version)
        if action == "accept":
            Subscription.objects.create(accepted_answer=answer, client=inv.client, expires_at=inv.expires_at, next_check_at=now)
        else:
            Subscription.objects.filter(accepted_answer__invitation_id=inv.pk).exclude(state="sent").update(state="revoked", last_reason="customer_revoked")
        return RestockRequest(True, _copy_for(inv, "accepted" if action == "accept" else "revoked", accepted=answer),
            (_button(inv, "revoke"),) if action == "accept" else (), str(inv.pk), action)


def _subscription_reason(subscription, settings_obj, *, now):
    accepted = subscription.accepted_answer
    inv = accepted.invitation
    if not _valid_answer(accepted, settings_obj) or _invitation_source(inv, settings_obj):
        return "subscription_source_changed"
    if subscription.client_id != inv.client_id or subscription.expires_at != inv.expires_at:
        return "subscription_binding_changed"
    if now >= inv.expires_at:
        return "expired"
    revoked = Answer.objects.select_related("invitation", "receipt__effect", "receipt__followup_task", "source").filter(invitation_id=inv.pk, kind="revoke").first()
    if revoked:
        return "customer_revoked" if _valid_answer(revoked, settings_obj) else "revocation_evidence_invalid"
    if subscription.state != "active":
        return "subscription_" + subscription.state
    return ""


def manager_admission(subscription, availability, *, now):
    """No mutable approval note, inventory change or consent substitutes this gate."""
    from management.services import ig_restock_waiting_list as waiting
    if waiting.is_list(subscription.accepted_answer.invitation.target):
        from management.services.ig_restock_waiting_list_review import manager_admission as admit_list
        return admit_list(subscription, availability, now=now)
    try:
        from management.services.ig_restock_manager_gate import admit_restock
    except ImportError:
        return {"ready": False, "reason": "manager_decision_unverified", "proof": {}}
    admission = admit_restock(subscription, availability, now=now)
    if not isinstance(admission, dict) or not isinstance(admission.get("proof"), dict):
        return {"ready": False, "reason": "manager_decision_unverified", "proof": {}}
    return admission


def _create_availability_event(subscription, observation, admission, *, now):
    from management.models import IgFollowUpTask
    if (not admission.get("ready") or not admission.get("proof")
        or observation.get("status") not in {"available", "unavailable", "unknown"}):
        return False
    accepted = subscription.accepted_answer
    inv = accepted.invitation
    if observation.get("target") != inv.target or AvailabilityEvent.objects.filter(subscription_id=subscription.pk).exists():
        return False
    identity = uuid.uuid4()
    event_key = f"{ORIGIN}:{subscription.pk}"
    task = IgFollowUpTask.objects.create(client_id=subscription.client_id, due_at=now, kind="qualification",
        trigger="event", reason=ORIGIN, policy_version="restock-agreement.v1", event_key=event_key,
        event_occurred_at=now, event_payload={"origin": ORIGIN, "purpose": PURPOSE,
            "subscription_id": str(subscription.pk), "event_id": str(identity)})
    _seal(AvailabilityEvent(id=identity, subscription=subscription, task=task, event_key=event_key,
        observed_at=now, availability_digest=observation["digest"]), "event", availability=deepcopy(observation),
        answer_hmac=accepted.envelope_hmac, target_digest=inv.target_digest, manager_decision=deepcopy(admission["proof"]))
    return True


def reconcile_subscriptions(limit=50, now=None):
    from management.models import InstagramBotSettings
    from management.services.ig_restock_availability import observe_target
    if not _enabled():
        return {"checked": 0, "created": 0, "reason": "disabled"}
    now = now or timezone.now()
    checked = created = 0
    candidates = list(Subscription.objects.filter(state="active", next_check_at__lte=now)
        .order_by("next_check_at", "pk").values_list("pk", flat=True)[:max(0, min(int(limit), 50))])
    for identity in candidates:
        with transaction.atomic():
            subscription = Subscription.objects.select_for_update().select_related("accepted_answer__invitation__client",
                "accepted_answer__invitation__request_source", "accepted_answer__invitation__revision", "accepted_answer__receipt__effect", "accepted_answer__receipt__followup_task", "accepted_answer__source").filter(pk=identity, state="active", next_check_at__lte=now).first()
            if not subscription:
                continue
            inv = subscription.accepted_answer.invitation
            settings_obj = InstagramBotSettings.objects.filter(pk=inv.envelope.get("settings_id")).first()
            reason = _subscription_reason(subscription, settings_obj, now=now) if settings_obj else "settings_missing"
            if not reason:
                observation = _observe_target(inv.target, now=now)
                if observation.get("target") != inv.target:
                    reason = "inventory_target_changed"
                else:
                    try:
                        from management.services.ig_fulfillment_reviews import ensure_restock_manager_review
                    except ImportError:
                        reason = "manager_decision_unverified"
                    else:
                        ensure_restock_manager_review(subscription, observation, now=now)
                        admission = manager_admission(subscription, observation, now=now)
                        reason = admission.get("reason") or ("ready" if admission.get("ready") else "manager_decision_unverified")
                        created += int(_create_availability_event(subscription, observation, admission, now=now))
            subscription.last_checked_at = now
            subscription.next_check_at = now + CHECK_INTERVAL
            subscription.last_reason = reason
            if reason == "expired":
                subscription.state = "expired"
            elif reason == "customer_revoked":
                subscription.state = "revoked"
            subscription.save(update_fields=["last_checked_at", "next_check_at", "last_reason", "state"])
            checked += 1
    return {"checked": checked, "created": created}


def observe_inventory_event(source: dict, *, now=None):
    # A hook is a wake-up only. A fresh exact-target manager decision is still
    # required; this must not assert a warehouse actor from catalogue edits.
    if not isinstance(source, dict) or source.get("kind") not in {"catalog_edit", "warehouse_movement"}:
        return {"checked": 0, "created": 0, "reason": "inventory_source_unverified"}
    now = now or timezone.now()
    occurred_at = source.get("occurred_at")
    if not hasattr(occurred_at, "tzinfo") or occurred_at.tzinfo is None or occurred_at > now + timedelta(minutes=1):
        return {"checked": 0, "created": 0, "reason": "inventory_source_unverified"}
    candidates = Subscription.objects.filter(state="active")
    if source["kind"] == "catalog_edit":
        from productcolors.models import ProductColorVariant
        from management.services.bot_followups import variant_inventory_revision
        variant = ProductColorVariant.objects.filter(pk=source.get("variant_id"), product_id=source.get("product_id")).first()
        if not variant or source.get("source_revision") != f"product_catalog:{variant_inventory_revision(variant.pk)}":
            return {"checked": 0, "created": 0, "reason": "inventory_source_stale"}
        from django.db.models import Q
        from management.services.ig_restock_waiting_list import SCHEMA
        candidates = candidates.filter(Q(accepted_answer__invitation__target__product_id=variant.product_id,
            accepted_answer__invitation__target__color_variant_id=variant.pk) | Q(accepted_answer__invitation__target__schema=SCHEMA))
    else:
        from django.contrib.contenttypes.models import ContentType
        from warehouse.models import StockItem, StockMovement
        movement = StockMovement.objects.filter(pk=source.get("movement_id"), object_id=source.get("stock_item_id"),
            content_type=ContentType.objects.get_for_model(StockItem), delta__gt=0, created_at=occurred_at).first()
        if not movement:
            return {"checked": 0, "created": 0, "reason": "inventory_source_unverified"}
    # Only a bounded fair slice is woken. Neither the callback nor mutable
    # scheduling fields bypass fresh signed target/stock/manager validation.
    ids = list(candidates.order_by("next_check_at", "pk").values_list("pk", flat=True)[:50])
    Subscription.objects.filter(pk__in=ids).update(next_check_at=now)
    return reconcile_subscriptions(now=now)


def process_due_restock_notifications(settings_obj, now=None, limit=50):
    from management.services.ig_restock_subscription_delivery import process_due_restock_notifications as process
    return process(settings_obj, now=now, limit=limit)


def restock_projection(client_id, now=None):
    from management.models import InstagramBotSettings
    now = now or timezone.now()
    client_id = getattr(client_id, "pk", client_id)
    result = []
    rows = Subscription.objects.filter(client_id=client_id).select_related("accepted_answer__invitation__client",
        "accepted_answer__invitation__revision", "accepted_answer__invitation__request_source",
        "accepted_answer__receipt__effect", "accepted_answer__receipt__followup_task", "accepted_answer__source").order_by("-created_at")[:16]
    for subscription in rows:
        answer, inv = subscription.accepted_answer, subscription.accepted_answer.invitation
        settings_obj = InstagramBotSettings.objects.filter(pk=inv.envelope.get("settings_id")).first()
        reason = _subscription_reason(subscription, settings_obj, now=now) if settings_obj else "settings_missing"
        valid = _valid_answer(answer, settings_obj) if settings_obj else False
        result.append({"subscription_id": str(subscription.pk), "state": subscription.state if valid else "unverified",
            "reason": reason, "label": _label(inv) if valid else "", "target": deepcopy(inv.target) if valid else {},
            "expires_at": inv.expires_at.isoformat() if valid else None, "accepted_at": answer.answered_at.isoformat() if valid else None,
            "last_checked_at": subscription.last_checked_at.isoformat() if subscription.last_checked_at else None,
            "source_refs": {"request": inv.request_source_id, "card": answer.receipt.effect_id, "answer": answer.source_id,
                **({"card_task": answer.receipt.followup_task_id} if answer.receipt.followup_task_id else {})} if valid else {},
            "native_future_capability": "unverified"})
    return {"version": 1, "client_id": client_id, "captured_at": now.isoformat(), "subscriptions": result,
        "coverage_complete": len(result) < 16, "native_future_capability": "unverified"}
