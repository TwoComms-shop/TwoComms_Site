# Custom Print Audit Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the restored Custom Print studio visually balanced and functionally dependable across all requested device sizes without changing server payload contracts or SEO meaning.

**Architecture:** Keep the existing Django configuration, eight-stage state machine, PNG preview and submit modules. Move mobile navigation to a stable app shell, add a single offset-aware scroll boundary, make generated selectors data-driven and symmetric, and use a shared completion/action policy for desktop and mobile.

**Tech Stack:** Django/Python configuration, vanilla JavaScript, CSS, Django i18n, Node test runner, Django source/contract tests, `npx agent-browser` screenshots.

---

### Batch 1: Lock data and rendering contracts

1. Add failing config tests for T-shirt black/white/coyote, no graphite, thermo green/pink, hoodie regular/oversize palettes, and exact ISO label text.
2. Add failing source tests for stable app bar ownership, no nested fabric button, two-column mobile fit grid, mobile font size, and visible final completion checklist.
3. Add failing Node tests for offset-aware step navigation and shared final action policy.
4. Update `custom_print_config.py` with `fit_colors`, T-shirt coyote and explicit auto-included hoodie material metadata.
5. Run the targeted tests and confirm only the new assertions fail before implementation.

### Batch 2: Fix product configuration visual system

1. Update `renderColorChips()` to resolve `fit_colors` before fabric-specific thermo palettes.
2. Replace the nested fabric `?` trigger with an accessible info button/icon and keyboard activation.
3. Render fit cards as compact equal-height two-column cards with fixed thumbnail geometry.
4. Render fabrics as equal grid tracks with locked heights and compact price/meta rows.
5. Render fleece as a binary segmented control with `aria-pressed`, a default state and explicit on/off labels.
6. Add CSS for fit, fabric, info, fleece, zone and format controls with one selected-state language.
7. Verify screenshots at `320`, `390`, `768` and `1440` before continuing.

### Batch 3: Repair mobile navigation and boundaries

1. Add a single `scrollToStudioTarget()` helper that calculates the app bar, safe-area and bottom-bar offsets.
2. Replace raw transition/validation `scrollIntoView()` calls with the helper.
3. Keep mobile app bar in a stable portal; stop moving `data-progress-shell` into active step wrappers.
4. Add a studio boundary observer/sentinel that releases the app shell before SEO and prevents empty tail scrolling from looking like an unfinished step.
5. Add `scroll-margin-top` and bottom padding contracts for active steps, error fields and dialogs.
6. Verify each transition from format through contact at `390x844` and `430x932`.

### Batch 4: Repair actions, contact and completion feedback

1. Make visible final `Додати в кошик` and `Надіслати менеджеру` invoke the same guarded handlers as the mobile bar.
2. Render a completion checklist with direct edit targets for missing product, config, zones, artwork, quantity/sizes, gift and contact fields.
3. Keep optional gift packaging explicit as a two-state switch and include its price/selection in the receipt.
4. Replace the generic manager icon with a help/person icon and generate a safe Telegram summary from the current state.
5. Add duplicate-submit and focus restoration coverage for all actions.

### Batch 5: iOS, keyboard and visual QA

1. Enforce `16px` mobile form controls and test brief/contact fields on the iPhone-sized viewport.
2. Run keyboard-only navigation through fit, fabric, zones, gift and final actions.
3. Run reduced-motion screenshots and inspect contrast/focus states.
4. Run all targeted tests, Django check, `collectstatic`, compressor and `git diff --check`.
5. Update `findings-2026-07-17.md` checkboxes with evidence and add a final QA note.

### Batch 6: Delivery

1. Stage only custom-print source, configuration, tests, assets and this audit folder.
2. Commit in focused batches, push `main` fast-forward, and verify `HEAD...origin/main` is `0 0`.
3. Deploy with server `git pull --ff-only`, `collectstatic`, `compress --force` and Passenger restart.
4. Live smoke `/custom-print/`, `/ru/custom-print/`, `/en/custom-print/` and hashed preview assets.
