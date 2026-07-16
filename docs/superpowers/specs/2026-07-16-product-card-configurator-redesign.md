# Product Card Configurator Redesign

**Date:** 2026-07-16
**Status:** Approved by delegated design authority
**Scope:** TwoComms public product detail page, Fable5 editor, generic option pricing, restock requests, print links, and mobile gallery gestures.

## 1. Context and constraints

The production MySQL database is the source of truth. The local SQLite database is suitable only for fixture-based tests. Production currently contains 76 products, 25 hoodies, 32 products with fit options, 21 warehouse prints, and 108 existing print-to-product links. The current Fable5 data model already supports product option profiles and exact color-by-option combinations, but the public PDP and editor expose mostly fit-specific behavior.

The work must preserve canonical product URLs, current color variants, cart/checkout price parity, legacy admin discoverability, existing warehouse print links, multilingual SEO, and the dirty local worktree. It must finish with tests, responsive visual QA, a scoped commit/push, production migrations, static rebuild, Passenger restart, and live verification.

## 2. Considered approaches

### A. CSS-only PDP patch

This would repair the flame position, selector spacing, block order, and mobile swipe quickly. It would not solve generic option pricing, hoodie lining, unavailable choices, stock requests, or editor/storage synchronization. It would also leave different price calculations in the PDP, cart, checkout, and payment paths. Rejected because the result would look better while remaining structurally incomplete.

### B. Complete the existing Fable5 option engine

Use `GarmentFlow.axes`, `ProductOptionProfile`, and `VariantCombinationProfile` as the shared option model. Expose the same normalized selection to the editor, PDP, cart, checkout, orders, payment, and feeds. Add a focused restock subscription subsystem and use the existing warehouse `Print.default_products` M2M as the print source of truth. This is the selected approach because it extends the architecture already deployed without replacing stable product/color data.

### C. New SKU/inventory matrix

Create a separate SKU per product/color/fit/lining/size combination and migrate all stock and checkout code to it. This is a stronger long-term inventory design, but it is too disruptive for the requested PDP upgrade and risks breaking legacy MyISAM-backed product/color tables. Rejected for this delivery.

## 3. Visual system

The buy box remains dark and compact, but the configuration area becomes one coherent vertical flow rather than an uneven color/fit split. The visual hierarchy is:

1. Product identity, actions, factual trust chips.
2. Price and bonus points.
3. One contextual material story, rendered only when the selected configuration has meaningful material content.
4. Size selection.
5. Color selection.
6. Generic option groups such as fit or lining.
7. Details tabs and purchase controls.

The generic `Premium fabric` chip is removed when it merely repeats the contextual material story. Stable factual chips such as `Made in UA` and the Armed Forces contribution remain. The contextual story changes title, icon, copy, and restrained accent based on the selected material/option: thermochromic, fleece, cotton, or a manually entered merchandising feature. It appears once.

Color controls use a stable 48px outer ring and a 36px inner swatch. A thermochromic flame is placed inside a small circular badge in the swatch's top-right quadrant, never outside the swatch bounding box. Labels remain under the swatches, with horizontal scrolling when necessary.

Each option group is full width. Choices use an equal-height responsive grid: two columns where space permits and one column on narrow phones. A choice includes a production SVG icon, label, short description, optional `+N грн` price badge, and a circular selected indicator. Disabled choices stay visible, are not selectable, use reduced contrast, and show a concise reason. The grid uses `repeat(auto-fit, minmax(...))`, fixed minimum heights, and no viewport-scaled typography.

## 4. Generic option and price architecture

`GarmentFlow` determines option axes by category. T-shirts use `fit`; hoodies use `lining`; future categories can add axes without new PDP markup. Every selection is normalized to a sorted dictionary such as `{"fit": "oversize"}` or `{"lining": "fleece"}`.

`ProductOptionProfile` stores product-wide availability, surcharge, reason, video, and localized merchandising for one option key. `VariantCombinationProfile` remains the exact color-by-options override. Resolution order is exact combination, product option, color details, then product defaults. The existing `variant_public_context()` accepts normalized option values while retaining `fit_code` compatibility.

The selected option dictionary and final resolved price travel together through PDP data, analytics, cart session data, checkout, persisted order items, Telegram order messages, email receipts, and Monobank invoice creation. Legacy `fit_option_code` and `fit_option_label` fields continue to be populated for fit selections; a new JSON field stores the generic option selection. No consumer may recalculate a surcharge independently.

All hoodies receive two lining choices through the existing hoodie flow. `fleece` is enabled and selected by default. `no_fleece` is rendered but disabled. A data migration creates missing product option profiles without overwriting operator-edited rows.

## 5. Size availability and restock requests

All known sizes remain visible. Available sizes act as radio controls. Unavailable sizes are visually crossed, cannot be selected for purchase, and expose a separate bell action with an accessible label such as `Notify me when M is available`. When every size is unavailable, the selector shows a stronger inline empty state and one primary `Notify when available` action.

The restock modal is a focused four-channel form:

- Telegram: recommended. The site creates a restock draft and starts the existing bot deep-link/contact-sharing verification flow with purpose `restock`. Webhook completion stores Telegram ID, username, and verified phone on the same request. The verified Telegram identity remains attached to future requests for an authenticated profile and can be reused with consent.
- Phone call: name and normalized phone number.
- Email: name and validated email address.
- WhatsApp: name and normalized phone number.

The modal summarizes product, color, selected options, and desired size so the user never re-enters product data. It supports keyboard focus trapping, Escape close, outside-click close, reduced motion, inline validation, loading, success, duplicate, rate-limit, and retry states. Consent copy explains that the contact is used only for this availability request.

`RestockSubscription` stores the immutable requested configuration, contact channel, normalized contact, Telegram identity, status, request metadata, notification timestamps, and deduplication fingerprint. Submission is CSRF protected, honeypot protected, rate limited, and idempotent for the same active request.

The sales/admin Telegram chat receives a structured alert immediately. Telegram subscriptions can be notified automatically when a matching size becomes available; email uses the configured Django mail backend. Phone and WhatsApp requests remain actionable staff leads because no outbound telephony/WhatsApp provider is currently configured. The model and admin expose delivery state and allow manual completion.

## 6. Print synchronization

`warehouse.Print.default_products` is the canonical many-to-many relation because production already contains 108 links and the warehouse editor writes this relation. The Fable5 product editor receives the print dictionary and selected print IDs, renders a searchable multi-select with thumbnail/name/category, and updates the M2M relation transactionally during product save.

The warehouse print editor sees the same relation automatically through Django's reverse M2M. The unused one-to-one `ProductPrintLink` is not promoted to a second source of truth. This avoids destructive migration and supports multiple prints per product immediately.

## 7. Content order and width

The product page order after the buy box is:

1. Product reviews.
2. Similar products.
3. Recently viewed products.
4. General product SEO block and SEO landing content.

Both SEO blocks move inside the same PDP shell and use the same maximum width as reviews and recommendation rails. They remain server-rendered and preserve headings, FAQ schema, and canonical behavior. The duplicated care-instruction output in the details tab is removed.

## 8. Mobile gallery gesture

The gallery owns a current image index shared by the hero image, thumbnails, dots, and color changes. On touch devices, a horizontal swipe exceeding 42px and dominating vertical movement advances to the previous or next image. The gesture uses pointer events, pointer capture, `touch-action: pan-y`, edge resistance, and a short snap animation. Vertical page scrolling is never prevented until horizontal intent is clear.

Swiping updates the active thumbnail, accessible status text, and compact position dots. It does not open zoom. Tapping without a swipe keeps the existing zoom behavior. Changing color replaces the image set and resets the index safely. Reduced-motion users get an immediate image change.

## 9. Error handling and compatibility

Every new model lookup has neutral fallbacks so products without Fable5 data still render and remain purchasable. Disabled option combinations cannot be added to cart even if a request is forged. The server recalculates the final price from product, color, and normalized options; client-supplied prices are never trusted.

Restock notification failures do not lose the request. They set an error state for retry and are visible in admin. Telegram webhook handling verifies session ownership/token state and accepts only contact data belonging to the Telegram sender. Public error responses do not expose credentials, chat IDs, or internal exception text.

## 10. Testing and acceptance criteria

Implementation follows red-green-refactor. Required automated coverage includes:

- Generic option normalization, availability, inheritance, and surcharge resolution.
- Hoodie fleece defaults and disabled no-fleece migration behavior.
- PDP rendering of enabled and disabled choices, price badges, material story deduplication, and unavailable sizes.
- Server-side rejection of disabled combinations and price parity across cart, checkout, order, email, Telegram, and Monobank.
- Restock validation, deduplication, throttling, channel-specific fields, Telegram session/webhook completion, and notification failure retention.
- Bidirectional print M2M editing without deleting existing links.
- Reviews/recommendations/recent/SEO order and full-width shell placement.
- Gallery swipe direction, threshold, vertical-scroll protection, thumbnail/dot synchronization, color reset, and tap-to-zoom preservation.

Rendered QA uses the production-like product `futbolka-boiova-kvitochka` plus a hoodie. It covers desktop, tablet, and phone widths; keyboard navigation; no-size state; multiple fits; disabled lining; thermochromic swatch alignment; modal channels; and swipe interaction. Final production verification checks migrations, representative runtime values, live HTTP responses, asset versions, console health, and screenshots against the supplied references.

## 11. Delivery sequence

1. Add regression tests and generic option/restock data contracts.
2. Implement server resolution and persistence.
3. Complete Fable5 editor axes, price controls, and print multi-select.
4. Redesign the PDP configurator, restock modal, content order, and swipe gallery.
5. Run targeted and broad tests, Django checks, migration checks, and responsive browser QA.
6. Commit scoped files, push the current main branch, deploy with migrations/static compression/restart, and live-verify real MySQL-backed products.
