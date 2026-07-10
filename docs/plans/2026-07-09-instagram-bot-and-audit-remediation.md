# Instagram Bot and Audit Remediation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix confirmed 2026-07-09 audit findings with production-verified releases, beginning with the management Instagram bot.

**Architecture:** Bot releases isolate CRM state, Meta delivery behavior, mutations, and dashboard UI. Storefront releases fix shared roots first: order attribution/session propagation, payment post-commit dispatch, then pixel and dependent data quality. Every item must pass a server-side acceptance check before its audit checkbox is changed.

**Tech Stack:** Django, MySQL, Django migrations/tests, Meta Graph API, JavaScript templates, Passenger, SSH.

---

## Release protocol

1. Add or extend a focused failing test.
2. Implement the smallest complete behavior.
3. Run focused tests via SSH on the production host's test database.
4. Commit the scoped diff, fast-forward `main`, push, and deploy via SSH.
5. Apply migrations/build assets/restart only when relevant; verify the production acceptance line.
6. Mark the exact audit row complete only after the live proof exists.

### Task 1: Remove the tracked deployment credential (F-093)

**Files:** delete `deploy_paramiko.py`; update `docs/qa/AUDIT_FINDINGS_2026-07-09.md`; test with a value-suppressed tracked credential scan.

1. Confirm no tracked runtime or cron caller depends on the obsolete script.
2. Delete it; do not replace a hard-coded deploy implementation.
3. Verify the scan, `git diff --check`, server `git pull --ff-only`, then commit `security: remove tracked deployment credential`.

### Task 2: Add client-scoped delivery state (IG-005/007/013/014, F-097)

**Files:** modify `twocomms/management/ig_bot_models.py`, `services/instagram_bot.py`, `bot_views.py`, `templates/management/bot.html`; create migration `0075_ig_client_delivery_state.py`; test `tests_ig_audit_fixes.py` and `tests_ig_clients_ui.py`.

1. Write tests for exact classified permanent Graph errors, clearing on success, and Ukrainian API display values.
2. Persist bounded delivery state per client. Do not infer Message Requests from an ambiguous `#551` error.
3. Separate operational blocked-send notifications from sales transfer semantics.
4. Deploy/migrate/test through SSH. Do not check F-097 until Meta delivery itself has a passing live condition.
5. Commit `fix(ig): expose client delivery blocks`.

### Task 3: Preserve automatic manager takeover (IG-003, F-098)

**Decision:** The user explicitly rejected a new manual `Передати менеджеру` button. Managers already join conversations automatically, so this item must remain open as an audit/product decision rather than adding duplicate UI or changing the existing automatic takeover flow.

1. Do not add a manual transfer action.
2. Keep existing automatic manager-echo/takeover behavior covered while repairing adjacent client actions.
3. Do not mark F-098 fixed without an agreed replacement acceptance criterion.

### Task 4: Repair CRM client actions (IG-001/002/009/010/011, F-095)

**Files:** modify `templates/management/bot.html`, `bot_views.py`; test `tests_ig_clients_ui.py`, `tests_ig_sales_automation.py`.

1. Test Hide/Unhide/Lost contract, list refresh, action visibility, and Ukrainian feedback.
2. Test Hide against all automation paths: ingress, queued replies, active reply processing, active follow-up processing, and overview/statistics.
3. Give short-lived automation leases to any send path that must be mutually exclusive with Hide; never hold a DB transaction across Gemini/Meta I/O. A successful Hide must mean a later automated send cannot start.
4. Make recovery of claimed inbound messages depend on an actual processing claim timestamp and an inactive lease, never on `created_at` alone.
5. Replace silent fetches with a shared JSON mutation helper; reload the relevant list and remove stale detail.
6. Deploy and verify staff UI interaction.
7. Commit scoped fixes only after server-side regression tests pass.

### Task 5: Localize and improve dashboard (IG-004, F-096)

**Files:** modify `bot_views.py`, `templates/management/bot.html`; test `tests_ig_clients_ui.py`, `tests_ig_sales_automation.py`.

1. Test date-range bounds, display labels, funnel denominators, and non-duplicated revenue.
2. Return Ukrainian display labels, add today/7/30 range, funnel/operational counters, and clear empty states.
3. Deploy and check authenticated desktop/mobile rendering.
4. Commit `feat(ig): improve Ukrainian sales dashboard`.

### Task 6: Non-text events and echo safety (IG-006, IG-008)

**Files:** modify `services/instagram_bot.py`, `bot_webhook.py`; test `tests_ig_webhook_extract.py`, `tests_ig_audit_fixes.py`.

1. Test reaction/story/unsupported/echo payloads.
2. Record safe signal-only events, skip unsupported events without Gemini, and harden bot-echo deduplication.
3. Deploy, inspect worker health, commit `fix(ig): handle reactions and bot echoes safely`.

### Task 7: Storefront P0 attribution/session (F-071/F-021/F-033/F-045/F-044/F-068/F-072/F-073/F-074)

**Files:** modify `twocomms/storefront/utm_tracking.py`, checkout/COD creation paths, `twocomms/storefront/monobank.py`; add focused existing storefront attribution tests.

1. Test first-touch and session transfer across COD/Monobank paths.
2. Implement one trusted propagation helper and deterministic legacy recovery only where safe.
3. Deploy and prove a safe canary plus read-only ORM joins.
4. Commit `fix(attribution): link order to first-touch session`.

### Task 8: Payment conversion/dispatch (F-019/F-083/F-075/F-099/W2-7)

**Files:** modify `twocomms/storefront/monobank.py` and shared payment utility; extend Monobank webhook/payment tests.

1. Test idempotent paid transition and exactly-once `on_commit` dispatch.
2. Update conversion, purchase UserAction, CheckoutCapture, and CAPI through a shared path.
3. Deploy and use safe production payment-path evidence.
4. Commit `fix(payments): unify paid event dispatch`.

### Task 9: Pixel and ads data (F-030/F-089/F-020/F-022/F-032/F-048/F-057/F-076/F-084)

**Files:** update `analytics-loader.js` caller/template, UTM middleware/utils/settings; use matching analytics tests.

1. Add BFCache lifecycle regression coverage.
2. Repair initialization, pixel configuration, and attribution-quality roots in small releases.
3. Deploy and verify client-error absence plus live pixel lifecycle before ads gate changes.

### Task 10: Security/ops findings (F-087/F-088/F-090/F-029/F-031/F-035/F-036/W0-5)

Use read-only SSH to identify the owning system first. Protect documents, configure a non-secret webhook secret, install a verified backup cron, and resolve capacity/DB/CSP/provider defects only where production ownership is clear. Do not mark external-owner work complete without live proof.

### Task 11: SEO/cart/routes/hygiene (F-001--F-018/F-023/F-027--F-028/F-043/F-050--F-051/F-059/F-078/F-009/F-014--F-015/F-100--F-101)

Work one root-cause group at a time: title/data policy; color/feed/localization; product H1/alts; redirects; Nova Poshta; capture validation; static endpoints; imports/timezones. For every group add a regression test, deploy, execute the matching audit accept line, and commit a scoped fix.

### Task 12: Closeout

Update `docs/qa/AUDIT_FINDINGS_2026-07-09.md` and `TWOCOMMS_A_TO_B/technical/IMPLEMENTATION_PLAN.md` only after live acceptance. Re-verify remaining `[ ]` items; keep partial, external, and owner-blocked findings open.
