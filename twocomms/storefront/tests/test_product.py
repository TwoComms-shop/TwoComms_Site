"""
Regression tests for storefront product detail and product AJAX endpoints.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from orders.models import Order, OrderItem
from productcolors.models import Color, ProductColorImage, ProductColorVariant
from reviews.models import Review, ReviewStatus
from storefront.models import Category, Product, ProductFAQ, ProductFitOption, ProductImage

PNG_PIXEL = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class ProductViewTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp(prefix="product_view_tests_")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.feed_task_patcher = patch(
            "storefront.signals.generate_google_merchant_feed_task.apply_async",
            return_value=None,
        )
        self.feed_task_mock = self.feed_task_patcher.start()
        self.addCleanup(self.feed_task_patcher.stop)
        self.image_task_patcher = patch(
            "storefront.signals.optimize_image_field_task.delay",
            return_value=None,
        )
        self.optimize_image_mock = self.image_task_patcher.start()
        self.addCleanup(self.image_task_patcher.stop)
        self.category = Category.objects.create(
            name="Test Category",
            slug="test-category",
            is_active=True,
        )
        self.product = Product.objects.create(
            title="Test Product",
            slug="test-product",
            category=self.category,
            price=1000,
            description="Test description",
            status="published",
        )

    def _image_file(self, name: str) -> SimpleUploadedFile:
        return SimpleUploadedFile(name, PNG_PIXEL, content_type="image/png")


class ProductHomepageImageTests(ProductViewTestCase):
    def test_homepage_image_prefers_home_card_image(self):
        with self.settings(MEDIA_ROOT=self._media_root):
            self.product.main_image = self._image_file("main.png")
            self.product.home_card_image = self._image_file("home-card.png")
            self.product.save(update_fields=["main_image", "home_card_image"])

        self.assertTrue(self.product.homepage_image.name.endswith("home-card.png"))

    def test_homepage_image_falls_back_to_display_image_chain(self):
        with self.settings(MEDIA_ROOT=self._media_root):
            color = Color.objects.create(name="Black", primary_hex="#000000")
            variant = ProductColorVariant.objects.create(
                product=self.product,
                color=color,
                order=0,
                is_default=True,
            )
            ProductColorImage.objects.create(
                variant=variant,
                image=self._image_file("variant-home-fallback.png"),
                order=0,
            )

        self.assertTrue(self.product.homepage_image.name.endswith("variant-home-fallback.png"))

    def test_home_card_image_enqueues_optimization(self):
        with self.settings(MEDIA_ROOT=self._media_root):
            self.product.home_card_image = self._image_file("home-card-opt.png")
            self.product.save(update_fields=["home_card_image"])

        self.optimize_image_mock.assert_any_call("storefront.Product", self.product.pk, "home_card_image")


class ProductDetailTests(ProductViewTestCase):
    def test_product_detail_page_loads_published_product(self):
        response = self.client.get(reverse("product", args=[self.product.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["product"].pk, self.product.pk)
        self.assertContains(response, self.product.title)
        self.assertEqual(response.context["breadcrumbs"][-1]["name"], self.product.title)

    def test_product_detail_renders_single_share_action_and_delivery_tab(self):
        response = self.client.get(reverse("product", args=[self.product.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-share-action="native"', html=False)
        self.assertNotContains(response, 'class="tc-share-row"', html=False)
        self.assertNotContains(response, 'data-share-action="telegram"', html=False)
        self.assertNotContains(response, 'data-share-action="facebook"', html=False)
        self.assertNotContains(response, 'data-share-action="x"', html=False)
        self.assertNotContains(response, 'data-share-action="copy"', html=False)
        self.assertContains(response, 'data-pdp-tab="delivery"', html=False)
        self.assertContains(response, 'id="panel-delivery"', html=False)
        self.assertContains(response, 'data-add-to-cart=', html=False)
        self.assertContains(response, 'product-detail.css?v=20260717-breadcrumb-layer-v1', html=False)
        self.assertContains(response, 'product-media-fit.css?v=20260428-media-fit-v1', html=False)
        self.assertContains(response, 'product-reviews.css?v=20260510-pdp-reviews-v2', html=False)
        self.assertContains(response, 'product-detail.js?v=20260428-image-alt-faq-v1', html=False)
        self.assertContains(response, 'product-media-fit.js?v=20260428-media-fit-v1', html=False)

    def test_product_detail_renders_description_collapse_hooks(self):
        self.product.full_description = "\n".join(
            [
                "ВАЙБ: КРИЖАНА СВІЖІСТЬ",
                "Колір має значення. Ми обрали цей відтінок для чистого образу.",
                "Тканина рівня люкс, м'яка і приємна до тіла.",
                "Посилені шви та еластичні манжети для довговічності.",
            ]
            * 3
        )
        self.product.save(update_fields=["full_description"])

        response = self.client.get(reverse("product", args=[self.product.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ВАЙБ: КРИЖАНА СВІЖІСТЬ")
        self.assertContains(response, 'class="tc-desc-collapse is-collapsible is-collapsed"', html=False)
        self.assertContains(response, 'data-pdp-description-collapse', html=False)
        self.assertContains(response, 'data-pdp-description-content', html=False)
        self.assertContains(response, 'data-pdp-description-toggle', html=False)
        self.assertContains(response, 'aria-expanded="false"', html=False)
        self.assertContains(response, f'aria-controls="tc-desc-content-{self.product.id}"', html=False)
        self.assertContains(response, "<noscript>", html=False)

    def test_product_detail_css_keeps_desktop_sizes_in_one_scroll_row(self):
        css_path = Path(__file__).resolve().parents[2] / "twocomms_django_theme/static/css/product-detail.css"
        css = css_path.read_text(encoding="utf-8")

        self.assertIn("display: flex", css)
        self.assertIn("flex-wrap: nowrap", css)
        self.assertIn("flex: 0 0 68px", css)
        self.assertIn(".btn-check:focus-visible + .tc-size-option", css)
        self.assertNotIn("grid-template-columns: repeat(5, 68px)", css)

    def test_product_detail_purchase_bar_cta_does_not_stretch_to_trust_column(self):
        css_path = Path(__file__).resolve().parents[2] / "twocomms_django_theme/static/css/product-detail.css"
        css = css_path.read_text(encoding="utf-8")

        self.assertIn("grid-template-columns: minmax(112px, 0.24fr) minmax(252px, 1fr) minmax(218px, 0.54fr)", css)
        self.assertIn("align-items: center", css)
        self.assertIn("height: 62px", css)
        self.assertIn("max-height: 68px", css)
        self.assertIn(".tc-purchase-side .tc-purchase-trust-link span", css)
        self.assertIn('body:has(#product-reviews .tc-reviews__form-wrap[open]) .tc-sticky-mobile', css)

    def test_product_detail_discount_price_stays_on_one_line(self):
        css_path = Path(__file__).resolve().parents[2] / "twocomms_django_theme/static/css/product-detail.css"
        css = css_path.read_text(encoding="utf-8")

        price_value_rules = [
            rule.split("}", 1)[0]
            for rule in css.split(".tc-price-values {")[1:]
        ]

        self.assertGreaterEqual(len(price_value_rules), 1)
        self.assertTrue(
            all("flex-wrap: nowrap;" in rule for rule in price_value_rules),
            "The current, original, and discount prices must remain in one row at every breakpoint.",
        )
        self.assertIn("white-space: nowrap;", price_value_rules[0])

    def test_product_detail_breadcrumbs_have_a_readable_route_specific_surface(self):
        css_path = Path(__file__).resolve().parents[2] / "twocomms_django_theme/static/css/product-detail.css"
        css = css_path.read_text(encoding="utf-8")

        self.assertIn('html[data-route-name="product"] .breadcrumb-nav {', css)
        self.assertIn("position: relative;", css)
        self.assertIn("z-index: 3;", css)
        self.assertIn("background: var(--pdp-surface-strong);", css)
        self.assertIn('html[data-route-name="product"] .breadcrumb-item.active {', css)
        self.assertIn("color: var(--pdp-text);", css)
        self.assertIn("overflow-wrap: anywhere;", css)
        self.assertIn("padding: 8px 10px !important;", css)

    def test_product_detail_reviews_prefill_registered_buyer_identity(self):
        User = get_user_model()
        user = User.objects.create_user(
            username="reviewbuyer",
            password="x",
            first_name="Олена",
            last_name="Клієнт",
            email="buyer@example.com",
        )
        order = Order.objects.create(
            user=user,
            full_name="Олена Клієнт",
            phone="+380991112233",
            email="buyer@example.com",
            city="Kyiv",
            np_office="1",
            pay_type="cod",
            payment_status="paid",
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            qty=1,
            unit_price=1000,
            line_total=1000,
        )

        self.client.force_login(user)
        response = self.client.get(reverse("product", args=[self.product.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["product_customer_has_paid_order"])
        self.assertContains(response, "Ви увійшли як клієнт")
        self.assertContains(response, "Олена Клієнт")
        self.assertContains(response, "Купівля цього товару підтверджена")
        self.assertContains(response, 'value="Олена Клієнт"', html=False)
        self.assertContains(response, 'value="buyer@example.com"', html=False)

    def test_product_detail_review_cards_show_account_and_purchase_statuses(self):
        User = get_user_model()
        buyer = User.objects.create_user(username="verified-reviewer", password="x")
        Review.objects.create(
            product=self.product,
            user=buyer,
            author_name="Покупець",
            rating=5,
            title="Сильна якість",
            body="Тканина щільна, посадка рівна, принт після прання без змін.",
            status=ReviewStatus.APPROVED,
            is_verified_purchase=True,
        )
        Review.objects.create(
            product=self.product,
            author_name="Гість",
            rating=4,
            body="Гарний товар, але покупку на акаунт не привʼязував.",
            status=ReviewStatus.APPROVED,
            is_verified_purchase=False,
        )

        response = self.client.get(reverse("product", args=[self.product.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Зареєстрований користувач")
        self.assertContains(response, "Купив товар")
        self.assertContains(response, "Гостьовий відгук")
        self.assertContains(response, "Покупка не підтверджена")

    def test_product_detail_media_fit_assets_define_wide_only_cover_mode(self):
        static_root = Path(__file__).resolve().parents[2] / "twocomms_django_theme/static"
        css = (static_root / "css/product-media-fit.css").read_text(encoding="utf-8")
        js = (static_root / "js/product-media-fit.js").read_text(encoding="utf-8")

        self.assertIn(".tc-media-stage.tc-media-fit-wide .tc-media-hero-img", css)
        self.assertIn("object-fit: cover", css)
        self.assertIn("object-fit: contain", css)
        self.assertIn("const WIDE_RATIO = 1.42", js)
        self.assertIn("MutationObserver", js)

    def test_product_detail_returns_404_for_unpublished_product(self):
        self.product.status = "draft"
        self.product.save(update_fields=["status"])

        response = self.client.get(reverse("product", args=[self.product.slug]))

        self.assertEqual(response.status_code, 404)

    def test_product_detail_moves_preselected_color_to_front(self):
        black = Color.objects.create(name="Black", primary_hex="#000000")
        white = Color.objects.create(name="White", primary_hex="#FFFFFF")
        ProductColorVariant.objects.create(
            product=self.product,
            color=black,
            order=0,
            is_default=True,
        )
        selected_variant = ProductColorVariant.objects.create(
            product=self.product,
            color=white,
            order=1,
            is_default=False,
        )

        response = self.client.get(
            reverse("product", args=[self.product.slug]),
            {"color": str(selected_variant.pk)},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[0][1], 301)
        self.assertEqual(response.context["preselected_color"], selected_variant.pk)
        self.assertEqual(response.context["color_variants"][0]["id"], selected_variant.pk)

    def test_product_detail_shows_fit_selector_for_tshirts(self):
        tshirt_category = Category.objects.create(
            name="Футболки",
            slug="futbolki",
            is_active=True,
        )
        product = Product.objects.create(
            title="Футболка тестова",
            slug="test-tshirt-fit",
            category=tshirt_category,
            price=1000,
            description="Fit selector coverage.",
            status="published",
        )
        ProductFitOption.objects.create(
            product=product,
            code="classic",
            label="Класичний",
            description="Прямий крій, стандартна посадка",
            is_default=True,
            order=0,
        )
        ProductFitOption.objects.create(
            product=product,
            code="oversize",
            label="Оверсайз",
            description="Вільний крій, спущене плече",
            order=1,
        )

        response = self.client.get(reverse("product", args=[product.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-fit-selector', html=False)
        self.assertContains(response, "Класичний")
        self.assertContains(response, "Оверсайз")

    def test_product_detail_preselects_fit_from_url_for_tshirts(self):
        tshirt_category = Category.objects.create(
            name="Футболки",
            slug="futbolki-preselect",
            is_active=True,
        )
        product = Product.objects.create(
            title="Футболка з посадкою",
            slug="test-tshirt-fit-preselected",
            category=tshirt_category,
            price=1000,
            description="Fit selector preselect coverage.",
            status="published",
        )
        ProductFitOption.objects.create(
            product=product,
            code="classic",
            label="Класичний",
            is_default=True,
            order=0,
        )
        ProductFitOption.objects.create(
            product=product,
            code="oversize",
            label="Оверсайз",
            order=1,
        )

        response = self.client.get(
            reverse("product", args=[product.slug]),
            {"fit": "oversize"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[0][1], 301)
        self.assertEqual(response.context["preselected_fit_code"], "oversize")
        self.assertContains(response, 'id="fit-oversize"', html=False)
        self.assertContains(response, 'value="oversize"', html=False)

    def test_product_detail_hides_fit_selector_for_non_tshirts(self):
        longsleeve_category = Category.objects.create(
            name="Лонгсліви",
            slug="longsleeve",
            is_active=True,
        )
        product = Product.objects.create(
            title="Лонгслів тестовий",
            slug="test-longsleeve-fit-hidden",
            category=longsleeve_category,
            price=1000,
            description="Fit selector hidden coverage.",
            status="published",
        )
        ProductFitOption.objects.create(
            product=product,
            code="classic",
            label="Класичний",
            description="Прямий крій",
            is_default=True,
            order=0,
        )

        response = self.client.get(reverse("product", args=[product.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'data-fit-selector', html=False)
        self.assertNotContains(response, "Оверсайз")

    def test_product_detail_renders_active_product_faq_tab_and_schema(self):
        ProductFAQ.objects.create(
            product=self.product,
            question="Це чоловіча чи жіноча футболка?",
            answer="Це футболка унісекс.",
            order=0,
            is_active=True,
        )
        ProductFAQ.objects.create(
            product=self.product,
            question="Неактивне питання",
            answer="Не має показуватись.",
            order=1,
            is_active=False,
        )

        response = self.client.get(reverse("product", args=[self.product.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["product_faq_items"][0]["question"], "Це чоловіча чи жіноча футболка?")
        self.assertContains(response, 'data-pdp-tab="faq"', html=False)
        self.assertContains(response, 'id="panel-faq"', html=False)
        self.assertContains(response, "FAQ товару")
        self.assertContains(response, "Це футболка унісекс.")
        self.assertNotContains(response, '"@type": "FAQPage"', html=False)
        self.assertNotContains(response, "Неактивне питання")


class GetProductImagesTests(ProductViewTestCase):
    def test_get_product_images_returns_main_and_gallery(self):
        with self.settings(MEDIA_ROOT=self._media_root):
            self.product.main_image = self._image_file("main.png")
            self.product.main_image_alt = "Main alt"
            self.product.save(update_fields=["main_image", "main_image_alt"])
            ProductImage.objects.create(
                product=self.product,
                image=self._image_file("gallery.png"),
                alt_text="Gallery alt",
                order=0,
            )

            response = self.client.get(reverse("get_product_images", args=[self.product.pk]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["count"], 2)
        self.assertTrue(payload["images"][0]["is_main"])
        self.assertFalse(payload["images"][1]["is_main"])
        self.assertEqual(payload["images"][0]["alt"], "Main alt")
        self.assertEqual(payload["images"][1]["alt"], "Gallery alt")

    def test_get_product_images_returns_404_for_missing_product(self):
        response = self.client.get(reverse("get_product_images", args=[99999]))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])


class GetProductVariantsTests(ProductViewTestCase):
    def test_get_product_variants_returns_current_contract(self):
        with self.settings(MEDIA_ROOT=self._media_root):
            default_color = Color.objects.create(name="Black", primary_hex="#000000")
            secondary_color = Color.objects.create(
                name="Split",
                primary_hex="#FFFFFF",
                secondary_hex="#111111",
            )
            default_variant = ProductColorVariant.objects.create(
                product=self.product,
                color=default_color,
                order=0,
                is_default=True,
            )
            secondary_variant = ProductColorVariant.objects.create(
                product=self.product,
                color=secondary_color,
                order=1,
                is_default=False,
            )
            ProductColorImage.objects.create(
                variant=secondary_variant,
                image=self._image_file("variant.png"),
                alt_text="Side",
                order=0,
            )

            response = self.client.get(reverse("get_product_variants", args=[self.product.pk]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["count"], 2)

        variants_by_id = {variant["id"]: variant for variant in payload["variants"]}
        self.assertEqual(variants_by_id[default_variant.pk]["primary_hex"], "#000000")
        self.assertTrue(variants_by_id[default_variant.pk]["is_default"])
        self.assertEqual(variants_by_id[secondary_variant.pk]["secondary_hex"], "#111111")
        self.assertEqual(len(variants_by_id[secondary_variant.pk]["images"]), 1)
        self.assertEqual(variants_by_id[secondary_variant.pk]["images"][0]["alt"], "Side")

    def test_get_product_variants_returns_optimized_image_sources(self):
        with self.settings(MEDIA_ROOT=self._media_root):
            color = Color.objects.create(name="Optimized", primary_hex="#123456")
            variant = ProductColorVariant.objects.create(
                product=self.product,
                color=color,
                order=0,
                is_default=True,
            )
            color_image = ProductColorImage.objects.create(
                variant=variant,
                image=self._image_file("variant-optimized.png"),
                alt_text="Optimized",
                order=0,
            )
            image_path = Path(color_image.image.path)
            optimized_dir = image_path.parent / "optimized"
            optimized_dir.mkdir(parents=True, exist_ok=True)
            for suffix in ("640w.avif", "640w.webp"):
                (optimized_dir / f"{image_path.stem}_{suffix}").write_bytes(PNG_PIXEL)
            for extension in ("avif", "webp"):
                (optimized_dir / f"{image_path.stem}.{extension}").write_bytes(PNG_PIXEL)

            response = self.client.get(reverse("get_product_variants", args=[self.product.pk]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        image_payload = payload["variants"][0]["images"][0]

        self.assertIsInstance(image_payload, dict)
        self.assertTrue(image_payload["original_url"].endswith("/product_colors/variant-optimized.png"))
        self.assertIn("/optimized/variant-optimized_640w.avif 640w", image_payload["avif_srcset"])
        self.assertIn("/optimized/variant-optimized_640w.webp 640w", image_payload["webp_srcset"])
        self.assertTrue(image_payload["url"].endswith("/optimized/variant-optimized_640w.webp"))
        self.assertEqual(image_payload["alt"], "Optimized")

    def test_get_product_variants_returns_404_for_missing_product(self):
        response = self.client.get(reverse("get_product_variants", args=[99999]))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])


class QuickViewTests(ProductViewTestCase):
    def test_quick_view_returns_json_html_fragment(self):
        response = self.client.get(reverse("quick_view", args=[self.product.pk]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertIn(self.product.title, payload["html"])
        self.assertIn(str(self.product.final_price), payload["html"])

    def test_quick_view_returns_404_for_unpublished_product(self):
        self.product.status = "draft"
        self.product.save(update_fields=["status"])

        response = self.client.get(reverse("quick_view", args=[self.product.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])


class AdminProductUnifiedTests(ProductViewTestCase):
    def setUp(self):
        super().setUp()
        self.staff_user = get_user_model().objects.create_user(
            username="product-admin",
            password="secret123",
            is_staff=True,
        )
        self.client.force_login(self.staff_user)

    def test_legacy_product_new_page_renders_manual_seo_and_content_fields(self):
        response = self.client.get(reverse("admin_product_new"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SEO Title")
        self.assertContains(response, "Meta description")
        self.assertContains(response, "Деталі")
        self.assertContains(response, "Кому підійде")
        self.assertContains(response, "ALT головного зображення")

    def test_legacy_product_new_ajax_saves_without_ai_and_places_product_first(self):
        response = self.client.post(
            reverse("admin_product_new"),
            {
                "form_type": "product",
                "title": "Legacy New Product",
                "slug": "legacy-new-product",
                "category": str(self.category.pk),
                "status": "published",
                "priority": "0",
                "price": "1200",
                "discount_percent": "",
                "points_reward": "0",
                "short_description": "Short legacy create.",
                "full_description": "Full legacy create.",
                "details_text": "Тип: футболка",
                "target_audience": "Для streetwear образу.",
                "care_instructions": "Прати навиворіт.",
                "seo_title": "Legacy SEO Title",
                "seo_description": "Legacy SEO Description",
                "seo_keywords": "legacy, twocomms",
                "drop_price": "0",
                "wholesale_price": "0",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        product = Product.objects.get(title="Legacy New Product")
        self.assertEqual(product.priority, 1)
        self.assertEqual(product.seo_title, "Legacy SEO Title")
        self.assertEqual(product.details_text, "Тип: футболка")

    def test_unified_edit_saves_manual_seo_content_blocks_and_faq(self):
        response = self.client.post(
            reverse("admin_product_edit_unified", args=[self.product.pk]),
            {
                "form_type": "product",
                "title": "Чорна футболка унісекс TwoComms",
                "slug": self.product.slug,
                "category": str(self.category.pk),
                "status": "published",
                "priority": "10",
                "price": "1000",
                "discount_percent": "",
                "points_reward": "0",
                "short_description": "Короткий опис для карточки.",
                "full_description": "Довгий опис товару.",
                "details_text": "Тип: футболка унісекс\nКолір: чорний",
                "target_audience": "Підійде тим, хто шукає streetwear з сенсом.",
                "care_instructions": "Прати навиворіт у делікатному режимі.",
                "main_image_alt": "Чорна футболка TwoComms",
                "seo_title": "Футболка унісекс | TwoComms",
                "seo_description": "Чорна футболка унісекс TwoComms з авторським принтом.",
                "seo_keywords": "футболка унісекс, TwoComms",
                "drop_price": "0",
                "wholesale_price": "0",
                "faqs-TOTAL_FORMS": "1",
                "faqs-INITIAL_FORMS": "0",
                "faqs-MIN_NUM_FORMS": "0",
                "faqs-MAX_NUM_FORMS": "1000",
                "faqs-0-question": "Де розміщений принт?",
                "faqs-0-answer": "Основний принт розміщений на спині.",
                "faqs-0-order": "0",
                "faqs-0-is_active": "on",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

        self.product.refresh_from_db()
        self.assertEqual(self.product.seo_title, "Футболка унісекс | TwoComms")
        self.assertEqual(self.product.details_text, "Тип: футболка унісекс\nКолір: чорний")
        self.assertEqual(self.product.care_instructions, "Прати навиворіт у делікатному режимі.")
        faq = ProductFAQ.objects.get(product=self.product)
        self.assertEqual(faq.question, "Де розміщений принт?")
        self.assertTrue(faq.is_active)
