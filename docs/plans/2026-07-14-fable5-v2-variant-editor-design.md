# Fable 5 v2 Variant Editor Design

## Purpose

Build a production-safe product editor that lets a TwoComms administrator define a product once, then override only the content that differs for a fit, a color, or an exact color × fit combination. The editor must also provide a reusable size-grid library, require the correct grid for every enabled fit, support per-product and per-color size availability, and present all enabled size grids together on the public product page for easy comparison.

The implementation extends the already deployed Fable 5 application. The legacy product editor and all existing public fallbacks remain operational.

## Confirmed constraints

- Production MySQL is the schema source of truth.
- Legacy `storefront` and `productcolors` tables use MyISAM in production.
- New persistent feature state belongs to `fable5` and must be additive.
- Relations from `fable5` to legacy tables use `db_constraint=False`.
- The existing `storefront.SizeGrid` table remains the canonical size-grid asset and structured-data store; no destructive migration is required.
- The editor stays staff-only, CSRF-protected, and usable without a Node build pipeline.
- The old editor remains available as a recovery path.
- Missing Fable 5 data must preserve current storefront behavior.

## Why the supplied Fable plan needs a structural correction

The supplied plan treats `ProductColorVariant` as the complete merchandising variant. That supports separate black and coyote content, but it cannot represent the required difference between black classic and black oversize.

Creating a separate `Product` row for every combination would solve content independence but would create duplicated catalog records, split analytics, complicate cart and order behavior, destabilize feed identifiers, and make daily administration slower.

The selected design keeps one canonical product and resolves presentation data through sparse inheritance.

## Variant hierarchy

The hierarchy is:

1. Product defaults: shared title, descriptions, SEO, price, print, and media.
2. Product option override: content shared by one fit across all colors, such as oversize copy.
3. Color override: content shared by one color across fits, such as black-fabric copy.
4. Exact combination override: content unique to black × oversize.

Each field resolves independently:

```text
exact color × option value
    -> product option value
    -> color variant
    -> base product
```

Empty override values inherit. The editor shows the effective value and its source. An administrator can switch each content block between “inherit” and “custom”; they do not have to duplicate unchanged data.

The exact combination is represented by a stable normalized `combination_key`, for example:

```text
fit=oversize
fit=classic
lining=fleece
fit=oversize;lining=fleece
```

The accompanying `option_values` JSON remains human-readable. The normalized key is used for uniqueness and lookup.

## Data ownership

### Existing models retained

- `storefront.Product`: canonical commercial product.
- `productcolors.ProductColorVariant`: canonical color/SKU layer.
- `storefront.ProductFitOption`: currently active public fit options.
- `storefront.SizeGrid`: reusable structured size grid.
- Existing Fable 5 models remain valid and become fallbacks.

### New Fable 5 models

#### Localized merchandising content

- `VariantDetailsI18n`: localized color-level title, descriptions, SEO, OG, and optional marketing copy.
- `ProductOptionProfile`: one product-level option override such as `fit=oversize`.
- `ProductOptionProfileI18n`: localized content for that option.
- `VariantCombinationProfile`: exact color × normalized option combination, including optional price delta, video, media policy, and activation state.
- `VariantCombinationProfileI18n`: localized exact-combination content.

#### Images

- `VariantImageAltI18n` and `ProductImageAltI18n`: localized alt text.
- `CoverSource`: auditable source of the canonical `Product.main_image`.
- Existing image models remain the physical media owners.

#### Garment flows and print compatibility

- `GarmentFlow`: data-driven option axes for T-shirts, hoodies, and longsleeves.
- `GarmentFlowCategory`: explicit through model linking a flow to a legacy category with `db_constraint=False`; this avoids the implicit constrained M2M problem in the supplied plan.
- `ProductPrintLink`: selected warehouse print.
- `ProductPrintCompatibility`: compatibility for this product and option combination. This deliberately replaces the supplied global `PrintGarmentCompatibility`, because editing one product must not silently change every other product using the same print.

#### Size-grid system

- `SizeGridProfile`: Fable metadata attached to an existing `SizeGrid`, including garment code and suggested option key.
- `ProductOptionSizeGrid`: mandatory explicit assignment of one active size grid to one enabled product option.
- `ProductSizeRule`: product-wide enable/disable rule for one size within one option.
- Existing `VariantSizeRule`: color-specific enable/disable and stock override within one option.

Effective size availability is:

```text
rows in assigned size grid
    intersect product option size rules
    intersect exact color/option size rules
    annotated with tracked stock where present
```

A published product cannot have an enabled fit without an assigned active grid. A disabled fit does not require a grid.

#### Editor reliability

- `ProductEditorState`: revision number, last editor, and updated timestamp for optimistic concurrency.
- `EditorDraft`: versioned server-side autosave payload.

## Size-grid library

The custom admin panel receives a dedicated “Розмірні сітки” section beside catalogs.

The library supports:

- filter by catalog, garment, fit, and status;
- create, edit, duplicate, archive, and preview;
- table-based editing of measurement columns and size rows;
- drag reordering of rows and columns;
- standard display labels such as `2XL` while preserving normalized keys such as `XXL`;
- optional explanatory image, legend, notes, measurement tolerance, and fit advice;
- live desktop/mobile preview using the same renderer as the public PDP;
- structured validation: unique size keys, unique column keys, at least one size row, valid numeric/text values, and no unsafe raw HTML.

The UI never asks the administrator to edit `guide_data` JSON directly. The backend still stores a validated normalized JSON document in the existing field for backward compatibility.

## Product editor information architecture

### Primary navigation

1. Основне
2. Принт
3. Варіанти
4. Розміри
5. Медіа
6. Контент і SEO
7. FAQ
8. Канали продажу

The sticky top bar contains return navigation, title, status, revision/save state, preview, and publish controls. The left rail contains section status and a readiness score. On narrow screens it becomes a horizontally scrollable chip bar.

### Variant matrix

Rows are colors; columns are active option combinations, initially classic and oversize for T-shirts. Each cell shows:

- enabled/disabled state;
- effective localized-title source;
- selected size grid;
- available-size count;
- photo count;
- SEO health;
- price delta;
- print compatibility warning.

Clicking a cell opens a right-side workspace with Content, SEO, Media, Sizes, and FAQ tabs. Inheritance badges show whether every field comes from the product, fit, color, or exact combination.

Bulk actions include:

- copy Ukrainian content to RU/EN as an editable draft;
- copy one fit profile to another;
- duplicate a color without photos;
- apply a size toggle to all colors;
- apply one grid to selected fits;
- reset selected fields to inheritance.

### Size assignment and toggles

The “Розміри” section has one card per enabled fit.

Each card requires a grid selection and renders the actual grid rows as tactile size chips. Clicking a chip toggles availability:

- enabled: filled/high-contrast state;
- disabled: muted gray state with a strike indicator and accessible `aria-pressed=false`;
- stock warning: amber state;
- unavailable because of a higher-level rule: locked state with an explanation.

The administrator can switch between “all colors” and an individual color. Product-wide changes become `ProductSizeRule`; color-specific changes become `VariantSizeRule`.

## Public PDP experience

Directly beneath the classic/oversize controls, the PDP shows a compact “Порівняти розмірні сітки” button when at least one assigned grid exists.

Opening it displays all enabled fits together:

- desktop: side-by-side comparison cards or a horizontally scrollable comparison table when there are more than two;
- mobile: stacked cards with a sticky classic/oversize segment control and a “Показати обидві” comparison mode;
- the currently selected fit is emphasized, but the other grid is never hidden;
- disabled sizes are visibly muted and cannot be selected in the purchase controls;
- the component includes measurement instructions, tolerance, grid source, and support CTA;
- focus is trapped in the dialog, Escape closes it, and returning focus is deterministic.

If no Fable assignment exists, the current single-grid renderer remains unchanged.

## SEO, GEO/AEO, and structured data

The existing indexable color, fit, and color × fit URL behavior is preserved. For a selected public combination, resolvers provide localized H1, title, meta description, visible descriptions, image alts, FAQ, and OG data.

The exact-combination URL remains self-canonical where the existing `variant_meta` policy already permits color × fit. Size-bearing URLs continue to consolidate to avoid crawl explosion.

Structured data uses `ProductGroup` with `hasVariant` entries for meaningful color/fit combinations. Each entry includes stable URL, localized name, image, color, size availability, SKU/offer identity where available, and price. FAQ schema is emitted only for visible FAQ content.

Generated SEO is a draft aid, not an automatic publishing action. Templates fill empty fields, show character/pixel previews, and never overwrite custom text without explicit confirmation.

## Media and cover behavior

Media upload is separate from the structural save transaction. XHR provides upload progress; optimization status is polled with a bounded timeout.

Cover selection validates that the chosen image belongs to the product. The selected bytes are copied through Django storage to a stable cover name, optimization is queued, and `CoverSource` is updated atomically. Replacing a cover schedules safe orphan cleanup only after the transaction commits.

URL imports use a hardened downloader:

- HTTPS only;
- DNS resolution and private/reserved-address rejection;
- bounded redirect count;
- streamed size limit;
- MIME and Pillow verification;
- timeout and decompression-bomb protection.

Deletes are staged for an undo window; physical deletion occurs only after the window expires or the next committed save confirms it.

## Save, autosave, and concurrency

Structural edits use one transactional save endpoint with a client-generated idempotency key and expected revision. Media uploads remain independent and return durable IDs referenced by the structural payload.

If the expected revision is stale, the API returns `409` with editor identity, timestamp, and changed-section hints. The UI offers reload or save-as-copy; it never silently overwrites newer work.

Autosave has two layers:

- local versioned snapshot after a short idle period;
- server draft after a longer idle period.

Draft restoration compares product revision and timestamps before offering recovery.

## Validation and readiness

Critical publication blockers:

- Ukrainian product title;
- positive price;
- at least one active color;
- canonical cover;
- at least one enabled merchandising option;
- an active size grid assigned to every enabled fit/option;
- at least one enabled size for every enabled fit;
- valid slug;
- no unresolved save conflict.

Warnings do not block publication:

- missing RU/EN translations;
- fewer than three images;
- missing optional print;
- inherited rather than custom SEO;
- missing image alts;
- print-fit compatibility warning.

The server returns machine-readable issue codes and section targets; the same readiness service powers the editor and product-list health dashboard.

## API principles

- Staff-only GET endpoints are explicitly read-only.
- Mutations require POST, CSRF, revision where applicable, and consistent `{ok, data, error, issues}` envelopes.
- Ownership checks are mandatory for every product, variant, image, size grid, and print reference.
- User-correctable errors return Ukrainian messages and stable codes.
- Unexpected exceptions are logged with a request ID and return a generic response.
- Query counts are bounded with `select_related` and `prefetch_related` for list and bootstrap endpoints.

## Migration and production safety

Before generating migrations, inspect the live engines and relevant `SHOW CREATE TABLE` output. Every relation to `storefront`, `productcolors`, `warehouse`, and users is reviewed for `db_constraint=False` where the target can be legacy MyISAM.

Data migration copies legacy color-level Fable content into Ukrainian i18n rows without deleting source fields. Garment flows are seeded idempotently. Existing products remain valid drafts; new publication checks apply only when a Fable save attempts to publish.

Deployment creates a database backup, performs a migration plan check, applies Fable migrations, runs Django checks, collects/compresses static assets, restarts Passenger, and verifies editor, PDP, feeds, and public health endpoints.

## Test strategy

### Unit and service tests

- field-level inheritance for every layer and language;
- combination-key normalization;
- size-grid normalization and validation;
- effective size resolution across grid/product/color rules;
- readiness issue classification;
- print compatibility;
- image URL security;
- cover ownership and storage copy;
- revision conflicts and idempotency.

### API tests

- staff access, CSRF, method restrictions, ownership boundaries;
- create/edit/duplicate flows;
- all cover sources;
- grid CRUD and preview;
- transactional product save rollback;
- publish blockers;
- autosave recovery;
- stable error envelopes.

### Public regression tests

- product with no Fable data renders current behavior;
- classic and oversize grids both appear when assigned;
- disabled sizes cannot enter cart or order;
- localized color × fit content reaches meta, visible copy, OG, and schema;
- feed identifiers remain stable unless a reviewed fit-specific offer policy explicitly requires otherwise;
- Telegram and order admin show fit, color, print, and thermo information.

### Browser QA

- desktop and 375 px editor;
- keyboard-only navigation and focus order;
- reduced motion;
- no console errors;
- image upload progress and optimization;
- dirty-state recovery and network interruption;
- public size-grid comparison on desktop and mobile;
- create and edit an existing legacy product without reloads.

## Out of scope unless separately approved

- replacing `ProductColorVariant` with a new inventory/SKU engine;
- destructive cleanup of legacy Fable fields;
- changing all historical marketplace offer IDs;
- removing the old product editor;
- introducing a frontend framework or build pipeline.
