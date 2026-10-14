from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from management.services import bot_catalog
from management.services import instagram_bot as bot
from management.services.ig_catalog_media import (
    CatalogMediaDelivery,
    CatalogMediaDeliveryState,
    CatalogMediaItem,
    CatalogMediaSelection,
    CatalogMediaState,
    send_catalog_media,
    select_catalog_media,
)
from management.models import IgClient, InstagramBotMessage, InstagramBotSettings
from productcolors.models import Color, ProductColorImage, ProductColorVariant
from storefront.models import Category, Product, ProductFitOption, ProductStatus


class InstagramCatalogContextTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Футболки", slug="ig-catalog-shirts")
        self.product = Product.objects.create(
            title="Футболка Night Shift",
            slug="ig-catalog-night-shift",
            category=category,
            price=950,
            status=ProductStatus.PUBLISHED,
        )
        ProductFitOption.objects.create(
            product=self.product,
            code="classic",
            label="Класична",
            is_active=True,
        )
        ProductFitOption.objects.create(
            product=self.product,
            code="oversize",
            label="Оверсайз",
            is_active=True,
        )
        blue = Color.objects.create(name="Синя", primary_hex="#1d4ed8")
        black = Color.objects.create(name="Чорна", primary_hex="#111111")
        self.blue = ProductColorVariant.objects.create(
            product=self.product,
            color=blue,
            stock=3,
            sku="NS-BLUE",
        )
        self.black = ProductColorVariant.objects.create(
            product=self.product,
            color=black,
            stock=2,
            sku="NS-BLACK",
        )

    @patch("management.services.bot_catalog.resolve_catalog_sizes", return_value={"classic": ["S", "M", "L"], "oversize": ["XS", "S", "M"]})
    def test_context_exposes_exact_variant_fit_and_size_contract(self, _sizes):
        text = bot_catalog._build()

        self.assertIn(f"id={self.product.pk}", text)
        self.assertIn(f"variant_id={self.blue.pk}", text)
        self.assertIn(f"variant_id={self.black.pk}", text)
        self.assertIn("classic: S/M/L", text)
        self.assertIn("oversize: XS/S/M", text)
        self.assertIn("не вигадуй", text.lower())

    def test_show_products_control_keeps_exact_ids_and_catalog_link_is_explicit(self):
        clean, control = bot._extract_control(
            "Покажу фото [SHOW_PRODUCTS:12,34] [CATALOG_LINK]"
        )

        self.assertEqual(clean, "Покажу фото")
        self.assertEqual(control["show_products"], "12,34")
        self.assertTrue(control["catalog_link"])


class InstagramCatalogMediaTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Худі", slug="ig-catalog-media")
        self.product = Product.objects.create(
            title="Худі Media",
            slug="ig-catalog-media-hoodie",
            category=category,
            price=1200,
            status=ProductStatus.PUBLISHED,
        )
        color = Color.objects.create(name="Чорна", primary_hex="#111111")
        variant = ProductColorVariant.objects.create(
            product=self.product,
            color=color,
            stock=4,
        )
        for index in range(5):
            ProductColorImage.objects.create(
                variant=variant,
                order=index,
                image=SimpleUploadedFile(
                    f"media-{index}.jpg",
                    b"jpeg-placeholder",
                    content_type="image/jpeg",
                ),
            )

    def test_selector_caps_real_images_and_returns_no_product_url(self):
        selection = select_catalog_media([self.product.pk])

        self.assertEqual(selection.state, CatalogMediaState.READY)
        self.assertEqual(len(selection.items), 4)
        self.assertTrue(all(item.url.startswith("https://") for item in selection.items))
        self.assertTrue(all("/product/" not in item.url for item in selection.items))
        self.assertTrue(all(item.mime_type == "image/jpeg" for item in selection.items))
        self.assertTrue(all(item.size_bytes > 0 for item in selection.items))

    def test_selector_reports_empty_for_missing_or_unpublished_product(self):
        self.product.status = ProductStatus.DRAFT
        self.product.save(update_fields=["status"])

        selection = select_catalog_media([self.product.pk])

        self.assertEqual(selection.state, CatalogMediaState.EMPTY)
        self.assertEqual(selection.items, ())

    def test_multiple_products_get_first_photos_before_extra_angles(self):
        second = Product.objects.create(title="Second", slug="ig-media-second",
            category=self.product.category, price=1200, status=ProductStatus.PUBLISHED)
        variant = ProductColorVariant.objects.create(product=second,
            color=Color.objects.create(name="Біла", primary_hex="#ffffff"), stock=1)
        for index in range(2):
            ProductColorImage.objects.create(variant=variant, order=index,
                image=SimpleUploadedFile(f"second-{index}.jpg", b"jpeg-placeholder",
                                         content_type="image/jpeg"))
        selection = select_catalog_media([self.product.pk, second.pk])
        self.assertEqual([item.product_id for item in selection.items],
                         [self.product.pk, second.pk, self.product.pk, second.pk])
        self.assertEqual(selection.shown_product_count, 2)


class InstagramCatalogDiscoveryPipelineTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Футболки", slug="ig-discovery-pipeline")
        self.product = Product.objects.create(
            title="Футболка Discovery",
            slug="ig-discovery-pipeline-shirt",
            category=category,
            price=950,
            status=ProductStatus.PUBLISHED,
        )
        color = Color.objects.create(name="Біла", primary_hex="#ffffff")
        variant = ProductColorVariant.objects.create(
            product=self.product,
            color=color,
            stock=4,
        )
        for index in range(4):
            ProductColorImage.objects.create(
                variant=variant,
                order=index,
                image=SimpleUploadedFile(
                    f"discovery-{index}.jpg",
                    b"jpeg-placeholder",
                    content_type="image/jpeg",
                ),
            )
        self.settings = InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["is_enabled", "ai_enabled", "updated_at"])

    def _run(self, *, sender, customer_text, generated_reply):
        client = IgClient.get_or_create_for_sender(sender)
        client.profile_fetched_at = timezone.now()
        client.save(update_fields=["profile_fetched_at", "updated_at"])
        row = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text=customer_text,
            status=InstagramBotMessage.Status.PROCESSING,
            processing_started_at=timezone.now(),
        )
        delivery = CatalogMediaDelivery(
            CatalogMediaDeliveryState.SENT,
            sent_count=4,
            attempted_count=4,
        )
        sent_part = 0

        def confirmed_text(_settings, _recipient, text, **kwargs):
            nonlocal sent_part
            sent_part += 1
            message_id = f"mid-discovery-{row.pk}-text-{sent_part}"
            receipt = bot.ProviderDeliveryReceipt(
                ok=True, kind="sent", provider_message_id=message_id,
                provider_message_ids=(message_id,), planned_chunk_count=1,
                delivered_chunk_count=1, request_text=text,
            )
            return receipt if kwargs.get("return_receipt") else receipt.as_legacy_tuple()

        with patch(
            "management.services.bot_sales_classifier.ensure_rule_classification",
            return_value=None,
        ), patch(
            "management.services.instagram_bot._rate_exceeded", return_value=False
        ), patch(
            "management.services.instagram_bot._repeated_question", return_value=0
        ), patch(
            "management.services.instagram_bot.send_sender_action"
        ), patch(
            "management.services.instagram_bot.gemini_generate",
            return_value=generated_reply,
        ), patch(
            "management.services.ig_catalog_media.send_catalog_media",
            return_value=delivery,
        ) as send_media, patch(
            "management.services.instagram_bot.send_text",
            side_effect=confirmed_text,
        ) as send_text:
            handled = bot._process_one(self.settings, row)
        return handled, send_media, send_text

    def test_ua_ru_en_general_discovery_sends_four_images_and_caption_without_url(self):
        cases = (
            ("ua", "Покажи, які футболки є", "Ось добірка"),
            ("ru", "Покажи, какие футболки есть", "Вот подборка"),
            ("en", "Show me the T-shirts you have", "Here is the selection"),
        )
        for suffix, customer_text, caption in cases:
            with self.subTest(language=suffix):
                generated = {
                    "reply_text": caption,
                    "controls": [{
                        "kind": "show_products",
                        "value": [self.product.pk],
                    }],
                }
                handled, send_media, send_text = self._run(
                    sender=f"ig-discovery-{suffix}",
                    customer_text=customer_text,
                    generated_reply=generated,
                )

                self.assertTrue(handled)
                selection = send_media.call_args.args[2]
                self.assertEqual(len(selection.items), 4)
                sent_caption = send_text.call_args.args[2]
                self.assertIn(caption, sent_caption)
                self.assertNotIn("http", sent_caption)

    def test_explicit_catalog_link_control_keeps_product_url(self):
        url = f"https://twocomms.shop/product/{self.product.slug}/"
        handled, send_media, send_text = self._run(
            sender="ig-discovery-link",
            # Exact product identity comes from the customer's own published
            # product reference, rather than an unanchored "this T-shirt".
            # Keep the mocked response in the customer's requested language.
            customer_text=f"Покажи фото цього товару та скинь посилання на нього: {url}",
            generated_reply={
                "reply_text": f"Ось фото і посилання: {url}",
                "controls": [
                    {"kind": "show_products", "value": [self.product.pk]},
                    {"kind": "catalog_link", "value": True},
                ],
            },
        )

        self.assertTrue(handled)
        self.assertTrue(send_media.called, "source-backed photo request must reach media delivery")
        self.assertTrue(send_text.called, "source-backed link request must reach text delivery")
        self.assertEqual(len(send_media.call_args.args[2].items), 4)
        self.assertIn(url, send_text.call_args.args[2])


class InstagramCatalogMediaTransportTests(SimpleTestCase):
    def _selection(self):
        return CatalogMediaSelection(
            state=CatalogMediaState.READY,
            items=(
                CatalogMediaItem(
                    "https://twocomms.shop/media/one.jpg", "One", "One", 1,
                    mime_type="image/jpeg", size_bytes=1024,
                ),
                CatalogMediaItem(
                    "https://twocomms.shop/media/two.jpg", "Two", "Two", 2,
                    mime_type="image/jpeg", size_bytes=1024,
                ),
            ),
            requested_product_ids=(1, 2),
        )

    @patch("management.services.instagram_bot.get_page_token", return_value="token")
    @patch("management.services.instagram_bot._provider_account_id", return_value="account")
    @patch(
        "management.services.instagram_bot._provider_http",
        side_effect=[(200, '{"message_id":"mid-1"}'), (500, "provider failed")],
    )
    def test_transport_reports_partial_without_replaying_first_image(
        self, provider_http, _account, _token
    ):
        result = send_catalog_media(object(), "recipient", self._selection())

        self.assertEqual(result.state, CatalogMediaDeliveryState.PARTIAL)
        self.assertEqual(result.sent_count, 1)
        self.assertEqual(result.attempted_count, 2)
        self.assertEqual(result.provider_message_ids, ("mid-1",))
        self.assertEqual(provider_http.call_count, 2)

    @patch("management.services.instagram_bot.get_page_token", return_value="token")
    @patch("management.services.instagram_bot._provider_account_id", return_value="account")
    @patch("management.services.instagram_bot._provider_http", side_effect=TimeoutError("unknown"))
    def test_transport_reports_ambiguous_provider_result(
        self, provider_http, _account, _token
    ):
        result = send_catalog_media(object(), "recipient", self._selection())

        self.assertEqual(result.state, CatalogMediaDeliveryState.AMBIGUOUS)
        self.assertEqual(result.sent_count, 0)
        self.assertEqual(result.attempted_count, 1)
        provider_http.assert_called_once()

    @patch("management.services.instagram_bot.get_page_token", return_value="token")
    @patch("management.services.instagram_bot._provider_account_id", return_value="account")
    @patch("management.services.instagram_bot._provider_http", return_value=(200, '{}'))
    def test_missing_receipt_is_unknown_and_stops_without_counting_sent(
        self, provider_http, _account, _token
    ):
        result = send_catalog_media(object(), "recipient", self._selection())
        self.assertEqual(result.state, CatalogMediaDeliveryState.UNKNOWN)
        self.assertEqual(result.sent_count, 0)
        self.assertEqual(result.attempted_count, 1)
        self.assertEqual(result.provider_message_ids, ())
        self.assertEqual(result.error, "provider_message_id_missing")
        provider_http.assert_called_once()

    @patch("management.services.instagram_bot.get_page_token", return_value="token")
    @patch("management.services.instagram_bot._provider_account_id", return_value="account")
    @patch("management.services.instagram_bot._provider_http", side_effect=[
        (200, '{"message_id":"mid-1"}'), (200, '{}')])
    def test_missing_second_receipt_preserves_first_without_retry(
        self, provider_http, _account, _token
    ):
        result = send_catalog_media(object(), "recipient", self._selection())
        self.assertEqual(result.state, CatalogMediaDeliveryState.UNKNOWN)
        self.assertEqual(result.sent_count, 1)
        self.assertEqual(result.attempted_count, 2)
        self.assertEqual(result.provider_message_ids, ("mid-1",))
        self.assertEqual(provider_http.call_count, 2)

    @patch("management.services.instagram_bot.get_page_token", return_value="token")
    @patch("management.services.instagram_bot._provider_account_id", return_value="account")
    @patch(
        "management.services.instagram_bot._provider_http",
        return_value=(200, '{"message_id":"mid-safe"}'),
    )
    def test_transport_sends_only_bounded_trusted_image_media(
        self, provider_http, _account, _token
    ):
        selection = CatalogMediaSelection(
            state=CatalogMediaState.READY,
            items=(
                CatalogMediaItem(
                    "https://twocomms.shop/media/safe.jpg", "Safe", "Safe", 1,
                    mime_type="image/jpeg",
                    size_bytes=1024,
                ),
                CatalogMediaItem(
                    "http://twocomms.shop/media/insecure.jpg", "HTTP", "HTTP", 2,
                    mime_type="image/jpeg",
                    size_bytes=1024,
                ),
                CatalogMediaItem(
                    "https://evil.example/media/foreign.jpg", "Foreign", "Foreign", 3,
                    mime_type="image/jpeg",
                    size_bytes=1024,
                ),
                CatalogMediaItem(
                    "https://twocomms.shop/media/not-image.html", "HTML", "HTML", 4,
                    mime_type="text/html",
                    size_bytes=1024,
                ),
                CatalogMediaItem(
                    "https://twocomms.shop/media/spoofed.jpg", "Spoofed", "Spoofed", 5,
                    mime_type="text/html",
                    size_bytes=1024,
                ),
                CatalogMediaItem(
                    "https://twocomms.shop/media/unknown.jpg", "Unknown", "Unknown", 6,
                    mime_type="image/jpeg",
                    size_bytes=0,
                ),
                CatalogMediaItem(
                    "https://twocomms.shop/media/huge.webp", "Huge", "Huge", 7,
                    mime_type="image/webp",
                    size_bytes=11 * 1024 * 1024,
                ),
            ),
            requested_product_ids=(1, 2, 3, 4, 5, 6, 7),
        )

        result = send_catalog_media(object(), "recipient", selection)

        self.assertEqual(result.state, CatalogMediaDeliveryState.SENT)
        self.assertEqual(result.attempted_count, 1)
        self.assertEqual(result.sent_count, 1)
        provider_http.assert_called_once()
