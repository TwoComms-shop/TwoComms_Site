# PROD-006 — Stale hardcoded category icon points to a missing media variant

**Priority:** P1
**State:** confirmed/open
**Owner:** templates and asset pipeline

## Symptom

`/media/category_icons/optimized/free-icon-clothes-1720753_24x24.png` returned 500 at 13:04:50 on 13 July. The referer identifies `management.twocomms.shop`. The file does not exist on production.

## Evidence and cause

- Missing optimized path confirmed by server filesystem check.
- Existing source is a 512×512 PNG whose filename contains a storage-generated suffix.
- `media/` is ignored by Git, so a hardcoded uploaded filename is not a reproducible release asset.
- Stale URL appears in:
  - `twocomms/twocomms_django_theme/templates/pages/wholesale_order_form.html:2442`;
  - `twocomms/management/templates/management/invoices.html:2883-2884`.
- Image-optimizer code derives the variant name from the current source stem. The current suffix would produce a different optimized filename than the hardcoded one.

The missing file should be a 404. PROD-004/005 explain why that 404 can become 500; this issue explains why the request exists.

## Implementation plan

1. Add a render test proving neither template emits the stale literal path.
2. Decide whether this is UI chrome or model media:
   - UI chrome: move a stable icon/SVG into tracked static assets;
   - model media: use the model/storage URL and variant helper with a tracked fallback.
3. Avoid constructing optimized filenames manually.
4. Add a rendered-HTML asset existence check to deployment smoke tests.
5. Verify media fallback behavior when the source or variant is absent.

## Acceptance criteria

- Both pages request a tracked or storage-derived asset that returns 200 with the correct MIME type.
- Removing the optional media source uses a fallback and never causes 500.
- A fresh deployment to an empty media directory does not emit a hardcoded production upload name.
