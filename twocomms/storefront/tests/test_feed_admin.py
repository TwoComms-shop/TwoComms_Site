from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from orders.models import Order
from productcolors.models import Color, ProductColorImage, ProductColorVariant
from storefront.models import (
    Category,
    MarketplaceFeed,
    MarketplaceFeedProductRule,
    Product,
    ProductImage,
)
from storefront.services.feed_profiles import resolve_feed_rules
from storefront.services.feed_registry import FEED_ADAPTERS, build_feed_xml, reserved_feed_slugs
from storefront.services.marketplace_feeds import build_profile_offers


def ensure_system_feed(key):
    definition = FEED_ADAPTERS[key]
    language = "uk_ru" if key == "kasta" else "uk"
    feed, _ = MarketplaceFeed.objects.get_or_create(
        system_key=key,
        defaults={
            "name": definition.label,
            "slug": key,
            "adapter": key,
            "language": language,
            "is_system": True,
            "is_active": True,
        },
    )
    return feed


class FeedAdminTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="feed-admin",
            password="pass1234",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def _create_orders(self, count):
        for index in range(count):
            Order.objects.create(
                user=self.staff,
                order_number=f"PAGE-{index:04d}",
                full_name=f"Customer {index}",
                phone="+380991112233",
                city="Kyiv",
                np_office="1",
                status="new",
                payment_status="unpaid",
            )

    def test_admin_panel_defaults_to_orders_without_loading_stats_or_analytics(self):
        with (
            patch("storefront.views.admin._build_stats") as stats,
            patch("storefront.views.admin.build_admin_analytics_context") as analytics,
        ):
            response = self.client.get("/admin-panel/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["section"], "orders")
        stats.assert_not_called()
        analytics.assert_not_called()
        self.assertContains(response, 'class="orders-admin-section oadm"')

    def test_explicit_stats_section_loads_stats_and_analytics(self):
        with (
            patch("storefront.views.admin._build_stats", return_value={"orders_today": 7}) as stats,
            patch(
                "storefront.views.admin.build_admin_analytics_context",
                return_value={"analytics_dashboard": {"config": {}}},
            ) as analytics,
        ):
            response = self.client.get("/admin-panel/?section=stats")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["section"], "stats")
        self.assertEqual(response.context["stats"], {"orders_today": 7})
        stats.assert_called_once_with("today")
        analytics.assert_called_once_with(response.wsgi_request)

    def test_orders_are_paginated_server_side_with_filter_preserving_controls(self):
        self._create_orders(55)
        filters = (
            f"section=orders&status=new&payment=unpaid&user_id={self.staff.pk}"
        )

        first_response = self.client.get(f"/admin-panel/?{filters}")

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(len(first_response.context["orders"]), 50)
        first_page = first_response.context["orders_page"]
        self.assertEqual(first_page.number, 1)
        self.assertEqual(first_page.paginator.count, 55)
        self.assertEqual(first_page.paginator.num_pages, 2)
        self.assertContains(first_response, "Сторінка 1 з 2")
        self.assertContains(
            first_response,
            (
                f'href="?section=orders&amp;status=new&amp;payment=unpaid'
                f'&amp;user_id={self.staff.pk}&amp;page=2"'
            ),
        )

        second_response = self.client.get(f"/admin-panel/?{filters}&page=2")

        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(len(second_response.context["orders"]), 5)
        second_page = second_response.context["orders_page"]
        self.assertEqual(second_page.number, 2)
        self.assertTrue(second_page.has_previous())
        self.assertFalse(second_page.has_next())
        self.assertContains(second_response, "Сторінка 2 з 2")
        self.assertContains(
            second_response,
            (
                f'href="?section=orders&amp;status=new&amp;payment=unpaid'
                f'&amp;user_id={self.staff.pk}&amp;page=1"'
            ),
        )

    def test_orders_navigation_precedes_statistics(self):
        response = self.client.get("/admin-panel/")

        content = response.content.decode()
        self.assertLess(
            content.index('href="?section=orders"'),
            content.index('href="?section=stats"'),
        )

    def test_order_deep_link_selects_the_page_containing_an_older_order(self):
        self._create_orders(55)
        target = Order.objects.get(order_number="PAGE-0000")

        response = self.client.get(
            "/admin-panel/",
            {
                "section": "orders",
                "status": "new",
                "payment": "unpaid",
                "user_id": self.staff.pk,
                "edit_order": target.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["orders_page"].number, 2)
        self.assertIn(target.pk, [order.pk for order in response.context["orders"]])
        self.assertContains(response, f'data-edit-order="{target.pk}"')

    def test_invalid_order_deep_links_fall_back_to_the_first_page(self):
        self._create_orders(55)

        for edit_order in ("not-an-id", "999999"):
            with self.subTest(edit_order=edit_order):
                response = self.client.get(
                    "/admin-panel/",
                    {"section": "orders", "edit_order": edit_order},
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["orders_page"].number, 1)

    def test_order_filters_reset_the_current_page(self):
        response = self.client.get("/admin-panel/")

        self.assertContains(response, "currentUrl.searchParams.delete('page');")


class FeedProfileModelTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Футболки", slug="feed-shirts")
        self.product = Product.objects.create(
            title="Feed product",
            slug="feed-product",
            category=self.category,
            price=1000,
            status="published",
        )

    def test_feed_defaults_and_choices_are_closed(self):
        feed = MarketplaceFeed(name="Campaign", slug="campaign", adapter="google")

        feed.full_clean()

        self.assertEqual(feed.language, "uk")
        self.assertTrue(feed.is_active)
        self.assertFalse(feed.is_system)
        self.assertEqual(feed.rules, {})
        with self.assertRaises(ValidationError):
            MarketplaceFeed(name="Unsafe", slug="unsafe", adapter="python.path").full_clean()
        with self.assertRaises(ValidationError):
            MarketplaceFeed(
                name="Unsupported language",
                slug="unsupported-language",
                adapter="google",
                language="de",
            ).full_clean()

    def test_feed_slug_and_system_key_are_unique(self):
        ensure_system_feed("google")

        with self.assertRaises(ValidationError):
            MarketplaceFeed(name="Duplicate", slug="google", adapter="google").full_clean()
        with self.assertRaises(ValidationError):
            MarketplaceFeed(
                name="Duplicate system",
                slug="another-google",
                adapter="google",
                system_key="google",
                is_system=True,
            ).full_clean()

    def test_custom_feed_rejects_reserved_and_malformed_slugs(self):
        for slug in ("admin-panel", "google-merchant-feed", "products_feed"):
            with self.subTest(slug=slug), self.assertRaises(ValidationError):
                MarketplaceFeed(name="Reserved", slug=slug, adapter="google").full_clean()

        with self.assertRaises(ValidationError):
            MarketplaceFeed(name="Malformed", slug="Not A Slug", adapter="google").full_clean()

    def test_feed_rejects_self_parent_for_unsaved_and_saved_instances(self):
        unsaved = MarketplaceFeed(name="Unsaved", slug="unsaved", adapter="google")
        unsaved.parent = unsaved
        with self.assertRaises(ValidationError):
            unsaved.full_clean()

        saved = MarketplaceFeed.objects.create(name="Saved", slug="saved", adapter="google")
        saved.parent = saved
        with self.assertRaises(ValidationError):
            saved.full_clean()

    def test_feed_rejects_parent_cycle(self):
        parent = MarketplaceFeed.objects.create(name="Base", slug="base", adapter="google")
        child = MarketplaceFeed.objects.create(
            name="Child",
            slug="child",
            adapter="google",
            parent=parent,
        )
        parent.parent = child

        with self.assertRaises(ValidationError):
            parent.full_clean()

    def test_feed_rejects_parent_with_another_adapter(self):
        parent = MarketplaceFeed.objects.create(name="Google", slug="google-base", adapter="google")
        child = MarketplaceFeed(name="Prom", slug="prom-copy", adapter="prom", parent=parent)

        with self.assertRaises(ValidationError):
            child.full_clean()

    def test_feed_rejects_malformed_rules(self):
        invalid_rules = (
            [],
            {"unknown": {}},
            {"filters": {"include_product_ids": [self.product.pk, "bad"]}},
            {"availability": {"quantity": -1}},
            {"images": {"max_count": 0}},
        )

        for rules in invalid_rules:
            with self.subTest(rules=rules), self.assertRaises(ValidationError):
                MarketplaceFeed(
                    name="Invalid rules",
                    slug="invalid-rules",
                    adapter="google",
                    rules=rules,
                ).full_clean()

    def test_system_feed_identity_cannot_be_mutated(self):
        feed = ensure_system_feed("google")

        for field, value in (
            ("slug", "google-new"),
            ("system_key", "meta"),
            ("adapter", "prom"),
            ("is_system", False),
        ):
            changed = MarketplaceFeed.objects.get(pk=feed.pk)
            setattr(changed, field, value)
            with self.subTest(field=field), self.assertRaises(ValidationError):
                changed.full_clean()

    def test_custom_feed_cannot_claim_system_identity(self):
        custom = MarketplaceFeed.objects.create(name="Custom", slug="custom", adapter="google")
        custom.system_key = "google"
        custom.is_system = True

        with self.assertRaises(ValidationError):
            custom.full_clean()

    def test_product_rule_defaults_and_unique_feed_product_constraint(self):
        feed = MarketplaceFeed.objects.create(name="Campaign", slug="campaign", adapter="google")
        rule = MarketplaceFeedProductRule(feed=feed, product=self.product)

        rule.full_clean()
        rule.save()

        self.assertEqual(rule.inclusion, "inherit")
        self.assertEqual(rule.availability, "inherit")
        self.assertIsNone(rule.quantity)
        self.assertEqual(rule.image_tokens, [])
        with self.assertRaises(IntegrityError), transaction.atomic():
            MarketplaceFeedProductRule.objects.create(feed=feed, product=self.product)

    def test_product_rule_rejects_invalid_quantity_and_image_token_shape(self):
        feed = MarketplaceFeed.objects.create(name="Campaign", slug="campaign", adapter="google")

        for quantity, tokens in ((-1, []), (None, "main"), (None, [1]), (None, ["foreign:1"])):
            with self.subTest(quantity=quantity, tokens=tokens), self.assertRaises(ValidationError):
                MarketplaceFeedProductRule(
                    feed=feed,
                    product=self.product,
                    quantity=quantity,
                    image_tokens=tokens,
                ).full_clean()

    def test_product_rule_accepts_owned_image_tokens(self):
        feed = MarketplaceFeed.objects.create(name="Campaign", slug="campaign", adapter="google")
        product_image = ProductImage.objects.create(
            product=self.product,
            image="products/extra/feed-product.jpg",
        )
        color = Color.objects.create(name="Black", primary_hex="#000000")
        variant = ProductColorVariant.objects.create(product=self.product, color=color)
        variant_image = ProductColorImage.objects.create(
            variant=variant,
            image="product_colors/feed-product.jpg",
        )
        rule = MarketplaceFeedProductRule(
            feed=feed,
            product=self.product,
            image_tokens=[
                f"product:{product_image.pk}",
                f"variant:{variant_image.pk}",
            ],
        )

        rule.full_clean()

    def test_product_rule_rejects_image_tokens_owned_by_another_product(self):
        other_product = Product.objects.create(
            title="Other product",
            slug="other-feed-product",
            category=self.category,
            price=900,
            status="published",
        )
        foreign_image = ProductImage.objects.create(
            product=other_product,
            image="products/extra/other-product.jpg",
        )
        feed = MarketplaceFeed.objects.create(name="Campaign", slug="campaign", adapter="google")
        rule = MarketplaceFeedProductRule(
            feed=feed,
            product=self.product,
            image_tokens=[f"product:{foreign_image.pk}"],
        )

        with self.assertRaises(ValidationError):
            rule.full_clean()


class FeedRuleResolutionTests(TestCase):
    def setUp(self):
        self.parent = MarketplaceFeed.objects.create(
            name="Base",
            slug="campaign-base",
            adapter="google",
            rules={"images": {"max_count": 4}},
        )
        self.child = MarketplaceFeed.objects.create(
            name="Child",
            slug="campaign-child",
            adapter="google",
            parent=self.parent,
            rules={"availability": {"mode": "force_in_stock"}},
        )

    def test_registry_covers_every_production_feed(self):
        self.assertEqual(
            set(FEED_ADAPTERS),
            {"google", "meta", "rozetka", "kasta", "buyme", "prom", "bezzet"},
        )
        self.assertIn("admin-panel", reserved_feed_slugs())
        self.assertIn("google-merchant-feed", reserved_feed_slugs())

    def test_child_rules_override_parent_without_losing_defaults(self):
        rules = resolve_feed_rules(self.child)

        self.assertEqual(rules["availability"]["mode"], "force_in_stock")
        self.assertEqual(rules["images"]["max_count"], 4)
        self.assertEqual(rules["text"]["language"], "uk")

    def test_resolver_rejects_invalid_saved_rule_payload(self):
        self.child.rules = {"unknown": {}}

        with self.assertRaises(ValidationError):
            resolve_feed_rules(self.child)


class FeedRuntimeTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Runtime", slug="runtime")
        self.product = Product.objects.create(
            title="Runtime hoodie",
            slug="runtime-hoodie",
            category=self.category,
            price=1500,
            status="published",
            main_image="products/runtime-main.jpg",
        )
        self.extra_image = ProductImage.objects.create(
            product=self.product,
            image="products/extra/runtime-detail.jpg",
        )
        self.other_product = Product.objects.create(
            title="Excluded shirt",
            slug="excluded-shirt",
            category=self.category,
            price=500,
            status="published",
            main_image="products/excluded-main.jpg",
        )
        self.feed = MarketplaceFeed.objects.create(
            name="Instagram campaign",
            slug="instagram-campaign",
            adapter="meta",
            rules={
                "filters": {"include_product_ids": [self.product.pk]},
                "images": {"max_count": 1},
            },
        )

    def test_profile_filters_products_and_caps_images(self):
        offers = build_profile_offers(self.feed, base_url="https://twocomms.shop")

        self.assertTrue(offers)
        self.assertEqual({offer.product_id for offer in offers}, {self.product.pk})
        self.assertTrue(all(len(offer.image_urls) == 1 for offer in offers))

    def test_product_rule_forces_sold_out_quantity_and_selected_image(self):
        MarketplaceFeedProductRule.objects.create(
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
        self.assertTrue(
            all(
                offer.image_urls == ["https://twocomms.shop/media/products/extra/runtime-detail.jpg"]
                for offer in offers
            )
        )

    def test_product_rule_can_exclude_a_product(self):
        MarketplaceFeedProductRule.objects.create(
            feed=self.feed,
            product=self.product,
            inclusion="exclude",
        )

        self.assertEqual(build_profile_offers(self.feed), [])

    def test_child_product_rule_inherits_parent_availability_field_by_field(self):
        parent = MarketplaceFeed.objects.create(
            name="Parent meta",
            slug="parent-meta",
            adapter="meta",
            rules={"filters": {"min_image_count": 0, "search_keywords": []}},
        )
        self.feed.parent = parent
        self.feed.rules = {"filters": {"include_product_ids": [self.product.pk], "min_image_count": 0, "search_keywords": []}}
        self.feed.save(update_fields=["parent", "rules"])
        MarketplaceFeedProductRule.objects.create(
            feed=parent,
            product=self.product,
            availability="out_of_stock",
            quantity=0,
        )
        MarketplaceFeedProductRule.objects.create(
            feed=self.feed,
            product=self.product,
            inclusion="include",
            availability="inherit",
        )

        offers = build_profile_offers(self.feed)

        self.assertTrue(offers)
        self.assertTrue(all(not offer.available for offer in offers))
        self.assertTrue(all(offer.export_quantity == 0 for offer in offers))


class FeedProfileAdminViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="marketplace-staff",
            password="pass1234",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        category = Category.objects.create(name="Marketplace", slug="marketplace")
        self.product = Product.objects.create(
            title="Marketplace product",
            slug="marketplace-product",
            category=category,
            price=1200,
            status="published",
            main_image="products/marketplace-product.jpg",
        )

    def test_staff_can_create_a_custom_feed_profile(self):
        response = self.client.post(
            reverse("admin_marketplace_feed_create"),
            {
                "name": "Google summer campaign",
                "slug": "google-summer-campaign",
                "adapter": "google",
                "language": "uk",
                "description": "Campaign export",
            },
        )

        feed = MarketplaceFeed.objects.get(slug="google-summer-campaign")
        self.assertRedirects(
            response,
            f"/admin-panel/?section=marketplace_feeds&feed={feed.pk}",
        )
        self.assertEqual(feed.created_by, self.staff)
        self.assertEqual(feed.rules, {})

    def test_staff_can_save_a_product_override(self):
        feed = MarketplaceFeed.objects.create(
            name="Campaign",
            slug="campaign",
            adapter="google",
        )

        response = self.client.post(
            reverse("admin_marketplace_feed_product_rule", args=[feed.pk]),
            {
                "product_id": self.product.pk,
                "inclusion": "exclude",
                "availability": "out_of_stock",
                "quantity": "0",
                "image_tokens": ["main"],
            },
        )

        self.assertRedirects(
            response,
            f"/admin-panel/?section=marketplace_feeds&feed={feed.pk}",
        )
        rule = MarketplaceFeedProductRule.objects.get(feed=feed, product=self.product)
        self.assertEqual(rule.inclusion, "exclude")
        self.assertEqual(rule.availability, "out_of_stock")
        self.assertEqual(rule.quantity, 0)
        self.assertEqual(rule.image_tokens, ["main"])


class FeedAdminActionTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("feed-actions", password="pass", is_staff=True)
        self.system_feed = ensure_system_feed("google")

    def test_mutations_require_staff(self):
        response = self.client.post(reverse("admin_marketplace_feed_duplicate", args=[self.system_feed.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_duplicate_creates_disabled_custom_child(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse("admin_marketplace_feed_duplicate", args=[self.system_feed.pk]))
        duplicate = MarketplaceFeed.objects.exclude(is_system=True).get()
        self.assertRedirects(response, f"/admin-panel/?section=marketplace_feeds&feed={duplicate.pk}")
        self.assertEqual(duplicate.parent, self.system_feed)
        self.assertFalse(duplicate.is_system)
        self.assertFalse(duplicate.is_active)

    def test_system_feed_cannot_be_deleted(self):
        self.client.force_login(self.staff)
        self.client.post(reverse("admin_marketplace_feed_delete", args=[self.system_feed.pk]))
        self.assertTrue(MarketplaceFeed.objects.filter(pk=self.system_feed.pk).exists())


class CustomFeedViewTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Custom", slug="custom-feed")
        Product.objects.create(
            title="Custom feed hoodie",
            slug="custom-feed-hoodie",
            category=category,
            price=1000,
            status="published",
            main_image="products/custom-feed.jpg",
        )
        self.feed = MarketplaceFeed.objects.create(
            name="Campaign",
            slug="campaign-export",
            adapter="google",
            is_active=True,
        )

    def test_active_custom_feed_is_public_xml_with_no_cache_headers(self):
        response = self.client.get(reverse("custom_marketplace_feed", args=[self.feed.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/xml; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "no-cache, no-store, must-revalidate")
        self.assertIn('filename="campaign-export.xml"', response["Content-Disposition"])
        self.assertIn(b"<g:id>", response.content)

    def test_inactive_custom_feed_returns_404(self):
        self.feed.is_active = False
        self.feed.save(update_fields=["is_active"])
        response = self.client.get(reverse("custom_marketplace_feed", args=[self.feed.slug]))
        self.assertEqual(response.status_code, 404)


class FeedAdminTemplateTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("feed-template", password="pass", is_staff=True)
        self.client.force_login(self.staff)
        for key in FEED_ADAPTERS:
            ensure_system_feed(key)

    def test_feed_section_lists_system_links_and_create_action(self):
        response = self.client.get("/admin-panel/?section=marketplace_feeds")
        self.assertEqual(response.status_code, 200)
        for path in (
            "/google_merchant_feed.xml",
            "/rozetka-feed.xml",
            "/kasta-feed.xml",
            "/buyme-feed.xml",
            "/prom-feed.xml",
            "/products_feed.xml",
            "/media/instagram-feed.xml",
        ):
            self.assertContains(response, path)
        self.assertContains(response, "Створити фід")
        self.assertContains(response, "Фіди")


class CanonicalFeedProfileTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Canonical", slug="canonical-feed")
        Product.objects.create(
            title="Чорна Худі Canonical",
            slug="canonical-hoodie",
            category=category,
            price=1200,
            status="published",
            main_image="products/canonical.jpg",
        )

    def test_canonical_google_route_applies_system_profile(self):
        feed = ensure_system_feed("google")
        feed.rules = {"availability": {"mode": "force_out_of_stock", "quantity": 0}}
        feed.save(update_fields=["rules"])
        response = self.client.get("/google_merchant_feed.xml")
        self.assertContains(response, "out_of_stock")

    def test_meta_adapter_serializes_profile_offers(self):
        feed = MarketplaceFeed.objects.create(
            name="Meta campaign",
            slug="meta-campaign",
            adapter="meta",
            rules={"filters": {"min_image_count": 0, "search_keywords": []}},
        )
        payload = build_feed_xml("meta", base_url="https://twocomms.shop", feed=feed)
        self.assertIn(b"quantity_to_sell_on_facebook", payload)
        self.assertIn(b"<g:id>", payload)

    def test_google_profile_language_changes_exported_title(self):
        feed = MarketplaceFeed.objects.create(
            name="Russian campaign",
            slug="russian-campaign",
            adapter="google",
            language="ru",
        )
        payload = build_feed_xml("google", base_url="https://twocomms.shop", feed=feed)
        self.assertIn("Черная Худи Canonical".encode(), payload)
        self.assertNotIn("Чорна Худі Canonical".encode(), payload)

    def test_meta_profile_language_changes_exported_product_text(self):
        feed = MarketplaceFeed.objects.create(
            name="Russian Meta campaign",
            slug="russian-meta-campaign",
            adapter="meta",
            language="ru",
            rules={"filters": {"min_image_count": 0, "search_keywords": []}},
        )
        payload = build_feed_xml("meta", base_url="https://twocomms.shop", feed=feed)
        self.assertIn("Черная Худи Canonical".encode(), payload)
        self.assertIn("Производство: Украина".encode(), payload)
        self.assertNotIn("Чорна Худі Canonical".encode(), payload)
