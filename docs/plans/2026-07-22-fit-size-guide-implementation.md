# Fit-Specific Size Guide UX Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship independent classic/oversize size-guide panels with one optimized shared oversize asset, product-specific alt text, responsive image/table UX, and server-side backfill/default behavior.

**Architecture:** Extend the existing Fable5 comparison payload instead of creating a parallel size system. Resolve explicit product/variant assignments first, then a canonical oversize default profile; render the comparison panel as an independent fit switch whose state does not alter the purchase fit. Store the optimized image once in the canonical `SizeGrid` media record and expose localized descriptive copy plus structured image metadata.

**Tech Stack:** Django, existing `SizeGrid`/Fable5 models, Pillow, Django templates, vanilla product-detail JavaScript, product-detail CSS, Django management commands, unittest/JSDOM-style JS tests already present in the repo.

---

### Task 1: Inspect and normalize the uploaded asset

**Files:**
- Create: `twocomms/twocomms_django_theme/static/img/size-guides/oversize-tshirt.webp`
- Create: `twocomms/twocomms_django_theme/static/img/size-guides/oversize-tshirt.avif` when encoder support is available
- Source only: `size_img/Oversize_tshirt.png` (do not commit the raw upload)

**Step 1: Write the failing image metadata check**

Add a small test/helper assertion that the shipped WebP has bounded dimensions, RGB/RGBA color mode, and materially smaller bytes than the source.

**Step 2: Run it to verify it fails**

Run the focused image check and confirm the target files do not yet exist.

**Step 3: Generate optimized variants**

Use Pillow with a high-quality resize/encode strategy that preserves readable table text, keeps the full aspect ratio, strips unnecessary metadata, and emits WebP (and AVIF only if the installed encoder supports it). Keep the asset in the theme static tree so it is deployable and cacheable once.

**Step 4: Run the metadata check to verify it passes**

Inspect dimensions, MIME type, byte size, and a rendered preview.

**Step 5: Commit**

Commit only the optimized static assets and test/helper changes.

### Task 2: Add canonical oversize guide resolution

**Files:**
- Modify: `twocomms/fable5/size_grid_services.py`
- Modify: `twocomms/storefront/services/size_guides.py`
- Create: `twocomms/fable5/management/commands/ensure_oversize_size_guides.py`
- Test: `twocomms/fable5/tests/test_size_grid_resolution.py`

**Step 1: Write failing resolver tests**

Cover: explicit classic and oversize assignments remain distinct; an explicit per-product or per-variant oversize grid wins; a missing oversize assignment resolves through the canonical profile; the fallback payload contains `XS`, `S`, `M`, `L`, `XL`, `XXL`; and the classic path is unchanged.

**Step 2: Run the focused tests to verify failure**

Run the relevant Fable5 test class and capture the missing-fallback failure.

**Step 3: Implement the fallback and payload metadata**

Add a narrow helper that finds the active canonical `SizeGrid` profile for `fit=oversize` within the product catalog, while preserving explicit assignments and variant overrides. Add localized fit copy, image URL/width/height, and an image alt seed to each comparison item. Keep the resolver tolerant when the optional Fable5 app is absent.

**Step 4: Implement the idempotent production command**

The command must locate the active apparel products with `oversize` enabled, create/update one canonical oversize `SizeGrid` per relevant catalog, load the optimized asset once into that row, normalize its structured data to `XS–XXL`, and create only missing `ProductOptionSizeGrid` rows. Never overwrite explicit classic/oversize assignments or variant overrides. Print created/updated/skipped counts and support `--dry-run`.

**Step 5: Run tests to verify pass**

Run focused Fable5 tests plus `manage.py check` and migration dry-run.

**Step 6: Commit**

Commit resolver, command, and tests.

### Task 3: Render independent comparison UX

**Files:**
- Modify: `twocomms/fable5/templates/fable5/_size_grid_comparison.html`
- Modify: `twocomms/twocomms_django_theme/templates/pages/product_detail.html`
- Modify: `twocomms/twocomms_django_theme/static/js/product-detail.js`
- Modify: `twocomms/twocomms_django_theme/static/css/product-detail.css`
- Test: `twocomms/fable5/tests/test_size_grid_resolution.py`
- Test: existing product-detail JS tests or a new focused test module adjacent to `product-detail.js`

**Step 1: Write failing template/behavior tests**

Assert the panel renders a classic/oversize switch independent of the top fit selector, carries `aria-selected`/`aria-controls`, renders an image when the guide has `image_url`, renders a table when it does not, and includes product-specific alt/caption and localized fit explanation.

**Step 2: Run tests to verify failure**

Run the focused Django/template and JS tests.

**Step 3: Implement the template**

Render one accessible panel shell with a segmented guide switch, a live status region, a responsive media block using `<picture>`/`img` with stable dimensions and product-specific alt, and a semantic table/text fallback. Include concise localized classic-vs-oversize copy and measurement semantics in visible text. Avoid duplicating the physical image per product.

**Step 4: Implement independent client state**

Initialize the guide switch from the current fit only as a visual default. Add click/keyboard handlers that show the selected comparison card, update ARIA state and live text, and leave `data-current-fit`, option inputs, cart payload, and offer selection untouched. Update the selected card's table/image/alt when the color variant changes. Do not hide a guide merely because the purchase fit is currently different; hide only genuinely unavailable fit data.

**Step 5: Implement responsive styling**

Use the existing product-detail design tokens. Make the switch compact and keyboard-visible, use a single-column mobile media/table flow, contain the image without cropping, allow intentional horizontal table scrolling, and keep text readable at phone/tablet/desktop widths. Add reduced-motion-safe transitions.

**Step 6: Run tests to verify pass**

Run Django template tests, JS tests, and a local browser smoke check at narrow mobile, tablet, and desktop viewports.

**Step 7: Commit**

Commit template, JS, CSS, and tests.

### Task 4: SEO/GEO/AI content and accessibility verification

**Files:**
- Modify: `twocomms/storefront/services/size_guides.py`
- Modify: `twocomms/fable5/templates/fable5/_size_grid_comparison.html`
- Test: `twocomms/fable5/tests/test_size_grid_resolution.py`

**Step 1: Write failing copy/alt tests**

Assert localized copy names the product and fit, states classic starts at `S` and oversize at `XS`, and image alt differs by product title while remaining stable for the same product/fit.

**Step 2: Implement copy and metadata**

Add safe localized strings and semantic headings/figcaption text. Keep the text concise, factual, and visible to crawlers/answer systems; do not keyword-stuff or rely on the image alone.

**Step 3: Run tests**

Run the focused test suite and HTML escaping checks.

**Step 4: Commit**

Commit copy/accessibility changes if separate from Task 3.

### Task 5: Production data run, deploy, and live verification

**Files:**
- No new source files; use the committed command and static assets.

**Step 1: Verify local scope**

Run `git diff --check`, targeted tests, `manage.py check`, `makemigrations --check --dry-run`, and inspect the staged file list. Confirm unrelated dirty artifacts remain unstaged.

**Step 2: Push the commits**

Push `main` only after a final `git status` check.

**Step 3: Pull and run the command on production**

Use the user-provided SSH workflow. Inside the server venv, run `git pull --ff-only`, `python manage.py ensure_oversize_size_guides --no-input` (first with `--dry-run` if needed, then real), `python manage.py collectstatic --no-input`, `python manage.py compress --force` if template/static bundles require it, and `touch tmp/restart.txt` last.

**Step 4: Verify the real database and live pages**

Query representative products to confirm both assignment keys and no duplicate image rows. After Passenger reload, curl representative `/product/<slug>/classic/` and `/product/<slug>/oversize/` routes, then use browser automation to click both guide tabs from a classic-starting page and verify image/table, alt, ARIA state, and responsive layout.

**Step 5: Report**

Report commit SHAs, server command counts, deployed SHA, restart completion, and live route/browser verification results. Do not include the SSH password in output.

