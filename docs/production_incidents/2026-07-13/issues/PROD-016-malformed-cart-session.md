# PROD-016 — Malformed cart session identifiers cause 500

**Priority:** P1
**State:** fixed `ed4aeb8d` (2026-07-16)
**Owner:** storefront cart/session validation

## Symptom

Retained stderr contains three paired failures on `/cart/` and `/cart/mini/`:

```text
ValueError: invalid literal for int() with base 10: 'abc'
ValueError: Field 'id' expected a number but got 'abc'
```

## Root cause

Cart session contents are treated as trusted ORM identifiers before per-item error handling:

- `storefront/views/cart.py:545-551` passes collected product/color-variant IDs directly to `in_bulk`.
- mini-cart and shared total helpers likewise call `in_bulk` on raw session values.
- `add_to_cart` stores raw `color_variant_id` from POST in the session. It converts only later for analytics/offer ID and does not prove the variant belongs to the product before storage.
- A stale, manually forged or historically malformed signed-session value can therefore raise during query construction and abort the whole page.

The three historical `process_guest_order` AttributeErrors nearby were fixed by changing the current path to `create_order`; keep a regression test, but the raw-ID flaw remains in current source.

## Implementation plan

1. Write tests with non-dict cart, missing keys, strings such as `abc`, negative/huge IDs, invalid qty and variant belonging to another product.
2. Create one cart-session normalization boundary that:
   - accepts only a dict of bounded items;
   - parses positive integer product/variant IDs safely;
   - caps quantity and normalizes size/fit;
   - drops or quarantines malformed items without querying them.
3. Use normalized typed IDs for every `in_bulk`/lookup/total path.
4. Validate color variant existence and product ownership in `add_to_cart` before session write.
5. Save cleaned cart once after detection and emit a safe metric, not an ERROR/Telegram alert.
6. Ensure price remains server-derived.

## Acceptance criteria

- Every malformed-session case returns a valid cart/mini response with bad rows omitted or a controlled 400 for a mutation.
- No raw string reaches an integer ORM lookup.
- Valid cart behavior, analytics IDs, price integrity and quantity caps remain unchanged.
- Guest-order route has a regression test proving the current callable exists.

## Resolution

All storefront cart, checkout, capture and Monobank entry points now use one
typed session boundary. It limits rows and quantity, rejects invalid mutation
IDs, bulk-validates variant ownership, removes only malformed rows, and keeps
prices server-derived. New distinct lines are rejected at the 100-row limit,
while updates to an existing line remain valid. Main cart, mini-cart, summary,
JSON items, update, checkout and capture regressions cover malformed IDs and
cross-product variants.

The focused suite passed 130/130 locally and on production. A cwd-independent
static-path follow-up landed in `569626c3`; production checks and `/cart/` plus
`/cart/mini/` smoke requests returned 200.
