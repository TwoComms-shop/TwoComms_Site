"""Owner brief Q01–Q07/Q17/Q19/Q20 across actual public projections."""
from copy import deepcopy
import html
import json
from pathlib import Path
import re

from django.template import Context, Template
from django.contrib.staticfiles import finders
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils.translation import override

from storefront.custom_print_config import (
    CUSTOM_REF_PREVIEW_ASSETS,
    PRODUCT_MATRIX,
    build_custom_print_config,
    normalize_custom_print_snapshot,
    resolve_fit_label,
    resolve_preview_render,
)
from storefront.support_content import CARE_FAQ_ITEMS, SUPPORT_PAGE_DEFINITIONS
from storefront.views.static_pages import _build_page_context


class OwnerContentConsistencyTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        source = (Path(__file__).resolve().parents[2] /
                  "twocomms_django_theme/templates/pages/support_page.html").read_text()
        # Render the actual visible content and actual structured-data blocks.
        # Excluding base.html avoids unrelated account/navigation/database work.
        blocks = [source.split("{% block " + name + " %}", 1)[1].split("{% endblock %}", 1)[0]
                  for name in ("content", "structured_data")]
        cls.template = Template("{% load seo_tags storefront_filters i18n %}" + "\n".join(blocks))

    def _render(self, page_key, language):
        request = RequestFactory().get("/" + page_key + "/")
        request.LANGUAGE_CODE = language
        context = _build_page_context(request, page_key)
        rendered = html.unescape(self.template.render(Context(context)))
        schemas = [json.loads(value) for value in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', rendered, re.S)]
        faq = next(value for value in schemas if isinstance(value, dict) and value.get("@type") == "FAQPage")
        visible = re.sub(r"<script\b[^>]*>.*?</script>", "", rendered, flags=re.S)
        return visible, faq, context, schemas

    def test_reviewed_care_and_faq_are_visible_and_equal_to_schema_in_all_languages(self):
        for language in ("uk", "ru", "en"):
            for page_key in ("faq", "care_guide"):
                with self.subTest(language=language, page=page_key), override(language):
                    rendered, faq, _, _ = self._render(page_key, language)
                    for item in faq["mainEntity"]:
                        self.assertIn(item["name"], rendered)
                        self.assertIn(item["acceptedAnswer"]["text"], rendered)
                    self.assertIn("100", json.dumps(faq, ensure_ascii=False))
                    self.assertIn("30 °C", rendered)
                    for obsolete in ("30+", "50+", "12 місяц", "12 месяц", "12 months", "40 °C"):
                        self.assertNotIn(obsolete, rendered)
                    self.assertIn({"uk": "оцінкою засновника", "ru": "оценке основателя", "en": "founder estimates"}[language], rendered)

    def test_care_faq_has_real_translations_and_no_lab_or_replacement_promise(self):
        with override("uk"):
            source = [(str(item["question"]), str(item["answer"])) for item in CARE_FAQ_ITEMS]
        for language in ("ru", "en"):
            with self.subTest(language=language), override(language):
                _, faq, _, _ = self._render("care_guide", language)
                self.assertEqual(len(faq["mainEntity"]), len(source))
                for original, translated in zip(source, faq["mainEntity"]):
                    self.assertNotEqual(translated["name"], original[0])
                    self.assertNotEqual(translated["acceptedAnswer"]["text"], original[1])
        with override("en"):
            rendered, _, _, _ = self._render("care_guide", "en")
            self.assertIn("not a laboratory test result", rendered)
            self.assertIn("do not promise an automatic replacement", rendered)

    def test_faq_material_cut_qc_price_and_wholesale_topics_survive_locale_overrides(self):
        for language in ("uk", "ru", "en"):
            with self.subTest(language=language), override(language):
                _, faq, _, _ = self._render("faq", language)
                text = json.dumps(faq, ensure_ascii=False)
                for required in ("95%", "5%", "190", "220", "90%", "10%", "320", "Instagram", "10–20%"):
                    self.assertIn(required, text)
                for obsolete in ("35 %", "15 %", "п’ять стадій", "пять стадий", "five stages"):
                    self.assertNotIn(obsolete, text)
                self.assertIn({"uk": "три етапи", "ru": "три этапа", "en": "three quality-control stages"}[language], text)
                self.assertIn({"uk": "злегка oversize", "ru": "слегка oversize", "en": "slightly oversized"}[language], text)

    def test_care_howto_schema_also_uses_current_localized_instructions(self):
        with override("uk"):
            source = SUPPORT_PAGE_DEFINITIONS["care_guide"]["how_to"]
            original = [(str(item["name"]), str(item["text"])) for item in source["step"]]
        for language in ("ru", "en"):
            with self.subTest(language=language), override(language):
                _, _, _, schemas = self._render("care_guide", language)
                howto = next(item for item in schemas if isinstance(item, dict) and item.get("@type") == "HowTo")
                self.assertIn("30 °C", howto["description"])
                for old, current in zip(original, howto["step"]):
                    self.assertNotEqual(current["name"], old[0])
                    self.assertNotEqual(current["text"], old[1])

    @override_settings(FREE_SHIPPING_THRESHOLD="2750")
    def test_actual_delivery_projection_preserves_dynamic_after_discount_full_payment_condition(self):
        for language in ("uk", "ru", "en"):
            with self.subTest(language=language), override(language):
                rendered, faq, _, _ = self._render("delivery", language)
                text = json.dumps(faq, ensure_ascii=False)
                self.assertIn("2750", text)
                self.assertIn("200", text)
                self.assertIn({"uk": "після знижок і лише за повної оплати", "ru": "после скидок и только при полной оплате", "en": "after discounts, only with full payment"}[language], rendered)
                self.assertNotIn("1%", text)

    def test_new_hoodie_choice_and_default_are_one_actual_cut_without_price_edits(self):
        before = deepcopy(PRODUCT_MATRIX)
        config = build_custom_print_config(submit_url="/submit/", safe_exit_url="/exit/")
        hoodie = config["products"]["hoodie"]
        self.assertEqual([item["value"] for item in hoodie["fits"]], ["oversize"])
        self.assertEqual(hoodie["default_fit"], "oversize")
        self.assertEqual(config["defaults"]["product"]["fit"], "oversize")
        self.assertEqual(config["defaults"]["product"]["fabric"], "premium")
        self.assertEqual(config["stage_profiles"]["hoodie"]["default_fit"], "oversize")
        self.assertEqual(hoodie["pricing"], before["hoodie"]["pricing"])
        self.assertEqual(hoodie["fabrics"], before["hoodie"]["fabrics"])
        self.assertEqual(PRODUCT_MATRIX, before)

    def test_legacy_regular_reads_preserve_fit_fabric_price_and_preview(self):
        for fabric in ("standard", "premium"):
            with self.subTest(fabric=fabric):
                legacy = {"product": {"type": "hoodie", "fit": "regular", "fabric": fabric, "color": "black"},
                          "pricing": {"base_price": 1600, "unit_total": 1850, "final_total": 1850}}
                normalized = normalize_custom_print_snapshot(legacy)
                self.assertEqual(normalized["product"], legacy["product"])
                for field in legacy["pricing"]:
                    self.assertEqual(normalized["pricing"][field], legacy["pricing"][field])
                self.assertEqual(normalized["ui"]["preview_render"]["profile"], "hoodie:regular")
                self.assertTrue(resolve_fit_label("hoodie", "regular"))
        missing_fabric = normalize_custom_print_snapshot({"product": {"type": "hoodie", "fit": "regular"}})
        self.assertEqual(missing_fabric["product"]["fabric"], "standard")

    def test_preview_front_back_remain_available_with_honest_label_and_color_fallback(self):
        for language in ("uk", "ru", "en"):
            with self.subTest(language=language), override(language):
                config = build_custom_print_config(submit_url="/submit/", safe_exit_url="/exit/")
                note = str(config["products"]["hoodie"]["hero_note"])
                self.assertIn({"uk": "не фото конкретного виробу", "ru": "не фото конкретного изделия", "en": "not photos of a specific garment"}[language], note)
        for fit in ("regular", "oversize"):
            preview = resolve_preview_render("hoodie", fit, "graphite")
            self.assertTrue(preview["fallback_used"])
            self.assertEqual(preview["selected_color"], "graphite")
            self.assertEqual(preview["preview_color"], "black")
            pair = CUSTOM_REF_PREVIEW_ASSETS[preview["profile"]]["black"]
            self.assertEqual(set(pair), {"front", "back"})
            for side in pair.values():
                self.assertTrue(finders.find(side["webp"].removeprefix("/static/")), side["webp"])
