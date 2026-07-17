# Custom Print Modernization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore the established eight-stage Custom Print journey and modernize its cinematic desktop, tablet, and mobile experience without changing cart, lead, moderation, pricing, or SEO contracts.

**Architecture:** Keep the Django template and existing configurator state engine as the contract boundary. Reintroduce the eight UI stages in the template and navigation mapping, then replace the flat guided stylesheet with a cohesive responsive studio layer. Keep the PNG preview, mobile shell, draft, and submit modules as focused helpers.

**Tech Stack:** Django templates/i18n, vanilla JavaScript, CSS, Node test runner, Django TestCase, Playwright/browser screenshots.

---

### Task 1: Lock The Eight-Stage Contract

**Files:**
- Modify: `tests/test_custom_print_guided_studio_source.py`
- Modify: `tests/custom-print-preview.test.cjs`

**Steps:**
1. Add failing source-contract tests for eight named studio stages, mobile `Крок 1 з 8`, one hero CTA, and absence of direct hero/stage Telegram links.
2. Add a failing JavaScript contract for eight-stage navigation and preview overlay ownership.
3. Run `python manage.py test tests.test_custom_print_guided_studio_source -v 2` and `node --test tests/custom-print-preview.test.cjs`; confirm the new assertions fail.
4. Commit the contract tests with `test: define modern custom print journey`.

### Task 2: Restore The Established Selection Flow

**Files:**
- Modify: `twocomms/twocomms_django_theme/templates/pages/custom_print.html`
- Modify: `twocomms/twocomms_django_theme/static/js/custom-print-configurator.js`
- Modify: `twocomms/twocomms_django_theme/static/js/custom-print-state.js`
- Modify: `twocomms/twocomms_django_theme/static/js/custom-print-mobile-shell.js`

**Steps:**
1. Change the stage map to `format`, `garment`, `config`, `placement`, `artwork`, `quantity`, `gift`, `contact`.
2. Give configuration and gift their own studio wrappers and progress entries.
3. Preserve the existing underlying step IDs and form field names so saved drafts and payloads remain compatible.
4. Update next/back navigation, completed summaries, resume mapping, and mobile progress to eight stages.
5. Run the targeted Django and Node tests until green.
6. Commit with `feat: restore custom print selection flow`.

### Task 3: Rebuild The Cinematic Hero

**Files:**
- Modify: `twocomms/twocomms_django_theme/templates/pages/custom_print.html`
- Modify: `twocomms/twocomms_django_theme/static/css/custom-print-guided-studio.css`

**Steps:**
1. Keep one primary start action and the existing garment-stage image.
2. Restore restrained gold/violet lighting, stage ring, garment labels, and calibrated zone frames.
3. Remove palette, handwritten signature, Telegram CTA, and feature-card rail.
4. Add stable desktop, tablet, and mobile geometry with container queries/media queries and no viewport-scaled typography.
5. Add reduced-motion rules for all entrance and ambient motion.
6. Run source tests and `git diff --check`.
7. Commit with `feat: restore cinematic custom print hero`.

### Task 4: Modernize Desktop And Tablet Studio

**Files:**
- Modify: `twocomms/twocomms_django_theme/static/css/custom-print-guided-studio.css`
- Modify: `twocomms/twocomms_django_theme/templates/pages/custom_print.html`

**Steps:**
1. Replace repeated flat panels with one studio surface and clear internal hierarchy.
2. Implement the 42/58 desktop split and sticky PNG preview.
3. Restyle product, mode, fit, fabric, artwork, zone, format, size, and gift selectors with distinctive but consistent visual controls.
4. Keep supporting copy compact and move secondary explanations to info hints.
5. Add a tablet layout where preview opens as an overlay instead of occupying the first screen.
6. Run source tests and static syntax checks.
7. Commit with `feat: modernize custom print studio`.

### Task 5: Finish The Mobile App Experience

**Files:**
- Modify: `twocomms/twocomms_django_theme/static/css/custom-print-guided-studio.css`
- Modify: `twocomms/twocomms_django_theme/static/js/custom-print-mobile-shell.js`
- Modify: `twocomms/twocomms_django_theme/static/js/custom-print-configurator.js`

**Steps:**
1. Show one active stage and keep app progress sticky at the top.
2. Portal the bottom price/action bar to `document.body` and account for safe-area insets.
3. Ensure the bar label and action update for all eight stages.
4. Keep manager and preview actions available without duplicating Telegram links.
5. Add focus/scroll behavior for invalid controls and prevent content from sitting behind fixed UI.
6. Run targeted tests.
7. Commit with `feat: complete mobile custom print app shell`.

### Task 6: Refine PNG Preview And Dialogs

**Files:**
- Modify: `twocomms/twocomms_django_theme/static/js/custom-print-preview.js`
- Modify: `twocomms/twocomms_django_theme/static/js/custom-print-submit-flow.js`
- Modify: `twocomms/twocomms_django_theme/static/css/custom-print-guided-studio.css`
- Modify: `twocomms/twocomms_django_theme/templates/pages/custom_print.html`

**Steps:**
1. Render full-screen mobile/tablet preview in `document.body` with front/back segmented control and exact dimensions.
2. Keep customer artwork out of preview and show placement outlines only.
3. Add crossfade, desktop pointer tilt, and reduced-motion fallback.
4. Preserve focus trap, Escape, restoration, and duplicate-submit safety in preview, manager, and moderation dialogs.
5. Run Node and source tests.
6. Commit with `feat: refine custom print preview and handoff`.

### Task 7: Repair Content And Localization

**Files:**
- Modify: `twocomms/twocomms_django_theme/templates/pages/custom_print.html`
- Modify: `twocomms/locale/uk/LC_MESSAGES/django.po`
- Modify: `twocomms/locale/ru/LC_MESSAGES/django.po`
- Modify: `twocomms/locale/en/LC_MESSAGES/django.po`
- Regenerate: corresponding `.mo` files

**Steps:**
1. Search Custom Print source and locale entries for replacement characters and broken apostrophes.
2. Correct only damaged strings and new interface labels.
3. Preserve SEO metadata, H1, canonical, schema, FAQ meaning, and URL.
4. Compile messages and run localization source tests.
5. Commit with `fix: repair custom print localization`.

### Task 8: Verification And Delivery

**Files:**
- Modify only if verification exposes a scoped defect.

**Steps:**
1. Run targeted Django and Node tests, `node --check`, `python manage.py check`, and `git diff --check`.
2. Start the local Django server and capture desktop/tablet/mobile screenshots for all required viewports.
3. Check horizontal overflow, fixed overlays, focus behavior, active-step navigation, PNG rendering, colors, and reduced motion.
4. Review the full diff and stage only Custom Print files, assets, tests, and plan docs.
5. Commit final verification fixes, push the feature branch, fast-forward `main`, and push `origin/main`.
6. Deploy with `git pull --ff-only`, `collectstatic`, `compress --force`, Passenger restart, and live UA/RU/EN smoke checks.

