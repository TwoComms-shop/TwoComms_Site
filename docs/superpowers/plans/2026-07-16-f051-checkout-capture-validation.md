# F-051 Checkout Capture Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/checkout/capture/` reject empty or unusable abandoned-checkout data with a truthful HTTP 400 response and no database/session mutation.

**Architecture:** Keep the existing same-origin guard, 30/min rate limit and debounced JSON/form clients. Parse JSON fail-closed as an object, retain form-encoded compatibility, and require at least one actionable recovery channel (a normalized Ukrainian phone or valid email); a cart or name alone is not recoverable. For authenticated name-bearing captures, a validated account email may supply the recovery channel, but an empty payload may not. Treat `converted=True` and a real COD/Monobank order tied to the current session as terminal states: preserve an existing terminal row, convert only an active row, or create a minimal PII-free terminal marker when no capture exists. Return stable machine-readable error codes while active-capture upserts remain compatible.

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

Assert normalized phone-only, valid-email-only and phone+name form/JSON requests return 200 `ok:true`, preserve prior nonblank fields, attach validated cart/total where present, and keep authenticated user/email behavior. Reject invalid phone-only JSON/form payloads. A valid authenticated account email may recover a name-bearing capture, but not an empty payload, and an invalid account email is ignored.

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

Clean fields, normalize phone through the checkout Ukrainian-phone validator, and validate submitted/account emails. Then require a normalized `phone` or valid `email`. A validated authenticated account email may be used only when the submitted payload also contains a name or normalized phone. Return `JsonResponse({'ok': False, 'error': 'contact_required'}, status=400)` before reading/persisting the session cart. Full name and cart remain optional context only.

- [x] **Step 3: Preserve valid upsert behavior**

Keep same-origin/rate-limit status codes, one row per session, nonblank field preservation, validated cart total and authenticated user binding. Serialize active-row updates with `transaction.atomic()` plus `select_for_update()`, save only named mutable fields, never write `converted`, and return `ok:true` without changing any field when the locked row is already converted.

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

**Quality-review follow-up (2026-07-16):** The expanded focused suite ran 26
tests with 4 expected RED failures before the follow-up implementation: invalid
JSON phone, invalid form phone, authenticated name plus valid account email, and
a valid late beacon changing a converted row. After phone normalization,
validated account-email policy and locked narrow-field upserts, focused GREEN is
26/26 and the unchanged checkout/cart/UTM neighbor set is 79/79. Converted rows
are now terminal and byte-for-byte unchanged by accepted late autosaves.
Final follow-up verification passed all 105 focused-plus-neighbor tests and the
4 existing checkout phone-normalizer tests; Django check, scoped compileall,
`git diff --check` and the scoped secret/SSH fingerprint scan were clean.

**F-075 conversion-race dependency (2026-07-16):** Quality re-review found that
COD conversion was update-only and Monobank invoice creation had no capture
conversion write, so a first late beacon could create a new active capture after
the order. The new focused RED ran 31 tests with 4 expected failures: COD and
Monobank completed-order sessions without captures, converted-only handling of
an active completed-order capture, and Monobank invoice-success conversion. The
Monobank API-failure control already passed and kept its capture active while
the orphan order was deleted. Focused GREEN is 31/31.

The endpoint does not scan the unindexed `Order.session_key`. It reads the
indexed order PK from server-written session evidence (`last_order_submit` for
COD or `monobank_pending_order_id` after a valid invoice), then verifies that
the selected order has the current session key. Under the existing capture row
lock, completed-order requests make the shared terminal transition. COD keeps
its success-boundary transition inside the order transaction; Monobank performs
the same transition only after invoice ID/payment fields are saved. Invoice API
failure leaves the capture active for retry.
Final dependency verification passed the expanded prior combined suite at
108/108, the full Monobank/Nova checkout module at 23/23 (including the same
4 phone-normalizer cases), and the view-export regression at 1/1. Django check,
scoped compileall, `git diff --check` and the scoped secret scan were clean.

**Final concurrency review (2026-07-16):** The PK/session-evidence guard alone
failed review because cached DB session evidence is persisted by response
middleware, leaving a window after an update-only conversion miss. Strict
closure now uses a terminal upsert: conditional active-row update, exact no-op
for an existing terminal row, or a minimal PII-free `converted=True` marker.
Creation runs in an inner `transaction.atomic()` savepoint; `IntegrityError` is
caught outside that savepoint and followed by a conditional update/terminal
check, so a unique-key race does not poison the surrounding COD transaction.

The final focused RED ran 35 tests with 5 expected contract breaks (2 failures
and 3 missing-marker errors). Focused GREEN is 35/35. SQLite's in-memory test
database cannot reliably exercise a two-connection barrier without lock-flaky
threads, so the concurrency regression deterministically forces the initial
update miss while a competing active unique row is present. The create then
raises the real database `IntegrityError` inside the inner savepoint; the retry
converts that row and a query inside the outer atomic proves the transaction
remains usable.

Final terminal-upsert verification passed the expanded prior suite at 112/112,
the full Monobank/Nova checkout module at 23/23, and the view-export regression
at 1/1. Django check, scoped compileall, `git diff --check` and the scoped secret
scan were clean.

### Task 3: Review, ship and reconcile audit docs

**Files:**
- Modify: `docs/qa/AUDIT_FINDINGS_2026-07-09.md`
- Modify: `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`
- Modify: this plan

- [x] **Step 1: Complete spec and quality reviews**

Confirm the browser autosave remains silent/compatible, no valid normalized-phone/email capture is lost, invalid requests cannot update existing rows, converted rows cannot be reopened, and errors expose no submitted PII.

- [x] **Step 2: Commit, push, deploy and restart Passenger**

Push only the view/tests/plan, pull `main`, run focused server tests/check/compile, restart Passenger and verify storefront HTTP 200. No migration or static build is required.

- [x] **Step 3: Run live negative acceptance**

POST `{}`, name-only, invalid-email-only, invalid-phone-only and non-object JSON without a cart; assert HTTP 400 with stable error codes and production CheckoutCapture count delta 0. Do not create a synthetic valid capture.

- [x] **Step 4: Mark F-051 fixed and deploy docs**

Use `[x] FIXED` only after local/server tests and live negative acceptance pass. Preserve the historical one cart-only row under retention rather than deleting it in this slice. Commit/push/deploy docs and add a final plan checkpoint.

**Production checkpoint (2026-07-16):** Runtime commits were rebased over the
merged PDP/Fable5 bundle and pushed through `c2945228`. Server tests passed
56/56 + 81/81, production check/compile passed, and
`orders_checkoutcapture` was confirmed InnoDB. The rollback canary covered
PII-free marker creation, terminal no-op and active-to-converted-only behavior.
After Passenger restart, six invalid JSON/form probes returned the expected
HTTP 400 errors and the production row count stayed 10→10. Historical
reconciliation updated exactly captures 2, 4, 7 and 8, each with a matching
Order session; final production state is 4 converted and 6 active no-order
captures. Root/product returned 200 and anonymous admin returned 302.

**Documentation closeout (2026-07-16):** Audit commit `d2d3477e` was pushed
and pulled on production. Because the intervening `d5937675` Fable5 commit also
changed static/template assets, the server additionally passed Fable5 31/31,
ran collectstatic (2 copied, 943 post-processed), rebuilt 4 compressor blocks
and restarted Passenger. Final live checks: root/product 200, capture GET 405
(POST-only), canonical anonymous Fable5 editor 403, server HEAD `d2d3477e`.
