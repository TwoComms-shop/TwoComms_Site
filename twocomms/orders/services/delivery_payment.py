"""Pure, audited shipping charge contract; never queries payment/provider truth.

Authenticated writers supply verified payment/allocation facts. Carrier charges
paid by the recipient are not money owed to the merchant, and merchant-funded
shipping is not a customer prepayment.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
from itertools import islice


SCHEMA = "order-delivery-payment.v1"
MODES = frozenset({"carrier_recipient", "customer_prepaid", "merchant_free"})
LABELS = {
    "carrier_recipient": "Доставку оплачує одержувач Новій пошті",
    "customer_prepaid": "Клієнт оплатив доставку нам",
    "merchant_free": "Доставку оплачує магазин",
}
ZERO = Decimal("0.00")
MAX_AMOUNT = Decimal("9999999999.99")
CUSTOM_PRINT_INCLUDED_SCHEMA = "custom-print-delivery-included.v1"
THRESHOLD_SCHEMA = "order-threshold-shipping.v1"


def build_threshold_delivery_contract(attempt, *, proposal, generation=None):
    """Bind a server-frozen ordinary checkout promotion to native identities.

    Writers persist the returned value in both Attempt.cart_snapshot under
    threshold_delivery_contract and Order.payment_payload under delivery_payment.
    Neither a copied Order JSON flag nor a live threshold grants this authority.
    """
    from orders.services.shipping_policy import ShippingPolicyError, verify_shipping_snapshot
    snapshot = getattr(attempt, 'cart_snapshot', None)
    if (not isinstance(snapshot, dict) or snapshot.get('checkout_surface') != 'instagram_proposal'
            or snapshot.get('proposal_id') != str(proposal.public_id)
            or not _id(getattr(attempt, 'pk', None)) or not _id(getattr(proposal, 'pk', None))
            or any(key in snapshot for key in ('assisted_execution', 'assisted_commerce', 'custom_print_creation',
                'delivery_payment', 'instagram_delivery_contract', 'ig_assisted_shipping'))):
        raise DeliveryPaymentError('threshold_shipping_owner_invalid')
    if (generation is not None and (generation.proposal_id != proposal.pk
            or generation.payment_attempt_id != attempt.pk
            or generation.proposal_revision != proposal.revision)):
        raise DeliveryPaymentError('threshold_shipping_generation_invalid')
    try:
        shipping = verify_shipping_snapshot(snapshot.get('shipping'))
    except ShippingPolicyError as exc:
        raise DeliveryPaymentError(exc.reason) from exc
    items = _items(snapshot.get('cart'))
    digest = threshold_shipping_cart_digest(snapshot.get('cart'))
    gross, discount = _money(attempt.gross_amount), _money(attempt.discount_amount)
    net = gross - discount
    if (items is None or gross != items['gross_total'] or net <= ZERO
            or _money(attempt.payable_amount) != net
            or _money(shipping['gross_total']) != gross
            or _money(shipping['discount_amount']) != discount
            or _money(shipping['basis_total']) != net
            or shipping['cart_digest'] != digest):
        raise DeliveryPaymentError('threshold_shipping_goods_changed')
    owner = {'attempt_id': attempt.pk, 'proposal_id': str(proposal.public_id),
        'proposal_pk': proposal.pk, 'revision': proposal.revision,
        'items_digest': proposal.items_digest, 'client_id': proposal.client_id,
        'episode_id': proposal.commercial_episode_id,
        'generation_id': generation.pk if generation is not None else None}
    for key, value in shipping['owner'].items():
        if key not in owner or owner[key] != value:
            raise DeliveryPaymentError('threshold_shipping_owner_changed')
    shipping = {**shipping, 'owner': owner}
    return {'schema': THRESHOLD_SCHEMA, 'authority': 'automatic_threshold',
        'mode': shipping['mode'], 'payer_type': shipping['payer_type'],
        'merchandise_total': f'{net:.2f}', 'delivery_amount': '0.00', 'payable_total': f'{net:.2f}',
        'items_digest': digest, 'quantity_total': items['quantity_total'],
        'shipping': shipping}


def threshold_shipping_cart_digest(item_rows):
    """Use the same actual goods identity for admission and Order validation."""
    if item_rows is None:
        raise DeliveryPaymentError('threshold_shipping_items_missing')
    rows = list(islice(item_rows, 201))
    items = _items(rows)
    if items is None:
        raise DeliveryPaymentError('threshold_shipping_items_missing')
    # Unlike historical manager contracts, new automatic terms also bind the
    # configuration axes. Preserve the historic manager digest unchanged.
    configured = []
    for row in rows:
        values = _get(row, 'option_values', {}) or {}
        if not isinstance(values, dict):
            raise DeliveryPaymentError('threshold_shipping_items_invalid')
        identity = {key: str(_get(row, key, '') or '') for key in
            ('product_id', 'color_variant_id', 'title', 'size', 'fit_option_code')}
        identity.update(qty=_get(row, 'qty'), unit_price=f"{_money(_get(row, 'unit_price')):.2f}",
            option_values=values, is_custom=bool(_get(row, 'is_custom', False)))
        configured.append(identity)
    try:
        encoded = sorted(json.dumps(row, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
            allow_nan=False) for row in configured)
        body = json.dumps({'goods': items['digest'], 'configurations': encoded}, separators=(',', ':'))
    except (TypeError, ValueError, RecursionError):
        raise DeliveryPaymentError('threshold_shipping_items_invalid') from None
    return hashlib.sha256(body.encode()).hexdigest()


def _threshold_shipping_snapshot(order, contract, merchandise, *, item_rows):
    """Read native checkout backreferences before accepting automatic terms."""
    from orders.models import PaymentAttempt
    from management.models import IgCheckoutInvoiceGeneration, IgCheckoutProposal
    try:
        if item_rows is not None:
            item_rows = list(islice(item_rows, 201))
        if not isinstance(contract, dict) or not isinstance(contract.get('shipping'), dict):
            raise DeliveryPaymentError('threshold_shipping_contract_invalid')
        owner = contract['shipping'].get('owner')
        if (not isinstance(owner, dict) or set(owner) != {'attempt_id', 'proposal_id', 'proposal_pk',
                'revision', 'items_digest', 'client_id', 'episode_id', 'generation_id'}
                or any(not _id(owner.get(key)) for key in
                    ('attempt_id', 'proposal_pk', 'revision', 'client_id', 'episode_id'))
                or not isinstance(owner.get('proposal_id'), str)
                or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', owner['proposal_id'])
                or not isinstance(owner.get('items_digest'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', owner['items_digest'])
                or owner.get('generation_id') is not None and not _id(owner['generation_id'])
                or not _id(getattr(order, 'pk', None))):
            raise DeliveryPaymentError('threshold_shipping_owner_invalid')
        # The Order payload is only a locator. Require the persisted native
        # attempt's reverse Order reference and its pre-provider frozen copy.
        attempt = PaymentAttempt.objects.filter(pk=owner['attempt_id'], order_id=order.pk).first()
        if attempt is None or (attempt.cart_snapshot or {}).get('threshold_delivery_contract') != contract:
            raise DeliveryPaymentError('threshold_shipping_native_binding_changed')
        proposal = IgCheckoutProposal.objects.select_related('deal', 'commercial_episode').filter(pk=owner.get('proposal_pk'),
            public_id=owner.get('proposal_id'), client_id=owner.get('client_id'),
            commercial_episode_id=owner.get('episode_id')).first()
        if proposal is None:
            raise DeliveryPaymentError('threshold_shipping_proposal_missing')
        if (proposal.deal.client_id != proposal.client_id
                or proposal.commercial_episode.client_id != proposal.client_id
                or proposal.commercial_episode.deal_id != proposal.deal_id):
            raise DeliveryPaymentError('threshold_shipping_episode_changed')
        generation = None
        if owner.get('generation_id') is not None:
            generation = IgCheckoutInvoiceGeneration.objects.filter(pk=owner['generation_id'],
                proposal_id=proposal.pk, payment_attempt_id=attempt.pk,
                proposal_revision=owner.get('revision')).first()
            if (generation is None or proposal.winner_invoice_generation_id != generation.pk
                    or generation.winner_slot != 1
                    or generation.state not in {'winner_claimed', 'paid_winner'}):
                raise DeliveryPaymentError('threshold_shipping_winner_changed')
        elif proposal.payment_attempt_id != attempt.pk:
            raise DeliveryPaymentError('threshold_shipping_attempt_changed')
        if proposal.revision != owner.get('revision') or proposal.items_digest != owner.get('items_digest'):
            raise DeliveryPaymentError('threshold_shipping_revision_changed')
        normalized = build_threshold_delivery_contract(attempt, proposal=proposal, generation=generation)
        items = _items(item_rows)
        if (normalized != contract or items is None
                or threshold_shipping_cart_digest(item_rows) != contract.get('items_digest')
                or items['quantity_total'] != contract.get('quantity_total')
                or _money(order.total_sum) != _money(attempt.gross_amount)
                or _money(order.discount_amount or '0') != _money(attempt.discount_amount)
                or merchandise != _money(contract.get('merchandise_total'))):
            raise DeliveryPaymentError('threshold_shipping_order_changed')
        result = _snapshot(merchandise, contract=contract, valid=True, source='automatic_threshold')
        return {**result, 'source_locked': True, 'shipping': dict(contract['shipping'])}
    except (DeliveryPaymentError, TypeError, ValueError, KeyError, AttributeError) as exc:
        return {**_snapshot(merchandise, valid=False, reason=getattr(exc, 'reason', 'threshold_shipping_contract_invalid'),
            source='automatic_threshold'), 'source_locked': True}


def build_custom_print_delivery_contract(*, gross_total, discount_amount, creation_data, order_id=None):
    """Freeze an agreed web charge included in the total, without claiming payment."""
    groups = creation_data.get("groups", []) if isinstance(creation_data, dict) else []
    if not isinstance(groups, list) or len(groups) > 100 or any(not isinstance(group, dict) for group in groups):
        raise DeliveryPaymentError("custom_print_delivery_binding_invalid")
    owners = []
    for group in groups:
        gift = group.get("gift") or {}
        if not isinstance(gift, dict) or not isinstance(gift.get("delivery", {}), dict):
            raise DeliveryPaymentError("custom_print_delivery_binding_invalid")
        delivery = gift.get("delivery") or {}
        if not group.get("applied") or not delivery.get("enabled"):
            continue
        owner_id = group.get("owner_lead_id")
        if (not _id(owner_id) or owner_id not in group.get("included_lead_ids", [])
                or group.get("owner_status") != "approved" or group.get("source") not in {"custom_print_cart", "main_custom_print"}
                or delivery.get("method") not in {"branch", "courier"}):
            raise DeliveryPaymentError("custom_print_delivery_binding_invalid")
        fee = _money(delivery.get("price"), positive=True)
        if fee >= _money(group.get("owner_price"), positive=True):
            raise DeliveryPaymentError("custom_print_delivery_amount_invalid")
        owners.append({"creation_id": str(group.get("id") or ""), "lead_id": owner_id,
                       "method": delivery["method"], "amount": f"{fee:.2f}"})
    if not owners:
        return None
    if len({owner["method"] for owner in owners}) > 1 or len({owner["lead_id"] for owner in owners}) != len(owners):
        raise DeliveryPaymentError("custom_print_delivery_conflict")
    gross, discount = _money(gross_total, positive=True), _money(discount_amount)
    fee = sum((_money(owner["amount"]) for owner in owners), ZERO)
    if discount >= gross or fee >= gross - discount:
        raise DeliveryPaymentError("custom_print_delivery_amount_invalid")
    binding = hashlib.sha256(json.dumps(creation_data, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema": CUSTOM_PRINT_INCLUDED_SCHEMA, "authority": "web_custom_print", "mode": "customer_paid_included",
            "payer_type": "Sender", "order_id": order_id, "owners": owners, "creation_digest": binding,
            "gross_total": f"{gross:.2f}", "discount_amount": f"{discount:.2f}", "delivery_amount": f"{fee:.2f}",
            "payable_total": f"{gross - discount:.2f}", "merchandise_total": f"{gross - discount - fee:.2f}"}


def _custom_print_included_snapshot(order, payload, contract, gross, discount):
    try:
        normalized = build_custom_print_delivery_contract(gross_total=gross, discount_amount=discount,
                        creation_data=payload.get("custom_print_creation"), order_id=getattr(order, "pk", None))
        if normalized is None or normalized != contract:
            raise DeliveryPaymentError("custom_print_delivery_changed")
        lead_ids = payload.get("custom_print_lead_ids") or []
        if any(owner["lead_id"] not in lead_ids for owner in normalized["owners"]):
            raise DeliveryPaymentError("custom_print_delivery_binding_invalid")
        fee = _money(normalized["delivery_amount"])
        funded = getattr(order, "payment_status", "") == "paid"
        paid = payload.get("paid_amount", payload.get("paid_value"))
        if funded and paid is not None and _money(paid) < _money(normalized["payable_total"]):
            funded = False
        courier = any(owner["method"] == "courier" for owner in normalized["owners"])
        return {"schema": CUSTOM_PRINT_INCLUDED_SCHEMA, "mode": "customer_paid_included", "payer_type": "Sender",
                "label": "Доставку включено в суму замовлення" + (" та оплачено" if funded else "; ще не оплачено"),
                "valid": True, "source_locked": False, "custom_print_source_locked": True, "authority": "web_custom_print", "reason": "courier_manual_required" if courier else "" if funded else "delivery_not_funded",
                "requires_manual": courier or not funded, "delivery_prepaid": funded, "requested": True, "funded": funded,
                "merchandise_total": _money(normalized["merchandise_total"]), "delivery_amount": fee,
                "customer_charge_amount": fee, "payable_total": _money(normalized["payable_total"]), "contract": normalized}
    except (DeliveryPaymentError, TypeError, ValueError, KeyError, AttributeError) as exc:
        return _snapshot(gross - discount, valid=False, reason=getattr(exc, "reason", "custom_print_delivery_invalid"), source="web_custom_print")


class DeliveryPaymentError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("Умови та оплату доставки потрібно звірити перед збереженням.")


def _money(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise DeliveryPaymentError("delivery_amount_invalid")
    text = str(value).strip()
    if not re.fullmatch(r"\d{1,10}(?:\.\d{1,2})?", text):
        raise DeliveryPaymentError("delivery_amount_invalid")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise DeliveryPaymentError("delivery_amount_invalid") from None
    if not amount.is_finite() or amount > MAX_AMOUNT or (positive and amount <= 0):
        raise DeliveryPaymentError("delivery_amount_invalid")
    return amount.quantize(Decimal("0.01"))


def _id(value):
    return type(value) is int and 0 < value < 2**63


def _evidence(values):
    if not isinstance(values, (list, tuple)) or len(values) > 512 or any(not _id(value) for value in values):
        raise DeliveryPaymentError("delivery_evidence_invalid")
    return sorted(set(values))


def _get(row, key, default=None):
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def _items(item_rows):
    """Read primitive dict/OrderItem attributes only; never traverse relations."""
    if item_rows is None:
        return None
    rows, gross, quantity = [], ZERO, 0
    for index, item in enumerate(item_rows):
        if index >= 200:
            raise DeliveryPaymentError("delivery_items_invalid")
        qty = _get(item, "qty")
        if type(qty) is not int or not 1 <= qty <= 1000:
            raise DeliveryPaymentError("delivery_items_invalid")
        price = _money(_get(item, "unit_price"))
        kind = _get(item, 'item_kind', 'clothing') or 'clothing'
        if kind not in {'clothing', 'dtf_film'}:
            raise DeliveryPaymentError('delivery_items_invalid')
        if kind == 'dtf_film':
            length = _money(_get(item, 'film_length_m'), positive=True)
            if qty != 1 or length > Decimal('999999.99'):
                raise DeliveryPaymentError('delivery_items_invalid')
            total = (price * length).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        else:
            total = price * qty
        saved_total = _get(item, "line_total")
        if saved_total is not None and _money(saved_total) != total:
            raise DeliveryPaymentError("delivery_items_invalid")
        gross += total
        quantity += qty
        row = {key: str(_get(item, key, "") or "")[:512] for key in (
            "product_id", "color_variant_id", "title", "size", "fit_option_code", "color_name_custom")}
        row.update(qty=qty, unit_price=f"{price:.2f}", line_total=f"{total:.2f}")
        # Preserve the historic clothing digest; add material identity only
        # for metre-based film so existing source-locked proofs stay valid.
        if kind == 'dtf_film':
            row.update(item_kind='dtf_film', film_length_m=f'{length:.2f}')
        rows.append(row)
    if not rows or gross > MAX_AMOUNT:
        raise DeliveryPaymentError("delivery_items_invalid")
    rows.sort(key=lambda row: json.dumps(row, sort_keys=True))
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    return {"digest": digest, "gross_total": gross, "quantity_total": quantity}


def build_delivery_payment_contract(*, mode, merchandise_total, delivery_amount="0", actor_id,
        authority="manual_manager", confirmed_amount=None, allocated_delivery_amount=None,
        review_id=None, decision_id=None, evidence_message_ids=(), item_rows=None, discount_amount="0", recorded_at=None):
    """Serialize server-audited shipping facts; caller owns actor authorization."""
    if (not isinstance(mode, str) or mode not in MODES or not isinstance(authority, str)
            or authority not in {"manual_manager", "payment_review"} or not _id(actor_id)):
        raise DeliveryPaymentError("delivery_authority_invalid")
    merchandise = _money(merchandise_total, positive=True)
    fee = _money(delivery_amount)
    discount = _money(discount_amount)
    payable = merchandise + fee
    if payable > MAX_AMOUNT:
        raise DeliveryPaymentError("delivery_amount_invalid")
    if (mode == "customer_prepaid" and fee <= 0) or (mode != "customer_prepaid" and fee != 0):
        raise DeliveryPaymentError("delivery_mode_amount_mismatch")
    evidence = _evidence(evidence_message_ids)
    if (review_id is not None or decision_id is not None) and not (_id(review_id) and _id(decision_id)):
        raise DeliveryPaymentError("delivery_review_binding_invalid")
    if authority == "payment_review" and not (_id(review_id) and _id(decision_id) and evidence):
        raise DeliveryPaymentError("delivery_review_binding_invalid")
    proof = {}
    if mode == "customer_prepaid":
        confirmed = _money(confirmed_amount, positive=True)
        if confirmed > payable:
            raise DeliveryPaymentError("delivery_payment_insufficient")
        if confirmed == payable:
            basis, allocated = "full_payment", fee
        else:
            allocated = _money(allocated_delivery_amount, positive=True)
            if allocated != fee or confirmed < allocated:
                raise DeliveryPaymentError("delivery_payment_insufficient")
            basis = "explicit_allocation"
        proof = {"basis": basis, "confirmed_amount": f"{confirmed:.2f}",
                 "allocated_delivery_amount": f"{allocated:.2f}"}
    item_contract = _items(item_rows)
    if item_contract and item_contract["gross_total"] != merchandise + discount:
        raise DeliveryPaymentError("delivery_items_total_mismatch")
    return {
        "schema": SCHEMA, "mode": mode, "merchandise_total": f"{merchandise:.2f}",
        "delivery_amount": f"{fee:.2f}", "payable_total": f"{payable:.2f}",
        "payer_type": "Recipient" if mode == "carrier_recipient" else "Sender",
        "actor_id": actor_id, "authority": authority, "review_id": review_id, "decision_id": decision_id,
        "evidence_message_ids": evidence, "payment_confirmation": proof,
        "items_digest": item_contract["digest"] if item_contract else "",
        "quantity_total": item_contract["quantity_total"] if item_contract else None,
        "recorded_at": datetime.now(timezone.utc).isoformat() if recorded_at is None else str(recorded_at)[:80],
    }


def _snapshot(merchandise, *, contract=None, valid=None, reason="", source="legacy_default"):
    mode = contract["mode"] if valid and contract else "carrier_recipient"
    fee = _money(contract["delivery_amount"]) if valid and contract else ZERO
    return {
        "schema": SCHEMA, "mode": mode, "payer_type": "Recipient" if mode == "carrier_recipient" else "Sender",
        "label": "Умови доставки потребують звірки" if valid is False else LABELS[mode],
        "valid": valid, "source_locked": bool(source == "legacy_instagram_review"
            or isinstance(contract, dict) and contract.get("authority") == "payment_review"),
        "requires_manual": valid is False,
        "reason": reason, "authority": contract.get("authority", source) if isinstance(contract, dict) else source,
        "delivery_prepaid": valid is True and mode == "customer_prepaid",
        "merchandise_total": merchandise, "delivery_amount": fee, "customer_charge_amount": fee,
        "payable_total": merchandise + fee, "contract": dict(contract) if valid and contract else {},
    }


def _binding_current(contract, payload):
    for contract_key, payload_key in (("review_id", "instagram_payment_review_id"),
                                      ("decision_id", "manager_payment_decision_id")):
        if payload.get(payload_key) is not None and contract.get(contract_key) != payload[payload_key]:
            raise DeliveryPaymentError("delivery_review_binding_changed")


def _known_amount(payload):
    for key in ("manager_confirmed_amount", "effective_confirmed_amount"):
        if payload.get(key) is not None:
            return payload[key]
    return None


def _legacy_contract(raw, merchandise, payload):
    if not isinstance(raw, dict) or not all(_id(raw.get(key)) for key in ("actor_id", "review_id", "decision_id")):
        raise DeliveryPaymentError("legacy_delivery_invalid")
    if not _evidence(raw.get("evidence_message_ids")):
        raise DeliveryPaymentError("legacy_delivery_invalid")
    fee = _money(raw.get("delivery_amount"), positive=True)
    payable = _money(raw.get("payable_total"), positive=True)
    if _money(raw.get("merchandise_total"), positive=True) != merchandise or payable != merchandise + fee:
        raise DeliveryPaymentError("delivery_order_total_changed")
    if raw.get("prepaid") is not True or raw.get("payer_type") != "Sender":
        # An old partial review included delivery in COD and also billed the
        # recipient carrier fee. Never silently repeat that double collection.
        raise DeliveryPaymentError("legacy_delivery_allocation_unknown")
    known = _known_amount(payload)
    if known is not None and _money(known) != payable:
        raise DeliveryPaymentError("legacy_delivery_payment_changed")
    contract = build_delivery_payment_contract(mode="customer_prepaid", merchandise_total=merchandise,
        delivery_amount=fee, actor_id=raw["actor_id"], authority="payment_review", confirmed_amount=payable,
        review_id=raw["review_id"], decision_id=raw["decision_id"], evidence_message_ids=raw["evidence_message_ids"], recorded_at="")
    _binding_current(contract, payload)
    return contract


def delivery_payment_snapshot(order, *, item_rows=None):
    """Validate a current order's shipping contract using supplied local facts only."""
    if order is None:
        return _snapshot(ZERO)
    try:
        gross = _money(getattr(order, "total_sum", "0") or "0")
        discount = _money(getattr(order, "discount_amount", "0") or "0")
        if discount > gross:
            raise DeliveryPaymentError("delivery_order_total_invalid")
        merchandise = gross - discount
    except DeliveryPaymentError as exc:
        return _snapshot(ZERO, valid=False, reason=exc.reason)
    payload = getattr(order, "payment_payload", None)
    payload = payload if isinstance(payload, dict) else {}
    if "delivery_payment" not in payload and "instagram_delivery_contract" not in payload:
        return _snapshot(merchandise)
    source = "delivery_payment" if "delivery_payment" in payload else "legacy_instagram_review"
    raw_contract = payload.get("delivery_payment")
    if isinstance(raw_contract, dict) and raw_contract.get('schema') == THRESHOLD_SCHEMA:
        return _threshold_shipping_snapshot(order, raw_contract, merchandise, item_rows=item_rows)
    if isinstance(raw_contract, dict) and raw_contract.get("schema") == CUSTOM_PRINT_INCLUDED_SCHEMA:
        return _custom_print_included_snapshot(order, payload, raw_contract, gross, discount)
    lock_hint = {"authority": "payment_review"} if isinstance(raw_contract, dict) and raw_contract.get("authority") == "payment_review" else None
    try:
        if item_rows is not None:
            # Bound materialization also keeps a supplied generator from being
            # consumed twice by validation and serialization.
            item_rows = list(islice(item_rows, 201))
        items = _items(item_rows)
        if items and items["gross_total"] != gross:
            raise DeliveryPaymentError("delivery_items_total_mismatch")
        if source == "legacy_instagram_review":
            contract = _legacy_contract(payload["instagram_delivery_contract"], merchandise, payload)
        else:
            contract = payload["delivery_payment"]
            if not isinstance(contract, dict) or contract.get("schema") != SCHEMA:
                raise DeliveryPaymentError("delivery_contract_invalid")
            proof = contract.get("payment_confirmation")
            if not isinstance(proof, dict):
                raise DeliveryPaymentError("delivery_payment_proof_invalid")
            normalized = build_delivery_payment_contract(mode=contract.get("mode"),
                merchandise_total=contract.get("merchandise_total"), delivery_amount=contract.get("delivery_amount"),
                actor_id=contract.get("actor_id"), authority=contract.get("authority"),
                confirmed_amount=proof.get("confirmed_amount"), allocated_delivery_amount=proof.get("allocated_delivery_amount"),
                review_id=contract.get("review_id"), decision_id=contract.get("decision_id"),
                evidence_message_ids=contract.get("evidence_message_ids"), item_rows=item_rows, discount_amount=discount,
                recorded_at=contract.get("recorded_at", ""))
            if (_money(contract.get("merchandise_total")) != merchandise
                    or _money(contract.get("payable_total")) != _money(normalized["payable_total"])
                    or contract.get("payer_type") != normalized["payer_type"]):
                raise DeliveryPaymentError("delivery_order_total_changed")
            if contract["mode"] == "customer_prepaid" and (
                proof.get("basis") != normalized["payment_confirmation"]["basis"] or any(
                    _money(proof.get(key)) != _money(normalized["payment_confirmation"][key])
                    for key in ("confirmed_amount", "allocated_delivery_amount")
                )
            ):
                raise DeliveryPaymentError("delivery_payment_proof_invalid")
            if contract["mode"] != "customer_prepaid" and proof:
                raise DeliveryPaymentError("delivery_payment_proof_invalid")
            digest = contract.get("items_digest")
            if digest and items is None:
                raise DeliveryPaymentError("delivery_items_unavailable")
            if digest and (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                    or (items and (digest != items["digest"] or contract.get("quantity_total") != items["quantity_total"]))):
                raise DeliveryPaymentError("delivery_items_changed")
            if contract.get("authority") == "payment_review" and any(
                    payload.get(key) is None for key in ("instagram_payment_review_id", "manager_payment_decision_id")):
                raise DeliveryPaymentError("delivery_review_binding_missing")
            if contract.get("authority") == "payment_review" or contract.get("review_id") is not None:
                _binding_current(contract, payload)
            known = _known_amount(payload)
            if contract["mode"] == "customer_prepaid" and known is not None and _money(known) != _money(proof["confirmed_amount"]):
                raise DeliveryPaymentError("delivery_payment_changed")
            contract = normalized
        return _snapshot(merchandise, contract=contract, valid=True, source=source)
    except (DeliveryPaymentError, TypeError, KeyError) as exc:
        reason = exc.reason if isinstance(exc, DeliveryPaymentError) else "delivery_contract_invalid"
        return _snapshot(merchandise, contract=lock_hint, valid=False, reason=reason, source=source)
