# T-shirt Size Advisor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship exact classic T-shirt measurements, compact guide navigation, and a localized height/weight size advisor on every eligible T-shirt product page.

**Architecture:** Extend the existing Fable5 fit-specific guide payload and PDP tab system. Keep canonical guide data separate from product sellability, calculate recommendations in a pure tested JavaScript function using the live availability matrix, and expose machine-readable tool metadata from the server-rendered template.

**Tech Stack:** Django, Fable5 `SizeGrid`, Django templates/i18n, vanilla JavaScript, Node test runner, CSS, Pillow, Playwright.

---

### Task 1: Canonical classic guide and production-safe backfill

**Files:**
- Modify: `twocomms/fable5/default_size_guides.py`
- Modify: `twocomms/fable5/size_grid_services.py`
- Create: `twocomms/fable5/management/commands/ensure_tshirt_size_guides.py`
- Modify: `twocomms/fable5/tests/test_size_grid_resolution.py`
- Create: `twocomms/twocomms_django_theme/static/img/size-guides/classic-tshirt.webp`

1. Add failing tests for all FS-101 rows, `S-3XL` guide display, classic image metadata, preservation of explicit overrides, and idempotent production updates.
2. Run the focused Django tests and confirm the new assertions fail for missing classic data/command.
3. Add canonical classic data, static fallback metadata, and the narrow idempotent command that updates only T-shirt catalogs and canonical classic grids.
4. Generate one optimized WebP from the uploaded screenshot and verify its dimensions and readability.
5. Run focused tests until green.

### Task 2: Pure size recommendation model

**Files:**
- Modify: `twocomms/twocomms_django_theme/static/js/product-detail.test.js`
- Modify: `twocomms/twocomms_django_theme/static/js/product-detail.js`

1. Add failing tests for validation boundaries, classic/oversize recommendations, tall/light and short/heavy edge cases, unavailable sizes, 2XL ceiling, and adjacent alternatives.
2. Confirm RED with the Node test runner.
3. Implement pure `recommendTshirtSize` and helper normalization functions without DOM dependencies.
4. Confirm GREEN and keep the output contract localized through template-provided strings.

### Task 3: Compact three-mode PDP interface

**Files:**
- Modify: `twocomms/fable5/templates/fable5/_size_grid_comparison.html`
- Modify: `twocomms/twocomms_django_theme/templates/pages/product_detail.html`
- Modify: `twocomms/twocomms_django_theme/static/js/product-detail.js`
- Modify: `twocomms/twocomms_django_theme/static/css/product-detail.css`
- Modify: `twocomms/fable5/tests/test_size_grid_resolution.py`

1. Add failing template/source tests for two thin size-header actions, absence of the large compare block, three mutually exclusive modes, labeled numeric inputs, live result, and focus/scroll targets.
2. Confirm RED in Django and Node.
3. Implement the accessible mode switch and advisor form using existing PDP tokens and compact typography.
4. Wire triggers, mode activation, validation, result rendering, fit synchronization only when explicitly selected in the advisor, and reduced-motion-aware scrolling.
5. Confirm focused tests GREEN.

### Task 4: Localization and SEO/GEO/AI semantics

**Files:**
- Modify: `twocomms/locale/ru/LC_MESSAGES/django.po`
- Modify: `twocomms/locale/en/LC_MESSAGES/django.po`
- Regenerate: `twocomms/locale/ru/LC_MESSAGES/django.mo`
- Regenerate: `twocomms/locale/en/LC_MESSAGES/django.mo`
- Modify: `twocomms/twocomms_django_theme/templates/pages/product_detail.html`
- Modify: `twocomms/fable5/tests/test_size_grid_resolution.py`

1. Add failing tests that render Ukrainian, Russian, and English UI plus valid `WebApplication`/`HowTo` JSON-LD.
2. Add concise translations and server-rendered JSON-LD with no dynamic-result claims.
3. Compile message catalogs and rerun multilingual tests.

### Task 5: Integration, visual verification, and deployment

1. Run targeted Django and Node suites, `manage.py check`, migration dry-run, translation checks, and `git diff --check`.
2. Start a local server and verify the real rendered page using a production-like fixture or authenticated production browser checks at phone, tablet, and desktop widths.
3. Review the exact diff and stage only task files.
4. Commit, push the feature commit to `origin/main`, and verify the remote SHA.
5. On production run fast-forward pull, the T-shirt guide command first in dry-run then write mode, `collectstatic`, `compress --force`, `check`, and Passenger restart.
6. Verify MariaDB guide rows/image assignments and live UA/RU/EN pages, calculator behavior, JSON-LD, responsive layout, and deployed SHA.
