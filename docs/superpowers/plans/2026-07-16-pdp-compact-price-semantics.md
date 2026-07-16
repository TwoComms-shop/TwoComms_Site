# PDP Compact Price Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PDP material and fit pricing truthful while removing selector overlap, wasted space, and the redundant hoodie lining choice.

**Architecture:** Extend `variant_public_context()` with a structured price breakdown derived from existing ownership levels. Render material price and fixed lining state in the material story, render only option-owned deltas in option cards, and use an explicit responsive two-zone selector grid. Preserve exact combination deltas as total overrides.

**Tech Stack:** Django 5 templates and tests, Python pricing services, vanilla JavaScript, CSS Grid, Node test runner, MySQL production verification.

---

### Task 1: Structured additive price breakdown

**Files:**
- Modify: `twocomms/fable5/services.py`
- Test: `twocomms/fable5/tests/test_generic_options.py`

- [ ] **Step 1: Write failing pricing tests**

Add tests asserting that a color/material delta of `400` plus a `fit=oversize` profile of `200` produces `price_delta=600`, `material_delta=400`, `option_delta=200`, and `final_price=base+600`. Add a second test proving an explicit combination delta of `290` remains the final total override.

- [ ] **Step 2: Run RED**

Run on the isolated server worktree:

```bash
python manage.py test fable5.tests.test_generic_options --settings=test_settings --noinput
```

Expected: the additive breakdown test fails because the current sparse resolver returns only one delta.

- [ ] **Step 3: Implement the breakdown**

Add a focused helper returning:

```python
{
    "material_delta": Decimal("400"),
    "option_delta": Decimal("200"),
    "combination_override": None,
    "total_delta": Decimal("600"),
    "option_components": {"fit=oversize": Decimal("200")},
}
```

Use active single-axis `ProductOptionProfile` rows for additive components. If the active exact `VariantCombinationProfile.price_delta` is not `None`, use it as `total_delta` and expose it as `combination_override`. `variant_public_context()` must use this total for final price and expose the breakdown.

- [ ] **Step 4: Run GREEN**

Run the same test module and expect all tests to pass.

### Task 2: Public payload and fixed-axis contract

**Files:**
- Modify: `twocomms/fable5/services.py`
- Modify: `twocomms/storefront/views/product.py`
- Test: `twocomms/storefront/tests/test_pdp_configurator.py`

- [ ] **Step 1: Write failing payload tests**

Assert that each option choice exposes `option_price_delta` without inheriting `VariantDetails.price_delta`. Assert that a lining axis with exactly one enabled choice exposes `is_fixed=True` and the selected fixed choice. Assert serialized configurations include `price_breakdown`.

- [ ] **Step 2: Run RED**

```bash
python manage.py test storefront.tests.test_pdp_configurator --settings=test_settings --noinput
```

Expected: missing keys fail the assertions.

- [ ] **Step 3: Implement payload fields**

Add `option_price_delta`, `is_fixed`, and `fixed_choice` to option context. Add `price_breakdown` to base variants, fit merchandising, and exact configuration rows in `storefront/views/product.py`.

- [ ] **Step 4: Run GREEN**

Run both configurator and generic option test modules and expect all tests to pass.

### Task 3: Semantic compact markup

**Files:**
- Modify: `twocomms/twocomms_django_theme/templates/pages/product_detail.html`
- Test: `twocomms/storefront/tests/test_pdp_configurator.py`

- [ ] **Step 1: Write failing render tests**

Assert that the thermochromic material story contains one material price badge and fit cards do not contain the `400` material delta. Assert a fixed lining axis renders one locked switch with `aria-checked="true"` and `aria-disabled="true"`, while keeping hidden configurator inputs.

- [ ] **Step 2: Run RED**

Run the configurator test module and expect missing markup assertions to fail.

- [ ] **Step 3: Implement markup**

Render `data-pdp-material-price` inside the existing material story. For fixed axes, render hidden radio inputs and the single locked lining switch inside the story; do not render a separate option block. For normal option cards, render option-owned price in a trailing cell and unavailable reason in normal document flow.

- [ ] **Step 4: Run GREEN**

Run the configurator test module and expect all tests to pass.

### Task 3A: Per-product fleece presentation setting

**Files:**
- Modify: `twocomms/fable5/models.py`
- Create: `twocomms/fable5/migrations/0007_product_option_axis_presentation.py`
- Modify: `twocomms/fable5/views.py`
- Modify: `twocomms/fable5/static/fable5/editor.js`
- Modify: `twocomms/fable5/static/fable5/editor.css`
- Test: `twocomms/fable5/tests/test_editor_generic_options.py`
- Test: `twocomms/fable5/tests/test_generic_options.py`

- [ ] **Step 1: Write failing model, save, and context tests**

Assert that the default `auto` presentation fixes a single enabled lining choice, `cards` retains the card layout, editor bootstrap exposes the selected mode, and save persists a changed mode without altering option profiles.

- [ ] **Step 2: Run RED**

Run the generic option and editor option test modules and expect missing model/payload assertions to fail.

- [ ] **Step 3: Implement setting and editor control**

Add an internal Fable5 model keyed by `(product, axis_code)` with `db_constraint=False` on Product and modes `auto`, `switch`, and `cards`. Add a segmented `Компактний switch / Картки` control for the lining axis, serialize it in bootstrap/save payloads, and resolve public fixed state from this preference with safe multi-choice fallback.

- [ ] **Step 4: Run GREEN**

Run both modules plus `makemigrations --check --dry-run`; expect all tests to pass and no pending migrations.

### Task 4: Dynamic updates and compact layout

**Files:**
- Modify: `twocomms/twocomms_django_theme/static/js/product-detail.js`
- Modify: `twocomms/twocomms_django_theme/static/js/product-detail.test.js`
- Modify: `twocomms/twocomms_django_theme/static/css/product-detail.css`
- Modify: `twocomms/twocomms_django_theme/templates/pages/product_detail.html`
- Test: `twocomms/storefront/tests/test_pdp_content_order.py`

- [ ] **Step 1: Write failing JS/CSS contract tests**

Add a pure `resolvePriceBreakdown()` test covering additive components and exact override. Add render/static assertions for two-zone grid, normal-flow status text, and fixed lining switch dimensions.

- [ ] **Step 2: Run RED**

```bash
node --test twocomms/twocomms_django_theme/static/js/product-detail.test.js
python manage.py test storefront.tests.test_pdp_content_order --settings=test_settings --noinput
```

Expected: helper and CSS contract assertions fail.

- [ ] **Step 3: Implement JS and CSS**

Update material price from `variant.price_breakdown.material_delta`. Update option card prices from `choice.option_price_delta` only. Use explicit color/fit grid columns on desktop, a stable card grid without absolute price/status overlap, two fit columns when space permits, and a one-column fallback at the existing mobile breakpoint. Bump one shared PDP asset release key.

- [ ] **Step 4: Run GREEN**

Run Node tests plus PDP render/content tests and expect all tests to pass.

### Task 5: Regression, deploy, and live QA

**Files:**
- Verify only; no additional source files expected.

- [ ] **Step 1: Run regression suite**

Run Fable5 generic option/content tests, storefront configurator/cart/checkout pricing tests, Node PDP tests, `manage.py check`, `makemigrations --check --dry-run`, and `git diff --check`.

- [ ] **Step 2: Commit and push**

Commit the implementation as a narrow PDP change, rebase on current `origin/main`, rerun fast tests, and push `HEAD:main`.

- [ ] **Step 3: Deploy**

On production: fast-forward pull, run migrations, check, collectstatic, compress, and Passenger restart. Do not change product data.

- [ ] **Step 4: Production data verification**

Confirm the thermochromic variant still has material delta `400`, no fit profile, stable final prices across fits, and a structured payload that attributes `400` only to material.

- [ ] **Step 5: Browser QA**

At desktop and mobile widths verify the thermochromic T-shirt and a hoodie: no overlap, no horizontal overflow, compact color/fit alignment, fixed fleece switch, correct disabled reason wrapping, correct price changes, gallery swipe/arrows, and no relevant console errors.
