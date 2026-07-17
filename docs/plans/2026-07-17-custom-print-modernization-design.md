# Custom Print Modernization Design

## Direction

Modernize the established TwoComms Custom Print experience instead of replacing it with a reduced wizard. The result must preserve the earlier rich selection flow and cinematic identity while removing duplicated actions, fragile SVG garment art, mobile overflow, and unclear completion states.

The page remains a premium dark product studio with warm gold as the main action color and restrained violet light around garment imagery. Visual depth comes from the real garment assets, lighting, typography, material surfaces, and purposeful motion. Repeated rectangular panels, decorative marketing cards, handwritten display type, and competing actions are excluded.

## User Journey

The configurator uses eight explicit stages:

1. Format: personal order or B2B.
2. Garment: hoodie, T-shirt, longsleeve, or customer garment.
3. Configuration: fit, fabric, color, and garment details.
4. Placement: front, back, sleeves, custom placement, and exact print format.
5. Artwork: ready file, idea, or designer assistance.
6. Quantity and sizes.
7. Gift options.
8. Contact and review.

On desktop, the current stage is expanded, completed stages remain visible as compact editable summaries, and the next stage is visibly queued. The preview stays sticky beside the flow. On mobile, only the active stage is shown inside an app shell with fixed progress at the top and price plus one main action at the bottom.

## Hero

The hero returns to the earlier cinematic composition: strong title and a single creation CTA on the left, the existing garment-stage photo on the right, a thin gold ring, violet backlight, garment labels, and calibrated print-zone indicators. Telegram, the color palette bar, handwritten signature, and four competing feature panels are removed.

On mobile, text and CTA occupy the upper portion and the complete garment stage remains visible below them. The composition must show the garments and sample print zones before the fold without cropping labels or hiding the next section entirely.

## Product Studio

The desktop workbench uses a 42/58 split. The preview is a cohesive studio surface rather than a stack of cards. The form uses visual selectors, swatches, proportional ISO format controls, and compact supporting copy. Selected controls receive a clear gold edge, light lift, and accessible state indicator.

The preview uses transparent PNG garment profiles and a masked color layer. It displays placement geometry and dimensions only; customer artwork is never composited onto the garment. Front/back changes use a short crossfade. Pointer tilt is limited to desktop and disabled for reduced motion.

## Mobile Preview

The preview is not permanently placed above the form. An eye action in the app bar and a contextual `Перегляд розташування` action open a full-screen sheet rendered in `document.body`. The sheet contains:

- the PNG garment and selected color;
- front/back segmented control;
- selected placement outlines;
- exact label such as `A4 · 21 x 29.7 cm`;
- a compact note that the manager verifies final offsets.

The sheet supports Escape, focus trapping, focus restoration, safe-area insets, and a clear close control. It never competes with the bottom price bar.

## Manager Contact

There is no Telegram action in the hero or repeated inside stages. A single manager action opens a contact sheet and saves the current draft before Telegram navigation. B2B uses `Обговорити партію з менеджером TwoComms`; personal orders use `Зв'язатися з менеджером`.

## Validation And Submission

The main action validates the active stage, shows a specific inline error, reveals the relevant control, and focuses the first invalid field. Completed stage summaries remain editable.

After a successful cart request, the page opens a moderation dialog instead of redirecting automatically. The dialog explains manager verification, possible artwork or price correction, and payment coordination. It provides cart and Telegram actions with duplicate-submit protection.

## SEO And Content

Canonical URL, robots behavior, hreflang, title, H1, Service schema, FAQ schema, and long-form meaning remain unchanged. Broken replacement characters in source translations are repaired. SEO content stays outside the mobile app mode and is presented as restrained full-width sections and accessible FAQ accordions rather than large nested cards.

## Quality Bar

The page must be tested at 320x568, 360x800, 390x844, 430x932, 768x1024, 1024x768, 1366x768, 1440x900, and 1920x1080. Each viewport must have no horizontal overflow, clipped text, overlapping fixed UI, hidden controls, or inaccessible content. Keyboard navigation, reduced motion, focus restoration, localization, and image transparency are part of acceptance.

