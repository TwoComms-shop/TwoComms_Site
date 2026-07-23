# T-shirt Size Advisor Design

## Goal

Replace the oversized size-guide shortcut on T-shirt product pages with a quiet,
direct path to exact classic and oversize measurements plus a compact height and
weight based size advisor. Keep guide browsing independent from the purchasable
fit and keep recommendations constrained by the current product's available sizes.

## Customer Experience

The size selector header exposes two small text actions: `Size guide` and
`Find my size`. Either action opens the existing size-information tab and scrolls
it into view. The large comparison card below the classic/oversize purchase
selector is removed.

Inside the size tab, one three-way segmented control switches between Classic,
Oversize, and Find my size. Only one surface is expanded at a time. The two guide
surfaces retain structured measurement tables, accessible image fallbacks, and
product-specific captions. The advisor asks for height, weight, and desired fit,
then returns a primary size, an adjacent alternative when available, and one short
fit explanation.

## Data And Recommendation Model

The uploaded CRC FS-101 chart becomes the canonical classic T-shirt guide:
`S-3XL`, chest `92-132 cm`, garment length `65-79 cm`, sleeve `16-24 cm`, and
shoulders `43-53 cm`. The image is stored once as an optimized repository asset
and uploaded once to the canonical production `SizeGrid`; structured rows remain
the source of truth for rendering and machines.

Guide rows are informational and must not implicitly expand a product's sellable
catalog sizes. The advisor receives the active product/colour/fit availability
matrix and never recommends a disabled or absent size. The deterministic scorer
uses height and weight bands, fit-specific adjustments, and real garment
measurements. It presents the result as guidance, not a guaranteed body fit, and
offers the next available size as a tighter or looser alternative.

## Localization And Machine Readability

All labels, validation, result copy, and explanatory content are localized for
Ukrainian, Russian, and English through Django translations and data attributes.
The rendered page contains concise visible instructions and a server-rendered
`WebApplication` plus `HowTo` JSON-LD graph describing the free size-selection
tool. Dynamic results are never fabricated into indexed schema.

## Accessibility And Motion

Tabs use `aria-selected`, `aria-controls`, and keyboard arrow navigation. Inputs
have explicit labels, numeric ranges, units, and inline validation. Results use a
polite live region. Triggering either top action moves focus to the chosen mode
after a reduced-motion-aware scroll. Stable panel dimensions and restrained
opacity/transform transitions prevent layout jumps.

## Verification

Use TDD for canonical data, the recommendation scorer, mode selection, and the
production backfill command. Run targeted Django and Node tests, translation
compilation, Django checks, static collection/compression, and responsive browser
checks at mobile, tablet, and desktop widths. Production verification must inspect
the real MariaDB rows, deployed SHA, page JSON-LD, interaction state, and Passenger
restart.
