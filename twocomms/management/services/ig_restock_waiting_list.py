"""Exact, one-off ALL-items restock interest; no stock or purchase authority."""
from copy import deepcopy
import re
import uuid

from django.db import transaction
from django.utils import timezone

SCHEMA = "restock-waiting-list.v2"
MAX_ITEMS = 16
MAX_QUANTITY = 50


def is_list(target):
    return isinstance(target, dict) and target.get("schema") == SCHEMA


def _restock():
    from management.services import ig_restock_subscriptions
    return ig_restock_subscriptions


def _digest(value):
    return _restock()._digest(value)


def _text(value, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("waiting_list_item_invalid")
    return value.strip()


def validate_target(target):
    if not is_list(target) or set(target) != {"schema", "completion", "items"} or target["completion"] != "all":
        raise ValueError("waiting_list_schema_invalid")
    items = target["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError("waiting_list_item_bound")
    identities, quantity = set(), 0
    keys = {"line_id", "recipient_id", "item_kind", "title", "color", "size", "fit", "options", "quantity", "catalog_target", "source"}
    for item in items:
        if not isinstance(item, dict) or set(item) != keys or item["item_kind"] not in {"catalog_apparel", "custom_apparel"}:
            raise ValueError("waiting_list_item_invalid")
        for key in ("line_id", "recipient_id", "title", "color", "size"):
            _text(item[key], 200 if key == "title" else 128)
        if item["line_id"] in identities or type(item["quantity"]) is not int or not 1 <= item["quantity"] <= MAX_QUANTITY:
            raise ValueError("waiting_list_item_identity_invalid")
        if not isinstance(item["fit"], str) or len(item["fit"]) > 50 or not isinstance(item["options"], dict) or len(item["options"]) > 16:
            raise ValueError("waiting_list_configuration_invalid")
        if not isinstance(item["source"], dict) or item["source"].get("kind") not in {"catalog_selection", "assisted_intention"}:
            raise ValueError("waiting_list_source_invalid")
        if item["item_kind"] == "catalog_apparel":
            from management.services.ig_restock_availability import _target
            native = _target(item["catalog_target"])
            if (native != item["catalog_target"] or any(native[key] != item[other] for key, other in
                (("quantity", "quantity"), ("size", "size"), ("fit_option_code", "fit"), ("option_values", "options")))):
                raise ValueError("waiting_list_catalog_target_invalid")
        elif item["catalog_target"] is not None or item["source"]["kind"] != "assisted_intention":
            raise ValueError("waiting_list_custom_source_invalid")
        identities.add(item["line_id"])
        quantity += item["quantity"]
    if quantity > MAX_QUANTITY:
        raise ValueError("waiting_list_quantity_bound")
    return target


def item_label(item):
    pieces = [item["title"], item["color"], item["size"]]
    if item["fit"]:
        pieces.append(item["fit"])
    pieces.extend(f"{key}: {value}" for key, value in sorted(item["options"].items()))
    pieces.append(f"×{item['quantity']}")
    if item["recipient_id"] != "self":
        pieces.append(item["recipient_id"])
    return " · ".join(pieces)


def label(target):
    validate_target(target)
    return "\n".join(f"{index + 1}. {item_label(item)}" for index, item in enumerate(target["items"]))


def copy_for(locale, kind, item_list):
    copies = {
        "uk": {"invite": "💛 Одне повідомлення, коли менеджер підтвердить наявність УСІХ позицій:\n{label}\n\nПідтвердьте кнопкою. Це не бронювання. Поза дозволеним вікном зв’язку чекатимемо вашого повідомлення.",
            "accepted": "Домовились 💛 Одне повідомлення після перевірки менеджером УСІХ позицій:\n{label}\n\nЦе не бронювання й не обіцянка дати. Якщо повідомлення довго немає, напишіть нам.",
            "notification": "💛 Менеджер щойно перевірив наявність УСІХ погоджених позицій у потрібній кількості:\n{label}\n\nДля оформлення й уточнення підготовки та відправлення напишіть нам. Це не бронювання.",
            "too_long": "Список завеликий для однієї точної картки. Уточніть коротший список очікуваних позицій — жодної позиції не вилучаємо й підписку ще не створено."},
        "ru": {"invite": "💛 Одно сообщение, когда менеджер подтвердит наличие ВСЕХ позиций:\n{label}\n\nПодтвердите кнопкой. Это не резерв. Вне разрешённого окна связи дождёмся вашего сообщения.",
            "accepted": "Договорились 💛 Одно сообщение после проверки менеджером ВСЕХ позиций:\n{label}\n\nЭто не резерв и не обещание даты. Если сообщения долго нет, напишите нам.",
            "notification": "💛 Менеджер только что проверил наличие ВСЕХ согласованных позиций в нужном количестве:\n{label}\n\nДля оформления и уточнения подготовки и отправки напишите нам. Это не резерв.",
            "too_long": "Список слишком велик для одной точной карточки. Уточните более короткий список ожидаемых позиций — ничего не удаляем, подписка ещё не создана."},
        "en": {"invite": "💛 One message when our manager confirms ALL these items are available:\n{label}\n\nConfirm below. This is not a reservation. Outside the permitted messaging window, we will wait for your message.",
            "accepted": "Agreed 💛 One message after our manager checks ALL listed items:\n{label}\n\nThis is not a reservation or promised date. If you hear nothing after a long pause, message us.",
            "notification": "💛 Our manager has just checked that ALL agreed items are available in the requested quantities:\n{label}\n\nMessage us to order and confirm preparation and dispatch. This is not a reservation.",
            "too_long": "The exact list is too long for one card. Please specify a shorter waiting list. We have removed no items and created no subscription."},
    }
    if kind not in copies.get(locale, copies["uk"]):
        return _restock()._copy(locale, kind, item_list)
    return copies.get(locale, copies["uk"])[kind].format(label=item_list)


def _assisted_source(source, client_id):
    from management.services import ig_assisted_commerce as commerce
    from management.services.ig_assisted_scopes import _source_history
    Request, Decision, _Execution, _Image = commerce._models()
    request = Request.objects.select_related("client", "revision").filter(pk=source.get("request_id"), client_id=client_id).first()
    decision = Decision.objects.filter(pk=source.get("decision_id"), request=request, client_id=client_id).first() if request else None
    if (not commerce._verified(request, "request") or not commerce._verified(decision, "decision")
        or decision.status != "approved" or request.envelope_hmac != source.get("request_digest")
        or decision.envelope_hmac != source.get("decision_digest") or str(request.intention_id) != source.get("intention_id")):
        raise ValueError("waiting_list_assisted_source_changed")
    _source_history(request)
    return request, decision


def _assisted_item_parity(item, request, decision):
    row = next((row for row in decision.envelope["snapshot"]["items"] if row["line_id"] == item["line_id"]), None)
    if (not row or row["availability"] != "wait" or row["recipient_id"] != item["recipient_id"]
        or row["quantity"] != item["quantity"] or row["item_kind"] != item["item_kind"]
        or row["title"] != item["title"] or row["color"] != item["color"] or row["size"] != item["size"]
        or row["fit_code"] != item["fit"] or row["option_values"] != item["options"]):
        raise ValueError("waiting_list_assisted_item_changed")
    if item["item_kind"] == "catalog_apparel" and item["catalog_target"] != {
        "product_id": row["product_id"], "color_variant_id": row["color_variant_id"], "size": row["size"],
        "fit_option_code": row["fit_code"], "option_values": row["option_values"], "quantity": row["quantity"]}:
        raise ValueError("waiting_list_assisted_catalog_changed")
    return request, decision


def _assisted_item_source(item, client_id):
    return _assisted_item_parity(item, *_assisted_source(item["source"], client_id))


def verify_item_sources(target, client, namespace):
    restock = _restock()
    validate_target(target)
    assisted_sources = {}
    for item in target["items"]:
        source = item["source"]
        if source["kind"] == "assisted_intention":
            identity = _digest(source)
            if identity not in assisted_sources:
                assisted_sources[identity] = _assisted_source(source, client.pk)
            _assisted_item_parity(item, *assisted_sources[identity])
        else:
            selection, native = source["selection"], item["catalog_target"]
            values = selection.get("values") or {}
            if (selection.get("line_id") != item["line_id"] or selection.get("recipient_id") != item["recipient_id"]
                or any(values.get(key) != native[key] for key in ("product_id", "size", "fit_option_code", "quantity"))
                or {**(values.get("option_values") or {}), "fit": values.get("fit_option_code")} != native["option_values"]
                or restock._selection_sources(selection, client, namespace) != source.get("selection_sources")):
                raise ValueError("waiting_list_original_source_changed")


def _assisted_items(client, cart, *, now):
    from management.services import ig_assisted_commerce as commerce, ig_assisted_scopes as scopes
    request = commerce.latest_assisted_request(client.pk)
    if request is None:
        return None
    _Request, Decision, _Execution, _Image = commerce._models()
    decision = Decision.objects.filter(request=request).order_by("-version").first()
    if not decision or decision.status != "approved" or not commerce._verified(decision, "decision") or not commerce._verified(request, "request"):
        return None
    if decision.expires_at <= now or request.created_at > now:
        return None
    coverage = scopes.intention_coverage(client.pk, request.intention_id, now=now)
    if not coverage.get("ready"):
        raise ValueError(coverage.get("reason") or "waiting_list_intention_unverified")
    mapping = {row["current_line_id"]: row["line_id"] for row in scopes.request_line_map(request)}
    deferred = set(coverage["deferred_line_ids"])
    current_rows = {row["line_id"]: row for row in cart["lines"]}
    source_rows = {row["line_id"]: row for row in request.envelope["snapshot"]["lines"]}
    _assisted_scope_current(request, cart)
    result = []
    for row in decision.envelope["snapshot"]["items"]:
        if row["availability"] != "wait" or mapping[row["line_id"]] not in deferred:
            continue
        if row["line_id"] not in current_rows:
            raise ValueError("waiting_list_intention_not_current")
        current = current_rows[row["line_id"]]
        for key, expected in source_rows[row["line_id"]]["source_choices"].items():
            if key == "purchase_requested":
                continue
            field = current.get("fields", {}).get(key) or {}
            # model_query remains an explicitly ambiguous customer wish; the
            # manager's immutable exact item supplies the reviewed description.
            expected_status = "ambiguous" if key == "model_query" else "confirmed"
            if field.get("status") != expected_status or field.get("value") != expected:
                raise ValueError("waiting_list_intention_configuration_changed")
        catalog = None
        if row["item_kind"] == "catalog_apparel":
            catalog = {"product_id": row["product_id"], "color_variant_id": row["color_variant_id"],
                "size": row["size"], "fit_option_code": row["fit_code"], "option_values": deepcopy(row["option_values"]), "quantity": row["quantity"]}
            # A prior manager row is historical catalog evidence. Resolve the
            # actual current, owned customer choices again before starting a
            # new invitation: the same variant PK may now name another color.
            from management.services.ig_commerce_projection import source_preferences_for
            from management.services.ig_restock_availability import resolve_source_interest
            preferences = source_preferences_for(client, line_id=row["line_id"])
            if not preferences:
                raise ValueError("waiting_list_catalog_source_changed")
            observed = resolve_source_interest(_restock()._quantity_basis(preferences, current), now=now).as_dict()
            if observed.get("target") != catalog:
                raise ValueError("waiting_list_catalog_source_changed")
        result.append({"line_id": row["line_id"], "recipient_id": row["recipient_id"], "item_kind": row["item_kind"],
            "title": row["title"], "color": row["color"], "size": row["size"], "fit": row["fit_code"],
            "options": deepcopy(row["option_values"]), "quantity": row["quantity"], "catalog_target": catalog,
            "source": {"kind": "assisted_intention", "request_id": str(request.pk), "request_digest": request.envelope_hmac,
                "decision_id": str(decision.pk), "decision_digest": decision.envelope_hmac, "intention_id": str(request.intention_id)}})
    return result or None


def _assisted_scope_current(request, cart):
    """The reviewed full wish scope must cover today's complete source cart.

    A newly added row cannot disappear merely because an older approval has
    some WAIT items. Even a newly READY catalog row requires a refreshed full
    manager scope; this producer never guesses a union of commercial reviews.
    Paid/deferred accounting still belongs to the native intention coverage.
    """
    sources = {row["line_id"]: row for row in request.envelope["snapshot"]["lines"]}
    current = {row["line_id"]: row for row in cart["lines"]}
    if len(current) != len(cart["lines"]) or set(sources) != set(current):
        raise ValueError("waiting_list_full_manager_scope_changed")
    for identity, original in sources.items():
        row = current[identity]
        choices = {key: field.get("value") for key, field in row.get("fields", {}).items()
            if key != "purchase_requested" and field.get("status") in {"confirmed", "ambiguous"}}
        expected = {key: value for key, value in original["source_choices"].items() if key != "purchase_requested"}
        if row["recipient_id"] != original["recipient_id"] or choices != expected:
            raise ValueError("waiting_list_intention_configuration_changed")


def capture_target(client, source, *, now):
    from management.services.ig_commerce_projection import capture_current_selection_lines, source_preferences_for
    from management.services.ig_restock_availability import resolve_source_interest
    restock = _restock()
    cart = capture_current_selection_lines(client.pk, now=now)
    if cart.get("status") != "captured" or not cart.get("coverage_complete"):
        raise ValueError("waiting_list_source_cart_unavailable")
    items = _assisted_items(client, cart, now=now)
    if items is None:
        items = []
        for line in cart["lines"]:
            preferences = source_preferences_for(client, line_id=line["line_id"])
            if not preferences:
                raise ValueError("waiting_list_item_unverified")
            preferences = restock._quantity_basis(preferences, line)
            observation = resolve_source_interest(preferences, now=now).as_dict()
            if observation.get("status") == "available":
                continue
            if observation.get("status") != "unavailable":
                raise ValueError("waiting_list_manager_source_required")
            sources = restock._selection_sources(preferences, client, source.provider_namespace)
            if not sources:
                raise ValueError("waiting_list_item_source_unverified")
            labels = observation.get("labels") or {}
            target = observation["target"]
            items.append({"line_id": line["line_id"], "recipient_id": line["recipient_id"], "item_kind": "catalog_apparel",
                "title": labels.get("product") or labels.get("product_title") or "", "color": labels.get("color") or "",
                "size": target["size"], "fit": target["fit_option_code"], "options": deepcopy(target["option_values"]),
                "quantity": target["quantity"], "catalog_target": deepcopy(target),
                "source": {"kind": "catalog_selection", "selection": restock._selection_identity(preferences), "selection_sources": sources}})
    if not items:
        raise ValueError("waiting_list_no_unavailable_items")
    target = {"schema": SCHEMA, "completion": "all", "items": items}
    validate_target(target)
    verify_item_sources(target, client, source.provider_namespace)
    return target, cart


def prepare_request(revision, source, proof, settings_obj, *, now):
    """Only a pure, owned live request; all receipts remain real RS1 effects."""
    restock = _restock()
    if not restock.is_restock_interest(source.text):
        return None
    target, cart = capture_target(revision.client, source, now=now)
    if len(target["items"]) == 1 and target["items"][0]["source"]["kind"] == "catalog_selection":
        return None  # Released exact single-catalog RS1 path remains unchanged.
    if len(target["items"]) > 1 and not re.search(r"\b(?:вс[іеё]х?|весь|ус[ііх]+|all|решт[ауи]|остаток|остатки)\b", source.text, re.I):
        return restock.RestockRequest(True, restock._copy(restock._locale(source.text), "clarify"), reason="waiting_list_scope_ambiguous")
    from management.services.ig_reply_language import resolve_own_source_reply_language
    language = resolve_own_source_reply_language(sources=[{"message_id": source.pk, "role": "user", "text": source.text,
        "provider_created_at": source.provider_created_at.isoformat()}], history=[], profile_language=revision.client.language,
        reset_floor=proof["reset_floor"], watermark_message_id=source.pk, watermark_event_at=now)
    locale = language.template_family if language.template_family in {"uk", "ru", "en"} else language.knowledge_locale
    frozen_label = label(target)
    if any(len(copy_for(locale, kind, frozen_label).encode()) > 1000 for kind in ("invite", "accepted", "notification")):
        return restock.RestockRequest(True, copy_for(locale, "too_long", ""), reason="waiting_list_single_card_bound")
    from management.services import ig_restock_waiting_list_review as review
    from management.services import ig_assisted_commerce as commerce
    with transaction.atomic():
        existing = restock.Invitation.objects.filter(revision=revision, request_source=source).first()
        if existing:
            if existing.target != target or restock._invitation_source(existing, settings_obj, require_head=True):
                return restock.RestockRequest(True, restock._copy(locale, "stale"), reason="invitation_changed")
            return restock.RestockRequest(True, existing.body, (restock._button(existing),), str(existing.pk), "existing_invitation")
        case = review.create_invitation_review(revision, source, proof, target, settings_obj, now=now,
            expected_configuration=commerce._configuration_capture(cart), locale=locale)
        identity, key_id = uuid.uuid4(), next(iter(restock._keys()))
        inv = restock.Invitation(id=identity, client=revision.client, request_source=source, revision=revision,
            target=target, target_digest=_digest(target), expires_at=now + restock.VALIDITY,
            acceptance_deadline=source.provider_created_at + restock.WINDOW, locale=locale,
            body=copy_for(locale, "invite", frozen_label), button_title=restock._copy(locale, "button"),
            accept_payload=restock._payload(identity, "accept", key_id), revoke_payload=restock._payload(identity, "revoke", key_id))
        restock._seal(inv, "invitation", source=proof, label=frozen_label, recipient=revision.client.igsid,
            settings_id=settings_obj.pk, settings_epoch=settings_obj.reply_permission_epoch,
            client_epoch=revision.client.reply_permission_epoch, manager_review=case,
            waiting_list={"schema": SCHEMA, "source_capture": deepcopy(cart), "configuration": commerce._configuration_capture(cart),
                "reply_language": language.as_dict()}, native_future_capability="unverified")
        return restock.RestockRequest(True, inv.body, (restock._button(inv),), str(inv.pk))


def invitation_reason(inv, settings_obj, *, require_head=False, now=None, check_dispatch_scope=True):
    restock = _restock()
    try:
        validate_target(inv.target)
        owned, reason = restock.owned_source(inv.revision, inv.request_source, settings_obj, require_head=require_head)
        if not owned or owned[2] != inv.envelope.get("source"):
            return reason or "waiting_list_request_source_changed"
        client = owned[0].client
        if (client.igsid != inv.envelope.get("recipient") or settings_obj.pk != inv.envelope.get("settings_id")
            or settings_obj.reply_permission_epoch != inv.envelope.get("settings_epoch")
            or client.reply_permission_epoch != inv.envelope.get("client_epoch")):
            return "permission_scope_changed"
        verify_item_sources(inv.target, client, inv.envelope["source"]["namespace"])
        from management.services import ig_restock_waiting_list_review as review
        if not review.invitation_review_valid(inv):
            return "manager_review_binding_changed"
        return restock._scope(client, settings_obj) if check_dispatch_scope else ""
    except (ValueError, KeyError, TypeError, AttributeError):
        return "waiting_list_source_unverified"


def current_target_reason(inv, *, now):
    from management.services.ig_restock_waiting_list_review import configuration_reason
    reason = configuration_reason(inv, now=now)
    if reason:
        return reason
    from management.services import ig_assisted_commerce as commerce
    from management.services.ig_commerce_projection import capture_current_selection_lines
    cart = capture_current_selection_lines(inv.client_id, now=now)
    if (cart.get("status") != "captured" or not cart.get("coverage_complete")
        or commerce._configuration_capture(cart) != inv.envelope["waiting_list"]["configuration"]):
        return "waiting_list_selection_changed"
    for item in inv.target["items"]:
        if item["source"]["kind"] == "assisted_intention":
            request, decision = _assisted_item_source(item, inv.client_id)
            try:
                _assisted_scope_current(request, cart)
            except (ValueError, KeyError, TypeError, AttributeError):
                return "waiting_list_full_manager_scope_changed"
            from management.services.ig_assisted_scopes import request_line_map, intention_coverage
            coverage = intention_coverage(inv.client_id, request.intention_id, now=now)
            original = next(row["line_id"] for row in request_line_map(request) if row["current_line_id"] == item["line_id"])
            if not coverage.get("ready") or original not in coverage["deferred_line_ids"]:
                return "waiting_list_intention_changed"
            Request, Decision, _Execution, _Image = commerce._models()
            if (Request.objects.filter(intention_id=request.intention_id).order_by("-version").values_list("pk", flat=True).first() != request.pk
                or Decision.objects.filter(request=request).order_by("-version").values_list("pk", flat=True).first() != decision.pk
                or decision.expires_at <= now):
                return "waiting_list_manager_source_changed"
    return ""


def observe(target, *, now):
    validate_target(target)
    observations = [observe_item(item, now=now) for item in target["items"]]
    status = "available" if all(row["status"] == "available" for row in observations) else "unavailable" if any(
        row["status"] == "unavailable" for row in observations) else "unknown"
    result = {"version": 2, "status": status, "reason": "waiting_list_inventory_observation", "target": deepcopy(target),
        "items": observations, "captured_at": now.isoformat(), "evidence": {}, "labels": {}}
    result["digest"] = _digest(result)
    return result


def observe_item(item, *, now):
    if item["item_kind"] == "catalog_apparel":
        from management.services.ig_restock_availability import observe_target, observe_configuration
        observed = observe_target(item["catalog_target"], now=now).as_dict()
        configuration = observe_configuration(item["catalog_target"], now=now)
        config = configuration.get("configuration_digest", "") if configuration.get("status") == "valid" else ""
    else:
        observed = {"status": "unknown", "reason": "custom_supply_requires_physical_manager_confirmation", "evidence": {}, "labels": {}}
        config = _digest({key: item[key] for key in item if key != "source"})
    return {**observed, "target": deepcopy(item), "configuration_digest": config,
        "digest": _digest({"item": item, "observation": observed})}


class WaitingListReadError(RuntimeError):
    pass


def waiting_list_projection(client_id, *, now=None, limit=16, include_manager_review=True, max_read_queries=192):
    """Private read model with a dedicated defensive bound, not a send gate.

    Shared prompt readers may request one descriptor without physically
    re-observing a manager decision. Its unknown physical state grants nothing.
    Completed rows alone survive a bounded/unstable read; coverage says when
    anything was omitted. The shared reader's original budget remains intact.
    """
    from django.db import connection, DatabaseError
    now = now or timezone.now()
    result = {"version": 2, "client_id": client_id, "captured_at": now.isoformat(), "lists": [],
        "coverage_complete": True, "reason": "", "native_future_capability": "unverified"}
    reads = 0
    limit = max(1, min(int(limit), 16))
    budget = max(1, min(int(max_read_queries), 192))
    def readonly(execute, sql, params, many, context):
        nonlocal reads
        if not sql.lstrip().upper().startswith("SELECT") or re.search(r"FOR\s+UPDATE|LOCK\s+IN\s+SHARE", sql, re.I):
            raise WaitingListReadError("waiting_list_side_effect_rejected")
        if reads >= budget:
            raise WaitingListReadError("waiting_list_read_budget_exceeded")
        reads += 1
        return execute(sql, params, many, context)
    try:
        with connection.execute_wrapper(readonly):
            _capture_projection(client_id, now=now, limit=limit, include_manager_review=include_manager_review, result=result)
    except (WaitingListReadError, DatabaseError) as error:
        result.update(coverage_complete=False, reason=str(error) if isinstance(error, WaitingListReadError) else "waiting_list_read_unavailable")
    result["diagnostics"] = {"read_only": True, "read_queries": reads, "max_read_queries": budget}
    return result


def _capture_projection(client_id, *, now, limit, include_manager_review, result):
    from management.models import IgClient, InstagramBotSettings
    from django.db.models import Exists, OuterRef
    restock = _restock()
    client = IgClient.objects.filter(pk=client_id, hidden_at__isnull=True, privacy_erasure_started_at__isnull=True).first()
    if not client:
        return result
    active = restock.Subscription.objects.filter(accepted_answer__invitation_id=OuterRef("pk"), state="active")
    invitations = list(restock.Invitation.objects.filter(client=client, target__schema=SCHEMA).annotate(
        _active_interest=Exists(active)).select_related("client", "revision", "request_source").order_by(
            "-_active_interest", "-created_at", "pk")[:limit + 1])
    result["coverage_complete"] = len(invitations) <= limit
    if not result["coverage_complete"]:
        result["reason"] = "waiting_list_history_bound"
    from management.services import ig_restock_waiting_list_review as review
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    for inv in invitations[:limit]:
        try:
            if not restock._verified(inv, "invitation") or _digest(inv.target) != inv.target_digest:
                result["coverage_complete"] = False
                result["reason"] = result["reason"] or "waiting_list_evidence_unverified"
                continue
            validate_target(inv.target)
            settings_obj = InstagramBotSettings.objects.filter(pk=inv.envelope.get("settings_id")).first()
            # Consent history is independent of a later service complaint,
            # token configuration or dispatch eligibility. The read descriptor
            # still checks exact native ownership, reset, epochs and receipts.
            source_reason = invitation_reason(inv, settings_obj, now=now, check_dispatch_scope=False) if settings_obj else "settings_missing"
            receipt = restock.Receipt.objects.select_related("effect").filter(invitation=inv).first()
            receipt_valid = bool(receipt and restock._valid_receipt(inv, receipt))
            answers = list(restock.Answer.objects.filter(invitation=inv).select_related("invitation", "receipt__effect", "source").order_by("pk"))
            accepted = next((row for row in answers if row.kind == "accept"), None)
            revoked = next((row for row in answers if row.kind == "revoke"), None)
            accepted_valid = bool(accepted and settings_obj and restock._valid_answer(accepted, settings_obj))
            state = "unverified" if source_reason else "expired" if now >= inv.expires_at else "revoked" if revoked and restock._valid_answer(revoked, settings_obj) else "accepted" if accepted_valid else "invited"
            subscription = restock.Subscription.objects.filter(accepted_answer=accepted).first() if accepted else None
            manager = review.projection(inv, subscription, now=now) if include_manager_review else {
                "ready": False, "request_id": None, "request_digest": None, "state": "unverified",
                "checked_at": None, "all_ready": False, "lines": []}
            # The shared agreement descriptor deliberately leaves notification
            # receipts unverified; the dedicated API verifies their full graph.
            event = restock.AvailabilityEvent.objects.select_related("task").filter(subscription=subscription).first() if subscription and include_manager_review else None
            task = event.task if event else None
            sent = bool(accepted_valid and receipt_valid and subscription and subscription.state == "sent"
                and subscription.client_id == client.pk and subscription.expires_at == inv.expires_at
                and task and restock._verified(event, "event") and task.status == "sent"
                and normalize_provider_message_id(task.provider_message_id)
                and task.sent_at and accepted.answered_at <= event.observed_at <= task.sent_at <= now
                and not task.claim_token and not task.claim_until and task.attempt_count == 1
                and task.message_text == copy_for(inv.locale, "notification", label(inv.target))
                and event.envelope.get("target_digest") == inv.target_digest and event.envelope.get("answer_hmac") == accepted.envelope_hmac
                and event.envelope.get("availability", {}).get("target") == inv.target
                and event.envelope.get("availability", {}).get("digest") == event.availability_digest
                and event.envelope.get("manager_decision", {}).get("completion") == "all"
                and event.envelope.get("manager_decision", {}).get("target_digest") == inv.target_digest
                and task.client_id == client.pk and task.event_key == event.event_key == f"{restock.ORIGIN}:{subscription.pk}"
                and task.reason == restock.ORIGIN and task.kind == "qualification" and task.trigger == "event"
                and task.policy_version == "restock-agreement.v1" and task.event_payload == {
                    "origin": restock.ORIGIN, "purpose": restock.PURPOSE, "subscription_id": str(subscription.pk), "event_id": str(event.pk)})
            result["lists"].append({"id": str(inv.pk), "version": 2, "completion": "all", "locale": inv.locale,
                "items": [{key: deepcopy(item[key]) for key in ("line_id", "recipient_id", "title", "color", "size", "fit", "options", "quantity", "item_kind")} for item in inv.target["items"]],
                "agreement": {"state": state, "accepted_at": accepted.answered_at.isoformat() if accepted_valid else None,
                    "expires_at": inv.expires_at.isoformat(), "invitation_receipt_confirmed": receipt_valid}, "manager_review": manager,
                "notification": {"state": "sent" if sent else "unknown" if subscription and subscription.state in {"sent", "unknown"} else "pending" if subscription and subscription.state == "active" else "not_sent",
                    "reason": subscription.last_reason if subscription else source_reason, "sent_at": task.sent_at.isoformat() if sent else None,
                    "native_receipt_confirmed": sent},
                "source_refs": [{"kind": "message", "id": row.pk, "event_at": row.provider_created_at.isoformat()}
                    for row in (inv.request_source, *([accepted.source] if accepted_valid else []),
                        *([revoked.source] if revoked and restock._valid_answer(revoked, settings_obj) else []))]})
        except (ValueError, KeyError, TypeError, AttributeError):
            result["coverage_complete"] = False
            result["reason"] = result["reason"] or "waiting_list_evidence_unverified"
    return result
