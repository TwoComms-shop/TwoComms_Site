# F-035 CSP Reporting Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CSP violation telemetry accept modern and legacy browser reports without 500 responses and retain privacy-safe evidence needed to decide whether any CSP source policy must change.

**Architecture:** Normalize legacy `application/csp-report` objects and Reporting API `application/reports+json` envelopes into one bounded stream. Sanitize and truncate every logged field, remove URL query/fragment data, filter non-actionable extension/inline/data noise, and write one JSON object per actionable report to a dedicated rotating `csp.log` with propagation disabled. Do not change the CSP allowlist until production evidence identifies a site-owned blocked origin or directive.

**Tech Stack:** Django 5.2, Python logging `RotatingFileHandler`, Django `SimpleTestCase`, JSON/URL standard-library parsers, production Passenger/cPanel logs.

**Baseline evidence (2026-07-16):** production `csp` logger has no handlers, `propagate=True`, and no `csp.log`; `stderr.log*` contains 588 bare `csp_violation` messages. The live response has 13 CSP directives with the expected analytics/social origins and `report-uri /csp-report/`. A standard Reporting API JSON array currently reaches `payload.get(...)` and raises `AttributeError`.

---

### Task 1: Add CSP receiver and logging regressions

**Files:**
- Create: `twocomms/storefront/tests/test_csp_reporting.py`
- Test: `twocomms/storefront/tests/test_csp_reporting.py`

- [x] **Step 1: Prove a Reporting API array is accepted and normalized**

POST an `application/reports+json` array containing two `type=csp-violation` envelopes with nested `body` dictionaries. Assert HTTP 204, two warning calls, document URL fallback from the envelope `url`, and structured event/directive fields.

- [x] **Step 2: Prove legacy objects remain compatible**

POST `{"csp-report": {...}}` as `application/csp-report`. Assert HTTP 204 and one structured warning containing `csp_violation`, `blocked_uri`, `document_uri`, `violated_directive`, `referrer`, and bounded user agent.

- [x] **Step 3: Prove privacy and noise controls**

Assert URL query strings/fragments are absent, common PII shapes are redacted, long fields are truncated, extension/self/inline/eval/data reports are ignored, non-CSP Reporting API types are ignored, and a single request cannot log more than 20 records.

- [x] **Step 4: Prove malformed input always fails closed with 204**

Cover invalid JSON, unsupported content type, top-level scalars, arrays containing scalars/null, and envelopes whose `body` is not an object. Assert no exception and no warning.

- [x] **Step 5: Assert dedicated logging configuration**

Assert `settings.LOGGING` defines a delayed `logging.handlers.RotatingFileHandler` for `csp.log` with bounded size/backups and PII filtering, while logger `csp` uses only that handler at WARNING with `propagate=False`.

- [x] **Step 6: Run focused RED**

Run: `cd twocomms && python manage.py test storefront.tests.test_csp_reporting --settings=test_settings -v 2`

Expected: failures from the current list `.get` crash, unsanitized/unbounded fields, missing structured message, and missing `csp` handler/logger configuration.

Evidence (2026-07-16): focused RED ran 9 tests and failed with 7 failures plus 9 errors (including subtests). It reproduced the Reporting API/scalar `.get` crashes, absent `csp_file`, raw `extra` logging, incomplete noise filtering, and missing sanitation/bounds before either production file was edited.

### Task 2: Normalize, sanitize, and persist actionable CSP reports

**Files:**
- Modify: `twocomms/storefront/views/static_pages.py`
- Modify: `twocomms/twocomms/settings.py`
- Test: `twocomms/storefront/tests/test_csp_reporting.py`

- [x] **Step 1: Add bounded report normalization**

Accept only the three documented JSON content types. Convert a legacy object, a direct report object, or at most 20 valid Reporting API envelopes into `(report, envelope)` pairs. Skip unrelated report types and malformed entries; always return HTTP 204.

- [x] **Step 2: Add privacy-safe field normalization**

Remove control characters, redact common email/phone/long-number shapes, strip query strings and fragments from URL-like fields, bound URL/directive/referrer/user-agent lengths, and drop extension plus self/inline/eval/data noise.

- [x] **Step 3: Log one JSON object per actionable report**

Serialize the normalized record with stable keys and `ensure_ascii=False`, then call `logging.getLogger("csp").warning(serialized_json)`. Do not place raw payloads or unsanitized `extra` fields on the LogRecord.

- [x] **Step 4: Configure a dedicated rotating CSP log**

Add `csp_file` pointing to `BASE_DIR / "csp.log"`, use WARNING, delayed open, 5 MiB rotation, five backups, UTF-8, and the existing PII filter. Add logger `csp` with only this handler and `propagate=False`.

- [x] **Step 5: Run GREEN and adjacent checks**

Run: `cd twocomms && python manage.py test storefront.tests.test_csp_reporting --settings=test_settings -v 2`

Run: `cd twocomms && python manage.py check --settings=test_settings`

Run: `cd twocomms && python -m compileall -q storefront/views/static_pages.py twocomms/settings.py storefront/tests/test_csp_reporting.py`

Expected: focused tests pass, Django check is clean, and compileall exits 0.

Evidence (2026-07-16): the initial focused GREEN ran 9 tests with 9 passes. Quality-review regressions then produced 5 failures and 2 errors across 15 tests for encoded URL PII/credentials, first-20 and 64 KiB input bounds, `RequestDataTooBig`, pre-sanitizer field slicing, UTC timestamps, and message-only formatting. A follow-up endpoint regression reproduced one `UnicodeEncodeError` from a JSON-escaped lone surrogate. The updated focused GREEN passed all 16 tests. `manage.py check --settings=test_settings` and the scoped `compileall` command exited 0. The existing stale compression-manifest warning remained informational and no CSP allowlist/header changed.

### Task 3: Review, ship, and validate production telemetry

**Files:**
- Modify: `docs/superpowers/plans/2026-07-16-f035-csp-reporting.md`

- [ ] **Step 1: Complete spec and code-quality review**

Review normalization coverage, privacy controls, logging isolation, rate/amplification bounds, and confirm no CSP source directive changed. Re-run Task 2 verification immediately before the code commit.

- [ ] **Step 2: Commit, push, deploy, and restart Passenger**

Commit only the plan, focused test, receiver, and logging settings. Push `main`, pull production with `--ff-only`, run focused server tests/check, and `touch tmp/restart.txt`. No migration, collectstatic, compress, or DB mutation is required.

- [ ] **Step 3: Run safe live canaries**

POST one synthetic Reporting API report using `https://audit.invalid/f035.js?secret=redacted` and one malformed payload. Both must return 204. Parse the newest `csp.log` line as JSON and assert event/directive fields exist, the URL is query-free, the marker is identifiable, the logger owns a rotating handler with `propagate=False`, and direct public GET `/csp.log` is 403/404.

- [ ] **Step 4: Observe real production evidence before policy changes**

Inventory sanitized non-canary `blocked_uri` origins/directives from the new log. Do not add an allowlist origin unless it maps to deployed code or verified GTM configuration. If no real reports have arrived yet, keep F-035 `[o] PARTIAL` with an observation window rather than claiming the original violations fixed.

### Task 4: Reconcile F-035 audit status

**Files:**
- Modify: `docs/qa/AUDIT_FINDINGS_2026-07-09.md`
- Modify: `docs/qa/PRE_ADS_MASTER_AUDIT_CHECKLIST.md`
- Modify: `TWOCOMMS_A_TO_B/technical/audit_report_section4_seo.md`
- Modify: `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`
- Modify: `docs/superpowers/plans/2026-07-16-f035-csp-reporting.md`

- [ ] **Step 1: Preserve the historical finding and record corrected scope**

Keep the original bare stderr count as historical evidence. Record that current breakage was the receiver/logging pipeline; distinguish it from an unproven current CSP allowlist defect.

- [ ] **Step 2: Mark status from production evidence**

Use `[x] FIXED/DONE` only if the telemetry pipeline works and enough real reports prove no policy residual. Use `[o] PARTIAL` if observation is still needed or a verified blocked origin remains unresolved.

- [ ] **Step 3: Commit, push, deploy, and verify documentation**

Commit only audit-plan/document changes, push/pull `main`, and verify every current F-035 row and linked report agrees on status, evidence, and observation requirements.
