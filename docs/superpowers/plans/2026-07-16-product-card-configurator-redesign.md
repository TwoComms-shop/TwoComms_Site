# Product Card Configurator Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the Fable5 option engine and ship a responsive PDP configurator with restock subscriptions, multi-print editing, generic option pricing, and mobile image swipes.

**Architecture:** Extend the deployed Fable5 `GarmentFlow` and sparse option profiles rather than adding a parallel variant system. Carry one normalized option dictionary and one authoritative resolved price through PDP, cart, orders, notifications, and payment; keep `warehouse.Print.default_products` as the print source of truth and add a focused `RestockSubscription` workflow.

**Tech Stack:** Django 5.2, MySQL/MyISAM-compatible relations, vanilla JavaScript, Django templates, CSS, Telegram Bot webhook, Django TestCase, Node test runner, in-app Browser QA.

---

## File map

- `twocomms/fable5/services.py`: generic option normalization, public option payload, availability, and authoritative price resolution.
- `twocomms/fable5/views.py`: editor bootstrap/save for garment axes, product option profiles, and warehouse prints.
- `twocomms/fable5/static/fable5/editor.js`: generic option and print editing controls.
- `twocomms/storefront/models.py`: restock request persistence.
- `twocomms/storefront/services/restock.py`: validation, deduplication, Telegram/admin notification, and customer delivery.
- `twocomms/storefront/views/product.py`: option and unavailable-size PDP context.
- `twocomms/storefront/views/restock.py`: CSRF-protected public restock endpoints.
- `twocomms/accounts/telegram_verify_views.py` and `twocomms/accounts/telegram_bot.py`: `restock` verification purpose and webhook completion.
- `twocomms/storefront/views/cart.py`, `twocomms/orders/models.py`, order/payment services: normalized option persistence and price parity.
- `twocomms/twocomms_django_theme/templates/pages/product_detail.html`: selector redesign, modal, block order, and gallery status.
- `twocomms/twocomms_django_theme/static/js/product-detail.js`: generic selector state, restock modal, and swipe gallery.
- `twocomms/twocomms_django_theme/static/css/product-detail.css`: responsive visual system.
- `twocomms/storefront/admin.py`: restock lead administration.

### Task 1: Generic option contract and authoritative pricing

**Files:**
- Modify: `twocomms/fable5/services.py`
- Modify: `twocomms/fable5/content_resolution.py`
- Modify: `twocomms/storefront/services/catalog_helpers.py`
- Test: `twocomms/fable5/tests/test_generic_options.py`

- [ ] **Step 1: Write failing option-resolution tests**

```python
def test_hoodie_lining_exposes_disabled_no_fleece(self):
    payload = product_option_context(self.product, variant=self.variant)
    lining = payload["axes"][0]
    self.assertEqual(lining["code"], "lining")
    self.assertTrue(lining["choices"][0]["is_enabled"])
    self.assertFalse(lining["choices"][1]["is_enabled"])

def test_option_price_uses_exact_combination_before_product_option(self):
    context = variant_public_context(
        self.variant,
        option_values={"fit": "oversize"},
    )
    self.assertEqual(context["final_price"], Decimal("1290"))
    self.assertEqual(context["price_delta_reason"], "Oversize blank")
```

- [ ] **Step 2: Verify RED**

Run: `python manage.py test fable5.tests.test_generic_options --settings=test_settings --keepdb`

Expected: import failure for `product_option_context` or assertion failure because `variant_public_context` accepts only `fit_code`.

- [ ] **Step 3: Implement the generic resolver**

Add `normalize_public_option_values(product, raw)`, `product_option_context(product, variant=None, option_values=None, lang="uk")`, `variant_allows_options(variant, option_values)`, and `variant_public_context(..., option_values=None)` while mapping legacy `fit_code` to `{"fit": code}`. Choice payloads must include `code`, `label`, `description`, `is_enabled`, `is_default`, `reason`, `price_delta`, `price_delta_reason`, and `icon`.

- [ ] **Step 4: Verify GREEN and compatibility**

Run: `python manage.py test fable5.tests.test_generic_options fable5.tests.test_content_resolution storefront.tests.test_fable5_variant_merchandising --settings=test_settings --keepdb`

Expected: all tests pass and existing fit-only calls retain identical results.

### Task 2: Persist generic selections through cart and orders

**Files:**
- Modify: `twocomms/storefront/views/cart.py`
- Modify: `twocomms/orders/models.py`
- Create: `twocomms/orders/migrations/0049_orderitem_option_values_and_more.py`
- Modify: `twocomms/twocomms_django_theme/static/js/main.js`
- Modify: `twocomms/orders/telegram_notifications.py`
- Modify: `twocomms/orders/email_receipt.py`
- Test: `twocomms/storefront/tests/test_generic_option_cart.py`

- [ ] **Step 1: Write failing cart and order tests**

```python
def test_add_to_cart_rejects_disabled_option(self):
    response = self.client.post("/cart/add/", {
        "product_id": self.product.id,
        "size": "M",
        "color_variant_id": self.variant.id,
        "option_values": '{"lining":"no_fleece"}',
    })
    self.assertEqual(response.status_code, 400)

def test_add_to_cart_stores_normalized_options_and_resolved_price(self):
    response = self.client.post("/cart/add/", {
        "product_id": self.product.id,
        "size": "M",
        "color_variant_id": self.variant.id,
        "option_values": '{"fit":"oversize"}',
    })
    self.assertTrue(response.json()["ok"])
    item = next(iter(self.client.session["cart"].values()))
    self.assertEqual(item["option_values"], {"fit": "oversize"})
    self.assertEqual(item["fit_option_code"], "oversize")
```

- [ ] **Step 2: Verify RED**

Run: `python manage.py test storefront.tests.test_generic_option_cart --settings=test_settings --keepdb`

Expected: disabled lining is accepted or `option_values` is absent.

- [ ] **Step 3: Implement normalized cart selection and snapshots**

Parse a JSON object with a strict size/token limit, normalize it through Fable5, reject unavailable choices, compute `_effective_item_price` from the same option values, include a stable option key in the cart key, and store `option_values` plus localized `option_labels`. Add JSON fields with `default=dict` to `OrderItem` and `DropshipperOrderItem`; keep legacy fit snapshots populated.

- [ ] **Step 4: Update browser payload and order displays**

Serialize checked `[data-product-option-axis]` controls into the `option_values` form field. Render option labels in the mini-cart, order Telegram message, and email receipt without duplicating the legacy fit line.

- [ ] **Step 5: Verify GREEN and payment regressions**

Run: `python manage.py test storefront.tests.test_generic_option_cart storefront.tests.test_fable5_variant_merchandising orders --settings=test_settings --keepdb`

Expected: option tests pass and checkout/payment totals remain green.

### Task 3: Restock subscription and Telegram verification

**Files:**
- Modify: `twocomms/storefront/models.py`
- Create: `twocomms/storefront/migrations/0084_restocksubscription.py`
- Create: `twocomms/storefront/services/restock.py`
- Create: `twocomms/storefront/views/restock.py`
- Modify: `twocomms/storefront/urls.py`
- Modify: `twocomms/storefront/admin.py`
- Modify: `twocomms/accounts/models.py`
- Modify: `twocomms/accounts/telegram_verify_views.py`
- Modify: `twocomms/accounts/telegram_bot.py`
- Create: `twocomms/accounts/migrations/0030_alter_telegram_verification_purpose.py`
- Test: `twocomms/storefront/tests/test_restock_subscriptions.py`
- Test: `twocomms/accounts/tests_restock_telegram.py`

- [ ] **Step 1: Write failing public endpoint tests**

```python
def test_email_subscription_is_idempotent(self):
    payload = self.valid_payload(channel="email", contact="Buyer@Example.com")
    first = self.client.post(self.url, payload, content_type="application/json")
    second = self.client.post(self.url, payload, content_type="application/json")
    self.assertEqual(first.status_code, 201)
    self.assertEqual(second.status_code, 200)
    self.assertEqual(RestockSubscription.objects.count(), 1)

def test_telegram_verification_completes_restock_subscription(self):
    session = self.make_session(purpose="restock", metadata={"restock_id": self.request.id})
    self.bot._post_verify_purpose_action(session)
    self.request.refresh_from_db()
    self.assertEqual(self.request.telegram_user_id, session.telegram_user_id)
    self.assertEqual(self.request.status, RestockSubscription.Status.ACTIVE)
```

- [ ] **Step 2: Verify RED**

Run: `python manage.py test storefront.tests.test_restock_subscriptions accounts.tests_restock_telegram --settings=test_settings --keepdb`

Expected: model/module imports fail.

- [ ] **Step 3: Implement model, endpoint, and service**

Create channel/status choices, product/color FKs with `db_constraint=False` where required, `option_values`, `option_labels`, size, normalized contact, Telegram fields, request fingerprint, metadata, and notification timestamps. The endpoint validates product/color/size/options, applies cache-based rate limiting, ignores honeypot submissions, and creates or reuses an active fingerprint.

- [ ] **Step 4: Add Telegram purpose and completion hook**

Allow anonymous `restock` verification starts with a validated `restock_id` in metadata. On webhook contact, copy verified Telegram identity to the subscription, mark it active, send the user confirmation, and dispatch the structured admin alert without losing the record if sending fails.

- [ ] **Step 5: Verify GREEN and security boundaries**

Run: `python manage.py test storefront.tests.test_restock_subscriptions accounts.tests_restock_telegram accounts --settings=test_settings --keepdb`

Expected: validation, ownership, idempotency, throttling, and webhook tests pass.

### Task 4: Fable5 axes, surcharges, and multiple print links

**Files:**
- Modify: `twocomms/fable5/views.py`
- Modify: `twocomms/fable5/static/fable5/editor.js`
- Modify: `twocomms/fable5/static/fable5/editor.css`
- Create: `twocomms/fable5/migrations/0006_seed_hoodie_lining_profiles.py`
- Test: `twocomms/fable5/tests/test_editor_generic_options.py`
- Test: `twocomms/fable5/tests/test_editor_print_links.py`

- [ ] **Step 1: Write failing editor API tests**

```python
def test_bootstrap_exposes_flow_axes_and_selected_prints(self):
    payload = self.client.get(self.edit_url).context["bootstrap"]
    self.assertEqual(payload["product"]["option_axes"][0]["code"], "lining")
    self.assertCountEqual(payload["product"]["print_ids"], [self.print_a.id, self.print_b.id])

def test_product_save_updates_print_m2m_without_deleting_other_products(self):
    response = self.save({"id": self.product.id, "print_ids": [self.print_b.id]})
    self.assertTrue(response.json()["ok"])
    self.assertEqual(list(self.product.warehouse_default_prints.values_list("id", flat=True)), [self.print_b.id])
    self.assertTrue(self.print_a.default_products.filter(id=self.other_product.id).exists())
```

- [ ] **Step 2: Verify RED**

Run: `python manage.py test fable5.tests.test_editor_generic_options fable5.tests.test_editor_print_links --settings=test_settings --keepdb`

Expected: option axes and print IDs are absent.

- [ ] **Step 3: Implement bootstrap/save contracts**

Resolve the category's active `GarmentFlow`, serialize product option profiles, expose all active warehouse prints with thumbnails, and transactionally update `Print.default_products` via the product's reverse M2M manager. Preserve rows for other products and existing color combination translations.

- [ ] **Step 4: Implement editor controls**

Render each axis as an option table with enabled/default toggles, price delta, reason, and description. Render a searchable multi-select print list with selected count. Send `option_profiles` and `print_ids` in the existing product save payload.

- [ ] **Step 5: Verify GREEN and migration behavior**

Run: `python manage.py test fable5.tests --settings=test_settings --keepdb && python manage.py makemigrations --check --dry-run --settings=test_settings`

Expected: Fable5 tests pass and only committed migrations are detected.

### Task 5: PDP configurator, material story, and restock modal

**Files:**
- Modify: `twocomms/storefront/views/product.py`
- Modify: `twocomms/twocomms_django_theme/templates/pages/product_detail.html`
- Create: `twocomms/twocomms_django_theme/templates/partials/product_restock_modal.html`
- Modify: `twocomms/twocomms_django_theme/static/js/product-detail.js`
- Modify: `twocomms/twocomms_django_theme/static/css/product-detail.css`
- Create: `twocomms/twocomms_django_theme/static/js/product-detail.test.js`
- Test: `twocomms/storefront/tests/test_pdp_configurator.py`

- [ ] **Step 1: Write failing render and DOM behavior tests**

```python
def test_disabled_options_and_unavailable_sizes_stay_visible(self):
    html = self.client.get(self.url).content.decode()
    self.assertIn('data-product-option-choice="no_fleece"', html)
    self.assertIn('aria-disabled="true"', html)
    self.assertIn('data-restock-size="XXL"', html)

def test_material_story_is_not_repeated_as_premium_badge(self):
    html = self.client.get(self.thermo_url).content.decode()
    self.assertEqual(html.count('data-pdp-material-story'), 1)
    self.assertNotIn('data-generic-premium-fabric', html)
```

- [ ] **Step 2: Verify RED**

Run: `python manage.py test storefront.tests.test_pdp_configurator --settings=test_settings --keepdb`

Expected: disabled option and restock attributes are absent.

- [ ] **Step 3: Implement server-rendered selector and modal markup**

Render all option axes and all known sizes. Keep unavailable controls visible, separate purchase selection from restock actions, include a single data-driven material story, and add semantic SVG icons and accessible status labels.

- [ ] **Step 4: Implement interaction state**

On color/option changes, resolve the matching merchandising payload, update availability, price, title, story, surcharge badges, URL/share state, and add-to-cart option JSON. Implement the four-channel modal, Telegram verification polling, validation, focus restoration, and success/error states.

- [ ] **Step 5: Implement responsive styles**

Use stable selector dimensions, an auto-fit option grid, contained thermochromic badge, horizontal color/size rails, mobile one-column choices, modal safe-area padding, and `prefers-reduced-motion`. Keep radii at or below the existing design system values and avoid nested decorative cards.

- [ ] **Step 6: Verify GREEN and JS tests**

Run: `python manage.py test storefront.tests.test_pdp_configurator --settings=test_settings --keepdb && node --test twocomms/twocomms_django_theme/static/js/product-detail.test.js`

Expected: render and interaction tests pass.

### Task 6: Mobile swipe, content order, and regression cleanup

**Files:**
- Modify: `twocomms/twocomms_django_theme/static/js/product-detail.js`
- Modify: `twocomms/twocomms_django_theme/templates/pages/product_detail.html`
- Modify: `twocomms/twocomms_django_theme/static/css/product-detail.css`
- Test: `twocomms/storefront/tests/test_pdp_content_order.py`
- Test: `twocomms/twocomms_django_theme/static/js/product-detail.test.js`

- [ ] **Step 1: Write failing swipe and order tests**

```javascript
test('horizontal swipe advances while vertical intent does not', () => {
  assert.equal(resolveSwipe({ dx: -70, dy: 12 }), 1);
  assert.equal(resolveSwipe({ dx: -35, dy: 80 }), 0);
});
```

```python
def test_reviews_and_recommendations_precede_general_seo(self):
    html = self.client.get(self.url).content.decode()
    self.assertLess(html.index('id="product-reviews"'), html.index('data-general-product-seo'))
    self.assertLess(html.index('tc-recent-panel'), html.index('data-general-product-seo'))
```

- [ ] **Step 2: Verify RED**

Run: `node --test twocomms/twocomms_django_theme/static/js/product-detail.test.js && python manage.py test storefront.tests.test_pdp_content_order --settings=test_settings --keepdb`

Expected: `resolveSwipe` or content-order assertions fail.

- [ ] **Step 3: Implement swipe state and indicators**

Track current gallery index, pointer start/current coordinates, horizontal intent, and swipe suppression of click-to-zoom. Apply a 42px threshold, edge resistance, `touch-action: pan-y`, active thumbnail/dot synchronization, color-reset behavior, and reduced-motion fallback.

- [ ] **Step 4: Reorder content and remove duplicates**

Move reviews, similar products, and recently viewed above both SEO blocks; keep both SEO blocks inside the full-width PDP shell. Remove the duplicated `care_instructions` template output.

- [ ] **Step 5: Verify GREEN**

Run: `node --test twocomms/twocomms_django_theme/static/js/product-detail.test.js && python manage.py test storefront.tests.test_pdp_content_order --settings=test_settings --keepdb`

Expected: swipe and content order tests pass.

### Task 7: Full verification, merge, deploy, and live QA

**Files:**
- Verify all modified files and migrations.
- Do not stage unrelated files from the original dirty checkout.

- [ ] **Step 1: Run complete targeted and broad checks**

Run:

```bash
python manage.py test fable5.tests storefront.tests accounts orders --settings=test_settings --keepdb
python manage.py check --settings=test_settings
python manage.py makemigrations --check --dry-run --settings=test_settings
npm test
git diff --check
```

Expected: zero failures, no model drift, no whitespace errors.

- [ ] **Step 2: Run local rendered QA**

Start the Django server with fixture data, then use the in-app Browser to verify desktop, tablet, and phone viewports. Exercise color, fit, hoodie lining, disabled size, all four modal channels, Telegram start state, add-to-cart price, swipe, zoom, reviews/recommendations/SEO order, and console health. Save screenshots outside the repository and compare them with the five supplied reference images using `view_image`.

- [ ] **Step 3: Commit and integrate without unrelated changes**

Commit only planned files on `codex/pdp-configurator-redesign`, update original `main` from `origin/main`, and merge the feature branch. Confirm the original middleware/settings or package changes are unchanged unless separately committed by the user.

- [ ] **Step 4: Push and deploy**

Push `main`, then on production run `git pull --ff-only origin main`, `python manage.py migrate`, `python manage.py check`, `python manage.py collectstatic --noinput`, `python manage.py compress --force`, and `touch tmp/restart.txt`.

- [ ] **Step 5: Live verification**

Confirm production HEAD and migrations; query representative T-shirt and hoodie option contexts; verify print link counts are preserved; submit and remove a controlled restock test request; check live PDP HTTP 200, static asset versions, desktop/mobile screenshots, swipe/selector/modal behavior, add-to-cart total parity, and relevant console/network errors.
