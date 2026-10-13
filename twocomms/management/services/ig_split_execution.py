"""One economic order, exact signed parcels and bounded manual dispatch.

No provider, payment, order creation or stock bypass lives here. An actual
customer choice owns delivery terms; the ordinary paid order owns the goods.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from management.ig_split_shipping_models import (IgSplitShippingChoice as Choice,
    IgSplitShippingOffer as Offer, IgSplitOrderPlan as Plan, IgSplitParcel as Parcel,
    IgSplitParcelDispatch as Dispatch)
from management.services import ig_split_shipping as shipping
from management.services import ig_fulfillment_reviews as reviews


class SplitExecutionError(ValueError):
    def __init__(self, reason):
        self.reason = str(reason)
        super().__init__(self.reason)


def _reject(reason):
    raise SplitExecutionError(reason)


def _cents(value):
    if isinstance(value, bool):
        _reject("split_amount_invalid")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        _reject("split_amount_invalid")
    if not amount.is_finite() or amount < 0 or amount > Decimal("1000000") or amount * 100 != (amount * 100).to_integral_value():
        _reject("split_amount_invalid")
    return int(amount * 100)


def _money(cents):
    return f"{Decimal(cents) / 100:.2f}"


def allocate_cents(total, weights):
    """Largest-remainder allocation conserves the single order's exact cents."""
    if (type(total) is not int or total < 0 or not isinstance(weights, (list, tuple)) or not weights
        or any(type(weight) is not int or weight < 0 for weight in weights)):
        _reject("split_money_allocation_invalid")
    denominator = sum(weights)
    if not denominator:
        if total:
            _reject("split_money_allocation_invalid")
        return [0] * len(weights)
    divided = [divmod(total * weight, denominator) for weight in weights]
    result = [value[0] for value in divided]
    for index in sorted(range(len(weights)), key=lambda index: (-divided[index][1], index))[:total - sum(result)]:
        result[index] += 1
    return result


def _actor(actor):
    from django.contrib.auth import get_user_model
    actual = get_user_model().objects.filter(pk=getattr(actor, "pk", None)).first()
    if not shipping._actor_allowed(actual):
        _reject("actor_forbidden")
    return actual


def verified_dispatch(dispatch):
    return shipping._verified(dispatch, "dispatch")


def _order_owner(order, client_id):
    from management.models import IgOrderAssignment, IgOrderAttribution
    assignment = IgOrderAssignment.objects.filter(order_id=order.pk).first()
    if assignment is not None:
        if assignment.client_id != client_id or assignment.unassigned_at is not None:
            _reject("split_order_owner_changed")
        return {"kind": "assignment", "id": assignment.pk, "version": assignment.version, "client_id": client_id}
    attribution = IgOrderAttribution.objects.filter(order_id=order.pk, client_id=client_id).first()
    if not attribution:
        _reject("split_order_owner_missing")
    return {"kind": "attribution", "id": attribution.pk, "client_id": client_id}


def _choice_frame(choice, now, *, historical=False, paid_execution=False):
    verified = shipping.validated_choice_history(choice)
    if not verified.get("ready"):
        _reject(verified.get("reason") or "split_choice_unverified")
    request = choice.offer.decision.request
    if not historical:
        # Order attachment is an approved source-owner transition. Renewed
        # terms for this same original purchase still supersede pending work;
        # an unrelated later purchase does not retarget the old order.
        latest = Offer.objects.filter(client_id=choice.client_id,
            decision__request_id=request.pk).order_by("-version", "-created_at", "-pk").first()
        if not latest or latest.pk != choice.offer_id or not shipping.verified_offer(latest):
            _reject("split_offer_superseded")
        decision = reviews.Decision.objects.filter(request=request).order_by("-version").first()
        if not decision or decision.pk != choice.offer.decision_id or not reviews._verified(decision, "decision"):
            _reject("split_decision_changed")
        clock = parse_datetime(decision.envelope.get("decided_at", ""))
        confirmed = parse_datetime(choice.envelope.get("confirmed_at", ""))
        if (not clock or timezone.is_naive(clock) or clock > now or clock > decision.created_at
            or decision.created_at > now or (not paid_execution and now - clock > timedelta(hours=24))
            or latest.created_at > now or not confirmed or timezone.is_naive(confirmed) or confirmed > now):
            _reject("manager_review_expired")
        decision_lines = decision.envelope.get("lines") or []
        expected = {row["line_id"]: row["target"]["quantity"] for row in request.envelope["lines"]}
        if (len(decision_lines) != len(expected) or {row.get("line_id") for row in decision_lines} != set(expected)
            or any(row.get("availability") != "available" or type(row.get("checked_quantity")) is not int
                or row["checked_quantity"] < expected[row["line_id"]] for row in decision_lines)):
            _reject("split_full_basket_not_ready")
        # A previously accepted, bound paid plan keeps its original logistics
        # terms. Live configuration and signed availability remain required;
        # expiry applies to creating a new plan, not dispatching its next parcel.
        if paid_execution:
            from management.services.ig_restock_availability import observe_configurations
            by_id = {row["line_id"]: row["target"] for row in request.envelope["lines"]}
            configs = observe_configurations([by_id[row["line_id"]] for row in decision_lines], now=now)
            reason = "configuration_changed" if len(configs) != len(decision_lines) or any(
                config.get("status") != "valid" or config.get("target") != by_id[row["line_id"]]
                or config.get("configuration_digest") != row.get("configuration_digest")
                for config, row in zip(configs, decision_lines)) else ""
        else:
            reason = reviews._decision_reason(request, decision, now=now)
        if reason:
            _reject(reason)
        if not shipping._actor_allowed(choice.offer.actor) or not shipping._actor_allowed(choice.offer.decision.actor):
            _reject("actor_forbidden")
        settings_obj = reviews._settings()
        from management.services.instagram_bot import ingress_provider_namespace
        data = request.envelope
        if (not settings_obj or not settings_obj.is_enabled or settings_obj.pk != data.get("settings_id")
            or settings_obj.reply_permission_epoch != data.get("settings_epoch")
            or ingress_provider_namespace(settings_obj) != data.get("source_namespace")
            or choice.client.reply_permission_epoch != data.get("client_epoch") or choice.client.privacy_erasure_started_at):
            _reject("split_permission_changed")
    try:
        parcels = verified["offer"]["options"][choice.option]["parcels"]
    except (KeyError, TypeError):
        _reject("split_choice_terms_invalid")
    if not isinstance(parcels, list) or not 1 <= len(parcels) <= 20:
        _reject("split_choice_terms_invalid")
    return verified, parcels


def _order_facts(order, choice, *, historical=False):
    """Read the actual winner and materializer's expanded source/order map."""
    from orders.models import PaymentAttempt
    from management.models import IgCheckoutProposal, IgCheckoutInvoiceGeneration
    from management.services.ig_checkout_cart_provenance import validate_attempt_provenance, bind_order_items, CartProvenanceError
    payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
    attempt = PaymentAttempt.objects.filter(pk=payload.get("attempt_id"), order_id=order.pk).first()
    if (not attempt or attempt.status != PaymentAttempt.Status.CONVERTED or not attempt.monobank_invoice_id
        or (not historical and (attempt.monobank_invoice_id != order.payment_invoice_id or attempt.paid_amount is None
            or _cents(attempt.paid_amount) <= 0))):
        _reject("split_verified_payment_missing")
    snapshot = attempt.cart_snapshot
    try:
        provenance = validate_attempt_provenance(snapshot)
    except CartProvenanceError as exc:
        _reject(exc.reason)
    if provenance.get("status") != "bound":
        _reject("split_order_source_unknown")
    owner = provenance["owner"]
    proposal = IgCheckoutProposal.objects.select_related("deal", "commercial_episode").filter(
        pk=owner["proposal_pk"], public_id=owner["proposal_id"], client_id=choice.client_id,
        revision=owner["revision"], deal_id=owner["deal_id"], commercial_episode_id=owner["episode_id"]).first()
    if (not proposal or proposal.status != proposal.Status.PAID or proposal.deal.order_id != order.pk
        or proposal.commercial_episode.intended_order_id != order.pk
        or proposal.payment_attempt_id != attempt.pk):
        _reject("split_paid_order_binding_changed")
    generation = IgCheckoutInvoiceGeneration.objects.filter(payment_attempt=attempt).first()
    if proposal.assisted_checkout_v2:
        if (not generation or generation.proposal_id != proposal.pk or generation.winner_slot != 1
            or proposal.winner_invoice_generation_id != generation.pk or not attempt.checkout_winner_claimed
            or generation.state != generation.State.PAID_WINNER):
            _reject("split_paid_generation_changed")
    elif generation is not None:
        _reject("split_paid_generation_changed")
    revision = proposal.revisions.filter(pk=owner["revision_id"], revision=proposal.revision).first()
    if (not revision or (revision.snapshot or {}).get("source_cart_binding") != provenance["binding"]):
        _reject("split_quote_source_changed")
    items = list(order.items.order_by("pk"))
    try:
        bound = bind_order_items(provenance, cart_items=snapshot["cart"], order_id=order.pk,
            order_items=[{"id": row.pk, "order_id": row.order_id, "product_id": row.product_id,
                "color_variant_id": row.color_variant_id, "qty": row.qty, "size": row.size,
                "fit_option_code": row.fit_option_code, "option_values": row.option_values,
                "unit_price": str(row.unit_price), "line_total": str(row.line_total)} for row in items])
    except CartProvenanceError as exc:
        _reject(exc.reason)
    if payload.get("source_cart_provenance") != bound:
        _reject("split_order_item_map_changed")
    request = choice.offer.envelope["request"]
    source_scope = request["source_scope"]
    binding = provenance["binding"]
    scope = binding["scope"]
    if any(source_scope.get(key) != scope.get(key) for key in ("client_id", "episode_id", "reset_floor", "source_namespace")):
        _reject("split_original_purchase_changed")
    for name, key in (("selection_session_id", "session_id"), ("selection_generation", "generation"), ("selection_revision", "selection_revision")):
        if source_scope.get(name) != binding.get(key):
            _reject("split_original_purchase_changed")
    expected = {(row["line_id"], row.get("recipient_id", "self")): row.get("target") for row in request["lines"]}
    actual = binding["quote_line_map"]
    if len(expected) != len(actual) or {(row["line_id"], row["recipient_id"]) for row in actual} != set(expected):
        _reject("split_full_order_coverage_missing")
    for row in actual:
        target = expected[row["line_id"], row["recipient_id"]]
        config = row["configuration"]
        if not target or any(target.get(key) != config.get(mapped) for key, mapped in (
            ("product_id", "product_id"), ("color_variant_id", "color_variant_id"), ("size", "size"),
            ("fit_option_code", "fit_option_code"), ("option_values", "option_values"), ("quantity", "qty"))):
            _reject("split_original_item_changed")
    gross = sum(_cents(row.line_total) for row in items)
    if gross != _cents(attempt.gross_amount) or (not historical and (gross != _cents(order.total_sum)
        or _cents(order.discount_amount) != _cents(attempt.discount_amount))):
        _reject("split_order_amount_changed")
    fixed = {"proposal_id": proposal.pk, "proposal_public_id": str(proposal.public_id),
        "proposal_revision": proposal.revision, "checkout_revision_id": revision.pk,
        "invoice_generation_id": generation.pk if generation else None, "payment_attempt_id": attempt.pk,
        "order_id": order.pk, "client_id": choice.client_id, "source_provenance_digest": reviews._digest(bound),
        "owner": _order_owner(order, choice.client_id), "gross_amount": _money(gross),
        "discount_amount": _money(_cents(attempt.discount_amount)), "item_map": bound["item_map"], "items": items}
    if historical:
        return fixed
    from orders.nova_poshta_documents import build_order_payment_snapshot
    payment = build_order_payment_snapshot(order)
    paid = _cents(attempt.paid_amount)
    if (payment["automatic_fulfillment_blocked"] or payment["delivery_payment_requires_manual"]
        or _cents(payload.get("paid_amount", payload.get("paid_value", "0"))) != paid
        or order.pay_type != attempt.pay_type or order.payment_status not in {"paid", "prepaid"}):
        _reject("split_order_payment_requires_manual")
    payable = _cents(payment["payable_total"])
    remaining = max(payable - paid, 0)
    if (paid > payable or (order.payment_status == "paid" and remaining)
        or _cents(payment["cod_amount"]) != remaining):
        _reject("split_cod_authority_missing")
    net_goods = gross - _cents(order.discount_amount)
    if not 0 <= remaining <= net_goods:
        _reject("split_cod_authority_missing")
    return {**fixed, "merchandise_amount": _money(net_goods),
        "paid_amount": _money(paid), "payable_amount": _money(payable), "cod_amount": _money(remaining),
        "pay_type": order.pay_type, "payment_status": order.payment_status,
        "delivery_contract_digest": reviews._digest((order.payment_payload or {}).get("delivery_payment") or {})}


def _allocations(choice, terms, facts):
    by_id = {row.pk: row for row in facts["items"]}
    mappings = facts["item_map"]
    all_lines = {row["line_id"] for row in mappings}
    selected = [line for parcel in terms for line in parcel.get("line_ids", [])]
    if len(set(selected)) != len(selected) or set(selected) != all_lines:
        _reject("split_full_order_coverage_missing")
    result, weights = [], []
    for term in terms:
        if term.get("payer") not in {"customer", "merchant"} or term.get("currency") != "UAH":
            _reject("split_choice_terms_invalid")
        cap = _cents(term.get("customer_delivery_cap"))
        if cap > 10000000 or term["payer"] == "merchant" and cap:
            _reject("split_delivery_cap_invalid")
        rows = []
        for mapping in mappings:
            if mapping["line_id"] not in term["line_ids"]:
                continue
            item = by_id[mapping["order_item_id"]]
            rows.append({"line_id": mapping["line_id"], "recipient_id": mapping["recipient_id"],
                "proposal_item_id": mapping["proposal_item_id"], "order_item_id": item.pk,
                "quantity": item.qty, "configuration": deepcopy(mapping["configuration"]),
                "unit_price": f"{item.unit_price:.2f}", "line_total": f"{item.line_total:.2f}"})
        if not rows:
            _reject("split_parcel_empty")
        weights.append(sum(_cents(row["line_total"]) for row in rows))
        result.append({"key": term["key"], "order_id": facts["order_id"], "items": rows,
            "source_line_ids": list(term["line_ids"]), "payer": term["payer"],
            "customer_delivery_cap": _money(cap), "currency": "UAH"})
    net = allocate_cents(_cents(facts["merchandise_amount"]), weights)
    cod = allocate_cents(_cents(facts["cod_amount"]), net)
    for parcel, goods, due in zip(result, net, cod):
        parcel.update(merchandise_amount=_money(goods), cod_amount=_money(due))
    return result


def _dispatch_reason(dispatch, parcel, plan):
    from management.models import IgOrderShipment
    if (not verified_dispatch(dispatch) or dispatch.parcel_id != parcel.pk
        or dispatch.envelope.get("plan_digest") != plan.envelope_hmac
        or dispatch.envelope.get("parcel_digest") != parcel.envelope_hmac):
        return "split_dispatch_unverified"
    shipment = dispatch.shipment
    allocation = parcel.envelope
    if (shipment.order_id != plan.order_id or shipment.direction != IgOrderShipment.Direction.OUTBOUND
        or shipment.purpose != IgOrderShipment.Purpose.SPLIT_INITIAL or shipment.supersedes_id is not None
        or shipment.tracking_number != dispatch.tracking_number
        or shipment.payer != (IgOrderShipment.Payer.CUSTOMER if allocation["payer"] == "customer" else IgOrderShipment.Payer.SHOP)
        or shipment.created_by_id != dispatch.actor_id or dispatch.cod_amount != Decimal(allocation["cod_amount"])
        or _cents(dispatch.customer_delivery_amount) > _cents(allocation["customer_delivery_cap"])
        or allocation["payer"] == "merchant" and dispatch.customer_delivery_amount != 0):
        return "split_dispatch_binding_changed"
    receipt = dispatch.envelope.get("inventory_receipt")
    if (not isinstance(receipt, dict) or receipt.get("schema") != "ig-split-inventory-receipt.v1"
        or receipt.get("dispatch_id") != str(dispatch.pk) or receipt.get("parcel_id") != str(parcel.pk)
        or receipt.get("order_id") != plan.order_id or receipt.get("plan_id") != str(plan.pk)
        or receipt.get("proposal_id") != plan.envelope.get("proposal_id")
        or receipt.get("invoice_generation_id") != plan.envelope.get("invoice_generation_id")):
        return "split_inventory_receipt_missing"
    return ""


def _validated_plan(plan_or_id, *, now, historical=False):
    from orders.models import Order
    identity = getattr(plan_or_id, "pk", plan_or_id)
    plan = Plan.objects.select_related("choice__offer__decision__request", "choice__offer__decision__actor",
        "choice__offer__actor", "choice__receipt__task", "choice__source", "choice__revision", "choice__client", "order").filter(pk=identity).first()
    if not plan or not shipping._verified(plan, "plan"):
        _reject("split_plan_unverified")
    choice = plan.choice
    if not historical:
        bound_at = parse_datetime(plan.envelope.get("bound_at", ""))
        decision_at = parse_datetime(choice.offer.decision.envelope.get("decided_at", ""))
        confirmed_at = parse_datetime(choice.envelope.get("confirmed_at", ""))
        if (not bound_at or not decision_at or not confirmed_at
            or any(timezone.is_naive(value) for value in (bound_at, decision_at, confirmed_at))
            or bound_at > now or plan.created_at > now or decision_at > bound_at
            or confirmed_at > bound_at or bound_at - decision_at > timedelta(hours=24)):
            _reject("split_initial_binding_clock_invalid")
    _choice_frame(choice, now, historical=historical, paid_execution=not historical)
    facts = _order_facts(plan.order, choice, historical=historical)
    original_facts = plan.envelope.get("order_facts")
    if not isinstance(original_facts, dict):
        _reject("split_plan_binding_changed")
    if historical:
        # A later legitimate balance payment cannot erase a physical parcel.
        # Original COD remains an immutable dispatch fact, while current money
        # must be rechecked independently for every still-pending dispatch.
        fixed = {key: value for key, value in facts.items() if key not in {"items", "item_map", "owner"}}
        if any(original_facts.get(key) != value for key, value in fixed.items()):
            _reject("split_plan_binding_changed")
        immutable = deepcopy(original_facts)
        facts = {**facts, **original_facts}
    else:
        if plan.order.status in {"cancelled", "refunded"}:
            _reject("split_order_unavailable")
        immutable = {key: value for key, value in facts.items() if key not in {"items", "item_map"}}
    if (plan.client_id != choice.client_id or plan.envelope.get("choice_digest") != choice.envelope_hmac
        or plan.envelope.get("order_facts") != immutable):
        _reject("split_plan_binding_changed")
    terms = choice.offer.envelope["options"][choice.option]["parcels"]
    expected = _allocations(choice, terms, facts)
    parcels = list(Parcel.objects.filter(plan=plan).order_by("created_at", "pk"))
    by_key = {row.key: row for row in parcels}
    if len(by_key) != len(expected) or set(by_key) != {row["key"] for row in expected}:
        _reject("split_manifest_coverage_changed")
    ordered = []
    for allocation in expected:
        parcel = by_key[allocation["key"]]
        if (not shipping._verified(parcel, "parcel") or parcel.envelope.get("plan_digest") != plan.envelope_hmac
            or any(parcel.envelope.get(key) != value for key, value in allocation.items())):
            _reject("split_parcel_allocation_changed")
        ordered.append(parcel)
    dispatches = list(Dispatch.objects.filter(parcel__plan=plan).select_related("shipment").order_by("created_at", "pk"))
    for dispatch in dispatches:
        parcel = next(row for row in ordered if row.pk == dispatch.parcel_id)
        reason = _dispatch_reason(dispatch, parcel, plan)
        if reason:
            _reject(reason)
    proof = {"plan_id": str(plan.pk), "plan_digest": plan.envelope_hmac,
        "choice_id": str(choice.pk), "choice_digest": choice.envelope_hmac,
        "order_facts_digest": reviews._digest(immutable),
        "parcels": [{"id": str(row.pk), "digest": row.envelope_hmac} for row in ordered],
        "dispatches": [{"id": str(row.pk), "digest": row.envelope_hmac, "parcel_id": str(row.parcel_id),
            "tracking_number": row.tracking_number} for row in dispatches]}
    return {"ready": True, "reason": "", "plan": plan, "choice": choice, "order": plan.order,
        "parcels": ordered, "dispatches": dispatches, "proof": proof}


def validated_split_plan(plan_or_id, *, now=None):
    try:
        return _validated_plan(plan_or_id, now=now or timezone.now())
    except SplitExecutionError as exc:
        return {"ready": False, "reason": exc.reason}


def verified_tracking_plan(order, *, now=None):
    plan = Plan.objects.filter(order_id=order.pk).first()
    if not plan:
        return {"ready": False, "reason": "split_plan_missing"}
    try:
        return _validated_plan(plan.pk, now=now or timezone.now(), historical=True)
    except SplitExecutionError as exc:
        return {"ready": False, "reason": exc.reason}


def bind_split_plan(choice_id, order_id, *, actor, operation_id=None, now=None):
    from orders.models import Order
    from management.models import IgClient, AdminAuditLog
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    now = now or timezone.now()
    try:
        if not reviews._enabled():
            _reject("disabled")
        actor = _actor(actor)
        try:
            operation_id = uuid.UUID(str(operation_id)) if operation_id is not None else uuid.uuid4()
        except (TypeError, ValueError):
            _reject("operation_invalid")
        choice = Choice.objects.select_related("client").filter(pk=choice_id).first()
        if not choice:
            _reject("split_choice_missing")
        with commercial_episode_client_lock(choice.client_id), transaction.atomic():
            IgClient.objects.select_for_update().get(pk=choice.client_id)
            order = Order.objects.select_for_update().filter(pk=order_id).first()
            if not order:
                _reject("split_order_unavailable")
            choice = Choice.objects.select_for_update().select_related("offer__decision__request", "offer__decision__actor",
                "offer__actor", "receipt__task", "client", "source", "revision").get(pk=choice.pk)
            existing = Plan.objects.filter(order=order).first()
            if existing:
                if existing.choice_id != choice.pk:
                    _reject("split_order_already_planned")
                validated = _validated_plan(existing.pk, now=now, historical=True)
                return {"ready": True, "reason": "", "created": False, "plan_id": str(existing.pk),
                    "parcel_ids": [str(row.pk) for row in validated["parcels"]]}
            _assert_initial_order_unshipped(order)
            _, terms = _choice_frame(choice, now)
            facts = _order_facts(order, choice)
            if Plan.objects.filter(choice=choice).exists():
                _reject("split_choice_already_bound")
            immutable = {key: value for key, value in facts.items() if key not in {"items", "item_map"}}
            allocations = _allocations(choice, terms, facts)
            plan = shipping._seal(Plan(choice=choice, order=order, client_id=choice.client_id), "plan",
                {**immutable, "choice_id": str(choice.pk), "choice_digest": choice.envelope_hmac,
                    "order_facts": immutable, "bound_at": now.isoformat(), "operation_id": str(operation_id)})
            parcels = [shipping._seal(Parcel(plan=plan, key=allocation["key"]), "parcel",
                {**allocation, "plan_id": str(plan.pk), "plan_digest": plan.envelope_hmac,
                    "choice_id": str(choice.pk), "choice_digest": choice.envelope_hmac}) for allocation in allocations]
            AdminAuditLog.objects.create(actor=actor, action="ig_split_plan_bound", entity_type="IgSplitOrderPlan",
                entity_id=str(plan.pk), before={}, after={"order_id": order.pk, "choice_id": str(choice.pk),
                    "parcel_ids": [str(row.pk) for row in parcels]}, reason="Exact customer-chosen full economic order parcel manifest")
            return {"ready": True, "reason": "", "created": True, "plan_id": str(plan.pk), "parcel_ids": [str(row.pk) for row in parcels]}
    except SplitExecutionError as exc:
        return {"ready": False, "reason": exc.reason, "created": False}


def record_split_dispatch(parcel_id, *, actor, operation_id, tracking_number, customer_delivery_amount, cod_amount, now=None):
    from orders.models import Order
    from management.models import IgClient, IgOrderShipment, AdminAuditLog
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    from management.services.ig_shipments import normalize_tracking
    from management.services.ig_inventory import InventoryReservationError, fulfill_split_parcel_inventory
    now = now or timezone.now()
    try:
        if not reviews._enabled():
            _reject("disabled")
        actor = _actor(actor)
        try:
            operation_id = uuid.UUID(str(operation_id))
        except (TypeError, ValueError):
            _reject("operation_invalid")
        tracking = normalize_tracking(tracking_number)
        if not tracking:
            _reject("split_tracking_invalid")
        fee, cod = _cents(customer_delivery_amount), _cents(cod_amount)
        parcel = Parcel.objects.select_related("plan").filter(pk=parcel_id).first()
        if not parcel:
            _reject("split_parcel_missing")
        with commercial_episode_client_lock(parcel.plan.client_id), transaction.atomic():
            IgClient.objects.select_for_update().get(pk=parcel.plan.client_id)
            order = Order.objects.select_for_update().get(pk=parcel.plan.order_id)
            plan = Plan.objects.select_for_update().get(pk=parcel.plan_id)
            parcel = Parcel.objects.select_for_update().get(pk=parcel.pk)
            existing = Dispatch.objects.select_related("shipment").filter(parcel=parcel).first()
            if existing:
                _validated_plan(plan.pk, now=now, historical=True)
                if (existing.operation_id != operation_id or existing.actor_id != actor.pk
                    or existing.tracking_number != tracking or _cents(existing.customer_delivery_amount) != fee
                    or _cents(existing.cod_amount) != cod or _dispatch_reason(existing, parcel, plan)):
                    _reject("split_dispatch_conflict")
                return {"ready": True, "reason": "", "created": False, "dispatch_id": str(existing.pk), "shipment_id": existing.shipment_id}
            validated = _validated_plan(plan.pk, now=now)
            if Dispatch.objects.filter(operation_id=operation_id).exists():
                _reject("operation_conflict")
            if (order.status in {"cancelled", "refunded"} or cod != _cents(parcel.envelope["cod_amount"])
                or fee > _cents(parcel.envelope["customer_delivery_cap"])
                or parcel.envelope["payer"] == "merchant" and fee):
                _reject("split_dispatch_terms_changed")
            if IgOrderShipment.objects.filter(tracking_number=tracking, direction=IgOrderShipment.Direction.OUTBOUND).exists():
                _reject("split_tracking_already_used")
            shipment = IgOrderShipment.objects.create(order=order, direction=IgOrderShipment.Direction.OUTBOUND,
                purpose=IgOrderShipment.Purpose.SPLIT_INITIAL, tracking_number=tracking,
                source=IgOrderShipment.Source.MANAGER_MANUAL, created_by=actor,
                payer=IgOrderShipment.Payer.CUSTOMER if parcel.envelope["payer"] == "customer" else IgOrderShipment.Payer.SHOP,
                evidence_message_id=validated["choice"].source_id, note=f"Split parcel {parcel.pk}; operation {operation_id}")
            dispatch = Dispatch(parcel=parcel, actor=actor, operation_id=operation_id, shipment=shipment,
                tracking_number=tracking, customer_delivery_amount=Decimal(_money(fee)), cod_amount=Decimal(_money(cod)))
            try:
                inventory = fulfill_split_parcel_inventory(parcel, dispatch, actor, now=now)
            except InventoryReservationError as exc:
                _reject(exc.reason)
            if not isinstance(inventory, dict) or not inventory.get("ready") or not isinstance(inventory.get("proof"), dict):
                _reject("split_inventory_receipt_missing")
            shipping._seal(dispatch, "dispatch", {"plan_id": str(plan.pk), "plan_digest": plan.envelope_hmac,
                "parcel_id": str(parcel.pk), "parcel_digest": parcel.envelope_hmac,
                "inventory_receipt": inventory["proof"], "recorded_at": now.isoformat()})
            if order.status in {"new", "prep"}:
                required = {row.pk: row for row in validated["parcels"]}
                sealed = list(Dispatch.objects.filter(parcel__plan=plan).select_related("shipment")[:len(required) + 1])
                if len(sealed) == len(required) and {row.parcel_id for row in sealed} == set(required):
                    for receipt in sealed:
                        reason = _dispatch_reason(receipt, required[receipt.parcel_id], plan)
                        if reason:
                            _reject(reason)
                    # The scalar order becomes shipped only after its entire
                    # exact manifest has physical dispatch receipts. Partial
                    # handoffs remain visible solely on their own parcels.
                    # Project only physical status, like the tracking
                    # aggregate. Money, loyalty and scalar TTN lifecycles
                    # retain their existing owners.
                    if Order.objects.filter(pk=order.pk, status__in=["new", "prep"]).update(status="ship"):
                        order.status = "ship"
            AdminAuditLog.objects.create(actor=actor, action="ig_split_parcel_dispatched", entity_type="IgSplitParcelDispatch",
                entity_id=str(dispatch.pk), before={}, after={"order_id": order.pk, "parcel_id": str(parcel.pk),
                    "shipment_id": shipment.pk, "customer_delivery_amount": _money(fee), "cod_amount": _money(cod)},
                reason="Actual manager-entered parcel tracking under exact customer delivery terms")
            from management.services.ig_order_fulfillment import kick_order_fulfillment
            transaction.on_commit(lambda: kick_order_fulfillment(order.pk))
            return {"ready": True, "reason": "", "created": True, "dispatch_id": str(dispatch.pk), "shipment_id": shipment.pk}
    except SplitExecutionError as exc:
        return {"ready": False, "reason": exc.reason, "created": False}
    except IntegrityError:
        # The physical UNIQUE TTN is the final concurrent-order fence. This
        # read happens after the outer transaction rolled back its inventory,
        # journal and receipt writes, so the winner is visible here.
        reason = "split_tracking_already_used" if Dispatch.objects.filter(tracking_number=tracking).exists() else "split_dispatch_conflict"
        return {"ready": False, "reason": reason, "created": False}


def _assert_initial_order_unshipped(order):
    from management.models import IgOrderShipment, IgCheckoutInventoryReservation as Reservation
    from warehouse.models import WriteOffRequest, StockMovement, MovementReason
    if order.status in {"done", "cancelled", "refunded"} or order.tracking_number:
        _reject("split_order_already_shipped")
    if (IgOrderShipment.objects.filter(order=order, direction=IgOrderShipment.Direction.OUTBOUND).exists()
        or Reservation.objects.filter(order=order, allocation_source="warehouse", fulfilled_quantity__gt=0).exists()
        or Reservation.objects.filter(order=order, allocation_source="warehouse", state__in=[Reservation.State.FULFILLED, Reservation.State.CONSUMED]).exists()
        or WriteOffRequest.objects.filter(order=order, status=WriteOffRequest.STATUS_COMPLETED).exists()
        or StockMovement.objects.filter(order=order, reason=MovementReason.ORDER_WRITE_OFF).exists()):
        _reject("split_order_already_shipped")


def assert_legacy_waybill_allowed(order):
    if Plan.objects.filter(order_id=order.pk).exists():
        _reject("split_manifest_required")


def execution_projection(client_id, *, now=None):
    now = now or timezone.now()
    result = {"version": 1, "plans": [], "can_bind_order": False, "bindable_orders": []}
    for plan in Plan.objects.filter(client_id=client_id).select_related("order").order_by("-created_at")[:20]:
        live = validated_split_plan(plan.pk, now=now)
        checked = verified_tracking_plan(plan.order, now=now)
        shown = {"id": str(plan.pk), "order_id": plan.order_id, "order_number": plan.order.order_number,
            "choice_id": str(checked["choice"].pk) if checked["ready"] else None,
            "ready": checked["ready"], "can_dispatch": live["ready"],
            "reason": live.get("reason", "") or checked.get("reason", ""), "parcels": []}
        if checked["ready"]:
            dispatches = {row.parcel_id: row for row in checked["dispatches"]}
            from management.ig_split_shipping_models import IgSplitParcelTracking
            tracking = {row.parcel_id: row for row in IgSplitParcelTracking.objects.filter(parcel__plan=plan)}
            labels = {row["line_id"]: row.get("label") or f"Позиція {index + 1}"
                for index, row in enumerate(checked["choice"].offer.envelope["request"]["lines"])}
            for parcel in checked["parcels"]:
                dispatch, tracked = dispatches.get(parcel.pk), tracking.get(parcel.pk)
                grouped = {}
                for item in parcel.envelope["items"]:
                    line = grouped.setdefault(item["line_id"], {"line_id": item["line_id"],
                        "label": labels[item["line_id"]], "quantity": 0})
                    line["quantity"] += item["quantity"]
                shown["parcels"].append({"id": str(parcel.pk), "key": parcel.key, "label": parcel.key,
                    "items": deepcopy(parcel.envelope["items"]), "lines": list(grouped.values()),
                    "can_dispatch": live["ready"] and dispatch is None, "cod_amount": parcel.envelope["cod_amount"],
                    "payer": parcel.envelope["payer"], "customer_delivery_cap": parcel.envelope["customer_delivery_cap"],
                    "dispatch": {"id": str(dispatch.pk), "tracking_number": dispatch.tracking_number,
                        "customer_delivery_amount": format(dispatch.customer_delivery_amount, ".2f"),
                        "cod_amount": format(dispatch.cod_amount, ".2f")} if dispatch else None,
                    "tracking": {"code": tracked.code, "status": tracked.status,
                        "provider_event_at": tracked.provider_event_at.isoformat() if tracked.provider_event_at else None,
                        "checked_at": tracked.checked_at.isoformat() if tracked.checked_at else None,
                        "terminal_at": tracked.terminal_at.isoformat() if tracked.terminal_at else None} if tracked else None})
        result["plans"].append(shown)
    from orders.models import Order
    for choice in Choice.objects.filter(client_id=client_id, plan__isnull=True).select_related("offer__decision__request",
        "offer__decision__actor", "offer__actor", "client", "receipt__task", "source", "revision").order_by("-created_at")[:10]:
        candidates = Order.objects.filter(payment_payload__source_cart_provenance__owner__client_id=client_id).order_by("-created")[:20]
        for order in candidates:
            if order.status in {"cancelled", "refunded"} or Plan.objects.filter(order=order).exists():
                continue
            try:
                _, terms = _choice_frame(choice, now)
                _assert_initial_order_unshipped(order)
                facts = _order_facts(order, choice)
                _allocations(choice, terms, facts)
            except SplitExecutionError:
                continue
            result["bindable_orders"].append({"choice_id": str(choice.pk), "id": order.pk,
                "order_id": order.pk, "number": order.order_number, "order_number": order.order_number})
    result["can_bind_order"] = bool(result["bindable_orders"]) and reviews._enabled()
    return result
