# Custom Print PNG Garment Asset Specification

Date: 2026-07-18
Status: production contract for the next asset pass

## Required Files

```text
tshirt-regular-front.png
tshirt-regular-back.png
tshirt-oversize-front.png
tshirt-oversize-back.png
hoodie-regular-front.png
hoodie-regular-back.png
hoodie-oversize-front.png
hoodie-oversize-back.png
hoodie-lacing.png          optional aligned front overlay
```

Keep `longsleeve-front.png` and `longsleeve-back.png` on the same contract while
the longsleeve remains selectable.

## Canvas And Export

- Exact canvas: `1200 x 1400 px`.
- Format: PNG with RGBA alpha, embedded or declared sRGB.
- Background: fully transparent.
- Recommended optimized file size: below `1.2 MB` per garment.
- Strip editor metadata and thumbnails from the production export.
- Do not include text, UI, format labels, print rectangles, backdrop, floor,
  external cast shadow, glow, model or mannequin.
- No semi-transparent fringe outside the garment. Validate the alpha edge on
  both pure black and pure white backgrounds.

## Camera And Registration

- Straight orthographic product view.
- Yaw, pitch and roll: `0 degrees`.
- Optical center: `x = 600 px`, tolerance `+/- 6 px`.
- Front/back shoulder and hem registration tolerance: `+/- 8 px`.
- Recommended outer alpha safe bounds:
  - left: `x >= 96 px`
  - right: `x <= 1104 px`
  - top: `y >= 84 px`
  - bottom: `y <= 1330 px`
- No visible pixels may touch any canvas edge.
- Regular and oversize are separate silhouettes. Do not create oversize by
  scaling the regular image.

## Material And Lighting

- Neutral grayscale base suitable for CSS color tinting.
- Preserve folds, seams, ribbing and fabric depth through luminance.
- Use soft frontal studio light with restrained side fill.
- Avoid colored rim light and clipped black/white areas: selected HEX is applied
  by a masked color layer and depends on readable luminosity.
- Front/back lighting direction, garment scale and fabric treatment must match.
- `hoodie-lacing.png` contains only aligned lacing/eyelet details on the same
  transparent `1200 x 1400` canvas.

## Preview Registration

Current control anchors on the canvas:

| Area | Center |
| --- | --- |
| Front torso | `600, 602` |
| Back torso | `600, 616` |
| Left sleeve | `204, 672` |
| Right sleeve | `996, 672` |

The designer must also provide, for every `product x fit x side`, the real
garment width in millimetres and a printable safe rectangle below the neckline.
Those values calibrate exact ISO overlays:

- A6: `105 x 148 mm`
- A5: `148 x 210 mm`
- A4: `210 x 297 mm`
- A3: `297 x 420 mm`
- A2: `420 x 594 mm`

## Formats Must Not Be Baked Into PNGs

Do not export separate garment images with A6/A5/A4/A3/A2 rectangles already
drawn. The application must render the selected format and zones as calibrated
overlays. Baking them into PNGs would:

- require at least 24 body images for four profiles before sleeve and combined
  zone variants;
- prevent correct front/back/sleeve combinations;
- make a physical-size correction require re-exporting the entire matrix;
- allow a rectangle to look correct while having the wrong millimetre scale.

The garment PNG communicates shape and material. The code communicates exact
format, zone, dimensions, guides and manager-check warnings.

## Delivery Checklist

- [ ] Every file is exactly `1200 x 1400` RGBA PNG.
- [ ] Front/back pairs align within the stated tolerances.
- [ ] Alpha edges are clean on black and white.
- [ ] Garment remains neutral under green, pink, coyote, black and white tint.
- [ ] Regular and oversize silhouettes are visibly different.
- [ ] Hoodie lacing layer aligns without translation or rescaling.
- [ ] A4/A3/A2 overlays remain inside the supplied printable-safe rectangle, or
      the profile is explicitly marked as requiring manager review.
- [ ] Files are inspected at `320x568`, `390x844`, `768x1024`, `1440x900`.
