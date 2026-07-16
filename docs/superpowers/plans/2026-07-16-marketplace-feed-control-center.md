# Marketplace Feed Control Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make orders the fast default admin landing and add a safe, polished control center for existing and custom marketplace feeds.

**Architecture:** Keep format-critical XML in a closed adapter registry, store editable feed profiles and per-product overrides in Django models, and apply resolved rules to normalized offers before adapter serialization. Canonical URLs fall back to registry defaults during rolling deploys; custom profiles publish through one guarded dynamic route.

**Tech Stack:** Django 5.2, MySQL-compatible migrations, Django templates/forms, Python `xml.etree.ElementTree`, existing marketplace feed services, Django `TestCase`.

---

## File Map

- Create `twocomms/storefront/services/feed_registry.py`: adapter metadata, defaults, reserved slugs, profile lookup, and builder dispatch.
- Create `twocomms/storefront/services/feed_profiles.py`: rule validation, inheritance, filtering, offer overrides, image-token handling, and admin health summaries.
- Create `twocomms/storefront/forms_feeds.py`: profile and per-product rule forms.
- Create `twocomms/storefront/views/feeds_admin.py`: staff mutations, duplication, validation, and manual regeneration.
- Create `twocomms/storefront/templates/partials/admin_feeds_section.html`: master-detail feed UI.
- Create `twocomms/storefront/migrations/0086_marketplace_feed_profiles.py`: schema and system-profile seed.
- Create `twocomms/storefront/tests/test_feed_admin.py`: admin, model, form, public route, and runtime coverage.
- Modify `twocomms/storefront/models.py`: `MarketplaceFeed` and `MarketplaceFeedProductRule`.
- Modify `twocomms/storefront/services/marketplace_feeds.py`: profile-aware offers/builders and Meta serializer.
- Modify `twocomms/storefront/management/commands/generate_instagram_feed.py`: delegate to the shared Meta builder.
- Modify `twocomms/storefront/management/commands/regenerate_feeds_if_dirty.py`: resolve profiles and report snapshot freshness.
- Modify `twocomms/storefront/views/static_pages.py`: canonical profile resolution and custom feed response.
- Modify `twocomms/storefront/views/admin.py`: orders default and feeds context.
- Modify `twocomms/storefront/urls.py`: admin feed actions and `/feeds/<slug>.xml`.
- Modify `twocomms/twocomms_django_theme/templates/pages/admin_panel.html`: nav order and feed partial include.
- Modify `twocomms/storefront/tests/test_marketplace_feeds.py`: adapter compatibility and snapshot regression coverage.

### Task 1: Fast Orders Landing

**Files:**
- Modify: `twocomms/storefront/views/admin.py`
- Modify: `twocomms/twocomms_django_theme/templates/pages/admin_panel.html`
- Test: `twocomms/storefront/tests/test_feed_admin.py`

- [ ] **Step 1: Write the failing default-section test**

```python
@patch("storefront.views.admin.build_admin_analytics_context")
def test_admin_panel_defaults_to_orders_without_loading_analytics(self, analytics):
    self.client.force_login(self.staff)
    response = self.client.get("/admin-panel/")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.context["section"], "orders")
    analytics.assert_not_called()
    self.assertContains(response, "Замовлення")
```

- [ ] **Step 2: Run the test and verify it fails because the section is `stats`**

Run: `cd twocomms && python manage.py test storefront.tests.test_feed_admin.FeedAdminLandingTests -v 2`

- [ ] **Step 3: Default `section` to `orders` and place Orders before Statistics in navigation**

```python
section = request.GET.get("section", "orders")
```

- [ ] **Step 4: Re-run the landing test and the existing analytics API tests**

Run: `cd twocomms && python manage.py test storefront.tests.test_feed_admin.FeedAdminLandingTests storefront.tests.test_admin_analytics_api -v 2`

### Task 2: Feed Profile Persistence and Validation

**Files:**
- Modify: `twocomms/storefront/models.py`
- Create: `twocomms/storefront/services/feed_profiles.py`
- Create: `twocomms/storefront/migrations/0086_marketplace_feed_profiles.py`
- Test: `twocomms/storefront/tests/test_feed_admin.py`

- [ ] **Step 1: Write failing tests for defaults, parent cycles, incompatible adapters, and product-rule uniqueness**

```python
def test_feed_rejects_parent_cycle(self):
    parent = MarketplaceFeed.objects.create(name="Base", slug="base", adapter="google")
    child = MarketplaceFeed.objects.create(name="Child", slug="child", adapter="google", parent=parent)
    parent.parent = child
    with self.assertRaises(ValidationError):
        parent.full_clean()

def test_feed_rejects_parent_with_another_adapter(self):
    parent = MarketplaceFeed.objects.create(name="Google", slug="google", adapter="google")
    child = MarketplaceFeed(name="Prom", slug="prom-copy", adapter="prom", parent=parent)
    with self.assertRaises(ValidationError):
        child.full_clean()
```

- [ ] **Step 2: Run model tests and verify they fail because models do not exist**

Run: `cd twocomms && python manage.py test storefront.tests.test_feed_admin.FeedProfileModelTests -v 2`

- [ ] **Step 3: Implement models with whitelisted choices, JSON rules, cycle validation, and exact constraints**

```python
class MarketplaceFeed(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80, unique=True)
    system_key = models.CharField(max_length=40, unique=True, null=True, blank=True)
    adapter = models.CharField(max_length=24, choices=MarketplaceFeedAdapter.choices)
    language = models.CharField(max_length=8, choices=MarketplaceFeedLanguage.choices, default="uk")
    is_active = models.BooleanField(default=True)
    is_system = models.BooleanField(default=False)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    description = models.CharField(max_length=240, blank=True)
    rules = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
```

- [ ] **Step 4: Add `MarketplaceFeedProductRule` with tri-state inclusion/availability and validated image tokens**

```python
class MarketplaceFeedProductRule(models.Model):
    feed = models.ForeignKey(MarketplaceFeed, on_delete=models.CASCADE, related_name="product_rules")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="marketplace_feed_rules")
    inclusion = models.CharField(max_length=12, choices=FeedRuleInclusion.choices, default="inherit")
    availability = models.CharField(max_length=12, choices=FeedRuleAvailability.choices, default="inherit")
    quantity = models.PositiveIntegerField(null=True, blank=True)
    image_tokens = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("feed", "product"), name="uniq_feed_product_rule")]
```

- [ ] **Step 5: Add a data migration that seeds seven system profiles without relying on production primary keys**

```python
SYSTEM_FEEDS = (
    ("google", "Google Merchant", "google", "uk"),
    ("meta", "Instagram / Meta", "meta", "uk"),
    ("rozetka", "Rozetka", "rozetka", "uk"),
    ("kasta", "Kasta", "kasta", "uk_ru"),
    ("buyme", "BuyMe", "buyme", "uk"),
    ("prom", "Prom.ua", "prom", "uk"),
    ("bezzet", "Bezzet", "bezzet", "uk"),
)
```

- [ ] **Step 6: Run model tests, migration consistency, and Django checks**

Run: `cd twocomms && python manage.py test storefront.tests.test_feed_admin.FeedProfileModelTests -v 2 && python manage.py makemigrations --check --dry-run && python manage.py check`

### Task 3: Registry and Rule Resolution

**Files:**
- Create: `twocomms/storefront/services/feed_registry.py`
- Modify: `twocomms/storefront/services/feed_profiles.py`
- Test: `twocomms/storefront/tests/test_feed_admin.py`

- [ ] **Step 1: Write failing tests for registry coverage, reserved slugs, inherited merge order, and invalid rule keys**

```python
def test_registry_covers_every_production_feed(self):
    self.assertEqual(set(FEED_ADAPTERS), {"google", "meta", "rozetka", "kasta", "buyme", "prom", "bezzet"})

def test_child_rules_override_parent_without_losing_defaults(self):
    rules = resolve_feed_rules(self.child)
    self.assertEqual(rules["availability"]["mode"], "force_in_stock")
    self.assertEqual(rules["images"]["max_count"], 4)
```

- [ ] **Step 2: Run tests and verify the missing registry/resolver failures**

Run: `cd twocomms && python manage.py test storefront.tests.test_feed_admin.FeedRuleResolutionTests -v 2`

- [ ] **Step 3: Implement immutable adapter definitions and versioned rule normalization**

```python
@dataclass(frozen=True)
class FeedAdapterDefinition:
    key: str
    label: str
    format_label: str
    languages: tuple[str, ...]
    canonical_paths: tuple[str, ...]
    snapshot_name: str | None
    command_name: str | None
    default_rules: dict[str, object]
```

- [ ] **Step 4: Implement bounded parent resolution and deterministic deep merge**

```python
def resolve_feed_rules(feed):
    chain = []
    current = feed
    while current is not None:
        if current.pk in {item.pk for item in chain} or len(chain) >= 12:
            raise ValidationError("Неможливий цикл залежностей фідів.")
        chain.append(current)
        current = current.parent
    rules = deepcopy(FEED_ADAPTERS[feed.adapter].default_rules)
    for item in reversed(chain):
        rules = deep_merge_rules(rules, validate_feed_rules(item.rules))
    return rules
```

- [ ] **Step 5: Run rule tests**

Run: `cd twocomms && python manage.py test storefront.tests.test_feed_admin.FeedRuleResolutionTests -v 2`

### Task 4: Profile-Aware Offer Pipeline and Meta Adapter

**Files:**
- Modify: `twocomms/storefront/services/marketplace_feeds.py`
- Modify: `twocomms/storefront/management/commands/generate_instagram_feed.py`
- Test: `twocomms/storefront/tests/test_marketplace_feeds.py`
- Test: `twocomms/storefront/tests/test_feed_admin.py`

- [ ] **Step 1: Write failing tests for include/exclude filters, sold-out overrides, quantity, selected images, and language fallback**

```python
def test_product_rule_forces_sold_out_and_selected_image(self):
    rule = MarketplaceFeedProductRule.objects.create(
        feed=self.feed,
        product=self.product,
        availability="out_of_stock",
        quantity=0,
        image_tokens=[f"product:{self.extra_image.pk}"],
    )
    offers = build_profile_offers(self.feed, base_url="https://twocomms.shop")
    self.assertTrue(offers)
    self.assertTrue(all(not offer.available for offer in offers))
    self.assertTrue(all(offer.export_quantity == 0 for offer in offers))
    self.assertEqual(offers[0].image_urls, ["https://twocomms.shop" + self.extra_image.image.url])
```

- [ ] **Step 2: Run runtime tests and verify expected failures**

Run: `cd twocomms && python manage.py test storefront.tests.test_feed_admin.FeedRuntimeTests -v 2`

- [ ] **Step 3: Extend `FeedOffer` with export availability/quantity and bilingual title fields, preserving stable identifiers**

```python
@dataclass
class FeedOffer:
    # existing fields stay unchanged
    title_ua: str
    title_ru: str
    export_available: bool | None = None
    export_quantity: int | None = None

    @property
    def available(self):
        if self.export_available is not None:
            return self.export_available
        return self.stock > 0
```

- [ ] **Step 4: Apply profile filters before normalization and per-product rules after normalization**

- [ ] **Step 5: Add `build_meta_catalog_feed_xml` to the shared adapter service and reduce the management command to a thin file writer**

```python
xml_payload = build_meta_catalog_feed_xml(base_url=base_url, feed=feed)
Path(options["output"]).write_bytes(xml_payload)
```

- [ ] **Step 6: Let every builder accept an optional feed/profile or prebuilt offers, then keep existing no-argument behavior identical**

- [ ] **Step 7: Run all marketplace and runtime tests**

Run: `cd twocomms && python manage.py test storefront.tests.test_marketplace_feeds storefront.tests.test_feed_admin.FeedRuntimeTests -v 2`

### Task 5: Forms, Staff Actions, and Public Custom Feeds

**Files:**
- Create: `twocomms/storefront/forms_feeds.py`
- Create: `twocomms/storefront/views/feeds_admin.py`
- Modify: `twocomms/storefront/views/static_pages.py`
- Modify: `twocomms/storefront/urls.py`
- Test: `twocomms/storefront/tests/test_feed_admin.py`

- [ ] **Step 1: Write failing tests for staff-only CRUD, duplicate behavior, system deletion protection, reserved slug validation, and inactive public feeds**

```python
def test_duplicate_creates_custom_child_without_system_flag(self):
    self.client.force_login(self.staff)
    response = self.client.post(reverse("admin_feed_duplicate", args=[self.system_feed.pk]))
    duplicate = MarketplaceFeed.objects.exclude(pk=self.system_feed.pk).get()
    self.assertRedirects(response, f"/admin-panel/?section=feeds&feed={duplicate.pk}")
    self.assertEqual(duplicate.parent, self.system_feed)
    self.assertFalse(duplicate.is_system)
```

- [ ] **Step 2: Run view tests and verify missing routes/views**

Run: `cd twocomms && python manage.py test storefront.tests.test_feed_admin.FeedAdminActionTests storefront.tests.test_feed_admin.CustomFeedViewTests -v 2`

- [ ] **Step 3: Implement adapter-aware profile and product-rule forms**

- [ ] **Step 4: Implement staff POST actions for save, duplicate, delete, product rule, validate, and regenerate**

- [ ] **Step 5: Implement `/feeds/<slug>.xml` with active-profile lookup, adapter dispatch, no-cache headers, and XML filename**

```python
response = HttpResponse(payload, content_type="application/xml; charset=utf-8")
response["Content-Disposition"] = f'inline; filename="{feed.slug}.xml"'
response["Cache-Control"] = "no-cache, no-store, must-revalidate"
```

- [ ] **Step 6: Run action and public-route tests**

Run: `cd twocomms && python manage.py test storefront.tests.test_feed_admin.FeedAdminActionTests storefront.tests.test_feed_admin.CustomFeedViewTests -v 2`

### Task 6: Feed Control Center UI

**Files:**
- Modify: `twocomms/storefront/views/admin.py`
- Create: `twocomms/twocomms_django_theme/templates/partials/admin_feeds_section.html`
- Modify: `twocomms/twocomms_django_theme/templates/pages/admin_panel.html`
- Test: `twocomms/storefront/tests/test_feed_admin.py`

- [ ] **Step 1: Write failing render tests for the Feeds navigation item, production links, format/language badges, create action, and validation errors**

```python
def test_feed_section_lists_all_system_links(self):
    self.client.force_login(self.staff)
    response = self.client.get("/admin-panel/?section=feeds")
    for path in ("/google_merchant_feed.xml", "/rozetka-feed.xml", "/kasta-feed.xml", "/buyme-feed.xml", "/prom-feed.xml", "/products_feed.xml", "/media/instagram-feed.xml"):
        self.assertContains(response, path)
```

- [ ] **Step 2: Run render tests and verify failure**

Run: `cd twocomms && python manage.py test storefront.tests.test_feed_admin.FeedAdminTemplateTests -v 2`

- [ ] **Step 3: Build bounded feed-list context with local snapshot metadata and paginated product/image selection**

- [ ] **Step 4: Add `Фіди` navigation and the master-detail partial with accessible native controls, copy/open icons, filters, warnings, and responsive layout**

- [ ] **Step 5: Run template tests and Django template checks**

Run: `cd twocomms && python manage.py test storefront.tests.test_feed_admin.FeedAdminTemplateTests -v 2 && python manage.py check`

### Task 7: Snapshot Integration

**Files:**
- Modify: `twocomms/storefront/management/commands/regenerate_feeds_if_dirty.py`
- Modify: `twocomms/storefront/management/commands/generate_google_merchant_feed.py`
- Modify: `twocomms/storefront/management/commands/generate_rozetka_feed.py`
- Modify: `twocomms/storefront/management/commands/generate_kasta_feed.py`
- Modify: `twocomms/storefront/management/commands/generate_buyme_feed.py`
- Modify: `twocomms/storefront/management/commands/generate_prom_feed.py`
- Modify: `twocomms/storefront/management/commands/generate_instagram_feed.py`
- Test: `twocomms/storefront/tests/test_marketplace_feeds.py`

- [ ] **Step 1: Extend the regenerator test so each command resolves its matching system profile and the Google legacy alias still matches v3**

- [ ] **Step 2: Run the regenerator test and verify failure**

Run: `cd twocomms && python manage.py test storefront.tests.test_marketplace_feeds.MarketplaceFeedTests.test_snapshot_regenerator_refreshes_every_file_backed_marketplace_feed -v 2`

- [ ] **Step 3: Pass a safe system key through each command and preserve CLI compatibility**

- [ ] **Step 4: Run feed tests plus a temporary-directory force regeneration**

Run: `cd twocomms && python manage.py test storefront.tests.test_marketplace_feeds -v 2`

### Task 8: Full Verification and Production Ship

**Files:**
- Verify all modified files

- [ ] **Step 1: Run focused and full storefront tests**

Run: `cd twocomms && python manage.py test storefront.tests.test_feed_admin storefront.tests.test_marketplace_feeds storefront.tests.test_admin_analytics_api -v 2`

- [ ] **Step 2: Run migration and framework checks**

Run: `cd twocomms && python manage.py makemigrations --check --dry-run && python manage.py check && python manage.py migrate --plan`

- [ ] **Step 3: Start a local server and perform staff browser QA at desktop and mobile widths**

Run: `cd twocomms && python manage.py runserver 127.0.0.1:8046`

- [ ] **Step 4: Inspect diff and commit only feed-control-center files**

Run: `git diff --check && git status --short && git diff --stat`

- [ ] **Step 5: Push `main`, deploy through the provided SSH host, migrate, force-regenerate, check, compress, and restart Passenger**

- [ ] **Step 6: Verify live canonical endpoints, custom feed endpoint, snapshot timestamps/counts, Google v2/v3 hash equality, home page, health endpoint, and admin auth behavior**

