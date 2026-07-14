# Fable 5 v2 Variant Editor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deliver a production-safe Fable 5 product editor with inherited product/fit/color/combination content, reusable per-fit size grids, interactive size availability, variant-aware SEO, polished administration, and public size-grid comparison.

**Architecture:** Keep one canonical `Product` and the existing color variants. Store only sparse overrides in additive Fable 5 tables, resolve every localized field through exact combination → product option → color → product, and use the existing `SizeGrid` as the canonical structured grid while Fable 5 owns fit assignments and availability rules. Preserve the old editor and public fallbacks, validate against production MySQL/MyISAM before migration, and ship through an isolated branch with browser and server verification.

**Tech Stack:** Django 5, Django modeltranslation, vanilla JavaScript modules without a build step, CSS custom properties, SQLite test settings, production MySQL/MyISAM + InnoDB Fable tables, Pillow, django-compressor, Passenger.

---

## Execution context and invariants

- Design source: `docs/plans/2026-07-14-fable5-v2-variant-editor-design.md`.
- Feature branch: `codex/fable5-v2-upgrade`.
- Isolated worktree: `/Users/zainllw0w/.config/superpowers/worktrees/site/fable5-v2-upgrade` for the current run. A future agent may use another clean worktree.
- Test command prefix for the current machine:

```bash
PYTHON=/Users/zainllw0w/TwoComms/site/.venv/bin/python
cd twocomms
```

- Never stage `FABLE5_UPGRADE.zip`, `fable5_upgrade/`, `fable5-package/`, incident documents, or any unrelated file from the main checkout.
- Never write the SSH password to a repository file, shell history file, plan, log, or process-list-visible script.
- Every relation from a new Fable model to `storefront`, `productcolors`, `warehouse`, or the user table must be reviewed for MyISAM compatibility and normally use `db_constraint=False`.
- Do not edit historical migrations.
- Do not publish until the exact active fit-to-size-grid assignments pass server validation.
- Use `transaction.atomic()` for structural save operations. Media transfer happens separately but ownership/reference updates are atomic.
- Run the focused RED test and observe the expected failure before production code for every behavior task.
- Commit after each completed task; do not push or deploy until the complete verification gate.

## Target data model summary

Add these models to `twocomms/fable5/models.py`:

```text
VariantDetailsI18n(details, lang, display_name, short_description,
                   full_description, marketing_text, seo_title,
                   seo_description, seo_keywords, og_title, og_description)

ProductOptionProfile(product, option_key, option_values, price_delta,
                     price_delta_reason, youtube_url, is_active)
ProductOptionProfileI18n(profile, lang, same localized content fields)

VariantCombinationProfile(variant, combination_key, option_values,
                          price_delta, price_delta_reason, youtube_url,
                          is_active)
VariantCombinationProfileI18n(profile, lang, same localized content fields)

VariantImageAltI18n(color_image, lang, alt)
ProductImageAltI18n(product_image, lang, alt)

GarmentFlow(code, name, axes, is_active)
GarmentFlowCategory(flow, category)

ProductPrintLink(product, print_ref, note)
ProductPrintCompatibility(link, combination_key, is_allowed, note)

SizeGridProfile(size_grid, garment_code, option_key, is_active)
ProductOptionSizeGrid(product, option_key, size_grid)
ProductSizeRule(product, option_key, size, is_enabled, note)

CoverSource(product, source_type, color_image, product_image, source_missing)
ProductEditorState(product, revision, updated_by, updated_at)
EditorDraft(user, product, payload, product_revision, updated_at)
```

Retain `VariantSizeRule` for color/option-specific availability and stock. Use normalized uppercase size keys (`XXL`) and separate display labels (`2XL`) from the size grid.

---

### Task 1: Freeze baseline contracts and production schema facts

**Files:**
- Create: `twocomms/fable5/tests/test_v2_baseline.py`
- Reference: `twocomms/fable5/models.py`
- Reference: `twocomms/storefront/models.py`
- Reference: `twocomms/productcolors/models.py`
- Reference: `twocomms/warehouse/models.py`

**Step 1: Record the current green baseline**

Run:

```bash
$PYTHON manage.py test fable5 storefront.tests.test_product_size_guides --settings=test_settings
```

Expected: 22 tests pass before v2 work.

**Step 2: Write failing schema-invariant tests**

Add tests that enumerate every planned external relation and fail until the model exists. Include a helper:

```python
def assert_external_relation_is_unconstrained(model_name, field_name):
    field = apps.get_model("fable5", model_name)._meta.get_field(field_name)
    assert field.db_constraint is False
```

Also assert no migration operation targets an app other than `fable5`.

**Step 3: Run RED**

Run:

```bash
$PYTHON manage.py test fable5.tests.test_v2_baseline --settings=test_settings -v 2
```

Expected: failure because v2 models are absent.

**Step 4: Inspect production engines read-only**

Over SSH, activate the production virtualenv and use Django/MySQL read-only queries to capture:

```sql
SHOW TABLE STATUS WHERE Name IN (
  'storefront_product', 'storefront_sizegrid',
  'productcolors_color', 'productcolors_productcolorvariant',
  'productcolors_productcolorimage', 'warehouse_print'
);
SHOW CREATE TABLE storefront_sizegrid;
SHOW CREATE TABLE productcolors_productcolorvariant;
```

Expected: document actual engines in the implementation log. Do not mutate the server.

**Step 5: Commit the contract tests**

```bash
git add twocomms/fable5/tests/test_v2_baseline.py
git commit -m "test(fable5): define v2 schema safety contracts"
```

---

### Task 2: Add additive v2 models and MyISAM-safe migrations

**Files:**
- Modify: `twocomms/fable5/models.py`
- Create: `twocomms/fable5/migrations/0002_variant_editor_v2.py`
- Create: `twocomms/fable5/migrations/0003_seed_garment_flows.py`
- Modify: `twocomms/fable5/admin.py`
- Test: `twocomms/fable5/tests/test_v2_models.py`

**Step 1: Write failing model-behavior tests**

Cover:

- unique `(details, lang)`;
- unique `(product, option_key)`;
- unique `(variant, combination_key)`;
- unique size-grid assignment per `(product, option_key)`;
- unique product-size rule per `(product, option_key, size)`;
- normalized combination keys;
- all external `db_constraint=False` fields;
- internal Fable-to-Fable constraints remain enabled.

**Step 2: Run RED**

```bash
$PYTHON manage.py test fable5.tests.test_v2_models --settings=test_settings -v 2
```

Expected: import/model lookup failures for new models.

**Step 3: Implement abstract localized field mixin and concrete models**

Use an abstract model for repeated localized fields, but keep concrete i18n tables. Do not include inheritance flags per field; blank values mean inherit. Normalize `option_key` and `combination_key` in service functions, not fragile model magic.

**Step 4: Generate the schema migration**

```bash
$PYTHON manage.py makemigrations fable5 --name variant_editor_v2 --settings=test_settings
```

Inspect the file. Expected: only `fable5` operations; no external physical constraints.

**Step 5: Add idempotent data migration**

Seed:

- `tshirt`: fit classic/oversize;
- `hoodie`: lining fleece, disabled no-fleece placeholder;
- `longsleeve`: fit classic by default.

Copy non-empty legacy `VariantDetails` display/SEO values into `VariantDetailsI18n(lang="uk")` with `get_or_create`; never blank or delete legacy fields.

Use `GarmentFlowCategory` rows for category matches rather than an implicit M2M.

**Step 6: Run GREEN and migration checks**

```bash
$PYTHON manage.py test fable5.tests.test_v2_baseline fable5.tests.test_v2_models --settings=test_settings -v 2
$PYTHON manage.py makemigrations --check --dry-run --settings=test_settings
$PYTHON manage.py migrate --plan --settings=test_settings
```

Expected: tests pass; no pending model changes.

**Step 7: Commit**

```bash
git add twocomms/fable5/models.py twocomms/fable5/admin.py twocomms/fable5/migrations twocomms/fable5/tests
git commit -m "feat(fable5): add inherited variant and size-grid models"
```

---

### Task 3: Implement localized content inheritance

**Files:**
- Create: `twocomms/fable5/services/content_resolution.py`
- Modify: `twocomms/fable5/services.py`
- Test: `twocomms/fable5/tests/test_content_resolution.py`

**Step 1: Write failing table-driven tests**

For each field and language, assert the precedence:

```python
exact_combination -> product_option -> color_variant -> product
```

Cover:

- exact UK value;
- exact RU blank falling to fit RU;
- fit RU blank falling to color RU;
- color EN blank falling to product EN;
- requested language blank falling to UK at the same/lower layer;
- legacy `VariantDetails` fallback;
- no Fable rows returning current Product behavior.

**Step 2: Run RED**

```bash
$PYTHON manage.py test fable5.tests.test_content_resolution --settings=test_settings -v 2
```

**Step 3: Implement pure normalization helpers**

Public API:

```python
normalize_option_values(raw: dict) -> dict[str, str]
build_combination_key(raw: dict) -> str
resolve_merchandising_context(product, variant=None, option_values=None, lang="uk") -> dict
resolve_variant_text(variant, field, lang="uk", option_values=None) -> str
```

Return both value and source metadata internally so the editor can show inheritance badges.

**Step 4: Run GREEN**

```bash
$PYTHON manage.py test fable5.tests.test_content_resolution --settings=test_settings -v 2
```

**Step 5: Commit**

```bash
git add twocomms/fable5/services.py twocomms/fable5/services/content_resolution.py twocomms/fable5/tests/test_content_resolution.py
git commit -m "feat(fable5): resolve inherited localized variant content"
```

---

### Task 4: Implement size-grid normalization and effective availability

**Files:**
- Create: `twocomms/fable5/services/size_grids.py`
- Create: `twocomms/fable5/services/readiness.py`
- Modify: `twocomms/storefront/services/size_guides.py`
- Test: `twocomms/fable5/tests/test_size_grid_resolution.py`
- Test: `twocomms/storefront/tests/test_product_size_guides.py`

**Step 1: Write failing grid normalization tests**

Assert:

- `2XL`, `XXL`, and `x2l` normalize to `XXL`;
- duplicate normalized rows are rejected;
- duplicate column keys are rejected;
- unsafe HTML is rejected or stored as plain text;
- a grid needs a size column and at least one row;
- unknown measurement cells are retained as text;
- column/row order is stable.

**Step 2: Write failing effective-size tests**

Example expected behavior:

```python
grid rows: S, M, L, XL, XXL
product classic disables S
black classic disables XXL
resolved base classic: M, L, XL, XXL
resolved black classic: M, L, XL
```

Also cover oversize using a different grid and inactive/missing grid publication issues.

**Step 3: Run RED**

```bash
$PYTHON manage.py test fable5.tests.test_size_grid_resolution --settings=test_settings -v 2
```

**Step 4: Implement services**

Public API:

```python
normalize_size_grid_payload(payload) -> dict
resolve_option_size_grid(product, option_key)
resolve_effective_sizes(product, option_key, variant=None) -> list[dict]
build_size_grid_comparison(product, variants=None, lang="uk") -> list[dict]
build_readiness(product) -> dict
```

Do not silently use the first catalog grid for a Fable-published fit. A catalog grid may be suggested in the editor, but explicit `ProductOptionSizeGrid` is required for publication.

**Step 5: Preserve legacy public fallback**

Extend `resolve_product_size_context` only when Fable assignments exist. Otherwise return byte-for-byte equivalent keys and current behavior.

**Step 6: Run GREEN**

```bash
$PYTHON manage.py test fable5.tests.test_size_grid_resolution storefront.tests.test_product_size_guides --settings=test_settings -v 2
```

**Step 7: Commit**

```bash
git add twocomms/fable5/services twocomms/storefront/services/size_guides.py twocomms/fable5/tests twocomms/storefront/tests/test_product_size_guides.py
git commit -m "feat(fable5): resolve fit-specific grids and size availability"
```

---

### Task 5: Build staff-only size-grid library APIs

**Files:**
- Create: `twocomms/fable5/views_size_grids.py`
- Modify: `twocomms/fable5/urls.py`
- Test: `twocomms/fable5/tests/test_size_grid_api.py`

**Step 1: Write failing access and CRUD tests**

Endpoints:

```text
GET  /admin-panel/fable5/api/size-grids/
POST /admin-panel/fable5/api/size-grids/save/
POST /admin-panel/fable5/api/size-grids/duplicate/
POST /admin-panel/fable5/api/size-grids/archive/
GET  /admin-panel/fable5/api/size-grids/preview/
```

Test anonymous/non-staff rejection, method restrictions, CSRF behavior, catalog ownership, normalized payload, duplicate naming, archive rules, and stable JSON envelopes.

**Step 2: Run RED**

```bash
$PYTHON manage.py test fable5.tests.test_size_grid_api --settings=test_settings -v 2
```

**Step 3: Implement query-efficient APIs**

Save `SizeGrid` and `SizeGridProfile` in one transaction. Keep the public `guide_data` format normalized. Archive by `is_active=False`; refuse hard deletion when a `ProductOptionSizeGrid` references the grid.

**Step 4: Run GREEN and query-count tests**

```bash
$PYTHON manage.py test fable5.tests.test_size_grid_api --settings=test_settings -v 2
```

**Step 5: Commit**

```bash
git add twocomms/fable5/views_size_grids.py twocomms/fable5/urls.py twocomms/fable5/tests/test_size_grid_api.py
git commit -m "feat(fable5): add structured size-grid library API"
```

---

### Task 6: Add the custom-admin size-grid workspace

**Files:**
- Modify: `twocomms/storefront/views/admin.py`
- Modify: `twocomms/twocomms_django_theme/templates/pages/admin_panel.html`
- Create: `twocomms/fable5/templates/fable5/size_grid_editor.html`
- Create: `twocomms/fable5/static/fable5/size-grids.js`
- Create: `twocomms/fable5/static/fable5/size-grids.css`
- Test: `twocomms/fable5/tests/test_size_grid_admin.py`

**Step 1: Write failing template/navigation tests**

Assert a staff-only “Розмірні сітки” navigation entry exists beside catalogs, list cards link to the editor, and anonymous users cannot open it.

**Step 2: Run RED**

```bash
$PYTHON manage.py test fable5.tests.test_size_grid_admin --settings=test_settings -v 2
```

**Step 3: Use the frontend-design skill for the UI brief and implementation**

The brief must require:

- dark TwoComms/Fable visual system;
- catalog/garment/fit filter chips;
- interactive rows and measurement columns, never raw JSON;
- drag reorder;
- inline validation;
- duplicate/archive actions;
- actual PDP renderer preview at desktop and 375 px;
- keyboard and screen-reader operation;
- no emoji as primary icons;
- reduced-motion support.

**Step 4: Integrate the produced application UI**

Use the existing Django template/static structure, not standalone HTML. Add cache-busted assets.

**Step 5: Run GREEN**

```bash
$PYTHON manage.py test fable5.tests.test_size_grid_admin fable5.tests.test_size_grid_api --settings=test_settings -v 2
node --check twocomms/fable5/static/fable5/size-grids.js
```

**Step 6: Commit**

```bash
git add twocomms/storefront/views/admin.py twocomms/twocomms_django_theme/templates/pages/admin_panel.html twocomms/fable5/templates/fable5/size_grid_editor.html twocomms/fable5/static/fable5 twocomms/fable5/tests
git commit -m "feat(admin): add interactive size-grid workspace"
```

---

### Task 7: Add transactional product save, revision, and readiness validation

**Files:**
- Modify: `twocomms/fable5/views.py`
- Create: `twocomms/fable5/services/product_save.py`
- Modify: `twocomms/fable5/urls.py`
- Test: `twocomms/fable5/tests/test_product_save_v2.py`

**Step 1: Write failing transaction tests**

Cover:

- expected revision success increments revision once;
- stale revision returns HTTP 409 without writes;
- idempotency key retry returns the original result;
- invalid nested variant rolls back product, fit, and size assignments;
- published fit without grid is rejected;
- published fit with zero effective sizes is rejected;
- draft may remain incomplete;
- legacy product without Fable data is not modified by merely opening it.

**Step 2: Run RED**

```bash
$PYTHON manage.py test fable5.tests.test_product_save_v2 --settings=test_settings -v 2
```

**Step 3: Implement a validated payload boundary**

Parse and normalize the complete structural payload before entering mutation code. Return issues shaped like:

```json
{"code":"missing_size_grid","severity":"error","section":"sizes","target":"fit=oversize","label":"Оберіть сітку для оверсайз"}
```

**Step 4: Implement atomic save and revision lock**

Use `select_for_update()` on `ProductEditorState` inside `transaction.atomic()`. Do not use broad exception swallowing. Log unexpected exceptions with request ID.

**Step 5: Run GREEN**

```bash
$PYTHON manage.py test fable5.tests.test_product_save_v2 --settings=test_settings -v 2
```

**Step 6: Commit**

```bash
git add twocomms/fable5/views.py twocomms/fable5/urls.py twocomms/fable5/services/product_save.py twocomms/fable5/tests/test_product_save_v2.py
git commit -m "feat(fable5): save product structures atomically with revisions"
```

---

### Task 8: Extend variant, combination, duplication, and bootstrap APIs

**Files:**
- Modify: `twocomms/fable5/views.py`
- Create: `twocomms/fable5/views_variants.py`
- Modify: `twocomms/fable5/urls.py`
- Test: `twocomms/fable5/tests/test_variant_api_v2.py`

**Step 1: Write failing API tests**

Cover:

- save color i18n;
- save fit-wide i18n;
- save exact color × fit i18n;
- blank override inherits rather than overwrites fallback rows;
- duplicate variant excludes photos;
- duplicate product creates draft with collision-safe slug;
- reorder variants;
- ownership rejection across products;
- bootstrap exposes effective values plus source badges;
- bootstrap exposes garment axes, assigned grids, effective size states, print, cover, readiness, and revision.

**Step 2: Run RED**

```bash
$PYTHON manage.py test fable5.tests.test_variant_api_v2 --settings=test_settings -v 2
```

**Step 3: Implement endpoints and serializers**

Keep serializer functions in service modules; prevent `views.py` from growing further. Preserve existing endpoint request shapes for backward compatibility.

**Step 4: Run GREEN**

```bash
$PYTHON manage.py test fable5.tests.test_variant_api_v2 --settings=test_settings -v 2
```

**Step 5: Commit**

```bash
git add twocomms/fable5/views.py twocomms/fable5/views_variants.py twocomms/fable5/urls.py twocomms/fable5/tests/test_variant_api_v2.py
git commit -m "feat(fable5): add inherited combination variant APIs"
```

---

### Task 9: Correct print linking and compatibility scope

**Files:**
- Create: `twocomms/fable5/views_prints.py`
- Create: `twocomms/fable5/services/prints.py`
- Modify: `twocomms/fable5/urls.py`
- Test: `twocomms/fable5/tests/test_print_api.py`

**Step 1: Write failing tests**

Assert:

- print search reads warehouse ORM without HTTP;
- changing compatibility on product A cannot affect product B using the same print;
- `Print.garment_fit` produces a warning default, not a global mutation;
- invalid print IDs and cross-object references are rejected;
- storage detail URL is built from configured host;
- no selected print remains a warning, not a blocker.

**Step 2: Run RED**

```bash
$PYTHON manage.py test fable5.tests.test_print_api --settings=test_settings -v 2
```

**Step 3: Implement product-scoped compatibility**

Use `ProductPrintLink` + `ProductPrintCompatibility`. Do not implement the supplied global `PrintGarmentCompatibility` behavior.

**Step 4: Run GREEN and commit**

```bash
$PYTHON manage.py test fable5.tests.test_print_api --settings=test_settings -v 2
git add twocomms/fable5/views_prints.py twocomms/fable5/services/prints.py twocomms/fable5/urls.py twocomms/fable5/tests/test_print_api.py
git commit -m "feat(fable5): scope print compatibility per product"
```

---

### Task 10: Harden media upload, localized alts, cover, and URL import

**Files:**
- Modify: `twocomms/fable5/views.py`
- Create: `twocomms/fable5/views_media.py`
- Create: `twocomms/fable5/services/image_import.py`
- Create: `twocomms/fable5/services/covers.py`
- Modify: `twocomms/fable5/urls.py`
- Test: `twocomms/fable5/tests/test_media_v2.py`

**Step 1: Write failing media tests**

Cover:

- localized alt upsert and fallback;
- color/product reorder ownership;
- cover from upload/product image/color image;
- source file deletion leaves copied cover alive;
- optimization dispatch occurs after commit;
- foreign product image cannot become cover;
- invalid MIME, decompression bomb, and over-limit upload rejection;
- HTTPS-only URL import;
- localhost, private, loopback, link-local, metadata IP, and redirect-to-private rejection;
- bounded size and timeout.

**Step 2: Run RED**

```bash
$PYTHON manage.py test fable5.tests.test_media_v2 --settings=test_settings -v 2
```

**Step 3: Implement hardened services**

Perform DNS validation before each outbound hop. Stream into a bounded temporary file. Verify using Pillow. Never trust only the HTTP content type.

Use Django storage APIs for cover copy; do not assume local filesystem paths.

**Step 4: Implement optimization status endpoint**

Return deterministic stages and stop polling after two minutes on the client. Missing optional AVIF support must not fail the whole image.

**Step 5: Run GREEN and commit**

```bash
$PYTHON manage.py test fable5.tests.test_media_v2 --settings=test_settings -v 2
git add twocomms/fable5/views.py twocomms/fable5/views_media.py twocomms/fable5/services twocomms/fable5/urls.py twocomms/fable5/tests/test_media_v2.py
git commit -m "feat(fable5): harden media and canonical cover workflow"
```

---

### Task 11: Build product list and editor design shell

**Files:**
- Create: `twocomms/fable5/templates/fable5/product_list.html`
- Modify: `twocomms/fable5/templates/fable5/editor.html`
- Create: `twocomms/fable5/static/fable5/tokens.css`
- Modify: `twocomms/fable5/static/fable5/editor.css`
- Create: `twocomms/fable5/static/fable5/core.js`
- Create: `twocomms/fable5/static/fable5/ui.js`
- Modify: `twocomms/fable5/static/fable5/editor.js`
- Modify: `twocomms/fable5/views.py`
- Modify: `twocomms/fable5/urls.py`
- Test: `twocomms/fable5/tests/test_editor_shell_v2.py`

**Step 1: Write failing shell tests**

Assert:

- `/admin-panel/fable5/` is the product list;
- new/edit routes remain compatible;
- list search/filter/health payload is staff-only;
- editor uses `json_script` only;
- SVG icon buttons have accessible names;
- sidebar sections and readiness targets exist;
- reduced-motion CSS exists;
- mobile chip navigation exists;
- legacy admin links still work.

**Step 2: Run RED**

```bash
$PYTHON manage.py test fable5.tests.test_editor_shell_v2 --settings=test_settings -v 2
```

**Step 3: Use frontend-design skill**

Create a production-app brief based on the approved design. Preserve TwoComms dark surfaces, restrained violet accent, strong hierarchy, dense-but-calm admin ergonomics, and no decorative animation that slows data entry.

**Step 4: Integrate shell and module boundary**

Use plain deferred scripts with one `window.Fable5` namespace if ES modules conflict with existing CSP/static behavior. `core.js` owns state/API/dirty/revision; `ui.js` owns dialog/toast/focus/language controls. Keep `editor.js` as compatibility bootstrap until all behavior moves.

**Step 5: Run GREEN and syntax checks**

```bash
$PYTHON manage.py test fable5.tests.test_editor_shell_v2 --settings=test_settings -v 2
node --check twocomms/fable5/static/fable5/core.js
node --check twocomms/fable5/static/fable5/ui.js
node --check twocomms/fable5/static/fable5/editor.js
```

**Step 6: Commit**

```bash
git add twocomms/fable5
git commit -m "feat(fable5): redesign product list and editor shell"
```

---

### Task 12: Implement the color × option matrix and interactive size controls

**Files:**
- Create: `twocomms/fable5/static/fable5/variants.js`
- Create: `twocomms/fable5/static/fable5/sizes.js`
- Modify: `twocomms/fable5/static/fable5/editor.css`
- Modify: `twocomms/fable5/templates/fable5/editor.html`
- Test: `twocomms/fable5/tests/test_editor_matrix.py`

**Step 1: Write failing markup/behavior contract tests**

Assert stable markers for:

- color rows and fit columns;
- exact-combination drawer;
- inherited-source badges;
- `aria-pressed` size chips;
- base-vs-color scope switch;
- assigned-grid selector per enabled fit;
- keyboard navigation and focus return;
- bulk apply/reset actions.

**Step 2: Run RED**

```bash
$PYTHON manage.py test fable5.tests.test_editor_matrix --settings=test_settings -v 2
```

**Step 3: Implement matrix rendering from state**

Do not duplicate hidden DOM forms for every cell. Render the active drawer from state, keep unsaved drafts keyed by combination, and collect them in global save.

**Step 4: Implement tactile size chips**

States:

```text
enabled / disabled / stock-warning / inherited-lock
```

Click and keyboard Space/Enter toggle the active scope. Product scope saves `ProductSizeRule`; color scope saves `VariantSizeRule`.

**Step 5: Run GREEN and JS checks**

```bash
$PYTHON manage.py test fable5.tests.test_editor_matrix --settings=test_settings -v 2
node --check twocomms/fable5/static/fable5/variants.js
node --check twocomms/fable5/static/fable5/sizes.js
```

**Step 6: Commit**

```bash
git add twocomms/fable5/static/fable5 twocomms/fable5/templates/fable5/editor.html twocomms/fable5/tests/test_editor_matrix.py
git commit -m "feat(fable5): add variant matrix and size toggles"
```

---

### Task 13: Complete media, SEO, language, draft, preview, and health interactions

**Files:**
- Create: `twocomms/fable5/static/fable5/media.js`
- Create: `twocomms/fable5/static/fable5/seo.js`
- Create: `twocomms/fable5/static/fable5/prints.js`
- Create: `twocomms/fable5/static/fable5/list.js`
- Modify: `twocomms/fable5/static/fable5/editor.js`
- Modify: `twocomms/fable5/templates/fable5/editor.html`
- Modify: `twocomms/fable5/views.py`
- Test: `twocomms/fable5/tests/test_editor_interactions.py`

**Step 1: Write failing contract tests for supplied top-10 requirements**

Cover markers and server behavior for:

- SEO template draft and Google preview;
- readiness score and clickable issues;
- local/server autosave recovery;
- live card/PDP preview;
- product/variant duplication;
- live validation;
- clipboard image paste and URL import;
- Ctrl+S, Alt+section, Escape, and `?` help;
- upload/optimization progress;
- list health filters.

**Step 2: Run RED**

```bash
$PYTHON manage.py test fable5.tests.test_editor_interactions --settings=test_settings -v 2
```

**Step 3: Implement language and SEO behavior**

All language tabs keep values in state. “Copy from Ukrainian” fills an editable draft. SEO generation fills empty fields by default and requires explicit confirmation to overwrite non-empty fields.

**Step 4: Implement upload queue and cover picker**

Limit concurrent XHR uploads to three. Cover picker groups every product/color image and visibly marks the selected source. Bounded status polling stops cleanly.

**Step 5: Implement drafts and conflicts**

Local snapshot after idle; server draft after longer idle. On 409, show editor/time and offer reload or duplicate draft. Never auto-merge rich text silently.

**Step 6: Run GREEN and syntax checks**

```bash
$PYTHON manage.py test fable5.tests.test_editor_interactions --settings=test_settings -v 2
for f in twocomms/fable5/static/fable5/*.js; do node --check "$f"; done
```

**Step 7: Commit**

```bash
git add twocomms/fable5
git commit -m "feat(fable5): complete guided product editing workflow"
```

---

### Task 14: Render comparative size grids and availability on the PDP

**Files:**
- Modify: `twocomms/storefront/views/product.py`
- Modify: `twocomms/twocomms_django_theme/templates/pages/product_detail.html`
- Create: `twocomms/twocomms_django_theme/templates/partials/product_size_grid_compare.html`
- Modify: relevant PDP CSS/JS files discovered from the template includes
- Modify: `twocomms/storefront/views/cart.py`
- Test: `twocomms/storefront/tests/test_product_size_guides.py`
- Test: `twocomms/storefront/tests/test_fable5_size_availability.py`

**Step 1: Write failing public tests**

Assert:

- classic and oversize assigned grids are both present in context;
- compare control is directly under fit controls;
- selected fit is emphasized but other grid remains visible;
- disabled sizes have correct accessible state and are absent from purchasable options;
- cart rejects a disabled fit/size/color combination server-side;
- legacy product without assignments renders the old single-grid block unchanged;
- mobile dialog markup has focus and Escape hooks.

**Step 2: Run RED**

```bash
$PYTHON manage.py test storefront.tests.test_product_size_guides storefront.tests.test_fable5_size_availability --settings=test_settings -v 2
```

**Step 3: Extend PDP context without broad try/except**

Call a stable Fable service that safely returns an empty comparison for no data. Catch only expected lookup/configuration exceptions inside the service.

**Step 4: Implement the comparison UI**

Desktop: side-by-side cards for two fits. Mobile: stacked or segmented, with a clear “both” state. Reuse the same normalized row renderer as the grid preview.

**Step 5: Enforce availability in cart**

Client disabling is not security or correctness. Validate chosen fit/size/color through `resolve_effective_sizes` before session/cart mutation.

**Step 6: Run GREEN and commit**

```bash
$PYTHON manage.py test storefront.tests.test_product_size_guides storefront.tests.test_fable5_size_availability --settings=test_settings -v 2
git add twocomms/storefront twocomms/twocomms_django_theme
git commit -m "feat(storefront): compare fit grids and enforce sizes"
```

---

### Task 15: Apply variant content to public SEO, OG, alts, FAQ, and schema

**Files:**
- Modify: `twocomms/storefront/views/product.py`
- Modify: `twocomms/storefront/services/variant_meta.py`
- Modify: product-detail SEO/schema template tags or services discovered by tests
- Modify: `twocomms/twocomms_django_theme/templates/pages/product_detail.html`
- Test: `twocomms/storefront/tests/test_fable5_variant_seo.py`

**Step 1: Write failing SEO contract tests**

Cover UK/RU/EN for:

- base product;
- color only;
- fit only;
- color × fit;
- exact override precedence;
- visible H1/title consistency;
- canonical behavior: color × fit self-canonical, size combinations consolidate;
- localized alt fallback;
- visible variant FAQ and matching FAQ schema;
- `ProductGroup`/`hasVariant` with stable URLs and offers;
- no Fable data preserving existing output.

**Step 2: Run RED**

```bash
$PYTHON manage.py test storefront.tests.test_fable5_variant_seo --settings=test_settings -v 2
```

**Step 3: Resolve content once in the view/service layer**

Avoid independent template fallbacks that can make title, H1, and schema disagree. Pass one resolved merchandising context to templates and schema builders.

**Step 4: Implement structured data carefully**

Emit meaningful fit/color variants, not size URL crawl permutations. Use current availability and assigned sizes. Preserve current review/rating behavior.

**Step 5: Run GREEN and existing SEO regressions**

```bash
$PYTHON manage.py test storefront.tests.test_fable5_variant_seo storefront.tests.test_seo_regressions --settings=test_settings -v 2
```

**Step 6: Commit**

```bash
git add twocomms/storefront twocomms/twocomms_django_theme
git commit -m "feat(seo): publish localized color and fit variants"
```

---

### Task 16: Integrate orders, Telegram, thermo, and feeds without identifier churn

**Files:**
- Modify: `twocomms/fable5/services.py`
- Modify: `twocomms/orders/telegram_notifications.py`
- Modify: order admin/email templates or services discovered by targeted search
- Modify: `twocomms/storefront/services/marketplace_feeds.py`
- Modify: relevant PDP swatch template/CSS
- Test: `twocomms/fable5/tests/test_order_context.py`
- Test: `twocomms/orders/tests/test_fable5_notifications.py`
- Test: `twocomms/storefront/tests/test_marketplace_feeds.py`

**Step 1: Write failing order-context tests**

Expected context includes resolved variant name, color, fit, size, print name/storage URL, and thermo flag. Missing Fable rows must return current text safely.

**Step 2: Write failing feed safety tests**

Assert:

- cover is first image;
- current offer IDs stay unchanged by merely adding Fable metadata;
- fit-specific content may enrich title/description only under an explicit offer policy;
- no duplicate URLs/images;
- disabled sizes are excluded;
- existing feed snapshots remain stable where behavior is unchanged.

**Step 3: Run RED**

```bash
$PYTHON manage.py test fable5.tests.test_order_context orders.tests.test_fable5_notifications storefront.tests.test_marketplace_feeds --settings=test_settings -v 2
```

**Step 4: Implement one shared order-line resolver**

Use it from Telegram, email, and admin detail. Do not duplicate query logic. Prefetch Fable relations for an order to avoid N+1 queries.

**Step 5: Add thermo presentation**

Use accessible text plus visual flame treatment. Do not rely on the emoji alone to convey state.

**Step 6: Run GREEN and commit**

```bash
$PYTHON manage.py test fable5.tests.test_order_context orders.tests.test_fable5_notifications storefront.tests.test_marketplace_feeds --settings=test_settings -v 2
git add twocomms/fable5 twocomms/orders twocomms/storefront twocomms/twocomms_django_theme
git commit -m "feat(fable5): carry variant details through orders and feeds"
```

---

### Task 17: Complete automated, accessibility, and browser verification

**Files:**
- Modify: tests as required by verified defects only
- Create: `docs/qa/fable5-v2-release-checklist.md`

**Step 1: Run the focused full suite**

```bash
$PYTHON manage.py test fable5 storefront.tests.test_product_size_guides storefront.tests.test_fable5_size_availability storefront.tests.test_fable5_variant_seo storefront.tests.test_marketplace_feeds orders.tests.test_fable5_notifications --settings=test_settings
$PYTHON manage.py check --settings=test_settings
$PYTHON manage.py makemigrations --check --dry-run --settings=test_settings
```

Expected: zero failures, no pending migrations.

**Step 2: Run static verification**

```bash
for f in twocomms/fable5/static/fable5/*.js; do node --check "$f"; done
git diff --check origin/main...HEAD
```

**Step 3: Run browser QA against a local/server test session**

Verify with screenshots at 1440 px and 375 px:

1. create draft product;
2. choose print;
3. enable classic and oversize;
4. assign separate grids;
5. disable product-wide S for classic;
6. disable black/oversize XXL;
7. create black and coyote colors;
8. add exact black × oversize SEO;
9. upload/reorder media and select cover;
10. publish without reload;
11. compare both grids on PDP;
12. prove disabled choices cannot be added to cart;
13. edit a legacy product;
14. test network interruption/draft recovery;
15. confirm no console errors.

**Step 4: Accessibility checks**

Keyboard-only: all sections, dialogs, matrix cells, size chips, language tabs, and cover picker. Confirm visible focus, focus restoration, Escape, correct roles, and reduced motion.

**Step 5: Request independent code/design review**

Use `superpowers:requesting-code-review` for the complete branch diff. Use the frontend-design evaluation loop for the final product editor, grid editor, and PDP comparison. Resolve all Critical and Important findings.

**Step 6: Write QA evidence and commit**

Record exact test counts, screenshots, known pre-existing warnings, and any intentionally deferred out-of-scope item.

```bash
git add docs/qa/fable5-v2-release-checklist.md
git commit -m "docs(qa): record Fable 5 v2 release verification"
```

---

### Task 18: Merge to main, push, migrate, deploy, and live-verify

**Files:**
- No new implementation files; deployment only.

**Step 1: Final branch verification**

```bash
git status --short
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
git diff --check origin/main...HEAD
```

Expected: only intended Fable/storefront/order/theme/docs changes.

**Step 2: Use finishing-a-development-branch with the user's already stated integration intent**

The user requested commit + push + deploy. Merge the feature branch into current `main` without including unrelated files from the original checkout. Re-run the focused suite on the merged result.

**Step 3: Push main**

```bash
git push origin main
git rev-list --left-right --count HEAD...origin/main
```

Expected: `0 0`.

**Step 4: Create production backup and preflight**

Over SSH, without persisting credentials:

- record server HEAD and dirty status;
- create a timestamped database backup or provider-supported snapshot;
- inspect migration plan;
- run `manage.py check`;
- confirm relevant table engines again;
- stop if server has unexpected uncommitted changes or schema drift.

**Step 5: Deploy**

```bash
git pull --ff-only origin main
python manage.py migrate fable5
python manage.py migrate
python manage.py check
python manage.py collectstatic --noinput
python manage.py compress --force
touch tmp/restart.txt
```

**Step 6: Live verification**

Verify:

- server HEAD equals pushed HEAD;
- all Fable migrations applied;
- `/healthz/` returns 200;
- public home/catalog/PDP return 200;
- anonymous Fable editor/API remain 403;
- authenticated staff list/editor/grid library return 200;
- a safe staff test product can save draft assignments;
- legacy PDP without Fable data remains unchanged;
- assigned classic/oversize grids both render;
- cover/static assets return 200 and current cache-bust markers;
- Google/Meta feed generation completes and cover is first;
- error logs show no new traceback after restart.

**Step 7: Report evidence**

Report commit SHA, push parity, migration IDs, test counts, live status codes, browser scenarios, and any remaining non-blocking warning. Do not claim completion without fresh output for every gate.

---

## Resume checklist for another agent

If context is lost, the next agent must:

1. Open this plan and the design document.
2. Run `git status --short --branch` and `git log --oneline -10`.
3. Identify the last completed task by commit history and test evidence.
4. Do not repeat completed migrations or overwrite unrelated files.
5. Run the focused tests for the previous completed task before continuing.
6. Continue at the first incomplete RED step.
7. Re-check production schema facts if migration definitions changed.
8. Keep secrets out of files and tool-visible logs.
9. Do not push/deploy until Tasks 1–17 and the final review are complete.
