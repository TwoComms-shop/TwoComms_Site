"""Atomic conversion of verified payment attempts into real orders."""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from orders.models import Order, OrderItem, PaymentAttempt
from orders.nova_poshta_data import apply_nova_poshta_refs
from productcolors.models import ProductColorVariant
from storefront.models import CustomPrintLead, Product


class PaymentAttemptConversionError(Exception):
    def __init__(self, message, *, retryable=False, marker=""):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.marker = str(marker or "")[:64]


def _paid_amount_from_payload(attempt, payload):
    raw = None
    if isinstance(payload, dict):
        raw = payload.get('paidAmount')
        if raw is None:
            raw = payload.get('finalAmount')
        if raw is None:
            raw = payload.get('amount')
    if raw is None:
        return attempt.payment_amount
    try:
        value = Decimal(str(raw)) / Decimal('100')
    except (TypeError, ValueError, ArithmeticError):
        return attempt.payment_amount
    return value if value > 0 else attempt.payment_amount


def _append_history(attempt, status, payload, source):
    history = list(attempt.payment_history or [])
    history.append({
        'ts': timezone.now().isoformat(),
        'status': status,
        'source': source,
        'payload': payload if isinstance(payload, dict) else str(payload)[:1000],
    })
    attempt.payment_history = history[-30:]


def materialize_payment_attempt(attempt_id, *, status, payload=None, source='webhook'):
    """Convert one verified attempt exactly once, returning (order, created)."""
    status = (status or '').lower()
    # The project uses one-stage Monobank acquiring. ``hold`` only means that
    # funds are reserved; it is not captured payment truth and must never
    # materialize an Order, even when this service is called directly.
    if status != 'success':
        raise PaymentAttemptConversionError(f'Unsupported conversion status: {status}')

    with transaction.atomic():
        attempt = (
            PaymentAttempt.objects.select_for_update()
            .select_related('user', 'promo_code', 'order')
            .get(pk=attempt_id)
        )
        if attempt.order_id:
            return attempt.order, False
        if not attempt.can_materialize:
            raise PaymentAttemptConversionError('Payment attempt is no longer convertible')

        # Freeze the verified callback in the attempt before copying its
        # history into the newly materialized order.
        _append_history(attempt, status, payload, source)
        snapshot = attempt.cart_snapshot if isinstance(attempt.cart_snapshot, dict) else {}
        cart_items = snapshot.get('cart') or []
        is_instagram_proposal = snapshot.get('checkout_surface') == 'instagram_proposal'
        source_provenance = None
        if is_instagram_proposal or 'source_cart_provenance' in snapshot or 'source_cart_binding' in snapshot:
            from management.services.ig_checkout_cart_provenance import (
                CartProvenanceError, bind_order_items, validate_attempt_provenance,
            )
            try:
                source_provenance = validate_attempt_provenance(snapshot)
            except CartProvenanceError as exc:
                raise PaymentAttemptConversionError(
                    exc.reason, marker='source_cart_provenance_invalid',
                ) from exc
        product_ids = [int(item['product_id']) for item in cart_items if item.get('product_id')]
        products = Product.objects.in_bulk(product_ids)
        if len(products) != len(set(product_ids)):
            raise PaymentAttemptConversionError('A product from the frozen cart is unavailable')

        variant_ids = [
            int(item['color_variant_id'])
            for item in cart_items
            if item.get('color_variant_id')
        ]
        variants = ProductColorVariant.objects.in_bulk(variant_ids)
        for item in cart_items:
            variant_id = item.get('color_variant_id')
            if variant_id and (not variants.get(int(variant_id)) or variants[int(variant_id)].product_id != int(item['product_id'])):
                raise PaymentAttemptConversionError('A selected product variant is unavailable')

        payment_status = (
            'prepaid'
            if attempt.pay_type in {
                PaymentAttempt.PayType.PREPAYMENT,
                PaymentAttempt.PayType.PREPAY_200,
            }
            else 'paid'
        )
        checkout_idempotency_key = None
        if attempt.checkout_series_key:
            from orders.checkout_series import (
                existing_series_order_idempotency_key,
            )

            if not attempt.checkout_winner_claimed:
                raise PaymentAttemptConversionError(
                    'Assisted checkout generation has not claimed the winner'
                )
            checkout_idempotency_key = existing_series_order_idempotency_key(
                attempt.checkout_series_key
            )
            existing_order = Order.objects.select_for_update().filter(
                checkout_idempotency_key=checkout_idempotency_key
            ).first()
            if existing_order is not None:
                if source_provenance and source_provenance['status'] == 'bound':
                    stored = (existing_order.payment_payload or {}).get('source_cart_provenance')
                    if not isinstance(stored, dict):
                        raise PaymentAttemptConversionError(
                            'cart_existing_order_provenance_missing', marker='source_cart_provenance_invalid',
                        )
                    # Preserve the winner's original frozen scope. Another
                    # generation cannot attach a different source cart to an
                    # already materialized series order.
                    original = dict(stored)
                    order_ids = []
                    original.pop('order_id', None)
                    try:
                        original['item_map'] = [dict(row) for row in stored['item_map']]
                        for row in original['item_map']:
                            order_ids.append(row.pop('order_item_id'))
                    except (KeyError, TypeError, ValueError) as exc:
                        raise PaymentAttemptConversionError(
                            'cart_existing_order_provenance_invalid', marker='source_cart_provenance_invalid',
                        ) from exc
                    if (stored.get('order_id') != existing_order.pk or original != source_provenance
                            or any(type(value) is not int or value <= 0 for value in order_ids)
                            or len(set(order_ids)) != len(order_ids)):
                        raise PaymentAttemptConversionError(
                            'cart_existing_order_provenance_changed', marker='source_cart_provenance_invalid',
                        )
                attempt.order = existing_order
                attempt.last_status_at = timezone.now()
                attempt.save(update_fields=[
                    'order', 'payment_history', 'last_status_at', 'updated',
                ])
                return existing_order, False
        order = Order.objects.create(
            user=attempt.user,
            full_name=attempt.full_name,
            phone=attempt.phone,
            email=attempt.email,
            city=attempt.city,
            np_office=attempt.np_office,
            session_key=attempt.session_key,
            pay_type=attempt.pay_type,
            total_sum=attempt.gross_amount,
            discount_amount=attempt.discount_amount,
            promo_code=attempt.promo_code,
            status='new',
            payment_status=payment_status,
            payment_provider='monobank_pay',
            payment_invoice_id=attempt.monobank_invoice_id,
            source='manual' if is_instagram_proposal else 'web',
            sale_source=(snapshot.get('sale_source') or 'Instagram') if is_instagram_proposal else '',
            checkout_idempotency_key=checkout_idempotency_key,
            utm_source=(attempt.tracking_payload or {}).get('utm_source', ''),
            utm_medium=(attempt.tracking_payload or {}).get('utm_medium', ''),
            utm_campaign=(attempt.tracking_payload or {}).get('utm_campaign', ''),
            utm_content=(attempt.tracking_payload or {}).get('utm_content', ''),
            utm_term=(attempt.tracking_payload or {}).get('utm_term', ''),
            payment_payload={
                **({'source_cart_provenance': source_provenance} if source_provenance else {}),
                'attempt_id': attempt.pk,
                'attempt_reference': attempt.reference,
                'tracking': attempt.tracking_payload or {},
                'history': attempt.payment_history or [],
                'paid_amount': str(_paid_amount_from_payload(attempt, payload)),
                'paid_value': str(_paid_amount_from_payload(attempt, payload)),
                'requested_payment_amount': str(attempt.payment_amount),
                'monobank_status': status,
                # Passenger may stop a request-owned daemon thread before it
                # reaches Telegram. Keep a durable DB marker so cron can
                # recover the paid order card without duplicating deliveries.
                'telegram_notifications': {
                    'order_notification_pending': True,
                    'order_notification_pending_at': timezone.now().isoformat(),
                },
            },
        )
        apply_nova_poshta_refs(order, {
            'np_settlement_ref': attempt.np_settlement_ref,
            'np_city_ref': attempt.np_city_ref,
            'np_warehouse_ref': attempt.np_warehouse_ref,
        })
        order.save(update_fields=['np_settlement_ref', 'np_city_ref', 'np_warehouse_ref'])

        items = []
        for item in cart_items:
            product = products[int(item['product_id'])]
            variant = variants.get(int(item['color_variant_id'])) if item.get('color_variant_id') else None
            items.append(OrderItem(
                order=order,
                product=product,
                color_variant=variant,
                title=item.get('title') or product.title,
                size=item.get('size', ''),
                fit_option_code=item.get('fit_option_code', ''),
                fit_option_label=item.get('fit_option_label', ''),
                option_values=item.get('option_values') or {},
                option_labels=item.get('option_labels') or {},
                qty=int(item.get('qty') or 1),
                unit_price=Decimal(str(item.get('unit_price') or 0)),
                line_total=Decimal(str(item.get('line_total') or 0)),
            ))
        items = OrderItem.objects.bulk_create(items)
        if source_provenance and source_provenance['status'] == 'bound':
            try:
                order_provenance = bind_order_items(
                    source_provenance, cart_items=cart_items, order_id=order.pk,
                    order_items=[{
                        'id': item.pk, 'order_id': item.order_id, 'product_id': item.product_id,
                        'qty': item.qty, 'size': item.size, 'fit_option_code': item.fit_option_code,
                        'color_variant_id': item.color_variant_id, 'option_values': item.option_values,
                        'unit_price': str(item.unit_price), 'line_total': str(item.line_total),
                    } for item in items],
                )
            except CartProvenanceError as exc:
                raise PaymentAttemptConversionError(
                    exc.reason, marker='source_cart_provenance_invalid',
                ) from exc
            order.payment_payload = {**order.payment_payload, 'source_cart_provenance': order_provenance}
            order.save(update_fields=['payment_payload'])

        custom_ids = snapshot.get('custom_print_lead_ids') or []
        if custom_ids:
            CustomPrintLead.objects.filter(pk__in=custom_ids).update(order=order)

        if attempt.promo_code_id:
            from orders.promo_reservations import consume_payment_attempt_promo

            promo_consumed = consume_payment_attempt_promo(attempt, order=order)
            reservation = dict((attempt.event_state or {}).get("promo_reservation") or {})
            authenticated_reservation = bool(
                reservation.get("state") == "reserved"
                and reservation.get("reservation_generation")
                and not reservation.get("guest_reservation_mismatch")
                and not reservation.get("reservation_generation_mismatch")
            )
            anonymous_bearer_reservation = bool(
                attempt.user_id is None
                and reservation.get("state") == "reserved"
                and reservation.get("guest_usage_id")
                and not reservation.get("guest_reservation_mismatch")
                and (
                    not attempt.checkout_series_key
                    or authenticated_reservation
                )
            )
            v2_owned_reservation = bool(
                attempt.checkout_series_key
                and authenticated_reservation
            )
            if not promo_consumed and (
                attempt.checkout_series_key or attempt.user_id is None
            ):
                # The provider payment is trusted, but an anonymous bearer
                # reservation must still match this exact invoice.  Roll the
                # Order and conversion marker back as one unit; only an active
                # reservation with a valid guest ledger row is retryable.
                raise PaymentAttemptConversionError(
                    (
                        "promo_usage_persistence_pending"
                        if anonymous_bearer_reservation or v2_owned_reservation
                        else "promo_reservation_invalid"
                    ),
                    retryable=bool(
                        anonymous_bearer_reservation or v2_owned_reservation
                    ),
                    marker=(
                        "promo_consumption_pending"
                        if anonymous_bearer_reservation or v2_owned_reservation
                        else ""
                    ),
                )

        attempt.status = (
            PaymentAttempt.Status.PREPAID
            if attempt.pay_type in {
                PaymentAttempt.PayType.PREPAYMENT,
                PaymentAttempt.PayType.PREPAY_200,
            }
            else PaymentAttempt.Status.PAID
        )
        attempt.paid_amount = _paid_amount_from_payload(attempt, payload)
        attempt.order = order
        attempt.last_status_at = timezone.now()
        attempt.save(update_fields=[
            'status', 'paid_amount', 'order', 'payment_history', 'event_state',
            'last_status_at', 'updated'
        ])

        from orders.payment_side_effects import enqueue_order_post_payment_side_effect

        enqueue_order_post_payment_side_effect(
            order.pk,
            previous_status='unpaid',
            pay_type=attempt.pay_type,
        )

        return order, True
