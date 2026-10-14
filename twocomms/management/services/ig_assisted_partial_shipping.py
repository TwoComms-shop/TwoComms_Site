"""Accepted selected-scope shipping caps bound to actual paid native Orders.

A maximum is a commitment, never a carrier quote, delivery prepayment or an
invoice for deferred merchandise. Ordinary/FULL39 shipping remains unchanged.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import re

from django.conf import settings
from django.db import transaction
from django.utils.crypto import salted_hmac, constant_time_compare


SCHEMA = 'ig-assisted-order-shipping.v1'
CARRIER_SCHEMA = 'ig-assisted-carrier-charge.v1'
MAXIMUM = Decimal('1000000.00')


class AssistedShippingError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        message = {
            'assisted_customer_shipping_maximum_exceeded': 'Фактична доставка перевищує погоджений максимум. Погодьте нові умови з клієнтом або оплатіть доставку повністю коштом магазину.',
            'assisted_carrier_fee_unknown': 'Перевізник ще не підтвердив фактичну вартість доставки. Відправку заблоковано до звірки.',
            'assisted_shipping_provider_review_required': 'Попередня накладна потребує звірки або підтвердженого видалення. Повторне створення та відправку заблоковано.',
        }.get(reason, 'Звірте фактичну вартість і платника доставки з погодженими умовами перед відправкою.')
        super().__init__(message)


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise AssistedShippingError('assisted_carrier_fee_unknown')
    if not re.fullmatch(r'\d{1,7}(?:\.\d{1,2})?', str(value)):
        raise AssistedShippingError('assisted_carrier_fee_invalid')
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise AssistedShippingError('assisted_carrier_fee_invalid') from None
    if not amount.is_finite() or not Decimal('0') <= amount <= MAXIMUM:
        raise AssistedShippingError('assisted_carrier_fee_invalid')
    return amount.quantize(Decimal('.01'))


def _expected_contract(order, checked):
    selection = checked.get('selection')
    if not isinstance(selection, dict):
        raise AssistedShippingError('assisted_shipping_selection_missing')
    phase = selection.get('shipping_scope')
    terms = selection.get('shipping_terms')
    if phase not in {'now', 'later', 'together'} or not isinstance(terms, dict) or set(terms) != {'now', 'later', 'together'}:
        raise AssistedShippingError('assisted_shipping_terms_invalid')
    for term in terms.values():
        if (not isinstance(term, dict) or set(term) != {'payer', 'customer_maximum', 'currency'}
            or term['payer'] not in {'customer', 'merchant'} or term['currency'] != 'UAH'
            or (term['payer'] == 'merchant' and _money(term['customer_maximum']) != 0)):
            raise AssistedShippingError('assisted_shipping_terms_invalid')
        _money(term['customer_maximum'])
    selected = selection.get('selected_line_ids')
    item_map = checked.get('item_map')
    if (not isinstance(selected, list) or not selected or len(selected) != len(set(selected))
        or not isinstance(item_map, list) or {row['line_id'] for row in item_map} != set(selected)):
        raise AssistedShippingError('assisted_shipping_item_coverage_changed')
    for name in ('scope_digest', 'terms_digest'):
        if not re.fullmatch(r'[0-9a-f]{64}', str(selection.get(name) or '')):
            raise AssistedShippingError('assisted_shipping_terms_invalid')
    term = terms[phase]
    return {'schema': SCHEMA, 'order_id': order.pk, 'execution_id': checked['execution_id'],
        'intention_id': str(selection['intention_id']), 'original_request_id': str(selection['original_request_id']),
        'scope_digest': selection['scope_digest'], 'terms_digest': selection['terms_digest'],
        'shipping_scope': phase, 'selected_line_ids': deepcopy(selected),
        'item_map': [{'line_id': row['line_id'], 'order_item_id': row['order_item_id']} for row in item_map],
        'payer': term['payer'], 'customer_maximum': f"{_money(term['customer_maximum']):.2f}", 'currency': 'UAH'}


def attach_assisted_shipping_contract(order, *, execution, now=None):
    """Called only after the canonical winner attached its actual Order."""
    from management.services.ig_assisted_checkout import validate_assisted_order
    if not transaction.get_connection().in_atomic_block:
        raise AssistedShippingError('assisted_shipping_transaction_required')
    if execution.scope != 'partial':
        return None
    checked = validate_assisted_order(order, execution=execution, now=now)
    if not checked.get('ready'):
        raise AssistedShippingError(checked.get('reason') or 'assisted_order_invalid')
    expected = _expected_contract(order, checked)
    payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
    if 'ig_assisted_shipping' in payload and payload['ig_assisted_shipping'] != expected:
        raise AssistedShippingError('assisted_shipping_attachment_conflict')
    if payload.get('ig_assisted_shipping') != expected:
        order.payment_payload = {**payload, 'ig_assisted_shipping': expected}
        order.save(update_fields=['payment_payload'])
    return expected


def assisted_shipping_contract(order, *, now=None):
    """Missing/tampered selected metadata cannot fall back to ordinary delivery."""
    from management.ig_assisted_commerce_models import IgAssistedCommerceExecution
    from management.services.ig_assisted_checkout import validate_assisted_order
    raw_payload = getattr(order, 'payment_payload', None)
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    order_id = getattr(order, 'pk', None)
    if order_id is None:
        if 'ig_assisted_shipping' in payload:
            raise AssistedShippingError('assisted_shipping_native_order_missing')
        return None
    execution = IgAssistedCommerceExecution.objects.filter(order_id=order_id).only('id', 'scope').first()
    if execution is None or execution.scope != 'partial':
        if 'ig_assisted_shipping' in payload:
            raise AssistedShippingError('assisted_shipping_native_order_missing')
        return None
    checked = validate_assisted_order(order, execution=execution, now=now)
    if not checked.get('ready'):
        raise AssistedShippingError(checked.get('reason') or 'assisted_order_invalid')
    expected = _expected_contract(order, checked)
    if payload.get('ig_assisted_shipping') != expected:
        raise AssistedShippingError('assisted_shipping_attachment_changed')
    return expected


def assisted_manual_order_edit_lock(order):
    """A paid native attachment cannot be destructively rebuilt as manual rows."""
    from management.ig_assisted_commerce_models import IgAssistedCommerceExecution
    execution = IgAssistedCommerceExecution.objects.filter(order_id=order.pk).values('client_id').first()
    if execution is None:
        return None
    base = (getattr(settings, 'MANAGEMENT_BASE_URL', '') or 'https://management.twocomms.shop').rstrip('/')
    return {'message': 'Це оплачене замовлення з погодженими позиціями. Повне ручне редагування заблоковано, щоб зберегти оплату, одержувача та склад замовлення. Умови доставки звіряйте у керуванні Instagram-замовленням.',
        'management_url': f"{base}/bot/?section=clients&client_id={execution['client_id']}"}


def assisted_shipping_snapshot(order, ordinary):
    """Adapt the native commitment to the existing delivery presentation DTO."""
    try:
        contract = assisted_shipping_contract(order)
    except AssistedShippingError as exc:
        return {**ordinary, 'valid': False, 'source_locked': True, 'requires_manual': True,
            'authority': 'ig_assisted_selected', 'reason': exc.reason,
            'label': 'Погоджені умови доставки потребують звірки'}
    if contract is None:
        return ordinary
    merchant = contract['payer'] == 'merchant'
    payer = 'Sender' if merchant else 'Recipient'
    label = ('Доставку оплачує магазин' if merchant else
        f"Доставку оплачує одержувач, не більше {contract['customer_maximum']} грн")
    if order.tracking_number:
        try:
            actual = assert_assisted_shipping_allowed(order)
            if actual['merchant_funded']:
                payer, label = 'Sender', 'Фактичну доставку повністю оплачує магазин'
        except AssistedShippingError:
            pass
    return {**ordinary, 'mode': 'merchant_free' if payer == 'Sender' else 'carrier_recipient',
        'payer_type': payer, 'label': label, 'valid': True, 'source_locked': True,
        'requires_manual': False, 'authority': 'ig_assisted_selected', 'reason': '',
        'delivery_prepaid': False, 'delivery_amount': Decimal('0.00'),
        'customer_charge_amount': Decimal('0.00'), 'customer_maximum': contract['customer_maximum'],
        'shipping_scope': contract['shipping_scope'], 'contract': contract}


def assisted_requested_payer_allowed(order, payer_type):
    contract = assisted_shipping_contract(order)
    if contract is None:
        return None
    return payer_type == 'Sender' or (contract['payer'] == 'customer' and payer_type == 'Recipient')


def assess_carrier_charge(contract, *, payer_type, carrier_cost):
    """Pure arithmetic after the caller obtained native accepted terms."""
    if payer_type not in {'Sender', 'Recipient'}:
        raise AssistedShippingError('assisted_carrier_payer_unknown')
    cost = _money(carrier_cost) if carrier_cost not in (None, '') else None
    if payer_type == 'Recipient':
        if cost is None:
            raise AssistedShippingError('assisted_carrier_fee_unknown')
        if contract['payer'] != 'customer':
            raise AssistedShippingError('assisted_merchant_shipping_recipient_charge')
        charge = cost
    else:
        # Carrier bills the merchant for the entire fee. Never describe an
        # unimplemented split-payer difference or claim customer prepayment.
        charge = Decimal('0.00')
    if charge > _money(contract['customer_maximum']):
        raise AssistedShippingError('assisted_customer_shipping_maximum_exceeded')
    return {'payer_type': payer_type, 'carrier_cost': f'{cost:.2f}' if cost is not None else None,
        'customer_charge': f'{charge:.2f}', 'merchant_funded': payer_type == 'Sender'}


def _charge_signature(body, *, secret=None):
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
    return salted_hmac(CARRIER_SCHEMA, canonical, secret=secret, algorithm='sha256').hexdigest()


def record_assisted_carrier_charge(order, *, tracking_number, document_ref='', payer_type, carrier_cost, source):
    """Internal provider consumers supply actual save/tracking response fields.

    This only stages the audited payload; the existing locked shipment writer
    saves it together with its TTN. No raw request cost is accepted as evidence.
    """
    contract = assisted_shipping_contract(order)
    if contract is None:
        return None
    if source not in {'nova_poshta_save', 'nova_poshta_tracking'} or not str(tracking_number or '').strip():
        raise AssistedShippingError('assisted_carrier_evidence_invalid')
    assessed = assess_carrier_charge(contract, payer_type=payer_type, carrier_cost=carrier_cost)
    body = {'schema': CARRIER_SCHEMA, 'order_id': order.pk, 'execution_id': contract['execution_id'],
        'terms_digest': contract['terms_digest'], 'scope_digest': contract['scope_digest'],
        'tracking_number': str(tracking_number).strip(), 'document_ref': str(document_ref or '').strip(),
        'source': source, **assessed}
    proof = {**body, 'signature': _charge_signature(body)}
    order.payment_payload = {**order.payment_payload, 'ig_assisted_carrier_charge': proof}
    return proof


def assert_assisted_shipping_allowed(order, *, tracking_number=None):
    contract = assisted_shipping_contract(order)
    if contract is None:
        return None
    payload = order.payment_payload
    review = payload.get('ig_assisted_shipping_review')
    if isinstance(review, dict) and review.get('state') == 'requires_review':
        raise AssistedShippingError('assisted_shipping_provider_review_required')
    proof = payload.get('ig_assisted_carrier_charge')
    if not isinstance(proof, dict):
        raise AssistedShippingError('assisted_carrier_evidence_missing')
    body = {key: value for key, value in proof.items() if key != 'signature'}
    signature = str(proof.get('signature') or '')
    if not any(constant_time_compare(signature, _charge_signature(body, secret=secret))
        for secret in [settings.SECRET_KEY, *getattr(settings, 'SECRET_KEY_FALLBACKS', ())]):
        raise AssistedShippingError('assisted_carrier_evidence_invalid')
    if (body.get('schema') != CARRIER_SCHEMA or body.get('order_id') != order.pk
        or body.get('execution_id') != contract['execution_id'] or body.get('terms_digest') != contract['terms_digest']
        or body.get('scope_digest') != contract['scope_digest']
        or body.get('tracking_number') != str(tracking_number or order.tracking_number or '').strip()
        or (body.get('document_ref') and body['document_ref'] != str(order.nova_poshta_document_ref or ''))):
        raise AssistedShippingError('assisted_carrier_evidence_owner_changed')
    assessed = assess_carrier_charge(contract, payer_type=body.get('payer_type'), carrier_cost=body.get('carrier_cost'))
    if any(body.get(key) != value for key, value in assessed.items()):
        raise AssistedShippingError('assisted_carrier_evidence_changed')
    return assessed


def verify_manual_assisted_tracking(order, *, tracking_number):
    """The server checks the authoritative manually entered carrier document."""
    if assisted_shipping_contract(order) is None:
        return None
    from orders.nova_poshta_service import NovaPoshtaService
    data = NovaPoshtaService().get_tracking_info(tracking_number, phone=order.phone)
    if not isinstance(data, dict) or str(data.get('Number') or '').strip() != str(tracking_number).strip():
        raise AssistedShippingError('assisted_carrier_document_unverified')
    document_ref = str(order.nova_poshta_document_ref or '')
    if data.get('Ref') and document_ref and str(data['Ref']) != document_ref:
        raise AssistedShippingError('assisted_carrier_document_unverified')
    return record_assisted_carrier_charge(order, tracking_number=tracking_number,
        document_ref=document_ref, payer_type=data.get('PayerType'), carrier_cost=data.get('Cost'),
        source='nova_poshta_tracking')


def resolve_assisted_shipping_review(order_id, *, actor, expected_document_ref, expected_terms_digest):
    """Recheck the existing document; never amend the customer's accepted cap."""
    from orders.models import Order
    from management.services.ig_assisted_commerce import _actor
    manager = _actor(getattr(actor, 'pk', None))
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        contract = assisted_shipping_contract(order)
        if (contract is None or expected_document_ref != str(order.nova_poshta_document_ref or '')
            or expected_terms_digest != contract['terms_digest']):
            raise AssistedShippingError('assisted_shipping_review_owner_changed')
        review = order.payment_payload.get('ig_assisted_shipping_review')
        if not isinstance(review, dict) or review.get('state') not in {'requires_review', 'resolved'}:
            raise AssistedShippingError('assisted_shipping_review_missing')
        previous = deepcopy(review)
        proof = verify_manual_assisted_tracking(order, tracking_number=order.tracking_number or '')
        order.payment_payload = {**order.payment_payload, 'ig_assisted_shipping_review': {
            'state': 'resolved', 'document_ref': expected_document_ref,
            'tracking_number': order.tracking_number or '', 'terms_digest': expected_terms_digest,
            'actor_id': manager.pk, 'previous_review': previous if previous.get('state') != 'resolved' else previous.get('previous_review'),
            'carrier_charge_signature': proof['signature']}}
        assert_assisted_shipping_allowed(order)
        order.save(update_fields=['payment_payload'])
        return {'ready': True, 'reason': '', 'order_id': order.pk, 'carrier_charge': proof}


def assisted_shipping_dispatch_ready(order):
    """Notification/tracking consumers abstain from preserved review TTNs."""
    try:
        assert_assisted_shipping_allowed(order)
        return True
    except AssistedShippingError:
        return False


def observe_assisted_tracking_charge(order, tracking_info):
    """Stage actual tracker evidence under its Order lock, before status effects.

    Latest unknown/over-cap fees hold projection; only a manager's verified
    resolver clears an existing review. The caller saves payment_payload.
    """
    if assisted_shipping_contract(order) is None:
        return {'ready': True, 'reason': '', 'is_assisted': False}
    data = tracking_info if isinstance(tracking_info, dict) else {}
    try:
        if (str(data.get('Number') or '').strip() != str(order.tracking_number or '').strip()
            or (data.get('Ref') and order.nova_poshta_document_ref
                and str(data['Ref']) != str(order.nova_poshta_document_ref))):
            raise AssistedShippingError('assisted_carrier_document_unverified')
        record_assisted_carrier_charge(order, tracking_number=order.tracking_number,
            document_ref=order.nova_poshta_document_ref or '', payer_type=data.get('PayerType'),
            carrier_cost=data.get('Cost'), source='nova_poshta_tracking')
        assert_assisted_shipping_allowed(order)
        return {'ready': True, 'reason': '', 'is_assisted': True}
    except AssistedShippingError as exc:
        order.payment_payload = {**order.payment_payload, 'ig_assisted_shipping_review': {
            'state': 'requires_review', 'reason': exc.reason,
            'tracking_number': order.tracking_number or '', 'document_ref': order.nova_poshta_document_ref or '',
            'payer_type': str(data.get('PayerType') or '')[:32], 'carrier_cost': str(data.get('Cost') or '')[:32]}}
        return {'ready': False, 'reason': exc.reason, 'is_assisted': True}


def preserve_assisted_waybill_failure(order_id, *, created, provider_cancelled):
    """Recover native refs after the local shipment transaction rolled back."""
    from orders.models import Order
    if not isinstance(created, dict):
        return None
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if assisted_shipping_contract(order) is None:
            return None
        reference = str(created.get('document_ref') or '')
        number = str(created.get('tracking_number') or '')
        review = {'state': 'cancelled' if provider_cancelled else 'requires_review',
            'reason': 'local_shipment_commit_failed', 'document_ref': reference, 'tracking_number': number,
            'provider_confirmed_cancelled': provider_cancelled is True}
        payload = dict(order.payment_payload)
        payload['ig_assisted_shipping_review'] = review
        payload.pop('ig_assisted_carrier_charge', None)
        order.payment_payload = payload
        if not provider_cancelled:
            if order.nova_poshta_document_ref and order.nova_poshta_document_ref != reference:
                raise AssistedShippingError('assisted_shipping_recovery_conflict')
            order.nova_poshta_document_ref = reference or None
            order.tracking_number = number or None
            order.nova_poshta_recipient_ref = created.get('recipient_ref') or None
            order.nova_poshta_recipient_contact_ref = created.get('recipient_contact_ref') or None
        order.save(update_fields=['payment_payload', 'tracking_number', 'nova_poshta_document_ref',
            'nova_poshta_recipient_ref', 'nova_poshta_recipient_contact_ref', 'updated'])
        return order
