"""Category breadth, actual navigation authority and immutable card compatibility."""
import ast
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from management.services import ig_catalog_category_discovery as discovery
from management.services import ig_catalog_choice_presentations as choices
from management.services.ig_revision_outbox import _canonical, _normalize_specs, _safe_bindings
from management import tests_ig_catalog_choice_presentations as presentation_fixtures


@override_settings(SITE_BASE_URL="https://twocomms.test", SECRET_KEY="category-breadth-test")
class CatalogCategoryDiscoveryTests(SimpleTestCase):
    def category(self, product_id=101, *, slug="hoodie", name="Худі", locale="uk", category_id=9):
        return {"product_id": product_id, "category_id": category_id, "slug": slug,
            "name": name, "locale": locale, "url": discovery.category_url(slug, locale)}

    def prepared(self, *, schema=choices.SCHEMA):
        fixture = presentation_fixtures.CatalogChoicePresentationTests()
        cards = fixture.cards()
        if schema == choices.SCHEMA:
            for card in cards:
                card["category"] = self.category(card["product_id"])
        return choices.build_catalog_choice_effects(12, fixture.scope(), cards, _schema=schema)

    def legacy_function(self, name, **namespace):
        path = Path(__file__).parent / "services" / "instagram_bot.py"
        tree = ast.parse(path.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        return namespace[name]

    def test_actual_route_preserves_category_and_language_without_hardcoded_catalog_mapping(self):
        for slug in ("hoodie", "tshirts", "long-sleeve", "future-category"):
            for locale, prefix in (("uk", ""), ("ru", "/ru"), ("en", "/en")):
                category = self.category(slug=slug, locale=locale)
                self.assertEqual(category["url"], f"https://twocomms.test{prefix}/catalog/{slug}/")
                self.assertTrue(discovery.validate_category(category, product_id=101))
                self.assertLessEqual(len(discovery.category_button_label(category)), 20)
        self.assertEqual(discovery.category_button_label(self.category(slug="future-category")), "Вся категорія")
        self.assertEqual(discovery.category_button_label(self.category(slug="hoodie", locale="ru")), "Все худи")

    def test_origin_and_detached_category_url_are_rejected(self):
        for origin in ("http://twocomms.test", "https://user@twocomms.test", "https://twocomms.test/path",
            "https://twocomms.test?x=1", "https://twocomms.test#fragment", "https://twocomms.test:444"):
            with self.subTest(origin=origin), override_settings(SITE_BASE_URL=origin):
                with self.assertRaises(ValueError):
                    discovery.category_url("hoodie")
        category = self.category()
        category["url"] = "https://elsewhere.test/catalog/hoodie/"
        self.assertFalse(discovery.validate_category(category, product_id=101))
        self.assertFalse(discovery.validate_category(self.category(), product_id=102))

    def test_capture_uses_real_published_product_and_active_category_relation(self):
        category = SimpleNamespace(pk=91, slug="future-category", name="Future category")
        with patch("storefront.models.Product.objects.filter") as queried:
            query = queried.return_value.select_related.return_value.only.return_value
            query.first.return_value = SimpleNamespace(pk=101, category=category)
            result = discovery.capture_product_category(101, "en")
            self.assertEqual(queried.call_args.kwargs, {"pk": 101, "status": "published", "category__is_active": True})
            self.assertEqual(result["category_id"], 91)
            self.assertEqual(result["url"], "https://twocomms.test/en/catalog/future-category/")
            query.first.return_value = None
            with self.assertRaisesRegex(ValueError, "unavailable"):
                discovery.capture_product_category(101)

    def test_three_card_legacy_hint_has_full_category_link_preserves_other_answer_and_deduplicates(self):
        reply = "Доставка Новою поштою. Час уточнить менеджер."
        with patch.object(discovery, "capture_product_category", side_effect=lambda pk, locale: self.category(pk)):
            text, urls = discovery.append_catalog_discovery_hint(reply, (101, 102, 103))
            self.assertTrue(text.startswith(reply))
            self.assertIn("Кілька моделей для початку", text)
            self.assertEqual(urls, (self.category()["url"],))
            self.assertEqual(text.count(urls[0]), 1)
            again, _urls = discovery.append_catalog_discovery_hint(text, (101, 102, 103))
            self.assertEqual(again, text)
            self.assertEqual(discovery.catalog_category_discovery([True]).urls, ())

    def test_url_authority_is_relation_derived_and_preserves_current_product_locale(self):
        def capture(pk, locale):
            return self.category(pk, locale=locale)
        client = SimpleNamespace(current_product_id=101, language="ru")
        with patch.object(discovery, "capture_product_category", side_effect=capture):
            expected = ("https://twocomms.test/ru/catalog/hoodie/",)
            self.assertEqual(discovery.category_urls_for_control({"show_products": True}, client), expected)
            self.assertEqual(discovery.category_urls_for_control({"show_products": "101,102", "url": "https://evil.test"}, client), expected)
            self.assertEqual(discovery.category_urls_for_control({"product": "101"}, client), expected)
            self.assertEqual(discovery.category_urls_for_control({"url": "https://evil.test"}, client), ())

    def test_v3_native_third_button_and_photo_fallback_keep_signed_actions_and_strict_metadata(self):
        prepared = self.prepared()
        native = prepared.effects[0]["payload"]["message"]["attachment"]["payload"]["elements"]
        self.assertEqual(len(prepared.manifest["actions"]), 6)
        for card in native:
            self.assertEqual([button["type"] for button in card["buttons"]], ["postback", "postback", "web_url"])
            self.assertEqual(card["buttons"][2], {"type": "web_url", "title": "Всі худі", "url": self.category()["url"]})
        self.assertEqual([row["kind"] for row in prepared.effects[1:-1]], ["image"] * 3)
        fallback = prepared.effects[-1]["payload"]["message"]
        self.assertIn(self.category()["url"], fallback["text"])
        self.assertEqual([row["payload"] for row in fallback["quick_replies"]],
            [button["payload"] for card in native for button in card["buttons"] if "payload" in button])
        self.assertNotIn("https://", prepared.copy_hint)
        self.assertEqual(len(_normalize_specs(prepared.effects, "customer-7")), 5)
        altered = deepcopy(prepared.effects[0])
        altered["payload"]["message"]["attachment"]["payload"]["elements"][0]["buttons"][2]["url"] += "wrong/"
        self.assertFalse(choices.validate_catalog_choice_metadata(altered["projection_metadata"],
            payload=altered["payload"], group=altered["group"], kind=altered["kind"]))

    def test_v1_v2_receipts_rebuild_without_new_category_requirement(self):
        for schema in (choices.SCHEMA_V1, choices.SCHEMA_V2):
            prepared = self.prepared(schema=schema)
            self.assertEqual(len(prepared.effects[0]["payload"]["message"]["attachment"]["payload"]["elements"][0]["buttons"]), 2)
            for effect in prepared.effects:
                self.assertTrue(choices.validate_catalog_choice_metadata(effect["projection_metadata"],
                    payload=effect["payload"], group=effect["group"], kind=effect["kind"]))
        fixture = presentation_fixtures.CatalogChoicePresentationTests()
        with self.assertRaisesRegex(ValueError, "category_invalid"):
            choices.build_catalog_choice_effects(12, fixture.scope(), fixture.cards(), _schema=choices.SCHEMA)

    def test_category_reference_is_content_free_bounded_and_detects_current_relation_changes(self):
        prepared = self.prepared()
        reference = choices.catalog_choice_reference(prepared.manifest)
        bindings = [{"selector": {"catalog_choice_reference": reference}}]
        self.assertLessEqual(len(_canonical(bindings)), 4096)
        self.assertEqual(_safe_bindings(bindings), bindings)
        self.assertNotIn("https://", str(reference))
        category = self.category()
        for value in (category, discovery.category_reference(category)):
            with patch.object(discovery, "capture_product_category", return_value=category):
                self.assertTrue(discovery.validate_current_category(value, product_id=101))
            for key, changed in (("slug", "future-category"), ("name", "Renamed"), ("category_id", 99)):
                current = {**category, key: changed}
                with patch.object(discovery, "capture_product_category", return_value=current):
                    self.assertFalse(discovery.validate_current_category(value, product_id=101))
            with patch.object(discovery, "capture_product_category", side_effect=ValueError("unavailable")):
                self.assertFalse(discovery.validate_current_category(value, product_id=101))

    def test_actual_legacy_hint_hook_does_not_require_truncated_candidates(self):
        function = self.legacy_function("_append_more_products_hint", _assisted_checkout_locale=lambda client: "uk",
            _MORE_PRODUCTS_HINT={"uk": "unused"})
        selection = SimpleNamespace(requested_product_ids=(101, 102, 103), items=(), truncated_count=0)
        with patch.object(discovery, "capture_product_category", side_effect=lambda pk, locale: self.category(pk)):
            text = function("Відповідь на інше питання.", selection, SimpleNamespace())
        self.assertTrue(text.startswith("Відповідь на інше питання."))
        self.assertIn(self.category()["url"], text)

    def test_actual_legacy_truth_hook_adds_only_verified_urls_and_keeps_price_rejection(self):
        @dataclass(frozen=True)
        class Context:
            authorized_urls: tuple = ("https://twocomms.test/product/101/",)
            authorized_prices: tuple = (900,)
            authorized_price_ranges: tuple = ((800, 1000),)
        function = self.legacy_function("_provider_reply_truth_context", _extract_authoritative_price_claim=
            lambda client, text, control: (text, {**control, "_price_claim_invalid": True}, None),
            _assisted_checkout_locale=lambda client: client.language)
        with patch("management.services.ig_reply_authority.build_reply_truth_context", return_value=Context()), \
            patch.object(discovery, "capture_product_category", side_effect=lambda pk, locale: self.category(pk)):
            result = function(SimpleNamespace(language="uk"), {"show_products": "101", "url": "https://evil.test"}, "Інше питання")
        self.assertEqual(result.authorized_urls, ("https://twocomms.test/product/101/", self.category()["url"]))
        self.assertEqual(result.authorized_prices, ())
        self.assertEqual(result.authorized_price_ranges, ())

    def test_sealed_own_source_language_controls_signed_buttons_and_localized_category(self):
        for locale, text, label in (("en", "Please show me your hoodies and answer in English.", "Choose"),
            ("uk", "Покажіть, будь ласка, ваші худі українською.", "Обрати"),
            ("ru", "Покажите, пожалуйста, ваши худи на русском.", "Выбрать")):
            revision = SimpleNamespace(client=SimpleNamespace(pk=7, language="ru" if locale != "ru" else "en"),
                bundle_snapshot={"sources": [{"message_id": 10, "role": "user", "text": text,
                    "provider_created_at": "2026-10-15T10:00:00+00:00"}]})
            with patch("management.services.ig_conversation_routes.conversation_route_reset_floor", return_value=1):
                actual = choices._presentation_locale(revision, SimpleNamespace())
            self.assertEqual(actual, locale)
            fixture = presentation_fixtures.CatalogChoicePresentationTests()
            scope, cards = fixture.scope(), fixture.cards()
            scope["locale"] = actual
            for card in cards:
                card["category"] = self.category(card["product_id"], locale=actual)
            prepared = choices.build_catalog_choice_effects(12, scope, cards, _schema=choices.SCHEMA)
            button = prepared.effects[0]["payload"]["message"]["attachment"]["payload"]["elements"][0]["buttons"]
            self.assertEqual(button[0]["title"], label)
            self.assertEqual(button[2]["url"], self.category(locale=locale)["url"])
            self.assertEqual(revision.client.language, "ru" if locale != "ru" else "en")

    def test_button_caption_foreign_quotes_and_pre_reset_text_do_not_override_own_language(self):
        sources = [{"message_id": 8, "role": "user", "text": "Please reply in Russian."},
            {"message_id": 10, "role": "user", "text": "Please answer in Russian.", "quick_reply_payload": "CC1:12:0:opaque"},
            {"message_id": 11, "role": "assistant", "text": "Please reply in Russian."},
            {"message_id": 12, "role": "user", "text": '"Please answer in Russian."'}]
        revision = SimpleNamespace(client=SimpleNamespace(pk=7, language="ru"), bundle_snapshot={"sources": sources})
        original = deepcopy(sources)
        choice = {"ready": True, "manifest": {"scope": {"locale": "en"}}}
        with patch("management.services.ig_conversation_routes.conversation_route_reset_floor", return_value=10):
            self.assertEqual(choices._presentation_locale(revision, SimpleNamespace(), choice=choice), "en")
            sources.append({"message_id": 13, "role": "user", "text": "Відповідайте українською, будь ласка."})
            self.assertEqual(choices._presentation_locale(revision, SimpleNamespace(), choice=choice), "uk")
        self.assertEqual(sources[:-1], original)

    def test_recovered_locale_requires_exact_sealed_source_and_accepted_historical_admission(self):
        row = SimpleNamespace(pk=10, client_id=7, sender_id="customer-7", role="user", source="webhook",
            status="done", provider_namespace="instagram_login:owner-1", text="Выбрать товар",
            quick_reply_payload="CC1:12:0:opaque", mid="actual-user-mid")
        snapshot = {"message_id": 10, "source_namespace": row.provider_namespace, "source_digest": "a" * 64,
            "text": row.text, "quick_reply_payload": row.quick_reply_payload, "provider_message_id": row.mid}
        source = SimpleNamespace(message=row, message_id=10, ordinal=1, source_namespace=row.provider_namespace,
            source_digest=snapshot["source_digest"])
        revision = SimpleNamespace(client=SimpleNamespace(pk=7, igsid="customer-7", language="ru"),
            bundle_snapshot={"sources": [snapshot]})
        request = SimpleNamespace(source_binding={"native_catalog_choice": {
            "action": {"kind": "product"}, "scope": {"locale": "en"}}})
        from unittest.mock import Mock
        revision.sources = Mock()
        revision.sources.select_related.return_value.order_by.return_value = [source]
        with patch("management.services.ig_turn_revisions._source_payload", return_value={"source_digest": source.source_digest}), \
            patch("management.services.ig_catalog_choice_actions.native_choice_request", return_value=request) as admitted:
            self.assertEqual(choices._admitted_source_locale(revision, reset_floor=1), "en")
            admitted.assert_called_once_with(revision.client, row, historical=True)
            admitted.reset_mock()
            for attribute, value in (("mid", "changed-mid"), ("provider_namespace", "instagram_login:foreign"),
                ("text", "changed-caption"), ("client_id", 99)):
                original = getattr(row, attribute)
                setattr(row, attribute, value)
                self.assertEqual(choices._admitted_source_locale(revision, reset_floor=1), "")
                setattr(row, attribute, original)
            self.assertEqual(choices._admitted_source_locale(revision, reset_floor=11), "")
            admitted.assert_not_called()
            admitted.return_value = None
            self.assertEqual(choices._admitted_source_locale(revision, reset_floor=1), "")
