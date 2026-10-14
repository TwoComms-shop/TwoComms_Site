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
POLICY_VERSION = 'threshold-after-discounts.v1'
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


def build_shipping_snapshot(*, merchandise_total, gross_total=None,
        discount_amount='0', currency='UAH', cart_digest='', owner=None,
        threshold=None, evaluated_at=None):
    """Return decimal-string DTO; eligibility is inclusive on full net goods."""
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
    eligible = net > ZERO and net >= threshold
    # Keep 100% reserved for achieved eligibility, even near the threshold.
    progress = 100 if eligible else min(99, max(0, int(net / threshold * 100))) if threshold else 0
    return {'schema': SCHEMA, 'policy_version': POLICY_VERSION, 'basis': BASIS,
        'threshold': f'{threshold:.2f}', 'basis_total': f'{net:.2f}',
        'gross_total': f'{gross:.2f}', 'discount_amount': f'{discount:.2f}',
        'currency': currency, 'cart_digest': cart_digest, 'owner': deepcopy(owner or {}),
        'evaluated_at': stamp, 'remaining': f'{max(threshold-net, ZERO):.2f}',
        'progress': progress, 'eligible': eligible, 'automatic': True,
        'authority': 'automatic_threshold', 'status': 'achieved' if eligible else 'remaining',
        'mode': 'merchant_free' if eligible else 'carrier_recipient',
        'payer_type': 'Sender' if eligible else 'Recipient'}


def verify_shipping_snapshot(snapshot):
    """Reproduce a frozen DTO without consulting current settings."""
    if not isinstance(snapshot, dict):
        raise ShippingPolicyError('shipping_snapshot_invalid')
    try:
        normalized = build_shipping_snapshot(merchandise_total=snapshot['basis_total'],
            gross_total=snapshot['gross_total'], discount_amount=snapshot['discount_amount'],
            currency=snapshot['currency'], cart_digest=snapshot['cart_digest'],
            owner=snapshot['owner'], threshold=snapshot['threshold'], evaluated_at=snapshot['evaluated_at'])
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
