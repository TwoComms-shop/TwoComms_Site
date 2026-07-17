# Language Suggestion Prompt Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an accessible delayed prompt that suggests Ukrainian to Russian/English visitors without changing server-rendered SEO or locale routing.

**Architecture:** Mount an inert dialog shell from the shared Django base template, load a small deferred JavaScript controller, and keep all state client-side in localStorage. Reuse Django's existing `set_language` endpoint and footer CSRF bootstrap behavior.

**Tech Stack:** Django templates, vanilla ES5-compatible JavaScript, CSS, Django test client.

---

### Task 1: Add dialog markup and styles

**Files:**
- Modify: `twocomms/twocomms_django_theme/templates/base.html`
- Create: `twocomms/twocomms_django_theme/static/css/language-suggestion.css`

**Steps:**
1. Add a hidden, empty-by-default dialog mount near the end of `<body>` with backdrop, title, description, current-language and Ukrainian action buttons, and a close icon button.
2. Add responsive CSS, focus-visible states, safe-area insets, reduced-motion rules, and a restrained TwoComms visual treatment.
3. Keep all user-facing strings in `data-*` attributes so the deferred script can choose the active locale without affecting initial SEO HTML.

### Task 2: Implement delayed controller

**Files:**
- Create: `twocomms/twocomms_django_theme/static/js/language-suggestion.js`
- Modify: `twocomms/twocomms_django_theme/templates/base.html`

**Steps:**
1. Detect `uk`, `ru`, or `en` from `html[lang]`; return early for Ukrainian, bots, unsupported languages, prior decision, or storage failure.
2. Schedule a 7-second idle/visible check and reveal the dialog with focus management.
3. Localize the title, body, stay button, and Ukrainian button for `uk`, `ru`, and `en`.
4. Build a same-origin `set_language` form using the current URL as `next`; reuse the CSRF-cookie/bootstrap fallback before submitting.
5. Persist `accepted`, `dismissed`, and timestamp values for a 180-day cooldown; restore focus and lock body scroll while open.

### Task 3: Add focused tests

**Files:**
- Create: `twocomms/storefront/tests/test_language_suggestion.py`

**Steps:**
1. Add template smoke tests proving the mount and deferred assets exist on home responses while canonical/hreflang tags remain present.
2. Add static-source assertions covering supported locale labels, 7-second delay, storage key, bot guard, and `next` URL handling.
3. Run the focused test file and Django system checks.

### Task 4: Verify, publish, and deploy

**Files:**
- No additional source files.

**Steps:**
1. Run focused tests, Django checks, and a syntax check for the new JavaScript.
2. Review the diff and ensure only the design/feature files are staged; preserve unrelated dirty files.
3. Commit with `feat(i18n): add delayed language suggestion prompt` and push `main`.
4. SSH to production, pull `main`, collect static files if required by the project deploy script, restart the app, and smoke-test `/`, `/ru/`, and `/en/`.
