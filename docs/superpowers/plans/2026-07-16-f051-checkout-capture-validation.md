# F-051 Checkout Capture Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/checkout/capture/` reject empty or unusable abandoned-checkout data with a truthful HTTP 400 response and no database/session mutation.

**Architecture:** Keep the existing same-origin guard, 30/min rate limit and debounced JSON/form clients. Parse JSON fail-closed as an object, retain form-encoded compatibility, and require at least one actionable recovery channel (phone or valid email); a cart or name alone is not recoverable. Return stable machine-readable error codes while the valid upsert behavior remains unchanged.

**Tech Stack:** Django 5.2, `JsonResponse`, Django validators/TestCase, MariaDB production verification.

**Baseline evidence (2026-07-16):** live `{}` POST returns HTTP 200 `{"ok":false}` and creates no row. Code also permits cart-only or full-name-only captures and returns `ok:true`, although `recover_checkouts` excludes rows without phone/email. Production has 10 captures: one cart-only row with all contact fields empty, age 7.06 days, converted false. It is outside recovery and remains under existing retention; this slice does not delete historical data.

---

### Task 1: Add fail-closed endpoint regressions

**Files:**
- Create: `twocomms/storefront/tests/test_checkout_capture.py`

- [x] **Step 1: Prove empty/unusable payloads are rejected**

POST empty JSON with and without a valid session cart; assert HTTP 400, `ok:false`, `error=contact_required`, zero CheckoutCapture rows, and no newly persisted session for the no-cart case. Repeat for whitespace-only fields, name-only and invalid-email-only payloads.

- [x] **Step 2: Prove malformed/non-object JSON is rejected**

POST malformed JSON, JSON list/string/null and assert HTTP 400 `error=invalid_payload`, no row and no 500. Keep form-encoded payload compatibility in a separate success test.

- [x] **Step 3: Prove existing rows are not touched by invalid requests**

Create an existing capture with `converted=True` and timestamps/contact data, issue an invalid request in the same session, and assert the row is byte-for-byte unchanged and remains converted.

- [x] **Step 4: Prove valid recovery channels still upsert**

Assert phone-only, valid-email-only and phone+name form/JSON requests return 200 `ok:true`, preserve prior nonblank fields, attach validated cart/total where present, and keep authenticated user/email behavior.

- [x] **Step 5: Run focused RED**

Run: `cd twocomms && python manage.py test storefront.tests.test_checkout_capture --settings=test_settings -v 2`

Expected: empty/cart-only/name-only responses are 200 or create rows, non-object JSON can error, and the new contract tests fail before production changes.

### Task 2: Implement strict capture contract

**Files:**
- Modify: `twocomms/storefront/views/checkout_capture.py`
- Test: `twocomms/storefront/tests/test_checkout_capture.py`

- [x] **Step 1: Parse request payload by content type**

For `application/json`, decode UTF-8 JSON and require a dictionary; malformed/non-object input returns `JsonResponse({'ok': False, 'error': 'invalid_payload'}, status=400)`. Other content types continue through `request.POST`.

- [x] **Step 2: Require a recovery channel before cart/session work**

Clean fields and validate email as today, then require `phone or email`. Return `JsonResponse({'ok': False, 'error': 'contact_required'}, status=400)` before reading/persisting the session cart. Full name and cart remain optional context only.

- [x] **Step 3: Preserve valid upsert behavior**

Keep same-origin/rate-limit status codes, one row per session, nonblank field preservation, validated cart total, authenticated user binding, and `converted=False` only after an accepted capture.

- [x] **Step 4: Run GREEN and adjacent checks**

Run the focused suite plus checkout/cart attribution neighbors, Django check, scoped compileall and `git diff --check`.

**Local implementation checkpoint (2026-07-16):** Focused RED ran 19 tests with
11 expected contract failures before the view change. A separate non-string JSON
field regression then failed with HTTP 500 before the type-safe cleaner change.
Focused GREEN passes 20 tests; checkout/cart/UTM neighbors pass 79 tests. The
suite covers empty/name-only/invalid-email payloads, malformed and non-object
JSON, no session/cart/database mutation on rejection, exact existing-row
preservation, valid phone/email/form upserts, cart total, authenticated binding,
same-origin and 30/min rate-limit behavior.

Final local verification: 99 focused and neighboring tests passed; Django check
reported no issues; scoped compileall and `git diff --check` passed; the scoped
secret/SSH fingerprint scan found no matches.

### Task 3: Review, ship and reconcile audit docs

**Files:**
- Modify: `docs/qa/AUDIT_FINDINGS_2026-07-09.md`
- Modify: `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`
- Modify: this plan

- [ ] **Step 1: Complete spec and quality reviews**

Confirm the browser autosave remains silent/compatible, no valid phone/email capture is lost, invalid requests cannot update existing rows, and errors expose no submitted PII.

- [ ] **Step 2: Commit, push, deploy and restart Passenger**

Push only the view/tests/plan, pull `main`, run focused server tests/check/compile, restart Passenger and verify storefront HTTP 200. No migration or static build is required.

- [ ] **Step 3: Run live negative acceptance**

POST `{}`, name-only, invalid-email-only and non-object JSON without a cart; assert HTTP 400 with stable error codes and production CheckoutCapture count delta 0. Do not create a synthetic valid capture.

- [ ] **Step 4: Mark F-051 fixed and deploy docs**

Use `[x] FIXED` only after local/server tests and live negative acceptance pass. Preserve the historical one cart-only row under retention rather than deleting it in this slice. Commit/push/deploy docs and add a final plan checkpoint.
