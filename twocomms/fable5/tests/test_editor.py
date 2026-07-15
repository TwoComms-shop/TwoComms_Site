import json
from pathlib import Path

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from storefront.models import Category, Product, ProductImage

from fable5.models import FeedImageRule, FeedOnlyImage, FeedProfile
from fable5.services import feed_image_urls


class Fable5EditorAccessTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Test", slug="test")
        self.product = Product.objects.create(
            title="Test product",
            slug="test-product",
            category=self.category,
            price=500,
        )
        self.staff = get_user_model().objects.create_user(
            username="fable5-staff",
            password="test-password",
            is_staff=True,
        )

    def test_editor_rejects_anonymous_users(self):
        response = self.client.get(reverse("fable5_product_new"))

        self.assertEqual(response.status_code, 403)

    def test_editor_bootstrap_cannot_break_out_of_json_script(self):
        self.product.title = '</script><script id="injected">alert(1)</script>'
        self.product.save(update_fields=["title"])
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse("fable5_product_edit", args=[self.product.pk])
        )
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('<script id="injected">', content)
        self.assertIn(r"\u003C/script\u003E", content)

    def test_staff_can_create_product_with_unified_save_endpoint(self):
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("fable5_api_product_save"),
            data={
                "payload": json.dumps(
                    {
                        "title": "Нова термо футболка",
                        "category_id": self.category.pk,
                        "price": 1200,
                        "status": "draft",
                    }
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        created = Product.objects.get(pk=body["product"]["id"])
        self.assertEqual(created.title, "Нова термо футболка")
        self.assertEqual(created.price, 1200)
        self.assertEqual(created.status, "draft")

    def test_feed_rule_rejects_an_image_owned_by_another_product(self):
        other = Product.objects.create(
            title="Other product",
            slug="other-product",
            category=self.category,
            price=700,
        )
        foreign_image = ProductImage.objects.create(
            product=other,
            image="products/extra/foreign.webp",
        )
        feed = FeedProfile.objects.create(name="Google", slug="google")
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("fable5_api_feed_rule_save"),
            data=json.dumps(
                {
                    "product_id": self.product.pk,
                    "feed_id": feed.pk,
                    "image_rules": [{"product_image_id": foreign_image.pk}],
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            FeedImageRule.objects.filter(feed=feed, product=self.product).exists()
        )

    def test_gallery_upload_rejects_non_image_content(self):
        self.client.force_login(self.staff)
        fake_image = SimpleUploadedFile(
            "payload.php",
            b"<?php echo 'not an image'; ?>",
            content_type="image/png",
        )

        response = self.client.post(
            reverse("fable5_api_images_upload"),
            data={
                "product_id": self.product.pk,
                "kind": "product",
                "files": [fake_image],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProductImage.objects.filter(product=self.product).exists())

    def test_setting_gallery_image_as_cover_records_its_source(self):
        from fable5.models import CoverSource

        image = ProductImage.objects.create(
            product=self.product,
            image="products/extra/cover-source.webp",
            alt_text="Cover source",
        )
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("fable5_api_set_cover"),
            data=json.dumps({
                "product_id": self.product.pk,
                "kind": "product",
                "image_id": image.pk,
                "target": "main",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        source = CoverSource.objects.get(product=self.product)
        self.assertEqual(source.source_type, CoverSource.SourceType.PRODUCT_IMAGE)
        self.assertEqual(source.product_image, image)
        self.assertEqual(response.json()["cover_source"]["product_image_id"], image.pk)

    def test_editor_css_preserves_hidden_buttons_and_wraps_mobile_actions(self):
        css = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "fable5"
            / "editor.css"
        ).read_text(encoding="utf-8")

        self.assertIn(".f5-btn[hidden]", css)
        self.assertIn(".f5-topbar__actions { width: 100%; flex-wrap: wrap; }", css)

    def test_javascript_transliteration_matches_server_for_russian_yo(self):
        javascript = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "fable5"
            / "editor.js"
        ).read_text(encoding="utf-8")

        self.assertIn('"ё": "yo"', javascript)

    def test_global_save_persists_variant_drafts_before_refreshing_state(self):
        javascript = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "fable5"
            / "editor.js"
        ).read_text(encoding="utf-8")

        self.assertIn("pendingVariantDrafts", javascript)
        self.assertIn("for (const draft of pendingVariantDrafts)", javascript)
        self.assertIn("index, data: collectVariantData", javascript)
        self.assertIn(
            "state.variants[draft.index] = variantResp.variant",
            javascript,
        )

    def test_global_save_includes_dirty_stock_and_feed_drafts(self):
        javascript = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "fable5"
            / "editor.js"
        ).read_text(encoding="utf-8")

        self.assertIn("collectStockSizes", javascript)
        self.assertIn("#f-stock [data-variant-index][data-dirty=", javascript)
        self.assertIn("pendingFeedDrafts", javascript)
        self.assertIn("for (const draft of pendingFeedDrafts)", javascript)

    def test_unknown_feed_does_not_append_feed_specific_images(self):
        feed = FeedProfile.objects.create(name="Meta", slug="meta")
        FeedOnlyImage.objects.create(
            product=self.product,
            feed=feed,
            image="fable5/feed_images/meta-only.webp",
        )

        urls = feed_image_urls("unknown", self.product, ["/default.webp"])

        self.assertEqual(urls, ["/default.webp"])

    def test_inactive_feed_keeps_legacy_default_images(self):
        feed = FeedProfile.objects.create(
            name="Inactive",
            slug="inactive",
            is_active=False,
        )
        FeedOnlyImage.objects.create(
            product=self.product,
            feed=feed,
            image="fable5/feed_images/inactive-only.webp",
        )

        urls = feed_image_urls("inactive", self.product, ["/default.webp"])

        self.assertEqual(urls, ["/default.webp"])

    def test_unexpected_api_errors_do_not_expose_internal_details(self):
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("fable5_api_images_reorder"),
            data=json.dumps({"product_id": 999999, "ids": []}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Не вдалося виконати операцію")

    def test_legacy_myisam_relations_do_not_create_database_constraints(self):
        external_relations = (
            ("ColorProfile", "color"),
            ("VariantDetails", "variant"),
            ("ProductFitNote", "product"),
            ("VariantFitRule", "variant"),
            ("VariantSizeRule", "variant"),
            ("VariantFAQ", "variant"),
            ("FeedProductRule", "product"),
            ("FeedImageRule", "product"),
            ("FeedImageRule", "product_image"),
            ("FeedImageRule", "color_image"),
            ("FeedOnlyImage", "product"),
        )

        for model_name, field_name in external_relations:
            field = apps.get_model("fable5", model_name)._meta.get_field(field_name)
            self.assertFalse(
                field.db_constraint,
                f"{model_name}.{field_name} must remain compatible with legacy MyISAM tables",
            )

    def test_legacy_admin_panel_links_to_fable5_editor(self):
        template = (
            Path(__file__).resolve().parents[2]
            / "twocomms_django_theme"
            / "templates"
            / "pages"
            / "admin_panel.html"
        ).read_text(encoding="utf-8")

        self.assertIn("{% url 'fable5_product_new' %}", template)
        self.assertIn("{% url 'fable5_product_edit' product.id %}", template)
        self.assertIn("Новий товар · Fable 5", template)
