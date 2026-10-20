"""Physical ALL-list checks using the existing immutable fulfilment case."""
from copy import deepcopy
import re

from django.db import transaction

from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_restock_waiting_list as waiting

SCOPES = {"restock_list_invitation", "restock_list"}


def _lines(target, *, now, baseline=None):
    lines = []
    for item in target["items"]:
        observed = waiting.observe_item(item, now=now)
        original = baseline[item["line_id"]] if baseline is not None else observed["configuration_digest"]
        if not original:
            raise ValueError("configuration_unverified")
        lines.append({"line_id": item["line_id"], "recipient_id": item["recipient_id"], "label": waiting.item_label(item),
            "target": deepcopy(item), "observation": observed, "configuration_digest": original})
    return lines


def create_invitation_review(revision, source, proof, target, settings_obj, *, now, expected_configuration, locale):
    from management.models import IgClient
    with transaction.atomic():
        client = IgClient.objects.select_for_update().get(pk=revision.client_id)
        if client.hidden_at or client.privacy_erasure_started_at:
            raise ValueError("waiting_list_owner_unavailable")
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_assisted_commerce import _configuration_capture
        cart = capture_current_selection_lines(client.pk, now=now)
        if (cart.get("status") != "captured" or not cart.get("coverage_complete")
            or _configuration_capture(cart) != expected_configuration):
            raise ValueError("waiting_list_source_cart_unavailable")
        waiting.verify_item_sources(target, client, proof["namespace"])
        lines = _lines(target, now=now)
        # Capture the configuration first, then resolve the actual owned wishes
        # again. A same-PK catalog color edit between the earlier target capture
        # and this baseline must not pin the new color under the old card label.
        second_target, second_cart = waiting.capture_target(client, source, now=now)
        if second_target != target or _configuration_capture(second_cart) != expected_configuration:
            raise ValueError("waiting_list_source_cart_unavailable")
        second_lines = _lines(target, now=now)
        if ({row["line_id"]: row["configuration_digest"] for row in lines}
            != {row["line_id"]: row["configuration_digest"] for row in second_lines}):
            raise ValueError("configuration_changed")
        payload = {"scope": "restock_list_invitation", "waiting_list": deepcopy(target), "target_digest": waiting._digest(target),
            "client_id": client.pk, "locale": locale,
            "source_id": source.pk, "source_namespace": proof["namespace"], "sources": [proof], "source_refs": [
                {"message_id": source.pk, "event_at": proof["provider_created_at"], "digest": proof["digest"]}],
            "settings_id": settings_obj.pk, "settings_epoch": settings_obj.reply_permission_epoch, "client_epoch": client.reply_permission_epoch,
            "customer_requirements": [], "waiting_list_configuration": _configuration_capture(cart), "lines": lines}
        request, _created = reviews._create_request(client, payload, source=source, revision=revision, now=now)
        if request is None:
            raise ValueError("waiting_list_manager_case_missing")
        return {"schema": waiting.SCHEMA, "request_id": str(request.pk), "request_hmac": request.envelope_hmac,
            "task_id": request.task_id, "client_id": client.pk, "target_digest": waiting._digest(target)}


def _invitation_review(inv):
    if inv.envelope.get("producer") == "manager_followup.v1":
        from management.services.ig_restock_followup import invitation_case
        case = invitation_case(inv)
        return case[0] if case else None
    try:
        frozen = inv.envelope.get("manager_review") or {}
        request = reviews.Request.objects.select_related("task").filter(pk=frozen.get("request_id"), client_id=inv.client_id).first()
        data = request.envelope if request else {}
        payload = deepcopy(data)
        for key in ("version", "purpose", "columns"):
            payload.pop(key, None)
        for line in payload.get("lines", []):
            line["observation"].pop("captured_at", None)
            line["observation"].pop("digest", None)
        expected_event_key = "ig-fulfillment-review:" + reviews._digest({"client": inv.client_id, "payload": payload})
        valid = bool(reviews._verified(request, "request") and data.get("scope") == "restock_list_invitation"
            and request.source_id == inv.request_source_id and request.revision_id == inv.revision_id
            and data.get("waiting_list") == inv.target and data.get("target_digest") == inv.target_digest
            and inv.envelope.get("source") in data.get("sources", [])
            and request.task.client_id == inv.client_id and request.task.reason == "fulfillment_review"
            and request.task.kind == "manager_task" and request.task.policy_version == reviews.PURPOSE
            and request.task.trigger == "event" and request.task.event_payload == {"origin": reviews.PURPOSE}
            and request.task.event_key == expected_event_key
            and frozen == {"schema": waiting.SCHEMA, "request_id": str(request.pk), "request_hmac": request.envelope_hmac,
                "task_id": request.task_id, "client_id": request.client_id, "target_digest": inv.target_digest}
            and [{"line_id": row["line_id"], "target": row["target"]} for row in data["lines"]] == [
                {"line_id": row["line_id"], "target": row} for row in inv.target["items"]])
        return request if valid else None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def invitation_review_valid(inv):
    return bool(_invitation_review(inv))


def _original_baseline(inv):
    if inv.envelope.get("producer") == "manager_followup.v1":
        from management.services.ig_restock_followup import original_baseline
        return original_baseline(inv)
    restock = waiting._restock()
    original = _invitation_review(inv) if restock._verified(inv, "invitation") and waiting._digest(inv.target) == inv.target_digest else None
    if original is None:
        raise ValueError("waiting_list_configuration_ancestry_changed")
    baseline = {row["line_id"]: row["configuration_digest"] for row in original.envelope["lines"]}
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in baseline.values()):
        raise ValueError("configuration_unverified")
    return baseline


def _request_baseline(request):
    if not reviews._verified(request, "request"):
        raise ValueError("request_evidence_invalid")
    if request.envelope["scope"] == "restock_list_invitation":
        baseline = {row["line_id"]: row["configuration_digest"] for row in request.envelope["lines"]}
    else:
        subscription = _subscription_for_review(request)
        if (not subscription or subscription.client_id != request.client_id
            or subscription.accepted_answer.envelope_hmac != request.envelope.get("answer_hmac")):
            raise ValueError("waiting_list_configuration_ancestry_changed")
        inv = subscription.accepted_answer.invitation
        baseline = _original_baseline(inv)
        if (request.envelope.get("waiting_list") != inv.target or request.envelope.get("target_digest") != inv.target_digest
            or {row["line_id"]: row["configuration_digest"] for row in request.envelope["lines"]} != baseline
            or [{"line_id": row["line_id"], "target": row["target"]} for row in request.envelope["lines"]]
                != [{"line_id": row["line_id"], "target": row} for row in inv.target["items"]]):
            raise ValueError("waiting_list_configuration_ancestry_changed")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in baseline.values()):
        raise ValueError("configuration_unverified")
    return baseline


def configuration_reason(inv, *, now):
    """Current configuration must remain the one accepted, never a renewal's."""
    try:
        baseline = _original_baseline(inv)
        for item in inv.target["items"]:
            if waiting.observe_item(item, now=now)["configuration_digest"] != baseline[item["line_id"]]:
                return "configuration_changed"
        return ""
    except (ValueError, KeyError, TypeError, AttributeError):
        return "waiting_list_configuration_ancestry_changed"


def _rows(inv, subscription):
    initial = inv.envelope["manager_review"]["request_id"]
    from django.db.models import Q
    owned = Q(pk=initial)
    if subscription:
        owned |= Q(envelope__subscription_id=str(subscription.pk))
    candidates = reviews.Request.objects.filter(owned, client_id=inv.client_id,
        envelope__target_digest=inv.target_digest).select_related("client", "task").order_by("-created_at", "-pk")[:1]
    return [row for row in candidates if reviews._verified(row, "request") and row.envelope.get("scope") in SCOPES
        and row.envelope.get("waiting_list") == inv.target and row.envelope.get("target_digest") == inv.target_digest
        and (str(row.pk) == initial or subscription and row.envelope.get("subscription_id") == str(subscription.pk))]


def _subscription_for_review(request):
    restock = waiting._restock()
    if request.envelope["scope"] == "restock_list":
        subscription = restock.Subscription.objects.filter(pk=request.envelope.get("subscription_id")).select_related(
            "accepted_answer__invitation", "accepted_answer__receipt__effect", "accepted_answer__receipt__followup_task", "accepted_answer__source").first()
    else:
        subscription = restock.Subscription.objects.filter(accepted_answer__invitation__envelope__manager_review__request_id=str(request.pk)).select_related(
            "accepted_answer__invitation", "accepted_answer__receipt__effect", "accepted_answer__receipt__followup_task", "accepted_answer__source").first()
    return subscription


def current_review_reason(request, settings_obj, *, now):
    restock = waiting._restock()
    data = request.envelope
    try:
        waiting.validate_target(data["waiting_list"])
        if data["target_digest"] != waiting._digest(data["waiting_list"]):
            return "waiting_list_review_target_changed"
        waiting.verify_item_sources(data["waiting_list"], request.client, data["source_namespace"])
        subscription = _subscription_for_review(request)
        if subscription:
            if (subscription.client_id != request.client_id or subscription.accepted_answer.invitation.target != data["waiting_list"]
                or data["scope"] == "restock_list" and subscription.accepted_answer.envelope_hmac != data.get("answer_hmac")):
                return "waiting_list_subscription_binding_changed"
            return restock._subscription_reason(subscription, settings_obj, now=now)
        if data["scope"] != "restock_list_invitation" or request.source_id is None or request.revision_id is None:
            return "waiting_list_subscription_missing"
        owned, reason = restock.owned_source(request.revision, request.source, settings_obj, require_head=False)
        if not owned or owned[2] not in data["sources"]:
            return reason or "waiting_list_source_changed"
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_assisted_commerce import _configuration_capture
        cart = capture_current_selection_lines(request.client_id, now=now)
        if cart.get("status") != "captured" or _configuration_capture(cart) != data["waiting_list_configuration"]:
            return "waiting_list_selection_changed"
        return ""
    except (ValueError, KeyError, TypeError, AttributeError):
        return "waiting_list_review_unverified"


def normalize_decisions(value, request, now):
    if isinstance(value, dict):
        if set(value) != {"lines"}:
            raise ValueError("waiting_list_split_not_subscription")
        value = value["lines"]
    expected = {row["line_id"]: row for row in request.envelope["lines"]}
    baseline = _request_baseline(request)
    if (not isinstance(value, list) or any(not isinstance(row, dict) for row in value) or len(value) != len(expected)
        or {row.get("line_id") for row in value} != set(expected)):
        raise ValueError("decision_line_coverage_invalid")
    allowed = {"line_id", "availability", "checked_quantity", "preparation", "dispatch", "alternative", "clarification", "supply_basis", "source_location"}
    clean = []
    for row in value:
        item = expected[row["line_id"]]["target"]
        if set(row) - allowed or row.get("availability") not in {"available", "unavailable", "wait", "clarify"}:
            raise ValueError("decision_schema_invalid")
        observation = waiting.observe_item(item, now=now)
        if observation["configuration_digest"] != baseline[row["line_id"]]:
            raise ValueError("configuration_changed")
        checked, basis = row.get("checked_quantity"), row.get("supply_basis")
        if checked is not None and (type(checked) is not int or not 0 <= checked <= 999):
            raise ValueError("checked_quantity_invalid")
        if basis is not None and basis not in {"warehouse_checked", "accessible_unrecorded", "incoming_confirmed"}:
            raise ValueError("supply_basis_invalid")
        if row["availability"] == "available" and (not observation["configuration_digest"]
            or type(checked) is not int or checked < item["quantity"] or basis not in {"warehouse_checked", "accessible_unrecorded"}):
            raise ValueError("physical_availability_unverified")
        notes = {}
        for key in ("source_location", "clarification", "alternative"):
            value = row.get(key, "")
            if not isinstance(value, str) or len(value) > 350 or re.search(r"[\x00-\x1f\x7f]", value):
                raise ValueError("decision_note_invalid")
            notes[key] = value
        clean.append({"line_id": item["line_id"], "item_kind": item["item_kind"], "availability": row["availability"],
            "checked_quantity": checked, "supply_basis": basis, "configuration_digest": observation["configuration_digest"],
            "accounting_recount_required": item["item_kind"] == "catalog_apparel" and row["availability"] == "available" and observation["status"] != "available",
            "preparation": reviews._timing(row.get("preparation"), now), "dispatch": reviews._timing(row.get("dispatch"), now),
            "inventory_digest": reviews.inventory_digest(observation), **notes})
    return clean, None


def decision_reason(request, decision, *, now):
    if not reviews._verified(decision, "decision") or decision.envelope.get("request_digest") != request.envelope_hmac:
        return "decision_evidence_invalid"
    if decision.created_at > now or now - decision.created_at > reviews.FRESHNESS:
        return "manager_review_expired"
    from django.contrib.auth import get_user_model
    from management.bot_access import has_all_bot_capabilities, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
    # Re-read the approving principal: a signed old decision and a cached
    # permission set cannot survive deactivation, reviewer exclusion or a
    # capability revocation at the later queue/physical-send boundary.
    actor = get_user_model().objects.filter(pk=decision.actor_id).first()
    if not actor or not actor.is_active or not has_all_bot_capabilities(actor,
        OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION):
        return "manager_actor_forbidden"
    by_id = {row["line_id"]: row for row in request.envelope["lines"]}
    try:
        baseline = _request_baseline(request)
    except (ValueError, KeyError, TypeError, AttributeError):
        return "waiting_list_configuration_ancestry_changed"
    rows = decision.envelope.get("lines", [])
    if len(rows) != len(by_id) or {row.get("line_id") for row in rows} != set(by_id):
        return "decision_line_coverage_invalid"
    for row in rows:
        fresh = waiting.observe_item(by_id[row["line_id"]]["target"], now=now)
        if (not fresh["configuration_digest"] or fresh["configuration_digest"] != row.get("configuration_digest")
            or row.get("configuration_digest") != baseline[row["line_id"]]):
            return "configuration_changed"
    return ""


def ensure_review(subscription, availability, *, now):
    restock = waiting._restock()
    inv = subscription.accepted_answer.invitation
    settings_obj = reviews._settings()
    if not settings_obj or restock._subscription_reason(subscription, settings_obj, now=now):
        return {"created": False, "reason": "subscription_changed"}
    try:
        baseline = _original_baseline(inv)
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"created": False, "reason": "waiting_list_configuration_ancestry_changed"}
    candidates = _rows(inv, subscription)
    generation = "initial"
    if candidates:
        request = candidates[0]
        decision = reviews.Decision.objects.filter(request=request).order_by("-version").first()
        if not decision or not decision_reason(request, decision, now=now):
            return {"created": False, "request_id": str(request.pk), "request_digest": request.envelope_hmac, "task_id": request.task_id}
        generation = str(decision.pk)
    with transaction.atomic():
        from management.models import IgClient
        client = IgClient.objects.select_for_update().get(pk=subscription.client_id)
        payload = {"scope": "restock_list", "waiting_list": deepcopy(inv.target), "target_digest": inv.target_digest,
            "subscription_id": str(subscription.pk), "answer_hmac": subscription.accepted_answer.envelope_hmac, "review_generation": generation,
            "client_id": client.pk, "locale": inv.locale, "source_id": inv.request_source_id,
            "source_namespace": inv.envelope["source"]["namespace"], "sources": [inv.envelope["source"]],
            "settings_id": settings_obj.pk, "settings_epoch": settings_obj.reply_permission_epoch, "client_epoch": client.reply_permission_epoch,
            "customer_requirements": [], "lines": _lines(inv.target, now=now, baseline=baseline)}
        request, created = reviews._create_request(client, payload, now=now)
        return {"created": created, "request_id": str(request.pk), "request_digest": request.envelope_hmac, "task_id": request.task_id}


def manager_admission(subscription, availability, *, now):
    restock = waiting._restock()
    inv = subscription.accepted_answer.invitation
    settings_obj = reviews._settings()
    if (not settings_obj or availability.get("target") != inv.target or not invitation_review_valid(inv)
        or restock._subscription_reason(subscription, settings_obj, now=now)):
        return {"ready": False, "reason": "waiting_list_subscription_changed", "proof": {}}
    rows = _rows(inv, subscription)
    if not rows:
        return {"ready": False, "reason": "manager_review_pending", "proof": {}}
    request = rows[0]
    reason = reviews._current_reason(request, settings_obj, now=now)
    decision = reviews.Decision.objects.filter(request=request).order_by("-version").first()
    if not reason and decision:
        reason = decision_reason(request, decision, now=now)
    if reason or not decision:
        return {"ready": False, "reason": reason or "manager_review_pending", "proof": {}}
    expected = {row["line_id"]: row for row in inv.target["items"]}
    for line in decision.envelope["lines"]:
        if (line["availability"] != "available" or type(line.get("checked_quantity")) is not int
            or line["checked_quantity"] < expected[line["line_id"]]["quantity"]
            or line.get("supply_basis") not in {"warehouse_checked", "accessible_unrecorded"}):
            return {"ready": False, "reason": "manager_review_unavailable", "proof": {}}
    return {"ready": True, "reason": "", "proof": {"version": 2, "completion": "all", "target_digest": inv.target_digest,
        "request_id": str(request.pk), "request_digest": request.envelope_hmac, "decision_id": str(decision.pk),
        "decision_version": decision.version, "decision_digest": decision.envelope_hmac,
        "subscription_id": str(subscription.pk), "items": deepcopy(decision.envelope["lines"])}}


def projection(inv, subscription, *, now):
    base = {"ready": False, "request_id": None, "request_digest": None, "state": "unverified", "checked_at": None, "all_ready": False, "lines": []}
    if not invitation_review_valid(inv):
        return base
    rows = _rows(inv, subscription)
    if not rows:
        return base
    request = rows[0]
    settings_obj = reviews._settings()
    reason = reviews._current_reason(request, settings_obj, now=now) if settings_obj else "settings_missing"
    # A terminal subscription cannot authorize another manager action or send.
    # Its verified physical-check facts remain readable as historical evidence.
    historical = bool(subscription and reason in {"subscription_sent", "subscription_unknown", "customer_revoked", "expired"}
        and waiting._restock()._valid_answer(subscription.accepted_answer, settings_obj)
        and not waiting.invitation_reason(inv, settings_obj, now=now))
    if reason and not historical:
        return base
    base.update(ready=not historical, request_id=str(request.pk), request_digest=request.envelope_hmac, state="pending")
    decision = reviews.Decision.objects.filter(request=request).order_by("-version").first()
    if decision:
        reason = decision_reason(request, decision, now=now)
        if reason:
            return {**base, "ready": False, "state": "expired" if reason == "manager_review_expired" else "unverified"}
        at = decision.created_at.isoformat()
        base.update(state="reviewed", checked_at=at, lines=[{key: row.get(key) for key in ("line_id", "availability", "checked_quantity", "supply_basis")} | {"checked_at": at} for row in decision.envelope["lines"]])
        expected = {row["line_id"]: row for row in inv.target["items"]}
        base["all_ready"] = all(row["availability"] == "available" and type(row.get("checked_quantity")) is int
            and row["checked_quantity"] >= expected[row["line_id"]]["quantity"] for row in decision.envelope["lines"])
    return base
