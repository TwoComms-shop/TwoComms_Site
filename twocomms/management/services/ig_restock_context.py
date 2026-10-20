"""Read-only native restock consent for one signed, current manager cart case."""
from copy import deepcopy
from types import SimpleNamespace
import hmac

from django.utils import timezone

from management.ig_restock_subscription_models import IgRestockSubscription, IgRestockAnswer
from management.services import ig_restock_subscriptions as restock
from management.services import ig_fulfillment_reviews as reviews


def _summary(status, line, *, notification_state="unknown", notification_receipt=None, answer=None, invitation=None, proofs=()):
    return {"status": status, "target": deepcopy(line["target"]), "label": line.get("label", ""),
        "accepted_at": answer.answered_at.isoformat() if answer and status != "unverified" else None,
        "expires_at": invitation.expires_at.isoformat() if invitation and status != "unverified" else None,
        "source_refs": [{"kind": "message", "id": proof["message_id"], "event_at": proof["provider_created_at"],
            "digest": proof["digest"]} for proof in proofs] if status != "unverified" else [],
        "notification_state": notification_state, "notification_receipt": notification_receipt,
        "native_future_capability": "unverified"}


def _binding(request, invitation, line):
    data = request.envelope
    frozen = invitation.envelope.get("manager_review")
    expected = {"request_id": str(request.pk), "request_hmac": request.envelope_hmac, "task_id": request.task_id,
        "client_id": request.client_id, "source_id": request.source_id, "revision_id": request.revision_id,
        "line_id": line["line_id"], "target_digest": invitation.target_digest}
    if (frozen != expected or invitation.client_id != request.client_id or invitation.request_source_id != request.source_id
        or invitation.revision_id != request.revision_id or invitation.target != line["target"]
        or invitation.target_digest != restock._digest(invitation.target)
        or invitation.envelope.get("source") not in data.get("sources", ())
        or invitation.envelope.get("recipient") != request.client.igsid
        or invitation.envelope.get("source", {}).get("namespace") != data.get("source_namespace")
        or invitation.envelope.get("settings_id") != data.get("settings_id")
        or invitation.envelope.get("settings_epoch") != data.get("settings_epoch")
        or invitation.envelope.get("client_epoch") != data.get("client_epoch")):
        return False
    selection = invitation.envelope.get("selection") or {}
    scope = data.get("source_scope") or {}
    return (selection.get("line_id") == line["line_id"] and selection.get("session_id") == scope.get("selection_session_id")
        and selection.get("generation") == scope.get("selection_generation")
        and selection.get("reset_floor") == scope.get("reset_floor")
        and selection.get("recipient_id") == line.get("recipient_id", "self"))


def _native_receipt(invitation, receipt):
    if getattr(receipt, "followup_task_id", None):
        return restock._valid_receipt(invitation, receipt)
    effect = receipt.effect
    return bool(restock._verified(receipt, "receipt") and receipt.invitation_id == invitation.pk
        and receipt.envelope.get("invitation_hmac") == invitation.envelope_hmac
        and restock._effect_matches(invitation, effect)
        and effect.provider_message_id == receipt.provider_message_id
        and effect.revision_snapshot_digest == invitation.envelope["source"]["revision_digest"]
        and receipt.envelope.get("effect_payload_digest") == effect.payload_digest
        and receipt.envelope.get("effect_plan_digest") == effect.plan_digest
        and effect.terminal_at and effect.terminal_at == receipt.delivered_at)


def _native_answer(answer, invitation, receipt, *, now):
    proof = answer.envelope.get("source") or {}
    source = answer.source
    return bool(restock._verified(answer, "answer") and answer.invitation_id == invitation.pk
        and answer.receipt_id == receipt.pk and answer.envelope.get("receipt_hmac") == receipt.envelope_hmac
        and answer.source_id == proof.get("message_id") and source.quick_reply_payload == answer.envelope.get("payload")
        and answer.answered_at == source.provider_created_at and answer.answered_at <= now
        and answer.answered_at >= receipt.delivered_at and source.pk > invitation.request_source_id
        and not source.attachments and not source.attachment_media and source.status != "ignored"
        and answer.envelope.get("payload") == (invitation.accept_payload if answer.kind == "accept" else invitation.revoke_payload))


def _delegated_receipt(subscription, invitation, event, request, now):
    """Verify the original producer; the restock task has no invented MID."""
    from django.core.exceptions import ValidationError
    from management.models import IgFollowUpTask
    from management.ig_fulfillment_review_models import IgFulfillmentReviewDecision
    from management.services import ig_fulfillment_notification_coordination as coordination
    task = event.task
    signed = (task.manager_context or {}).get("delegated_delivery")
    if (not isinstance(signed, dict) or signed.get("version") != 1 or task.status != "cancelled"
        or task.skip_reason != coordination.FULFILLED or task.provider_message_id or task.sent_message_id
        or task.attempt_count or task.sent_at or subscription.last_reason != coordination.FULFILLED):
        return None
    proof = signed.get("proof")
    if not isinstance(proof, dict):
        return None
    expected = restock._sign("delegated_delivery", proof, signed.get("key_id"))
    if not expected or not hmac.compare_digest(expected, str(signed.get("hmac", ""))):
        return None
    try:
        original = IgFollowUpTask.objects.select_related("sent_message").filter(pk=proof.get("task_id"), client_id=subscription.client_id).first()
        decision = IgFulfillmentReviewDecision.objects.select_related("request").filter(pk=proof.get("decision_id"), request_id=request.pk).first()
    except (ValidationError, ValueError, TypeError):
        return None
    if (not original or not reviews._verified(decision, "decision") or not reviews._verified(decision.request, "request")
        or decision.request.envelope_hmac != request.envelope_hmac or decision.envelope.get("request_digest") != request.envelope_hmac
        or coordination._invited_available_target(decision.request, decision, invitation) != invitation.target
        or original.reason != reviews.UPDATE_REASON or original.kind != "fulfillment" or original.trigger != "event"
        or original.policy_version != reviews.PURPOSE
        or original.event_key != f"ig-fulfillment-update:{request.pk}:{decision.version}"
        or original.event_payload != {"origin": reviews.UPDATE_REASON, "request_id": str(request.pk), "request_digest": request.envelope_hmac,
            "decision_id": str(decision.pk), "decision_version": decision.version, "decision_digest": decision.envelope_hmac}
        or coordination._task_state(original) != "sent" or not coordination._after_accept(original, subscription)
        or original.sent_at > now):
        return None
    actual = coordination._cart_native_receipt(original, subscription, (decision.request, decision))
    if not actual:
        return None
    # CRM history may be repaired or removed after the durable native checkpoint.
    # The coordinator validates any present history; its optional ID grants no send.
    comparable = lambda value: {key: item for key, item in value.items() if key != "sent_message_id"}
    if comparable(proof) != comparable(actual):
        return None
    return {"purpose": reviews.UPDATE_REASON, "task_id": original.pk, "sent_at": original.sent_at.isoformat(),
        "native_receipt_confirmed": True}


def _notification_state(subscription, invitation, request, now):
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    if subscription.state == "unknown":
        return "unknown", None
    if subscription.state != "sent":
        return ("pending" if subscription.state == "active" else "not_sent"), None
    event = getattr(subscription, "availability_event", None)
    if not event or not restock._verified(event, "event"):
        return "unknown", None
    task = event.task
    binding = (task.client_id == subscription.client_id and task.reason == restock.ORIGIN
        and task.kind == "qualification" and task.policy_version == "restock-agreement.v1"
        and task.event_key == event.event_key and event.event_key == f"{restock.ORIGIN}:{subscription.pk}"
        and task.trigger == "event" and event.subscription_id == subscription.pk
        and task.event_payload == {"origin": restock.ORIGIN, "purpose": restock.PURPOSE,
            "subscription_id": str(subscription.pk), "event_id": str(event.pk)}
        and event.envelope.get("answer_hmac") == subscription.accepted_answer.envelope_hmac
        and event.envelope.get("target_digest") == invitation.target_digest)
    if not binding:
        return "unknown", None
    if (task.manager_context or {}).get("delegated_delivery"):
        receipt = _delegated_receipt(subscription, invitation, event, request, now)
        return ("sent", receipt) if receipt else ("unknown", None)
    from management.services import ig_fulfillment_notification_coordination as coordination
    valid = (task.status == "sent" and normalize_provider_message_id(task.provider_message_id)
        and task.attempt_count == 1 and task.sent_at and timezone.is_aware(task.sent_at) and task.sent_at <= now
        and task.sent_at >= max(subscription.accepted_answer.answered_at, subscription.accepted_answer.created_at)
        and not task.claim_token and task.claim_until is None
        and task.message_text == restock._copy(invitation.locale, "notification", invitation.envelope["label"])
        and task.client_id == subscription.client_id and task.reason == restock.ORIGIN
        and task.event_key == event.event_key and task.trigger == "event" and event.subscription_id == subscription.pk
        and task.event_payload == {"origin": restock.ORIGIN, "purpose": restock.PURPOSE,
            "subscription_id": str(subscription.pk), "event_id": str(event.pk)}
        and event.envelope.get("answer_hmac") == subscription.accepted_answer.envelope_hmac
        and event.envelope.get("target_digest") == invitation.target_digest)
    if not valid or not coordination._restock_binding(event, subscription) or not coordination._history_matches(task, invitation):
        return "unknown", None
    return "sent", {"purpose": restock.ORIGIN, "task_id": task.pk, "sent_at": task.sent_at.isoformat(), "native_receipt_confirmed": True}


def _read_fence(subscription):
    answer, invitation = subscription.accepted_answer, subscription.accepted_answer.invitation
    receipt, source = answer.receipt, answer.source
    effect = receipt.effect
    if getattr(receipt, "followup_task_id", None):
        task = receipt.followup_task
        native = ("followup_task", task.pk, task.status, task.provider_message_id, task.sent_at,
            task.event_payload, task.event_key, task.message_text, task.manager_context, task.attempt_count,
            task.claim_token, task.claim_until, task.client_id)
    else:
        native = ("revision_effect", effect.state, effect.provider_message_id, effect.terminal_at,
            effect.payload, effect.payload_digest, effect.plan_digest, effect.revision_snapshot_digest,
            effect.revision_id, effect.source_message_id, effect.recipient_igsid, effect.provider_namespace,
            effect.settings_id_snapshot, effect.settings_permission_epoch, effect.client_permission_epoch)
    event = getattr(subscription, "availability_event", None)
    delivery = (event.pk, event.envelope, event.envelope_hmac, event.task.status,
        event.task.provider_message_id, event.task.sent_at, event.task.event_payload,
        event.task.manager_context, event.task.message_text, event.task.attempt_count,
        event.task.skip_reason, event.task.sent_message_id, event.task.claim_token, event.task.claim_until) if event else None
    from management.services.ig_turn_revisions import _source_payload
    return deepcopy((subscription.pk, subscription.client_id, subscription.accepted_answer_id,
        subscription.state, subscription.expires_at, subscription.revoke_id, subscription.revoke_hmac,
        subscription.revoke_envelope, subscription.revoke_source_id, delivery,
        restock._columns(answer), answer.envelope, answer.envelope_hmac, restock._columns(invitation),
        invitation.envelope, invitation.envelope_hmac, restock._columns(receipt), receipt.envelope,
        receipt.envelope_hmac, native,
        _source_payload(source, ordinal=0), source.role, source.source, source.status, source.client_id, source.sender_id))


def _manager_review_reuse(subscription, request, decision):
    """Verify loaded event provenance; the outer projection owns currentness."""
    event = getattr(subscription, "availability_event", None)
    if (not event or not decision or not restock._verified(event, "event")
        or not reviews._verified(request, "request") or not reviews._verified(decision, "decision")
        or decision.request_id != request.pk or decision.envelope.get("request_digest") != request.envelope_hmac
        or request.envelope.get("scope") != "cart" or event.subscription_id != subscription.pk):
        return None
    answer = subscription.accepted_answer
    invitation = answer.invitation
    task = event.task
    observation = event.envelope.get("availability") or {}
    if (task.status != "pending" or task.provider_message_id or task.sent_at
        or task.attempt_count or task.claim_token or task.claim_until is not None
        or task.client_id != subscription.client_id or task.reason != restock.ORIGIN or task.kind != "qualification"
        or task.trigger != "event" or task.policy_version != "restock-agreement.v1"
        or task.event_key != event.event_key or event.event_key != f"{restock.ORIGIN}:{subscription.pk}"
        or task.event_payload != {"origin": restock.ORIGIN, "purpose": restock.PURPOSE,
            "subscription_id": str(subscription.pk), "event_id": str(event.pk)}
        or event.envelope.get("answer_hmac") != answer.envelope_hmac
        or event.envelope.get("target_digest") != invitation.target_digest
        or observation.get("target") != invitation.target or observation.get("digest") != event.availability_digest):
        return None
    line_id = invitation.envelope["manager_review"]["line_id"]
    lines = [line for line in request.envelope.get("lines", ()) if isinstance(line, dict)
        and line.get("line_id") == line_id and line.get("target") == invitation.target]
    answers = [line for line in decision.envelope.get("lines", ()) if isinstance(line, dict) and line.get("line_id") == line_id]
    if len(lines) != 1 or len(answers) != 1:
        return None
    line = answers[0]
    if (line.get("availability") != "available" or type(line.get("checked_quantity")) is not int
        or line["checked_quantity"] < invitation.target.get("quantity", 1000)
        or line.get("supply_basis") not in {"warehouse_checked", "accessible_unrecorded"}):
        return None
    expected = {"version": 1, "review_scope": "cart", "request_id": str(request.pk),
        "request_digest": request.envelope_hmac, "decision_id": str(decision.pk),
        "decision_version": decision.version, "decision_digest": decision.envelope_hmac,
        "subscription_id": str(subscription.pk), "target": invitation.target, "line_id": line_id,
        "inventory_digest": line.get("inventory_digest"), "configuration_digest": line.get("configuration_digest"),
        "checked_quantity": line["checked_quantity"], "supply_basis": line["supply_basis"],
        "invitation_id": str(invitation.pk), "invitation_digest": invitation.envelope_hmac,
        "answer_id": str(answer.pk), "answer_digest": answer.envelope_hmac}
    if event.envelope.get("manager_decision") != expected:
        return None
    return {"origin_scope": "cart", "request_id": str(request.pk), "decision_id": str(decision.pk),
        "checked_at": decision.envelope.get("decided_at"), "status": "available",
        "provenance": "availability_event", "event_id": str(event.pk)}


def capture_restock_interest(request, settings_obj, cart, now, *, reviewed_decision=None):
    """The caller owns current request verification; this helper grants no effect."""
    if not request or not reviews._verified(request, "request") or request.envelope.get("scope") != "cart":
        return None
    data = request.envelope
    lines = data.get("lines") or []
    if (len(lines) != 1 or not lines[0].get("target") or cart.get("status") != "captured"
        or not cart.get("coverage_complete") or reviews._cart_identity(cart) != data.get("cart_identity")):
        return None
    line = lines[0]
    from django.db.models import OuterRef, Subquery
    revocations = IgRestockAnswer.objects.filter(invitation_id=OuterRef("accepted_answer__invitation_id"), kind="revoke")
    query = IgRestockSubscription.objects.filter(client_id=request.client_id,
        accepted_answer__invitation__envelope__manager_review__request_id=str(request.pk)).select_related(
        "accepted_answer__source", "accepted_answer__invitation", "accepted_answer__receipt__effect", "accepted_answer__receipt__followup_task",
        "availability_event__task__sent_message").annotate(revoke_id=Subquery(revocations.values("pk")[:1]),
            revoke_hmac=Subquery(revocations.values("envelope_hmac")[:1]),
            revoke_envelope=Subquery(revocations.values("envelope")[:1]),
            revoke_source_id=Subquery(revocations.values("source_id")[:1])).order_by("-created_at")
    candidates = list(query[:2])
    if not candidates:
        return None
    if len(candidates) != 1:
        return _summary("unverified", line)
    subscription = candidates[0]
    answer, invitation = subscription.accepted_answer, subscription.accepted_answer.invitation
    receipt = answer.receipt
    if not _binding(request, invitation, line):
        return None
    failure = lambda: _summary("unverified", line)
    if (not restock._verified(invitation, "invitation") or not _native_receipt(invitation, receipt)
        or answer.kind != "accept" or not _native_answer(answer, invitation, receipt, now=now)
        or subscription.expires_at != invitation.expires_at
        or settings_obj.pk != invitation.envelope["settings_id"]
        or settings_obj.reply_permission_epoch != invitation.envelope["settings_epoch"]
        or request.client.reply_permission_epoch != invitation.envelope["client_epoch"]):
        return failure()
    revoked = list(IgRestockAnswer.objects.filter(pk=subscription.revoke_id, invitation=invitation, kind="revoke").select_related("source")[:2]) if subscription.revoke_id else []
    if len(revoked) > 1:
        return failure()
    proofs = [invitation.envelope["source"], answer.envelope["source"]]
    if revoked:
        revocation = revoked[0]
        if not _native_answer(revocation, invitation, receipt, now=now):
            return failure()
        ack = revocation.envelope.get("ack_card")
        if not isinstance(ack, dict):
            return failure()
        from management.models import IgRevisionDeliveryEffect
        effects = list(IgRevisionDeliveryEffect.objects.filter(revision_id=answer.envelope["source"]["revision_id"],
            source_message_id=answer.source_id).order_by("pk")[:2])
        if len(effects) != 1:
            return failure()
        effect = effects[0]
        expected = {"effect_id": effect.pk, "payload_digest": effect.payload_digest,
            "plan_digest": effect.plan_digest, "provider_message_id": effect.provider_message_id}
        if (ack != expected or not restock._effect_matches(invitation, effect, body=restock._copy(invitation.locale,
            "accepted", invitation.envelope["label"]), action="revoke", source_id=answer.source_id,
            revision_id=answer.envelope["source"]["revision_id"])
            or effect.revision_snapshot_digest != answer.envelope["source"]["revision_digest"]
            or not effect.terminal_at or effect.terminal_at > revocation.answered_at
            or (revocation.source.reply_to_provider_message_id and revocation.source.reply_to_provider_message_id != effect.provider_message_id)):
            return failure()
        proofs.append(revocation.envelope["source"])
    bundle = SimpleNamespace(client=request.client, envelope={"sources": proofs,
        "source_namespace": data["source_namespace"], "source_id": max(proof["message_id"] for proof in proofs)})
    if reviews._batch_cart_sources_reason(bundle, settings_obj, cart, now=now):
        return failure()
    final = list(query[:2])
    if len(final) != 1 or _read_fence(subscription) != _read_fence(final[0]):
        return failure()
    notification, notification_receipt = _notification_state(subscription, invitation, request, now)
    if revoked:
        status = "revoked"
    elif subscription.state == "revoked":
        return failure()
    elif now >= invitation.expires_at:
        status = "expired"
    elif subscription.state in {"active", "sent", "unknown"}:
        status = "accepted"
    else:
        return failure()
    summary = _summary(status, line, notification_state=notification, notification_receipt=notification_receipt,
        answer=answer, invitation=invitation, proofs=proofs)
    if status == "accepted" and subscription.state == "active" and notification == "pending":
        reuse = _manager_review_reuse(subscription, request, reviewed_decision)
        if reuse:
            summary["manager_review_reuse"] = reuse
    return summary
