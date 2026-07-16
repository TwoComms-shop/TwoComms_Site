# F-028 PDP Variant Locale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop Fable5 colour merchandising from leaking Ukrainian names and SEO text into RU/EN product pages, variant APIs, quick view, and structured data without changing the existing per-locale commercial values of print designs.

**Architecture:** Normalize the active locale at each request/schema boundary and pass it explicitly into `variant_public_context()`. Make the product-instance detailed-variant memo language-keyed so one Product object cannot reuse Ukrainian results for RU/EN. Keep the cross-locale naming-policy question separate: the curated source currently encodes different UK/RU/EN identities, which remain untouched, while the cross-locale slug-family policy awaits owner approval.

**Tech Stack:** Django 5.2, modeltranslation, Django `TestCase`, Fable5 merchandising services, JSON-LD, production MariaDB/live HTTP verification.

---

### Task 1: Add locale-propagation regressions

**Files:**
- Modify: `twocomms/storefront/tests/test_fable5_variant_merchandising.py`
- Test: `twocomms/storefront/tests/test_fable5_variant_merchandising.py`

- [x] **Step 1: Add a no-fit product fixture with localized product fields and legacy UK variant content**

Create a published hoodie with `title_uk`, `title_ru`, `title_en`, localized SEO fields, one default colour variant, and legacy Ukrainian `VariantDetails.display_name`/SEO values. Do not add fit options so the later fit-specific re-resolution cannot hide the base-helper defect.

- [x] **Step 2: Assert helper memoization is isolated by language**

Call `get_detailed_color_variants(product, lang="ru")` and then `get_detailed_color_variants(product, lang="en")` on the same Product instance. Assert RU and EN `display_name`, `seo_title`, `seo_description`, and `marketing_html` come from their requested-language Product fallbacks and neither payload contains the legacy Ukrainian variant text.

- [x] **Step 3: Assert RU/EN PDP and variant API payloads use the request locale**

Request `/ru/product/<slug>/`, `/en/product/<slug>/`, and localized `get_product_variants` routes. Assert the visible H1, `selected_variant_merchandising`, and API `display_name` equal the locale-specific Product title. Assert Ukrainian legacy content is absent from the rendered `variant-data` payload.

- [x] **Step 4: Run the focused module to verify RED**

Run: `cd twocomms && python manage.py test storefront.tests.test_fable5_variant_merchandising --settings=test_settings -v 2`

Expected: FAIL because `get_detailed_color_variants()` has no `lang` parameter and its callers/default variant resolver use Ukrainian.

### Task 2: Propagate and isolate the active language

**Files:**
- Modify: `twocomms/storefront/services/catalog_helpers.py`
- Modify: `twocomms/storefront/views/product.py`
- Modify: `twocomms/storefront/seo_utils.py`
- Modify: `twocomms/storefront/urls.py`
- Test: `twocomms/storefront/tests/test_fable5_variant_merchandising.py`

- [x] **Step 1: Add a normalized language argument and per-language memo**

Implement `get_detailed_color_variants(product, lang="uk")`, normalize to `uk`, `ru`, or `en`, call `variant_public_context(variant, lang=language)`, and store results in a dictionary keyed by language on the Product instance. Preserve UK as the compatibility default for non-request callers.

- [x] **Step 2: Pass the request language from every public product boundary**

In `product_detail`, `get_product_variants`, and `quick_view`, derive `(request.LANGUAGE_CODE or "uk").split("-", 1)[0].lower()` and pass it to the helper. Do not alter pricing, fit resolution, redirects, or product selection.

- [x] **Step 3: Restore integer product API route precedence**

Move the existing product images, variants, and quick-view integer routes before the wildcard variant-slug routes. This is a prerequisite for exercising the localized variants API through its real public URL; do not change route names or view behavior.

- [x] **Step 4: Pass the active language to structured variant content**

At both `variant_public_context()` calls in `storefront/seo_utils.py`, use the existing `get_language()` import, normalize it to a two-letter code, and pass `lang=language`. Do not change canonical, offer, feed ID, or schema graph behavior.

- [x] **Step 5: Run GREEN and adjacent regressions**

Run: `cd twocomms && python manage.py test storefront.tests.test_fable5_variant_merchandising storefront.tests.test_product --settings=test_settings -v 2`

Run: `cd twocomms && python manage.py check --settings=test_settings`

Expected: all selected tests pass and Django check reports no issues.

Local evidence: the focused F-028 module passed 16/16, including RU/EN color-PDP Product JSON-LD name and description assertions. Removing only the selected-variant `lang=language` propagation made both JSON-LD locale subtests fail with the legacy UK display name; restoring it returned the module to green. The restored images/variants/quick-view route contracts passed 7/7, and Django check reported no issues. The required combined command ran 42 tests with 35 passing and 7 unrelated failures before the JSON-LD regression was added; a clean detached `HEAD` reproduced the underlying admin/template/CSS/image-signal failures independently of this change.

### Task 3: Ship and verify the code slice

**Files:**
- Modify: `docs/superpowers/plans/2026-07-16-f028-pdp-variant-locale.md`

- [x] **Step 1: Review the exact diff and rerun focused verification**

Confirm only the plan, regression test, helper, request callers, and structured-data language propagation changed. Re-run the Task 2 commands immediately before committing.

- [x] **Step 2: Commit, push, deploy, and restart Passenger**

Commit the plan/test/code slice on the explicitly authorized `main`, push `origin/main`, pull with `--ff-only` on production, run the focused test module with `--settings=test_settings`, and `touch tmp/restart.txt` because Python code changed.

- [x] **Step 3: Verify production behavior**

Verify server HEAD equals local/origin. Crawl the audited 13 product slugs across UK/RU/EN and require HTTP 200 plus locale-consistent title/H1. For RU/EN hoodie/long-sleeve samples, inspect `variant-data` and Product JSON-LD to ensure Ukrainian variant name/SEO text no longer leaks. Verify `/healthz/` returns 200.

Production evidence (2026-07-16): code commit `da910c46`; `origin/main` and server
HEAD are `da910c469fd91b8b5bb3535890e74ad9acf384b4`. Local and server focused
modules passed **16/16**, integer product API route contracts passed **7/7**, and
Django check was clean. Passenger was restarted and `/healthz/` returned HTTP 200.
The live crawl passed **13 SKU × 3 locales = 39/39** for HTTP, title base, H1,
variant data and Product JSON-LD. RU/EN localized variants API responses were 200
and correct; quick-view/images endpoints were 200. Representative four-layer
values: RU `death-grabs-ass-hd` = `Худи «Сердце И Деньги»`, EN =
`Hoodie «death grabs ass»`; RU `last-breath-ls` =
`Лонгслив «Череп С Розой»`, EN = `Longsleeve «Skull and Rose»`. No migration or
production data mutation was required.

### Task 4: Reconcile the audit status

**Files:**
- Modify: `docs/qa/AUDIT_FINDINGS_2026-07-09.md`
- Modify: `docs/qa/PRE_ADS_MASTER_AUDIT_CHECKLIST.md`
- Modify: `TWOCOMMS_A_TO_B/technical/audit_report_section4_seo.md`
- Modify: `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`
- Modify: `docs/superpowers/plans/2026-07-16-f028-pdp-variant-locale.md`

- [x] **Step 1: Record exact test, deploy, server, DB, and live evidence**

Document the code commit, server test counts, server HEAD, live crawl count, and representative RU/EN H1/variant JSON/JSON-LD values. State explicitly that no data migration was required.

- [x] **Step 2: Mark the finding according to its real remaining scope**

Mark the locale-propagation defect fixed. Keep F-028 as `[o] PARTIAL` while the separate commercial naming-policy decision is unapproved; use `[x] FIXED/DONE` only if the audit evidence proves no owner decision remains.

- [x] **Step 3: Commit, push, deploy, and verify documentation**

Commit only the audit-plan/document changes, push `main`, deploy with `git pull --ff-only`, and verify all current F-028 rows on the server agree on the same status and evidence.

Documentation evidence (2026-07-16): `ab583207` was pushed to `origin/main`
and fast-forwarded on production. Server HEAD
`ab58320714bf11544ab9d436d90aad280b7556fc` shows all four canonical F-028
rows as `[o] PARTIAL`, the P2 summary as `1 PARTIAL / 8 OPEN`, and the PRE_ADS
SEO-049/GEO-006 rows linked to the section 4 evidence report.
