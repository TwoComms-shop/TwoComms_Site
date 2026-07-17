# Payment Attempts and Paid Orders Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Separate unpaid Monobank checkouts from orders and create one order only after verified payment/prepayment, with deduplicated analytics, Telegram, attribution, discounts, and an informative admin attempts view.

**Architecture:** Add `PaymentAttempt` as the payment lifecycle aggregate and freeze the cart/customer/price snapshot there. The Monobank invoice endpoint only creates/reuses an attempt and invoice. Webhook/return locks the attempt and atomically materializes the final `Order` once. Admin and cleanup operate on attempts; orders stay paid/fulfilled records only.

**Tech Stack:** Django ORM/migrations, Monobank acquiring API, Facebook Pixel/CAPI, Telegram notifier, existing custom admin templates, Django tests.

---

### Task 1: Add regression tests for the required lifecycle

**Files:**
- Modify: `twocomms/storefront/tests/test_monobank_webhook.py`
- Modify: `twocomms/storefront/tests/test_checkout.py`
- Create: `twocomms/orders/tests/test_payment_attempts.py`

**Steps:**
1. Add failing tests proving invoice creation creates a `PaymentAttempt` but zero `Order`/`OrderItem` rows.
2. Add failing tests for COD rejection in both public checkout endpoints.
3. Add failing tests for duplicate invoice requests reusing one attempt.
4. Add failing tests for verified full/prepay webhook conversion, duplicate webhook idempotency, failure/expiry staying attempt-only, and amount mismatch staying processing.
5. Add failing tests proving promo usage, Telegram conversion, `Lead`/`Purchase`, and final discounted values occur once and use the same stable event id.
6. Run each focused test and confirm the expected failures before implementation.

### Task 2: Implement the `PaymentAttempt` persistence model

**Files:**
- Modify: `twocomms/orders/models.py`
- Create: `twocomms/orders/migrations/0051_payment_attempt.py`

**Steps:**
1. Add status/method choices, unique fingerprint, nullable one-to-one order link, identity/session, customer/delivery fields, frozen cart/custom-print JSON, gross/discount/payable/paid amounts, invoice metadata/expiry, tracking payload, timestamps, error reason, and notification/event markers.
2. Add indexes for status/created, invoice id, session/user, and expiry.
3. Add helpers for final payable amount, prepayment amount, stable event ids, display labels, and conversion eligibility.
4. Generate and inspect the migration, then run the model tests.

### Task 3: Move invoice creation from `Order` to `PaymentAttempt`

**Files:**
- Modify: `twocomms/storefront/views/monobank.py`
- Modify: `twocomms/storefront/views/checkout.py`
- Modify: `twocomms/storefront/views/utils.py`

**Steps:**
1. Replace order idempotency lookup with atomic attempt fingerprint lookup and expiry handling.
2. Freeze validated cart, variants, custom-print prices, customer/delivery data, attribution, discount and payment amount on the attempt.
3. Reject `cod` with a 400 response; never silently convert it to online payment.
4. Build Monobank metadata from attempt id/reference and attach invoice data to the attempt.
5. Remove pre-payment order/item/custom-lead creation, order Telegram notification, order `Lead`, and order conversion markers from invoice creation.
6. Keep cart and promo session data until conversion; clear them only after successful materialization.

### Task 4: Atomically materialize one order on verified payment

**Files:**
- Modify: `twocomms/storefront/views/monobank.py`
- Modify: `twocomms/storefront/views/utils.py`
- Modify: `twocomms/orders/facebook_conversions_service.py`
- Modify: `twocomms/orders/telegram_notifications.py`

**Steps:**
1. Resolve attempts by invoice/reference and pull Monobank status with signature and amount verification.
2. Lock the attempt; return existing linked order for duplicate success callbacks.
3. On success, create one `Order` and `OrderItem` set from the frozen snapshot, link approved custom leads, apply the frozen discount, and record promo usage once.
4. Mark attempt converted/paid or prepaid and save actual paid amount/history.
5. Schedule one post-commit Telegram payment notification and one `Purchase` event for either full payment or prepayment.
6. Persist and reuse attempt/order event ids for browser/CAPI deduplication; use discounted payable value and separate `paid_value`.
7. Keep failed, cancelled, expired, unknown, and mismatched attempts out of orders.

### Task 5: Add cleanup and admin payment-attempts view

**Files:**
- Create: `twocomms/orders/management/commands/expire_payment_attempts.py`
- Modify: `twocomms/storefront/views/admin.py`
- Modify: `twocomms/twocomms_django_theme/templates/pages/admin_panel.html`
- Create: `twocomms/twocomms_django_theme/templates/partials/admin_payment_attempts_section.html`

**Steps:**
1. Add a bounded cleanup command that marks overdue non-terminal attempts expired without touching orders.
2. Add a hidden-by-default admin view/filter with counts, pagination, filters, countdown/age labels, amount breakdown, product/customer/invoice/attribution data, and linked order.
3. Add a compact tab/action from the orders area without mixing attempts into normal order counts.
4. Add template tests or render checks for the staff-only view.

### Task 6: Verify, inspect production records, and ship

**Files:**
- No new source files unless verification exposes a defect.

**Steps:**
1. Run focused tests, full checkout/orders tests, `makemigrations --check`, `manage.py check`, and compile checks.
2. Inspect production records `TWC17072026N01` and `TWC17072026N02` read-only; preserve audit history and mark any historical duplicate consistently without deleting evidence.
3. Review staged diff and ensure unrelated Fable5 artifacts remain untracked.
4. Commit relevant changes on `main`, push `origin/main`, pull on the server with the requested SSH command, run migrations/check/static/restart as needed.
5. Verify production endpoints, admin attempts view, order counts, and live webhook/return behavior using safe read-only checks.
