# F-036 Telegram Delivery Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover order/admin Telegram text notifications from transient connection failures and stop recording or reporting failed deliveries as successful.

**Architecture:** Add a bounded retry policy only to Telegram `sendMessage` requests, where replay does not involve file stream state. Retry connection/timeouts, HTTP 429, and 5xx responses with short bounded backoff; do not retry Telegram `ok:false` or non-429 4xx responses. Preserve per-target isolation, log recovery/exhaustion without tokens/chat IDs/message bodies, and make critical callers act on the existing boolean result instead of claiming success after `False`.

**Tech Stack:** Django 5.2, `requests`, Python logging/time, daemon-thread order tasks, Django `TestCase`/`SimpleTestCase`, production cPanel/Passenger logs.

**Baseline evidence (2026-07-16):** the production order bot is configured for two targets and a safe `getMe` probe returns HTTP 200/`ok=true`. Across `stderr.log*`, the exact `Exception in send_message to admin` signature appears 27 times. The current rotation contains 17 such lines: 12 SSL/EOF failures, four `RemoteDisconnected`, and one unclassified. Individual print lines have no timestamps; the current file mtime is 2026-07-16 16:07:50 UTC. The notifier performs one POST and returns `False`, while task/payment/contact/Nova Poshta paths can still claim success. This is alert reliability, not storefront availability.

---

### Task 1: Add transport and truthful-state regressions

**Files:**
- Create: `twocomms/orders/tests/test_telegram_notifications.py`
- Create: `twocomms/orders/tests/test_tasks.py`
- Modify: `twocomms/storefront/tests/test_monobank_webhook.py`
- Create: `twocomms/storefront/tests/test_telegram_contact_manager.py`

- [x] **Step 1: Prove transient connection recovery**

Mock `requests.post` to raise a `requests.ConnectionError` containing `RemoteDisconnected` and then return a successful Telegram response. Assert two attempts, one patched backoff sleep, a `retry_recovered` log, and a successful result. Repeat with `SSLError`/`Timeout` coverage where useful without duplicating implementation assertions.

- [x] **Step 2: Prove bounded exhaustion and response classification**

Assert three transient failures produce exactly three attempts and `False`; HTTP 500/429 are bounded and retried; Telegram HTTP 200 `ok:false` plus non-429 4xx are returned without blind retry. Tests must patch sleep and never access Telegram.

- [x] **Step 3: Prove two-target isolation and partial delivery**

Make the first target exhaust retries and the second succeed. Assert the second is still attempted, the overall result reflects at least one successful admin delivery, and a sanitized partial-failure log is emitted without target IDs/token/message content.

- [x] **Step 4: Prove background tasks do not log false success**

Mock each supported notifier method to return `False`, call `_send_notification`, and assert a delivery-failed warning with no `sent` info log. Assert `True` produces the existing success log.

- [x] **Step 5: Prove post-payment state is persisted only after delivery**

Extend `_send_post_payment_events` coverage so `send_new_order_notification=False` does not set `payment_payload.telegram_notifications.order_notification_sent`; `True` does. Other receipt/pixel dispatch behavior must remain unchanged.

- [x] **Step 6: Prove contact-manager response reflects Telegram failure**

POST the contact-manager endpoint with a valid cart while `send_admin_message=False`; assert an error response so the buyer can retry. Assert `True` retains the success response.

- [x] **Step 7: Run focused RED**

Run: `cd twocomms && python manage.py test orders.tests.test_telegram_notifications orders.tests.test_tasks storefront.tests.test_monobank_webhook storefront.tests.test_telegram_contact_manager --settings=test_settings -v 2`

Expected: failures from one-attempt transport, unconditional task success log, unconditional payment flag, and contact-manager success after `False`.

Evidence (2026-07-16): focused RED ran 32 tests and failed with 10 expected failures. The failures covered missing transient/status retries, missing recovery/exhaustion/partial logs, false task success, false payment persistence, and false contact-manager success. Existing true paths and single-attempt generic/non-retry baselines remained green. P1 follow-up review added four personal-text regressions; the notifier-only RED ran 12 tests with four expected failures because `send_personal_message` bypassed retries, trusted HTTP 200 `ok:false`, and printed sensitive transport context.

### Task 2: Implement bounded sendMessage retry and truthful callers

**Files:**
- Modify: `twocomms/orders/telegram_notifications.py`
- Modify: `twocomms/orders/tasks.py`
- Modify: `twocomms/storefront/views/utils.py`
- Modify: `twocomms/storefront/views/cart.py`
- Test: files from Task 1

- [x] **Step 1: Add a sendMessage-only retry helper**

Keep generic document/media `_post_json` behavior single-attempt. For `sendMessage`, attempt at most three times and retry only `requests.ConnectionError`, `requests.Timeout`, HTTP 429, and HTTP 5xx. Use short backoff with an upper bound; tests patch sleep. Treat HTTP 200 `ok:false` and non-429 4xx as final failures.

- [x] **Step 2: Replace raw prints with sanitized structured logs**

Log event names for retry, recovered, exhausted, partial, and API rejection using attempt/target index/count and exception class/status only. Never log bot tokens, chat IDs, message bodies, reply markup, or raw exception strings.

- [x] **Step 3: Preserve target isolation and result contracts**

Continue after one target fails. `return_results=True` returns only successful Telegram results for message-reference persistence; boolean mode returns true when at least one configured admin target received the text. Emit a partial warning when not all targets succeeded.

- [x] **Step 4: Make critical callers consume boolean results**

In `orders.tasks`, log sent only after `True`; log failed after `False`. In post-payment dispatch, set `order_notification_sent` only after `send_new_order_notification=True`. In contact-manager, return the existing retryable error response when `send_admin_message=False`.

- [x] **Step 5: Run GREEN and adjacent checks**

Run the Task 1 focused command.

Run: `cd twocomms && python manage.py check --settings=test_settings`

Run: `cd twocomms && python -m compileall -q orders/telegram_notifications.py orders/tasks.py storefront/views/utils.py storefront/views/cart.py orders/tests/test_telegram_notifications.py orders/tests/test_tasks.py storefront/tests/test_telegram_contact_manager.py`

Expected: focused tests pass, Django check is clean, and compileall exits 0.

Evidence (2026-07-16): final focused GREEN passed 36/36 tests, including the personal-text path used by status and TTN notifications; `manage.py check --settings=test_settings`, the scoped `compileall`, and `git diff --check` all exited 0. The test run and Django check retain the pre-existing stale compressor-manifest warning; no system-check issue was reported.

### Task 3: Review, ship, and verify without synthetic messages

**Files:**
- Modify: `docs/superpowers/plans/2026-07-16-f036-telegram-delivery-reliability.md`

- [x] **Step 1: Complete spec and code-quality review**

Review duplicate-delivery tradeoffs, retry classification/bounds, target isolation, sanitized logs, daemon-thread residual, and all false-success state paths. Confirm no bot token/chat ID/message text enters new logs or tests.

- [x] **Step 2: Commit, push, deploy, and restart Passenger**

Commit only the plan, focused tests, notifier, tasks, and two critical storefront callers. Push/pull `main`, run focused server tests/check/compile, and `touch tmp/restart.txt`. No migration/static/DB mutation is required.

- [x] **Step 3: Run safe production checks**

Run Telegram `getMe` and report only HTTP/`ok` booleans. Inspect logger/code SHA and pre/post error counters without exposing IDs or messages. Do not send a synthetic admin/customer message.

- [ ] **Step 4: Observe natural delivery evidence**

Keep F-036 `[o] PARTIAL` until natural order/admin traffic proves either `retry_recovered` or successful `admin_tg_messages` references and no false `sent` state after exhaustion. Record that bounded retries can duplicate a message if Telegram processed a request but its response was lost; there is no Telegram idempotency key. Durable outbox/process-exit loss remains separate follow-up scope.

### Task 4: Reconcile F-036 audit status

**Files:**
- Modify: `docs/qa/AUDIT_FINDINGS_2026-07-09.md`
- Modify: `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`
- Modify: `docs/superpowers/plans/2026-07-16-f036-telegram-delivery-reliability.md`

- [x] **Step 1: Preserve historical evidence and corrected scope**

Retain the original `RemoteDisconnected` sample and add sanitized current counts/config/getMe evidence. State explicitly that F-036 concerns notification reliability, not site uptime.

- [x] **Step 2: Mark status from natural production evidence**

Use `[o] PARTIAL` after code/server verification while natural delivery observation is pending. Use `[x] FIXED/DONE` only after a real notification demonstrates truthful success/recovery and no remaining finding-specific defect.

- [x] **Step 3: Commit, push, deploy, and verify documentation**

Push/pull only the audit/plan documentation and verify every F-036 row agrees on status, evidence, duplicate risk, and residual observation.
