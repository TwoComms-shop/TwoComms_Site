"""An exact WAIT-list offer carried by the original manager-update outbox.

The follow-up task is the native delivery owner. It never acquires a synthetic
customer turn or revision delivery effect, and offering never creates consent.
"""
from copy import deepcopy
import uuid

from django.db import transaction
from django.utils import timezone

from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_restock_subscriptions as restock
from management.services import ig_restock_waiting_list as waiting

TASK_KEY = "restock_invitation.v1"
PROVENANCE = "manager_followup.v1"
MAX_BYTES = 950


def _failure(reason):
    return {"ready": False, "reason": reason, "invitation_id": None, "body": "", "quick_replies": ()}


def _plan(inv):
    return {"recipient": {"id": inv.envelope["recipient"]}, "message": {
        "text": inv.body, "quick_replies": [restock._native_button(inv)]}}


def _wire(inv):
    return {"ready": True, "reason": "", "invitation_id": str(inv.pk), "body": inv.body,
        "quick_replies": (restock._button(inv),)}


def _target(request, decision):
    """Use every signed WAIT line, independently of accounting stock counts."""
    rows = request.envelope["lines"]
    decisions = decision.envelope["lines"]
    if (len({row["line_id"] for row in rows}) != len(rows)
        or len(decisions) != len(rows) or {row["line_id"] for row in decisions} != {row["line_id"] for row in rows}):
        raise ValueError("waiting_list_manager_coverage_changed")
    by_id = {row["line_id"]: row for row in decisions}
    items = []
    for row in rows:
        if by_id[row["line_id"]]["availability"] != "wait":
            continue
        native, preferences = row["target"], row["preferences"]
        if not native or not row.get("configuration_digest") or not by_id[row["line_id"]].get("configuration_digest"):
            raise ValueError("waiting_list_configuration_unverified")
        if row["configuration_digest"] != by_id[row["line_id"]]["configuration_digest"]:
            raise ValueError("waiting_list_configuration_changed")
        labels = row["observation"].get("labels") or {}
        item = {"line_id": row["line_id"], "recipient_id": row["recipient_id"], "item_kind": "catalog_apparel",
            "title": labels.get("product") or labels.get("product_title") or "", "color": labels.get("color") or "",
            "size": native["size"], "fit": native["fit_option_code"], "options": deepcopy(native["option_values"]),
            "quantity": native["quantity"], "catalog_target": deepcopy(native), "source": {
                "kind": "catalog_selection", "selection": restock._selection_identity(preferences),
                "selection_sources": restock._selection_sources(preferences, request.client, request.envelope["source_namespace"])}}
        if not item["source"]["selection_sources"]:
            raise ValueError("waiting_list_original_source_changed")
        items.append(item)
    if not items:
        raise ValueError("waiting_list_no_manager_wait_items")
    target = {"schema": waiting.SCHEMA, "completion": "all", "items": items}
    waiting.validate_target(target)
    waiting.verify_item_sources(target, request.client, request.envelope["source_namespace"])
    return target


def _case(task, projection):
    data = task.event_payload or {}
    # Reject malformed or synthetic task identifiers before ORM conversion.
    uuid.UUID(str(data.get("request_id")))
    uuid.UUID(str(data.get("decision_id")))
    request = reviews.Request.objects.select_related("client", "source", "revision", "task").filter(
        pk=data.get("request_id"), client_id=task.client_id).first()
    decision = reviews.Decision.objects.filter(pk=data.get("decision_id"), request=request).first() if request else None
    if (not reviews._verified(request, "request") or not reviews._verified(decision, "decision")
        or request.envelope.get("scope") != "cart" or not request.source_id or not request.revision_id
        or decision.envelope.get("request_digest") != request.envelope_hmac
        or data != {"origin": reviews.UPDATE_REASON, "request_id": str(request.pk), "request_digest": request.envelope_hmac,
            "decision_id": str(decision.pk), "decision_version": decision.version, "decision_digest": decision.envelope_hmac}
        or task.reason != reviews.UPDATE_REASON or task.kind != "fulfillment" or task.trigger != "event"
        or task.event_key != f"ig-fulfillment-update:{request.pk}:{decision.version}"
        or str((projection.get("current_request") or {}).get("id")) != str(request.pk)
        or str((projection.get("decision") or {}).get("id")) != str(decision.pk)):
        raise ValueError("waiting_list_manager_binding_changed")
    return request, decision


def _binding(request, decision, task, target):
    return {"schema": PROVENANCE, "request_id": str(request.pk), "request_hmac": request.envelope_hmac,
        "decision_id": str(decision.pk), "decision_hmac": decision.envelope_hmac,
        "decision_version": decision.version, "task_id": task.pk, "client_id": task.client_id,
        "target_digest": restock._digest(target)}


def _target_parity(inv, request, decision):
    """Immutable signed source parity; current source proofs have their owner."""
    waiting.validate_target(inv.target)
    rows = {row["line_id"]: row for row in request.envelope["lines"]}
    decisions = decision.envelope["lines"]
    expected = [row["line_id"] for row in decisions if row["availability"] == "wait"]
    actual = [item["line_id"] for item in inv.target["items"]]
    if (len(decisions) != len(rows) or {row["line_id"] for row in decisions} != set(rows)
        or len(set(expected)) != len(expected) or set(expected) != set(actual)):
        return False
    by_id = {row["line_id"]: row for row in decisions}
    for item in inv.target["items"]:
        row, answer = rows[item["line_id"]], by_id[item["line_id"]]
        native, labels = row["target"], row["observation"].get("labels") or {}
        if (item["catalog_target"] != native or item["recipient_id"] != row["recipient_id"]
            or item["item_kind"] != "catalog_apparel" or item["source"]["kind"] != "catalog_selection"
            or item["source"]["selection"] != restock._selection_identity(row["preferences"])
            or item["title"] != (labels.get("product") or labels.get("product_title") or "")
            or item["color"] != (labels.get("color") or "") or not row.get("configuration_digest")
            or answer.get("configuration_digest") != row["configuration_digest"]):
            return False
    return True


def invitation_case(inv, *, task=None, history=None):
    """Validate historical provenance without claiming current stock or review."""
    from management.models import IgFollowUpTask
    frozen = inv.envelope.get("manager_review") or {}
    if (not restock._verified(inv, "invitation") or inv.envelope.get("producer") != PROVENANCE
        or restock._digest(inv.target) != inv.target_digest or frozen.get("schema") != PROVENANCE):
        return None
    try:
        task = task or IgFollowUpTask.objects.filter(pk=frozen.get("task_id"), client_id=inv.client_id).first()
        if not task or task.pk != frozen.get("task_id") or task.client_id != inv.client_id:
            return None
        history = history if history is not None else reviews.validated_update_history(task)
        if not history.get("ready"):
            return None
        request, decision = _case(task, history)
        if (inv.request_source_id != request.source_id or inv.revision_id != request.revision_id
            or inv.envelope.get("source") not in request.envelope.get("sources", [])
            or not _target_parity(inv, request, decision) or frozen != _binding(request, decision, task, inv.target)):
            return None
        return request, decision, task
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def original_baseline(inv):
    case = invitation_case(inv)
    if not case:
        raise ValueError("waiting_list_configuration_ancestry_changed")
    ids = {item["line_id"] for item in inv.target["items"]}
    return {row["line_id"]: row["configuration_digest"] for row in case[0].envelope["lines"] if row["line_id"] in ids}


def _locale(request, source, proof):
    from management.services.ig_reply_language import resolve_own_source_reply_language
    language = resolve_own_source_reply_language(sources=[{"message_id": source.pk, "role": "user", "text": source.text,
        "provider_created_at": source.provider_created_at.isoformat()}], history=[], profile_language=request.client.language,
        reset_floor=proof["reset_floor"], watermark_message_id=source.pk, watermark_event_at=source.provider_created_at)
    locale = language.template_family if language.template_family in {"uk", "ru", "en"} else language.knowledge_locale
    return locale, language.as_dict()


def _copy(locale, text, target):
    offers = {
        "uk": "💛 Повідомити один раз, коли менеджер підтвердить наявність усіх позицій?\n{items}\nПідтвердьте кнопкою. Це не бронювання.",
        "ru": "💛 Сообщить один раз, когда менеджер подтвердит наличие всех позиций?\n{items}\nПодтвердите кнопкой. Это не резерв.",
        "en": "💛 One message when our manager confirms all these items are available?\n{items}\nConfirm below. This is not a reservation.",
    }
    return text + "\n\n" + offers.get(locale, offers["uk"]).format(items=waiting.label(target))


def accepted_copy(locale, target):
    """Every item is a notification wish, independently of stock authority.

    The attribution stays on each item's line so exact-line truth validation
    never inherits an acknowledgement from a separate header or sibling.
    This copy version belongs only to manager_followup.v1 invitations.
    """
    copies = {
        "uk": ("Домовились 💛 Одне повідомлення після перевірки всіх позицій менеджером.",
            "Для сповіщення ви обрали:", "Це не бронювання й не обіцянка дати. Якщо повідомлення довго немає, напишіть нам."),
        "ru": ("Договорились 💛 Одно сообщение после проверки всех позиций менеджером.",
            "Для уведомления вы выбрали:", "Это не резерв и не обещание даты. Если сообщения долго нет, напишите нам."),
        "en": ("Agreed 💛 One message after our manager checks all listed items.",
            "For this notification you selected:", "This is not a reservation or promised date. If you hear nothing after a long pause, message us."),
    }
    header, attribution, footer = copies.get(locale, copies["uk"])
    lines = [f"{index + 1}. {attribution} {waiting.item_label(item)}" for index, item in enumerate(target["items"])]
    return header + "\n" + "\n".join(lines) + "\n\n" + footer


def _current_case_reason(request, decision, settings_obj, now):
    from django.contrib.auth import get_user_model
    from management.bot_access import has_all_bot_capabilities, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
    actor = get_user_model().objects.filter(pk=decision.actor_id, is_active=True).first()
    if not actor or not has_all_bot_capabilities(actor, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION):
        return "manager_actor_forbidden"
    return (reviews._current_reason(request, settings_obj, now=now)
        or reviews._decision_reason(request, decision, now=now))



def subscription_overlap_reason(client_id, target, settings_obj, *, now):
    """Deduplicate authenticated matching variants, allowing other interests."""
    def identities(value):
        values = [item.get("catalog_target") for item in value["items"]] if waiting.is_list(value) else [value]
        return {restock._digest({key: item[key] for key in
            ("product_id", "color_variant_id", "size", "fit_option_code", "option_values")})
            for item in values if isinstance(item, dict) and "color_variant_id" in item}
    expected = identities(target)
    rows = list(restock.Subscription.objects.filter(client_id=client_id, state="active").select_related(
        "accepted_answer__invitation__client", "accepted_answer__invitation__revision",
        "accepted_answer__invitation__request_source", "accepted_answer__source",
        "accepted_answer__receipt__effect", "accepted_answer__receipt__followup_task").order_by("pk")[:17])
    if len(rows) > 16:
        return "waiting_list_subscription_capture_bound"
    for row in rows:
        if not restock._subscription_reason(row, settings_obj, now=now):
            if identities(row.accepted_answer.invitation.target) & expected:
                return "waiting_list_existing_subscription"
    return ""


def prepare_task_offer(task, projection, settings_obj, *, text, now):
    """Called only by the existing task/client transaction before its claim."""
    if task and TASK_KEY in (task.manager_context or {}):
        return task_offer(task, projection, settings_obj, text=text, now=now)
    if not task or not restock._enabled() or task.status != "pending" or task.attempt_count or task.provider_message_id:
        return _failure("waiting_list_offer_not_pending")
    try:
        request, decision = _case(task, projection)
        reason = _current_case_reason(request, decision, settings_obj, now)
        if reason:
            return _failure(reason)
        owned, reason = restock.owned_source(request.revision, request.source, settings_obj, require_head=False)
        if not owned:
            return _failure(reason)
        revision, source, proof = owned
        reason = restock._scope(revision.client, settings_obj)
        if reason:
            return _failure(reason)
        if now >= source.provider_created_at + restock.WINDOW:
            return _failure("waiting_list_acceptance_window_closed")
        target = _target(request, decision)
        from management.services.ig_assisted_commerce import _configuration_capture
        from management.services.ig_commerce_projection import capture_current_selection_lines
        cart = capture_current_selection_lines(task.client_id, now=now)
        if (cart.get("status") != "captured" or not cart.get("coverage_complete")
            or reviews._cart_identity(cart) != request.envelope.get("cart_identity")):
            return _failure("waiting_list_source_cart_unavailable")
        locale, language = _locale(request, source, proof)
        body = _copy(locale, text, target)
        if any(len(value.encode()) > MAX_BYTES for value in (body,
            accepted_copy(locale, target), waiting.copy_for(locale, "notification", waiting.label(target)))):
            return _failure("waiting_list_single_card_bound")
        # A previous source invitation or active overlapping agreement owns its
        # consent. Another manager update must not silently compete with it.
        if restock.Invitation.objects.filter(revision=revision, request_source=source).exists():
            return _failure("waiting_list_source_invitation_exists")
        reason = subscription_overlap_reason(task.client_id, target, settings_obj, now=now)
        if reason:
            return _failure(reason)
        if restock.Invitation.objects.filter(client_id=task.client_id, target_digest=restock._digest(target),
                acceptance_deadline__gt=now).exists():
            return _failure("waiting_list_existing_invitation")
        identity, key_id = uuid.uuid4(), next(iter(restock._keys()))
        inv = restock.Invitation(id=identity, client=revision.client, request_source=source, revision=revision,
            target=target, target_digest=restock._digest(target), expires_at=now + restock.VALIDITY,
            acceptance_deadline=source.provider_created_at + restock.WINDOW, locale=locale, body=body,
            button_title=restock._copy(locale, "button"), accept_payload=restock._payload(identity, "accept", key_id),
            revoke_payload=restock._payload(identity, "revoke", key_id))
        restock._seal(inv, "invitation", source=proof, label=waiting.label(target), recipient=revision.client.igsid,
            settings_id=settings_obj.pk, settings_epoch=settings_obj.reply_permission_epoch,
            client_epoch=revision.client.reply_permission_epoch, manager_review=_binding(request, decision, task, target),
            waiting_list={"schema": waiting.SCHEMA, "source_capture": deepcopy(cart), "configuration": _configuration_capture(cart),
                "reply_language": language}, native_future_capability="unverified", producer=PROVENANCE)
        task.manager_context = {**(task.manager_context or {}), TASK_KEY: {"invitation_id": str(inv.pk),
            "invitation_hmac": inv.envelope_hmac, "payload_digest": restock._digest(_plan(inv))}}
        return _wire(inv)
    except (ValueError, KeyError, TypeError, AttributeError):
        return _failure("waiting_list_offer_unverified")


def task_offer(task, projection, settings_obj, *, text, now, require_current=True):
    try:
        frozen = (task.manager_context or {}).get(TASK_KEY)
        if not isinstance(frozen, dict):
            return _failure("waiting_list_offer_absent")
        inv = restock.Invitation.objects.select_related("client", "request_source", "revision").filter(
            pk=frozen.get("invitation_id"), client_id=task.client_id).first()
        if (not restock._verified(inv, "invitation") or inv.envelope.get("producer") != PROVENANCE
            or frozen != {"invitation_id": str(inv.pk), "invitation_hmac": inv.envelope_hmac,
                "payload_digest": restock._digest(_plan(inv))} or text != inv.body or len(inv.body.encode()) > MAX_BYTES):
            return _failure("waiting_list_offer_plan_changed")
        request, decision = _case(task, projection)
        if (inv.envelope.get("manager_review") != _binding(request, decision, task, inv.target)
            or not _target_parity(inv, request, decision)):
            return _failure("waiting_list_manager_binding_changed")
        reason = waiting.invitation_reason(inv, settings_obj, require_head=False, now=now,
            check_dispatch_scope=require_current)
        if not reason and require_current:
            reason = (_current_case_reason(request, decision, settings_obj, now)
                or waiting.current_target_reason(inv, now=now))
            if not reason and now >= inv.acceptance_deadline:
                reason = "waiting_list_acceptance_window_closed"
        return _failure(reason) if reason else _wire(inv)
    except (ValueError, KeyError, TypeError, AttributeError):
        return _failure("waiting_list_offer_unverified")


def valid_task_receipt(inv, receipt):
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    from management.services.ig_fulfillment_delivery import render_task_update
    try:
        task = receipt.followup_task
        history = reviews.validated_update_history(task)
        case = invitation_case(inv, task=task, history=history)
        if not history.get("ready") or not case:
            return False
        frozen = (task.manager_context or {}).get(TASK_KEY)
        return bool(not receipt.effect_id and restock._verified(receipt, "receipt")
            and receipt.invitation_id == inv.pk and receipt.envelope.get("invitation_hmac") == inv.envelope_hmac
            and task.client_id == inv.client_id and task.status == "sent" and task.attempt_count == 1
            and not task.claim_token and task.claim_until is None and normalize_provider_message_id(task.provider_message_id)
            and task.provider_message_id == receipt.provider_message_id and task.sent_at == receipt.delivered_at
            and task.sent_at and task.sent_at >= max(inv.created_at, case[1].created_at)
            and receipt.delivered_at < inv.acceptance_deadline and task.message_text == inv.body
            and render_task_update(task, history) == inv.body
            and frozen == {"invitation_id": str(inv.pk), "invitation_hmac": inv.envelope_hmac,
                "payload_digest": restock._digest(_plan(inv))}
            and receipt.envelope.get("task_event_key") == task.event_key
            and receipt.envelope.get("task_payload_digest") == restock._digest(_plan(inv))
            and receipt.envelope.get("task_proof") == {"task_id": task.pk, "event_key": task.event_key,
                "event_payload_digest": restock._digest(task.event_payload), "payload_digest": restock._digest(_plan(inv)),
                "manager_review": inv.envelope["manager_review"]})
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def record_task_delivery(task_id, *, now=None):
    from management.models import IgFollowUpTask
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    from management.services.ig_fulfillment_delivery import render_task_update
    now = now or timezone.now()
    with transaction.atomic():
        task = IgFollowUpTask.objects.select_for_update().filter(pk=task_id).first()
        frozen = (task.manager_context or {}).get(TASK_KEY) if task else None
        if not isinstance(frozen, dict):
            return False
        try:
            uuid.UUID(str(frozen.get("invitation_id")))
        except (ValueError, TypeError, AttributeError):
            return False
        inv = restock.Invitation.objects.select_for_update().select_related("client", "revision", "request_source").filter(
            pk=frozen.get("invitation_id"), client_id=task.client_id).first()
        if not inv or not restock._verified(inv, "invitation"):
            return False
        existing = restock.Receipt.objects.select_related("followup_task").filter(invitation=inv).first()
        if existing:
            return valid_task_receipt(inv, existing)
        try:
            history = reviews.validated_update_history(task)
        except (ValueError, TypeError, AttributeError):
            return False
        case = invitation_case(inv, task=task, history=history)
        if (not history.get("ready") or not case or task.status != "sent" or task.attempt_count != 1 or task.claim_token or task.claim_until
            or not normalize_provider_message_id(task.provider_message_id) or not task.sent_at or task.sent_at > now
            or task.sent_at < max(inv.created_at, case[1].created_at) or task.sent_at >= inv.acceptance_deadline or task.message_text != inv.body
            or render_task_update(task, history) != inv.body or frozen != {"invitation_id": str(inv.pk),
                "invitation_hmac": inv.envelope_hmac, "payload_digest": restock._digest(_plan(inv))}):
            return False
        receipt = restock.Receipt(invitation=inv, followup_task=task, provider_message_id=task.provider_message_id, delivered_at=task.sent_at)
        restock._seal(receipt, "receipt", invitation_hmac=inv.envelope_hmac,
            task_event_key=task.event_key, task_payload_digest=restock._digest(_plan(inv)),
            task_proof={"task_id": task.pk, "event_key": task.event_key, "event_payload_digest": restock._digest(task.event_payload),
                "payload_digest": restock._digest(_plan(inv)), "manager_review": inv.envelope["manager_review"]})
        return valid_task_receipt(inv, receipt)
