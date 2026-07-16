# PDP compact configurator and price semantics

## Scope

Refine the existing Fable5 PDP configurator without changing the gallery, restock flow, tabs, recommendations, SEO ordering, or the overall visual language. The change covers price attribution, compact selector layout, unavailable fit cards, and the hoodie fleece state.

## Confirmed production state

- The thermochromic green variant of `futbolka-boiova-kvitochka` has a `400 UAH` delta in `VariantDetails` with the reason `термохромна тканина`.
- That product has no fit `ProductOptionProfile` and no variant combination price override.
- The current UI repeats the material delta inside every fit card because fit-choice rendering consumes the fully resolved variant delta.
- The disabled fit card puts a long reason and the repeated price in the same absolute-positioned footer, which causes overlap.

## Price model

Price components have distinct ownership:

1. `Product.final_price` or `ProductColorVariant.price_override` is the base garment price.
2. `VariantDetails.price_delta` is the color or material delta. It is applied once to every configuration of that color.
3. A single-axis `ProductOptionProfile.price_delta` is an additive option delta, such as an oversize surcharge.
4. An explicit `VariantCombinationProfile.price_delta` remains an exact total delta override for backward compatibility.

Without an exact combination override, the public price is:

`base variant price + material delta + selected option deltas`

The public payload exposes a structured breakdown. The material story shows only the material delta. Each option card shows only its own option delta. A disabled option shows its unavailable reason and no price. The main price remains the final purchasable price.

## Desktop composition

Use a compact two-zone configurator row:

- Color occupies a narrow intrinsic column.
- Fit occupies the flexible column.
- Fit choices are equal-height cards in one horizontal row when two choices fit.
- Card content uses a stable three-part grid: icon, title/description, and trailing check or price.
- Unavailable reason is a normal-flow second row spanning the text and trailing columns. It may wrap to two lines and never shares an absolute layer with price.
- The grid uses explicit minimums and `min-width: 0` so long localized text cannot expand or overlap adjacent content.

This removes the large blank area below a single color swatch without hiding any fit state.

## Mobile composition

Below the existing PDP mobile breakpoint:

- Color and fit become single-column sections.
- Fit cards remain a two-column row when the viewport safely supports it and become one column on narrow phones.
- Reasons wrap in normal flow.
- Touch targets stay at least 44 CSS pixels high.
- No text is truncated when it communicates price or unavailability.

## Fixed hoodie fleece state

When a `lining` axis has exactly one enabled choice:

- Do not render the unavailable choice as a second selector card.
- Keep the enabled choice as a real hidden form/configuration value so cart and pricing behavior remain unchanged.
- Add a compact locked-on switch to the existing material story with the fleece icon, label `З флісом`, and explanation `Ця модель доступна тільки з флісом`.
- The switch is a status, not a fake editable control: it uses `aria-checked="true"` and `aria-disabled="true"` and does not change value when activated.

If a second lining choice becomes enabled later, the fixed state automatically disappears and the normal option selector returns.

## Material story price

The story receives a compact price badge only when the material/color delta is non-zero. For the production thermochromic variant it reads `Термотканина +400 грн`. Changing fit does not change this badge. The badge is visually secondary to the final product price and does not duplicate a fit surcharge.

## Admin behavior

No new database fields are required. Existing Fable5 controls retain their meaning:

- Color-level delta edits the material/color component.
- Option profile delta edits the option component.
- Exact color x option delta remains the explicit override.

The public distinction is derived from the existing ownership level, so reverse storage links and product editing remain synchronized.

## Accessibility and interaction

- Disabled fits remain visible, non-clickable, and expose the reason in visible text.
- Selected fit uses both border/check state and the checked radio state.
- Price badges are text, not color-only indicators.
- The fixed fleece state has visible explanatory copy and correct switch semantics.
- Focus rings and keyboard selection remain available for enabled fit controls.

## Verification

- Unit tests cover additive material plus option pricing and exact combination override compatibility.
- PDP render tests prove material delta is absent from fit cards and fixed fleece renders once.
- JavaScript tests cover displayed option deltas and stable final price resolution.
- Production QA covers the thermochromic T-shirt and a hoodie at desktop and mobile widths, including no horizontal overflow, no overlap, correct price attribution, and console health.
