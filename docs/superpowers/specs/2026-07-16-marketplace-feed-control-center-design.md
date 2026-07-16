# Marketplace Feed Control Center Design

## Goal

Make orders the default custom-admin section and add a staff-friendly feed control center that exposes every production feed, preserves existing marketplace contracts, and lets staff create campaign-specific feeds without editing code.

## Current State

- `/admin-panel/` defaults to `stats`, which invokes the expensive analytics context before the operator can reach day-to-day order work.
- Google, Rozetka, Kasta, BuyMe, Prom, and Bezzet are dynamic XML endpoints backed by `storefront.services.marketplace_feeds`.
- Google, Rozetka, Kasta, BuyMe, Prom, and Instagram/Meta also have file-backed snapshots under `MEDIA_ROOT`; `regenerate_feeds_if_dirty` refreshes them on cron.
- Instagram/Meta is a separate management command with hard-coded hoodie, image-count, availability, and image-order rules.
- Feed formats are not interchangeable. Google/Meta use RSS with Google attributes; marketplace consumers use several distinct YML dialects; Kasta requires Ukrainian and Russian fields; BuyMe deliberately removes forbidden brand terms.
- Existing URLs and stable offer IDs are production contracts and must not change.

## Approaches Considered

### 1. Generic XML field builder

Expose tags and mappings directly in the admin. This is flexible but unsafe: staff can omit required tags, change stable IDs, or create an XML document that is syntactically valid but rejected by the destination marketplace. It also duplicates hard-won platform logic already covered by tests.

### 2. One bespoke settings page per feed

Keep every generator independent and build a custom form for each. This minimizes generator changes but does not scale to duplicates, campaign feeds, shared rules, or consistent health reporting. It would also keep Meta isolated from the normalized offer pipeline.

### 3. Validated adapters plus editable profiles (selected)

Keep a code-owned adapter registry for format-critical behavior and add database-backed feed profiles for safe business rules. A profile selects a known adapter, can inherit another profile, and controls filters, availability, quantity, language where supported, and image policy. Per-product overrides provide exact include/exclude, sold-out, stock, and image selection. This preserves platform correctness while supporting new campaign feeds.

## Architecture

### Adapter registry

`storefront.services.feed_registry` is the source of truth for supported adapters:

- Google Merchant: RSS 2.0, Ukrainian or Russian text, dynamic canonical endpoint and snapshot aliases.
- Meta/Instagram: RSS 2.0 using stable pixel-compatible IDs, dynamic custom endpoint and canonical snapshot.
- Rozetka: YML with Rozetka category identifiers and Ukrainian merchandising fields.
- Kasta: bilingual Ukrainian/Russian YML fields.
- BuyMe: YML with its grouping, price-drop, and brand-removal constraints.
- Prom: Prom-compatible YML.
- Bezzet: legacy UAPROM-style YML with its quantity behavior.

Each definition owns the display label, format, supported languages, canonical and alias URLs, snapshot command/path, default rules, and builder callable. Adapter identifiers are a closed choice list; staff cannot inject a Python path, output path, or arbitrary XML.

### Feed profiles

`MarketplaceFeed` stores a named configuration:

- unique slug and optional system key;
- adapter and active/system flags;
- optional parent profile;
- description and selected language;
- validated rules JSON;
- creator and timestamps.

System profiles correspond to production feeds and cannot be deleted or moved to another adapter. Custom profiles are published at `/feeds/<slug>.xml`. Duplicate creates an independent custom profile whose parent is the source, then allows explicit overrides.

Rules use a versioned schema with conservative defaults:

- filters: selected categories, include/exclude product IDs, minimum image count, price range, and optional search keywords;
- availability: inherit catalog behavior, force in stock, or force sold out, plus a non-negative exported quantity;
- images: variant-first, newest-first, main-first, or explicitly selected, with an adapter-capped maximum count;
- text: selected supported language.

Unknown or malformed rule keys are rejected. Parent chains are capped and cycle-checked. Child rules are merged onto parent rules, while system defaults remain the final fallback.

### Per-product overrides

`MarketplaceFeedProductRule` belongs to one feed and one product. It can:

- inherit/include/exclude the product;
- inherit/force-in-stock/force-sold-out availability;
- override quantity;
- select exact image tokens from main, common, or color-variant images.

Image tokens are server-validated against the selected product before saving. Overrides apply after profile filters and before adapter serialization. Stable offer IDs, SKU/MPN logic, product URLs, prices, and adapter-required tags remain code-owned.

### Runtime pipeline

1. Resolve an active profile by system key or public slug.
2. Resolve and validate inherited rules.
3. Query only published products, apply profile filters, and prefetch all required variants/images.
4. Build normalized `FeedOffer` records.
5. Apply per-product inclusion, availability, quantity, language, and image overrides.
6. Serialize through the selected adapter.
7. Return XML with no-cache headers and a deterministic filename.

If no migrated system profile exists during a rolling deploy, canonical endpoints use registry defaults. This keeps production feeds available before and during migration execution.

### Snapshot generation and health

The cron regenerator remains the snapshot authority. It resolves the system profile for each command so admin changes affect the next regeneration without introducing a second scheduler. Manual regeneration is a staff-only POST using a command whitelist.

The feeds section reports:

- active/disabled state;
- adapter, format, and language badges;
- canonical, alias, and custom URLs with copy/open actions;
- snapshot timestamp and file size when applicable;
- current product/offer estimate and number of overrides;
- validation warnings for inactive parents, empty results, unsupported language, missing selected images, or stale snapshots.

Opening the feeds section must not fetch external URLs or regenerate XML. Health reads local metadata and bounded database counts; expensive validation runs only through an explicit staff action.

## Admin UX

The primary navigation order starts with `Замовлення`, followed by `Статистика`, then `Фіди`. `/admin-panel/` renders orders and does not call the analytics dashboard builder.

The feeds section uses an operational master-detail layout:

- a compact header with total, active, warning, and custom-feed counters plus `Створити фід`;
- a searchable list grouped into system exports and campaign/custom feeds;
- each row shows destination, language, format, state, update freshness, item estimate, and quick link actions;
- the detail pane has tabs for Overview, Rules, Products & Images, and History/validation state;
- creation starts from a destination preset, because platform requirements should determine valid controls;
- duplicate is a first-class action for creating a campaign variation;
- destructive actions use explicit confirmation; system profiles cannot be deleted.

Controls use native selects, toggles, numeric inputs, checkboxes, and image swatches. The visual style follows the existing quiet admin palette and density, uses Font Awesome already bundled by the project, and avoids nested decorative cards.

## Error Handling and Safety

- Every mutation is staff-only, POST-only, CSRF-protected, and validated by Django forms/services.
- Public custom feeds return 404 when inactive or unknown.
- A profile cannot inherit from itself, a descendant, or an incompatible adapter.
- A custom slug cannot shadow known root URLs or reserved paths.
- Quantities and image limits are bounded; selected image IDs must belong to the configured product.
- Manual generation captures command errors and shows a staff message without exposing filesystem details publicly.
- Existing canonical URLs, legacy aliases, stable offer IDs, and analytics-noise exclusions remain intact.

## Testing

- Admin regression: `/admin-panel/` defaults to orders and does not build analytics; `?section=stats` still does.
- Model and rule tests: defaults, merge order, cycle prevention, adapter compatibility, slug reservation, and rule validation.
- Runtime tests: filters, per-product availability, quantities, explicit images, language selection, inactive profiles, and fallback defaults.
- Adapter contract tests: all existing marketplace tests remain green; Meta moves under equivalent contract coverage.
- View tests: staff authorization, create/edit/duplicate/delete protections, POST-only generation, and public custom endpoint headers/content.
- Template tests: feeds navigation visibility, all production URLs, language/format labels, and form errors.
- Browser QA: desktop and mobile screenshots, overflow checks, copy/open controls, create/edit flow, image selector, and orders default landing.
- Production verification: migrate, force-regenerate snapshots, run Django checks/compression, restart Passenger, verify canonical and custom XML URLs, snapshot freshness, admin authentication redirect, and site health.

## Deployment

1. Apply migrations before restarting application workers.
2. Run `regenerate_feeds_if_dirty --force --min-age-sec=0` so canonical snapshots reflect seeded profiles.
3. Run `manage.py check`, rebuild compressed assets, and restart Passenger.
4. Verify all dynamic feeds and snapshots, including legacy Google v2/v3 hash parity.
5. Verify `/admin-panel/` redirects anonymous users and renders orders first for authenticated staff.
