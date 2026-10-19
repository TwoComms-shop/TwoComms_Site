"""Shared arithmetic for the approved, discounted merchandise threshold.

The caller supplies goods only: deposits, carrier charges and provisional custom
prices cannot unlock this promotion. A display snapshot does not grant shipment
authority; native checkout writers freeze and bind it before provider admission.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from django.conf import settings


SCHEMA = 'shipping-policy.v1'
LEGACY_POLICY_VERSION = 'threshold-after-discounts.v1'
POLICY_VERSION = 'threshold-after-discounts-full-payment.v2'
BASIS = 'approved_merchandise_after_discounts'
ZERO = Decimal('0.00')
MAX_AMOUNT = Decimal('9999999999.99')


class ShippingPolicyError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ShippingPolicyError('shipping_amount_invalid')
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ShippingPolicyError('shipping_amount_invalid') from None
    if not amount.is_finite() or not ZERO <= amount <= MAX_AMOUNT:
        raise ShippingPolicyError('shipping_amount_invalid')
    try:
        normalized = amount.quantize(Decimal('.01'))
    except InvalidOperation:
        raise ShippingPolicyError('shipping_amount_invalid') from None
    if amount != normalized:
        raise ShippingPolicyError('shipping_amount_invalid')
    return normalized


def free_shipping_threshold():
    """Read the current display/admission setting, never reprice a paid Order."""
    return _money(getattr(settings, 'FREE_SHIPPING_THRESHOLD', '3000'))


def shipping_payment_mode(value):
    """Normalize actual payment choices without inferring them from money."""
    if value is not None and not isinstance(value, str):
        raise ShippingPolicyError('shipping_payment_mode_invalid')
    if value in {'online_full', 'full'}:
        return 'online_full'
    if value in {'prepay_200', 'prepay_200_cod', 'prepayment'}:
        return 'prepay_200_cod'
    if value in {None, '', 'unknown'}:
        return 'unknown'
    raise ShippingPolicyError('shipping_payment_mode_invalid')


def build_shipping_snapshot(*, merchandise_total, gross_total=None,
        discount_amount='0', currency='UAH', cart_digest='', owner=None,
        threshold=None, evaluated_at=None, payment_mode='unknown',
        policy_version=POLICY_VERSION):
    """Freeze the net-goods threshold and the actual full-payment choice.

    Legacy policy reconstruction exists solely to validate frozen contracts;
    new callers use v2, including when their payment choice is still unknown.
    """
    if policy_version not in {POLICY_VERSION, LEGACY_POLICY_VERSION}:
        raise ShippingPolicyError('shipping_policy_version_invalid')
    payment_mode = shipping_payment_mode(payment_mode)
    net, discount = _money(merchandise_total), _money(discount_amount)
    gross = _money(gross_total) if gross_total is not None else net + discount
    if gross > MAX_AMOUNT or gross - discount != net or currency != 'UAH':
        raise ShippingPolicyError('shipping_goods_basis_invalid')
    threshold = free_shipping_threshold() if threshold is None else _money(threshold)
    if cart_digest and (not isinstance(cart_digest, str) or not re.fullmatch('[a-f0-9]{64}', cart_digest)):
        raise ShippingPolicyError('shipping_cart_digest_invalid')
    if owner is not None and not isinstance(owner, dict):
        raise ShippingPolicyError('shipping_owner_invalid')
    stamp = datetime.now(timezone.utc).isoformat() if evaluated_at is None else evaluated_at
    try:
        parsed = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        raise ShippingPolicyError('shipping_timestamp_invalid') from None
    if parsed.tzinfo is None or len(stamp) > 80:
        raise ShippingPolicyError('shipping_timestamp_invalid')
    threshold_met = net > ZERO and net >= threshold
    eligible = threshold_met and (policy_version == LEGACY_POLICY_VERSION or payment_mode == 'online_full')
    if policy_version == LEGACY_POLICY_VERSION:
        # Historical frozen DTOs are verified by exact reproduction.
        progress = 100 if eligible else min(99, max(0, int(net / threshold * 100))) if threshold else 0
    else:
        # V2 amount progress is separate from payment-mode eligibility.
        progress = 100 if threshold_met else min(99, max(0, round(net / threshold * 100))) if threshold else 0
    result = {'schema': SCHEMA, 'policy_version': policy_version, 'basis': BASIS,
        'threshold': f'{threshold:.2f}', 'basis_total': f'{net:.2f}',
        'gross_total': f'{gross:.2f}', 'discount_amount': f'{discount:.2f}',
        'currency': currency, 'cart_digest': cart_digest, 'owner': deepcopy(owner or {}),
        'evaluated_at': stamp, 'remaining': f'{max(threshold-net, ZERO):.2f}',
        'progress': progress, 'eligible': eligible, 'automatic': True,
        'authority': 'automatic_threshold', 'status': 'achieved' if eligible else 'remaining',
        'mode': 'merchant_free' if eligible else 'carrier_recipient',
        'payer_type': 'Sender' if eligible else 'Recipient'}
    if policy_version == POLICY_VERSION:
        result.update(payment_mode=payment_mode, requires_full_payment=True,
            threshold_met=threshold_met,
            conditional=bool(threshold_met and payment_mode == 'unknown'))
        if result['conditional']:
            result['status'] = 'conditional_full_payment'
    return result


def verify_shipping_snapshot(snapshot):
    """Reproduce a frozen DTO without consulting current settings."""
    if not isinstance(snapshot, dict):
        raise ShippingPolicyError('shipping_snapshot_invalid')
    try:
        normalized = build_shipping_snapshot(merchandise_total=snapshot['basis_total'],
            gross_total=snapshot['gross_total'], discount_amount=snapshot['discount_amount'],
            currency=snapshot['currency'], cart_digest=snapshot['cart_digest'],
            owner=snapshot['owner'], threshold=snapshot['threshold'], evaluated_at=snapshot['evaluated_at'],
            payment_mode=snapshot.get('payment_mode', 'unknown'), policy_version=snapshot['policy_version'])
    except KeyError:
        raise ShippingPolicyError('shipping_snapshot_invalid') from None
    if snapshot != normalized:
        raise ShippingPolicyError('shipping_snapshot_changed')
    return normalized


def loses_automatic_free_shipping(previous, current):
    """Only an automatic promotion can be lost through basket repricing."""
    return bool(isinstance(previous, dict) and previous.get('authority') == 'automatic_threshold'
        and previous.get('eligible') is True and isinstance(current, dict)
        and current.get('authority') == 'automatic_threshold' and current.get('eligible') is False)
