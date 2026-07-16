# F-048 FBC Order Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover coarse Meta order attribution from a fresh, valid `_fbc` cookie without treating the generic `_fbp` browser identifier or a stale Meta cookie as a new paid acquisition.

**Architecture:** Keep page-view middleware inference limited to explicit UTM and current-request click IDs. Add an order-link fallback that validates `fb.<domain-index>.<epoch-ms>.<fbclid>`, enforces a configurable seven-day click window against `Order.created`, and creates `facebook/paid_social` attribution only when stronger evidence is absent. Add a separate dry-run-first historical command that creates/link UTMSession rows only for deterministic session keys and never invents a key for irrecoverable orders.

**Tech Stack:** Django 5.2, Python `datetime`, MariaDB production data, Django `TestCase` and management commands.

**Baseline evidence (2026-07-16):** production has 38 web orders. Thirty-one carry `tracking.fbp`; 29 of those have no internal UTM, but `_fbp` alone is expected for organic/direct users. Thirteen unattributed orders carry structurally valid `_fbc`, all with click timestamps 0-44.65 days before order creation; 12 expose deterministic external session evidence across nine unique keys, while one has no key. No matching UTMSession exists. Under a conservative seven-day window, nine orders remain eligible, eight are linkable across seven keys, and one has no key. There have been no new web orders since 2026-07-14. A raw-fbclid rollback canary already proved the existing `9854c18b` path on production and cleaned to 0 Order / 0 UTMSession rows.

---

### Task 1: Specify fresh FBC order fallback

**Files:**
- Modify: `twocomms/storefront/tests/test_utm_attribution.py`
- Modify: `twocomms/storefront/tests/test_utm_normalization.py`

- [x] **Step 1: Add FBC parser regressions**

Test a valid `fb.1.1700000000000.click-id` value, malformed prefix/domain/timestamp/click ID, whitespace/control characters, and overlong values. The parser must return the click timestamp and click ID only for valid input.

- [x] **Step 2: Add order-link policy regressions**

Create an unattributed Order and assert that a valid `_fbc` inside the configured seven-day window creates a linked UTMSession with only `facebook/paid_social`, `fbc`, and extracted `fbclid`. Assert `_fbp` alone, malformed `_fbc`, future `_fbc`, stale `_fbc`, and an already-attributed order do not synthesize attribution. Explicit UTM/current raw click-ID behavior must remain authoritative.

- [x] **Step 3: Run focused RED**

Run: `cd twocomms && python manage.py test storefront.tests.test_utm_normalization storefront.tests.test_utm_attribution --settings=test_settings -v 2`

Expected: new parser and FBC-only order tests fail because no validated order-time fallback exists; existing tests remain green.

RED evidence (2026-07-16): 28 tests ran; 26 passed and exactly two expected assertions failed. `ParseFbcTests.test_valid_fbc_returns_click_timestamp_and_id` received `None`, and `UTMOrderAttributionTests.test_fresh_fbc_rebuilds_meta_attribution_at_order_link_time` left `order.utm_source` as `None` instead of `facebook`. All malformed/stale/future/FBP-only and existing-attribution policy tests passed before implementation.

### Task 2: Implement validated order-time recovery

**Files:**
- Modify: `twocomms/storefront/utm_utils.py`
- Modify: `twocomms/storefront/utm_tracking.py`
- Modify: `twocomms/twocomms/settings.py`
- Test: files from Task 1

- [x] **Step 1: Parse FBC without logging identifiers**

Add `parse_fbc(value)` that returns `None` or a small immutable result containing `created_at_ms` and `click_id`. Require ASCII, total length <=255, `fb` prefix, numeric non-negative domain index, a 13-digit plausible epoch-ms value, and a non-empty bounded click ID without whitespace/control characters.

- [x] **Step 2: Add an explicit policy window**

Set `META_FBC_ATTRIBUTION_WINDOW_DAYS = max(1, min(90, _env_int(..., 7)))`. Tests override the setting; production defaults to seven days unless the owner explicitly changes policy.

- [x] **Step 3: Recover only at order-link time**

After session/UTMSession/first-touch fallbacks fail in `link_order_to_utm`, inspect request `_fbc`. Require a valid parsed value, click time not later than `Order.created` plus small clock skew, and age within the configured window. Reuse `_rebuild_utm_session_from_attribution()` with `utm_source=facebook`, `utm_medium=paid_social`, `fbc`, and extracted `fbclid`. Do not alter general middleware inference and do not use `_fbp` as acquisition evidence.

- [x] **Step 4: Run GREEN and adjacent checks**

Run the Task 1 command, `python manage.py check --settings=test_settings`, scoped `compileall`, and `git diff --check`.

GREEN evidence (2026-07-16): the focused normalization/order-attribution suite passed 29/29. Django check reported no issues; scoped `compileall` and `git diff --check` exited 0. The scoped secret scan found no added credentials (the only match was unchanged settings context reading `MONOBANK_TOKEN` from the environment). Self-review confirmed no middleware changes and no `_fbp` acquisition inference.

### Task 3: Add guarded historical FBC reconciliation

**Files:**
- Create: `twocomms/storefront/management/commands/backfill_fbc_order_attribution.py`
- Create: `twocomms/storefront/tests/test_backfill_fbc_order_attribution.py`

- [x] **Step 1: Write command RED tests**

Cover dry-run/no writes, exact apply guards, valid seven-day candidates, stale/future/malformed FBC, `_fbp`-only, missing/invalid/mismatched session keys, same-key duplicate orders, conflicting FBC evidence inside one key group, existing attribution/UTMSession conflicts, rollback on write failure, and idempotent second apply.

RED evidence (2026-07-16): the new 12-test command suite was run before the command existed. Eleven scenarios errored at command discovery with `Unknown command: 'backfill_fbc_order_attribution'`; the guard scenario intercepted the same missing-command `CommandError`. After correcting a test-only timezone import, the repeated RED had no unrelated setup failure.

- [x] **Step 2: Build a fail-closed plan**

Scan only web Orders with no UTM FK and no raw UTM fields. Require valid fresh FBC and no conflicting raw click IDs. Resolve a 32-character Django key from `Order.session_key` or `tracking.external_id=session:<key>` without writing the key. Group by key and abort ambiguous groups. Report eligible, linkable, stale, no-key, invalid, conflicting, create-session, reuse-session, and order counts without printing IDs or cookie values.

- [x] **Step 3: Apply atomically with exact guards**

Require `--expect-groups`, `--expect-orders`, all residual-category counts, `--expect-create-sessions`, and `--expect-reuse-sessions` before `--apply`. Lock candidate Orders and matching UTMSessions, rescan, create at most one UTMSession per safe key, set `facebook/paid_social`, validated FBC/fbclid, evidence-based `first_seen/last_seen` on newly created sessions, and link every safe Order while copying five raw UTM fields. Reused sessions preserve their existing timestamps. Never mutate `Order.session_key`, `payment_payload`, SiteSession, UserAction, or create events. Any drift/conflict rolls back the entire apply.

- [x] **Step 4: Run command tests and full attribution suite**

Run the new command tests plus normalization, order attribution, existing click-ID backfill, and existing Order reconciliation tests.

GREEN evidence (2026-07-16): the focused command suite passed 13/13 after an additional RED regression caught and fixed reuse of a SiteSession-linked UTM with a different session key. The combined command, normalization, order-attribution, click-ID backfill, and Order reconciliation suite passed 60/60. Django check reported no issues; scoped compileall, `git diff --check`, and the scoped secret scan exited clean. This task did not run or apply the command against production.

Reviewer hardening evidence (2026-07-16): four Important follow-up regressions first failed against `d368825d`. Invalid/stale/future/mismatched evidence did not poison otherwise-valid peers sharing its key, an out-of-scope Order did not block reuse of its UTMSession, and reuse overwrote `first_seen/last_seen`; the new create/reuse CLI guards were initially absent. After the fail-closed changes, the focused command suite passed 17/17 and the combined attribution suite passed 64/64. Create-to-reuse drift now aborts before mutation, any existing Order link blocks UTM reuse, reused timestamps remain unchanged, and residual rows poison every valid key they expose without losing their residual bucket counts.

Quality-review evidence (2026-07-16): two new whole-group regressions first failed because durable attribution in `SiteSession.first_touch_data` and `UserAction.metadata['first_touch']` was ignored, leaving both conflicting two-Order groups linkable. The command now reads (but never logs or writes) those snapshots and permits only empty evidence or canonical `facebook/paid_social` with the same FBC/fbclid. Any explicit campaign/content/term, alternate source/medium, Google/TikTok click ID, or mismatched Meta evidence makes the entire key group conflicting. The focused suite passed 20/20 and the combined attribution suite passed 67/67 after the change.

### Task 4: Ship, back up, apply, and reconcile docs

**Files:**
- Modify: `docs/qa/AUDIT_FINDINGS_2026-07-09.md`
- Modify: `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`
- Modify: this plan

- [ ] **Step 1: Review, commit, push, and deploy code**

Run spec and data-safety review, commit only the scoped code/tests/plan, push `main`, pull on production, run the focused server suite/check/compile, restart Passenger, and verify storefront HTTP 200.

- [ ] **Step 2: Dry-run production and create a private rollback snapshot**

Run the command without `--apply`. Before mutation, write a mode-0600 JSON snapshot under untracked `twocomms/tmp/audit_backups/` containing only candidate primary keys and original Order/UTMSession fields needed for rollback. Print only path, mode, and aggregate counts, never FBC/FBP/session values.

- [ ] **Step 3: Apply with exact observed guards and verify**

Apply only if dry-run counts equal the reviewed baseline. Re-run dry-run expecting zero linkable candidates, verify every changed Order/session against the private snapshot, confirm raw-only/no-key and stale rows stayed unchanged, and verify storefront health. Do not delete the snapshot in this slice.

- [ ] **Step 4: Mark F-048 partial, not fixed**

Use `[o] PARTIAL`: future fresh FBC order attribution is fixed and the safely linkable historical cohort is reconciled, but stale FBC rows and the order without a deterministic key remain intentionally untouched. Record that `_fbp`-only is not a defect. Commit/push/deploy docs and add a final plan checkpoint.
