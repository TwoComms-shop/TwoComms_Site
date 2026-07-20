import tempfile
import json
from pathlib import Path
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.contrib.auth.models import User
from django.utils import translation

from storefront.models import Category


PNG_PIXEL = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


TEST_CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"},
    "fragments": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"},
}


@override_settings(COMPRESS_ENABLED=False, COMPRESS_OFFLINE=False, CACHES=TEST_CACHES)
class CustomPrintPageTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._temp_media = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls._temp_media.cleanup()
        super().tearDownClass()

    def setUp(self):
        translation.activate("uk")
        self.addCleanup(translation.deactivate)
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client(
            HTTP_HOST="twocomms.shop",
            SERVER_PORT="443",
            **{"wsgi.url_scheme": "https"},
        )
        for index, (name, slug) in enumerate(
            (
                ("Футболки", "tshirts"),
                ("Худі", "hoodie"),
                ("Лонгсліви", "long-sleeve"),
            ),
            start=1,
        ):
            Category.objects.create(
                name=name,
                slug=slug,
                is_active=True,
                order=index,
            )

    def _png_file(self, name: str = "design.png") -> SimpleUploadedFile:
        return SimpleUploadedFile(name, PNG_PIXEL, content_type="image/png")

    def _get(self, url, **kwargs):
        return self.client.get(url, secure=True, **kwargs)

    def _post(self, url, data, **kwargs):
        return self.client.post(url, data, secure=True, **kwargs)

    def test_custom_print_page_renders_adaptive_configurator(self):
        response = self._get(reverse("custom_print"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TwoComms Custom Print")
        self.assertContains(response, "Збери річ під свій дизайн")
        self.assertContains(response, "Кастомний DTF-друк")
        self.assertContains(response, "data-start-flow")
        self.assertContains(response, "Написати нам у Telegram")
        self.assertContains(response, "Не хочеш клацати всі кроки?")
        self.assertContains(response, "Напиши менеджеру — опиши ідею простими словами.")
        self.assertContains(response, "cp-manager-shortcut")
        self.assertContains(response, "cp-manager-inline-link")
        self.assertContains(response, "Для себе")
        self.assertContains(response, "Для команди / бренду")
        self.assertContains(response, "Худі")
        self.assertContains(response, 'data-step="mode"')
        self.assertContains(response, "Для кого збираємо?")
        self.assertContains(response, "customPrintConfiguratorForm")
        content = response.content.decode("utf-8")
        self.assertRegex(content, r"custom-print-configurator(?:\.[0-9a-f]+)?\.css")
        self.assertRegex(content, r"custom-print-configurator(?:\.[0-9a-f]+)?\.js")
        self.assertGreaterEqual(content.count("data-safe-exit-trigger"), 2)
        self.assertContains(response, "customPrintConfiguratorConfig")
        self.assertNotContains(response, "Custom Print Atelier")
        self.assertNotContains(response, "DTG")
        self.assertNotContains(response, "Шовкографія")
        self.assertNotContains(response, "/admin/storefront/customprintlead/")

    def test_custom_print_page_embeds_structured_config_payload(self):
        response = self._get(reverse("custom_print"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '"quick_start_modes"')
        self.assertContains(response, '"starter_styles"')
        self.assertContains(response, '"hoodie"')
        self.assertContains(response, '"safe_exit_url"')
        self.assertContains(response, '"telegram_manager_url"')
        self.assertContains(response, '"progress_steps"')
        self.assertContains(response, '"front_size_presets"')
        self.assertContains(response, '"back_size_presets"')
        self.assertContains(response, '"sleeve_mode_options"')
        self.assertContains(response, '"current_step": "mode"')

    def test_custom_print_page_keeps_product_settings_in_single_config_step(self):
        response = self._get(reverse("custom_print"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-step="config"')
        self.assertContains(response, 'data-step-edit="config"')
        self.assertContains(response, 'data-step-next="zones"')
        self.assertContains(response, "Посадка")
        self.assertContains(response, "Тканина")
        self.assertContains(response, "Колір виробу")
        self.assertNotContains(response, 'data-step="fit"')
        self.assertNotContains(response, 'data-step="fabric"')
        self.assertNotContains(response, "Далі до тканини")
        self.assertNotContains(response, "Вибір тканини і кольору")

    def test_custom_print_page_mentions_custom_return_exception(self):
        response = self._get(reverse("custom_print"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "готові товари належної якості можна повернути або обміняти протягом 14 днів")
        self.assertContains(response, "кастомний одяг, виготовлений за індивідуальним замовленням, не підлягає поверненню чи обміну")

    def test_custom_print_page_cache_busts_configurator_script(self):
        response = self._get(reverse("custom_print"), follow=True)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertRegex(content, r"custom-print-configurator(?:\.[0-9a-f]+)?\.css\?v=")
        self.assertRegex(content, r"custom-print-configurator(?:\.[0-9a-f]+)?\.js\?v=")

    def test_custom_print_page_disables_browser_cache(self):
        response = self._get(reverse("custom_print"), follow=True)

        self.assertEqual(response.status_code, 200)
        cache_control = response.headers.get("Cache-Control", "")
        self.assertIn("no-store", cache_control)
        self.assertIn("no-cache", cache_control)
        self.assertIn("must-revalidate", cache_control)
        self.assertIn("max-age=0", cache_control)
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        self.assertEqual(response.headers.get("Expires"), "0")

    def test_custom_print_config_exposes_progress_steps_tshirt_rules_and_zone_presets(self):
        from storefront.custom_print_config import build_custom_print_config

        config = build_custom_print_config(
            submit_url="https://twocomms.shop/custom-print/lead/",
            safe_exit_url="https://twocomms.shop/custom-print/safe-exit/",
            add_to_cart_url="https://twocomms.shop/custom-print/add-to-cart/",
        )

        self.assertEqual(
            [item["value"] for item in config["progress_steps"]],
            ["mode", "product", "config", "zones", "artwork", "quantity", "gift", "contact"],
        )
        self.assertEqual(
            [item["value"] for item in config["front_size_presets"]],
            ["A6", "A5", "A4"],
        )
        self.assertEqual(
            [item["value"] for item in config["back_size_presets"]],
            ["A4", "A3", "A2"],
        )
        self.assertEqual(
            [item["value"] for item in config["sleeve_mode_options"]],
            ["a6", "full_text"],
        )
        self.assertEqual(config["front_size_default"], "A4")
        self.assertEqual(config["back_size_default"], "A4")
        self.assertEqual(config["products"]["hoodie"]["add_ons"][0]["price_delta"], 150)
        self.assertEqual(config["artwork_services"][1]["value"], "adjust")
        self.assertEqual(config["artwork_services"][1]["label"], "Потрібно допрацювати")
        self.assertEqual(config["artwork_services"][1]["price_delta"], 100)
        self.assertEqual(config["artwork_services"][2]["price_delta"], 300)
        self.assertEqual(
            [item["value"] for item in config["products"]["tshirt"]["fits"]],
            ["regular", "oversize"],
        )
        self.assertEqual(
            [item["value"] for item in config["products"]["tshirt"]["fabrics"]["regular"]],
            ["standard", "premium"],
        )
        self.assertEqual(
            [item["value"] for item in config["products"]["tshirt"]["fabrics"]["oversize"]],
            ["standard", "premium", "thermo"],
        )
        self.assertEqual(config["products"]["tshirt"]["fabrics"]["oversize"][2]["price_delta"], 500)
        self.assertEqual(
            [item["value"] for item in config["products"]["hoodie"]["fabrics"]["oversize"]],
            ["premium"],
        )
        self.assertEqual(config["products"]["hoodie"]["fabrics"]["oversize"][0]["price_delta"], 0)

    def test_normalize_snapshot_preserves_zone_sizes_and_sleeves(self):
        from storefront.custom_print_config import normalize_custom_print_snapshot

        normalized = normalize_custom_print_snapshot(
            {
                "product": {
                    "type": "hoodie",
                    "fit": "oversize",
                    "fabric": "premium",
                    "color": "graphite",
                },
                "print": {
                    "zones": ["front", "back", "sleeve"],
                    "zone_options": {
                        "front": {
                            "size_preset": "A4",
                        },
                        "back": {
                            "size_preset": "A2",
                        },
                        "sleeve": {
                            "left_enabled": True,
                            "right_enabled": True,
                            "left_mode": "full_text",
                            "left_text": "TWOCOMMS",
                            "right_mode": "a6",
                        },
                    },
                },
                "ui": {"current_step": "zones"},
            }
        )

        self.assertEqual(normalized["product"]["fit"], "oversize")
        self.assertEqual(normalized["product"]["fabric"], "premium")
        self.assertEqual(
            normalized["print"]["zone_options"]["front"]["size_preset"],
            "A4",
        )
        self.assertEqual(normalized["print"]["zone_options"]["back"]["size_preset"], "A2")
        self.assertTrue(normalized["print"]["zone_options"]["sleeve"]["left_enabled"])
        self.assertTrue(normalized["print"]["zone_options"]["sleeve"]["right_enabled"])
        self.assertEqual(normalized["print"]["zone_options"]["sleeve"]["left_mode"], "full_text")
        self.assertEqual(normalized["print"]["zone_options"]["sleeve"]["left_text"], "TWOCOMMS")

    def test_build_placement_specs_expand_back_and_both_sleeves(self):
        from storefront.custom_print_config import build_placement_specs

        specs = build_placement_specs(
            {
                "print": {
                    "zones": ["front", "back", "sleeve"],
                    "zone_options": {
                        "front": {
                            "size_preset": "A5",
                        },
                        "back": {
                            "size_preset": "A2",
                        },
                        "sleeve": {
                            "left_enabled": True,
                            "right_enabled": True,
                            "left_mode": "full_text",
                            "left_text": "LEFT TEXT",
                            "right_mode": "a6",
                        },
                    },
                }
            }
        )

        self.assertEqual(specs[0]["zone"], "front")
        self.assertEqual(specs[0]["size_preset"], "A5")
        self.assertEqual(specs[1]["zone"], "back")
        self.assertEqual(specs[1]["size_preset"], "A2")
        self.assertEqual(specs[2]["placement_key"], "sleeve_left")
        self.assertEqual(specs[2]["mode"], "full_text")
        self.assertEqual(specs[2]["text"], "LEFT TEXT")
        self.assertEqual(specs[3]["placement_key"], "sleeve_right")
        self.assertEqual(specs[3]["mode"], "a6")

    def test_home_page_contains_inline_custom_print_tile_inside_categories_grid(self):
        response = self._get(reverse("home"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "categories-top-grid")
        self.assertContains(response, "categories-top-grid--exact")
        self.assertContains(response, "categories-cta-wrap")
        self.assertContains(response, "Замовити кастомний одяг")
        self.assertContains(response, "category-card-custom-panel")
        self.assertContains(response, "category-card-custom-action")
        self.assertContains(response, reverse("custom_print"))
        self.assertNotContains(response, "custom-print-home-cta")

    def test_home_page_uses_custom_survey_and_pagination_shells(self):
        from storefront.models import Product
        from storefront.views.utils import HOME_PRODUCTS_PER_PAGE

        category = Category.objects.get(slug="tshirts")
        for index in range(1, HOME_PRODUCTS_PER_PAGE + 3):
            Product.objects.create(
                title=f"Тестовий товар {index}",
                slug=f"test-product-{index}",
                category=category,
                price=1000 + index,
                status="published",
            )

        response = self._get(reverse("home"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "survey-unified-block")
        self.assertContains(response, "survey-container-v3")
        self.assertContains(response, "Бонус за фідбек")
        self.assertContains(response, "data-survey-cta")
        self.assertContains(response, '<section class="mb-4 mb-md-5" id="survey-section">')
        self.assertContains(response, 'id="survey-content"')
        self.assertNotContains(response, '<section class="mb-4 mb-md-5 reveal" id="survey-section">')
        self.assertNotContains(response, 'id="featured-content"')
        self.assertNotContains(response, 'id="featuredToggle"')
        self.assertNotContains(response, "БІЛЬШЕ НЕ ПОКАЗУВАТИ")
        self.assertContains(response, "pagination-showcase")
        self.assertContains(response, "pagination-rail")
        self.assertNotContains(response, "page-selector-shell")
        self.assertNotContains(response, "Сторінка")

    @override_settings(MEDIA_ROOT=Path(tempfile.gettempdir()) / "twocomms-custom-print-tests")
    @patch("storefront.views.static_pages.notify_new_custom_print_lead")
    def test_custom_print_ready_submission_creates_lead_and_attachment(self, notify_mock):
        from storefront.models import CustomPrintLead, CustomPrintLeadAttachment

        payload = {
            "service_kind": "ready",
            "product_type": "tshirt",
            "placements": ["front"],
            "placement_specs_json": json.dumps(
                [
                    {
                        "zone": "front",
                        "label": "На грудях / спереду",
                        "variant": "standard",
                        "is_free": True,
                        "format": "standard",
                        "size": "standard",
                        "file_index": 0,
                    }
                ]
            ),
            "pricing_snapshot_json": json.dumps(
                {
                    "product_label": "Футболка",
                    "base_price": 700,
                    "design_price": 0,
                    "discount_percent": 0,
                    "discount_amount": 0,
                    "final_total": 700,
                    "estimate_required": False,
                }
            ),
            "quantity": "2",
            "size_mode": "mixed",
            "sizes_note": "M x1, L x1",
            "client_kind": "personal",
            "name": "Олена",
            "contact_channel": "telegram",
            "contact_value": "@olena_print",
            "brief": "",
            "fit": "regular",
            "fabric": "premium",
            "color_choice": "black",
            "file_triage_status": "print-ready",
            "config_draft_json": json.dumps(
                {
                    "version": 2,
                    "quick_start_mode": "have_file",
                    "mode": "personal",
                    "product": {
                        "type": "tshirt",
                        "fit": "regular",
                        "fabric": "premium",
                        "color": "black",
                    },
                    "print": {
                        "zones": ["front"],
                        "add_ons": [],
                        "placement_note": "",
                    },
                    "artwork": {
                        "service_kind": "ready",
                        "triage_status": "print-ready",
                        "files": [
                            {
                                "name": "design.png",
                                "zone": "front",
                                "status": "print-ready",
                            }
                        ],
                    },
                    "order": {
                        "quantity": 2,
                        "size_mode": "mixed",
                        "sizes_note": "M x1, L x1",
                        "gift": False,
                    },
                    "contact": {
                        "channel": "telegram",
                        "name": "Олена",
                        "value": "@olena_print",
                    },
                    "pricing": {
                        "base_price": 700,
                        "final_total": 700,
                        "estimate_required": False,
                    },
                    "ui": {"current_step": "review"},
                }
            ),
            "files": self._png_file(),
        }

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self._post(
                reverse("custom_print_lead"),
                payload,
                HTTP_X_REQUESTED_WITH="fetch",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(callbacks), 1)
        self.assertJSONEqual(
            response.content,
            {
                "ok": True,
                "lead_number": CustomPrintLead.objects.get().lead_number,
                "message": "Дякуємо! Менеджер зв’яжеться з вами найближчим часом.",
            },
        )
        lead = CustomPrintLead.objects.get()
        self.assertEqual(lead.service_kind, "ready")
        self.assertEqual(lead.product_type, "tshirt")
        self.assertEqual(lead.placements, ["front"])
        self.assertEqual(lead.contact_channel, "telegram")
        self.assertEqual(lead.contact_value, "@olena_print")
        self.assertEqual(lead.source, "main_custom_print")
        self.assertEqual(lead.fit, "regular")
        self.assertEqual(lead.fabric, "premium")
        self.assertEqual(lead.color_choice, "black")
        self.assertEqual(lead.file_triage_status, "print-ready")
        self.assertEqual(lead.pricing_snapshot_json["base_price"], 700)
        self.assertEqual(lead.placement_specs_json[0]["zone"], "front")
        self.assertEqual(lead.config_draft_json["quick_start_mode"], "have_file")
        self.assertEqual(lead.config_draft_json["artwork"]["triage_status"], "print-ready")
        attachment = CustomPrintLeadAttachment.objects.get(lead=lead)
        self.assertEqual(attachment.placement_zone, "front")
        self.assertEqual(attachment.attachment_role, "design")
        self.assertEqual(attachment.sort_order, 0)
        notify_mock.assert_called_once_with(lead)

    @patch("storefront.views.static_pages._send_custom_print_lead_capi")
    @patch("storefront.views.static_pages.notify_new_custom_print_lead")
    def test_custom_print_submission_keeps_browser_event_id_for_server_capi(self, notify_mock, capi_mock):
        from storefront.models import CustomPrintLead

        event_id = "cp-browser-event-123"
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self._post(
                reverse("custom_print_lead"),
                {
                    "service_kind": "design",
                    "product_type": "tshirt",
                    "placements": ["front"],
                    "placement_specs_json": json.dumps([{
                        "zone": "front",
                        "label": "Спереду",
                        "variant": "standard",
                        "is_free": True,
                        "format": "standard",
                        "size": "standard",
                        "file_index": 0,
                    }]),
                    "pricing_snapshot_json": json.dumps({"base_price": 700, "final_total": 700}),
                    "config_draft_json": json.dumps({"pricing": {"base_price": 700, "final_total": 700}}),
                    "quantity": "1",
                    "size_mode": "single",
                    "sizes_note": "M",
                    "client_kind": "personal",
                    "name": "Олена",
                    "contact_channel": "telegram",
                    "contact_value": "@olena_print",
                    "fit": "regular",
                    "fabric": "standard",
                    "brief": "Потрібен чистий друк.",
                    "color_choice": "black",
                    "file_triage_status": "needs-review",
                    "analytics_event_id": event_id,
                    "analytics_fbp": "fb.1.1710000000000.click_123",
                    "analytics_fbc": "fb.1.1710000000000.click_456",
                },
                HTTP_X_REQUESTED_WITH="fetch",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(callbacks), 2)
        self.assertEqual(response.json()["analytics_event_id"], event_id)
        lead = CustomPrintLead.objects.get()
        self.assertEqual(lead.config_draft_json["analytics"]["lead_event_id"], event_id)
        notify_mock.assert_called_once_with(lead)
        capi_mock.assert_called_once()
        self.assertEqual(capi_mock.call_args.args[0].pk, lead.pk)
        self.assertEqual(capi_mock.call_args.args[2], event_id)

    @override_settings(MEDIA_ROOT=Path(tempfile.gettempdir()) / "twocomms-custom-print-tests")
    @patch("storefront.views.static_pages.notify_new_custom_print_lead")
    def test_custom_print_adjust_submission_creates_lead(self, notify_mock):
        from storefront.models import CustomPrintLead

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self._post(
                reverse("custom_print_lead"),
                {
                    "service_kind": "adjust",
                    "product_type": "hoodie",
                    "placements": ["back", "sleeve"],
                    "placement_specs_json": json.dumps(
                        [
                            {
                                "zone": "back",
                                "label": "На спині",
                                "variant": "standard",
                                "is_free": True,
                                "format": "standard",
                                "size": "standard",
                                "file_index": 0,
                            },
                            {
                                "zone": "sleeve",
                                "label": "На рукаві",
                                "variant": "extra",
                                "is_free": False,
                                "format": "small",
                                "size": "S",
                            },
                        ]
                    ),
                    "pricing_snapshot_json": json.dumps(
                        {
                            "product_label": "Худі",
                            "base_price": 1600,
                            "design_price": 300,
                            "discount_percent": 10,
                            "discount_amount": 160,
                            "final_total": None,
                            "estimate_required": True,
                            "estimate_reason": "multi_zone",
                        }
                    ),
                    "quantity": "5",
                    "size_mode": "single",
                    "sizes_note": "L x5",
                    "client_kind": "brand",
                    "business_kind": "bulk",
                    "brand_name": "Void Unit",
                    "name": "Микита",
                    "contact_channel": "whatsapp",
                    "contact_value": "+380501112233",
                    "brief": "Потрібно прибрати фон і підчистити контур.",
                    "files": self._png_file("source.png"),
                },
                HTTP_X_REQUESTED_WITH="fetch",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(callbacks), 1)
        lead = CustomPrintLead.objects.get()
        self.assertEqual(lead.service_kind, "adjust")
        self.assertEqual(lead.client_kind, "brand")
        self.assertEqual(lead.business_kind, "bulk")
        self.assertEqual(lead.brand_name, "Void Unit")
        self.assertEqual(lead.placements, ["back", "sleeve"])
        self.assertEqual(lead.contact_channel, "whatsapp")
        self.assertTrue(lead.pricing_snapshot_json["estimate_required"])
        notify_mock.assert_called_once_with(lead)

    @patch("storefront.views.static_pages.notify_new_custom_print_lead")
    def test_custom_print_design_submission_allows_no_file(self, notify_mock):
        from storefront.models import CustomPrintLead, CustomPrintLeadAttachment

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self._post(
                reverse("custom_print_lead"),
                {
                    "service_kind": "design",
                    "product_type": "customer_garment",
                    "placements": ["custom"],
                    "placement_specs_json": json.dumps(
                        [
                            {
                                "zone": "custom",
                                "label": "Інше",
                                "variant": "estimate",
                                "is_free": False,
                                "format": "custom",
                                "size": "manager_review",
                            }
                        ]
                    ),
                    "pricing_snapshot_json": json.dumps(
                        {
                            "product_label": "Свій одяг",
                            "base_price": None,
                            "design_price": 500,
                            "discount_percent": 0,
                            "discount_amount": 0,
                            "final_total": None,
                            "estimate_required": True,
                            "estimate_reason": "customer_garment",
                        }
                    ),
                    "placement_note": "Хочу вертикальний принт по боковому шву та рукаву.",
                    "quantity": "3",
                    "size_mode": "manager",
                    "sizes_note": "S x1, M x2",
                    "client_kind": "personal",
                    "garment_note": "Моє темно-сіре худі без кишені, щільна тканина.",
                    "name": "Ігор",
                    "contact_channel": "phone",
                    "contact_value": "+380671234567",
                    "brief": "Потрібен мінімалістичний шрифт і техно-естетика.",
                },
                HTTP_X_REQUESTED_WITH="fetch",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(callbacks), 1)
        lead = CustomPrintLead.objects.get()
        self.assertEqual(lead.service_kind, "design")
        self.assertEqual(lead.product_type, "customer_garment")
        self.assertEqual(lead.placements, ["custom"])
        self.assertEqual(lead.placement_note, "Хочу вертикальний принт по боковому шву та рукаву.")
        self.assertEqual(lead.garment_note, "Моє темно-сіре худі без кишені, щільна тканина.")
        self.assertTrue(lead.pricing_snapshot_json["estimate_required"])
        self.assertEqual(CustomPrintLeadAttachment.objects.filter(lead=lead).count(), 0)
        notify_mock.assert_called_once_with(lead)

    def test_custom_print_adjust_requires_brief_and_file(self):
        response = self._post(
            reverse("custom_print_lead"),
            {
                "service_kind": "adjust",
                "product_type": "hoodie",
                "placements": ["front"],
                "quantity": "2",
                "sizes_note": "M x2",
                "name": "Тест",
                "contact_channel": "telegram",
                "contact_value": "@test",
            },
            HTTP_X_REQUESTED_WITH="fetch",
        )

        self.assertEqual(response.status_code, 400)
        self.assertJSONEqual(
            response.content,
            {
                "ok": False,
                "errors": {
                    "brief": ["Опишіть, що саме потрібно змінити у файлі."],
                },
            },
        )

    @patch("storefront.views.static_pages.notify_custom_print_moderation_request")
    def test_custom_print_add_to_cart_creates_awaiting_review_lead_session_entry_and_notifies_manager(self, notify_mock):
        from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY
        from storefront.models import CustomPrintLead, CustomPrintModerationStatus

        response = self._post(
            reverse("custom_print_add_to_cart"),
            {
                "service_kind": "ready",
                "product_type": "hoodie",
                "placements": ["front"],
                "placement_specs_json": json.dumps(
                    [
                        {
                            "zone": "front",
                            "placement_key": "front",
                            "label": "Спереду",
                            "size_preset": "A4",
                            "requires_artwork_file": True,
                            "file_index": 0,
                        }
                    ]
                ),
                "pricing_snapshot_json": json.dumps(
                    {
                        "product_label": "Худі",
                        "base_price": 1800,
                        "design_price": 0,
                        "discount_percent": 0,
                        "discount_amount": 0,
                        "final_total": 1800,
                        "estimate_required": False,
                    }
                ),
                "config_draft_json": json.dumps(
                    {
                        "version": 2,
                        "mode": "personal",
                        "product": {
                            "type": "hoodie",
                            "fit": "oversize",
                            "fabric": "premium",
                            "color": "graphite",
                        },
                        "print": {
                            "zones": ["front"],
                            "zone_options": {"front": {"size_preset": "A4"}},
                        },
                        "artwork": {
                            "service_kind": "ready",
                            "triage_status": "print-ready",
                            "files": [{"name": "front.png", "zone": "front", "status": "print-ready"}],
                        },
                        "order": {
                            "quantity": 1,
                            "size_mode": "single",
                            "sizes_note": "L x1",
                        },
                        "contact": {
                            "channel": "telegram",
                            "name": "Тест",
                            "value": "@test",
                        },
                        "pricing": {
                            "base_price": 1800,
                            "final_total": 1800,
                            "estimate_required": False,
                        },
                        "ui": {"current_step": "contact"},
                    }
                ),
                "quantity": "1",
                "size_mode": "single",
                "sizes_note": "L x1",
                "client_kind": "personal",
                "name": "Тест",
                "contact_channel": "telegram",
                "contact_value": "@test",
                "fit": "oversize",
                "fabric": "premium",
                "color_choice": "graphite",
                "file_triage_status": "print-ready",
                "files": self._png_file("front.png"),
            },
            HTTP_X_REQUESTED_WITH="fetch",
        )

        self.assertEqual(response.status_code, 200)
        lead = CustomPrintLead.objects.get()
        self.assertEqual(lead.source, "custom_print_cart")
        self.assertEqual(lead.moderation_status, CustomPrintModerationStatus.AWAITING_REVIEW)
        self.assertTrue(lead.moderation_token)
        payload = json.loads(response.content)
        self.assertTrue(payload["ok"])
        session_cart = self.client.session[SESSION_CUSTOM_CART_KEY]
        self.assertIn(f"custom:{lead.pk}", session_cart)
        self.assertEqual(
            session_cart[f"custom:{lead.pk}"]["moderation_status"],
            CustomPrintModerationStatus.AWAITING_REVIEW,
        )
        notify_mock.assert_called_once_with(lead)

    def test_cart_mini_renders_custom_item_with_remove_button_without_regular_cart_products(self):
        from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY
        from storefront.models import CustomPrintLead, CustomPrintModerationStatus

        lead = CustomPrintLead.objects.create(
            service_kind="ready",
            product_type="hoodie",
            placements=["front"],
            placement_specs_json=[{"zone": "front", "placement_key": "front", "label": "Спереду"}],
            quantity=1,
            size_mode="single",
            sizes_note="L x1",
            client_kind="personal",
            name="Тест",
            contact_channel="telegram",
            contact_value="@test",
            brief="",
            fit="oversize",
            fabric="premium",
            color_choice="graphite",
            source="custom_print_cart",
            moderation_status=CustomPrintModerationStatus.AWAITING_REVIEW,
            pricing_snapshot_json={"final_total": 1800},
            config_draft_json={"pricing": {"final_total": 1800}},
        )
        session = self.client.session
        session[SESSION_CUSTOM_CART_KEY] = {
            f"custom:{lead.pk}": {
                "lead_id": lead.pk,
                "lead_number": lead.lead_number,
                "label": "Худі · front",
                "product_label": "Худі",
                "quantity": 1,
                "final_total": 1800,
                "moderation_status": CustomPrintModerationStatus.AWAITING_REVIEW,
            }
        }
        session.save()

        response = self._get(reverse("cart_mini"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, lead.lead_number)
        self.assertContains(response, "Кастомний друк")
        self.assertContains(response, "На перевірці")
        self.assertContains(response, "data-custom-remove")
        self.assertContains(response, "Видалити")
        self.assertNotContains(response, "Кошик порожній.")

    def test_custom_print_remove_deletes_item_from_session(self):
        from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY

        session = self.client.session
        session[SESSION_CUSTOM_CART_KEY] = {
            "custom:77": {
                "lead_id": 77,
                "lead_number": "CP-77",
                "label": "Худі · Спереду",
                "quantity": 1,
                "final_total": 1800,
            }
        }
        session.save()

        response = self._post(
            reverse("custom_print_remove"),
            {"key": "custom:77"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {
                "ok": True,
                "removed": True,
                "custom_cart_count": 0,
            },
        )
        self.assertEqual(self.client.session.get(SESSION_CUSTOM_CART_KEY), {})

    def test_custom_print_remove_resets_pending_monobank_invoice(self):
        from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY

        session = self.client.session
        session[SESSION_CUSTOM_CART_KEY] = {
            "custom:77": {
                "lead_id": 77,
                "lead_number": "CP-77",
                "label": "Худі · Спереду",
                "quantity": 1,
                "final_total": 1800,
            }
        }
        session["monobank_invoice_id"] = "inv-stale"
        session["monobank_pending_order_id"] = 123
        session.save()

        response = self._post(
            reverse("custom_print_remove"),
            {"key": "custom:77"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        session = self.client.session
        self.assertNotIn("monobank_invoice_id", session)
        self.assertNotIn("monobank_pending_order_id", session)

    def test_cart_page_renders_custom_only_pending_item_with_full_details_and_no_submit_review_step(self):
        from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY
        from storefront.models import CustomPrintLead, CustomPrintModerationStatus

        lead = CustomPrintLead.objects.create(
            service_kind="adjust",
            product_type="hoodie",
            placements=["front", "back"],
            placement_specs_json=[
                {
                    "zone": "front",
                    "placement_key": "front",
                    "label": "Спереду",
                    "size_preset": "A4",
                },
                {
                    "zone": "back",
                    "placement_key": "back",
                    "label": "На спині",
                    "size_preset": "A3",
                },
            ],
            quantity=3,
            size_mode="mixed",
            sizes_note="M x2, L x1",
            client_kind="brand",
            name="Тест",
            contact_channel="telegram",
            contact_value="@test",
            brief="Потрібна адаптація логотипу",
            fit="oversize",
            fabric="premium",
            color_choice="graphite",
            file_triage_status="needs-work",
            source="custom_print_cart",
            moderation_status=CustomPrintModerationStatus.AWAITING_REVIEW,
            pricing_snapshot_json={"final_total": 5400, "unit_total": 1800},
            config_draft_json={
                "mode": "brand",
                "product": {
                    "type": "hoodie",
                    "fit": "oversize",
                    "fabric": "premium",
                    "color": "graphite",
                },
                "print": {
                    "zones": ["front", "back"],
                    "add_ons": ["lacing"],
                },
                "artwork": {
                    "service_kind": "adjust",
                    "triage_status": "needs-work",
                },
                "order": {
                    "quantity": 3,
                    "size_mode": "mixed",
                    "size_breakdown": {"M": 2, "L": 1},
                    "gift": {"enabled": True, "text": "Подарункова упаковка"},
                },
                "pricing": {
                    "unit_total": 1800,
                    "final_total": 5400,
                },
            },
        )
        session = self.client.session
        session[SESSION_CUSTOM_CART_KEY] = {
            f"custom:{lead.pk}": {
                "lead_id": lead.pk,
                "lead_number": lead.lead_number,
                "label": "Худі · Спереду, На спині",
                "product_label": "Худі",
                "quantity": 3,
                "final_total": 5400,
                "unit_total": 1800,
                "moderation_status": CustomPrintModerationStatus.AWAITING_REVIEW,
                "zone_labels": ["Спереду", "На спині"],
                "size_breakdown": {"M": 2, "L": 1},
                "fit": "oversize",
                "fabric": "premium",
                "color": "graphite",
                "mode": "brand",
                "gift_enabled": True,
                "gift_text": "Подарункова упаковка",
                "service_kind": "adjust",
                "file_triage_status": "needs-work",
                "add_on_labels": ["Люверси зі шнурками"],
            }
        }
        session.save()

        response = self._get(reverse("cart"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, lead.lead_number)
        self.assertContains(response, "Кастомний друк")
        self.assertContains(response, "На перевірці менеджера")
        self.assertContains(response, "Написати менеджеру в Telegram")
        self.assertContains(response, "Худі")
        self.assertContains(response, "Оверсайз")
        self.assertContains(response, "Преміум")
        self.assertContains(response, "graphite")
        self.assertContains(response, "Потрібно допрацювати")
        self.assertContains(response, "Потрібна підготовка")
        self.assertContains(response, "Люверси зі шнурками")
        self.assertContains(response, "M×2, L×1")
        self.assertContains(response, "Подарункова упаковка")
        self.assertContains(response, "Ціна узгоджується після модерації")
        self.assertContains(response, f'data-custom-key="custom:{lead.pk}"', html=False)
        self.assertNotContains(response, "Відправити менеджеру на перевірку")

    def test_cart_items_api_keeps_custom_only_cart_non_empty_and_excludes_pending_custom_from_payable_total(self):
        from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY
        from storefront.models import CustomPrintLead, CustomPrintModerationStatus

        lead = CustomPrintLead.objects.create(
            service_kind="ready",
            product_type="hoodie",
            placements=["front"],
            placement_specs_json=[{"zone": "front", "placement_key": "front", "label": "Спереду"}],
            quantity=1,
            size_mode="single",
            sizes_note="L x1",
            client_kind="personal",
            name="Тест",
            contact_channel="telegram",
            contact_value="@test",
            fit="oversize",
            fabric="premium",
            color_choice="graphite",
            source="custom_print_cart",
            moderation_status=CustomPrintModerationStatus.AWAITING_REVIEW,
            pricing_snapshot_json={"final_total": 1800, "unit_total": 1800},
            config_draft_json={
                "product": {"type": "hoodie", "fit": "oversize", "fabric": "premium", "color": "graphite"},
                "print": {"zones": ["front"]},
                "artwork": {"service_kind": "ready", "triage_status": "print-ready"},
                "order": {"quantity": 1, "size_mode": "single", "sizes_note": "L x1"},
                "pricing": {"final_total": 1800, "unit_total": 1800},
            },
        )
        session = self.client.session
        session[SESSION_CUSTOM_CART_KEY] = {
            f"custom:{lead.pk}": {
                "lead_id": lead.pk,
                "lead_number": lead.lead_number,
                "label": "Худі · Спереду",
                "product_label": "Худі",
                "quantity": 1,
                "final_total": 1800,
                "unit_total": 1800,
                "moderation_status": CustomPrintModerationStatus.AWAITING_REVIEW,
                "zone_labels": ["Спереду"],
                "fit": "oversize",
                "fabric": "premium",
                "color": "graphite",
                "service_kind": "ready",
                "file_triage_status": "print-ready",
            }
        }
        session.save()

        response = self._get(reverse("cart_items_api"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["items"], [])
        self.assertEqual(len(payload["custom_items"]), 1)
        self.assertEqual(payload["items_count"], 1)
        self.assertEqual(payload["positions_count"], 1)
        self.assertTrue(payload["has_custom_items"])
        self.assertEqual(payload["combined_total"], 1800.0)
        self.assertEqual(payload["approved_total"], 0.0)
        self.assertFalse(payload["payment_allowed"])

    def test_custom_print_design_requires_brief(self):
        response = self._post(
            reverse("custom_print_lead"),
            {
                "service_kind": "design",
                "product_type": "tshirt",
                "placements": ["back"],
                "quantity": "1",
                "sizes_note": "L x1",
                "name": "Тест",
                "contact_channel": "telegram",
                "contact_value": "@test",
            },
            HTTP_X_REQUESTED_WITH="fetch",
        )

        self.assertEqual(response.status_code, 400)
        self.assertJSONEqual(
            response.content,
            {
                "ok": False,
                "errors": {
                    "brief": ["Опишіть ідею, стиль або референси для дизайну."],
                },
            },
        )

    def test_custom_print_custom_placement_requires_note(self):
        response = self._post(
            reverse("custom_print_lead"),
            {
                "service_kind": "design",
                "product_type": "longsleeve",
                "placements": ["custom"],
                "placement_specs_json": json.dumps([{"zone": "custom"}]),
                "quantity": "1",
                "sizes_note": "M x1",
                "name": "Тест",
                "contact_channel": "telegram",
                "contact_value": "@test",
                "brief": "Зробіть щось сміливе.",
            },
            HTTP_X_REQUESTED_WITH="fetch",
        )

        self.assertEqual(response.status_code, 400)
        self.assertJSONEqual(
            response.content,
            {
                "ok": False,
                "errors": {
                    "placement_note": ["Опишіть нестандартне розміщення принта."],
                },
            },
        )

    def test_custom_print_phone_channel_validates_contact_value(self):
        response = self._post(
            reverse("custom_print_lead"),
            {
                "service_kind": "design",
                "product_type": "tshirt",
                "placements": ["front"],
                "placement_specs_json": json.dumps([{"zone": "front"}]),
                "quantity": "1",
                "sizes_note": "M x1",
                "name": "Тест",
                "contact_channel": "phone",
                "contact_value": "abc",
                "brief": "Потрібен дизайн.",
            },
            HTTP_X_REQUESTED_WITH="fetch",
        )

        self.assertEqual(response.status_code, 400)
        self.assertJSONEqual(
            response.content,
            {
                "ok": False,
                "errors": {
                    "contact_value": ["Введіть коректний номер телефону."],
                },
            },
        )

    @patch("storefront.views.static_pages.notify_custom_print_safe_exit")
    def test_custom_print_safe_exit_with_contact_persists_snapshot_and_returns_manager_link(self, notify_mock):
        from storefront.models import CustomPrintLead

        snapshot = {
            "version": 2,
            "quick_start_mode": "start_blank",
            "mode": "brand",
            "product": {
                "type": "hoodie",
                "fit": "oversize",
                "fabric": "premium",
                "color": "graphite",
            },
            "print": {
                "zones": ["back", "sleeve"],
                "add_ons": ["grommets"],
                "placement_note": "",
            },
            "artwork": {
                "service_kind": "adjust",
                "triage_status": "needs-work",
                "files": [{"name": "logo.png", "zone": "back", "status": "needs-work"}],
            },
            "order": {
                "quantity": 12,
                "size_mode": "mixed",
                "sizes_note": "M x6, L x6",
                "gift": False,
            },
            "contact": {
                "channel": "telegram",
                "name": "Микита",
                "value": "@void_unit",
            },
            "pricing": {
                "base_price": 1850,
                "final_total": None,
                "estimate_required": True,
            },
            "ui": {"current_step": "artwork"},
        }

        response = self.client.post(
            reverse("custom_print_safe_exit"),
            data=json.dumps(snapshot),
            content_type="application/json",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        lead = CustomPrintLead.objects.get()
        self.assertEqual(lead.source, "custom_print_safe_exit")
        self.assertEqual(lead.product_type, "hoodie")
        self.assertEqual(lead.fit, "oversize")
        self.assertEqual(lead.fabric, "premium")
        self.assertEqual(lead.color_choice, "graphite")
        self.assertEqual(lead.file_triage_status, "needs-work")
        self.assertEqual(lead.exit_step, "artwork")
        self.assertEqual(lead.config_draft_json["print"]["add_ons"], ["lacing"])
        payload = json.loads(response.content)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["lead_number"], lead.lead_number)
        self.assertIn("t.me", payload["manager_url"])
        notify_mock.assert_called_once_with(lead=lead, snapshot=lead.config_draft_json)

    @patch("storefront.views.static_pages.notify_custom_print_safe_exit")
    def test_custom_print_safe_exit_without_contact_still_notifies(self, notify_mock):
        from storefront.models import CustomPrintLead

        response = self.client.post(
            reverse("custom_print_safe_exit"),
            data=json.dumps(
                {
                    "version": 2,
                    "quick_start_mode": "starter_style",
                    "mode": "personal",
                    "product": {"type": "hoodie", "fit": "regular", "fabric": "standard", "color": "sand"},
                    "print": {"zones": ["front"], "add_ons": [], "placement_note": ""},
                    "artwork": {"service_kind": "design", "triage_status": "needs-review", "files": []},
                    "order": {"quantity": 1, "size_mode": "single", "sizes_note": "M x1", "gift": True},
                    "contact": {"channel": "", "name": "", "value": ""},
                    "pricing": {"base_price": 1600, "final_total": None, "estimate_required": True},
                    "ui": {"current_step": "review"},
                }
            ),
            content_type="application/json",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CustomPrintLead.objects.count(), 0)
        payload = json.loads(response.content)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["lead_number"])
        self.assertIn("t.me", payload["manager_url"])
        notify_mock.assert_called_once()


@override_settings(COMPRESS_ENABLED=False, COMPRESS_OFFLINE=False)
class CustomPrintAdminAndNotificationTests(TestCase):
    def setUp(self):
        self.client = Client(
            HTTP_HOST="twocomms.shop",
            SERVER_PORT="443",
            **{"wsgi.url_scheme": "https"},
        )
        self.staff_user = User.objects.create_user(
            username="staff",
            password="secret123",
            is_staff=True,
        )

    def _make_lead(self, **overrides):
        from storefront.models import CustomPrintLead

        payload = {
            "service_kind": "adjust",
            "product_type": "hoodie",
            "placements": ["back", "sleeve"],
            "placement_note": "",
            "placement_specs_json": [
                {
                    "zone": "back",
                    "label": "На спині",
                    "variant": "standard",
                    "is_free": True,
                    "format": "standard",
                    "size": "standard",
                },
                {
                    "zone": "sleeve",
                    "label": "На рукаві",
                    "variant": "extra",
                    "is_free": False,
                    "format": "small",
                    "size": "S",
                },
            ],
            "quantity": 12,
            "size_mode": "mixed",
            "sizes_note": "M x6, L x6",
            "client_kind": "brand",
            "business_kind": "branding",
            "brand_name": "Void Unit",
            "garment_note": "",
            "name": "Микита",
            "contact_channel": "telegram",
            "contact_value": "@void_unit",
            "brief": "Тестовий lead",
            "pricing_snapshot_json": {
                "product_label": "Худі",
                "base_price": 1600,
                "design_price": 300,
                "discount_percent": 5,
                "discount_amount": 80,
                "final_total": None,
                "estimate_required": True,
                "estimate_reason": "multi_zone",
            },
            "source": "main_custom_print",
        }
        payload.update(overrides)
        return CustomPrintLead.objects.create(**payload)

    def test_staff_admin_panel_renders_custom_print_orders_section(self):
        self.client.force_login(self.staff_user)
        lead = self._make_lead()

        response = self.client.get(
            reverse("admin_panel"),
            {"section": "custom_print_orders", "lead": lead.pk},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Кастомні замовлення")
        self.assertContains(response, lead.lead_number)
        self.assertContains(response, "В роботі", count=0)
        self.assertContains(response, "data-custom-print-lead-detail")

    def test_staff_can_update_custom_print_lead_status_from_custom_admin(self):
        lead = self._make_lead()
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse("admin_custom_print_lead_status", args=[lead.pk]),
            data=json.dumps({"status": "in_progress"}),
            content_type="application/json",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        lead.refresh_from_db()
        self.assertEqual(lead.status, "in_progress")
        self.assertJSONEqual(response.content, {"success": True, "status": "in_progress"})

    def test_staff_can_approve_custom_print_lead_from_custom_admin(self):
        from storefront.models import CustomPrintModerationStatus

        lead = self._make_lead(
            source="custom_print_cart",
            moderation_status=CustomPrintModerationStatus.AWAITING_REVIEW,
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse("admin_custom_print_lead_moderation", args=[lead.pk]),
            data=json.dumps({"action": "approve", "price": "2450.50", "note": "Підтверджено менеджером"}),
            content_type="application/json",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        lead.refresh_from_db()
        self.assertEqual(lead.moderation_status, CustomPrintModerationStatus.APPROVED)
        self.assertEqual(str(lead.approved_price), "2450.50")
        self.assertEqual(lead.manager_note, "Підтверджено менеджером")
        self.assertJSONEqual(
            response.content,
            {
                "success": True,
                "moderation_status": "approved",
                "approved_price": "2450.50",
                "manager_note": "Підтверджено менеджером",
            },
        )

    def test_staff_cannot_approve_custom_print_lead_without_positive_final_price(self):
        from storefront.models import CustomPrintModerationStatus

        lead = self._make_lead(
            source="custom_print_cart",
            product_type="customer_garment",
            pricing_snapshot_json={"final_total": None, "estimate_required": True},
            moderation_status=CustomPrintModerationStatus.AWAITING_REVIEW,
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse("admin_custom_print_lead_moderation", args=[lead.pk]),
            data=json.dumps({"action": "approve", "note": "Без ціни"}),
            content_type="application/json",
            secure=True,
        )

        self.assertEqual(response.status_code, 400)
        lead.refresh_from_db()
        self.assertEqual(lead.moderation_status, CustomPrintModerationStatus.AWAITING_REVIEW)
        self.assertJSONEqual(
            response.content,
            {
                "success": False,
                "error": "Для погодження потрібно вказати фінальну ціну більше 0 грн.",
            },
        )

    def test_custom_print_notification_uses_custom_admin_deeplink(self):
        from storefront.custom_print_notifications import _build_admin_panel_link, _build_message

        lead = self._make_lead()
        link = _build_admin_panel_link(lead)
        message = _build_message(lead)

        self.assertEqual(
            link,
            f"https://twocomms.shop/admin-panel/?section=custom_print_orders&lead={lead.pk}",
        )
        self.assertIn(link, message)
        self.assertNotIn("/admin/storefront/customprintlead/", message)

    def test_custom_print_safe_exit_notification_uses_custom_admin_deeplink_when_lead_exists(self):
        from storefront.custom_print_notifications import _build_safe_exit_message

        lead = self._make_lead(
            source="custom_print_safe_exit",
            fit="oversize",
            fabric="premium",
            color_choice="graphite",
            file_triage_status="needs-work",
            exit_step="artwork",
            config_draft_json={
                "version": 2,
                "quick_start_mode": "have_file",
                "mode": "brand",
                "product": {"type": "hoodie", "fit": "oversize", "fabric": "premium", "color": "graphite"},
                "print": {"zones": ["back", "sleeve"], "add_ons": ["grommets"], "placement_note": ""},
                "artwork": {"service_kind": "adjust", "triage_status": "needs-work", "files": []},
                "order": {"quantity": 12, "size_mode": "mixed", "sizes_note": "M x6, L x6", "gift": False},
                "contact": {"channel": "telegram", "name": "Микита", "value": "@void_unit"},
                "pricing": {"base_price": 1850, "final_total": None, "estimate_required": True},
                "ui": {"current_step": "artwork"},
            },
        )

        message = _build_safe_exit_message(lead.config_draft_json, lead=lead)

        self.assertIn("safe exit", message.lower())
        self.assertIn(lead.lead_number, message)
        self.assertIn(f"https://twocomms.shop/admin-panel/?section=custom_print_orders&lead={lead.pk}", message)
