# Fit-Specific Size Guide UX Design

## Goal

Give every apparel product two independent size-guide experiences: the existing
classic grid (`S-2XL`) and the oversize grid (`XS-2XL`), with an optimized shared
oversize visual asset, product-specific alt text, accessible responsive controls,
and crawlable explanatory copy.

## Architecture

The existing Fable5 `ProductOptionSizeGrid` assignments remain the source of truth
for fit-specific grids. A single canonical oversize `SizeGrid` image is stored once
and reused by all oversize assignments; a resolver fallback makes it the default
for future oversize profiles without copying the file per product. Existing classic
assignments and data are preserved.

The product detail size-guide panel becomes an independent comparison surface. It
renders a compact fit switch (`classic` / `oversize`) even when the product's main
fit selector is currently on the other option. The selected guide updates locally,
keeps the main product selection untouched, and exposes both a structured table and
an optional visual guide. Visual guides use responsive `<picture>`/`img` markup with
stable dimensions, lazy loading, keyboard focus, and an alt assembled from the
current product title plus fit label. Text copy explains the garment fit and
measurement semantics for SEO, GEO, and AI-readable page content.

## Data and media

- Keep the uploaded source outside runtime duplication; generate a repository-shipped
  optimized WebP/AVIF asset from `size_img/Oversize_tshirt.png`.
- Add a stable default oversize profile/asset reference in the size-guide resolver.
- Add an idempotent management command for production that creates or updates the
  canonical oversize grid and assigns it only to products with an enabled oversize
  fit and no explicit oversize assignment.
- Preserve per-product or per-variant overrides and existing classic grids.
- Use product title and localized fit terminology for image alt text; never use a
  generic repeated alt when a product-specific label is available.

## UI and accessibility

- Add a visible `Класика` / `Оверсайз` segmented control inside the size-guide panel.
- Keep it independent from the top fit selector; selecting a guide does not silently
  change the purchasable fit or cart payload.
- For an image guide, show a responsive framed media region with contain behavior,
  width/height metadata, a zoom/open affordance, and a caption naming the fit.
- For a text-only guide, show a polished table with clear column headers, mobile
  horizontal scrolling only when needed, and a short measurement note.
- Ensure focus states, `aria-selected`, `aria-controls`, and live-region updates are
  present; verify at phone, tablet, and desktop widths.

## SEO, GEO, and AI content

Each guide panel includes concise localized copy stating that classic sizing starts
at `S` while oversize starts at `XS`, how to interpret the measurements, and that the
displayed chart belongs to the selected product fit. Use semantic headings and
visible text so search engines and answer systems can understand the distinction;
do not rely on image pixels alone. Image alt text remains descriptive and localized.

## Verification

- Unit tests for resolver fallback, idempotent assignment, per-product alt payload,
  and preservation of explicit overrides.
- Template/JavaScript tests for independent guide switching, selected state,
  unavailable-grid behavior, and no mutation of the main fit selection.
- Django `check`, migration dry-run, targeted Fable5/storefront tests, JS tests,
  image metadata inspection, `git diff --check`, and responsive browser checks.
- Production deploy runs the management command against the real server DB, then
  collects static files, reloads Passenger, and verifies representative product
  pages plus the size-guide interaction.

