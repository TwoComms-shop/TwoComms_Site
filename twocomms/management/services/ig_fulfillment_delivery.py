"""One native customer update for a signed, current manager cart decision.

The existing FollowUpTask is this purpose's durable delivery effect. A native
MID is committed before conversation history; uncertain outcomes never replay.
"""
from contextlib import contextmanager
from datetime import timedelta
import json
import re
import uuid
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from management.services.ig_payment_reminder_time import quiet_time_allowed
from management.services.ig_purpose_sources import latest_customer_source

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
        "wait": "очікуємо", "clarify": "потрібне уточнення", "preparation": "підготовка", "dispatch": "відправка",
        "calendar": "календ. дн.", "business": "роб. дн.", "from": "від", "number": "поз.", "same_day": "у день перевірки",
        "arrival": "Це строки підготовки/відправки, не гарантія дати прибуття.",
        "deadline": "Потрібну вам дату ще слід звірити з доставкою.",
        "split": "Хочете обговорити окремі відправлення? Умови доставки погодимо до оформлення.",
        "clarification": "Уточнімо деталі перед оформленням.", "alternative": "Заміни погодимо окремо."},
    "ru": {"intro": "Менеджер проверил позиции:", "available": "в наличии", "unavailable": "нет в наличии",
        "wait": "ожидаем", "clarify": "нужно уточнение", "preparation": "подготовка", "dispatch": "отправка",
        "calendar": "календ. дн.", "business": "раб. дн.", "from": "от", "number": "поз.", "same_day": "в день проверки",
        "arrival": "Это сроки подготовки/отправки, не гарантия даты прибытия.",
        "deadline": "Нужную вам дату ещё следует сверить с доставкой.",
        "split": "Хотите обсудить отдельные отправления? Условия доставки согласуем до оформления.",
        "clarification": "Уточним детали перед оформлением.", "alternative": "Замены согласуем отдельно."},
    "en": {"intro": "Our manager checked the items:", "available": "available", "unavailable": "unavailable",
        "wait": "awaiting availability", "clarify": "clarification needed", "preparation": "preparation", "dispatch": "dispatch",
        "calendar": "calendar days", "business": "business days", "from": "from", "number": "item", "same_day": "on the check date",
        "arrival": "Preparation/dispatch timings do not guarantee an arrival date.",
        "deadline": "Your required date still needs to be checked against delivery.",
        "split": "Would you like to discuss separate shipments? We will agree delivery terms before ordering.",
        "clarification": "Let us clarify the details before ordering.", "alternative": "We will agree any substitutions separately."},
}


def _timing(value, phase, copy):
    if value is None:
        return ""
    if not isinstance(value, dict):
        raise ValueError("invalid_timing")
    low, high, unit = value.get("min_days"), value.get("max_days"), value.get("unit")
    anchor = parse_datetime(str(value.get("anchor_at") or ""))
    if (type(low) is not int or type(high) is not int or not 0 <= low <= high <= 365
            or unit not in {"calendar", "business"} or not anchor or timezone.is_naive(anchor)):
        raise ValueError("invalid_timing")
    if low == high == 0:
        return f"{copy[phase]} {copy['same_day']} ({anchor.astimezone(KYIV):%d.%m.%Y})"
    days = str(low) if low == high else f"{low}–{high}"
    return f"{copy[phase]} {days} {copy[unit]} {copy['from']} {anchor.astimezone(KYIV):%d.%m.%Y}"


def render_manager_update(projection):
    """Finite typed facts only; numbering binds compressed groups to cart order."""
    request, decision = projection.get("current_request") or {}, projection.get("decision") or {}
    locale = request.get("locale") or "uk"
    copy = COPY.get(locale, COPY["uk"])
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
            for phase in ("preparation", "dispatch"):
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
        requirements = request.get("customer_requirements") or []
        has_deadline = any(isinstance(hint, dict) and hint.get("date_status") in {"resolved", "past", "needs_clarification"}
                and hint.get("supersession_hint") != "withdrawn" for hint in requirements)
        if has_timing or has_deadline:
            suffix.append(copy["arrival"])
        if has_deadline:
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


def _guard(task, settings_obj, *, now):
    from management.models import IgClient
    if not task or task.reason != ORIGIN or task.trigger != "event":
        return None, "task_binding_changed"
    projection = _core().validated_update_task(task, settings_obj, now=now)
    if not isinstance(projection, dict) or not projection.get("ready"):
        return None, (projection.get("reason") if isinstance(projection, dict) else "") or "manager_decision_unverified"
    request = projection.get("current_request") or {}
    if request.get("scope") != "cart" or request.get("client_id") != task.client_id or not projection.get("proof"):
        return None, "task_binding_changed"
    from management.services.ig_fulfillment_notification_coordination import cart_update_coordination
    coordinated = cart_update_coordination(task, projection, settings_obj, now=now)
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
    text = render_manager_update(projection)
    if not text:
        return None, "summary_requires_manager_clarification"
    return (projection, text), ""


def _defer(task, reason, now):
    task.status = "pending" if reason in HOLD_REASONS else "cancelled"
    task.skip_reason = reason
    task.claim_token, task.claim_until, task.attempt_count = "", None, 0
    task.next_attempt_at = now + CHECK_INTERVAL if reason in HOLD_REASONS else None
    task.save(update_fields=["status", "skip_reason", "claim_token", "claim_until", "attempt_count", "next_attempt_at", "updated_at"])


@contextmanager
def _physical_boundary(task_id, token, settings_obj, text, proof, *, now=None, admission=None, **delivery):
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
            binding = _core().validated_update_history(task)
            if not isinstance(binding, dict) or not binding.get("ready"):
                return False
            if render_manager_update(binding) != task.message_text:
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
    for identity in IgFollowUpTask.objects.filter(reason=ORIGIN, status="sent", sent_message__isnull=True).exclude(provider_message_id="").values_list("pk", flat=True)[:bound]:
        _history(identity)
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
                # Copy only the finite signed admission proof into this attempt.
                proof = json.loads(json.dumps(projection["proof"]))
                task.status, task.claim_token, task.claim_until = "processing", token, (fixed_clock or timezone.now()) + timedelta(minutes=3)
                task.attempt_count, task.message_text = 1, text
                task.save(update_fields=["status", "claim_token", "claim_until", "attempt_count", "message_text", "updated_at"])
            permission = capture_reply_permission(settings_obj.pk, client.pk)
            admission, receipt = {"admitted": False}, None
            if bot.renew_client_automation_lease(client.pk, lease):
                try:
                    receipt = bot.send_text(settings_obj, client.igsid, text, return_receipt=True, allow_url_fallback=False,
                        provider_message_callback=lambda mid: admission["admitted"] and _checkpoint(identity, token, mid, text, now=fixed_clock),
                        permission_boundary_factory=lambda: customer_send_boundary(settings_obj.pk, client.pk, permission),
                        provider_request_boundary_factory=lambda **state: _physical_boundary(identity, token, settings_obj, text, proof,
                            now=fixed_clock, admission=admission, **state))
                except Exception:
                    pass
            sent += int(_finish(identity, token, receipt, admitted=admission["admitted"], now=fixed_clock))
        finally:
            bot.release_client_automation_lease(client.pk, lease)
    return sent
