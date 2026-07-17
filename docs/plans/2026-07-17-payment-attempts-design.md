# Payment Attempts and Paid Orders Design

**Status:** approved for implementation from the checkout requirements

## Goal

Keep unpaid checkout activity separate from real orders, create exactly one
order only after verified full payment or verified `prepay_200`, and expose a
compact staff-only payment-attempts view without duplicating Telegram or
Facebook events.

## Decision

Introduce an `orders.PaymentAttempt` model. It owns the immutable checkout
snapshot, customer/delivery data, calculated gross/discount/payable/payment
amounts, payment method, Monobank invoice data, expiry, attribution, and
attempt status. It has a nullable one-to-one link to the final `Order`.

`Order` remains the source of truth for fulfillment and revenue. A checkout
request atomically creates or reuses one active attempt using a stable
fingerprint. No `Order` or `OrderItem` is created at invoice creation. A
verified Monobank success transition locks the attempt, validates the pulled
status and amount, creates one order from the frozen snapshot, records promo
usage, and schedules post-payment side effects after commit. Repeated webhook,
return, retry, or browser refresh is a no-op after the attempt is converted.

COD is rejected at every public checkout boundary. Legacy/manual order tools
remain available only for staff-created records and are not used by public
checkout.

## Analytics and notifications

- Browser `InitiateCheckout` is emitted when checkout starts.
- Browser/CAPI `AddPaymentInfo` is emitted once when an invoice is created;
  both use the attempt's stable event id.
- `Purchase` is emitted for every verified money movement: full payment and
  `prepay_200`. The final discounted order value is sent as `value`, while
  the amount actually paid is retained as `paid_value`.
- `Lead` is not emitted for a payment attempt, so a prepayment cannot be
  counted as a second conversion.
- Browser and CAPI use the same `eventID/event_id`; final payable value after
  discount is used everywhere, while `paid_value` records the actual payment.
- Telegram has one attempt-start notification and one converted-order payment
  notification, each guarded by persisted idempotency markers.

## Admin experience

The normal orders tab contains only real orders. A secondary, hidden-by-default
"Payment attempts" view shows initiated/processing/failed/expired/converted
attempts with status, age/expiry countdown data, customer, product snapshot,
gross/discount/payable/paid amounts, invoice reference, attribution and the
linked order when conversion happened. It is paginated and indexed so a large
volume of retries does not pollute the orders list.

## Failure handling

Failed, cancelled, and expired attempts remain auditable but cannot create an
order. A timed-out invoice can be retried by creating a new attempt fingerprint
while preserving the prior attempt history. Amount mismatch, invalid signature,
unknown invoice, duplicate invoice, or unavailable product fails closed.
