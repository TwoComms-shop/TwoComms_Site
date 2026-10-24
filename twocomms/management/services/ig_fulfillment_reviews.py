"""Source-bound manager feasibility review; estimates confer no commerce effects."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import re
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest as Request, IgFulfillmentReviewDecision as Decision

PURPOSE = "fulfillment-review.v1"
UPDATE_REASON = "fulfillment_manager_update"
FRESHNESS = timedelta(hours=24)
CALLBACK_PREFIX = "FR1:"
CALLBACK_ACTIONS = frozenset({"a", "13", "3", "7", "u", "o"})


def _enabled():
    return bool(getattr(settings, "IG_FULFILLMENT_REVIEWS_ENABLED", False))


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _keys():
    values = [str(getattr(settings, "IG_FULFILLMENT_HMAC_KEY", "") or settings.SECRET_KEY),
        *map(str, getattr(settings, "SECRET_KEY_FALLBACKS", ()))]
    return {hashlib.sha256(value.encode()).hexdigest()[:24]: value for value in values if value}


def _sign(kind, value, key_id):
    key = _keys().get(key_id)
    return hmac.new(key.encode(), (PURPOSE + ":" + kind + ":" + _json(value)).encode(), hashlib.sha256).hexdigest() if key else ""


def _columns(row):
    columns = {}
    for field in row._meta.concrete_fields:
        if field.name in {"envelope", "envelope_hmac", "created_at"}:
            continue
        value = getattr(row, field.attname)
        columns[field.attname] = (value.isoformat(timespec="microseconds") if isinstance(value, datetime)
            else format(field.to_python(value), f".{field.decimal_places}f") if value is not None and hasattr(field, "decimal_places")
            else str(value) if isinstance(value, (uuid.UUID, Decimal)) else value)
    return columns


def _seal(row, kind, payload):
    row.key_id = next(iter(_keys()))
    row.envelope = {"version": 1, "purpose": PURPOSE, "columns": _columns(row), **deepcopy(payload)}
    row.envelope_hmac = _sign(kind, row.envelope, row.key_id)
    row.save(force_insert=True)
    return row


def _verified(row, kind):
    expected = _sign(kind, row.envelope, row.key_id) if row and isinstance(row.envelope, dict) else ""
    return bool(expected and hmac.compare_digest(expected, row.envelope_hmac)
        and row.envelope.get("version") == 1 and row.envelope.get("purpose") == PURPOSE
        and row.envelope.get("columns") == _columns(row))


def _observation(value):
    return deepcopy(value.as_dict() if hasattr(value, "as_dict") else value)


def inventory_digest(value):
    """Clock-independent exact physical evidence, stable between equal reads."""
    value = _observation(value)
    return _digest({key: value.get(key) for key in ("version", "status", "reason", "target", "evidence", "labels")})


def _configuration(target, *, now):
    from management.services.ig_restock_availability import observe_configuration
    return _observation(observe_configuration(target, now=now))


def _cart_identity(cart):
    return _digest({key: cart.get(key) for key in ("schema", "scope", "session_id", "generation", "selection_revision", "active_line_id", "lines")})


def _settings(settings_obj=None):
    from management.models import InstagramBotSettings
    return InstagramBotSettings.objects.filter(pk=settings_obj.pk).first() if settings_obj is not None else InstagramBotSettings.objects.order_by("pk").first()


def _label(observation, index):
    target, labels = observation.get("target") or {}, observation.get("labels") or {}
    parts = [labels.get("product"), labels.get("color"), target.get("size"), labels.get("fit"),
        "× " + str(target.get("quantity")) if target.get("quantity") else ""]
    text = " · ".join(str(part) for part in parts if part)
    return re.sub(r"[\x00-\x1f\x7f]", " ", text)[:160] or f"Позиція {index + 1} (точні параметри потребують уточнення)"


def _display_label(observation, index):
    """Bounded configuration label; quantity has its own presentation field."""
    target, labels = observation.get("target") or {}, observation.get("labels") or {}
    text = " · ".join(str(part) for part in (labels.get("product"), labels.get("color"),
        target.get("size"), labels.get("fit")) if part)
    return re.sub(r"[\x00-\x1f\x7f]", " ", text)[:160] or f"Позиція {index + 1} (точні параметри потребують уточнення)"


def _lines(cart, watermark, now):
    from management.services.ig_response_cart_plan import source_line_preferences
    from management.services.ig_restock_availability import resolve_source_interest
    from management.services.ig_restock_subscriptions import _quantity_basis
    lines = []
    for row in cart.get("lines", []):
        preferences = source_line_preferences(cart, row, source_watermark=watermark)
        preferences = _quantity_basis(preferences, row)
        observed = resolve_source_interest(preferences, now=now).as_dict()
        lines.append({"line_id": row["line_id"], "recipient_id": row.get("recipient_id", "self"),
            "label": _label(observed, row.get("index", 0)),
            "display_label": _display_label(observed, row.get("index", 0)), "target": observed.get("target") or {},
            "preferences": preferences, "observation": observed, "inventory_digest": inventory_digest(observed)})
    # Pending manager debt needs a configuration proof too. Inventory counts
    # cannot stand in for the exact SKU/fit/options identity when no decision
    # has yet been recorded. Capture all exact targets in one bounded batch.
    from management.services.ig_restock_availability import observe_configurations
    exact = [line for line in lines if line["target"]]
    configurations = observe_configurations([line["target"] for line in exact], now=now)
    for line, configuration in zip(exact, configurations):
        line["configuration_digest"] = (configuration.get("configuration_digest", "")
            if configuration.get("status") == "valid" and configuration.get("target") == line["target"] else "")
    return lines


def _unverified_configuration_inquiries(revision, cart, source_ids):
    """Reuse native inquiry capture; an old selected SKU cannot answer a new wish."""
    from management.services.ig_product_inquiries import capture_product_inquiries, product_inquiries_for_revision
    from management.services.ig_response_plan import capture_response_plan
    rows = [row for row in (revision.bundle_snapshot or {}).get("sources") or []
        if row.get("message_id") in source_ids]
    def relevant(item):
        if item.get("source_message_id") not in source_ids:
            return False
        axis = item.get("axis")
        if axis in {"color", "size"}:
            # A zero-count M/black wish can still be this exact selected SKU.
            # It needs physical review, not another configuration inquiry.
            from management.services.ig_product_inquiries import _color
            from management.services.ig_commerce_turns import _canonical_source_size
            line = next((line for line in cart.get("lines") or []
                if line.get("line_id") == item.get("line_id")), {})
            selected = ((line.get("fields") or {}).get(axis) or {}).get("value") or line.get(axis)
            normalize = _color if axis == "color" else _canonical_source_size
            if selected and normalize(str(selected)) == normalize(str(item.get("requested_value") or "")):
                return False
            return True
        return bool(axis == "information" and re.search(
            r"нач[іеё]с|п[іо]дклад|lining|fleece", item.get("question") or "", re.I))
    # The first pure pass is only a candidate screen, with no claim that an
    # omitted catalogue is unknown. Actual classification reuses its existing
    # fresh catalogue capability; ordinary stock/price questions pay no read.
    screen = capture_product_inquiries(rows, cart_capture=cart, profile_language=revision.client.language)
    if not any(relevant(item) for item in screen.get("inquiries") or []):
        return []
    plan = capture_response_plan(revision.client, revision=revision, source_cart_capture=cart)
    actual = product_inquiries_for_revision(revision, response_plan=plan)
    return [item for item in actual.get("inquiries") or [] if relevant(item)]


def _explicit_configuration_change(inquiry):
    # A comparison/knowledge question does not withdraw a chosen black item.
    # Only an own explicit changed requirement can supersede the old scope.
    text = str(inquiry.get("question") or "")
    return bool("?" not in text and re.search(
        r"\b(?:хочу|потріб\w*|обира\w*|выбира\w*|замість|вместо|instead|choose|want|need)\b", text, re.I)
        and not re.search(r"дізнат\w*|знат\w*|узнат\w*|\b(?:know|if|maybe|perhaps)\b|можливо|возможно", text, re.I))


def build_fulfillment_callback(request, action, *, version=0):
    """Compact signed intent; actual actor and native notification receipt gate apply."""
    if action not in CALLBACK_ACTIONS or type(version) is not int or not 0 <= version <= 9999:
        raise ValueError("callback_action_invalid")
    body = f"{request.pk.hex}:{version}:{action}"
    token = CALLBACK_PREFIX + body + ":" + _sign("callback", {"body": body, "request_hmac": request.envelope_hmac}, request.key_id)[:12]
    if len(token.encode()) > 64:
        raise ValueError("callback_bound")
    return token


def parse_fulfillment_callback(value):
    match = re.fullmatch(r"FR1:([a-f0-9]{32}):(\d{1,4}):(a|13|3|7|u|o):([a-f0-9]{12})", str(value))
    if not match:
        return None
    request = Request.objects.filter(pk=uuid.UUID(hex=match[1])).first()
    if not _verified(request, "request"):
        return None
    expected = build_fulfillment_callback(request, match[3], version=int(match[2]))
    if not hmac.compare_digest(value, expected):
        return None
    return {"request_id": str(request.pk), "expected_request_digest": request.envelope_hmac,
        "version": int(match[2]), "action": match[3]}


def _queue_manager(request):
    from management.services.instagram_bot import notify_manager
    data = request.envelope
    stock_labels = {"available": "облік: є", "unavailable": "облік: недостатньо", "unknown": "облік: не підтверджено"}
    lines = "\n".join(f"{index + 1}. {line['label'][:80]}{'…' if len(line['label']) > 80 else ''} · "
        f"{stock_labels.get(line['observation'].get('status'), stock_labels['unknown'])}"
        for index, line in enumerate(data["lines"]))
    requirements = "\n".join(item.get("raw_quote", "") for item in data.get("customer_requirements", []))[:750]
    text = f"📦 Перевірка наявності та строків #{str(request.pk)[:8]}\n\n{lines}\n\nЗапит клієнта: {requirements}\n\nПідтвердьте точну кількість та строк підготовки/відправлення. Усі позиції та побажання — у картці. Прибуття перевізника не гарантується."
    buttons = [{"text": label, "callback_data": build_fulfillment_callback(request, action)}
        for label, action in (("Є: перевірити кількість", "a"), ("1–3 дні", "13"), ("3 дні", "3"), ("7 днів", "7"), ("Немає", "u"), ("Відкрити", "o"))]
    notify_manager(text, dedupe_key=f"ig-fulfillment-review:{request.pk}", event_type="fulfillment_review",
        client=request.client, reply_markup={"inline_keyboard": [buttons[:2], buttons[2:4], buttons[4:],
            [{"text": "Відкрити перевірку", "url": f"https://management.twocomms.shop/bot/?section=clients&client_id={request.client_id}&fulfillment_request={request.pk}"}]]},
        metadata={"fulfillment_request_id": str(request.pk), "request_digest": request.envelope_hmac, "decision_version": 0},
        deliver_immediately=False, raise_on_error=True)


def _create_request(client, payload, *, source=None, revision=None, now):
    from management.models import IgFollowUpTask
    identity = deepcopy(payload)
    for line in identity["lines"]:
        line["observation"].pop("captured_at", None)
        line["observation"].pop("digest", None)
    if payload.get("scope") in {"restock", "restock_list"}:
        identity = {key: payload.get(key) for key in ("scope", "client_id", "subscription_id", "answer_hmac", "settings_id", "settings_epoch", "client_epoch", "review_generation")}
        identity["lines"] = [{key: line.get(key) for key in ("line_id", "target", "configuration_digest")} for line in payload["lines"]]
    event_key = "ig-fulfillment-review:" + _digest({"client": client.pk, "payload": identity})
    task, created = IgFollowUpTask.objects.get_or_create(event_key=event_key, defaults={"client": client,
        "due_at": now, "kind": "manager_task", "status": "skipped", "reason": "fulfillment_review",
        "skip_reason": "human_business_decision_required", "trigger": "event", "event_occurred_at": now,
        "event_payload": {"origin": PURPOSE}, "policy_version": PURPOSE,
        "manager_approval_status": "pending", "manager_approval_requested_at": now})
    if not created:
        return Request.objects.filter(task=task).first(), False
    request = _seal(Request(task=task, client=client, source=source, revision=revision), "request", payload)
    task.manager_context = {"request_id": str(request.pk), "request_digest": request.envelope_hmac,
        "required_decisions": ["exact_availability", "preparation_or_dispatch"], "order_authority": False,
        "payment_authority": False, "stock_authority": False}
    if payload.get("deadline_history"):
        task.manager_context["deadline_history"] = deepcopy(payload["deadline_history"])
    task.save(update_fields=["manager_context", "updated_at"])
    _queue_manager(request)
    return request, True


def _context_requirements(requirements):
    """Latest explicit gift/recipient/logistics wishes, excluding neutral text."""
    result = {}
    for hint in requirements:
        if not isinstance(hint, dict):
            continue
        if type(hint.get("gift")) is bool:
            result["gift"] = hint["gift"]
        if isinstance(hint.get("recipient_hint"), str) and hint["recipient_hint"]:
            result["recipient_hint"] = hint["recipient_hint"]
        elif "recipient_ambiguous" in (hint.get("reasons") or ()):
            result["recipient_hint"] = "ambiguous"
        if hint.get("delivery_preference") in {"together", "split"}:
            result["delivery_preference"] = hint["delivery_preference"]
        elif "delivery_preference_ambiguous" in (hint.get("reasons") or ()):
            result["delivery_preference"] = "ambiguous"
    return result


def _historical_hard_requirements(client, cart, source_rows, settings_obj, *, now):
    """Carry a preselection cutoff only from this episode's sealed sources.

    Initial questions can predate a complete SKU. No request exists yet in that
    case. A finite original-source read bridges that gap, never another episode
    or an earlier completed purchase.
    """
    from management.models import IgCommercialEpisode, InstagramBotMessage
    from management.services.ig_fulfillment_requirements import extract_requirements, requirement_digest, deadline_supersession, global_deadline_withdrawal
    from management.services.ig_purpose_sources import owned_source
    from management.services.ig_ingress_source_proof import is_current_customer_source
    from django.db.models import Q
    scope = cart.get("scope") or {}
    # An unassigned early cutoff cannot be guessed across multiple recipients.
    if len(cart.get("lines") or []) != 1:
        return [], [], ""
    episode = IgCommercialEpisode.objects.filter(pk=scope.get("episode_id"), client_id=client.pk,
        open_slot=1, state="active").first()
    if not episode or episode.intended_order_id or (episode.sequence != 1 and not episode.opened_watermark_message_id):
        return [], [], ""
    # A current owned global withdrawal is a fresh, finite history boundary.
    # A named B instruction cannot resolve unknown earlier item requirements.
    for row in reversed(source_rows):
        if global_deadline_withdrawal(extract_requirements(row.text,
                provider_timestamp=row.provider_created_at, now=now)):
            return [], [], ""
    lower = max(scope.get("reset_floor") or 1, episode.opened_watermark_message_id + 1)
    before = min(row.pk for row in source_rows)
    # Chatter never consumes the requirement bound. This SQL candidate filter
    # only narrows a bounded read; exact source authentication and semantic
    # interpretation below continue to own every admitted requirement.
    candidate = Q(pk__in=[])
    for stem in ("leave", "leaving", "depart", "виїжджа", "від'їжджа", "від’їжджа", "уезжа",
            "deadline", "date", "дедлайн", "дат", "термін", "срок", "не потрібно до", "не нужно к"):
        candidate |= Q(text__icontains=stem)
    from management.services.ig_ingress_source_proof import VERSION as POLL_VERSION
    source_kind = Q(source="webhook") | (Q(source="poll", ingress_proof__version=POLL_VERSION)
        & Q(ingress_proof__signed__isnull=False) & ~Q(ingress_proof__signed=""))
    rows = list(InstagramBotMessage.objects.filter(candidate, source_kind, client_id=client.pk, sender_id=client.igsid,
        role="user", source__in=("webhook", "poll"), provider_namespace=scope.get("source_namespace"), pk__gte=lower, pk__lt=before,
        provider_created_at__gte=now - FRESHNESS, provider_created_at__lte=now).exclude(status="failed").select_related("client").order_by("-pk")[:65])
    sources, requirements = [], []
    boundary_found = False
    for row in rows[:64]:
        if not is_current_customer_source(row, namespace=scope.get("source_namespace")):
            # An unproved poll label is not a customer event, including a
            # purported withdrawal. It cannot erase earlier owned evidence.
            continue
        hint = extract_requirements(row.text, provider_timestamp=row.provider_created_at, now=now)
        if not (hint.get("deadline_hard") or deadline_supersession(hint)):
            continue
        frozen = row.revision_sources.select_related("revision").order_by("-revision_id").first()
        owned, _ = owned_source(frozen.revision, row, settings_obj, require_head=False) if frozen else (None, "")
        if not owned:
            return [], [], "source_proof_incomplete"
        hint["source_id"] = row.pk
        if hint.get("deadline_hard"):
            hint["requirement_digest"] = requirement_digest(hint)
        sources.append(owned[2])
        requirements.append(hint)
        if len(requirements) > 16:
            return [], [], "requirement_bound"
        if global_deadline_withdrawal(hint):
            boundary_found = True
            break
    if len(rows) > 64 and not boundary_found:
        return [], [], "candidate_bound"
    return list(reversed(sources)), list(reversed(requirements)), ""


def _prepare_fulfillment_review(revision, *, settings_obj=None, now=None):
    from management.models import IgClient
    from management.services.ig_commerce_projection import capture_current_selection_lines
    from management.services.ig_purpose_sources import owned_source
    from management.services.ig_fulfillment_requirements import extract_requirements, requirement_digest, deadline_supersession, global_deadline_withdrawal
    from management.services.ig_restock_subscriptions import is_restock_interest
    now, settings_obj = now or timezone.now(), _settings(settings_obj)
    if not getattr(settings, "IG_FULFILLMENT_REVIEWS_ENABLED", False) or not settings_obj:
        return {"created": False, "reason": "disabled", "pure": False}
    with transaction.atomic():
        client = IgClient.objects.select_for_update().get(pk=revision.client_id)
        sources, requirements, source_rows = [], [], []
        from management.services.ig_turn_revisions import MAX_SOURCES
        frozen_rows = list(revision.sources.select_related("message").order_by("ordinal")[:MAX_SOURCES + 1])
        if not frozen_rows or len(frozen_rows) > MAX_SOURCES or len(frozen_rows) != revision.source_count:
            return {"created": False, "reason": "source_bound", "pure": False}
        for frozen in frozen_rows:
            owned, reason = owned_source(revision, frozen.message, settings_obj)
            if not owned:
                return {"created": False, "reason": reason, "pure": False}
            _, row, proof = owned
            sources.append(proof)
            source_rows.append(row)
            requirement = {**extract_requirements(row.text, provider_timestamp=row.provider_created_at, now=now), "source_id": row.pk}
            if requirement.get("deadline_hard"):
                requirement["requirement_digest"] = requirement_digest(requirement)
            requirements.append(requirement)
        native_source_count = len(sources)
        own_history_reason = ""
        if len(sources) > 16:
            # Validate the complete native MAX32 ledger above, then project
            # actual fulfillment witnesses plus the latest head. Neutral chat
            # remains in the immutable revision, rather than consuming all
            # sixteen signed review proof slots.
            relevant = [hint for hint in requirements if hint.get("deadline_hard")
                or hint.get("request_detected") or hint.get("custom_print_hint")
                or deadline_supersession(hint) or _context_requirements([hint])]
            withdrawals = [hint["source_id"] for hint in relevant if global_deadline_withdrawal(hint)]
            if withdrawals:
                relevant = [hint for hint in relevant if hint["source_id"] >= max(withdrawals)]
            latest_id = max(row.pk for row in source_rows)
            selected = {hint["source_id"] for hint in relevant} | {latest_id}
            if len(selected) > 16:
                own_history_reason = "own_requirement_bound"
                selected = {hint["source_id"] for hint in relevant[:15]} | {latest_id}
            sources = [proof for proof in sources if proof["message_id"] in selected]
            requirements = [hint for hint in requirements if hint["source_id"] in selected]
        context_requirements = _context_requirements(requirements)
        own_requirements = list(requirements)
        # Ordinary gifts and multiple beneficiaries are retail selection
        # context, not reasons to stop an otherwise available purchase.
        explicit = bool(context_requirements.get("delivery_preference")) or any(hint.get("request_detected") or hint.get("custom_print_hint") or hint.get("supersession_hint") in {"changed", "withdrawn"} for hint in requirements)
        from management.services.ig_business_cooperation import business_cooperation_for_sources
        business = business_cooperation_for_sources([
            {"message_id": row.pk, "role": "user", "text": row.text} for row in source_rows])
        # A product mentioned for resale is not a retail selection requesting
        # stock/preparation review. Mixed messages with an actual fulfillment
        # requirement retain the same review and source authority as before.
        own_availability = any(is_restock_interest(row.text) or (
            re.search(r"\b(?:мо[яєюї]|мій|мого|моя|мою|мое|моё|мой|моего|my|our|замовлен\w*|заказ\w*|ця|цей|це|эта|этот|this|that|XS|S|M|L|XL|XXL|[2-6]XL)\b", row.text, re.I)
            and re.search(r"(?:наявн|налич|доступн|availab|in\s+stock)", row.text, re.I)
            and "?" in row.text) for row in source_rows)
        if business and not explicit and not own_availability:
            return {"created": False, "reason": "no_review_requirement", "pure": False}
        cart = capture_current_selection_lines(client.pk, now=now)
        if cart.get("status") != "captured" or not cart.get("coverage_complete") or not cart.get("lines"):
            return {"created": False, "reason": "source_cart_unavailable", "pure": False}
        own_sources = list(sources)
        historical_sources, historical_requirements, history_reason = _historical_hard_requirements(client, cart, source_rows, settings_obj, now=now)
        history_reason = own_history_reason or history_reason
        explicit = explicit or bool(history_reason)
        if historical_requirements:
            sources = list({proof["message_id"]: proof for proof in [*historical_sources, *sources]}.values())
            requirements = [*historical_requirements, *requirements]
            context_requirements = _context_requirements(requirements)
            explicit = explicit or any(hint.get("deadline_hard") for hint in historical_requirements)
        lines = _lines(cart, max(row.pk for row in source_rows), now)
        # Partial product selection is not an out-of-stock incident. Automatic
        # checks start only after an exact target exists; an explicit customer
        # timing/availability question can still request clarification.
        source_ids = {row.pk for row in source_rows}
        selection_requested = any(proof.get("source_message_id") in source_ids for line in lines
            for proof in line["preferences"].get("evidence", {}).values())
        from management.services.ig_commerce_turns import _current_source_text
        notification_optin_requested = any(is_restock_interest(row.text)
            and is_restock_interest(_current_source_text(row.text)) for row in source_rows)
        notification_only_explicit = notification_optin_requested and not explicit
        # Requested future notification keeps its own signed physical-review
        # workflow. Current usable inventory alone cannot grant that provenance.
        explicit = explicit or notification_optin_requested
        availability_requested = any((re.search(r"(?:наявн|налич|доступн|availab|in\s+stock)", row.text, re.I) and "?" in row.text)
            or is_restock_interest(row.text)
            for row in source_rows)
        if len(lines) == 1 and lines[0]["target"] and lines[0]["configuration_digest"]:
            from management.services.ig_response_cart_plan import stock_question
            selected = lines[0]
            values = selected["preferences"].get("values") or {}
            labels = selected["observation"].get("labels") or {}
            availability_requested = availability_requested or any(stock_question(row.text,
                size=selected["target"].get("size", ""), color=values.get("color") or labels.get("color", ""),
                title=labels.get("product", "")) for row in source_rows)
        prior = Request.objects.filter(client=client, source__isnull=False).order_by(
            "-created_at", "-source_id", "-pk").first()
        if (history_reason and prior and _verified(prior, "request")
                and (prior.envelope.get("deadline_history") or {}).get("reason") == history_reason
                and prior.envelope.get("cart_identity") == _cart_identity(cart)
                and not _current_reason(prior, settings_obj, now=now, current_source_cart=cart)):
            return {"created": False, "task_id": prior.task_id, "request_id": str(prior.pk),
                "request_digest": prior.envelope_hmac, "ack_facts": {"review_requested": True,
                    "arrival_guaranteed": False, "deadline_clarification_required": True},
                "pure": False, "reason": "deadline_history_clarification_required"}
        if historical_requirements and prior and not any(hint.get("request_detected")
                or hint.get("custom_print_hint") or hint.get("supersession_hint") != "none" for hint in own_requirements):
            from management.services.ig_fulfillment_requirements import active_hard_requirements
            old_hard = active_hard_requirements(prior.envelope.get("customer_requirements") or [])
            current_hard = active_hard_requirements(requirements)
            if (old_hard and [requirement_digest(hint) for hint in old_hard] == [requirement_digest(hint) for hint in current_hard]
                and _verified(prior, "request") and prior.envelope.get("cart_identity") == _cart_identity(cart)
                and not _current_reason(prior, settings_obj, now=now, current_source_cart=cart)):
                return {"created": False, "task_id": prior.task_id, "request_id": str(prior.pk),
                    "request_digest": prior.envelope_hmac, "ack_facts": {"review_requested": True, "arrival_guaranteed": False},
                    "pure": False, "reason": "unchanged_hard_deadline"}
        from management.services.ig_inventory_availability import capture_inventory_availability
        inventory = (capture_inventory_availability(client.pk, current_source_cart=cart, now=now)
            if availability_requested or selection_requested else {})
        available_ids = {row["line_id"] for row in inventory.get("lines") or [] if row.get("status") == "available"}
        if inventory:
            from management.services.ig_inventory_availability import owned_checkout_capacity_line_ids
            available_ids.update(owned_checkout_capacity_line_ids(client.pk, current_source_cart=cart,
                inventory_capture=inventory, now=now))
        exact_ids = {line["line_id"] for line in lines if line["target"] and line["configuration_digest"]}
        physical_confirmation_requested = bool((availability_requested or selection_requested) and exact_ids - available_ids)
        if availability_requested and not exact_ids:
            physical_confirmation_requested = True
        inquiries = (_unverified_configuration_inquiries(revision, cart, source_ids)
            if availability_requested or selection_requested else [])
        if any(_explicit_configuration_change(item) or re.search(
                r"наявн|налич|доступн|availab|in\s+stock", item.get("question") or "", re.I)
                for item in inquiries):
            # The existing product-configuration/information review owns the
            # exact unlisted wish. Do not create a physical check for the old
            # selected SKU and accidentally answer another colour/lining.
            return {"created": False, "reason": "desired_configuration_requires_review", "pure": False}
        if not explicit and exact_ids and exact_ids.issubset(available_ids) and not physical_confirmation_requested:
            return {"created": False, "reason": "inventory_available", "pure": False}
        if not explicit and physical_confirmation_requested and available_ids:
            # Review only unresolved exact goods. Positive siblings continue
            # normal selection and independent questions under inventory facts.
            lines = [line for line in lines if line["line_id"] not in available_ids]
        if not explicit and not physical_confirmation_requested:
            if (prior and _verified(prior, "request")
                    and prior.envelope.get("cart_identity") == _cart_identity(cart)
                    and not _current_reason(prior, settings_obj, now=now, current_source_cart=cart)):
                return {"created": False, "task_id": prior.task_id, "request_id": str(prior.pk),
                    "request_digest": prior.envelope_hmac, "pure": False,
                    "reason": "existing_physical_review"}
            return {"created": False, "reason": "no_review_requirement", "pure": False}
        # A new availability question keeps an earlier deadline only through
        # its original sealed sources and an identical purchase selection.
        changes_requirement = any(hint.get("request_detected") or hint.get("supersession_hint") in {"changed", "withdrawn"} for hint in requirements)
        if (prior and _verified(prior, "request") and prior.envelope.get("cart_identity") == _cart_identity(cart)
                and context_requirements and not changes_requirement
                and not any(hint.get("custom_print_hint") for hint in requirements)
                and all(_context_requirements(prior.envelope.get("customer_requirements") or []).get(key) == value
                    for key, value in context_requirements.items())
                and not _current_reason(prior, settings_obj, now=now, current_source_cart=cart)):
            return {"created": False, "task_id": prior.task_id, "request_id": str(prior.pk),
                "request_digest": prior.envelope_hmac, "ack_facts": {"review_requested": True, "arrival_guaranteed": False},
                "pure": False, "reason": "unchanged_customer_requirements"}
        if (prior and (not explicit or notification_only_explicit) and (not notification_optin_requested or (
                    prior.source_id == max(row.pk for row in source_rows) and prior.revision_id == revision.pk))
                and not changes_requirement and _verified(prior, "request")
                and prior.envelope.get("cart_identity") == _cart_identity(cart)
                and prior.created_at <= now <= prior.created_at + FRESHNESS
                and not _current_reason(prior, settings_obj, now=now, current_source_cart=cart)):
            previous_lines = prior.envelope.get("lines") or []
            previous_by_id = {line["line_id"]: line for line in previous_lines}
            same_configuration = len(previous_by_id) == len(lines) and all(
                line["line_id"] in previous_by_id
                and all(previous_by_id[line["line_id"]].get(key) == line.get(key)
                    for key in ("target", "configuration_digest")) for line in lines)
            previous_decision = Decision.objects.filter(request=prior).order_by("-version").first()
            if same_configuration and (previous_decision is None or not _decision_reason(
                    prior, previous_decision, now=now, current_source_cart=cart)):
                return {"created": False, "task_id": prior.task_id, "request_id": str(prior.pk),
                    "request_digest": prior.envelope_hmac,
                    "ack_facts": {"review_requested": True, "arrival_guaranteed": False},
                    "pure": False, "reason": "unchanged_physical_confirmation"}
        if not history_reason and prior and _verified(prior, "request") and prior.envelope.get("cart_identity") == _cart_identity(cart) and not changes_requirement:
            from management.models import IgCustomerTurnRevision, InstagramBotMessage
            inherited = []
            for proof in prior.envelope["sources"]:
                old_revision = IgCustomerTurnRevision.objects.filter(pk=proof["revision_id"]).first()
                old_source = InstagramBotMessage.objects.filter(pk=proof["message_id"]).first()
                owned = owned_source(old_revision, old_source, settings_obj, require_head=False)[0] if old_revision and old_source else None
                if not owned or owned[2] != proof:
                    inherited = []
                    break
                inherited.append(proof)
            if inherited:
                sources = list({proof["message_id"]: proof for proof in [*inherited, *sources]}.values())
                requirements = [*deepcopy(prior.envelope["customer_requirements"]), *requirements]
        if len(sources) > 16 or len(requirements) > 16:
            history_reason = "requirement_bound"
        if history_reason:
            # The signed marker carries uncertainty instead of fabricating an
            # absent deadline or retaining an arbitrary subset of its proofs.
            sources, requirements = own_sources, own_requirements
        scope = cart.get("scope") or {}
        from management.services.ig_reply_language import resolve_own_source_reply_language
        own_rows = [{"message_id": row.pk, "role": "user", "text": row.text,
            "provider_created_at": row.provider_created_at.isoformat()} for row in source_rows]
        latest_source = max(own_rows, key=lambda row: row["message_id"])
        language = resolve_own_source_reply_language(sources=[latest_source],
            history=[row for row in own_rows if row is not latest_source], profile_language=client.language,
            reset_floor=scope.get("reset_floor") or 1, watermark_message_id=latest_source["message_id"],
            watermark_event_at=now)
        payload = {"scope": "cart", "source_namespace": sources[0]["namespace"], "source_id": max(row.pk for row in source_rows),
            "client_id": client.pk, "locale": language.template_family if language.template_family in {"uk", "ru", "en"} else language.knowledge_locale,
            "reply_language": language.as_dict(),
            "settings_id": settings_obj.pk, "settings_epoch": settings_obj.reply_permission_epoch,
            "client_epoch": client.reply_permission_epoch, "sources": sources, "customer_requirements": requirements,
            "lines": lines, "cart_identity": _cart_identity(cart), "source_scope": {**{key: scope.get(key) for key in ("client_id", "episode_id", "reset_floor")}, "source_namespace": sources[0]["namespace"],
                "selection_session_id": cart.get("session_id"), "selection_generation": cart.get("generation"),
                "selection_revision": cart.get("selection_revision"), "source_cart_capture_digest": cart.get("capture_digest")},
            "source_refs": [{"message_id": item["message_id"], "event_at": item["provider_created_at"], "digest": item["digest"]} for item in sources]}
        if history_reason:
            payload["deadline_history"] = {"status": "incomplete", "reason": history_reason,
                "needs_customer_clarification": True,
                "guidance": "Earlier deadline requirements could not be fully verified. Explain that checkout needs "
                    "deadline clarification; ask whether the customer explicitly withdraws ALL earlier deadlines. "
                    "An unrelated item change or manager stock review cannot clear this hold."}
        if native_source_count > 16:
            payload["source_bundle"] = {"revision_id": revision.pk, "revision_digest": revision.snapshot_digest,
                "source_count": native_source_count, "review_witness_count": len(own_sources)}
        request, created = _create_request(client, payload, source=source_rows[-1], revision=revision, now=now)
        return {"created": created, "task_id": request.task_id, "request_id": str(request.pk),
            "request_digest": request.envelope_hmac, "ack_facts": {"review_requested": True, "arrival_guaranteed": False,
                **({"deadline_clarification_required": True} if history_reason else {})},
            "pure": False, "reason": "review_requested"}


def prepare_fulfillment_review(revision, *, settings_obj=None, now=None):
    result = _prepare_fulfillment_review(revision, settings_obj=settings_obj, now=now)
    return {**result, "ready": bool(result.get("request_id"))}


def _batch_cart_sources_reason(request, settings_obj, cart, *, now, _permission_checked=False,
        _deadline_hold=False, _deadline_hold_line_ids=(), _source_excerpts=None):
    """Two bounded actual-row reads, including every inherited revision source."""
    from django.db.models import Q
    from management.models import IgTurnRevisionSource, InstagramBotMessage
    from management.services.ig_turn_revisions import _digest as revision_digest, _source_payload
    from management.services.ig_ingress_source_proof import is_current_customer_source
    from management.services.instagram_bot import ingress_provider_namespace
    from management.services.ig_fulfillment_requirements import extract_requirements
    data, client = request.envelope, request.client
    proofs = data.get("sources")
    floor = (cart.get("scope") or {}).get("reset_floor")
    namespace = ingress_provider_namespace(settings_obj)
    if (not isinstance(proofs, list) or not 1 <= len(proofs) <= 16 or type(floor) is not int
        or namespace != data.get("source_namespace") or cart.get("status") != "captured"
        or not cart.get("coverage_complete") or (cart.get("scope") or {}).get("client_id") != client.pk):
        return "source_scope_changed"
    pairs = [(proof.get("revision_id"), proof.get("message_id")) for proof in proofs if isinstance(proof, dict)]
    if len(pairs) != len(proofs) or len(set(pairs)) != len(pairs) or any(type(value) is not int for pair in pairs for value in pair):
        return "source_proof_invalid"
    predicate = Q(pk__in=[])
    for revision_id, message_id in pairs:
        predicate |= Q(revision_id=revision_id, message_id=message_id)
    query = IgTurnRevisionSource.objects.filter(predicate).select_related("revision", "message", "message__client").order_by("pk")

    def capture():
        rows = list(query[:17])
        if len(rows) != len(proofs):
            return None, "source_changed"
        by_pair = {(row.revision_id, row.message_id): row for row in rows}
        signatures = []
        for proof in proofs:
            frozen = by_pair.get((proof["revision_id"], proof["message_id"]))
            if not frozen:
                return None, "source_changed"
            row, revision = frozen.message, frozen.revision
            snapshot = next((item for item in revision.bundle_snapshot.get("sources", ()) if item.get("message_id") == row.pk), None)
            body = _source_payload(row, previous=frozen, ordinal=frozen.ordinal)
            if (revision.client_id != client.pk or not revision.snapshot_digest
                or revision_digest(revision.bundle_snapshot) != revision.snapshot_digest
                or revision.snapshot_digest != proof.get("revision_digest")
                or revision.permission_epoch != client.reply_permission_epoch
                or revision.erasure_started_at_snapshot != client.privacy_erasure_started_at
                or row.client_id != client.pk or row.sender_id != client.igsid or row.role != "user"
                or not is_current_customer_source(row, namespace=namespace)
                or row.status == "failed" or row.pk < floor
                or not row.mid or not row.provider_created_at or timezone.is_naive(row.provider_created_at)
                or row.provider_namespace != namespace or frozen.source_namespace != namespace
                or frozen.source_digest != proof.get("digest") or body["source_digest"] != frozen.source_digest
                or not snapshot or snapshot.get("source_digest") != frozen.source_digest
                or proof.get("namespace") != namespace or proof.get("mid") != row.mid
                or proof.get("provider_created_at") != row.provider_created_at.isoformat() or proof.get("reset_floor") != floor):
                return None, "source_changed"
            signatures.append({"body": body, "client_id": row.client_id, "sender_id": row.sender_id,
                "source": row.source, "status": row.status, "frozen_id": frozen.pk, "revision_digest": revision.snapshot_digest,
                "permission_epoch": revision.permission_epoch, "erasure": str(revision.erasure_started_at_snapshot)})
        return signatures, ""

    first, reason = capture()
    if reason:
        return reason
    watermark = cart.get("source_watermark") or (cart.get("fence") or {}).get("source_watermark") or {}
    cutoff = watermark.get("message_id")
    if type(cutoff) is not int or cutoff < data["source_id"]:
        return "source_head_changed"
    later_query = InstagramBotMessage.objects.filter(client_id=client.pk, sender_id=client.igsid,
        role="user", pk__gt=data["source_id"], pk__lte=cutoff, provider_namespace=namespace,
        source__in=["webhook", "poll"]).exclude(status="failed").order_by("pk")
    later = list(later_query[:17]) if cutoff > data["source_id"] else []
    if len(later) > 16:
        if _deadline_hold:
            second, reason = capture()
            return reason or ("review_source_read_conflict" if first != second else "deadline_continuation_bound")
        return "source_continuation_bound"
    captured_context = _context_requirements(data.get("customer_requirements") or [])
    for row in later:
        from management.services.ig_split_shipping import is_authenticated_choice_source
        if is_authenticated_choice_source(request, row, settings_obj=settings_obj, now=now,
                _permission_checked=_permission_checked):
            continue
        hint = extract_requirements(row.text, provider_timestamp=row.provider_created_at, now=now)
        if _deadline_hold:
            # A newer stock/gift/cart question can replace a presentation
            # request without withdrawing the exact earlier item's cutoff.
            # Only a newly sealed own deadline change/withdrawal supersedes
            # that negative hold. Unsealed or changed text grants no release.
            from management.services.ig_fulfillment_requirements import deadline_supersession
            supersession = deadline_supersession(hint)
            if supersession == "withdrawn" or supersession == "changed" and hint.get("deadline_hard") is True:
                from management.services.ig_purpose_sources import owned_source
                from management.services.ig_fulfillment_checkout import _deadline_requirement_update_applies
                frozen = row.revision_sources.select_related("revision").order_by("-revision_id").first()
                owned = owned_source(frozen.revision, row, settings_obj, require_head=False)[0] if frozen else None
                if owned and _deadline_requirement_update_applies(request, row, cart, now=now,
                        affected_line_ids=_deadline_hold_line_ids):
                    return "requirements_superseded"
            continue
        if not _deadline_hold:
            # Screen without queries. Neutral/price continuations must not add
            # source-by-source reads to the existing bounded state capture.
            from management.services.ig_product_inquiries import capture_product_inquiries
            candidate = capture_product_inquiries([{"message_id": row.pk, "role": "user", "text": row.text,
                "provider_created_at": row.provider_created_at.isoformat(), "source_digest": ""}],
                cart_capture=cart, profile_language=client.language)
            changed_configuration = any(item.get("axis") in {"color", "size"}
                and _explicit_configuration_change(item) for item in candidate.get("inquiries") or [])
            if changed_configuration:
                frozen = row.revision_sources.select_related("revision").order_by("-revision_id").first()
                if frozen:
                    from management.services.ig_purpose_sources import owned_source
                    owned, _ = owned_source(frozen.revision, row, settings_obj, require_head=False)
                    if owned and any(_explicit_configuration_change(item) for item in
                            _unverified_configuration_inquiries(frozen.revision, cart, {row.pk})):
                        return "requirements_superseded"
        updated_context = _context_requirements([hint])
        if (hint.get("request_detected") or hint.get("custom_print_hint") or hint.get("supersession_hint") != "none"
                or any(captured_context.get(key) != value for key, value in updated_context.items())):
            return "requirements_superseded"
    second, reason = capture()
    if reason or first != second:
        return reason or "review_source_read_conflict"
    if later:
        signature = lambda rows: [(row.pk, row.client_id, row.sender_id, row.source, row.status,
            _source_payload(row, ordinal=0)) for row in rows]
        if signature(later) != signature(list(later_query[:17])):
            return "review_source_read_conflict"
    if _source_excerpts is not None:
        _source_excerpts.extend({"message_id": item["body"]["message_id"],
            "text": item["body"]["text"][:350],
            "event_at": item["body"]["provider_created_at"],
            "truncated": len(item["body"]["text"]) > 350} for item in second)
    return ""


def _current_reason(request, settings_obj, *, now, current_source_cart=None, _settings_fresh=False,
        _deadline_hold=False, _deadline_hold_line_ids=(), _source_excerpts=None):
    from management.services.ig_purpose_sources import owned_source, latest_customer_source
    from management.services.ig_commerce_projection import capture_current_selection_lines
    from management.services.ig_reply_boundary import capture_reply_permission
    if not _verified(request, "request"):
        return "request_evidence_invalid"
    settings_obj = settings_obj if _settings_fresh else _settings(settings_obj)
    if not settings_obj:
        return "settings_missing"
    data, client = request.envelope, request.client
    if not _enabled() or settings_obj.pk != data["settings_id"]:
        return "disabled_or_settings_changed"
    if current_source_cart is not None and data["scope"] == "cart":
        from management.services.ig_reply_boundary import _client_allowed
        from management.services.ig_permission_transitions import permission_transition_blocks
        from management.services.instagram_bot import allowed_sender_ids, ingress_provider_namespace
        from management.services.ig_revision_echo_integration import uses_revision_echo_scope
        from management.services.ig_revision_echo import revision_echo_blocks
        allowed_senders = allowed_sender_ids(settings_obj)
        if (not settings_obj.is_enabled or not _client_allowed(client)[0]
            or settings_obj.reply_permission_epoch != data["settings_epoch"] or client.reply_permission_epoch != data["client_epoch"]
            or (allowed_senders and client.igsid not in allowed_senders)
            or permission_transition_blocks(settings_id=settings_obj.pk, client_id=client.pk)
            or (uses_revision_echo_scope(ingress_provider_namespace(settings_obj), client.igsid)
                and revision_echo_blocks(client.pk, ingress_provider_namespace(settings_obj)))):
            return "permission_changed"
    else:
        permission = capture_reply_permission(settings_obj.pk, client.pk)
        if not permission or permission.settings_epoch != data["settings_epoch"] or permission.client_epoch != data["client_epoch"]:
            return "permission_changed"
    if data["scope"] in {"restock_list", "restock_list_invitation"}:
        from management.services.ig_restock_waiting_list_review import current_review_reason
        return current_review_reason(request, settings_obj, now=now)
    if data["scope"] == "restock":
        from management.ig_restock_subscription_models import IgRestockSubscription
        from management.services.ig_restock_subscriptions import _subscription_reason
        subscription = IgRestockSubscription.objects.filter(pk=data["subscription_id"]).select_related("accepted_answer__invitation", "accepted_answer__receipt__effect", "accepted_answer__source").first()
        if not subscription or _subscription_reason(subscription, settings_obj, now=now):
            return "subscription_changed"
        if subscription.accepted_answer.envelope_hmac != data["answer_hmac"]:
            return "subscription_changed"
        return ""
    if current_source_cart is not None:
        reason = _batch_cart_sources_reason(request, settings_obj, current_source_cart, now=now,
            _permission_checked=True, _deadline_hold=_deadline_hold,
            _deadline_hold_line_ids=_deadline_hold_line_ids, _source_excerpts=_source_excerpts)
        if reason:
            return reason
        return "" if _cart_identity(current_source_cart) == data["cart_identity"] else "selection_changed"
    cart = capture_current_selection_lines(client.pk, now=now)
    reason = _batch_cart_sources_reason(request, settings_obj, cart, now=now, _permission_checked=True,
        _source_excerpts=_source_excerpts)
    if reason:
        return reason
    return "" if _cart_identity(cart) == data["cart_identity"] else "selection_changed"


def _timing(value, now):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) not in ({"min_days", "max_days", "unit"}, {"min_days", "max_days", "unit", "anchor"}):
        raise ValueError("timing_schema_invalid")
    low, high = value["min_days"], value["max_days"]
    if type(low) is not int or type(high) is not int or not 0 <= low <= high <= 90 or value["unit"] not in {"calendar", "business"}:
        raise ValueError("timing_range_invalid")
    if "anchor" in value:
        if value["anchor"] != "after_payment_confirmation":
            raise ValueError("timing_anchor_invalid")
        return deepcopy(value)
    return {**value, "anchor_at": now.isoformat()}


def _base_arrival(value, *, availability, basis, now):
    """Incoming physical bases, distinct from preparation and dispatch."""
    if value is None:
        return None
    if (availability != "wait" or basis != "incoming_confirmed"
            or not isinstance(value, dict) or set(value) != {"min_days", "max_days", "unit"}):
        raise ValueError("base_arrival_scope_invalid")
    return _timing(value, now)


def normalize_deadline_review(value, request):
    """Bind a negative/pending decision to exact source requirements and lines.

    This deliberately has no positive arrival-feasibility state. Source quotes,
    notes and blank capacity never authorize a finished-product promise.
    """
    from management.services.ig_fulfillment_requirements import requirement_digest, active_hard_requirements
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"requirement_digest", "outcome", "line_ids", "options"}
        or value.get("outcome") not in {"impossible", "review_pending"}):
        raise ValueError("deadline_review_schema_invalid")
    digest = value.get("requirement_digest")
    requirements = active_hard_requirements(request.envelope.get("customer_requirements") or [])
    matches = [hint for hint in requirements if isinstance(hint, dict) and hint.get("deadline_hard") is True
        and hint.get("supersession_hint") != "withdrawn" and requirement_digest(hint) == digest]
    if len(matches) != 1:
        raise ValueError("deadline_requirement_changed")
    expected = {line["line_id"] for line in request.envelope.get("lines") or []}
    lines, options = value.get("line_ids"), value.get("options")
    if (not isinstance(lines, list) or not lines or any(not isinstance(item, str) for item in lines)
        or len(set(lines)) != len(lines) or not set(lines).issubset(expected)):
        raise ValueError("deadline_line_scope_invalid")
    allowed = {"later_domestic_contact", "later_domestic_destination", "international_review_required"}
    if (not isinstance(options, list) or any(not isinstance(item, str) or item not in allowed for item in options)
        or len(set(options)) != len(options)):
        raise ValueError("deadline_options_invalid")
    return deepcopy(value)


def _decisions(value, request, now):
    if request.envelope.get("scope") in {"restock_list", "restock_list_invitation"}:
        from management.services.ig_restock_waiting_list_review import normalize_decisions
        return normalize_decisions(value, request, now)
    from management.services.ig_restock_availability import observe_target
    if isinstance(value, dict):
        if set(value) - {"lines", "split_offer", "deadline_review"}:
            raise ValueError("decision_schema_invalid")
        rows, split = value.get("lines"), value.get("split_offer")
    else:
        rows, split = value, None
    expected = {line["line_id"]: line for line in request.envelope["lines"]}
    if (not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("line_id"), str) for row in rows)
        or len(rows) != len(expected) or {row["line_id"] for row in rows} != set(expected)):
        raise ValueError("decision_line_coverage_invalid")
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"line_id", "availability", "checked_quantity", "preparation", "dispatch", "alternative", "clarification", "supply_basis", "source_location", "base_arrival"}:
            raise ValueError("decision_schema_invalid")
        availability = row.get("availability")
        if availability not in {"available", "unavailable", "wait", "clarify"}:
            raise ValueError("availability_invalid")
        target = expected[row["line_id"]]["target"]
        observed = observe_target(target, now=now).as_dict()
        configuration = _configuration(target, now=now)
        checked = row.get("checked_quantity")
        basis = row.get("supply_basis")
        if basis is not None and basis not in {"warehouse_checked", "accessible_unrecorded", "incoming_confirmed"}:
            raise ValueError("supply_basis_invalid")
        if checked is not None and (type(checked) is not int or not 0 <= checked <= 999):
            raise ValueError("checked_quantity_invalid")
        if availability == "available" and (basis not in {"warehouse_checked", "accessible_unrecorded"}
            or configuration.get("status") != "valid" or configuration.get("target") != target or not target
            or type(checked) is not int or checked < target.get("quantity", 1000)):
            raise ValueError("physical_availability_unverified")
        if row.get("base_arrival") is not None and (not target or configuration.get("status") != "valid"
                or configuration.get("target") != target):
            raise ValueError("base_arrival_configuration_unverified")
        base_arrival = _base_arrival(row.get("base_arrival"), availability=availability, basis=basis, now=now)
        clean = {}
        for name in ("alternative", "clarification", "source_location"):
            text = row.get(name, "")
            if not isinstance(text, str) or len(text) > 350 or re.search(r"[\x00-\x1f\x7f]", text):
                raise ValueError("decision_note_invalid")
            clean[name] = text
        result.append({"line_id": row["line_id"], "availability": availability, "checked_quantity": checked,
            "supply_basis": basis,
            "accounting_recount_required": availability == "available" and observed.get("status") != "available",
            "configuration_digest": configuration.get("configuration_digest", ""),
            "preparation": _timing(row.get("preparation"), now), "dispatch": _timing(row.get("dispatch"), now),
            "inventory_digest": inventory_digest(observed),
            **({"base_arrival": base_arrival} if base_arrival is not None else {}), **clean})
    if split is not None:
        if not isinstance(split, dict) or set(split) != {"groups", "extra_delivery_amount", "payer", "feasible"}:
            raise ValueError("split_offer_schema_invalid")
        groups = split["groups"]
        if not isinstance(groups, list) or not 2 <= len(groups) <= len(expected) or any(not isinstance(group, list) or not group for group in groups):
            raise ValueError("split_groups_invalid")
        flat = [line for group in groups for line in group]
        if any(not isinstance(line, str) for line in flat) or len(flat) != len(expected) or len(set(flat)) != len(flat) or set(flat) != set(expected):
            raise ValueError("split_groups_invalid")
        try:
            amount = Decimal(split["extra_delivery_amount"])
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("split_cost_invalid")
        if not isinstance(split["extra_delivery_amount"], str) or not amount.is_finite() or not 0 <= amount <= 100000 or amount.as_tuple().exponent < -2:
            raise ValueError("split_cost_invalid")
        if split["payer"] not in {"customer", "merchant"} or split["feasible"] is not True:
            raise ValueError("split_feasibility_invalid")
    return result, deepcopy(split)


def _telegram_gate(request, receipt, latest_version):
    from management.models import IgBotNotification
    if not isinstance(receipt, dict) or set(receipt) - {"chat_id", "message_id", "version"} or receipt.get("version") != latest_version:
        return False
    notification = IgBotNotification.objects.filter(dedupe_key=f"ig-fulfillment-review:{request.pk}", status="sent").first()
    if not notification or not notification.sent_at:
        return False
    payload = notification.payload or {}
    mid = str(payload.get("main_delivery_message_id") or notification.telegram_message_id)
    return bool(mid and str(receipt.get("message_id", "")) == mid and str(receipt.get("chat_id", "")) == str(payload.get("chat_id", "")))


def handle_fulfillment_callback(data, *, actor, chat_id, message_id, operation_id, now=None):
    from management.bot_access import has_all_bot_capabilities, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
    if not has_all_bot_capabilities(actor, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION):
        return {"ready": False, "reason": "actor_forbidden"}
    parsed = parse_fulfillment_callback(data)
    if not parsed:
        return {"ready": False, "reason": "callback_invalid"}
    request = Request.objects.filter(pk=parsed["request_id"]).first()
    receipt = {"chat_id": str(chat_id), "message_id": str(message_id), "version": parsed["version"]}
    if not _telegram_gate(request, receipt, parsed["version"]):
        return {"ready": False, "reason": "telegram_receipt_unverified"}
    # A preset does not guess a physical count or whether days refer to making
    # or dispatch. The authenticated form collects these indispensable facts.
    if parsed["action"] != "u":
        return {"ready": True, "reason": "manager_form_required", "request_id": str(request.pk),
            "action": parsed["action"], "request_digest": request.envelope_hmac}
    rows = [{"line_id": line["line_id"], "availability": "unavailable"} for line in request.envelope["lines"]]
    result = apply_fulfillment_decision(request.pk, actor=actor, expected_request_digest=request.envelope_hmac,
        operation_id=operation_id, decisions_by_line=rows, telegram_receipt=receipt, now=now)
    return {**result, "ready": result.get("ok", False)}


def _apply_fulfillment_decision(request_id, *, actor, expected_request_digest, operation_id, decisions_by_line, now=None, telegram_receipt=None):
    from management.models import IgClient, IgFollowUpTask, AdminAuditLog
    from management.bot_access import has_all_bot_capabilities, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
    now = now or timezone.now()
    if not has_all_bot_capabilities(actor, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION):
        return {"ok": False, "reason": "actor_forbidden"}
    try:
        operation_id = uuid.UUID(str(operation_id))
        request_id = uuid.UUID(str(request_id))
    except ValueError:
        return {"ok": False, "reason": "identity_invalid"}
    with transaction.atomic():
        request = Request.objects.select_related("client", "revision").filter(pk=request_id).first()
        if not request:
            return {"ok": False, "reason": "request_missing"}
        IgClient.objects.select_for_update().get(pk=request.client_id)
        request = Request.objects.select_for_update().select_related("client", "revision").get(pk=request_id)
        if not _verified(request, "request") or not hmac.compare_digest(str(expected_request_digest), request.envelope_hmac):
            return {"ok": False, "reason": "request_digest_changed"}
        previous = Decision.objects.filter(operation_id=operation_id).first()
        if previous:
            if (previous.request_id != request.pk or previous.actor_id != actor.pk or not _verified(previous, "decision")
                or previous.envelope.get("decision_input_digest") != _digest(decisions_by_line)):
                return {"ok": False, "reason": "operation_conflict"}
            return {"ok": True, "created": False, "decision_id": str(previous.pk), "version": previous.version}
        settings_obj = _settings()
        if not settings_obj or settings_obj.pk != request.envelope["settings_id"]:
            return {"ok": False, "reason": "settings_changed"}
        reason = _current_reason(request, settings_obj, now=now)
        if reason:
            return {"ok": False, "reason": reason}
        latest = Decision.objects.filter(request=request).order_by("-version").first()
        version = latest.version + 1 if latest else 1
        if telegram_receipt is not None and not _telegram_gate(request, telegram_receipt, version - 1):
            return {"ok": False, "reason": "telegram_receipt_unverified"}
        try:
            lines, split = _decisions(decisions_by_line, request, now)
            deadline_review = normalize_deadline_review(decisions_by_line.get("deadline_review"), request) if isinstance(decisions_by_line, dict) else None
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        if latest:
            prior_deadline = latest.envelope.get("deadline_review") or {}
            # Resolve one pending deadline by appending evidence. No correction
            # of immutable stock/timing facts is smuggled through this path.
            prior_lines = deepcopy(latest.envelope.get("lines"))
            def without_check_anchors(rows):
                rows = deepcopy(rows)
                for row in rows or []:
                    for field in ("base_arrival", "preparation", "dispatch"):
                        if isinstance(row.get(field), dict):
                            row[field].pop("anchor_at", None)
                return rows
            resolves_pending = bool(_verified(latest, "decision") and deadline_review
                and prior_deadline.get("outcome") == "review_pending" and deadline_review.get("outcome") == "impossible"
                and prior_deadline.get("requirement_digest") == deadline_review.get("requirement_digest")
                and prior_deadline.get("line_ids") == deadline_review.get("line_ids")
                and without_check_anchors(prior_lines) == without_check_anchors(lines)
                and latest.envelope.get("split_offer") == split)
            if not resolves_pending:
                return {"ok": False, "reason": "decision_already_recorded"}
            lines = prior_lines
        decision = _seal(Decision(request=request, actor=actor, operation_id=operation_id, version=version), "decision",
            {"request_digest": request.envelope_hmac, "decision_input_digest": _digest(decisions_by_line), "decided_at": now.isoformat(), "lines": lines,
                "split_offer": split, "arrival_guaranteed": False, "order_authority": False,
                "stock_authority": False, "payment_authority": False,
                **({"waiting_acceptance_required": True} if any(line["availability"] == "wait" for line in lines) else {}),
                **({"deadline_review": deadline_review} if deadline_review is not None else {})})
        task = request.task
        task.manager_approval_status = "approved"
        task.manager_approval_actor = actor
        task.manager_approval_decided_at = now
        task.manager_context = {**(task.manager_context or {}), "decision_id": str(decision.pk), "decision_version": version,
            "decision_digest": decision.envelope_hmac}
        if any(line.get("item_kind") != "custom_apparel" and line["availability"] == "available" and (line.get("supply_basis") == "accessible_unrecorded"
                or line.get("accounting_recount_required")) for line in lines):
            task.manager_context["required_decisions"] = ["stock_record_sync_or_manual_order"]
            task.manager_context["business_debt_open"] = True
        task.save(update_fields=["manager_approval_status", "manager_approval_actor", "manager_approval_decided_at", "manager_context", "updated_at"])
        if request.envelope["scope"] == "cart":
            IgFollowUpTask.objects.get_or_create(event_key=f"ig-fulfillment-update:{request.pk}:{version}", defaults={
                "client_id": request.client_id, "due_at": now, "kind": "fulfillment", "reason": UPDATE_REASON,
                "trigger": "event", "event_occurred_at": now, "policy_version": PURPOSE,
                "event_payload": {"origin": UPDATE_REASON, "request_id": str(request.pk), "request_digest": request.envelope_hmac,
                    "decision_id": str(decision.pk), "decision_version": version, "decision_digest": decision.envelope_hmac}})
        AdminAuditLog.objects.create(actor=actor, action="ig_fulfillment_decision", entity_type="IgFulfillmentReviewRequest",
            entity_id=str(request.pk), before={"version": version - 1}, after={"version": version,
                "decision_id": str(decision.pk), "decision_digest": decision.envelope_hmac}, reason="Source-bound manager feasibility decision")
        return {"ok": True, "created": True, "decision_id": str(decision.pk), "version": version}


def apply_fulfillment_decision(request_id, *, actor, expected_request_digest, operation_id, decisions_by_line, now=None, telegram_receipt=None):
    from django.db import IntegrityError
    try:
        result = _apply_fulfillment_decision(request_id, actor=actor, expected_request_digest=expected_request_digest,
            operation_id=operation_id, decisions_by_line=decisions_by_line, now=now, telegram_receipt=telegram_receipt)
    except IntegrityError:
        result = {"ok": False, "reason": "decision_conflict"}
    return {**result, "ready": result.get("ok", False)}


def _decision_reason(request, decision, *, now, _catalog_readback=None, current_source_cart=None):
    if request.envelope.get("scope") in {"restock_list", "restock_list_invitation"}:
        from management.services.ig_restock_waiting_list_review import decision_reason
        return decision_reason(request, decision, now=now)
    if not _verified(decision, "decision") or decision.envelope.get("request_digest") != request.envelope_hmac:
        return "decision_evidence_invalid"
    if decision.created_at > now or now - decision.created_at > FRESHNESS:
        return "manager_review_expired"
    by_line = {line["line_id"]: line for line in request.envelope["lines"]}
    available = [line for line in decision.envelope["lines"] if line["availability"] == "available"]
    if available:
        from management.services.ig_restock_availability import observe_configurations
        try:
            targets = [by_line[line["line_id"]]["target"] for line in available]
            reuse = ({"_catalog_readback": _catalog_readback, "source_cart": current_source_cart}
                if _catalog_readback is not None and current_source_cart is not None else {})
            configurations = observe_configurations(targets, now=now, **reuse)
        except (KeyError, TypeError, ValueError):
            return "configuration_changed"
        if not isinstance(configurations, list) or len(configurations) != len(available):
            return "configuration_changed"
        for line, target, configuration in zip(available, targets, configurations):
            if (not isinstance(configuration, dict) or configuration.get("status") != "valid"
                or configuration.get("target") != target
                or configuration.get("configuration_digest") != line["configuration_digest"]):
                return "configuration_changed"
    return ""


def _waiting_acceptance(request, decision, cart, update, settings_obj, *, now):
    from management.models import InstagramBotMessage
    from management.services.ig_purpose_sources import owned_source
    from management.services.ig_fulfillment_requirements import _customer_text
    proposed = {"state": "proposed", "accepted": False, "source_refs": []}
    if not decision.envelope.get("waiting_acceptance_required") or not cart or not update:
        return proposed
    watermark = cart.get("source_watermark") or (cart.get("fence") or {}).get("source_watermark") or {}
    cutoff = watermark.get("message_id")
    if type(cutoff) is not int or _cart_identity(cart) != request.envelope.get("cart_identity"):
        return proposed
    rows = list(InstagramBotMessage.objects.filter(client_id=request.client_id, sender_id=request.client.igsid,
        role="user", pk__gt=request.envelope["source_id"], pk__lte=cutoff,
        provider_namespace=request.envelope["source_namespace"], source__in=("webhook", "poll"),
        provider_created_at__gt=update.sent_at, provider_created_at__lte=now).exclude(status="failed").order_by("-provider_created_at", "-pk")[:17])
    if len(rows) > 16:
        return proposed
    for row in rows:
        raw = row.text or ""
        if len(raw) > 1000 or re.search(r"[?？]", raw):
            continue
        text, hypothetical = _customer_text(raw)
        # Quoted/reported/hypothetical instructions and a plain restock opt-in
        # do not choose this manager's exact waiting proposal.
        if hypothetical or text.strip() != raw.strip() or re.search(
                r"\b(?:said|says|told|wrote|сказ\w*|каж\w*|говор\w*|напис\w*|якщо|если|if|maybe|можливо|возможно)\b", raw, re.I):
            continue
        waiting = bool(re.search(r"\b(?:wait|waiting|чекати|чекатиму|почекаю|зачекаю|ждать|подожду|дождусь)\b", text, re.I))
        plain_refusal = bool(row.reply_to_provider_message_id == update.provider_message_id
            and re.fullmatch(r"(?:no|ні|нет)[.! ]*", text.strip(), re.I))
        if not waiting and not plain_refusal:
            continue
        frozen = row.revision_sources.select_related("revision").order_by("-revision_id").first()
        owned, _ = owned_source(frozen.revision, row, settings_obj, require_head=False) if frozen else (None, "")
        if not owned:
            continue
        if plain_refusal:
            return proposed
        if re.search(r"\b(?:не|ні|нет|not|no|never|won[’']?t|don[’']?t|нехочу|відмов\w*|отказ\w*)\b", text, re.I):
            return proposed
        # Only an unqualified acknowledgement accepts the whole proposal.
        # A different duration, first-item-only scope or extra condition is a
        # counteroffer requiring another scoped discussion, never acceptance.
        affirmative = bool(re.fullmatch(
            r"(?:(?:yes|так|да)[, ]+)?(?:"
            r"(?:I|we)(?:[’']ll|\s+(?:will|can|agree\s+to))\s+wait(?:\s+on\s+these\s+terms)?|"
            r"(?:я\s+)?(?:почекаю|зачекаю|чекатиму|подожду|дождусь)(?:\s+(?:на\s+цих\s+умовах|на\s+этих\s+условиях))?|"
            r"(?:я\s+)?(?:згоден|згодна|согласен|согласна)\s+(?:зачекати|чекати|подождать|ждать)"
            r"(?:\s+(?:на\s+цих\s+умовах|на\s+этих\s+условиях))?)[.! ]*", text.strip(), re.I))
        if affirmative and row.reply_to_provider_message_id == update.provider_message_id:
            return {"state": "accepted", "accepted": True, "source_refs": [{"kind": "message",
                "id": row.pk, "event_at": row.provider_created_at.isoformat(), "digest": owned[2]["digest"]}],
                "request_digest": request.envelope_hmac, "decision_digest": decision.envelope_hmac}
        # A newer explicit waiting counteroffer replaces the earlier consent;
        # do not continue scanning to resurrect agreement to the old terms.
        return proposed
    return proposed


def _projection(request, settings_obj, *, now, current_source_cart=None, _catalog_readback=None):
    base = {"version": 1, "captured_at": now.isoformat(), "status": "none", "request_digest": "", "current_request": None, "decision": None,
        "can_decide": False, "decision_mode": "none", "decision_recorded": False,
        "decision_blocked_reason": "request_missing", "source_excerpts": []}
    if not request:
        return base
    settings_obj = _settings(settings_obj)
    if not settings_obj:
        return base
    if current_source_cart is None and request.envelope.get("scope") == "cart":
        from management.services.ig_commerce_projection import capture_current_selection_lines
        current_source_cart = capture_current_selection_lines(request.client_id, now=now)
    permission_fields = ("pk", "is_enabled", "reply_permission_epoch", "allowed_senders", "ig_user_id", "page_id")
    initial_settings = {key: getattr(settings_obj, key) for key in permission_fields}
    # Read the actual persisted head once, including when the request itself
    # later proves stale. Processed status must not pretend no decision exists.
    decision = Decision.objects.filter(request=request).order_by("-version").first()
    base["decision_recorded"] = bool(decision)
    source_excerpts = []
    reason = _current_reason(request, settings_obj, now=now, current_source_cart=current_source_cart,
        _settings_fresh=True, _source_excerpts=source_excerpts)
    if reason:
        return {**base, "status": "stale", "reason": reason, "decision_blocked_reason": reason}
    payload = deepcopy(request.envelope)
    if current_source_cart is not None and payload.get("scope") == "cart":
        payload["source_scope"]["source_cart_capture_digest"] = current_source_cart.get("capture_digest")
    payload.update(id=str(request.pk), created_at=request.created_at.isoformat())
    from management.services.ig_fulfillment_requirements import active_hard_requirements, requirement_digest
    active = active_hard_requirements(payload.get("customer_requirements") or [])
    payload["active_hard_requirement_digest"] = requirement_digest(active[-1]) if active else ""
    base.update(status="pending", request_digest=request.envelope_hmac, current_request=payload,
        source_excerpts=source_excerpts, can_decide=True, decision_mode="initial", decision_blocked_reason="")
    # Expired/future/invalid persisted heads never reopen an initial form.
    if decision:
        base.update(can_decide=False, decision_mode="none", decision_recorded=True,
            decision_blocked_reason="decision_already_recorded")
        reason = _decision_reason(request, decision, now=now, _catalog_readback=_catalog_readback,
            current_source_cart=current_source_cart)
        if reason:
            base["reason"] = reason
            base["decision_blocked_reason"] = reason
        else:
            if (decision.envelope.get("deadline_review") or {}).get("outcome") == "review_pending":
                base.update(can_decide=True, decision_mode="deadline_resolution", decision_blocked_reason="")
            base.update(status="reviewed", decision={**deepcopy(decision.envelope), "id": str(decision.pk),
                "version": decision.version, "created_at": decision.created_at.isoformat()})
    from django.db.models import Subquery
    from management.models import InstagramBotSettings
    latest = Decision.objects.filter(request_id=request.pk).order_by("-version")
    final_settings_query = InstagramBotSettings.objects.filter(pk=settings_obj.pk)
    # One fresh statement observes the signed request, its latest decision,
    # owner and settings together. Preserve both read fences while avoiding
    # separate round trips that exhaust the bounded capture for staff roles.
    final_request = Request.objects.filter(pk=request.pk).select_related("client").annotate(
        review_decision_id=Subquery(latest.values("pk")[:1]),
        review_decision_version=Subquery(latest.values("version")[:1]),
        **{f"review_settings_{key}": Subquery(final_settings_query.values(key)[:1])
            for key in permission_fields}).first()
    latest_again = (final_request.review_decision_id, final_request.review_decision_version) if final_request and final_request.review_decision_id else None
    if not final_request or not _verified(final_request, "request") or final_request.envelope_hmac != request.envelope_hmac:
        return {**base, "status": "stale", "decision": None, "reason": "review_read_conflict", "can_decide": False, "decision_mode": "none", "decision_blocked_reason": "review_read_conflict", "source_excerpts": []}
    if latest_again != ((decision.pk, decision.version) if decision else None):
        return {**base, "status": "pending", "decision": None, "reason": "review_read_conflict", "can_decide": False, "decision_mode": "none", "decision_blocked_reason": "review_read_conflict", "source_excerpts": []}
    final_settings = {key: getattr(final_request, f"review_settings_{key}") for key in permission_fields}
    if final_settings != initial_settings:
        return {**base, "status": "stale", "decision": None, "reason": "permission_changed", "can_decide": False, "decision_mode": "none", "decision_blocked_reason": "permission_changed", "source_excerpts": []}
    owner_fields = ("igsid", "reply_permission_epoch", "privacy_erasure_started_at", "current_commercial_episode_id")
    if {key: getattr(final_request.client, key) for key in owner_fields} != {
            key: getattr(request.client, key) for key in owner_fields}:
        return {"version": 1, "captured_at": now.isoformat(), "status": "stale", "current_request": None,
            "request_digest": "", "decision": None, "reason": "owner_scope_changed",
            "can_decide": False, "decision_mode": "none", "decision_recorded": bool(decision),
            "decision_blocked_reason": "owner_scope_changed", "source_excerpts": []}
    if payload.get("scope") == "cart" and current_source_cart is not None:
        from management.services.ig_restock_context import capture_restock_interest
        # Presentation consumes the existing verified decision, never runs the
        # expensive fresh-send admission gate inside the bounded state reader.
        interest = capture_restock_interest(request, settings_obj, current_source_cart, now,
            reviewed_decision=decision if base["status"] == "reviewed" else None)
        if interest:
            base["restock_interest"] = interest
    # Native receipt is independent of manager approval and of a customer
    # reading the message. Do not leave the staff timeline permanently pending.
    base["customer_update"] = {"state": "absent", "native_receipt_confirmed": False, "sent_at": None}
    base["customer_waiting"] = {"state": "not_required", "accepted": False, "source_refs": []}
    if decision and base["status"] == "reviewed" and any(line.get("availability") == "wait" for line in decision.envelope.get("lines") or []):
        base["customer_waiting"] = {"state": "proposed", "accepted": False, "source_refs": []}
    if decision and base["status"] == "reviewed" and payload.get("scope") == "cart":
        from management.models import IgFollowUpTask
        from management.services.ig_delivery_receipts import normalize_provider_message_id
        update = IgFollowUpTask.objects.filter(client_id=request.client_id, kind="fulfillment", reason=UPDATE_REASON,
            event_key=f"ig-fulfillment-update:{request.pk}:{decision.version}").first()
        expected = {"origin": UPDATE_REASON, "request_id": str(request.pk), "request_digest": request.envelope_hmac,
            "decision_id": str(decision.pk), "decision_version": decision.version, "decision_digest": decision.envelope_hmac}
        if update and update.event_payload == expected:
            from management.services.ig_fulfillment_delivery import render_task_update
            sent = bool(update.status == "sent" and update.attempt_count == 1
                and normalize_provider_message_id(update.provider_message_id) and update.sent_at
                and decision.created_at <= update.sent_at <= now and not update.claim_token
                and update.message_text == render_task_update(update, base))
            base["customer_update"] = {"state": "sent" if sent else "unknown" if update.status in {"unknown", "ambiguous"} else
                "cancelled" if update.status in {"cancelled", "skipped"} else "pending",
                "native_receipt_confirmed": sent,
                "sent_at": update.sent_at.isoformat() if sent and update.sent_at else None}
            if sent and base["customer_waiting"]["state"] == "proposed":
                base["customer_waiting"] = _waiting_acceptance(request, decision, current_source_cart,
                    update, settings_obj, now=now)
    return base


def fulfillment_review_projection(client_id, *, now=None, current_source_cart=None, request_id=None, _catalog_readback=None):
    now = now or timezone.now()
    query = Request.objects.filter(client_id=client_id, created_at__lte=now)
    if request_id is not None:
        try:
            query = query.filter(pk=uuid.UUID(str(request_id)))
        except ValueError:
            return {"version": 1, "status": "none", "current_request": None, "decision": None, "captured_at": now.isoformat(), "can_decide": False,
                "decision_mode": "none", "decision_recorded": False, "decision_blocked_reason": "request_missing", "source_excerpts": []}
    else:
        # Exact-list reviews have their own standing-list panel/context. They
        # must not displace the current CART review in this legacy default
        # reader; exact request IDs still expose their authenticated native form.
        query = query.filter(source__isnull=False).exclude(
            envelope__scope__in=["restock_list_invitation", "restock_list"])
    request = query.select_related("client", "revision").order_by("-created_at", "-source_id", "-pk").first()
    return _projection(request, None, now=now, current_source_cart=current_source_cart, _catalog_readback=_catalog_readback)


def validated_update_task(task, settings_obj, *, now):
    data = task.event_payload or {}
    if task.reason != UPDATE_REASON or data.get("origin") != UPDATE_REASON or task.kind != "fulfillment":
        return {"ready": False, "reason": "task_scope_changed"}
    request = Request.objects.filter(pk=data.get("request_id"), client_id=task.client_id).select_related("client", "revision").first()
    if not request or request.envelope.get("scope") != "cart":
        return {"ready": False, "reason": "request_missing"}
    projected = _projection(request, settings_obj, now=now)
    decision = projected.get("decision")
    if projected["status"] != "reviewed" or not decision:
        return {"ready": False, "reason": projected.get("reason") or "manager_review_pending"}
    if (data.get("request_digest") != request.envelope_hmac or data.get("decision_id") != decision["id"]
        or data.get("decision_version") != decision["version"]
        or task.event_key != f"ig-fulfillment-update:{request.pk}:{decision['version']}"):
        return {"ready": False, "reason": "decision_changed"}
    row = Decision.objects.filter(pk=decision["id"]).first()
    if not _verified(row, "decision") or data.get("decision_digest") != row.envelope_hmac:
        return {"ready": False, "reason": "decision_changed"}
    proof = {"recipient": request.client.igsid, "source_namespace": request.envelope["source_namespace"],
        "request_id": str(request.pk), "request_digest": request.envelope_hmac, "decision_id": decision["id"],
        "decision_version": decision["version"], "decision_digest": row.envelope_hmac, "task_id": task.pk}
    return {**projected, "ready": True, "reason": "", "proof": proof, "request": projected["current_request"]}


def validated_update_history(task):
    """Original signed delivery binding; expiry/cart movement cannot replay a MID."""
    data = task.event_payload or {}
    request = Request.objects.filter(pk=data.get("request_id"), client_id=task.client_id).select_related("client").first()
    decision = Decision.objects.filter(pk=data.get("decision_id"), request=request).first() if request else None
    if (task.reason != UPDATE_REASON or task.kind != "fulfillment" or data.get("origin") != UPDATE_REASON
        or not _verified(request, "request") or not _verified(decision, "decision")
        or request.client.privacy_erasure_started_at or request.envelope.get("scope") != "cart"
        or data.get("request_digest") != request.envelope_hmac or data.get("decision_digest") != decision.envelope_hmac
        or data.get("decision_version") != decision.version or decision.envelope.get("request_digest") != request.envelope_hmac
        or task.event_key != f"ig-fulfillment-update:{request.pk}:{decision.version}"):
        return {"ready": False, "reason": "delivery_binding_invalid"}
    return {"ready": True, "recipient": request.client.igsid, "source_namespace": request.envelope["source_namespace"],
        "current_request": {**deepcopy(request.envelope), "id": str(request.pk)},
        "decision": {**deepcopy(decision.envelope), "id": str(decision.pk), "version": decision.version}}


def _cart_restock_review(subscription, settings_obj, configuration, *, now, current_source_cart=None):
    """Reuse an original CART review without changing either consent purpose.

    Eligibility suppresses duplicate manager debt. Only a fresh AVAILABLE
    decision produces admission proof; neither outcome authorizes delivery.
    """
    from management.ig_restock_subscription_models import IgRestockSubscription
    from management.services.ig_restock_subscriptions import _subscription_reason, _invitation_manager_case
    from management.services.ig_commerce_projection import capture_current_selection_lines
    from management.bot_access import has_all_bot_capabilities, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
    from django.utils.dateparse import parse_datetime

    empty = {"eligible": False, "ready": False, "reason": "manager_review_stale",
        "request": None, "decision": None, "proof": {}}
    settings_obj = _settings(settings_obj)
    def subscription_read():
        return IgRestockSubscription.objects.filter(pk=subscription.pk, client_id=subscription.client_id).select_related(
            "accepted_answer__invitation__client", "accepted_answer__invitation__revision",
            "accepted_answer__invitation__request_source", "accepted_answer__receipt__effect", "accepted_answer__source").first()

    actual = subscription_read()
    if not settings_obj or not actual or _subscription_reason(actual, settings_obj, now=now):
        return empty
    accepted, inv = actual.accepted_answer, actual.accepted_answer.invitation
    if (not isinstance(configuration, dict) or configuration.get("status") != "valid"
        or configuration.get("target") != inv.target
        or not _invitation_manager_case(inv, settings_obj=settings_obj, now=now)):
        return empty
    original_id = inv.envelope["manager_review"]["request_id"]
    cart_query = Request.objects.filter(client_id=actual.client_id, source__isnull=False)
    request = cart_query.select_related("client", "revision", "task").order_by("-created_at", "-source_id", "-pk").first()
    if (not request or str(request.pk) != original_id or request.created_at > now
        or not _verified(request, "request") or request.envelope.get("scope") != "cart"):
        return empty
    cart = current_source_cart if current_source_cart is not None else capture_current_selection_lines(actual.client_id, now=now)
    if _current_reason(request, settings_obj, now=now, current_source_cart=cart):
        return empty
    line_id = inv.envelope["selection"]["line_id"]
    matches = [line for line in request.envelope.get("lines", ()) if line.get("line_id") == line_id
        and line.get("target") == inv.target
        and line.get("recipient_id", "self") == inv.envelope["selection"].get("recipient_id", "self")]
    if len(matches) != 1:
        return empty
    decision = Decision.objects.filter(request=request).select_related("actor").order_by("-version").first()
    line = None
    if decision is None:
        if (now - request.created_at > FRESHNESS
            or matches[0].get("configuration_digest") != configuration.get("configuration_digest")):
            return empty
    else:
        if _decision_reason(request, decision, now=now, current_source_cart=cart):
            return empty
        try:
            decided_at = parse_datetime(decision.envelope.get("decided_at", ""))
        except (TypeError, ValueError):
            return empty
        if (not decided_at or timezone.is_naive(decided_at) or decided_at > now
            or decided_at > decision.created_at or now - decided_at > FRESHNESS
            or not decision.actor.is_active or not has_all_bot_capabilities(decision.actor,
                OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)):
            return empty
        answers = [row for row in decision.envelope.get("lines", ()) if row.get("line_id") == line_id]
        if len(answers) != 1:
            return empty
        line = answers[0]
        # WAIT/unavailable also requires the original exact configuration; a
        # fresh timestamp cannot renew an obsolete SKU decision.
        if line.get("configuration_digest") != configuration.get("configuration_digest"):
            return empty

    def standing_head():
        standing = Request.objects.filter(client_id=actual.client_id, source__isnull=True,
            envelope__subscription_id=str(actual.pk),
            envelope__lines__0__configuration_digest=configuration["configuration_digest"]).order_by("-created_at", "-pk").first()
        latest = Decision.objects.filter(request=standing).order_by("-version").first() if standing else None
        identity = (standing.pk, standing.envelope_hmac, latest.pk if latest else None,
            latest.envelope_hmac if latest else None) if standing else None
        return standing, latest, identity

    standing, standing_decision, standing_fence = standing_head()
    if standing_decision:
        try:
            standing_clock = parse_datetime(standing_decision.envelope.get("decided_at", ""))
        except (TypeError, ValueError):
            return empty
        if (not _verified(standing, "request") or not _verified(standing_decision, "decision")
            or not standing_clock or timezone.is_naive(standing_clock)
            or standing_decision.created_at > now or standing_clock > now
            or standing_clock > standing_decision.created_at
            or decision is None or standing_decision.created_at >= decision.created_at):
            # An explicit newer standing decision owns its purpose. Never hide
            # it behind the original CART availability, even if contradictory
            # or future-dated. No older-row fallback repairs an invalid head.
            return empty
    final_request = cart_query.select_related("client").order_by("-created_at", "-source_id", "-pk").first()
    final_decision = Decision.objects.filter(request=request).select_related("actor").order_by("-version").first()
    if (not final_request or final_request.pk != request.pk or not _verified(final_request, "request")
        or final_request.envelope_hmac != request.envelope_hmac
        or ((final_decision.pk, final_decision.envelope_hmac) if final_decision else None)
            != ((decision.pk, decision.envelope_hmac) if decision else None)
        or (final_decision is not None and not _verified(final_decision, "decision"))
        or standing_head()[2] != standing_fence):
        return {**empty, "reason": "review_read_conflict"}
    # Identity fences do not authenticate mutable sources, current selection,
    # permission or catalog configuration. Re-read each owner before exporting
    # even an informational reuse descriptor; admission also needs these gates.
    final_settings = _settings(settings_obj)
    final_subscription = subscription_read()
    final_cart = capture_current_selection_lines(actual.client_id, now=now)
    final_configuration = _configuration(inv.target, now=now)
    if (not final_settings or not final_subscription
        or final_subscription.accepted_answer_id != actual.accepted_answer_id
        or _subscription_reason(final_subscription, final_settings, now=now)
        or final_cart.get("capture_digest") != cart.get("capture_digest")
        or _current_reason(final_request, final_settings, now=now, current_source_cart=final_cart)
        or final_configuration.get("status") != "valid" or final_configuration.get("target") != inv.target
        or final_configuration.get("configuration_digest") != configuration.get("configuration_digest")
        or (final_decision is not None and (not final_decision.actor.is_active
            or not has_all_bot_capabilities(final_decision.actor,
                OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)))):
        return {**empty, "reason": "review_read_conflict"}
    eligible = {"eligible": True, "ready": False, "reason": "manager_review_pending",
        "request": request, "decision": decision, "proof": {}}
    if line is None:
        return eligible
    if line.get("availability") != "available":
        return {**eligible, "reason": "manager_review_unavailable"}
    if (line.get("supply_basis") not in {"warehouse_checked", "accessible_unrecorded"}
        or type(line.get("checked_quantity")) is not int
        or line["checked_quantity"] < inv.target.get("quantity", 1000)):
        return empty
    proof = {"version": 1, "review_scope": "cart", "request_id": str(request.pk),
        "request_digest": request.envelope_hmac, "decision_id": str(decision.pk),
        "decision_version": decision.version, "decision_digest": decision.envelope_hmac,
        "subscription_id": str(actual.pk), "target": deepcopy(inv.target), "line_id": line_id,
        "inventory_digest": line["inventory_digest"], "configuration_digest": line["configuration_digest"],
        "checked_quantity": line["checked_quantity"], "supply_basis": line["supply_basis"],
        "invitation_id": str(inv.pk), "invitation_digest": inv.envelope_hmac,
        "answer_id": str(accepted.pk), "answer_digest": accepted.envelope_hmac}
    return {**eligible, "ready": True, "reason": "", "proof": proof}


def ensure_restock_manager_review(subscription, availability, *, now=None):
    from management.services import ig_restock_waiting_list as waiting
    if waiting.is_list(subscription.accepted_answer.invitation.target):
        from management.services.ig_restock_waiting_list_review import ensure_review
        return ensure_review(subscription, availability, now=now or timezone.now())
    from management.models import IgClient
    from management.services.ig_restock_subscriptions import _subscription_reason
    now = now or timezone.now()
    observed = _observation(availability)
    inv = subscription.accepted_answer.invitation
    settings_obj = _settings()
    if not settings_obj or not getattr(settings, "IG_FULFILLMENT_REVIEWS_ENABLED", False) or _subscription_reason(subscription, settings_obj, now=now):
        return {"created": False, "reason": "subscription_changed"}
    if observed.get("target") != inv.target:
        return {"created": False, "reason": "inventory_unverified"}
    configuration = _configuration(inv.target, now=now)
    if configuration.get("status") != "valid" or configuration.get("target") != inv.target:
        return {"created": False, "reason": "configuration_unverified"}
    reused = _cart_restock_review(subscription, settings_obj, configuration, now=now)
    if reused.get("reason") == "review_read_conflict":
        # An unstable read cannot manufacture new manager debt from the older
        # observation. The next reconciliation obtains a new consistent frame.
        return {"created": False, "reason": "review_read_conflict"}
    if reused["eligible"]:
        request = reused["request"]
        return {"created": False, "reused": True, "review_scope": "cart", "reason": reused["reason"],
            "request_id": str(request.pk), "request_digest": request.envelope_hmac, "task_id": request.task_id}
    existing = Request.objects.filter(client_id=subscription.client_id, source__isnull=True).select_related("client").order_by("-created_at")[:50]
    generation = "initial"
    for candidate in existing:
        if (candidate.envelope.get("subscription_id") != str(subscription.pk)
            or candidate.envelope.get("lines", [{}])[0].get("configuration_digest") != configuration["configuration_digest"]
            or not _verified(candidate, "request")):
            continue
        decision = Decision.objects.filter(request=candidate).order_by("-version").first()
        if not decision or now - decision.created_at <= FRESHNESS:
            return {"created": False, "request_id": str(candidate.pk), "request_digest": candidate.envelope_hmac, "task_id": candidate.task_id}
        generation = str(decision.pk)
        break
    with transaction.atomic():
        client = IgClient.objects.select_for_update().get(pk=subscription.client_id)
        payload = {"scope": "restock", "subscription_id": str(subscription.pk), "answer_hmac": subscription.accepted_answer.envelope_hmac,
            "review_generation": generation,
            "client_id": client.pk, "locale": inv.locale, "source_id": inv.request_source_id,
            "source_namespace": inv.envelope["source"]["namespace"], "sources": [inv.envelope["source"]],
            "settings_id": settings_obj.pk, "settings_epoch": settings_obj.reply_permission_epoch, "client_epoch": client.reply_permission_epoch,
            "customer_requirements": [], "lines": [{"line_id": "restock:" + str(subscription.pk), "recipient_id": "self",
                "label": inv.envelope["label"], "target": inv.target, "observation": observed, "inventory_digest": inventory_digest(observed)}]}
        payload["lines"][0]["configuration_digest"] = configuration["configuration_digest"]
        # Captured clocks are retained as evidence but cannot create new manager debt on every poll.
        payload["lines"][0]["observation"].pop("captured_at", None)
        payload["lines"][0]["observation"].pop("digest", None)
        request, created = _create_request(client, payload, now=now)
        return {"created": created, "request_id": str(request.pk), "request_digest": request.envelope_hmac, "task_id": request.task_id}


def restock_manager_admission(subscription, availability, *, now=None):
    from management.services import ig_restock_waiting_list as waiting
    if waiting.is_list(subscription.accepted_answer.invitation.target):
        from management.services.ig_restock_waiting_list_review import manager_admission
        return manager_admission(subscription, availability, now=now or timezone.now())
    now, settings_obj = now or timezone.now(), _settings()
    observed = _observation(availability)
    if not settings_obj:
        return {"ready": False, "reason": "manager_review_pending", "proof": {}}
    requests = Request.objects.filter(client_id=subscription.client_id, source__isnull=True).select_related("client").order_by("-created_at")[:50]
    target = subscription.accepted_answer.invitation.target
    configuration = _configuration(target, now=now)
    if configuration.get("status") != "valid" or configuration.get("target") != target:
        return {"ready": False, "reason": "manager_review_stale", "proof": {}}
    if observed.get("target") != target:
        return {"ready": False, "reason": "manager_review_stale", "proof": {}}
    reused = _cart_restock_review(subscription, settings_obj, configuration, now=now)
    if reused.get("reason") == "review_read_conflict":
        return {"ready": False, "reason": "manager_review_stale", "proof": {}}
    if reused["eligible"]:
        return {"ready": reused["ready"], "reason": reused["reason"], "proof": reused["proof"]}
    request = next((row for row in requests if row.envelope.get("subscription_id") == str(subscription.pk)
        and row.envelope.get("lines", [{}])[0].get("configuration_digest") == configuration.get("configuration_digest")), None)
    if not request:
        return {"ready": False, "reason": "manager_review_pending", "proof": {}}
    reason = _current_reason(request, settings_obj, now=now)
    if reason:
        return {"ready": False, "reason": "manager_review_stale", "proof": {}}
    decision = Decision.objects.filter(request=request).order_by("-version").first()
    if not decision:
        return {"ready": False, "reason": "manager_review_pending", "proof": {}}
    reason = _decision_reason(request, decision, now=now)
    if reason:
        return {"ready": False, "reason": "manager_review_expired" if reason == "manager_review_expired" else "manager_review_stale", "proof": {}}
    line = decision.envelope["lines"][0]
    target = subscription.accepted_answer.invitation.target
    if line["availability"] != "available":
        return {"ready": False, "reason": "manager_review_unavailable", "proof": {}}
    if observed.get("target") != target or line["configuration_digest"] != configuration.get("configuration_digest"):
        return {"ready": False, "reason": "manager_review_stale", "proof": {}}
    proof = {"version": 1, "request_id": str(request.pk), "request_digest": request.envelope_hmac,
        "decision_id": str(decision.pk), "decision_version": decision.version, "decision_digest": decision.envelope_hmac,
        "subscription_id": str(subscription.pk), "target": deepcopy(target), "inventory_digest": line["inventory_digest"],
        "configuration_digest": line["configuration_digest"],
        "checked_quantity": line["checked_quantity"]}
    proof["supply_basis"] = line["supply_basis"]
    return {"ready": True, "reason": "", "proof": proof}
