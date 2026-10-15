"""Read-only coordination of one availability fact across two native purposes.

Neither a mutable subscription flag nor a manager approval is a send receipt.
Callers serialize dispatch with the existing client automation lease and apply
these guards again immediately before each physical request. Delegation retains
the original purpose's native MID; it never manufactures a restock receipt.
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from management.models import IgFollowUpTask
from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest, IgFulfillmentReviewDecision
from management.ig_restock_subscription_models import IgRestockSubscription, IgRestockAvailabilityEvent
from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_restock_subscriptions as restock
from management.services.ig_delivery_receipts import normalize_provider_message_id

HOLD = "other_availability_send_in_progress"
CORRUPT = "availability_coordination_unverified"
FULFILLED = "fulfilled_by_manager_update"
OWNED = "restock_receipt_owns_update"


def _result(reason="", receipt=None):
    result = {"reason": reason}
    if receipt is not None:
        result["delegated_receipt"] = receipt
    return result


def _one_available(request, decision):
    lines = request.envelope.get("lines")
    decided = decision.envelope.get("lines")
    if not isinstance(lines, list) or len(lines) != 1 or not isinstance(decided, list) or len(decided) != 1:
        return None
    line, answer = lines[0], decided[0]
    if (not isinstance(line, dict) or not isinstance(answer, dict) or answer.get("line_id") != line.get("line_id")
        or answer.get("availability") != "available" or not isinstance(line.get("target"), dict)
        or not line["target"] or type(answer.get("checked_quantity")) is not int
        or answer["checked_quantity"] < line["target"].get("quantity", 1000)):
        return None
    return line["target"]


def _cart_binding(task):
    """Historical signed task binding; no fresh projection or provider action."""
    if not task or task.reason != reviews.UPDATE_REASON or task.kind != "fulfillment" or task.trigger != "event":
        return None
    data = task.event_payload or {}
    try:
        request = IgFulfillmentReviewRequest.objects.filter(pk=data.get("request_id"), client_id=task.client_id).first()
        decision = IgFulfillmentReviewDecision.objects.filter(pk=data.get("decision_id"), request=request).first() if request else None
    except (ValidationError, ValueError, TypeError):
        return None
    if (not reviews._verified(request, "request") or not reviews._verified(decision, "decision")
        or request.envelope.get("scope") != "cart" or decision.envelope.get("request_digest") != request.envelope_hmac
        or data != {"origin": reviews.UPDATE_REASON, "request_id": str(request.pk), "request_digest": request.envelope_hmac,
            "decision_id": str(decision.pk), "decision_version": decision.version, "decision_digest": decision.envelope_hmac}
        or task.policy_version != reviews.PURPOSE or task.event_key != f"ig-fulfillment-update:{request.pk}:{decision.version}"):
        return None
    return request, decision


def _invited_available_target(request, decision, invitation):
    """Match one subscribed line, without borrowing another cart line's answer."""
    line_id = (invitation.envelope.get("manager_review") or {}).get("line_id")
    lines = [line for line in request.envelope.get("lines", ()) if isinstance(line, dict)
        and line.get("line_id") == line_id and line.get("target") == invitation.target]
    answers = [line for line in decision.envelope.get("lines", ()) if isinstance(line, dict)
        and line.get("line_id") == line_id]
    if len(lines) != 1 or len(answers) != 1:
        return None
    answer = answers[0]
    if (answer.get("availability") != "available" or type(answer.get("checked_quantity")) is not int
        or answer["checked_quantity"] < invitation.target.get("quantity", 1000)
        or answer.get("supply_basis") not in {"warehouse_checked", "accessible_unrecorded"}):
        return None
    return invitation.target


def _fresh_decision(request, decision, now, *, invitation=None):
    decided = parse_datetime(str(decision.envelope.get("decided_at") or ""))
    latest = IgFulfillmentReviewDecision.objects.filter(request=request).order_by("-version").values_list("pk", flat=True).first()
    return bool(decided and timezone.is_aware(decided) and decided <= now + timedelta(minutes=1)
        and now < decided + reviews.FRESHNESS and latest == decision.pk
        and decision.actor.is_active and (_invited_available_target(request, decision, invitation)
            if invitation else _one_available(request, decision)))


def _accepted(subscription, request, target, settings_obj):
    answer, inv = subscription.accepted_answer, subscription.accepted_answer.invitation
    binding = inv.envelope.get("manager_review") or {}
    return bool(subscription.client_id == request.client_id == inv.client_id
        and subscription.expires_at == inv.expires_at and answer.kind == "accept"
        and restock._valid_answer(answer, settings_obj) and restock._invitation_manager_case(inv)
        and binding.get("request_id") == str(request.pk) and binding.get("request_hmac") == request.envelope_hmac
        and inv.request_source_id == request.source_id and inv.revision_id == request.revision_id
        and inv.target == target and inv.target_digest == restock._digest(target))


def _subscriptions(request):
    return list(IgRestockSubscription.objects.filter(client_id=request.client_id,
        accepted_answer__invitation__envelope__manager_review__request_id=str(request.pk))
        .select_related("accepted_answer__invitation__client", "accepted_answer__invitation__revision",
            "accepted_answer__invitation__request_source", "accepted_answer__receipt__effect", "accepted_answer__source")[:2])


def _task_state(task):
    if task.status in {"processing", "ambiguous", "unknown"}:
        return "hold"
    if task.status == "sent":
        return "sent" if task.attempt_count == 1 and normalize_provider_message_id(task.provider_message_id) and task.sent_at else "corrupt"
    if task.provider_message_id or (task.status == "pending" and task.attempt_count):
        return "corrupt"
    return "pending" if task.status == "pending" else "inactive"


def _history_matches(task, inv):
    """A missing CRM projection does not erase a durable native checkpoint."""
    if not task.sent_message_id:
        return True
    message = task.sent_message
    return bool(message.client_id == task.client_id and message.sender_id == inv.envelope["recipient"]
        and message.role == "model" and message.source == "followup" and message.status == "done"
        and message.text == task.message_text and message.provider_message_id == task.provider_message_id
        and message.provider_namespace == inv.envelope["source"]["namespace"])


def _after_accept(task, subscription):
    answer = subscription.accepted_answer
    return bool(timezone.is_aware(task.sent_at) and task.sent_at >= max(answer.answered_at, answer.created_at))


def _cart_native_receipt(task, subscription, binding):
    from management.services.ig_fulfillment_delivery import render_task_update
    request, decision = binding
    historical = reviews.validated_update_history(task)
    inv = subscription.accepted_answer.invitation
    if not _invited_available_target(request, decision, inv):
        return None
    text = render_task_update(task, historical) if historical.get("ready") else ""
    if not text or text != task.message_text or not _history_matches(task, inv):
        return None
    return {"purpose": reviews.UPDATE_REASON, "task_id": task.pk,
        "provider_message_id": task.provider_message_id, "sent_at": task.sent_at.isoformat(),
        "sent_message_id": task.sent_message_id, "request_id": str(request.pk), "request_hmac": request.envelope_hmac,
        "decision_id": str(decision.pk), "decision_hmac": decision.envelope_hmac,
        "subscription_id": str(subscription.pk), "accepted_answer_id": subscription.accepted_answer_id,
        "target_digest": inv.target_digest, "render_digest": restock._digest({"text": text})}


def _restock_binding(event, subscription):
    if not event or not restock._verified(event, "event") or event.subscription_id != subscription.pk:
        return None
    task = IgFollowUpTask.objects.select_related("sent_message").filter(pk=event.task_id).first()
    if not task:
        return None
    inv = subscription.accepted_answer.invitation
    proof = event.envelope.get("manager_decision") or {}
    observation = event.envelope.get("availability") or {}
    if (task.client_id != subscription.client_id or task.reason != restock.ORIGIN or task.kind != "qualification"
        or task.trigger != "event" or task.policy_version != "restock-agreement.v1"
        or task.event_key != event.event_key or event.event_key != f"{restock.ORIGIN}:{subscription.pk}"
        or task.event_payload != {"origin": restock.ORIGIN, "purpose": restock.PURPOSE,
            "subscription_id": str(subscription.pk), "event_id": str(event.pk)}
        or event.envelope.get("answer_hmac") != subscription.accepted_answer.envelope_hmac
        or event.envelope.get("target_digest") != inv.target_digest or observation.get("target") != inv.target
        or observation.get("digest") != event.availability_digest):
        return None
    try:
        request = IgFulfillmentReviewRequest.objects.filter(pk=proof.get("request_id"), client_id=subscription.client_id).first()
        decision = IgFulfillmentReviewDecision.objects.filter(pk=proof.get("decision_id"), request=request).first() if request else None
    except (ValidationError, ValueError, TypeError):
        return None
    if (not reviews._verified(request, "request") or not reviews._verified(decision, "decision")
        or proof.get("request_digest") != request.envelope_hmac or proof.get("decision_digest") != decision.envelope_hmac
        or proof.get("decision_version") != decision.version or decision.envelope.get("request_digest") != request.envelope_hmac
        or proof.get("subscription_id") != str(subscription.pk) or proof.get("target") != inv.target):
        return None
    if request.envelope.get("scope") == "cart":
        # This is historical event provenance, not fresh send admission. Keep
        # the actual CART owner and native subscription proof; never manufacture
        # a standing request or erase an already delivered MID on expiry.
        answer = subscription.accepted_answer
        binding = inv.envelope.get("manager_review") or {}
        if (proof.get("version") != 1 or proof.get("review_scope") != "cart"
            or not restock._verified(inv, "invitation") or not restock._verified(answer, "answer")
            or answer.kind != "accept" or answer.invitation_id != inv.pk
            or inv.client_id != subscription.client_id
            or inv.target_digest != restock._digest(inv.target)
            or not restock._invitation_manager_case(inv)
            or binding.get("request_id") != str(request.pk) or binding.get("request_hmac") != request.envelope_hmac
            or proof.get("line_id") != binding.get("line_id")
            or proof.get("invitation_id") != str(inv.pk) or proof.get("invitation_digest") != inv.envelope_hmac
            or proof.get("answer_id") != str(answer.pk) or proof.get("answer_digest") != answer.envelope_hmac):
            return None
        lines = [line for line in request.envelope.get("lines", ())
            if isinstance(line, dict) and line.get("line_id") == proof["line_id"] and line.get("target") == inv.target]
        decisions = [line for line in decision.envelope.get("lines", ())
            if isinstance(line, dict) and line.get("line_id") == proof["line_id"]]
        if len(lines) != 1 or len(decisions) != 1:
            return None
        line = decisions[0]
        if (line.get("availability") != "available" or type(line.get("checked_quantity")) is not int
            or line["checked_quantity"] < inv.target.get("quantity", 1000)
            or line.get("supply_basis") not in {"warehouse_checked", "accessible_unrecorded"}
            or any(proof.get(key) != line.get(key) for key in
                ("inventory_digest", "configuration_digest", "checked_quantity", "supply_basis"))):
            return None
    elif (request.envelope.get("scope") != "restock" or proof.get("review_scope") not in {None, "restock"}
        or request.envelope.get("subscription_id") != str(subscription.pk)
        or request.envelope.get("answer_hmac") != subscription.accepted_answer.envelope_hmac
        or _one_available(request, decision) != inv.target):
        return None
    return task, request, decision


def _additional_facts(request, decision):
    line = decision.envelope["lines"][0]
    return bool(decision.envelope.get("split_offer") or line.get("preparation") or line.get("dispatch")
        or line.get("clarification") or line.get("alternative") or any(
            isinstance(hint, dict) and (hint.get("request_detected") or hint.get("date_status") in
                {"resolved", "past", "needs_clarification"} or hint.get("custom_print_hint") or hint.get("delivery_preference") not in {None, "unknown"})
            for hint in request.envelope.get("customer_requirements", ())))


def cart_update_coordination(task, projection, settings_obj, now=None):
    now = now or timezone.now()
    from management.services.ig_restock_waiting_list_coordination import cart_update_coordination as list_coordination
    list_result = list_coordination(task, now=now)
    if list_result.get("reason"):
        return list_result
    binding = _cart_binding(task)
    if not binding:
        return _result(CORRUPT)
    request, decision = binding
    target = _one_available(request, decision)
    if not target or not _fresh_decision(request, decision, now):
        return _result()
    if (not isinstance(projection, dict) or not projection.get("ready")
        or (projection.get("current_request") or {}).get("id") != str(request.pk)
        or (projection.get("decision") or {}).get("id") != str(decision.pk)):
        return _result(CORRUPT)
    subscriptions = _subscriptions(request)
    if not subscriptions:
        return _result()
    if len(subscriptions) != 1 or not _accepted(subscriptions[0], request, target, settings_obj):
        return _result(CORRUPT)
    subscription = subscriptions[0]
    if subscription.state == "unknown":
        return _result(HOLD)
    event = IgRestockAvailabilityEvent.objects.select_related("task__sent_message").filter(subscription=subscription).first()
    if not event:
        return _result()
    other = _restock_binding(event, subscription)
    if not other:
        return _result(CORRUPT)
    other_task = other[0]
    state = _task_state(other_task)
    if state in {"hold", "corrupt"}:
        return _result(HOLD if state == "hold" else CORRUPT)
    if subscription.state in {"revoked", "expired"}:
        return _result()
    if state != "sent" or not _after_accept(other_task, subscription):
        return _result()
    inv = subscription.accepted_answer.invitation
    expected = restock._copy(inv.locale, "notification", restock._label(inv))
    if other_task.message_text != expected or not _history_matches(other_task, inv):
        return _result(CORRUPT)
    return _result() if _additional_facts(request, decision) else _result(OWNED)


def restock_update_coordination(event, settings_obj, now=None):
    now = now or timezone.now()
    subscription = IgRestockSubscription.objects.select_related("accepted_answer__invitation__client",
        "accepted_answer__invitation__revision", "accepted_answer__invitation__request_source",
        "accepted_answer__receipt__effect", "accepted_answer__source").filter(pk=event.subscription_id).first()
    if not subscription:
        return _result(CORRUPT)
    inv = subscription.accepted_answer.invitation
    frozen = inv.envelope.get("manager_review") or {}
    try:
        request = IgFulfillmentReviewRequest.objects.filter(pk=frozen.get("request_id"), client_id=subscription.client_id).first()
    except (ValidationError, ValueError, TypeError):
        return _result(CORRUPT)
    if not reviews._verified(request, "request") or frozen.get("request_hmac") != request.envelope_hmac:
        return _result(CORRUPT)
    lines = request.envelope.get("lines")
    if request.envelope.get("scope") != "cart" or not isinstance(lines, list):
        return _result()
    if (not _accepted(subscription, request, inv.target, settings_obj)
        or not _restock_binding(event, subscription)):
        return _result(CORRUPT)
    decision = IgFulfillmentReviewDecision.objects.filter(request=request).order_by("-version").select_related("actor").first()
    if not decision:
        return _result()
    if not reviews._verified(decision, "decision"):
        return _result(CORRUPT)
    target = _invited_available_target(request, decision, inv)
    if not target:
        return _result()
    if target != inv.target:
        return _result(CORRUPT)
    task = IgFollowUpTask.objects.select_related("sent_message").filter(event_key=f"ig-fulfillment-update:{request.pk}:{decision.version}").first()
    if not task:
        return _result()
    cart_binding = _cart_binding(task)
    if not cart_binding or cart_binding[0].pk != request.pk or cart_binding[1].pk != decision.pk:
        return _result(CORRUPT)
    state = _task_state(task)
    if state in {"hold", "corrupt"}:
        return _result(HOLD if state == "hold" else CORRUPT)
    if state == "sent":
        receipt = _cart_native_receipt(task, subscription, cart_binding)
        if not receipt:
            return _result(CORRUPT)
        return _result(FULFILLED, receipt) if _after_accept(task, subscription) else _result()
    if state == "pending" and _fresh_decision(request, decision, now, invitation=inv):
        current = reviews.validated_update_task(task, settings_obj, now=now)
        if current.get("ready"):
            return _result(HOLD)
    return _result()
