"""One native customer update for a signed, current manager cart decision.

The existing FollowUpTask is this purpose's durable delivery effect. A native
MID is committed before conversation history; uncertain outcomes never replay.
"""
from contextlib import contextmanager
from datetime import timedelta
import json
import hmac
import re
import uuid
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from management.services.ig_payment_reminder_time import quiet_time_allowed
from management.services.ig_purpose_sources import latest_customer_source
from management.services import ig_restock_followup as restock_followup

ORIGIN = "fulfillment_manager_update"
CHECK_INTERVAL = timedelta(minutes=15)
WINDOW = timedelta(hours=23)
MAX_BYTES = 950
HOLD_REASONS = {"waiting_window", "quiet_hours", "pending_inbound", "permission_denied",
    "manager_review_pending", "manager_review_expired", "manager_review_stale",
    "summary_requires_manager_clarification", "source_not_ready", "manager_decision_unverified",
    "other_availability_send_in_progress", "availability_coordination_unverified"}
KYIV = ZoneInfo("Europe/Kyiv")
COPY = {
    "uk": {"intro": "Менеджер перевірив позиції:", "available": "у наявності", "unavailable": "немає в наявності",
        "wait": "очікуємо", "clarify": "потрібне уточнення", "preparation": "підготовка", "dispatch": "відправка", "base_arrival": "прибуття основи до нас",
        "calendar": "календ. дн.", "business": "роб. дн.", "from": "від", "number": "поз.", "same_day": "у день перевірки",
        "arrival": "Це строки підготовки/відправки, не гарантія дати прибуття.",
        "deadline": "Потрібну вам дату ще слід звірити з доставкою.",
        "split": "Хочете обговорити окремі відправлення? Умови доставки погодимо до оформлення.",
        "clarification": "Уточнімо деталі перед оформленням.", "alternative": "Заміни погодимо окремо."},
    "ru": {"intro": "Менеджер проверил позиции:", "available": "в наличии", "unavailable": "нет в наличии",
        "wait": "ожидаем", "clarify": "нужно уточнение", "preparation": "подготовка", "dispatch": "отправка", "base_arrival": "поступление основы к нам",
        "calendar": "календ. дн.", "business": "раб. дн.", "from": "от", "number": "поз.", "same_day": "в день проверки",
        "arrival": "Это сроки подготовки/отправки, не гарантия даты прибытия.",
        "deadline": "Нужную вам дату ещё следует сверить с доставкой.",
        "split": "Хотите обсудить отдельные отправления? Условия доставки согласуем до оформления.",
        "clarification": "Уточним детали перед оформлением.", "alternative": "Замены согласуем отдельно."},
    "en": {"intro": "Our manager checked the items:", "available": "available", "unavailable": "unavailable",
        "wait": "awaiting availability", "clarify": "clarification needed", "preparation": "preparation", "dispatch": "dispatch", "base_arrival": "base arrival to us",
        "calendar": "calendar days", "business": "business days", "from": "from", "number": "item", "same_day": "on the check date",
        "arrival": "Preparation/dispatch timings do not guarantee an arrival date.",
        "deadline": "Your required date still needs to be checked against delivery.",
        "split": "Would you like to discuss separate shipments? We will agree delivery terms before ordering.",
        "clarification": "Let us clarify the details before ordering.", "alternative": "We will agree any substitutions separately."},
}

# COPY is the legacy renderer used by already delivered tasks. New attempts
# freeze their copy and original task binding under a separate HMAC.
CUSTOMER_COPY = {locale: {**copy, "arrival": arrival} for locale, copy, arrival in (
    ("uk", COPY["uk"], "Допоможемо оформити й відправити замовлення. Точний день отримання залежить від перевізника."),
    ("ru", COPY["ru"], "Поможем оформить и отправить заказ. Точный день получения зависит от перевозчика."),
    ("en", COPY["en"], "We’ll help you place and ship the order. The exact arrival date depends on the carrier."),
)}
CUSTOMER_COPY_KEY = "fulfillment_customer_copy"


def _copy_binding(task):
    return {"version": 2, "task_id": task.pk, "client_id": task.client_id,
        "event_key": task.event_key, "event_payload": task.event_payload}


def render_task_update(task, projection):
    """Verify frozen new copy; unmarked historic tasks use the exact old copy."""
    frozen = (task.manager_context or {}).get(CUSTOMER_COPY_KEY)
    if frozen is None:
        return _render_update(task, projection)
    if (not isinstance(frozen, dict) or set(frozen) != {"body", "key_id", "hmac"}
        or not isinstance(frozen["key_id"], str) or not isinstance(frozen["hmac"], str)):
        return ""
    body = frozen["body"]
    if not isinstance(body, dict) or set(body) != {*_copy_binding(task), "text"}:
        return ""
    if any(body.get(key) != value for key, value in _copy_binding(task).items()):
        return ""
    expected = _core()._sign("customer_update_copy", body, frozen["key_id"])
    text = body["text"]
    if (not expected or not isinstance(frozen["hmac"], str) or not hmac.compare_digest(expected, frozen["hmac"])
        or not isinstance(text, str) or not text or len(text.encode()) > MAX_BYTES):
        return ""
    request, decision = projection.get("current_request") or {}, projection.get("decision") or {}
    data = task.event_payload or {}
    if str(request.get("id")) != data.get("request_id") or str(decision.get("id")) != data.get("decision_id"):
        return ""
    return text


def _freeze_task_update(task, projection, text):
    body = {**_copy_binding(task), "text": text}
    key_id = next(iter(_core()._keys()))
    task.manager_context = {**(task.manager_context or {}), CUSTOMER_COPY_KEY: {
        "body": body, "key_id": key_id, "hmac": _core()._sign("customer_update_copy", body, key_id)}}
    if render_task_update(task, projection) != text:
        raise ValueError("customer_update_copy_invalid")


def _timing(value, phase, copy):
    if value is None:
        return ""
    if not isinstance(value, dict):
        raise ValueError("invalid_timing")
    low, high, unit = value.get("min_days"), value.get("max_days"), value.get("unit")
    anchor = parse_datetime(str(value.get("anchor_at") or ""))
    after_payment = value.get("anchor") == "after_payment_confirmation"
    if (type(low) is not int or type(high) is not int or not 0 <= low <= high <= 365
            or unit not in {"calendar", "business"} or (not after_payment and (not anchor or timezone.is_naive(anchor)))):
        raise ValueError("invalid_timing")
    if after_payment:
        days = str(low) if low == high else f"{low}–{high}"
        phrase = {"from": "after payment is confirmed", "від": "після підтвердження оплати", "от": "после подтверждения оплаты"}[copy["from"]]
        return f"{copy[phase]} {days} {copy[unit]} {phrase}"
    if low == high == 0:
        return f"{copy[phase]} {copy['same_day']} ({anchor.astimezone(KYIV):%d.%m.%Y})"
    days = str(low) if low == high else f"{low}–{high}"
    return f"{copy[phase]} {days} {copy[unit]} {copy['from']} {anchor.astimezone(KYIV):%d.%m.%Y}"


def render_manager_update(projection, *, copy_version=1):
    """Finite typed facts only; numbering binds compressed groups to cart order."""
    request, decision = projection.get("current_request") or {}, projection.get("decision") or {}
    locale = request.get("locale") or "uk"
    copies = CUSTOMER_COPY if copy_version == 2 else COPY
    copy = copies.get(locale, copies["uk"])
    lines, decisions = request.get("lines"), decision.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 20 or not isinstance(decisions, list):
        return ""
    by_id = {str(row.get("line_id")): row for row in decisions if isinstance(row, dict)}
    if len(by_id) != len(decisions) or len(by_id) != len(lines):
        return ""
    if any(not isinstance(line, dict) for line in lines) or {str(line.get("line_id")) for line in lines} != set(by_id):
        return ""
    groups, detailed = {}, []
    try:
        for index, line in enumerate(lines, 1):
            row = by_id.get(str(line.get("line_id")))
            if not row or row.get("availability") not in {"available", "unavailable", "wait", "clarify"}:
                return ""
            facts = [copy[row["availability"]]]
            for phase in ("base_arrival", "preparation", "dispatch"):
                timing = _timing(row.get(phase), phase, copy)
                if timing:
                    facts.append(timing)
            detail = "; ".join(facts)
            groups.setdefault(detail, []).append(index)
            label = re.sub(r"\s+", " ", str(line.get("label") or "")).strip()
            # Labels are catalogue display values, never an action or raw URL.
            if re.search(r"https?://|[<>\x00-\x1f]", label, re.I):
                label = ""
            label = label[:60].rstrip()
            detailed.append(f"{index}. {label + ': ' if label else ''}{detail}.")
        # A plain availability confirmation does not need an unrelated
        # delivery warning. Any stated timing or customer deadline does.
        has_timing = any(row.get("preparation") or row.get("dispatch") for row in decisions)
        suffix = []
        if any(row.get("base_arrival") for row in decisions):
            suffix.append({"uk": "Це прибуття основи до нас; строк підготовки та відправки погодимо окремо.",
                "ru": "Это поступление основы к нам; сроки подготовки и отправки согласуем отдельно.",
                "en": "This is the base reaching us; preparation and dispatch timings are separate."}.get(locale, "Preparation and dispatch timings are separate."))
        if decision.get("waiting_acceptance_required") is True:
            suffix.append({"uk": "Чи згодні ви зачекати на цих умовах? Сповіщення про наявність не означає згоди чекати.",
                "ru": "Согласны ли вы подождать на этих условиях? Уведомление о наличии не означает согласия ждать.",
                "en": "Do you agree to wait on these terms? Restock notifications do not mean agreement to wait."}.get(locale, "Do you agree to wait on these terms?"))
        requirements = request.get("customer_requirements") or []
        has_deadline = any(isinstance(hint, dict) and hint.get("date_status") in {"resolved", "past", "needs_clarification"}
                and hint.get("supersession_hint") != "withdrawn" for hint in requirements)
        if has_timing or has_deadline:
            suffix.append(copy["arrival"])
        deadline_review = decision.get("deadline_review") or {}
        outcome = deadline_review.get("outcome")
        if outcome == "impossible":
            suffix.append({"en": "We cannot meet your departure deadline, including with expedited service.",
                "uk": "Не встигнемо до вашого від’їзду, навіть із терміновим виконанням.",
                "ru": "Не успеем до вашего отъезда, даже при срочном выполнении."}.get(locale, "We cannot meet your departure deadline, including with expedited service."))
            option_copy = {
                "en": {"later_domestic_contact": "Would you like your boyfriend or another contact in Ukraine to receive it later?",
                    "later_domestic_destination": "We can discuss later delivery by Nova Poshta to where you will be staying in Ukraine; carrier transit is separate from preparation.",
                    "international_review_required": "For international delivery, the team must first check feasibility and a shipping quote."},
                "uk": {"later_domestic_contact": "Можемо обговорити отримання пізніше вашим хлопцем або іншою контактною особою в Україні.",
                    "later_domestic_destination": "Можемо обговорити пізніше отримання Новою поштою там, де ви перебуватимете в Україні; перевезення окреме від підготовки.",
                    "international_review_required": "Міжнародну доставку спочатку перевіримо щодо можливості та вартості."},
                "ru": {"later_domestic_contact": "Можем обсудить получение позже вашим парнем или другим контактным лицом в Украине.",
                    "later_domestic_destination": "Можем обсудить получение позже Новой почтой там, где вы будете в Украине; перевозка отдельно от подготовки.",
                    "international_review_required": "Международную доставку сначала проверим на возможность и стоимость."}}
            for option in deadline_review.get("options") or []:
                if option not in option_copy.get(locale, option_copy["en"]):
                    return ""
                suffix.append(option_copy.get(locale, option_copy["en"])[option])
        elif has_deadline:
            suffix.append(copy["deadline"])
        if any(isinstance(hint, dict) and hint.get("date_status") == "needs_clarification"
                and hint.get("supersession_hint") != "withdrawn" for hint in requirements):
            suffix.append(copy["clarification"])
        if any(row.get("availability") == "clarify" or row.get("clarification") for row in decisions):
            suffix.append(copy["clarification"])
        if any(row.get("alternative") for row in decisions):
            suffix.append(copy["alternative"])
        split = decision.get("split_offer")
        if split:
            if not isinstance(split, dict) or split.get("feasible") is not True:
                return ""
            # A split is only a question. Its price/payer needs explicit consent;
            # this update creates neither an order nor a delivery charge.
            suffix.append(copy["split"])
        message = "\n".join([copy["intro"], *detailed, *suffix])
        if len(message.encode("utf-8")) <= MAX_BYTES:
            return message
        compact = [f"{copy['number']} {', '.join(map(str, positions))}: {facts}." for facts, positions in groups.items()]
        message = "\n".join([copy["intro"], *compact, *suffix])
        return message if len(message.encode("utf-8")) <= MAX_BYTES else ""
    except (TypeError, ValueError, AttributeError):
        return ""


def _core():
    from management.services import ig_fulfillment_reviews
    return ig_fulfillment_reviews


def _assisted_task(task):
    from management.services import ig_assisted_manager_update
    return bool(task and (task.event_payload or {}).get("source_kind") == ig_assisted_manager_update.SOURCE_KIND)


def validated_update_task(task, settings_obj, *, now):
    """An explicit source type owns its tables, currentness and approval proof."""
    from management.services import ig_assisted_manager_update
    if task and "source_kind" in (task.event_payload or {}):
        if not _assisted_task(task):
            return {"ready": False, "reason": "task_source_kind_unknown"}
        return ig_assisted_manager_update.validated_update_task(task, settings_obj, now=now)
    return _core().validated_update_task(task, settings_obj, now=now)


def validated_update_history(task):
    from management.services import ig_assisted_manager_update
    if task and "source_kind" in (task.event_payload or {}):
        if not _assisted_task(task):
            return {"ready": False, "reason": "task_source_kind_unknown"}
        return ig_assisted_manager_update.validated_update_history(task)
    return _core().validated_update_history(task)


def _render_update(task, projection, *, copy_version=1):
    if _assisted_task(task):
        from management.services import ig_assisted_manager_update
        return ig_assisted_manager_update.render_manager_update(projection, copy_version=copy_version)
    return render_manager_update(projection, copy_version=copy_version)


def _guard(task, settings_obj, *, now):
    from management.models import IgClient
    if not task or task.reason != ORIGIN or task.trigger != "event":
        return None, "task_binding_changed"
    projection = validated_update_task(task, settings_obj, now=now)
    if not isinstance(projection, dict) or not projection.get("ready"):
        return None, (projection.get("reason") if isinstance(projection, dict) else "") or "manager_decision_unverified"
    request = projection.get("current_request") or {}
    expected_scope = "assisted_cart" if _assisted_task(task) else "cart"
    if request.get("scope") != expected_scope or request.get("client_id") != task.client_id or not projection.get("proof"):
        return None, "task_binding_changed"
    if _assisted_task(task):
        reason = restock_followup.subscription_overlap_reason(task.client_id, projection["waiting_list"],
            settings_obj, now=now, exact_scope=True)
        if reason:
            return None, reason
    from management.services.ig_fulfillment_notification_coordination import cart_update_coordination
    coordinated = ({} if _assisted_task(task) else cart_update_coordination(task, projection, settings_obj, now=now))
    if coordinated.get("reason"):
        return None, coordinated["reason"]
    client = IgClient.objects.filter(pk=task.client_id).first()
    if not client or client.privacy_erasure_started_at:
        return None, "client_erased"
    proof = projection.get("proof")
    if not isinstance(proof, dict) or proof.get("recipient") != client.igsid or proof.get("source_namespace") != request.get("source_namespace"):
        return None, "task_binding_changed"
    if latest_customer_source(client, settings_obj, now=now, after_id=int(request.get("source_id") or 0), pending_only=True):
        return None, "pending_inbound"
    source = latest_customer_source(client, settings_obj, now=now)
    if source and source.provider_namespace != proof["source_namespace"]:
        return None, "task_binding_changed"
    if not source or not source.provider_created_at or now >= source.provider_created_at + WINDOW:
        return None, "waiting_window"
    if not quiet_time_allowed(now):
        return None, "quiet_hours"
    text = (render_task_update(task, projection) if CUSTOMER_COPY_KEY in (task.manager_context or {})
        or task.status != "pending" or task.attempt_count else _render_update(task, projection, copy_version=2))
    if not text:
        return None, "summary_requires_manager_clarification"
    if restock_followup.TASK_KEY in (task.manager_context or {}):
        offer = restock_followup.task_offer(task, projection, settings_obj, text=text, now=now)
        if not offer["ready"]:
            return None, offer["reason"]
    return (projection, text), ""


def _defer(task, reason, now):
    task.status = "pending" if reason in HOLD_REASONS else "cancelled"
    task.skip_reason = reason
    task.claim_token, task.claim_until, task.attempt_count = "", None, 0
    task.next_attempt_at = now + CHECK_INTERVAL if reason in HOLD_REASONS else None
    task.save(update_fields=["status", "skip_reason", "claim_token", "claim_until", "attempt_count", "next_attempt_at", "updated_at"])


@contextmanager
def _physical_boundary(task_id, token, settings_obj, text, proof, *, quick_replies=(), now=None, admission=None, **delivery):
    from management.models import IgFollowUpTask
    from management.services.instagram_bot import ProviderRequestBoundaryResult
    clock = now or timezone.now()
    with transaction.atomic():
        task = IgFollowUpTask.objects.select_for_update().filter(pk=task_id).first()
        guard, reason = _guard(task, settings_obj, now=clock)
        if not reason and (task.status != "processing" or task.claim_token != token or task.provider_message_id
                or not task.claim_until or task.claim_until <= clock):
            reason = "claim_changed"
        if not reason and (guard[1] != text or task.message_text != text or guard[0]["proof"] != proof
                or delivery.get("delivered_chunk_count", 0) != 0 or delivery.get("planned_chunk_count", 1) != 1):
            reason = "physical_message_changed"
        if not reason:
            from management.services.instagram_bot import _provider_quick_replies_payload
            offer = restock_followup.task_offer(task, guard[0], settings_obj, text=text, now=clock)
            expected = offer["quick_replies"] if offer["ready"] else ()
            if _provider_quick_replies_payload(quick_replies) != _provider_quick_replies_payload(expected):
                reason = "physical_buttons_changed"
        if reason and task and task.status == "processing" and task.claim_token == token and not task.provider_message_id:
            _defer(task, reason, clock)
    if admission is not None and not reason:
        admission["admitted"] = True
    yield ProviderRequestBoundaryResult(not reason, reason=reason)


def _checkpoint(task_id, token, mid, text, *, now=None):
    from management.models import IgFollowUpTask
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    mid = normalize_provider_message_id(mid)
    if not mid:
        return False
    with transaction.atomic():
        task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
        if task.provider_message_id:
            return task.provider_message_id == mid
        if (task.reason != ORIGIN or task.status != "processing" or task.claim_token != token
                or task.attempt_count != 1 or task.message_text != text):
            return False
        task.provider_message_id, task.status, task.sent_at = mid, "sent", now or timezone.now()
        task.claim_token, task.claim_until = "", None
        task.save(update_fields=["provider_message_id", "status", "sent_at", "claim_token", "claim_until", "updated_at"])
    return True


def _history(task_id):
    from management.models import IgFollowUpTask, InstagramBotMessage
    try:
        with transaction.atomic():
            task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
            if task.status != "sent" or not task.provider_message_id or task.sent_message_id:
                return bool(task.sent_message_id)
            binding = validated_update_history(task)
            if not isinstance(binding, dict) or not binding.get("ready"):
                return False
            if render_task_update(task, binding) != task.message_text:
                return False
            task.sent_message = InstagramBotMessage.objects.create(client_id=task.client_id, sender_id=binding["recipient"],
                role="model", source="followup", status="done", text=task.message_text,
                provider_message_id=task.provider_message_id, provider_namespace=binding["source_namespace"], processed_at=task.sent_at)
            task.save(update_fields=["sent_message", "updated_at"])
        return True
    except Exception:
        return False


def _finish(task_id, token, receipt, *, admitted, now=None):
    from management.models import IgFollowUpTask
    from management.services.instagram_bot import ProviderDeliveryReceipt
    from management.services.ig_delivery_receipts import normalize_provider_message_ids
    native = isinstance(receipt, ProviderDeliveryReceipt)
    mids = normalize_provider_message_ids(receipt.provider_message_ids) if native else ()
    ok = bool(admitted and native and receipt.ok and len(mids) == 1 and receipt.planned_chunk_count == 1 and receipt.delivered_chunk_count == 1)
    with transaction.atomic():
        task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
        if task.provider_message_id:
            if (mids and (len(mids) != 1 or mids[0] != task.provider_message_id)
                    or native and receipt.request_text and receipt.request_text != task.message_text):
                task.status, task.last_error = "ambiguous", "native_receipt_identity_changed"
                task.save(update_fields=["status", "last_error", "updated_at"])
                return False
            ok = task.status == "sent"
        elif task.status == "processing" and task.claim_token == token:
            if native and receipt.request_text and receipt.request_text != task.message_text:
                ok = False
            if ok:
                task.provider_message_id, task.status, task.sent_at = mids[0], "sent", now or timezone.now()
            elif mids or (admitted and not (native and receipt.kind == "cancelled" and not receipt.delivered_chunk_count)):
                task.status, task.last_error = "ambiguous", "provider_outcome_unknown_no_retry"
                task.provider_message_id = mids[0] if len(mids) == 1 else ""
            else:
                task.status, task.attempt_count, task.skip_reason = "pending", 0, "known_not_dispatched"
                task.next_attempt_at = (now or timezone.now()) + CHECK_INTERVAL
            task.claim_token, task.claim_until = "", None
            task.save(update_fields=["provider_message_id", "status", "sent_at", "last_error", "attempt_count", "skip_reason", "claim_token", "claim_until", "next_attempt_at", "updated_at"])
        else:
            ok = False
    if ok:
        _history(task_id)
        restock_followup.record_task_delivery(task_id, now=now)
    return ok


def process_due_fulfillment_updates(settings_obj, now=None, limit=20):
    from django.db.models import Q
    from management.models import IgFollowUpTask, InstagramBotSettings
    from management.services import instagram_bot as bot
    from management.services.ig_reply_boundary import capture_reply_permission, customer_send_boundary
    if not _core()._enabled():
        return 0
    settings_obj = settings_obj or InstagramBotSettings.load()
    fixed_clock, clock = now, now or timezone.now()
    bound = max(0, min(int(limit), 20))
    repair = Q(sent_message__isnull=True) | Q(**{
        "manager_context__has_key": restock_followup.TASK_KEY, "restock_invitation_receipt__isnull": True})
    repairs = IgFollowUpTask.objects.filter(repair, reason=ORIGIN, status="sent").exclude(provider_message_id="").filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=clock))
    for identity in repairs.order_by("next_attempt_at", "pk").values_list("pk", flat=True)[:bound]:
        history_ok = _history(identity)
        receipt_ok = restock_followup.record_task_delivery(identity, now=clock)
        # Unrepairable historic evidence must not starve newer checkpoints.
        # This is a projection retry only: SENT tasks can never be dispatched.
        task = IgFollowUpTask.objects.filter(pk=identity, status="sent").first()
        if task and (not history_ok or restock_followup.TASK_KEY in (task.manager_context or {}) and not receipt_ok):
            IgFollowUpTask.objects.filter(pk=identity, status="sent").update(next_attempt_at=clock + CHECK_INTERVAL)
    IgFollowUpTask.objects.filter(pk__in=list(IgFollowUpTask.objects.filter(reason=ORIGIN, status="processing", claim_until__lte=clock,
        provider_message_id="").values_list("pk", flat=True)[:bound]), status="processing", provider_message_id="").update(
        status="ambiguous", skip_reason="processing_crash_no_retry", claim_token="", claim_until=None)
    ids = list(IgFollowUpTask.objects.filter(reason=ORIGIN, status="pending", due_at__lte=clock)
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=clock)).order_by("due_at", "pk").values_list("pk", flat=True)[:bound])
    sent = 0
    for identity in ids:
        candidate = IgFollowUpTask.objects.filter(pk=identity).first()
        if not candidate:
            continue
        client, lease = bot.acquire_client_automation_lease(candidate.client_id)
        if not client:
            continue
        token = uuid.uuid4().hex
        try:
            with transaction.atomic():
                task = IgFollowUpTask.objects.select_for_update().get(pk=identity)
                if task.status != "pending" or task.provider_message_id or task.attempt_count:
                    continue
                guard, reason = _guard(task, settings_obj, now=fixed_clock or timezone.now())
                if reason:
                    _defer(task, reason, fixed_clock or timezone.now())
                    continue
                projection, text = guard
                quick_replies = ()
                # A prior known-not-dispatched attempt owns its frozen copy.
                # Add a new offer only before that immutable copy is sealed.
                if CUSTOMER_COPY_KEY not in (task.manager_context or {}) or restock_followup.TASK_KEY in (task.manager_context or {}):
                    offer = restock_followup.prepare_task_offer(task, projection, settings_obj, text=text,
                        now=fixed_clock or timezone.now())
                    if offer["ready"]:
                        text, quick_replies = offer["body"], offer["quick_replies"]
                # Copy only the finite signed admission proof into this attempt.
                proof = json.loads(json.dumps(projection["proof"]))
                if CUSTOMER_COPY_KEY not in (task.manager_context or {}):
                    _freeze_task_update(task, projection, text)
                task.status, task.claim_token, task.claim_until = "processing", token, (fixed_clock or timezone.now()) + timedelta(minutes=3)
                task.attempt_count, task.message_text = 1, text
                task.save(update_fields=["status", "claim_token", "claim_until", "attempt_count", "message_text", "manager_context", "updated_at"])
            permission = capture_reply_permission(settings_obj.pk, client.pk)
            admission, receipt = {"admitted": False}, None
            if bot.renew_client_automation_lease(client.pk, lease):
                try:
                    receipt = bot.send_text(settings_obj, client.igsid, text, quick_replies=quick_replies, return_receipt=True, allow_url_fallback=False,
                        provider_message_callback=lambda mid: admission["admitted"] and _checkpoint(identity, token, mid, text, now=fixed_clock),
                        permission_boundary_factory=lambda: customer_send_boundary(settings_obj.pk, client.pk, permission),
                        provider_request_boundary_factory=lambda **state: _physical_boundary(identity, token, settings_obj, text, proof,
                            quick_replies=quick_replies, now=fixed_clock, admission=admission, **state))
                except Exception:
                    pass
            sent += int(_finish(identity, token, receipt, admitted=admission["admitted"], now=fixed_clock))
        finally:
            bot.release_client_automation_lease(client.pk, lease)
    return sent
